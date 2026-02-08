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
STRATEGIES_DIR: Path = PROJECT_ROOT / "strategies"

# ---------------------------------------------------------------------------
# AmiBroker settings
# ---------------------------------------------------------------------------

# Set this to the full path of your AmiBroker database directory, e.g.
# AMIBROKER_DB_PATH = r"C:\AmiBroker\Databases\MyDatabase"
AMIBROKER_DB_PATH: str = r"C:\Program Files (x86)\AmiBroker\Databases\GoldAsia"

AMIBROKER_EXE: str = "Broker.Application"  # COM dispatch name

# ---------------------------------------------------------------------------
# Symbol & file references
# ---------------------------------------------------------------------------

GCZ25_SYMBOL: str = "GCZ25"

AFL_STRATEGY_FILE: Path = AFL_DIR / "ma_crossover.afl"
APX_TEMPLATE: Path = APX_DIR / "base.apx"
APX_OUTPUT: Path = APX_DIR / "gcz25_test.apx"

RESULTS_HTML: Path = RESULTS_DIR / "results.html"
RESULTS_CSV: Path = RESULTS_DIR / "results.csv"

LOG_FILE: Path = LOGS_DIR / "ole_backtest.log"

# ---------------------------------------------------------------------------
# Backtest settings
# ---------------------------------------------------------------------------

BACKTEST_SETTINGS: dict = {
    "run_mode": 2,            # 2 = portfolio backtest
    "poll_interval": 0.5,     # seconds between status polls
    "max_wait": 300,          # maximum seconds to wait for completion
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
