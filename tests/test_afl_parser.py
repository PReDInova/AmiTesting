"""Tests for scripts.afl_parser -- AFL indicator extraction."""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.afl_parser import (
    parse_afl_indicators,
    parse_afl_timeframe,
    parse_afl_params,
    modify_afl_params,
)


# ---------------------------------------------------------------------------
# parse_afl_indicators
# ---------------------------------------------------------------------------

class TestParseIndicators:
    def test_parse_ma_crossover(self):
        afl = """
        fastMA = MA(Close, 5);
        slowMA = MA(Close, 15);
        Buy = Cross(fastMA, slowMA);
        """
        indicators = parse_afl_indicators(afl)
        assert len(indicators) == 2
        types = [i["type"] for i in indicators]
        assert all(t == "sma" for t in types)
        periods = sorted([i["params"]["period"] for i in indicators])
        assert periods == [5, 15]

    def test_parse_ema(self):
        afl = "signal = EMA(Close, 20);"
        indicators = parse_afl_indicators(afl)
        assert len(indicators) == 1
        assert indicators[0]["type"] == "ema"
        assert indicators[0]["params"]["period"] == 20

    def test_parse_bbands(self):
        afl = """
        upper = BBandTop(Close, 20, 2);
        lower = BBandBot(Close, 20, 2);
        """
        indicators = parse_afl_indicators(afl)
        # BBandTop and BBandBot with same params deduplicate to 1
        assert len(indicators) == 1
        assert indicators[0]["type"] == "bbands"
        assert indicators[0]["params"]["period"] == 20
        assert indicators[0]["params"]["std_dev"] == 2.0

    def test_ignores_comments(self):
        afl = """
        // fastMA = MA(Close, 5);
        /* slowMA = MA(Close, 15); */
        real = MA(Close, 10);
        """
        indicators = parse_afl_indicators(afl)
        assert len(indicators) == 1
        assert indicators[0]["params"]["period"] == 10

    def test_no_indicators(self):
        afl = "Buy = 1; Sell = 0; Short = 0; Cover = 0;"
        indicators = parse_afl_indicators(afl)
        assert len(indicators) == 0

    def test_duplicate_dedup(self):
        afl = """
        a = MA(Close, 10);
        b = MA(Close, 10);
        """
        indicators = parse_afl_indicators(afl)
        assert len(indicators) == 1

    def test_mixed_indicators(self):
        afl = """
        sma = MA(Close, 20);
        ema = EMA(Close, 10);
        upper = BBandTop(Close, 20, 2.5);
        lower = BBandBot(Close, 20, 2.5);
        """
        indicators = parse_afl_indicators(afl)
        assert len(indicators) == 3
        types = sorted([i["type"] for i in indicators])
        assert types == ["bbands", "ema", "sma"]

    def test_real_ma_crossover_afl(self):
        """Test against the actual ma_crossover.afl in the project."""
        afl_path = Path(__file__).resolve().parent.parent / "afl" / "ma_crossover.afl"
        if not afl_path.exists():
            pytest.skip("ma_crossover.afl not found")
        afl = afl_path.read_text(encoding="utf-8")
        indicators = parse_afl_indicators(afl)
        assert len(indicators) == 2
        assert all(i["type"] == "sma" for i in indicators)
        periods = sorted([i["params"]["period"] for i in indicators])
        assert periods == [5, 15]


# ---------------------------------------------------------------------------
# parse_afl_timeframe
# ---------------------------------------------------------------------------

class TestParseTimeframe:
    def test_parse_1minute(self):
        assert parse_afl_timeframe("TimeFrameSet(in1Minute);") == 60

    def test_parse_5minute(self):
        assert parse_afl_timeframe("TimeFrameSet(in5Minute);") == 300

    def test_parse_daily(self):
        assert parse_afl_timeframe("TimeFrameSet(inDaily);") == 86400

    def test_no_timeframe(self):
        assert parse_afl_timeframe("Buy = 1; Sell = 0;") is None

    def test_commented_timeframe(self):
        assert parse_afl_timeframe("// TimeFrameSet(in1Minute);") is None

    def test_real_afl_timeframe(self):
        afl_path = Path(__file__).resolve().parent.parent / "afl" / "ma_crossover.afl"
        if not afl_path.exists():
            pytest.skip("ma_crossover.afl not found")
        afl = afl_path.read_text(encoding="utf-8")
        assert parse_afl_timeframe(afl) == 60


# ---------------------------------------------------------------------------
# parse_afl_params
# ---------------------------------------------------------------------------

class TestParseAflParams:
    def test_parse_multiple_params(self):
        afl = """
        temaLength = Param("TEMA Length", 21, 5, 100, 1);
        sdMult     = Param("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1);
        profitMult = Param("Profit Target Mult", 1.0, 0.5, 3.0, 0.1);
        """
        params = parse_afl_params(afl)
        assert len(params) == 3

        assert params[0]["name"] == "TEMA Length"
        assert params[0]["default"] == 21.0
        assert params[0]["min"] == 5.0
        assert params[0]["max"] == 100.0
        assert params[0]["step"] == 1.0
        assert params[0]["type"] == "param"

        assert params[1]["name"] == "StdDev Multiplier"
        assert params[1]["default"] == 1.0
        assert params[1]["min"] == 0.1
        assert params[1]["max"] == 5.0
        assert params[1]["step"] == 0.1

        assert params[2]["name"] == "Profit Target Mult"
        assert params[2]["default"] == 1.0

    def test_commented_out_params_excluded(self):
        afl = """
        // temaLength = Param("TEMA Length", 21, 5, 100, 1);
        /* sdMult = Param("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1); */
        real = Param("Active Param", 10, 1, 50, 1);
        """
        params = parse_afl_params(afl)
        assert len(params) == 1
        assert params[0]["name"] == "Active Param"

    def test_parse_optimize_calls(self):
        afl = """
        temaLength = Optimize("TEMA Length", 21, 5, 100, 1);
        sdMult     = Optimize("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1);
        """
        params = parse_afl_params(afl)
        assert len(params) == 2
        assert params[0]["type"] == "optimize"
        assert params[1]["type"] == "optimize"
        assert params[0]["name"] == "TEMA Length"
        assert params[0]["default"] == 21.0

    def test_mixed_param_and_optimize(self):
        afl = """
        temaLength = Optimize("TEMA Length", 21, 5, 100, 1);
        sdMult     = Param("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1);
        adxPeriod  = Optimize("ADX Period", 14, 7, 28, 1);
        """
        params = parse_afl_params(afl)
        assert len(params) == 3
        assert params[0]["type"] == "optimize"
        assert params[0]["name"] == "TEMA Length"
        assert params[1]["type"] == "param"
        assert params[1]["name"] == "StdDev Multiplier"
        assert params[2]["type"] == "optimize"
        assert params[2]["name"] == "ADX Period"

    def test_empty_content(self):
        assert parse_afl_params("") == []
        assert parse_afl_params("Buy = 1; Sell = 0;") == []


# ---------------------------------------------------------------------------
# modify_afl_params
# ---------------------------------------------------------------------------

class TestModifyAflParams:
    def test_change_default_value(self):
        afl = 'temaLength = Param("TEMA Length", 21, 5, 100, 1);'
        result = modify_afl_params(afl, overrides={"TEMA Length": 30})
        assert 'Param("TEMA Length", 30, 5, 100, 1)' in result

    def test_convert_param_to_optimize(self):
        afl = 'temaLength = Param("TEMA Length", 21, 5, 100, 1);'
        result = modify_afl_params(afl, optimize_names={"TEMA Length"})
        assert 'Optimize("TEMA Length", 21, 5, 100, 1)' in result
        assert "Param" not in result

    def test_overrides_without_optimize_names(self):
        afl = """
        temaLength = Param("TEMA Length", 21, 5, 100, 1);
        sdMult     = Param("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1);
        """
        result = modify_afl_params(afl, overrides={"StdDev Multiplier": 2.5})
        # StdDev default changed
        assert 'Param("StdDev Multiplier", 2.5, 0.1, 5.0, 0.1)' in result
        # TEMA unchanged
        assert 'Param("TEMA Length", 21, 5, 100, 1)' in result

    def test_optimize_names_without_overrides(self):
        afl = """
        temaLength = Param("TEMA Length", 21, 5, 100, 1);
        sdMult     = Param("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1);
        """
        result = modify_afl_params(afl, optimize_names={"TEMA Length"})
        assert 'Optimize("TEMA Length", 21, 5, 100, 1)' in result
        # StdDev stays as Param
        assert 'Param("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1)' in result

    def test_preserve_comments_and_whitespace(self):
        afl = """// Strategy parameters
temaLength = Param("TEMA Length", 21, 5, 100, 1);  // TEMA period
/* Volatility settings */
sdMult = Param("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1);
Buy = 1; Sell = 0;
"""
        result = modify_afl_params(afl, overrides={"TEMA Length": 30})
        # Comments preserved
        assert "// Strategy parameters" in result
        assert "// TEMA period" in result
        assert "/* Volatility settings */" in result
        # Non-param code preserved
        assert "Buy = 1; Sell = 0;" in result
        # Override applied
        assert 'Param("TEMA Length", 30, 5, 100, 1)' in result

    def test_min_max_overrides(self):
        afl = 'temaLength = Param("TEMA Length", 21, 5, 100, 1);'
        result = modify_afl_params(
            afl,
            min_overrides={"TEMA Length": 10},
            max_overrides={"TEMA Length": 50},
        )
        assert 'Param("TEMA Length", 21, 10, 50, 1)' in result

    def test_min_max_with_optimize_and_default(self):
        afl = 'sdMult = Param("StdDev Multiplier", 1.0, 0.1, 5.0, 0.1);'
        result = modify_afl_params(
            afl,
            overrides={"StdDev Multiplier": 2.0},
            optimize_names={"StdDev Multiplier"},
            min_overrides={"StdDev Multiplier": 0.5},
            max_overrides={"StdDev Multiplier": 3.0},
        )
        assert 'Optimize("StdDev Multiplier", 2, 0.5, 3, 0.1)' in result

    def test_step_override(self):
        afl = 'adxPer = Param("ADX Period", 14, 5, 50, 1);'
        result = modify_afl_params(
            afl,
            optimize_names={"ADX Period"},
            min_overrides={"ADX Period": 7},
            max_overrides={"ADX Period": 28},
            step_overrides={"ADX Period": 7},
        )
        assert 'Optimize("ADX Period", 14, 7, 28, 7)' in result
