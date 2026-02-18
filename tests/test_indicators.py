"""Tests for scripts.indicators -- technical indicator computation."""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.indicators import (
    compute_indicator,
    compute_indicators,
    get_available_indicators,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars(n: int, base_price: float = 100.0, base_time: int = 1700000000):
    """Generate *n* synthetic 1-min bars with a gentle upward drift."""
    bars = []
    for i in range(n):
        c = base_price + i * 0.1
        bars.append({
            "time": base_time + i * 60,
            "open": round(c, 2),
            "high": round(c + 0.5, 2),
            "low": round(c - 0.3, 2),
            "close": round(c + 0.2, 2),
            "volume": 100 + i,
        })
    return bars


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

class TestSMA:
    def test_sma_basic(self):
        bars = _make_bars(30)
        result = compute_indicator(bars, "sma", {"period": 5})
        assert result["type"] == "sma"
        assert "error" not in result
        assert result["label"] == "SMA(5)"
        # First 4 bars have no SMA (warm-up), so data starts at bar 5
        assert len(result["data"]) == 26

    def test_sma_values_correct(self):
        bars = [
            {"time": 1000 + i * 60, "open": 10, "high": 12, "low": 9,
             "close": c, "volume": 100}
            for i, c in enumerate([10, 20, 30, 40, 50])
        ]
        result = compute_indicator(bars, "sma", {"period": 3})
        # SMA(3) at index 2 = (10+20+30)/3 = 20
        assert result["data"][0]["value"] == 20.0
        # SMA(3) at index 3 = (20+30+40)/3 = 30
        assert result["data"][1]["value"] == 30.0

    def test_sma_preserves_timestamps(self):
        bars = _make_bars(10)
        result = compute_indicator(bars, "sma", {"period": 3})
        for pt in result["data"]:
            assert "time" in pt
            assert "value" in pt


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

class TestEMA:
    def test_ema_basic(self):
        bars = _make_bars(30)
        result = compute_indicator(bars, "ema", {"period": 10})
        assert result["type"] == "ema"
        assert "error" not in result
        assert result["label"] == "EMA(10)"
        # EMA produces values from the first bar (ewm)
        assert len(result["data"]) == 30

    def test_ema_values_not_nan(self):
        bars = _make_bars(20)
        result = compute_indicator(bars, "ema", {"period": 5})
        for pt in result["data"]:
            assert pt["value"] is not None


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

class TestBBands:
    def test_bbands_returns_three_series(self):
        bars = _make_bars(30)
        result = compute_indicator(bars, "bbands", {"period": 10, "std_dev": 2.0})
        assert result["type"] == "bbands"
        assert "series" in result
        assert set(result["series"].keys()) == {"upper", "middle", "lower"}

    def test_bbands_ordering(self):
        bars = _make_bars(30)
        result = compute_indicator(bars, "bbands", {"period": 10, "std_dev": 2.0})
        for i, mid in enumerate(result["series"]["middle"]):
            upper_val = result["series"]["upper"][i]["value"]
            lower_val = result["series"]["lower"][i]["value"]
            assert upper_val >= mid["value"] >= lower_val

    def test_bbands_label(self):
        bars = _make_bars(30)
        result = compute_indicator(bars, "bbands", {"period": 20, "std_dev": 2.0})
        assert result["label"] == "BBands(20, 2.0)"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_available_indicators(self):
        available = get_available_indicators()
        types = [ind["type"] for ind in available]
        assert "sma" in types
        assert "ema" in types
        assert "bbands" in types

    def test_unknown_indicator(self):
        bars = _make_bars(10)
        result = compute_indicator(bars, "nonexistent", {})
        assert "error" in result


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

class TestComputeMultiple:
    def test_compute_multiple_indicators(self):
        bars = _make_bars(30)
        configs = [
            {"type": "sma", "params": {"period": 5}},
            {"type": "ema", "params": {"period": 10}},
        ]
        results = compute_indicators(bars, configs)
        assert len(results) == 2
        assert results[0]["type"] == "sma"
        assert results[1]["type"] == "ema"

    def test_empty_configs(self):
        bars = _make_bars(10)
        results = compute_indicators(bars, [])
        assert results == []
