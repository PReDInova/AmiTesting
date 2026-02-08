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

from scripts.apx_builder import build_apx
from config.settings import AFL_STRATEGY_FILE, APX_TEMPLATE


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
        assert formula_elem.text == afl_content, (
            "FormulaContent does not match the AFL source"
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
        """build_apx should raise ValueError when the template XML is missing
        the <FormulaContent> element."""
        output_path = tmp_path / "output.apx"

        with pytest.raises(ValueError, match="FormulaContent"):
            build_apx(
                afl_path=str(tmp_afl_file),
                output_apx_path=str(output_path),
                template_apx_path=str(tmp_apx_template_no_formula),
            )


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
        assert formula_elem.text == afl_source, (
            "FormulaContent does not match the real AFL source file"
        )
