"""
Tests for scripts.ole_backtest — OLE/COM-based AmiBroker backtester.

All tests are fully mocked; no real AmiBroker installation is required.
The win32com.client.Dispatch call is patched so the module can be imported
and exercised in any CI environment.
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_app(is_busy_sequence=None):
    """Build a fully-wired MagicMock that looks like the AmiBroker COM object.

    Parameters
    ----------
    is_busy_sequence : list[bool] | None
        Sequence of values that ``analysis_doc.IsBusy`` will return on
        successive accesses.  Defaults to ``[False]`` (immediately done).
    """
    if is_busy_sequence is None:
        is_busy_sequence = [False]

    app = MagicMock(name="AmiBrokerApp")

    analysis_doc = MagicMock(name="AnalysisDoc")
    busy_iter = iter(is_busy_sequence)
    type(analysis_doc).IsBusy = PropertyMock(
        side_effect=lambda: next(busy_iter, False)
    )

    app.AnalysisDocs.Open.return_value = analysis_doc
    return app


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------

class TestConnect:
    """Verify connect() behaviour under various COM conditions."""

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_connect_success(self, mock_dispatch, mock_popen):
        """connect() should return True and set Visible = 1 on the COM app."""
        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        result = bt.connect()

        assert result is True
        mock_dispatch.assert_called_once_with("Broker.Application")
        mock_popen.assert_called_once()
        assert mock_app.Visible == 1

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_connect_failure(self, mock_dispatch, mock_popen):
        """connect() should return False when Dispatch raises."""
        mock_dispatch.side_effect = Exception("COM server not registered")

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        result = bt.connect()

        assert result is False


# ---------------------------------------------------------------------------
# Database loading tests
# ---------------------------------------------------------------------------

class TestLoadDatabase:
    """Verify load_database() behaviour."""

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_load_database_success(self, mock_dispatch, mock_popen):
        """load_database() should call LoadDatabase on the COM app and return True."""
        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        bt.connect()

        result = bt.load_database(r"C:\TestDB")

        assert result is True
        mock_app.LoadDatabase.assert_called_once_with(r"C:\TestDB")

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_load_database_failure(self, mock_dispatch, mock_popen):
        """load_database() should return False when LoadDatabase raises."""
        mock_app = MagicMock()
        mock_app.LoadDatabase.side_effect = Exception("DB not found")
        mock_dispatch.return_value = mock_app

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        bt.connect()

        result = bt.load_database(r"C:\BadPath")

        assert result is False


# ---------------------------------------------------------------------------
# Backtest execution tests
# ---------------------------------------------------------------------------

class TestRunBacktest:
    """Verify run_backtest() behaviour."""

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_run_backtest_success(self, mock_dispatch, mock_popen, tmp_path):
        """run_backtest() should return True when IsBusy becomes False,
        and Export should be called twice (HTML and CSV)."""
        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app

        analysis_doc = MagicMock(name="AnalysisDoc")
        # IsBusy returns False immediately so the poll loop exits right away
        type(analysis_doc).IsBusy = PropertyMock(return_value=False)
        mock_app.AnalysisDocs.Open.return_value = analysis_doc

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        bt.connect()

        apx_file = tmp_path / "test.apx"
        apx_file.write_text("<xml/>", encoding="utf-8")

        result = bt.run_backtest(str(apx_file))

        assert result is True
        analysis_doc.Run.assert_called_once()
        # Export is called once for HTML and once for CSV
        assert analysis_doc.Export.call_count == 2
        analysis_doc.Close.assert_called_once()

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_run_backtest_timeout(self, mock_dispatch, mock_popen):
        """run_backtest() should return False when IsBusy never becomes False
        and max_wait is exceeded."""
        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app

        analysis_doc = MagicMock(name="AnalysisDoc")
        # IsBusy always returns True to trigger the timeout path
        type(analysis_doc).IsBusy = PropertyMock(return_value=True)
        mock_app.AnalysisDocs.Open.return_value = analysis_doc

        from scripts.ole_backtest import OLEBacktester
        import config.settings as settings

        bt = OLEBacktester()
        bt.connect()

        # Override BACKTEST_SETTINGS to shorten the timeout
        original_settings = settings.BACKTEST_SETTINGS.copy()
        settings.BACKTEST_SETTINGS["max_wait"] = 1
        settings.BACKTEST_SETTINGS["poll_interval"] = 0.25

        try:
            result = bt.run_backtest()
            assert result is False
        finally:
            # Restore original settings
            settings.BACKTEST_SETTINGS.update(original_settings)

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_run_backtest_open_fails(self, mock_dispatch, mock_popen):
        """run_backtest() should return False when AnalysisDocs.Open returns None."""
        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app
        mock_app.AnalysisDocs.Open.return_value = None

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        bt.connect()

        result = bt.run_backtest()

        assert result is False


# ---------------------------------------------------------------------------
# Disconnect tests
# ---------------------------------------------------------------------------

class TestDisconnect:
    """Verify disconnect() behaviour."""

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_disconnect(self, mock_dispatch, mock_popen):
        """disconnect() should call Quit() on the COM application."""
        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        bt.connect()
        bt.disconnect()

        mock_app.Quit.assert_called_once()


# ---------------------------------------------------------------------------
# Full orchestration tests
# ---------------------------------------------------------------------------

class TestRunFullTest:
    """Verify run_full_test() end-to-end orchestration."""

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_run_full_test_success(self, mock_dispatch, mock_popen):
        """run_full_test() should return True when connect, load_database,
        and run_backtest all succeed; Quit should still be called."""
        mock_app = MagicMock()
        mock_dispatch.return_value = mock_app

        analysis_doc = MagicMock(name="AnalysisDoc")
        type(analysis_doc).IsBusy = PropertyMock(return_value=False)
        mock_app.AnalysisDocs.Open.return_value = analysis_doc

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        result = bt.run_full_test(db_path=r"C:\TestDB")

        assert result is True
        # Quit is called by disconnect() in the finally block
        mock_app.Quit.assert_called_once()

    @patch("subprocess.Popen")
    @patch("win32com.client.Dispatch")
    def test_run_full_test_connect_fails(self, mock_dispatch, mock_popen):
        """run_full_test() should return False when connect() fails."""
        mock_dispatch.side_effect = Exception("COM unavailable")

        from scripts.ole_backtest import OLEBacktester

        bt = OLEBacktester()
        result = bt.run_full_test()

        assert result is False
