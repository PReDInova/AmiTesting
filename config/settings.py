"""
Configuration settings for the AmiTesting project.

Centralizes all path constants, symbol definitions, backtest parameters,
and logging setup used across the project.
"""

import logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

AFL_DIR: Path = PROJECT_ROOT / "afl"
APX_DIR: Path = PROJECT_ROOT / "apx"
SCRIPTS_DIR: Path = PROJECT_ROOT / "scripts"
RESULTS_DIR: Path = PROJECT_ROOT / "results"
LOGS_DIR: Path = PROJECT_ROOT / "logs"
INDICATORS_DIR: Path = PROJECT_ROOT / "indicators"
STRATEGIES_DIR: Path = PROJECT_ROOT / "strategies"

# ---------------------------------------------------------------------------
# AmiBroker settings
# ---------------------------------------------------------------------------

# Set this to the full path of your AmiBroker database directory, e.g.
# AMIBROKER_DB_PATH = r"C:\AmiBroker\Databases\MyDatabase"
AMIBROKER_DB_PATH: str = r"C:\Program Files (x86)\AmiBroker\Databases\GoldAsia"
AMIBROKER_DB_DIR: str = r"C:\Program Files (x86)\AmiBroker\Databases"

AMIBROKER_EXE: str = "Broker.Application"  # COM dispatch name
AMIBROKER_EXE_PATH: str = r"C:\Program Files (x86)\AmiBroker\Broker.exe"

# ---------------------------------------------------------------------------
# Symbol & file references
# ---------------------------------------------------------------------------

GCZ25_SYMBOL: str = "GCZ5"

AFL_STRATEGY_FILE: Path = AFL_DIR / "ma_crossover.afl"
APX_TEMPLATE: Path = APX_DIR / "base.apx"
APX_OUTPUT: Path = APX_DIR / "gcz25_test.apx"

RESULTS_HTML: Path = RESULTS_DIR / "results.html"
RESULTS_CSV: Path = RESULTS_DIR / "results.csv"

LOG_FILE: Path = LOGS_DIR / "ole_backtest.log"

CACHE_DIR: Path = PROJECT_ROOT / "cache"

# ---------------------------------------------------------------------------
# Backtest settings
# ---------------------------------------------------------------------------

BACKTEST_SETTINGS: dict = {
    "run_mode": 2,            # 2 = portfolio backtest
    "poll_interval": 0.5,     # seconds between status polls
    "max_wait": 600,          # maximum seconds to wait for completion
    "starting_capital": 100_000,  # must match APX template <InitialEquity>
}

# ---------------------------------------------------------------------------
# Chart settings (Sprint 3 / Sprint 4 -- trade candlestick charts)
# ---------------------------------------------------------------------------

CHART_SETTINGS: dict = {
    "bars_before_entry": 50,    # bars of context before trade entry
    "bars_after_exit": 20,      # bars of context after trade exit
    "cache_max_age_hours": 24,  # re-fetch from AmiBroker if cache older than this
    "valid_intervals": [60, 300, 600, 86400],  # allowed timeframes in seconds
    "explorer_default_days": 5,  # default number of days to load in indicator explorer
}

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def setup_logging() -> None:
    """Configure Python logging to write to both *LOG_FILE* and the console
    at the INFO level."""

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Avoid adding duplicate handlers if called more than once
    if logger.handlers:
        return

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
