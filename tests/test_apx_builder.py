"""
Tests for scripts.apx_builder — APX file generation from AFL + XML template.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.apx_builder import build_apx, _compute_date_range
from config.settings import AFL_STRATEGY_FILE, APX_TEMPLATE, DEFAULT_SYMBOL


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestBuildApxSuccess:
    """Tests that verify normal, successful APX generation."""

    def test_build_apx_success(self, tmp_afl_file, tmp_apx_template, tmp_path):
        """build_apx should create an output file whose <FormulaContent>
        contains the AFL source code."""
        output_path = tmp_path / "output" / "test_output.apx"

        build_apx(
            afl_path=str(tmp_afl_file),
            output_apx_path=str(output_path),
            template_apx_path=str(tmp_apx_template),
        )

        # File should exist
        assert output_path.exists(), "Output APX file was not created"

        # Parse and verify AFL content is embedded
        tree = ET.parse(output_path)
        formula_elem = tree.getroot().find(".//FormulaContent")
        assert formula_elem is not None, "FormulaContent element missing in output"

        afl_content = tmp_afl_file.read_text(encoding="utf-8")
        # AmiBroker format stores newlines as literal \r\n escape sequences
        afl_escaped = afl_content.replace("\r\n", "\\r\\n").replace("\n", "\\r\\n")
        assert formula_elem.text == afl_escaped, (
            "FormulaContent does not match the escaped AFL source"
        )

    def test_build_apx_returns_output_path(
        self, tmp_afl_file, tmp_apx_template, tmp_path
    ):
        """build_apx should return the output path as a string."""
        output_path = tmp_path / "returned.apx"

        result = build_apx(
            afl_path=str(tmp_afl_file),
            output_apx_path=str(output_path),
            template_apx_path=str(tmp_apx_template),
        )

        assert result == str(output_path)

    def test_build_apx_preserves_backtest_settings(
        self, tmp_afl_file, tmp_apx_template, tmp_path
    ):
        """Non-formula XML elements (e.g. Backtest/InitialEquity) should be
        preserved in the output APX file."""
        output_path = tmp_path / "preserved.apx"

        build_apx(
            afl_path=str(tmp_afl_file),
            output_apx_path=str(output_path),
            template_apx_path=str(tmp_apx_template),
        )

        tree = ET.parse(output_path)
        root = tree.getroot()

        # The template fixture includes <InitialEquity>100000</InitialEquity>
        equity_elem = root.find(".//InitialEquity")
        assert equity_elem is not None, "InitialEquity element was not preserved"
        assert equity_elem.text == "100000"

        # The template fixture includes <ReverseSignalForcesExit>1</ReverseSignalForcesExit>
        reverse_elem = root.find(".//ReverseSignalForcesExit")
        assert reverse_elem is not None, "ReverseSignalForcesExit element was not preserved"
        assert reverse_elem.text == "1"


# ---------------------------------------------------------------------------
# Error-path tests
# ---------------------------------------------------------------------------

class TestBuildApxErrors:
    """Tests that verify correct error handling."""

    def test_build_apx_afl_not_found(self, tmp_apx_template, tmp_path):
        """build_apx should raise FileNotFoundError when the AFL file does
        not exist."""
        output_path = tmp_path / "output.apx"
        nonexistent_afl = tmp_path / "nonexistent.afl"

        with pytest.raises(FileNotFoundError):
            build_apx(
                afl_path=str(nonexistent_afl),
                output_apx_path=str(output_path),
                template_apx_path=str(tmp_apx_template),
            )

    def test_build_apx_template_not_found(self, tmp_afl_file, tmp_path):
        """build_apx should raise FileNotFoundError when the template does
        not exist."""
        output_path = tmp_path / "output.apx"
        nonexistent_template = tmp_path / "nonexistent.apx"

        with pytest.raises(FileNotFoundError):
            build_apx(
                afl_path=str(tmp_afl_file),
                output_apx_path=str(output_path),
                template_apx_path=str(nonexistent_template),
            )

    def test_build_apx_missing_formula_element(
        self, tmp_afl_file, tmp_apx_template_no_formula, tmp_path
    ):
        """build_apx should still succeed when the template XML is missing
        the <FormulaContent> element — FormulaPath is set instead."""
        output_path = tmp_path / "output.apx"

        result = build_apx(
            afl_path=str(tmp_afl_file),
            output_apx_path=str(output_path),
            template_apx_path=str(tmp_apx_template_no_formula),
        )

        assert Path(result).exists(), "Output APX file was not created"


# ---------------------------------------------------------------------------
# Integration with real project files
# ---------------------------------------------------------------------------

class TestBuildApxRealFiles:
    """Tests that use the actual AFL and APX files shipped with the project."""

    def test_build_apx_with_real_files(self, tmp_path):
        """Build an APX using the real ma_crossover.afl and base.apx,
        then verify that the AFL content is embedded in FormulaContent."""
        output_path = tmp_path / "real_output.apx"

        result = build_apx(
            afl_path=str(AFL_STRATEGY_FILE),
            output_apx_path=str(output_path),
            template_apx_path=str(APX_TEMPLATE),
        )

        assert Path(result).exists(), "Output file from real build does not exist"

        # Parse output and verify AFL content
        tree = ET.parse(result)
        formula_elem = tree.getroot().find(".//FormulaContent")
        assert formula_elem is not None

        afl_source = AFL_STRATEGY_FILE.read_text(encoding="utf-8")
        # AmiBroker format stores newlines as literal \r\n escape sequences
        afl_escaped = afl_source.replace("\r\n", "\\r\\n").replace("\n", "\\r\\n")
        assert formula_elem.text == afl_escaped, (
            "FormulaContent does not match the escaped real AFL source file"
        )


# ---------------------------------------------------------------------------
# Symbol parameter tests
# ---------------------------------------------------------------------------

class TestBuildApxSymbol:
    """Symbol parameter is injected into the APX <Symbol> tag."""

    def test_build_apx_custom_symbol(self, tmp_path):
        """When symbol='NQ' is passed, the APX <Symbol> tag contains 'NQ'."""
        # Create minimal AFL file
        afl_file = tmp_path / "test.afl"
        afl_file.write_text("Buy = 1; Sell = 0;", encoding="utf-8")

        # Create minimal APX template with Symbol tag
        template = tmp_path / "template.apx"
        template.write_bytes(
            b'<?xml version="1.0" encoding="iso-8859-1"?>\r\n'
            b'<AmiBrokerAnalysis>\r\n'
            b'<FormulaPath></FormulaPath>\r\n'
            b'<FormulaContent></FormulaContent>\r\n'
            b'<Symbol>PLACEHOLDER</Symbol>\r\n'
            b'</AmiBrokerAnalysis>\r\n'
        )

        output = tmp_path / "output.apx"
        build_apx(
            str(afl_file),
            str(output),
            str(template),
            symbol="NQ",
        )

        content = output.read_bytes()
        assert b"<Symbol>NQ</Symbol>" in content

    def test_build_apx_default_symbol(self, tmp_path):
        """When symbol is not passed, DEFAULT_SYMBOL is used."""
        afl_file = tmp_path / "test.afl"
        afl_file.write_text("Buy = 1; Sell = 0;", encoding="utf-8")

        template = tmp_path / "template.apx"
        template.write_bytes(
            b'<?xml version="1.0" encoding="iso-8859-1"?>\r\n'
            b'<AmiBrokerAnalysis>\r\n'
            b'<FormulaPath></FormulaPath>\r\n'
            b'<FormulaContent></FormulaContent>\r\n'
            b'<Symbol>PLACEHOLDER</Symbol>\r\n'
            b'</AmiBrokerAnalysis>\r\n'
        )

        output = tmp_path / "output.apx"
        build_apx(
            str(afl_file),
            str(output),
            str(template),
        )

        content = output.read_bytes()
        assert f"<Symbol>{DEFAULT_SYMBOL}</Symbol>".encode() in content


# ---------------------------------------------------------------------------
# Date range tests
# ---------------------------------------------------------------------------

class TestBuildApxDateRange:
    """Date range is computed by _compute_date_range and enforced via AFL
    filter (injected by run.py).  The APX RangeType stays at 0 ('All quotes')
    because AmiBroker's OLE interface ignores APX date values."""

    @staticmethod
    def _make_template(tmp_path):
        """Create a minimal APX template with all date-range XML tags."""
        template = tmp_path / "template.apx"
        template.write_bytes(
            b'<?xml version="1.0" encoding="iso-8859-1"?>\r\n'
            b'<AmiBroker-Analysis CompactMode="0">\r\n'
            b'<General>\r\n'
            b'<FormulaPath></FormulaPath>\r\n'
            b'<FormulaContent></FormulaContent>\r\n'
            b'<Symbol></Symbol>\r\n'
            b'<RangeType>0</RangeType>\r\n'
            b'<FromDate>2025-01-01 00:00:00</FromDate>\r\n'
            b'<ToDate>2025-12-31</ToDate>\r\n'
            b'</General>\r\n'
            b'<BacktestSettings>\r\n'
            b'<InitialEquity>100000</InitialEquity>\r\n'
            b'<RangeType>0</RangeType>\r\n'
            b'<RangeFromDate>2025-01-01 00:00:00</RangeFromDate>\r\n'
            b'<RangeToDate>2025-12-31</RangeToDate>\r\n'
            b'<BacktestRangeType>0</BacktestRangeType>\r\n'
            b'<BacktestRangeFromDate>2025-01-01 00:00:00</BacktestRangeFromDate>\r\n'
            b'<BacktestRangeToDate>2025-12-31</BacktestRangeToDate>\r\n'
            b'</BacktestSettings>\r\n'
            b'</AmiBroker-Analysis>\r\n'
        )
        return template

    def test_range_type_stays_0_when_date_range_provided(self, tmp_path):
        """RangeType must remain 0 ('All quotes') even when a date range is
        specified — the date window is enforced via AFL, not APX settings."""
        afl_file = tmp_path / "test.afl"
        afl_file.write_text("Buy = 1; Sell = 0;", encoding="utf-8")
        template = self._make_template(tmp_path)
        output = tmp_path / "output.apx"

        build_apx(
            str(afl_file),
            str(output),
            str(template),
            date_range="1m@0m",
            dataset_start="2025-06-01",
            dataset_end="2025-12-31",
        )

        content = output.read_bytes()
        # RangeType must remain 0 in all locations
        assert content.count(b"<RangeType>0</RangeType>") == 2
        assert b"<BacktestRangeType>0</BacktestRangeType>" in content

    def test_date_tags_unchanged_when_date_range_provided(self, tmp_path):
        """APX date tags (FromDate, ToDate, etc.) must stay at their template
        defaults — the APX builder no longer modifies them."""
        afl_file = tmp_path / "test.afl"
        afl_file.write_text("Buy = 1; Sell = 0;", encoding="utf-8")
        template = self._make_template(tmp_path)
        output = tmp_path / "output.apx"

        build_apx(
            str(afl_file),
            str(output),
            str(template),
            date_range="1m@0m",
            dataset_start="2025-06-01",
            dataset_end="2025-12-31",
        )

        content = output.read_bytes()
        # Template dates must be preserved unchanged
        assert b"<FromDate>2025-01-01 00:00:00</FromDate>" in content
        assert b"<ToDate>2025-12-31</ToDate>" in content
        assert b"<RangeFromDate>2025-01-01 00:00:00</RangeFromDate>" in content
        assert b"<RangeToDate>2025-12-31</RangeToDate>" in content

    def test_range_type_unchanged_when_no_date_range(self, tmp_path):
        """When date_range is not provided, RangeType stays at 0."""
        afl_file = tmp_path / "test.afl"
        afl_file.write_text("Buy = 1; Sell = 0;", encoding="utf-8")
        template = self._make_template(tmp_path)
        output = tmp_path / "output.apx"

        build_apx(
            str(afl_file),
            str(output),
            str(template),
        )

        content = output.read_bytes()
        assert content.count(b"<RangeType>0</RangeType>") == 2
        assert b"<BacktestRangeType>0</BacktestRangeType>" in content

    def test_compute_date_range_simple_1m(self):
        """Simple '1m' measures 1 month back from dataset end."""
        from_d, to_d = _compute_date_range("1m", "2025-06-01", "2025-12-31")
        assert from_d == "2025-11-30"
        assert to_d == "2025-12-31"

    def test_compute_date_range_compound_1m_at_3m(self):
        """'1m@3m' = 1 month starting 3 months after dataset start."""
        from_d, to_d = _compute_date_range("1m@3m", "2025-06-01", "2025-12-31")
        assert from_d == "2025-09-01"
        assert to_d == "2025-10-01"

    def test_different_date_ranges_produce_different_windows(self):
        """Different date_range codes must produce different date windows
        via _compute_date_range so that AFL filters cover different periods."""
        results = {}
        for code in ["1m@0m", "1m@3m", "3m@0m", "1m"]:
            from_d, to_d = _compute_date_range(code, "2025-01-01", "2025-12-31")
            results[code] = (from_d, to_d)

        # All four date ranges should produce DIFFERENT (from, to) pairs
        unique_windows = set(results.values())
        assert len(unique_windows) == 4, (
            f"Expected 4 unique date windows but got {len(unique_windows)}: {results}"
        )

    def test_real_template_range_type_stays_0(self, tmp_path):
        """Verify that the real base.apx template keeps RangeType=0 even
        when a date range is provided — dates are enforced via AFL filter."""
        afl_file = tmp_path / "test.afl"
        afl_file.write_text("Buy = 1; Sell = 0;", encoding="utf-8")
        output = tmp_path / "real_dates.apx"

        build_apx(
            str(afl_file),
            str(output),
            str(APX_TEMPLATE),
            date_range="1m@0m",
            dataset_start="2025-06-01",
            dataset_end="2025-12-31",
        )

        content = output.read_bytes()
        # RangeType must stay 0 in all locations
        assert b"<RangeType>2</RangeType>" not in content
        assert b"<BacktestRangeType>2</BacktestRangeType>" not in content
        assert content.count(b"<RangeType>0</RangeType>") == 2
        assert b"<BacktestRangeType>0</BacktestRangeType>" in content
