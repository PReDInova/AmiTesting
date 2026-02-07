"""
Tests for config.settings — path constants, backtest parameters, and logging.
"""

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable
PROJECT_ROOT_PATH = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT_PATH not in sys.path:
    sys.path.insert(0, PROJECT_ROOT_PATH)

from config.settings import (
    PROJECT_ROOT,
    AFL_DIR,
    APX_DIR,
    SCRIPTS_DIR,
    RESULTS_DIR,
    LOGS_DIR,
    AMIBROKER_DB_PATH,
    AMIBROKER_EXE,
    GCZ25_SYMBOL,
    AFL_STRATEGY_FILE,
    APX_TEMPLATE,
    BACKTEST_SETTINGS,
    setup_logging,
)


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

class TestProjectPaths:
    """Verify that the project root and subdirectory constants are correct."""

    def test_project_root_exists(self):
        """PROJECT_ROOT should point to an existing directory."""
        assert PROJECT_ROOT.is_dir(), (
            f"PROJECT_ROOT does not exist or is not a directory: {PROJECT_ROOT}"
        )

    def test_subdirectories_defined(self):
        """AFL_DIR, APX_DIR, SCRIPTS_DIR, RESULTS_DIR, and LOGS_DIR should
        all be defined and reside under PROJECT_ROOT."""
        for name, path in [
            ("AFL_DIR", AFL_DIR),
            ("APX_DIR", APX_DIR),
            ("SCRIPTS_DIR", SCRIPTS_DIR),
            ("RESULTS_DIR", RESULTS_DIR),
            ("LOGS_DIR", LOGS_DIR),
        ]:
            assert isinstance(path, Path), f"{name} is not a Path instance"
            # Each subdirectory path should start with PROJECT_ROOT
            assert str(path).startswith(str(PROJECT_ROOT)), (
                f"{name} ({path}) is not under PROJECT_ROOT ({PROJECT_ROOT})"
            )


# ---------------------------------------------------------------------------
# Backtest settings
# ---------------------------------------------------------------------------

class TestBacktestSettings:
    """Verify the BACKTEST_SETTINGS dictionary."""

    def test_backtest_settings_keys(self):
        """BACKTEST_SETTINGS must contain run_mode, poll_interval, max_wait."""
        for key in ("run_mode", "poll_interval", "max_wait"):
            assert key in BACKTEST_SETTINGS, (
                f"Missing key '{key}' in BACKTEST_SETTINGS"
            )

    def test_backtest_settings_values(self):
        """run_mode should be 2; poll_interval and max_wait should be > 0."""
        assert BACKTEST_SETTINGS["run_mode"] == 2
        assert BACKTEST_SETTINGS["poll_interval"] > 0
        assert BACKTEST_SETTINGS["max_wait"] > 0


# ---------------------------------------------------------------------------
# AmiBroker identifiers
# ---------------------------------------------------------------------------

class TestAmiBrokerIdentifiers:
    """Verify COM dispatch name and symbol constant."""

    def test_com_dispatch_name(self):
        """AMIBROKER_EXE must be 'Broker.Application'."""
        assert AMIBROKER_EXE == "Broker.Application"

    def test_symbol(self):
        """GCZ25_SYMBOL must be 'GCZ25'."""
        assert GCZ25_SYMBOL == "GCZ25"


# ---------------------------------------------------------------------------
# File references
# ---------------------------------------------------------------------------

class TestFileReferences:
    """Verify that the AFL and APX file references point to real files."""

    def test_afl_strategy_file_exists(self):
        """AFL_STRATEGY_FILE should point to an existing file on disk."""
        assert AFL_STRATEGY_FILE.exists(), (
            f"AFL_STRATEGY_FILE not found: {AFL_STRATEGY_FILE}"
        )

    def test_apx_template_exists(self):
        """APX_TEMPLATE should point to an existing file on disk."""
        assert APX_TEMPLATE.exists(), (
            f"APX_TEMPLATE not found: {APX_TEMPLATE}"
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class TestLogging:
    """Verify the setup_logging helper."""

    def test_setup_logging_creates_log_dir(self):
        """Calling setup_logging() should create the LOGS_DIR directory."""
        setup_logging()
        assert LOGS_DIR.is_dir(), (
            f"LOGS_DIR was not created by setup_logging(): {LOGS_DIR}"
        )
