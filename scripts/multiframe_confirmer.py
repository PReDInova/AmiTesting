"""
Multi-timeframe signal confirmation.

Supports strategies that require signal confirmation across multiple
timeframes (e.g., 1-min Buy signal confirmed by 5-min uptrend).

Works by maintaining multiple bar streams at different intervals and
requiring signals to agree across timeframes before triggering trades.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TimeframeSignal:
    """A signal detected on a specific timeframe."""
    timeframe: str           # e.g. "1min", "5min", "15min"
    signal_type: str         # "Buy", "Sell", "Short", "Cover"
    symbol: str
    timestamp: datetime
    close_price: float
    indicators: dict = field(default_factory=dict)


class MultiframeConfirmer:
    """Confirms signals across multiple timeframes.

    Parameters
    ----------
    timeframes : list[str]
        List of timeframe labels (e.g. ["1min", "5min", "15min"]).
    confirmation_mode : str
        "all" — all timeframes must agree.
        "majority" — majority of timeframes must agree.
        "primary_plus_one" — primary (first) timeframe plus at least one other.
    signal_window_seconds : int
        Maximum age of signals from other timeframes to consider
        as confirming (default: 300s = 5 minutes).
    """

    def __init__(
        self,
        timeframes: list[str],
        confirmation_mode: str = "primary_plus_one",
        signal_window_seconds: int = 300,
    ):
        self._timeframes = timeframes
        self._mode = confirmation_mode
        self._window = signal_window_seconds

        # Store latest signal per timeframe
        self._latest_signals: dict[str, Optional[TimeframeSignal]] = {
            tf: None for tf in timeframes
        }

        logger.info(
            "MultiframeConfirmer initialized: timeframes=%s, mode=%s, window=%ds",
            timeframes, confirmation_mode, signal_window_seconds,
        )

    def update_signal(self, signal: TimeframeSignal) -> None:
        """Update the latest signal for a timeframe."""
        if signal.timeframe in self._latest_signals:
            self._latest_signals[signal.timeframe] = signal
            logger.debug("Updated %s signal: %s %s @ %.2f",
                         signal.timeframe, signal.signal_type,
                         signal.symbol, signal.close_price)

    def check_confirmation(
        self,
        primary_signal: TimeframeSignal,
        reference_time: datetime = None,
    ) -> tuple[bool, dict]:
        """Check if a primary signal is confirmed by other timeframes.

        Parameters
        ----------
        primary_signal : TimeframeSignal
            The signal to confirm (usually from the fastest timeframe).
        reference_time : datetime
            Current time for staleness checking (defaults to now).

        Returns
        -------
        (confirmed, details) : tuple[bool, dict]
            confirmed is True if the signal passes confirmation.
            details contains per-timeframe agreement status.
        """
        if reference_time is None:
            reference_time = datetime.now()

        self.update_signal(primary_signal)

        # Determine if each timeframe agrees
        agreements = {}
        for tf in self._timeframes:
            sig = self._latest_signals.get(tf)
            if sig is None:
                agreements[tf] = {"status": "no_signal", "agrees": False}
                continue

            # Check staleness
            try:
                age = (reference_time - sig.timestamp).total_seconds()
            except TypeError:
                age = 0

            if age > self._window:
                agreements[tf] = {
                    "status": "stale",
                    "agrees": False,
                    "age_seconds": age,
                }
                continue

            # Check agreement: same direction
            agrees = self._signals_agree(primary_signal.signal_type,
                                          sig.signal_type)
            agreements[tf] = {
                "status": "active",
                "agrees": agrees,
                "signal_type": sig.signal_type,
                "age_seconds": age,
            }

        # Apply confirmation mode
        agreeing = [tf for tf, info in agreements.items() if info["agrees"]]

        if self._mode == "all":
            confirmed = len(agreeing) == len(self._timeframes)
        elif self._mode == "majority":
            confirmed = len(agreeing) > len(self._timeframes) / 2
        elif self._mode == "primary_plus_one":
            primary_agrees = agreements.get(primary_signal.timeframe, {}).get("agrees", False)
            other_agrees = any(
                info["agrees"] for tf, info in agreements.items()
                if tf != primary_signal.timeframe
            )
            confirmed = primary_agrees and other_agrees
        else:
            confirmed = False

        return (confirmed, {
            "mode": self._mode,
            "timeframes": agreements,
            "agreeing_count": len(agreeing),
            "total_timeframes": len(self._timeframes),
            "confirmed": confirmed,
        })

    def _signals_agree(self, primary_type: str, other_type: str) -> bool:
        """Check if two signal types agree in direction."""
        bullish = {"Buy", "Cover"}
        bearish = {"Sell", "Short"}
        if primary_type in bullish and other_type in bullish:
            return True
        if primary_type in bearish and other_type in bearish:
            return True
        return False

    def get_state(self) -> dict:
        """Get current state of all timeframe signals."""
        state = {}
        for tf, sig in self._latest_signals.items():
            if sig:
                state[tf] = {
                    "signal_type": sig.signal_type,
                    "symbol": sig.symbol,
                    "close_price": sig.close_price,
                    "timestamp": sig.timestamp.isoformat() if sig.timestamp else None,
                }
            else:
                state[tf] = None
        return state

    def reset(self) -> None:
        """Clear all stored signals."""
        for tf in self._latest_signals:
            self._latest_signals[tf] = None
