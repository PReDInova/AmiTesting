"""
Live Signal Alert System -- Main Orchestrator.

Streams live market data from ProjectX, injects it into AmiBroker via OLE,
periodically scans for Buy/Short signals from AFL strategies, and fires
alerts when new signals are detected.

Usage::

    python3.13 -m scripts.live_signal_alert
    python3.13 -m scripts.live_signal_alert --symbol NQH6 --interval 1 --scan-interval 60
    python3.13 -m scripts.live_signal_alert --backfill-only
"""

import argparse
import logging
import queue
import signal as signal_mod
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import setup_logging, AMIBROKER_EXE, AMIBROKER_DB_PATH
from config.live_settings import (
    PROJECTX_SYMBOLS,
    PROJECTX_BAR_INTERVAL,
    PROJECTX_BAR_UNIT,
    PROJECTX_INITIAL_DAYS,
    AMIBROKER_INJECT_SYMBOL,
    AMIBROKER_DB,
    SCAN_INTERVAL_SECONDS,
    SCAN_LOOKBACK_BARS,
    SCAN_STRATEGY_AFL_PATH,
    ALERT_CHANNELS,
    ALERT_SOUND_FILE,
    ALERT_WEBHOOK_URL,
    ALERT_DEDUP_WINDOW_SECONDS,
)
from scripts.projectx_feed import ProjectXFeed
from scripts.bar_aggregator import BarData, FeedStatus
from scripts.quote_injector import QuoteInjector
from scripts.signal_scanner import SignalScanner
from scripts.alert_dispatcher import AlertDispatcher, AlertEvent

logger = logging.getLogger(__name__)


class LiveAlertOrchestrator:
    """Main orchestrator for the live signal alert system.

    Coordinates:
    1. ProjectX REST polling feed (async, background thread)
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
        scan_interval: int = None,
        strategy_afl_path: str = None,
        alert_channels: list[str] = None,
        ami_symbol: str = None,
        initial_days: int = None,
        poll_seconds: int = None,
        backfill_only: bool = False,
    ):
        self.symbols = symbols or PROJECTX_SYMBOLS
        self.interval = interval or PROJECTX_BAR_INTERVAL
        self.unit = unit or PROJECTX_BAR_UNIT
        self.scan_interval = scan_interval or SCAN_INTERVAL_SECONDS
        self.strategy_afl_path = strategy_afl_path or SCAN_STRATEGY_AFL_PATH
        self.alert_channels = alert_channels or ALERT_CHANNELS
        self.ami_symbol = ami_symbol or AMIBROKER_INJECT_SYMBOL
        self.initial_days = initial_days or PROJECTX_INITIAL_DAYS
        self.poll_seconds = poll_seconds or SCAN_INTERVAL_SECONDS
        self.backfill_only = backfill_only

        # Shared queues
        self._bar_queue: queue.Queue[BarData] = queue.Queue()
        self._status_queue: queue.Queue[FeedStatus] = queue.Queue()

        # Components (initialized in start())
        self._feed: ProjectXFeed | None = None
        self._injector: QuoteInjector | None = None
        self._scanner: SignalScanner | None = None
        self._dispatcher: AlertDispatcher | None = None

        # State
        self._running = False
        self._last_scan_time = 0.0
        self._bars_injected = 0
        self._scans_run = 0

    def start(self) -> None:
        """Initialize all components and enter the main loop.

        This method blocks until stop() is called or a KeyboardInterrupt.
        """
        setup_logging()
        logger.info("=" * 60)
        logger.info("Live Signal Alert System -- Starting")
        logger.info("  Symbols: %s", self.symbols)
        logger.info("  Interval: %d (unit=%d)", self.interval, self.unit)
        logger.info("  Scan every: %ds", self.scan_interval)
        logger.info("  Strategy: %s", self.strategy_afl_path)
        logger.info("  Alerts: %s", self.alert_channels)
        logger.info("  AmiBroker symbol: %s", self.ami_symbol)
        logger.info("  Backfill only: %s", self.backfill_only)
        logger.info("=" * 60)

        # Initialize COM on main thread
        import pythoncom
        pythoncom.CoInitialize()

        try:
            # 1. Connect quote injector (COM)
            self._injector = QuoteInjector(AMIBROKER_EXE, AMIBROKER_DB)
            if not self._injector.connect():
                logger.error("Cannot connect to AmiBroker. Is it running?")
                return

            # 2. Initialize signal scanner (shares COM connection)
            self._scanner = SignalScanner(
                ab=self._injector.ab,
                strategy_afl_path=self.strategy_afl_path,
                symbol=self.ami_symbol,
                lookback_bars=SCAN_LOOKBACK_BARS,
            )

            # 3. Initialize alert dispatcher
            self._dispatcher = AlertDispatcher(
                channels=self.alert_channels,
                sound_file=ALERT_SOUND_FILE,
                webhook_url=ALERT_WEBHOOK_URL,
                dedup_window=ALERT_DEDUP_WINDOW_SECONDS,
            )

            # 4. Start ProjectX feed (async, background thread)
            self._feed = ProjectXFeed(
                symbols=self.symbols,
                interval=self.interval,
                unit=self.unit,
                initial_days=self.initial_days,
                poll_seconds=self.poll_seconds,
                bar_queue=self._bar_queue,
                status_queue=self._status_queue,
                ami_symbol=self.ami_symbol,
            )
            self._feed.start()

            # 5. Enter main loop
            self._running = True
            self._main_loop()

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
        except Exception as exc:
            logger.exception("Fatal error in orchestrator: %s", exc)
        finally:
            self.stop()

    def stop(self) -> None:
        """Gracefully shut down all components."""
        self._running = False
        logger.info("Shutting down Live Signal Alert System...")

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

            # 2. Process feed status messages
            while not self._status_queue.empty():
                try:
                    status = self._status_queue.get_nowait()
                    level = logging.INFO if status.connected else logging.WARNING
                    logger.log(level, "Feed: %s", status.message)
                except queue.Empty:
                    break

            # 3. Check if backfill-only mode (exit after first batch)
            if self.backfill_only and self._bars_injected > 0:
                logger.info("Backfill complete. %d bars injected.",
                             self._bars_injected)
                self._running = False
                break

            # 4. Check if it's time for a signal scan
            now = time.monotonic()
            if (not self.backfill_only and
                    now - self._last_scan_time >= self.scan_interval and
                    self._bars_injected > 0):
                self._run_scan()
                self._last_scan_time = now

            # 5. Check if feed thread is still alive
            if self._feed and not self._feed.is_alive and self._running:
                logger.warning("Feed thread died. Restarting...")
                self._feed.start()

            # Sleep briefly to avoid busy-wait
            time.sleep(0.5)

    def _run_scan(self) -> None:
        """Execute one signal scan cycle."""
        try:
            self._scans_run += 1
            logger.info("Running signal scan #%d...", self._scans_run)
            signals = self._scanner.scan()

            for sig in signals:
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
                    logger.info("Alert dispatched: %s %s @ %.2f",
                                sig.signal_type, sig.symbol, sig.close_price)

            if signals:
                logger.info("Scan #%d found %d new signal(s).",
                            self._scans_run, len(signals))
            else:
                logger.info("Scan #%d complete -- no new signals.",
                            self._scans_run)

        except Exception as exc:
            logger.error("Signal scan failed: %s", exc)


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
        "--scan-interval", type=int, default=None,
        help="Signal scan interval in seconds (default: 60)")
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
        "--poll-seconds", type=int, default=None,
        help="Seconds between ProjectX REST polls (default: 60)")
    parser.add_argument(
        "--backfill-only", action="store_true",
        help="Only backfill historical data, then exit (no scanning)")

    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else None

    orchestrator = LiveAlertOrchestrator(
        symbols=symbols,
        interval=args.interval,
        unit=args.unit,
        scan_interval=args.scan_interval,
        strategy_afl_path=args.strategy,
        alert_channels=args.alerts,
        ami_symbol=args.ami_symbol,
        initial_days=args.initial_days,
        poll_seconds=args.poll_seconds,
        backfill_only=args.backfill_only,
    )

    # Handle Ctrl+C gracefully
    signal_mod.signal(signal_mod.SIGINT,
                      lambda *_: setattr(orchestrator, '_running', False))

    orchestrator.start()


if __name__ == "__main__":
    main()
