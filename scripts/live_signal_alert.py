"""
Live Signal Alert System -- Main Orchestrator.

Streams live market data from ProjectX, injects it into AmiBroker via OLE,
periodically scans for Buy/Short signals from AFL strategies, and fires
alerts when new signals are detected.

Usage::

    python3.13 -m scripts.live_signal_alert
    python3.13 -m scripts.live_signal_alert --symbol NQH6 --interval 1
    python3.13 -m scripts.live_signal_alert --backfill-only
"""

import argparse
import logging
import queue
import signal as signal_mod
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import setup_logging, AMIBROKER_EXE, AMIBROKER_DB_PATH
from config.live_settings import (
    PROJECTX_SYMBOLS,
    PROJECTX_BAR_INTERVAL,
    PROJECTX_BAR_UNIT,
    PROJECTX_INITIAL_DAYS,
    AMIBROKER_INJECT_SYMBOL,
    SCAN_LOOKBACK_BARS,
    SCAN_STRATEGY_AFL_PATH,
    ALERT_CHANNELS,
    ALERT_SOUND_FILE,
    ALERT_WEBHOOK_URL,
    ALERT_DEDUP_WINDOW_SECONDS,
    TRADE_ENABLED,
    TRADE_SIZE,
    TRADE_TIMEOUT_SECONDS,
    TRADE_FILL_POLL_INTERVAL,
)
from scripts.projectx_feed import ProjectXFeed
from scripts.bar_aggregator import BarData, FeedStatus
from scripts.quote_injector import QuoteInjector
from scripts.signal_scanner import SignalScanner
from scripts.alert_dispatcher import AlertDispatcher, AlertEvent
from scripts.trade_executor import TradeExecutor, TradeRequest
from scripts.portfolio_aggregator import PortfolioAggregator
from scripts.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class LiveAlertOrchestrator:
    """Main orchestrator for the live signal alert system.

    Coordinates:
    1. ProjectX WebSocket feed via TradingSuite (async, background thread)
    2. AmiBroker quote injection (COM, main thread)
    3. Signal scanning via OLE Exploration (COM, main thread)
    4. Alert dispatch

    All COM operations happen on the main thread which calls
    pythoncom.CoInitialize() at startup.
    """

    def __init__(
        self,
        symbols: list[str] = None,
        interval: int = None,
        unit: int = None,
        strategy_afl_path: str = None,
        alert_channels: list[str] = None,
        ami_symbol: str = None,
        initial_days: int = None,
        backfill_only: bool = False,
        account_id: int = None,
        status_callback: Optional[Callable[[str, dict], None]] = None,
        trade_enabled: bool = False,
        trade_symbol: str = None,
        trade_size: int = None,
        trade_timeout: float = None,
        strategies: list[dict] = None,
        paper_mode: bool = True,
    ):
        self.symbols = symbols or PROJECTX_SYMBOLS
        self.interval = interval or PROJECTX_BAR_INTERVAL
        self.unit = unit or PROJECTX_BAR_UNIT
        self.strategy_afl_path = strategy_afl_path or SCAN_STRATEGY_AFL_PATH
        self.alert_channels = alert_channels or ALERT_CHANNELS
        self.ami_symbol = ami_symbol or AMIBROKER_INJECT_SYMBOL
        self.initial_days = initial_days or PROJECTX_INITIAL_DAYS
        self.backfill_only = backfill_only
        self.account_id = account_id
        self._status_callback = status_callback

        # Trade execution
        self._trade_enabled = trade_enabled
        self._trade_symbol = trade_symbol  # defaults to symbols[0] in start()
        self._trade_size = trade_size or TRADE_SIZE
        self._trade_timeout = trade_timeout if trade_timeout is not None else TRADE_TIMEOUT_SECONDS
        self._paper_mode = paper_mode

        # Multi-strategy support: build the strategies list.
        # When ``strategies`` is provided, each entry is a dict with keys:
        #   name, afl_path, priority (int, lower=higher), version_id, strategy_id
        # For backward compatibility, when ``strategies`` is *not* provided
        # we synthesise a single-entry list from the legacy ``strategy_afl_path``.
        if strategies:
            # Sort by priority (lower number = higher priority)
            self._strategies: list[dict] = sorted(
                strategies, key=lambda s: s.get("priority", 0))
        else:
            self._strategies = [{
                "name": Path(self.strategy_afl_path).stem,
                "afl_path": self.strategy_afl_path,
                "priority": 0,
                "version_id": None,
                "strategy_id": None,
            }]

        # Shared queues
        self._bar_queue: queue.Queue[BarData] = queue.Queue()
        self._feed_status_queue: queue.Queue[FeedStatus] = queue.Queue()

        # Components (initialized in start())
        self._feed: ProjectXFeed | None = None
        self._injector: QuoteInjector | None = None
        self._scanner: SignalScanner | None = None  # legacy single-scanner ref
        self._scanners: dict[str, SignalScanner] = {}  # keyed by strategy name
        self._dispatcher: AlertDispatcher | None = None
        self._trade_executor: TradeExecutor | None = None
        self._portfolio_aggregator: PortfolioAggregator | None = None
        self._risk_manager: RiskManager | None = None

        # Per-strategy state: indicator values, alert history, signal counts
        # Keyed by strategy name.
        self._strategy_state: dict[str, dict] = {}

        # State
        self._running = False
        self._last_scan_time = 0.0
        self._bars_injected = 0
        self._scans_run = 0
        self._thread: threading.Thread | None = None

    def _notify(self, event_type: str, data: dict) -> None:
        """Fire a status callback if one is registered."""
        if self._status_callback:
            try:
                self._status_callback(event_type, data)
            except Exception as exc:
                logger.debug("Status callback error: %s", exc)

    def start(self) -> None:
        """Initialize all components and enter the main loop.

        This method blocks until stop() is called or a KeyboardInterrupt.
        """
        setup_logging()
        logger.info("=" * 60)
        logger.info("Live Signal Alert System -- Starting")
        logger.info("  Symbols: %s", self.symbols)
        logger.info("  Interval: %d (unit=%d)", self.interval, self.unit)
        logger.info("  Strategies (%d):", len(self._strategies))
        for strat in self._strategies:
            logger.info("    [p%d] %s  %s",
                        strat.get("priority", 0),
                        strat["name"], strat["afl_path"])
        logger.info("  Alerts: %s", self.alert_channels)
        logger.info("  AmiBroker symbol: %s", self.ami_symbol)
        logger.info("  Account ID: %s", self.account_id)
        logger.info("  Trade enabled: %s", self._trade_enabled)
        logger.info("  Paper mode: %s", self._paper_mode)
        if self._trade_enabled:
            logger.info("  Trade size: %d", self._trade_size)
            logger.info("  Trade timeout: %.0fs", self._trade_timeout)
        logger.info("  Backfill only: %s", self.backfill_only)
        logger.info("=" * 60)

        # Initialize COM on main thread
        import pythoncom
        pythoncom.CoInitialize()

        try:
            # 1. Connect quote injector (COM)
            # Use AmiBroker's currently-loaded database for live streaming
            self._injector = QuoteInjector(AMIBROKER_EXE)
            if not self._injector.connect():
                msg = "Cannot connect to AmiBroker. Is it running?"
                logger.error(msg)
                self._notify("error", {"message": msg})
                return

            # 2. Initialize signal scanners (one per strategy, shares COM)
            # Map ProjectX interval/unit to AmiBroker periodicity
            ami_periodicity = SignalScanner.AMI_PERIODICITY.get(
                (self.unit, self.interval), 5)  # default to 1-min
            logger.info("  Exploration periodicity: %d (unit=%d, interval=%d)",
                        ami_periodicity, self.unit, self.interval)

            for strat in self._strategies:
                sname = strat["name"]
                scanner = SignalScanner(
                    ab=self._injector.ab,
                    strategy_afl_path=strat["afl_path"],
                    symbol=self.ami_symbol,
                    lookback_bars=SCAN_LOOKBACK_BARS,
                    periodicity=ami_periodicity,
                )
                self._scanners[sname] = scanner

                # Initialize per-strategy state
                self._strategy_state[sname] = {
                    "indicators": {},
                    "indicator_time": None,
                    "alert_history": [],
                    "signal_count": 0,
                    "priority": strat.get("priority", 0),
                    "version_id": strat.get("version_id"),
                    "strategy_id": strat.get("strategy_id"),
                }

                logger.info("  Scanner initialised for strategy '%s'", sname)

            # Backward-compat: keep self._scanner pointing at the first
            # (highest-priority) scanner so any external code that
            # referenced it directly still works.
            first_name = self._strategies[0]["name"]
            self._scanner = self._scanners[first_name]

            # 3. Initialize alert dispatcher
            self._dispatcher = AlertDispatcher(
                channels=self.alert_channels,
                sound_file=ALERT_SOUND_FILE,
                webhook_url=ALERT_WEBHOOK_URL,
                dedup_window=ALERT_DEDUP_WINDOW_SECONDS,
            )

            # 4. Initialize portfolio aggregator and risk manager
            self._portfolio_aggregator = PortfolioAggregator(
                strategy_names=[s["name"] for s in self._strategies],
            )
            self._risk_manager = RiskManager()
            logger.info("  PortfolioAggregator and RiskManager initialised.")

            # 5. Start ProjectX feed (async, background thread)
            self._feed = ProjectXFeed(
                symbols=self.symbols,
                interval=self.interval,
                unit=self.unit,
                initial_days=self.initial_days,
                bar_queue=self._bar_queue,
                status_queue=self._feed_status_queue,
                ami_symbol=self.ami_symbol,
                account_id=self.account_id,
            )
            self._feed.start()

            # 6. Start trade executor (if enabled)
            if self._trade_enabled and self.account_id:
                trade_sym = self._trade_symbol or self.symbols[0]
                if self._paper_mode:
                    logger.info("Paper mode enabled — using PaperTradeExecutor.")
                    from scripts.trade_executor import PaperTradeExecutor
                    self._trade_executor = PaperTradeExecutor(
                        account_id=self.account_id,
                        symbol=trade_sym,
                        size=self._trade_size,
                        timeout=self._trade_timeout,
                        poll_interval=TRADE_FILL_POLL_INTERVAL,
                    )
                else:
                    self._trade_executor = TradeExecutor(
                        account_id=self.account_id,
                        symbol=trade_sym,
                        size=self._trade_size,
                        timeout=self._trade_timeout,
                        poll_interval=TRADE_FILL_POLL_INTERVAL,
                    )
                self._trade_executor.start()
                logger.info("Trade executor started for %s (paper=%s)",
                            trade_sym, self._paper_mode)
            elif self._trade_enabled and not self.account_id:
                logger.warning("Trade enabled but no account_id — "
                               "trades will NOT be placed.")

            # 7. Enter main loop
            self._running = True
            self._notify("started", {
                "symbols": self.symbols,
                "ami_symbol": self.ami_symbol,
            })
            self._main_loop()

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
        except Exception as exc:
            logger.exception("Fatal error in orchestrator: %s", exc)
            self._notify("error", {"message": str(exc)})
        finally:
            self.stop()

    def start_background(self) -> threading.Thread:
        """Start the orchestrator in a background daemon thread.

        Returns the thread object for monitoring.
        """
        self._thread = threading.Thread(
            target=self.start, daemon=True, name="LiveAlertOrchestrator")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        """Gracefully shut down all components."""
        self._running = False
        logger.info("Shutting down Live Signal Alert System...")

        # Close cached AnalysisDoc refs and temp files in each scanner
        for sname, scanner in self._scanners.items():
            try:
                scanner.close()
            except Exception:
                pass

        if self._trade_executor:
            self._trade_executor.stop()
        if self._feed:
            self._feed.stop()
        if self._injector:
            self._injector.disconnect()

        alert_count = 0
        if self._dispatcher:
            alert_count = len(self._dispatcher.alert_history)

        logger.info("Shutdown complete. %d bars injected, %d scans run, "
                     "%d alerts dispatched.",
                     self._bars_injected, self._scans_run, alert_count)

        self._notify("stopped", {
            "bars_injected": self._bars_injected,
            "scans_run": self._scans_run,
            "alerts_dispatched": alert_count,
        })

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def bars_injected(self) -> int:
        return self._bars_injected

    @property
    def scans_run(self) -> int:
        return self._scans_run

    @property
    def alerts_dispatched(self) -> int:
        if self._dispatcher:
            return len(self._dispatcher.alert_history)
        return 0

    @property
    def trade_executor(self) -> Optional['TradeExecutor']:
        return self._trade_executor

    def _main_loop(self) -> None:
        """Main processing loop (runs on COM thread).

        Alternates between:
        1. Draining the bar queue and injecting into AmiBroker
        2. Checking if it's time to run a signal scan
        3. Processing feed status messages
        """
        while self._running:
            # 1. Process all available bars
            bars_this_cycle = 0
            while not self._bar_queue.empty():
                try:
                    bar = self._bar_queue.get_nowait()
                    if self._injector.inject_bar(
                        symbol=bar.symbol,
                        timestamp=bar.timestamp,
                        open_=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                    ):
                        self._bars_injected += 1
                        bars_this_cycle += 1
                        if bars_this_cycle <= 3 or bars_this_cycle % 100 == 0:
                            logger.info(
                                "Injected bar: %s %s O=%.2f H=%.2f "
                                "L=%.2f C=%.2f V=%.0f",
                                bar.symbol,
                                bar.timestamp.strftime("%Y-%m-%d %H:%M"),
                                bar.open, bar.high, bar.low,
                                bar.close, bar.volume,
                            )
                except queue.Empty:
                    break

            # Refresh AmiBroker if we injected bars
            if bars_this_cycle > 0:
                self._injector.refresh_all()
                if bars_this_cycle > 3:
                    logger.info("Injected %d bars total this cycle.",
                                bars_this_cycle)
                self._notify("bar_injected", {"count": self._bars_injected})

            # 2. Process feed status messages
            while not self._feed_status_queue.empty():
                try:
                    status = self._feed_status_queue.get_nowait()
                    level = logging.INFO if status.connected else logging.WARNING
                    logger.log(level, "Feed: %s", status.message)
                    self._notify("feed_status", {
                        "connected": status.connected,
                        "message": status.message,
                    })
                except queue.Empty:
                    break

            # 3. Check if backfill-only mode (exit after first batch)
            if self.backfill_only and self._bars_injected > 0:
                logger.info("Backfill complete. %d bars injected.",
                             self._bars_injected)
                self._running = False
                break

            # 4. Scan on new bars (event-driven with cooldown)
            MIN_SCAN_COOLDOWN = 5  # seconds
            now = time.monotonic()
            if (bars_this_cycle > 0
                    and not self.backfill_only
                    and self._bars_injected > 0
                    and now - self._last_scan_time >= MIN_SCAN_COOLDOWN):
                self._run_scans()
                self._last_scan_time = now

            # 5. Process trade results
            if self._trade_executor:
                self._process_trade_results()

            # 6. Check if feed thread is still alive
            if self._feed and not self._feed.is_alive and self._running:
                logger.warning("Feed thread died. Restarting...")
                self._feed.start()

            # Sleep briefly to avoid busy-wait
            time.sleep(0.5)

    def _run_scans(self) -> None:
        """Execute one scan cycle across ALL registered strategies.

        Iterates through scanners in priority order (lowest number first).
        Alerts are dispatched for every new signal.  Signals from all
        strategies are collected by the PortfolioAggregator, then passed
        through the RiskManager before routing to the trade executor.
        """
        try:
            self._scans_run += 1
            logger.info("Running signal scan #%d (%d strategies)...",
                        self._scans_run, len(self._scanners))

            # Collect signals from every strategy for aggregation
            all_scan_signals: list[dict] = []
            all_raw_signals = []  # Signal objects across all strategies

            for strat in self._strategies:
                sname = strat["name"]
                scanner = self._scanners[sname]
                state = self._strategy_state[sname]

                try:
                    signals = scanner.scan()
                except Exception as exc:
                    logger.error("Scan failed for strategy '%s': %s",
                                 sname, exc)
                    self._notify("error", {
                        "message": f"Scan failed ({sname}): {exc}",
                        "strategy": sname,
                    })
                    continue

                # Update per-strategy indicator state
                state["indicators"] = scanner.latest_indicators
                state["indicator_time"] = scanner.latest_indicator_time

                # Emit per-strategy indicator values
                self._notify("indicators", {
                    "strategy": sname,
                    "values": scanner.latest_indicators,
                    "bar_time": scanner.latest_indicator_time,
                })

                for sig in signals:
                    state["signal_count"] += 1
                    event = AlertEvent(
                        signal_type=sig.signal_type,
                        symbol=sig.symbol,
                        timestamp=sig.timestamp,
                        price=sig.close_price,
                        strategy_name=sig.strategy_name,
                        indicator_values=sig.indicator_values,
                    )
                    dispatched = self._dispatcher.dispatch(event)
                    if dispatched:
                        logger.info("Alert dispatched [%s]: %s %s @ %.2f",
                                    sname, sig.signal_type, sig.symbol,
                                    sig.close_price)
                        sig_dict = {
                            "signal_type": sig.signal_type,
                            "symbol": sig.symbol,
                            "price": sig.close_price,
                            "timestamp": sig.timestamp.isoformat(),
                            "strategy": sig.strategy_name,
                        }
                        all_scan_signals.append(sig_dict)
                        state["alert_history"].append(sig_dict)
                        self._notify("alert", sig_dict)

                all_raw_signals.extend(signals)

                if signals:
                    logger.info("  Strategy '%s': %d new signal(s).",
                                sname, len(signals))

            # --- Portfolio aggregation & risk check before trading ---
            if (all_scan_signals
                    and self._trade_executor
                    and self._trade_executor.enabled):
                # Aggregate signals across strategies
                aggregated = self._portfolio_aggregator.aggregate(
                    all_raw_signals)

                # Route each aggregated signal through risk checks
                for agg_sig in aggregated:
                    trade_sym = self._trade_symbol or self.symbols[0]
                    approved, reason = self._risk_manager.check(
                        signal=agg_sig,
                        symbol=trade_sym,
                        size=self._trade_size,
                    )
                    if not approved:
                        logger.warning(
                            "RiskManager rejected trade: %s %s — %s",
                            agg_sig.signal_type, trade_sym, reason)
                        self._notify("risk_rejected", {
                            "signal_type": agg_sig.signal_type,
                            "symbol": trade_sym,
                            "reason": reason,
                            "strategy": agg_sig.strategy_name,
                        })
                        continue

                    trade_req = TradeRequest(
                        signal_type=agg_sig.signal_type,
                        symbol=trade_sym,
                        size=self._trade_size,
                        price=agg_sig.close_price,
                        strategy_name=agg_sig.strategy_name,
                        timestamp=agg_sig.timestamp,
                    )
                    self._trade_executor.submit_trade(trade_req)
                    logger.info(
                        "Trade submitted: %s %s x%d @ %.2f "
                        "[strategy=%s, bar %s]",
                        agg_sig.signal_type, trade_sym,
                        self._trade_size, agg_sig.close_price,
                        agg_sig.strategy_name,
                        agg_sig.timestamp.strftime("%H:%M:%S"))

            self._notify("scan_complete", {
                "scan_num": self._scans_run,
                "signals_found": len(all_raw_signals),
                "new_alerts": len(all_scan_signals),
                "strategies_scanned": len(self._scanners),
            })

            if all_raw_signals:
                logger.info("Scan #%d found %d new signal(s) across %d "
                            "strategies.",
                            self._scans_run, len(all_raw_signals),
                            len(self._scanners))
            else:
                logger.info("Scan #%d complete -- no new signals.",
                            self._scans_run)

        except Exception as exc:
            logger.error("Signal scan cycle failed: %s", exc)
            self._notify("error", {"message": f"Scan cycle failed: {exc}"})

    def _process_trade_results(self) -> None:
        """Drain trade results from the executor and log/notify them."""
        if not self._trade_executor:
            return

        for result in self._trade_executor.get_results():
            req = result.request
            if result.success:
                logger.warning(
                    "TRADE FILLED: %s %s x%d @ %.2f (%.1fs)",
                    req.signal_type, req.symbol,
                    req.size, result.fill_price or 0.0,
                    result.elapsed_seconds)
            else:
                logger.error(
                    "TRADE %s: %s %s — %s",
                    result.status.upper(), req.signal_type,
                    req.symbol, result.error_message or "")

            # Use actual execution time (when the order filled), not
            # the signal's bar timestamp.  The bar timestamp is when the
            # AFL signal fired in AmiBroker; executed_at is when the
            # market order actually filled on the exchange.
            exec_ts = (result.executed_at.isoformat()
                       if result.executed_at
                       else datetime.now().isoformat())
            self._notify("trade", {
                "signal_type": req.signal_type,
                "symbol": req.symbol,
                "size": req.size,
                "order_id": result.order_id,
                "fill_price": result.fill_price,
                "status": result.status,
                "error": result.error_message,
                "elapsed": result.elapsed_seconds,
                "timestamp": exec_ts,
                "signal_timestamp": req.timestamp.isoformat(),
                "strategy": req.strategy_name,
            })


# ======================================================================
# CLI Entry Point
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Live Signal Alert System -- streams ProjectX data "
                    "into AmiBroker and scans for signals",
    )
    parser.add_argument(
        "--symbol", default=None,
        help="ProjectX symbol to stream (default: NQH6)")
    parser.add_argument(
        "--ami-symbol", default=None,
        help="AmiBroker symbol to inject into (default: NQ)")
    parser.add_argument(
        "--interval", type=int, default=None,
        help="Bar interval value (default: 1)")
    parser.add_argument(
        "--unit", type=int, default=None,
        help="Bar unit: 1=Second, 2=Minute, 3=Hour, 4=Day (default: 2)")
    parser.add_argument(
        "--strategy", default=None,
        help="Path to strategy AFL file")
    parser.add_argument(
        "--alerts", nargs="*", default=None,
        help="Alert channels: log desktop sound webhook")
    parser.add_argument(
        "--initial-days", type=int, default=None,
        help="Days of historical data to backfill (default: 2)")
    parser.add_argument(
        "--backfill-only", action="store_true",
        help="Only backfill historical data, then exit (no scanning)")
    parser.add_argument(
        "--account-id", type=int, default=None,
        help="ProjectX account ID to use")
    parser.add_argument(
        "--trade", action="store_true",
        help="Enable trade execution (places real orders)")
    parser.add_argument(
        "--trade-size", type=int, default=None,
        help="Contracts per trade (default: 1)")
    parser.add_argument(
        "--trade-timeout", type=float, default=None,
        help="Seconds before cancelling unfilled order (default: 30)")
    parser.add_argument(
        "--strategies", nargs="*", default=None,
        help="Strategy AFL paths to run simultaneously. Each strategy "
             "gets a separate scanner.  If omitted, --strategy is used.")
    parser.add_argument(
        "--paper-mode", action="store_true", default=True,
        help="Use paper trade executor (default: True)")
    parser.add_argument(
        "--live-mode", action="store_true", default=False,
        help="Use live trade executor (disables paper mode)")

    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else None

    # Build strategies list from --strategies args (if provided)
    strategies = None
    if args.strategies:
        strategies = []
        for idx, afl_path in enumerate(args.strategies):
            strat_name = Path(afl_path).stem
            strategies.append({
                "name": strat_name,
                "afl_path": afl_path,
                "priority": idx,
                "version_id": None,
                "strategy_id": None,
            })

    # --live-mode overrides --paper-mode
    paper_mode = not args.live_mode

    orchestrator = LiveAlertOrchestrator(
        symbols=symbols,
        interval=args.interval,
        unit=args.unit,
        strategy_afl_path=args.strategy,
        alert_channels=args.alerts,
        ami_symbol=args.ami_symbol,
        initial_days=args.initial_days,
        backfill_only=args.backfill_only,
        account_id=args.account_id,
        trade_enabled=args.trade,
        trade_size=args.trade_size,
        trade_timeout=args.trade_timeout,
        strategies=strategies,
        paper_mode=paper_mode,
    )

    # Handle Ctrl+C gracefully
    signal_mod.signal(signal_mod.SIGINT,
                      lambda *_: setattr(orchestrator, '_running', False))

    orchestrator.start()


if __name__ == "__main__":
    main()
