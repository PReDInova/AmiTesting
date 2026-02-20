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

DEFAULT_SYMBOL: str = "GC"

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


def setup_logging(correlation_id: str = "") -> None:
    """Configure Python logging to write to both *LOG_FILE* and the console
    at the INFO level.

    Parameters
    ----------
    correlation_id : str
        Optional session/run ID prepended to each log line for correlating
        concurrent operations (e.g. multiple live strategies).
    """

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Avoid adding duplicate handlers if called more than once
    if logger.handlers:
        return

    # Include correlation ID in format if provided
    if correlation_id:
        fmt = f"%(asctime)s | %(levelname)-8s | [{correlation_id}] %(name)s | %(message)s"
    else:
        fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

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


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def validate_config(check_live: bool = False) -> list[str]:
    """Validate configuration at startup. Returns a list of error messages.

    If the list is empty, configuration is valid.

    Parameters
    ----------
    check_live : bool
        If True, also validate live trading configuration (.env, etc.).
    """
    import os

    errors = []

    # Check AmiBroker database path
    ami_db = Path(AMIBROKER_DB_PATH)
    if not ami_db.exists():
        errors.append(f"AmiBroker database not found: {AMIBROKER_DB_PATH}")

    # Check AmiBroker executable path
    ami_exe = Path(AMIBROKER_EXE_PATH)
    if not ami_exe.exists():
        errors.append(f"AmiBroker executable not found: {AMIBROKER_EXE_PATH}")

    # Check APX template
    if not APX_TEMPLATE.exists():
        errors.append(f"APX template not found: {APX_TEMPLATE}")

    # Check AFL strategy file
    if not AFL_STRATEGY_FILE.exists():
        errors.append(f"AFL strategy file not found: {AFL_STRATEGY_FILE}")

    # Check required directories exist (create if needed)
    for d in [RESULTS_DIR, LOGS_DIR, CACHE_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if check_live:
        # Check .env file exists
        env_path = PROJECT_ROOT / ".env"
        if not env_path.exists():
            errors.append(f".env file not found: {env_path}")
        else:
            # Check required env vars
            from dotenv import load_dotenv
            load_dotenv(env_path)
            if not os.environ.get("PROJECT_X_API_KEY"):
                errors.append("PROJECT_X_API_KEY not set in .env")
            if not os.environ.get("PROJECT_X_USERNAME"):
                errors.append("PROJECT_X_USERNAME not set in .env")

    return errors
