"""
Tests for scripts.afl_validator -- AFL pre-validation and post-validation.

Ensures that bad AFL scripts are caught before and after backtest execution,
since AmiBroker's OLE interface does not report formula errors.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.afl_validator import (
    validate_afl,
    validate_afl_file,
    validate_backtest_results,
    auto_fix_afl,
)


# =========================================================================
# Pre-validation: validate_afl
# =========================================================================

class TestValidateAflGoodScripts:
    """Scripts that should pass validation."""

    def test_complete_long_only_strategy(self):
        afl = (
            "Buy = Cross(MA(Close,10), MA(Close,50));\n"
            "Sell = Cross(MA(Close,50), MA(Close,10));\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is True
        assert errors == []

    def test_complete_long_short_strategy(self):
        afl = (
            "Buy = Cross(MA(Close,10), MA(Close,50));\n"
            "Sell = Cross(MA(Close,50), MA(Close,10));\n"
            "Short = Cross(MA(Close,50), MA(Close,10));\n"
            "Cover = Cross(MA(Close,10), MA(Close,50));\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is True

    def test_with_comments_and_plots(self):
        afl = (
            "// My strategy\n"
            "Buy = 1;\n"
            "Sell = 1;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            "Plot(Close, \"Price\", colorDefault, styleLine);\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is True

    def test_real_ma_crossover_file(self):
        """The actual project AFL should pass validation."""
        from config.settings import AFL_STRATEGY_FILE
        valid, errors = validate_afl_file(str(AFL_STRATEGY_FILE))
        assert valid is True, f"Real AFL failed validation: {errors}"


# =========================================================================
# Pre-validation: missing variables (Error 702)
# =========================================================================

class TestValidateAflMissingVars:
    """Scripts missing required trading variables."""

    def test_missing_short_cover(self):
        """The exact Error 702 scenario -- Buy/Sell present but no Short/Cover."""
        afl = (
            "Buy = Cross(MA(Close,10), MA(Close,50));\n"
            "Sell = Cross(MA(Close,50), MA(Close,10));\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert len(errors) == 2
        assert any("Short" in e for e in errors)
        assert any("Cover" in e for e in errors)
        assert any("702" in e for e in errors)

    def test_missing_buy_sell(self):
        afl = (
            "Short = Cross(MA(Close,50), MA(Close,10));\n"
            "Cover = Cross(MA(Close,10), MA(Close,50));\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("Buy" in e for e in errors)
        assert any("Sell" in e for e in errors)

    def test_missing_all_four(self):
        afl = "Plot(Close, \"Price\", colorDefault, styleLine);\n"
        valid, errors = validate_afl(afl)
        assert valid is False
        assert len(errors) == 4  # Buy, Sell, Short, Cover

    def test_missing_only_cover(self):
        afl = (
            "Buy = 1;\n"
            "Sell = 1;\n"
            "Short = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert len(errors) == 1
        assert "Cover" in errors[0]


# =========================================================================
# Pre-validation: syntax issues
# =========================================================================

class TestValidateAflSyntax:
    """Scripts with syntax problems."""

    def test_empty_formula(self):
        valid, errors = validate_afl("")
        assert valid is False
        assert any("empty" in e.lower() for e in errors)

    def test_comments_only(self):
        afl = "// This is just a comment\n/* nothing here */"
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("empty" in e.lower() for e in errors)

    def test_no_semicolons(self):
        afl = (
            "Buy = 1\n"
            "Sell = 1\n"
            "Short = 0\n"
            "Cover = 0\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("semicolon" in e.lower() for e in errors)

    def test_unmatched_parentheses(self):
        afl = (
            "Buy = Cross(MA(Close,10), MA(Close,50);\n"
            "Sell = 1;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("parenthes" in e.lower() for e in errors)

    def test_unicode_characters(self):
        """AFL with non-ISO-8859-1 characters (e.g. em-dash) should fail."""
        afl = (
            "// Strategy \u2014 with em-dash\n"
            "Buy = 1;\n"
            "Sell = 1;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("ISO-8859-1" in e for e in errors)


# =========================================================================
# Pre-validation: file-based
# =========================================================================

class TestValidateAflFile:

    def test_nonexistent_file(self, tmp_path):
        valid, errors = validate_afl_file(str(tmp_path / "missing.afl"))
        assert valid is False
        assert any("not found" in e for e in errors)

    def test_valid_file(self, tmp_path):
        afl_file = tmp_path / "good.afl"
        afl_file.write_text(
            "Buy = 1;\nSell = 1;\nShort = 0;\nCover = 0;\n",
            encoding="utf-8",
        )
        valid, errors = validate_afl_file(str(afl_file))
        assert valid is True


# =========================================================================
# Post-validation: validate_backtest_results
# =========================================================================

class TestValidateBacktestResults:
    """Ensure empty/missing results are flagged after a backtest."""

    def test_csv_not_found(self, tmp_path):
        valid, warnings = validate_backtest_results(
            str(tmp_path / "missing.csv")
        )
        assert valid is False
        assert any("not found" in w for w in warnings)

    def test_csv_empty(self, tmp_path):
        csv = tmp_path / "empty.csv"
        csv.write_text("", encoding="utf-8")
        valid, warnings = validate_backtest_results(str(csv))
        assert valid is False
        assert any("empty" in w.lower() for w in warnings)

    def test_csv_header_only(self, tmp_path):
        """Header row but no trade data -- Buy/Sell never triggered."""
        csv = tmp_path / "header_only.csv"
        csv.write_text("Symbol,Trade,Date,Price,Profit\n", encoding="utf-8")
        valid, warnings = validate_backtest_results(str(csv))
        assert valid is False
        assert any("0 trades" in w for w in warnings)

    def test_csv_with_trades(self, tmp_path):
        csv = tmp_path / "good.csv"
        csv.write_text(
            "Symbol,Trade,Date,Price,Profit\n"
            "GCZ25,Long,2025-06-01,2350.00,500.00\n",
            encoding="utf-8",
        )
        valid, warnings = validate_backtest_results(str(csv))
        assert valid is True
        assert warnings == []

    def test_html_missing_is_warning(self, tmp_path):
        csv = tmp_path / "good.csv"
        csv.write_text(
            "Symbol,Trade,Date,Price,Profit\n"
            "GCZ25,Long,2025-06-01,2350.00,500.00\n",
            encoding="utf-8",
        )
        valid, warnings = validate_backtest_results(
            str(csv),
            html_path=str(tmp_path / "missing.html"),
        )
        assert valid is False
        assert any("HTML" in w for w in warnings)

    def test_html_empty_is_warning(self, tmp_path):
        csv = tmp_path / "good.csv"
        csv.write_text(
            "Symbol,Trade,Date,Price,Profit\n"
            "GCZ25,Long,2025-06-01,2350.00,500.00\n",
            encoding="utf-8",
        )
        html = tmp_path / "empty.html"
        html.write_text("", encoding="utf-8")
        valid, warnings = validate_backtest_results(
            str(csv), html_path=str(html)
        )
        assert valid is False
        assert any("HTML" in w and "empty" in w.lower() for w in warnings)

    def test_both_files_valid(self, tmp_path):
        csv = tmp_path / "good.csv"
        csv.write_text(
            "Symbol,Trade,Date,Price,Profit\n"
            "GCZ25,Long,2025-06-01,2350.00,500.00\n",
            encoding="utf-8",
        )
        html = tmp_path / "good.html"
        html.write_text("<html><body>Results</body></html>", encoding="utf-8")
        valid, warnings = validate_backtest_results(
            str(csv), html_path=str(html)
        )
        assert valid is True
        assert warnings == []


# =========================================================================
# ERR-001: Reserved function names used as variables
# =========================================================================

class TestReservedFunctionNames:
    """AFL is case-insensitive -- built-in names cannot be variable names."""

    def test_tema_as_variable(self):
        """The exact Error 31 scenario from the knowledge base."""
        afl = (
            "ema1 = EMA(Close, 21);\n"
            "ema2 = EMA(ema1, 21);\n"
            "ema3 = EMA(ema2, 21);\n"
            "tema = 3 * ema1 - 3 * ema2 + ema3;\n"
            "Buy = Cross(tema, Close);\n"
            "Sell = 0;\n"
            "Short = Cross(Close, tema);\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("tema" in e.lower() and "reserved" in e.lower() for e in errors)
        assert any("Error 31" in e for e in errors)

    def test_ema_as_variable(self):
        """EMA is a built-in function -- cannot use as variable name."""
        afl = (
            "ema = EMA(Close, 14);\n"
            "Buy = Cross(ema, Close);\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("ema" in e.lower() and "reserved" in e.lower() for e in errors)

    def test_rsi_as_variable(self):
        afl = (
            "rsi = RSI(14);\n"
            "Buy = rsi < 30;\n"
            "Sell = rsi > 70;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("rsi" in e.lower() for e in errors)

    def test_safe_variable_names_pass(self):
        """Variables like emaVal, temaLine, rsiValue should pass."""
        afl = (
            "emaVal = EMA(Close, 14);\n"
            "temaLine = 3 * emaVal;\n"
            "rsiValue = RSI(14);\n"
            "Buy = Cross(emaVal, Close);\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is True

    def test_case_insensitive_detection(self):
        """TEMA, Tema, tema should all be caught."""
        afl = (
            "TEMA = EMA(Close, 21);\n"
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("TEMA" in e for e in errors)

    def test_trading_vars_not_flagged(self):
        """Buy, Sell, Short, Cover are also built-in but should NOT be flagged."""
        afl = (
            "Buy = Cross(MA(Close,10), MA(Close,50));\n"
            "Sell = Cross(MA(Close,50), MA(Close,10));\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is True

    def test_multiple_reserved_names(self):
        """Multiple violations should each be reported."""
        afl = (
            "tema = EMA(Close, 21);\n"
            "macd = MACD();\n"
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        reserved_errors = [e for e in errors if "reserved" in e.lower()]
        assert len(reserved_errors) >= 2


# =========================================================================
# ERR-002: Invalid color constants
# =========================================================================

class TestInvalidColorConstants:
    """AmiBroker has a specific set of valid color constants."""

    def test_colorCyan_invalid(self):
        """The exact Error 29 scenario from the knowledge base."""
        afl = (
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            'Plot(Close, "Price", colorCyan, styleLine);\n'
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("colorCyan" in e for e in errors)
        assert any("colorAqua" in e for e in errors)  # should suggest fix

    def test_colorMagenta_invalid(self):
        afl = (
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            'Plot(Close, "Price", colorMagenta, styleLine);\n'
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("colorMagenta" in e for e in errors)
        assert any("colorViolet" in e for e in errors)

    def test_colorPurple_invalid(self):
        afl = (
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            'Plot(Close, "Price", colorPurple, styleLine);\n'
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("colorPurple" in e for e in errors)

    def test_valid_colors_pass(self):
        """All standard AmiBroker colors should pass."""
        afl = (
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            'Plot(Close, "Price", colorAqua, styleLine);\n'
            'Plot(Close, "Price2", colorRed, styleLine);\n'
            'Plot(Close, "Price3", colorDefault, styleLine);\n'
        )
        valid, errors = validate_afl(afl)
        assert valid is True

    def test_color_in_comment_not_flagged(self):
        """Colors in comments should not be flagged."""
        afl = (
            "// Use colorCyan for the line\n"
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            'Plot(Close, "Price", colorAqua, styleLine);\n'
        )
        valid, errors = validate_afl(afl)
        assert valid is True


# =========================================================================
# ERR-004: Dangerous #include_once paths
# =========================================================================

class TestIncludePaths:

    def test_backslash_tab_in_path(self):
        afl = (
            '#include_once "C:\\Users\\test\\indicators\\tema.afl"\n'
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is False
        assert any("#include_once" in e for e in errors)
        assert any("escape" in e.lower() for e in errors)

    def test_forward_slash_path_passes(self):
        afl = (
            "smoothingLength = 21;\n"
            "sourcePrice = Close;\n"
            '#include_once "C:/Users/test/indicators/tema.afl"\n'
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is True

    def test_safe_backslash_path_passes(self):
        """Paths without dangerous escape sequences should pass."""
        afl = (
            '#include_once "C:\\Users\\docs\\strategy.afl"\n'
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        valid, errors = validate_afl(afl)
        assert valid is True


# =========================================================================
# Auto-fix: auto_fix_afl
# =========================================================================

class TestAutoFixAfl:
    """auto_fix_afl should correct known errors and return change descriptions."""

    def test_fix_reserved_variable_name(self):
        afl = (
            "ema1 = EMA(Close, 21);\n"
            "ema2 = EMA(ema1, 21);\n"
            "ema3 = EMA(ema2, 21);\n"
            "tema = 3 * ema1 - 3 * ema2 + ema3;\n"
            "Buy = Cross(tema, Close);\n"
            "Sell = 0;\n"
            "Short = Cross(Close, tema);\n"
            "Cover = 0;\n"
        )
        fixed, changes = auto_fix_afl(afl)
        assert len(changes) >= 1
        assert any("tema" in c.lower() for c in changes)
        # The fixed version should use temaVal instead of tema
        assert "temaVal" in fixed
        # Original reserved name should not appear as a variable
        assert "tema =" not in fixed
        # But EMA() function calls should still be present
        assert "EMA(" in fixed
        # Fixed version should pass validation
        valid, errors = validate_afl(fixed)
        assert valid is True, f"Fixed AFL still fails: {errors}"

    def test_fix_invalid_color(self):
        afl = (
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            'Plot(Close, "Price", colorCyan, styleLine);\n'
        )
        fixed, changes = auto_fix_afl(afl)
        assert len(changes) >= 1
        assert any("colorCyan" in c for c in changes)
        assert "colorAqua" in fixed
        assert "colorCyan" not in fixed
        valid, errors = validate_afl(fixed)
        assert valid is True, f"Fixed AFL still fails: {errors}"

    def test_fix_include_path(self):
        afl = (
            '#include_once "C:\\Users\\test\\indicators\\tema.afl"\n'
            "Buy = 1;\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        fixed, changes = auto_fix_afl(afl)
        assert len(changes) >= 1
        assert any("forward slash" in c.lower() for c in changes)
        assert "C:/Users/test/indicators/tema.afl" in fixed

    def test_fix_multiple_errors(self):
        """Should fix all known errors in one pass."""
        afl = (
            "tema = EMA(Close, 21);\n"
            "Buy = Cross(tema, Close);\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            'Plot(tema, "TEMA", colorCyan, styleLine);\n'
        )
        fixed, changes = auto_fix_afl(afl)
        assert len(changes) >= 2
        assert "temaVal" in fixed
        assert "colorAqua" in fixed
        valid, errors = validate_afl(fixed)
        assert valid is True, f"Fixed AFL still fails: {errors}"

    def test_no_fix_needed(self):
        """Clean AFL should be returned unchanged."""
        afl = (
            "emaVal = EMA(Close, 14);\n"
            "Buy = Cross(emaVal, Close);\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
            'Plot(Close, "Price", colorAqua, styleLine);\n'
        )
        fixed, changes = auto_fix_afl(afl)
        assert changes == []
        assert fixed == afl

    def test_fix_preserves_function_calls(self):
        """EMA() function calls should NOT be renamed, only variable assignments."""
        afl = (
            "ema = EMA(Close, 14);\n"
            "Buy = Cross(ema, Close);\n"
            "Sell = 0;\n"
            "Short = 0;\n"
            "Cover = 0;\n"
        )
        fixed, changes = auto_fix_afl(afl)
        # ema variable should be renamed to emaVal
        assert "emaVal = EMA(Close, 14)" in fixed
        # EMA() function call should NOT be renamed
        assert "EMA(" in fixed
        assert "EMAVal(" not in fixed
