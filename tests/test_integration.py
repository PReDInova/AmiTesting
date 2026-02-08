"""
Integration tests for the AmiTesting pipeline.

- test_apx_builder_pipeline: builds an .apx from the real AFL + template
  and validates the resulting XML.
- test_full_pipeline_with_mock_ole: patches win32com to simulate the entire
  build-apx + OLE-backtest pipeline without a real AmiBroker installation.
- Live AmiBroker tests are marked with @pytest.mark.ami so they can be
  skipped in environments where AmiBroker is not installed.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.settings import (
    AFL_STRATEGY_FILE,
    APX_TEMPLATE,
    AMIBROKER_DB_PATH,
)
from scripts.apx_builder import build_apx


# ---------------------------------------------------------------------------
# APX builder pipeline (no COM needed)
# ---------------------------------------------------------------------------

class TestApxBuilderPipeline:
    """End-to-end APX generation using the real project files."""

    def test_apx_builder_pipeline(self, tmp_path):
        """Build .apx from the real AFL source and base.apx template,
        then validate the output XML structure and AFL injection."""
        output_path = tmp_path / "pipeline_output.apx"

        result = build_apx(
            afl_path=str(AFL_STRATEGY_FILE),
            output_apx_path=str(output_path),
            template_apx_path=str(APX_TEMPLATE),
        )

        # Output file exists
        result_path = Path(result)
        assert result_path.exists()

        # Parse and validate XML structure
        tree = ET.parse(result_path)
        root = tree.getroot()

        assert root.tag == "AmiBrokerProject"
        assert root.attrib.get("SchemaVersion") == "1"

        # FormulaContent should contain the AFL source
        formula_elem = root.find(".//FormulaContent")
        assert formula_elem is not None
        assert "MA(Close" in formula_elem.text
        assert "Cross(" in formula_elem.text

        # Backtest settings should still be present
        backtest_elem = root.find(".//Backtest")
        assert backtest_elem is not None

        equity_elem = root.find(".//InitialEquity")
        assert equity_elem is not None
        assert equity_elem.text == "100000"

        point_value_elem = root.find(".//PointValue")
        assert point_value_elem is not None
        assert point_value_elem.text == "100"


# ---------------------------------------------------------------------------
# Full pipeline with mocked OLE
# ---------------------------------------------------------------------------

class TestFullPipelineWithMockOle:
    """Simulate the entire build-apx + backtest pipeline without AmiBroker."""

    @patch("win32com.client.Dispatch")
    def test_full_pipeline_with_mock_ole(self, mock_dispatch, tmp_path):
        """Patch win32com, build the .apx, run OLEBacktester.run_full_test,
        and assert all COM steps were called correctly."""
        # -- Set up the COM mock --
        mock_app = MagicMock(name="AmiBrokerApp")
        mock_dispatch.return_value = mock_app

        analysis_doc = MagicMock(name="AnalysisDoc")
        type(analysis_doc).IsBusy = PropertyMock(return_value=False)
        mock_app.AnalysisDocs.Open.return_value = analysis_doc

        # -- Step 1: Build APX --
        output_apx = tmp_path / "integration_test.apx"
        apx_result = build_apx(
            afl_path=str(AFL_STRATEGY_FILE),
            output_apx_path=str(output_apx),
            template_apx_path=str(APX_TEMPLATE),
        )
        assert Path(apx_result).exists()

        # -- Step 2: Run the OLE backtest with mock --
        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        success = bt.run_full_test(
            db_path=r"C:\MockDB",
            apx_path=str(output_apx),
        )

        # -- Assertions --
        assert success is True

        # COM connect
        mock_dispatch.assert_called_with("Broker.Application")
        assert mock_app.Visible == 1

        # Database loaded
        mock_app.LoadDatabase.assert_called_once_with(r"C:\MockDB")

        # Analysis project opened (once for validation, once for backtest)
        assert mock_app.AnalysisDocs.Open.call_count == 2
        mock_app.AnalysisDocs.Open.assert_called_with(str(output_apx))
        analysis_doc.Run.assert_called_once()

        # Results exported (HTML + CSV)
        assert analysis_doc.Export.call_count == 2

        # Analysis doc closed (once after validation, once after backtest)
        assert analysis_doc.Close.call_count == 2

        # Disconnect called Quit
        mock_app.Quit.assert_called_once()


# ---------------------------------------------------------------------------
# Live AmiBroker tests (skipped unless --ami marker is selected)
# ---------------------------------------------------------------------------

@pytest.mark.ami
class TestLiveAmiBroker:
    """Tests that require a real, running AmiBroker installation.

    These are skipped by default.  Run with ``pytest -m ami`` to include them.
    """

    @pytest.mark.skipif(
        not AMIBROKER_DB_PATH,
        reason="AMIBROKER_DB_PATH is not configured in config/settings.py",
    )
    def test_live_connect_and_disconnect(self):
        """Connect to a live AmiBroker instance and immediately disconnect."""
        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        try:
            connected = bt.connect()
            assert connected is True, "Could not connect to live AmiBroker"
        finally:
            bt.disconnect()

    @pytest.mark.skipif(
        not AMIBROKER_DB_PATH,
        reason="AMIBROKER_DB_PATH is not configured in config/settings.py",
    )
    def test_live_load_database(self):
        """Connect to AmiBroker and load the configured database."""
        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        try:
            bt.connect()
            loaded = bt.load_database()
            assert loaded is True, "Could not load configured database"
        finally:
            bt.disconnect()
