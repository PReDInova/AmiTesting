"""
Configuration for the Live Signal Alert system.

ProjectX credentials are loaded from .env via python-dotenv.
All other settings have sensible defaults that can be overridden
via CLI arguments to live_signal_alert.py.
"""

from config.settings import PROJECT_ROOT, AMIBROKER_DB_PATH, AFL_DIR

# ---------------------------------------------------------------------------
# ProjectX connection
# ---------------------------------------------------------------------------
# Credentials loaded from .env by python-dotenv:
#   PROJECT_X_API_KEY
#   PROJECT_X_USERNAME

PROJECTX_SYMBOLS = ["NQH6"]          # Instruments to stream (contract name)
PROJECTX_CONTRACT_ID = None           # Auto-resolved from symbol if None
PROJECTX_BAR_INTERVAL = 1             # Bar interval value (1 = 1 minute)
PROJECTX_BAR_UNIT = 2                 # Unit: 1=Second, 2=Minute, 3=Hour, 4=Day
PROJECTX_INITIAL_DAYS = 2             # Days of historical data to backfill

# ---------------------------------------------------------------------------
# AmiBroker injection
# ---------------------------------------------------------------------------
AMIBROKER_INJECT_SYMBOL = "NQ"        # Symbol name in AmiBroker DB
AMIBROKER_DB = AMIBROKER_DB_PATH

# ---------------------------------------------------------------------------
# Signal scanning
# ---------------------------------------------------------------------------
SCAN_INTERVAL_SECONDS = 60            # Run exploration scan every N seconds
SCAN_LOOKBACK_BARS = 5                # Check last N bars for new signals
SCAN_STRATEGY_AFL_PATH = str(AFL_DIR / "ma_crossover.afl")

# ---------------------------------------------------------------------------
# Alert settings
# ---------------------------------------------------------------------------
ALERT_CHANNELS = ["log", "desktop", "sound"]
ALERT_SOUND_FILE = None               # Path to .wav, or None for default beep
ALERT_WEBHOOK_URL = None              # Optional webhook endpoint
ALERT_DEDUP_WINDOW_SECONDS = 300      # Suppress duplicate signals within window

# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------
RECONNECT_DELAY_SECONDS = 5
MAX_RECONNECT_ATTEMPTS = 20
COM_RETRY_ATTEMPTS = 3
COM_RETRY_DELAY = 1.0
