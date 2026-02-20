"""
Signal replay engine for stepping through historical sessions.

Loads recorded signals, trades, and indicator snapshots from the database
and replays them chronologically.  Supports:
  - Session-based replay  (re-watch a past live session)
  - Condition re-evaluation with different Param() values
  - Side-by-side comparison of actual vs. hypothetical signals
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from scripts.signal_scanner import parse_afl_params, parse_afl_conditions, compute_proximity
from scripts.strategy_db import (
    list_live_signals,
    list_live_trades,
    get_live_session,
)

logger = logging.getLogger(__name__)


@dataclass
class ReplayEvent:
    """A single event in the replay timeline."""
    index: int
    timestamp: str
    event_type: str          # "signal", "trade"
    signal_type: str = ""    # "Buy", "Short", etc.
    symbol: str = ""
    price: float = 0.0
    indicators: dict = field(default_factory=dict)
    conditions: dict = field(default_factory=dict)
    trade_status: str = ""
    fill_price: float = 0.0
    pnl: float | None = None
    was_traded: bool = False
    was_deduped: bool = False
    # Re-evaluation fields
    proximity: list = field(default_factory=list)
    would_signal: bool | None = None


class TradeReplay:
    """Replay engine that steps through a recorded live session.

    Parameters
    ----------
    strategy_afl_path : str
        Path to the AFL strategy file.
    symbol : str
        AmiBroker symbol.
    bar_interval : int
        Bar interval in minutes.
    session_id : str, optional
        UUID of a live session to replay.
    param_overrides : dict, optional
        Custom Param() values for "what-if" analysis.
    """

    def __init__(
        self,
        strategy_afl_path: str = "",
        symbol: str = "NQ",
        bar_interval: int = 1,
        session_id: str = "",
        param_overrides: dict | None = None,
    ):
        self._afl_path = strategy_afl_path
        self._symbol = symbol
        self._bar_interval = bar_interval
        self.session_id = session_id
        self.param_overrides = param_overrides or {}

        # Timeline of events loaded from DB
        self._events: list[ReplayEvent] = []
        self._position: int = 0

        # Parsed AFL for condition re-evaluation
        self._parsed_params: dict = {}
        self._parsed_conditions: list = []
        if strategy_afl_path:
            try:
                afl_content = Path(strategy_afl_path).read_text(encoding="utf-8")
                self._parsed_params = parse_afl_params(afl_content)
                self._parsed_conditions = parse_afl_conditions(afl_content)
                # Apply param overrides
                for pname, pval in self.param_overrides.items():
                    if pname in self._parsed_params:
                        self._parsed_params[pname]["default"] = float(pval)
            except Exception as exc:
                logger.warning("Could not parse AFL for replay: %s", exc)

        # Summary counters
        self._actual_signals = 0
        self._actual_trades = 0
        self._would_signal_count = 0
        self._match_count = 0
        self._mismatch_count = 0

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_bars(
        self,
        start_date: str = None,
        end_date: str = None,
        source: str = "session",
        bars_data: list[dict] = None,
        db_path=None,
    ) -> int:
        """Load events for replay.

        Parameters
        ----------
        source : str
            ``"session"`` — load signals/trades from the DB for *session_id*.
            ``"data"``    — use provided *bars_data*.
            ``"amibroker"``— fetch bars from AmiBroker (requires COM).
        """
        if source == "session" and self.session_id:
            return self._load_session(db_path=db_path)
        if source == "data" and bars_data:
            return self._load_from_bars(bars_data)
        if source == "amibroker":
            return self._load_from_amibroker(start_date, end_date)
        # Fallback: try session
        if self.session_id:
            return self._load_session(db_path=db_path)
        return 0

    def _load_session(self, db_path=None) -> int:
        """Load events from a recorded live session."""
        signals = list_live_signals(session_id=self.session_id, limit=10000, db_path=db_path)
        trades = list_live_trades(session_id=self.session_id, limit=10000, db_path=db_path)

        raw_events: list[dict] = []

        for sig in signals:
            raw_events.append({
                "timestamp": sig.get("signal_at") or sig.get("created_at", ""),
                "event_type": "signal",
                "signal_type": sig.get("signal_type", ""),
                "symbol": sig.get("symbol", ""),
                "price": sig.get("close_price") or 0.0,
                "indicators": sig.get("indicators", {}),
                "conditions": sig.get("conditions", {}),
                "was_traded": bool(sig.get("was_traded")),
                "was_deduped": bool(sig.get("was_deduped")),
            })
            self._actual_signals += 1

        for trade in trades:
            raw_events.append({
                "timestamp": trade.get("signal_at") or trade.get("created_at", ""),
                "event_type": "trade",
                "signal_type": trade.get("signal_type", ""),
                "symbol": trade.get("symbol", ""),
                "price": trade.get("signal_price") or 0.0,
                "indicators": trade.get("indicators", {}),
                "trade_status": trade.get("status", ""),
                "fill_price": trade.get("fill_price") or 0.0,
                "pnl": trade.get("pnl"),
                "was_traded": True,
            })
            self._actual_trades += 1

        raw_events.sort(key=lambda e: e.get("timestamp", ""))
        self._build_events(raw_events)
        return len(self._events)

    def _load_from_bars(self, bars_data: list[dict]) -> int:
        """Load raw bar data as replay events."""
        raw_events = []
        for bar in bars_data:
            raw_events.append({
                "timestamp": bar.get("timestamp", ""),
                "event_type": "signal",
                "signal_type": "",
                "symbol": self._symbol,
                "price": bar.get("close", 0.0),
                "indicators": {},
            })
        raw_events.sort(key=lambda e: e.get("timestamp", ""))
        self._build_events(raw_events)
        return len(self._events)

    def _load_from_amibroker(self, start_date, end_date) -> int:
        """Fetch bars from AmiBroker via OLE."""
        try:
            from scripts.ole_stock_data import StockDataFetcher
            fetcher = StockDataFetcher()
            fetcher.connect()
            df = fetcher.fetch_ohlcv(
                symbol=self._symbol,
                start_date=start_date,
                end_date=end_date,
                interval=self._bar_interval * 60,
            )
            fetcher.disconnect()

            if df is None or df.empty:
                return 0

            bars = []
            for _, row in df.iterrows():
                bars.append({
                    "timestamp": row.name.isoformat() if hasattr(row.name, 'isoformat') else str(row.name),
                    "close": float(row.get("Close", 0)),
                })
            return self._load_from_bars(bars)
        except Exception as exc:
            logger.exception("Failed to fetch bars from AmiBroker: %s", exc)
            return 0

    def _build_events(self, raw_events: list[dict]):
        """Convert raw event dicts to ReplayEvent objects with optional re-evaluation."""
        self._events = []
        for i, raw in enumerate(raw_events):
            evt = ReplayEvent(
                index=i,
                timestamp=raw.get("timestamp", ""),
                event_type=raw["event_type"],
                signal_type=raw.get("signal_type", ""),
                symbol=raw.get("symbol", ""),
                price=raw.get("price", 0.0),
                indicators=raw.get("indicators", {}),
                conditions=raw.get("conditions", {}),
                trade_status=raw.get("trade_status", ""),
                fill_price=raw.get("fill_price", 0.0),
                pnl=raw.get("pnl"),
                was_traded=raw.get("was_traded", False),
                was_deduped=raw.get("was_deduped", False),
            )

            # Re-evaluate conditions if AFL was parsed and indicators available
            if self._parsed_conditions and evt.indicators:
                prox = compute_proximity(
                    self._parsed_conditions,
                    self._parsed_params,
                    evt.indicators,
                )
                evt.proximity = prox
                # Check if any signal type would fire
                evt.would_signal = False
                for sig_type in ("Buy", "Short"):
                    type_conds = [p for p in prox if p["signal"] == sig_type]
                    if type_conds and all(p["met"] for p in type_conds):
                        evt.would_signal = True
                        self._would_signal_count += 1
                        break

                actual_fired = evt.event_type == "signal" and evt.signal_type
                if bool(actual_fired) == evt.would_signal:
                    self._match_count += 1
                else:
                    self._mismatch_count += 1

            self._events.append(evt)
        self._position = 0

    # ------------------------------------------------------------------
    # Stepping
    # ------------------------------------------------------------------

    def step(self, num_bars: int = 1) -> list[dict]:
        """Advance the replay by *num_bars* events and return them."""
        results = []
        for _ in range(num_bars):
            if self._position >= len(self._events):
                break
            results.append(self._event_to_dict(self._events[self._position]))
            self._position += 1
        return results

    def step_back(self, num_bars: int = 1) -> list[dict]:
        """Go backwards in the replay."""
        results = []
        for _ in range(num_bars):
            if self._position <= 0:
                break
            self._position -= 1
            results.append(self._event_to_dict(self._events[self._position]))
        return results

    def step_to_end(self) -> list[dict]:
        """Run through all remaining events."""
        return self.step(len(self._events) - self._position)

    def jump_to(self, position: int) -> list[dict]:
        """Jump to a specific position."""
        position = max(0, min(position, len(self._events) - 1))
        self._position = position
        if self._events:
            return [self._event_to_dict(self._events[self._position])]
        return []

    def reset(self):
        """Go back to the beginning."""
        self._position = 0

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def get_all_events(self) -> list[dict]:
        """Return all events as dicts (for full timeline display)."""
        return [self._event_to_dict(e) for e in self._events]

    @property
    def progress(self) -> dict:
        total = len(self._events)
        return {
            "current": self._position,
            "total": total,
            "pct": round(self._position / total * 100, 1) if total else 0,
            "done": self._position >= total,
        }

    @property
    def results(self) -> list[dict]:
        """Events processed so far."""
        return [self._event_to_dict(e) for e in self._events[:self._position]]

    def get_summary(self) -> dict:
        """Return summary of the replay."""
        signal_types: dict[str, int] = {}
        trade_statuses: dict[str, int] = {}
        total_pnl = 0.0
        for evt in self._events:
            if evt.event_type == "signal" and evt.signal_type:
                signal_types[evt.signal_type] = signal_types.get(evt.signal_type, 0) + 1
            elif evt.event_type == "trade":
                trade_statuses[evt.trade_status] = trade_statuses.get(evt.trade_status, 0) + 1
                if evt.pnl is not None:
                    total_pnl += evt.pnl

        return {
            "session_id": self.session_id,
            "total_events": len(self._events),
            "actual_signals": self._actual_signals,
            "actual_trades": self._actual_trades,
            "signal_types": signal_types,
            "trade_statuses": trade_statuses,
            "total_pnl": round(total_pnl, 2),
            "total_bars": len(self._events),
            "bars_processed": self._position,
            "would_signal_count": self._would_signal_count,
            "match_count": self._match_count,
            "mismatch_count": self._mismatch_count,
            "accuracy_pct": round(
                self._match_count / (self._match_count + self._mismatch_count) * 100, 1
            ) if (self._match_count + self._mismatch_count) else None,
            "progress": self.progress,
            "strategy": self._afl_path,
            "symbol": self._symbol,
        }

    def _event_to_dict(self, evt: ReplayEvent) -> dict:
        """Convert a ReplayEvent to a JSON-serializable dict."""
        return {
            "index": evt.index,
            "timestamp": evt.timestamp,
            "event_type": evt.event_type,
            "signal_type": evt.signal_type,
            "symbol": evt.symbol,
            "price": evt.price,
            "indicators": evt.indicators,
            "trade_status": evt.trade_status,
            "fill_price": evt.fill_price,
            "pnl": evt.pnl,
            "was_traded": evt.was_traded,
            "was_deduped": evt.was_deduped,
            "proximity": evt.proximity,
            "would_signal": evt.would_signal,
        }
