"""
Technical indicator computation module -- Sprint 4.

Provides a registry of indicator functions that compute values from OHLCV
bar data.  Each indicator returns TradingView-compatible line data that
the frontend can overlay on the candlestick chart.

Usage::

    from scripts.indicators import compute_indicators
    results = compute_indicators(bars, [
        {"type": "sma", "params": {"period": 20}},
        {"type": "bbands", "params": {"period": 20, "std_dev": 2.0}},
    ])
"""

import logging
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Indicator Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Callable] = {}


def register(name: str):
    """Decorator to register an indicator function."""
    def wrapper(fn):
        _REGISTRY[name] = fn
        return fn
    return wrapper


def get_available_indicators() -> list[dict]:
    """Return metadata about all registered indicators."""
    return [
        {"type": ind_type, "doc": (fn.__doc__ or "").strip()}
        for ind_type, fn in _REGISTRY.items()
    ]


def compute_indicator(bars: list[dict], indicator_type: str, params: dict) -> dict:
    """Compute a single indicator on OHLCV bars.

    Returns a result dict with ``type``, ``label``, ``params``, and either
    ``data`` (single-line) or ``series`` (multi-line like Bollinger Bands).
    On error the dict contains an ``error`` key instead.
    """
    fn = _REGISTRY.get(indicator_type)
    if fn is None:
        return {"type": indicator_type, "params": params,
                "error": f"Unknown indicator: {indicator_type}"}
    try:
        return fn(bars, **params)
    except Exception as exc:
        logger.error("Indicator %s computation failed: %s", indicator_type, exc)
        return {"type": indicator_type, "params": params, "error": str(exc)}


def compute_indicators(bars: list[dict], indicator_configs: list[dict]) -> list[dict]:
    """Compute multiple indicators in batch.

    *indicator_configs* is a list of ``{"type": str, "params": dict}`` dicts.
    """
    return [
        compute_indicator(bars, cfg.get("type", ""), cfg.get("params", {}))
        for cfg in indicator_configs
    ]


# ---------------------------------------------------------------------------
# Helpers: bars <-> pandas
# ---------------------------------------------------------------------------

def _bars_to_series(bars: list[dict], field: str = "close") -> tuple[list[int], pd.Series]:
    """Extract *field* from bars as a pandas Series; also return timestamps."""
    times = [b["time"] for b in bars]
    values = [b[field] for b in bars]
    return times, pd.Series(values, dtype=float)


def _series_to_line_data(times: list[int], series: pd.Series) -> list[dict]:
    """Convert timestamps + Series to ``[{"time": ..., "value": ...}]``, dropping NaN."""
    return [
        {"time": times[i], "value": round(float(val), 2)}
        for i, val in enumerate(series)
        if pd.notna(val)
    ]


# ---------------------------------------------------------------------------
# Indicator implementations
# ---------------------------------------------------------------------------

@register("sma")
def compute_sma(bars: list[dict], period: int = 20, **kwargs) -> dict:
    """Simple Moving Average."""
    times, close = _bars_to_series(bars)
    sma = close.rolling(window=period).mean()
    return {
        "type": "sma",
        "label": f"SMA({period})",
        "params": {"period": period},
        "data": _series_to_line_data(times, sma),
    }


@register("ema")
def compute_ema(bars: list[dict], period: int = 20, **kwargs) -> dict:
    """Exponential Moving Average."""
    times, close = _bars_to_series(bars)
    ema = close.ewm(span=period, adjust=False).mean()
    return {
        "type": "ema",
        "label": f"EMA({period})",
        "params": {"period": period},
        "data": _series_to_line_data(times, ema),
    }


@register("bbands")
def compute_bbands(bars: list[dict], period: int = 20, std_dev: float = 2.0, **kwargs) -> dict:
    """Bollinger Bands (upper, middle, lower)."""
    times, close = _bars_to_series(bars)
    middle = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return {
        "type": "bbands",
        "label": f"BBands({period}, {std_dev})",
        "params": {"period": period, "std_dev": std_dev},
        "series": {
            "upper": _series_to_line_data(times, upper),
            "middle": _series_to_line_data(times, middle),
            "lower": _series_to_line_data(times, lower),
        },
    }


@register("tema")
def compute_tema(bars: list[dict], period: int = 21, **kwargs) -> dict:
    """Triple Exponential Moving Average."""
    times, close = _bars_to_series(bars)
    ema1 = close.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    tema = 3 * ema1 - 3 * ema2 + ema3
    return {
        "type": "tema",
        "label": f"TEMA({period})",
        "params": {"period": period},
        "data": _series_to_line_data(times, tema),
    }


@register("derivative")
def compute_derivative(bars: list[dict], period: int = 21,
                       lookback: int = 8, **kwargs) -> dict:
    """Derivative peak/trough detector on TEMA-smoothed price.

    Computes first derivative (slope) and second derivative (acceleration)
    of a TEMA-smoothed price series.
    """
    period = int(period)
    lookback = int(lookback)
    times, close = _bars_to_series(bars)

    # TEMA smoothing
    ema1 = close.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    tema = 3 * ema1 - 3 * ema2 + ema3

    # First derivative (rate of change / slope)
    first_deriv = (tema - tema.shift(lookback)) / lookback

    # Second derivative (acceleration / curvature)
    second_deriv = first_deriv - first_deriv.shift(lookback)

    return {
        "type": "derivative",
        "label": f"Deriv({period},{lookback})",
        "params": {"period": period, "lookback": lookback},
        "series": {
            "first_deriv": _series_to_line_data(times, first_deriv),
            "second_deriv": _series_to_line_data(times, second_deriv),
        },
    }


@register("adx")
def compute_adx(bars: list[dict], period: int = 14, **kwargs) -> dict:
    """Average Directional Index with +DI/-DI."""
    times, high = _bars_to_series(bars, "high")
    _, low = _bars_to_series(bars, "low")
    _, close = _bars_to_series(bars, "close")

    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Directional Movement
    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(0.0, index=high.index)
    minus_dm = pd.Series(0.0, index=high.index)
    plus_dm[(up_move > 0) & (up_move > down_move)] = up_move
    minus_dm[(down_move > 0) & (down_move > up_move)] = down_move

    # Wilder's smoothing via ewm
    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    smooth_plus_dm = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    smooth_minus_dm = minus_dm.ewm(alpha=alpha, adjust=False).mean()

    plus_di = 100 * smooth_plus_dm / atr
    minus_di = 100 * smooth_minus_dm / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()

    return {
        "type": "adx",
        "label": f"ADX({period})",
        "params": {"period": period},
        "series": {
            "adx": _series_to_line_data(times, adx),
            "plus_di": _series_to_line_data(times, plus_di),
            "minus_di": _series_to_line_data(times, minus_di),
        },
    }


@register("vwap")
def compute_vwap(bars: list[dict], session_reset_hour: int = 18, **kwargs) -> dict:
    """Session VWAP with standard deviation bands."""
    times, high = _bars_to_series(bars, "high")
    _, low = _bars_to_series(bars, "low")
    _, close = _bars_to_series(bars, "close")
    _, volume = _bars_to_series(bars, "volume")

    tp = (high + low + close) / 3.0

    # Detect session boundaries from bar timestamps (Unix epoch seconds)
    bar_hours = pd.Series([
        pd.Timestamp(t, unit="s").hour for t in times
    ], dtype=int)
    prev_hours = bar_hours.shift(1)
    session_start = (bar_hours >= session_reset_hour) & (prev_hours < session_reset_hour)
    # First bar is always a session start
    session_start.iloc[0] = True

    # Assign session IDs
    session_id = session_start.cumsum()

    # Cumulative sums within each session
    tp_vol = tp * volume
    tp2_vol = volume * tp ** 2
    cum_tp_vol = tp_vol.groupby(session_id).cumsum()
    cum_vol = volume.groupby(session_id).cumsum()
    cum_tp2_vol = tp2_vol.groupby(session_id).cumsum()

    vwap = cum_tp_vol / cum_vol
    variance = cum_tp2_vol / cum_vol - vwap ** 2
    # Clamp negative variance from floating point errors
    variance = variance.clip(lower=0)
    dev = variance ** 0.5

    upper1 = vwap + dev
    lower1 = vwap - dev
    upper2 = vwap + 2 * dev
    lower2 = vwap - 2 * dev
    upper3 = vwap + 3 * dev
    lower3 = vwap - 3 * dev

    return {
        "type": "vwap",
        "label": f"VWAP(reset={session_reset_hour}h)",
        "params": {"session_reset_hour": session_reset_hour},
        "series": {
            "vwap": _series_to_line_data(times, vwap),
            "upper1": _series_to_line_data(times, upper1),
            "lower1": _series_to_line_data(times, lower1),
            "upper2": _series_to_line_data(times, upper2),
            "lower2": _series_to_line_data(times, lower2),
            "upper3": _series_to_line_data(times, upper3),
            "lower3": _series_to_line_data(times, lower3),
        },
    }


@register("rsi")
def compute_rsi(bars: list[dict], period: int = 14, **kwargs) -> dict:
    """Relative Strength Index."""
    times, close = _bars_to_series(bars)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)

    return {
        "type": "rsi",
        "label": f"RSI({period})",
        "params": {"period": period},
        "data": _series_to_line_data(times, rsi),
    }


@register("stochastic")
def compute_stochastic(bars: list[dict], k_period: int = 14, d_period: int = 3,
                       smooth: int = 3, **kwargs) -> dict:
    """Stochastic Oscillator (%K, %D)."""
    times, high = _bars_to_series(bars, "high")
    _, low = _bars_to_series(bars, "low")
    _, close = _bars_to_series(bars, "close")

    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()

    raw_k = (close - lowest_low) / (highest_high - lowest_low) * 100
    k = raw_k.rolling(window=smooth).mean()
    d = k.rolling(window=d_period).mean()

    return {
        "type": "stochastic",
        "label": f"Stoch({k_period},{d_period},{smooth})",
        "params": {"k_period": k_period, "d_period": d_period, "smooth": smooth},
        "series": {
            "k": _series_to_line_data(times, k),
            "d": _series_to_line_data(times, d),
        },
    }


@register("donchian")
def compute_donchian(bars: list[dict], period: int = 20, **kwargs) -> dict:
    """Donchian Channel (upper, middle, lower)."""
    times, high = _bars_to_series(bars, "high")
    _, low = _bars_to_series(bars, "low")

    upper = high.rolling(window=period).max()
    lower = low.rolling(window=period).min()
    middle = (upper + lower) / 2

    return {
        "type": "donchian",
        "label": f"Donchian({period})",
        "params": {"period": period},
        "series": {
            "upper": _series_to_line_data(times, upper),
            "middle": _series_to_line_data(times, middle),
            "lower": _series_to_line_data(times, lower),
        },
    }


@register("atr")
def compute_atr(bars: list[dict], period: int = 14, **kwargs) -> dict:
    """Average True Range."""
    times, high = _bars_to_series(bars, "high")
    _, low = _bars_to_series(bars, "low")
    _, close = _bars_to_series(bars, "close")

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()

    return {
        "type": "atr",
        "label": f"ATR({period})",
        "params": {"period": period},
        "data": _series_to_line_data(times, atr),
    }


@register("stdev_bands")
def compute_stdev_bands(bars: list[dict], lookback: int = 30,
                        multiplier: float = 1.0, **kwargs) -> dict:
    """Rolling standard deviation bands around close."""
    times, close = _bars_to_series(bars)
    std = close.rolling(window=lookback).std()
    upper = close + multiplier * std
    lower = close - multiplier * std

    return {
        "type": "stdev_bands",
        "label": f"StdDev({lookback}, {multiplier})",
        "params": {"lookback": lookback, "multiplier": multiplier},
        "series": {
            "upper": _series_to_line_data(times, upper),
            "lower": _series_to_line_data(times, lower),
        },
    }
