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

import run as run_module


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

        assert root.tag == "AmiBroker-Analysis"
        assert root.attrib.get("CompactMode") == "0"

        # FormulaContent is populated by default to match FormulaPath,
        # preventing the "formula is different" dialog in AmiBroker.
        formula_elem = root.find(".//FormulaContent")
        assert formula_elem is not None
        assert formula_elem.text is not None and len(formula_elem.text.strip()) > 0

        # BacktestSettings should still be present
        backtest_elem = root.find(".//BacktestSettings")
        assert backtest_elem is not None

        equity_elem = root.find(".//InitialEquity")
        assert equity_elem is not None
        assert equity_elem.text == "100000"

        margin_elem = root.find(".//MarginRequirement")
        assert margin_elem is not None
        assert margin_elem.text == "100"


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

        # Analysis project opened once for backtest (validation step was removed
        # to avoid Run() blocking on second open)
        assert mock_app.AnalysisDocs.Open.call_count == 1
        mock_app.AnalysisDocs.Open.assert_called_with(str(output_apx))
        analysis_doc.Run.assert_called_once()

        # Results exported (HTML + CSV)
        assert analysis_doc.Export.call_count == 2

        # Analysis doc closed once after backtest
        assert analysis_doc.Close.call_count == 1

        # Disconnect called Quit
        mock_app.Quit.assert_called_once()


# ---------------------------------------------------------------------------
# Symbol pipeline tests
# ---------------------------------------------------------------------------

class TestSymbolPipeline:
    """Symbol parameter flows through the full pipeline."""

    @patch.object(run_module, "OLEBacktester")
    @patch.object(run_module, "build_apx")
    @patch.object(run_module, "update_run")
    @patch.object(run_module, "create_run", return_value="fake-run-id")
    @patch.object(run_module, "validate_afl_file", return_value=(True, []))
    @patch.object(run_module, "get_latest_version", return_value={
        "id": "ver-1", "version_number": 1, "label": "test", "afl_content": "Buy=1;Sell=0;",
    })
    @patch("scripts.strategy_db.get_strategy", return_value={"id": "strat-1", "name": "TestStrat"})
    @patch.object(run_module, "list_strategies", return_value=[
        {"id": "strat-1", "name": "TestStrat"},
    ])
    @patch.object(run_module, "seed_indicator_tooltips")
    @patch.object(run_module, "seed_param_tooltips")
    @patch.object(run_module, "seed_default_strategies")
    @patch.object(run_module, "init_db")
    @patch.object(run_module, "setup_logging")
    def test_pipeline_passes_symbol_to_build_apx(
        self,
        mock_setup_logging,
        mock_init_db,
        mock_seed_strats,
        mock_seed_params,
        mock_seed_indicators,
        mock_list_strats,
        mock_get_strategy,
        mock_get_version,
        mock_validate,
        mock_create_run,
        mock_update_run,
        mock_build_apx,
        mock_ole_cls,
        tmp_path,
    ):
        """run.main() with symbol passes it to build_apx and create_run."""
        # build_apx must return a valid path string
        mock_build_apx.return_value = str(tmp_path / "fake.apx")

        # OLEBacktester mock
        mock_bt = MagicMock()
        mock_bt.run_full_test.return_value = True
        mock_ole_cls.return_value = mock_bt

        run_module.main(symbol="NQ")

        # Assert build_apx was called with symbol="NQ"
        mock_build_apx.assert_called_once()
        call_kwargs = mock_build_apx.call_args
        # symbol is passed as a keyword argument
        assert call_kwargs.kwargs.get("symbol") == "NQ" or \
            (len(call_kwargs) > 1 and call_kwargs[1].get("symbol") == "NQ")

        # Assert create_run was called with symbol="NQ"
        mock_create_run.assert_called_once()
        create_kwargs = mock_create_run.call_args
        assert create_kwargs.kwargs.get("symbol") == "NQ" or \
            (len(create_kwargs) > 1 and create_kwargs[1].get("symbol") == "NQ")


class TestListSymbols:
    """list_symbols() enumerates symbols via COM."""

    @patch("scripts.ole_backtest.pythoncom")
    @patch("scripts.ole_backtest.win32com.client.Dispatch")
    def test_list_symbols_mock_com(self, mock_dispatch, mock_pythoncom):
        """list_symbols returns sorted ticker names from mocked COM."""
        from scripts.ole_backtest import list_symbols

        # Build mock AmiBroker app with 3 stocks
        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app

        # Create mock stock objects with Ticker attributes
        stock_c = MagicMock()
        stock_c.Ticker = "ZB"
        stock_b = MagicMock()
        stock_b.Ticker = "NQ"
        stock_a = MagicMock()
        stock_a.Ticker = "GC"

        mock_app.Stocks.Count = 3
        mock_app.Stocks.side_effect = lambda i: [stock_a, stock_b, stock_c][i]

        result = list_symbols(db_path=r"C:\MockDB")

        assert result == ["GC", "NQ", "ZB"]
        mock_app.LoadDatabase.assert_called_once_with(r"C:\MockDB")

    @patch("scripts.ole_backtest.pythoncom")
    @patch("scripts.ole_backtest.win32com.client.Dispatch")
    def test_list_symbols_empty_database(self, mock_dispatch, mock_pythoncom):
        """list_symbols returns an empty list when the database has no symbols."""
        from scripts.ole_backtest import list_symbols

        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app
        mock_app.Stocks.Count = 0

        result = list_symbols(db_path=r"C:\MockDB")

        assert result == []

    @patch("scripts.ole_backtest.pythoncom")
    @patch("scripts.ole_backtest.win32com.client.Dispatch")
    def test_list_symbols_com_error_returns_empty(self, mock_dispatch, mock_pythoncom):
        """list_symbols returns an empty list when COM raises an error."""
        from scripts.ole_backtest import list_symbols

        mock_dispatch.side_effect = Exception("COM not available")

        result = list_symbols(db_path=r"C:\MockDB")

        assert result == []


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
