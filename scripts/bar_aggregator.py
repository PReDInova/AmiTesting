"""
Tick-to-bar aggregation for live streaming data.

Accumulates raw ticks and emits completed OHLCV bars at a configured
interval. Used as a fallback when the ProjectX SDK's
ProjectXRealtimeDataManager is not available for bar aggregation.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class BarData:
    """A completed OHLCV bar."""
    symbol: str
    timestamp: datetime        # Bar open time
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: int = 60        # Bar size in seconds


@dataclass
class FeedStatus:
    """Status message from the feed."""
    connected: bool
    message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


class BarAggregator:
    """Accumulates ticks into fixed-interval OHLCV bars.

    Parameters
    ----------
    symbol : str
        Instrument symbol.
    interval : int
        Bar duration in seconds.
    on_bar_complete : callable
        Called with a BarData when a bar closes.
    """

    def __init__(
        self,
        symbol: str,
        interval: int,
        on_bar_complete: Callable[[BarData], None],
    ):
        self.symbol = symbol
        self.interval = interval
        self.on_bar_complete = on_bar_complete
        self._current_bar: Optional[dict] = None
        self._bar_end_time: Optional[datetime] = None

    def on_tick(self, price: float, volume: float,
                timestamp: datetime) -> None:
        """Process an incoming tick."""
        if self._current_bar is None:
            self._start_new_bar(timestamp, price, volume)
            return

        # Check if tick belongs to the current bar or a new one
        if timestamp >= self._bar_end_time:
            self._close_current_bar()
            self._start_new_bar(timestamp, price, volume)
            return

        # Update current bar
        self._current_bar["high"] = max(self._current_bar["high"], price)
        self._current_bar["low"] = min(self._current_bar["low"], price)
        self._current_bar["close"] = price
        self._current_bar["volume"] += volume

    def _start_new_bar(self, timestamp: datetime, price: float,
                       volume: float) -> None:
        """Initialize a new bar period."""
        # Align bar start to interval boundary
        epoch = datetime(2000, 1, 1)
        elapsed = (timestamp - epoch).total_seconds()
        bar_start_secs = (elapsed // self.interval) * self.interval
        bar_start = epoch + timedelta(seconds=bar_start_secs)

        self._bar_end_time = bar_start + timedelta(seconds=self.interval)
        self._current_bar = {
            "timestamp": bar_start,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
        }

    def _close_current_bar(self) -> None:
        """Close the current bar and emit via callback."""
        if self._current_bar is None:
            return

        bar = BarData(
            symbol=self.symbol,
            timestamp=self._current_bar["timestamp"],
            open=self._current_bar["open"],
            high=self._current_bar["high"],
            low=self._current_bar["low"],
            close=self._current_bar["close"],
            volume=self._current_bar["volume"],
            interval=self.interval,
        )
        self.on_bar_complete(bar)
        self._current_bar = None
        self._bar_end_time = None

    def flush(self) -> None:
        """Force-close any in-progress bar (e.g., at market close)."""
        if self._current_bar is not None:
            self._close_current_bar()
