"""
Market regime detection for adaptive strategy management.

Classifies current market conditions into regimes (trending, ranging,
volatile, quiet) and recommends which strategies should be active.

Uses indicator-based classification that can run on AmiBroker data
or standalone price data.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RegimeState:
    """Current market regime classification."""
    regime: str           # "trending", "ranging", "volatile", "quiet"
    confidence: float     # 0.0 to 1.0
    adx_value: float      # Average Directional Index
    volatility_pct: float # Realized volatility as % of price
    trend_direction: str  # "up", "down", "flat"
    timestamp: datetime = None


class RegimeDetector:
    """Detect market regime from price data.

    Uses a combination of:
    - ADX for trend strength
    - ATR/price ratio for volatility
    - Price vs moving average for trend direction

    Parameters
    ----------
    adx_period : int
        Period for ADX calculation (default: 14).
    atr_period : int
        Period for ATR calculation (default: 14).
    ma_period : int
        Period for trend direction MA (default: 50).
    trending_adx_threshold : float
        ADX above this = trending market (default: 25).
    high_vol_threshold : float
        ATR/price ratio above this = high volatility (default: 0.02).
    low_vol_threshold : float
        ATR/price ratio below this = low volatility (default: 0.005).
    """

    def __init__(
        self,
        adx_period: int = 14,
        atr_period: int = 14,
        ma_period: int = 50,
        trending_adx_threshold: float = 25.0,
        high_vol_threshold: float = 0.02,
        low_vol_threshold: float = 0.005,
    ):
        self._adx_period = adx_period
        self._atr_period = atr_period
        self._ma_period = ma_period
        self._trending_threshold = trending_adx_threshold
        self._high_vol = high_vol_threshold
        self._low_vol = low_vol_threshold

        self._current_regime: Optional[RegimeState] = None
        self._history: list[RegimeState] = []

        logger.info(
            "RegimeDetector initialized: ADX period=%d, threshold=%.1f, "
            "vol thresholds=(%.4f, %.4f)",
            adx_period, trending_adx_threshold,
            low_vol_threshold, high_vol_threshold,
        )

    @property
    def current_regime(self) -> Optional[RegimeState]:
        return self._current_regime

    @property
    def history(self) -> list[RegimeState]:
        return self._history

    def classify(self, bars: list[dict]) -> RegimeState:
        """Classify the current market regime from bar data.

        Parameters
        ----------
        bars : list[dict]
            List of OHLCV bar dicts with keys: open, high, low, close.
            Must have at least max(adx_period, atr_period, ma_period) + 1 bars.

        Returns
        -------
        RegimeState
            The classified regime.
        """
        if len(bars) < max(self._adx_period, self._atr_period, self._ma_period) + 1:
            return RegimeState(
                regime="unknown",
                confidence=0.0,
                adx_value=0.0,
                volatility_pct=0.0,
                trend_direction="flat",
                timestamp=datetime.now(),
            )

        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]

        # Calculate ADX
        adx = self._calculate_adx(highs, lows, closes, self._adx_period)

        # Calculate ATR-based volatility
        atr = self._calculate_atr(highs, lows, closes, self._atr_period)
        current_price = closes[-1]
        vol_pct = atr / current_price if current_price > 0 else 0

        # Calculate trend direction via MA
        ma = sum(closes[-self._ma_period:]) / self._ma_period
        if current_price > ma * 1.005:
            trend_dir = "up"
        elif current_price < ma * 0.995:
            trend_dir = "down"
        else:
            trend_dir = "flat"

        # Classify regime
        is_trending = adx > self._trending_threshold
        is_high_vol = vol_pct > self._high_vol
        is_low_vol = vol_pct < self._low_vol

        if is_trending and not is_high_vol:
            regime = "trending"
            confidence = min(adx / 50.0, 1.0)  # Scale ADX to confidence
        elif is_high_vol:
            regime = "volatile"
            confidence = min(vol_pct / (self._high_vol * 2), 1.0)
        elif is_low_vol and not is_trending:
            regime = "quiet"
            confidence = 1.0 - (vol_pct / self._low_vol)
        else:
            regime = "ranging"
            confidence = 1.0 - min(adx / self._trending_threshold, 1.0)

        state = RegimeState(
            regime=regime,
            confidence=round(confidence, 3),
            adx_value=round(adx, 2),
            volatility_pct=round(vol_pct, 6),
            trend_direction=trend_dir,
            timestamp=datetime.now(),
        )

        self._current_regime = state
        self._history.append(state)

        # Keep history bounded
        if len(self._history) > 1000:
            self._history = self._history[-500:]

        logger.debug("Regime: %s (confidence=%.2f, ADX=%.1f, vol=%.4f%%)",
                      regime, confidence, adx, vol_pct * 100)

        return state

    def get_strategy_recommendations(
        self,
        strategy_types: dict[str, str] = None,
    ) -> dict[str, bool]:
        """Recommend which strategies should be active for current regime.

        Parameters
        ----------
        strategy_types : dict[str, str]
            Mapping of strategy_name → strategy_type.
            Types: "trend_following", "mean_reversion", "breakout",
                   "scalping", "all_weather".

        Returns
        -------
        dict[str, bool]
            Mapping of strategy_name → should_be_active.
        """
        if not self._current_regime or not strategy_types:
            return {name: True for name in (strategy_types or {})}

        regime = self._current_regime.regime
        recommendations = {}

        # Regime-to-strategy-type mapping
        active_types = {
            "trending": {"trend_following", "breakout", "all_weather"},
            "ranging": {"mean_reversion", "scalping", "all_weather"},
            "volatile": {"breakout", "all_weather"},
            "quiet": {"mean_reversion", "scalping", "all_weather"},
            "unknown": {"all_weather"},
        }

        allowed = active_types.get(regime, set())

        for name, stype in strategy_types.items():
            recommendations[name] = stype in allowed

        return recommendations

    # ------------------------------------------------------------------
    # Technical indicator calculations
    # ------------------------------------------------------------------

    def _calculate_atr(
        self, highs: list, lows: list, closes: list, period: int
    ) -> float:
        """Calculate Average True Range."""
        trs = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)

        if len(trs) < period:
            return sum(trs) / len(trs) if trs else 0

        # Wilder's smoothing
        atr = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[i]) / period

        return atr

    def _calculate_adx(
        self, highs: list, lows: list, closes: list, period: int
    ) -> float:
        """Calculate Average Directional Index (simplified)."""
        plus_dm = []
        minus_dm = []
        trs = []

        for i in range(1, len(highs)):
            up = highs[i] - highs[i - 1]
            down = lows[i - 1] - lows[i]

            plus_dm.append(up if up > down and up > 0 else 0)
            minus_dm.append(down if down > up and down > 0 else 0)

            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)

        if len(trs) < period:
            return 0

        # Smoothed DI+, DI-, DX
        smooth_plus = sum(plus_dm[:period])
        smooth_minus = sum(minus_dm[:period])
        smooth_tr = sum(trs[:period])

        dx_values = []
        for i in range(period, len(trs)):
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]
            smooth_tr = smooth_tr - smooth_tr / period + trs[i]

            if smooth_tr == 0:
                continue

            di_plus = 100 * smooth_plus / smooth_tr
            di_minus = 100 * smooth_minus / smooth_tr
            di_sum = di_plus + di_minus

            if di_sum == 0:
                dx_values.append(0)
            else:
                dx_values.append(100 * abs(di_plus - di_minus) / di_sum)

        if not dx_values:
            return 0

        # ADX is smoothed DX
        if len(dx_values) < period:
            return sum(dx_values) / len(dx_values)

        adx = sum(dx_values[:period]) / period
        for i in range(period, len(dx_values)):
            adx = (adx * (period - 1) + dx_values[i]) / period

        return adx
