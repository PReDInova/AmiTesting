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
