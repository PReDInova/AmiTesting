"""
Shared mutable state dicts and locks for the AmiTesting dashboard.

These are imported by blueprint modules that need to read or update
backtest, batch, or live session state.
"""

import threading
import time


# ---------------------------------------------------------------------------
# Backtest state tracking
# ---------------------------------------------------------------------------

_backtest_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "success": None,
    "error": None,
    "pid": None,
    "run_id": None,
}
_backtest_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Batch state tracking
# ---------------------------------------------------------------------------

_batch_state = {
    "running": False,
    "batch_id": None,
    "runner": None,  # reference to BatchRunner for cancel support
}
_batch_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Live session state tracking
# ---------------------------------------------------------------------------

_live_state = {
    "running": False,
    "started_at": None,
    "stopped_at": None,
    "account_name": None,
    "account_id": None,
    "strategy_name": None,
    "strategy_afl_path": None,
    "symbol": None,
    "ami_symbol": None,
    "bars_injected": 0,
    "scans_run": 0,
    "alerts_dispatched": 0,
    "alert_history": [],
    "feed_status": "",
    "feed_connected": False,
    "last_scan_time": None,
    "bar_interval": None,
    "error": None,
    "orchestrator": None,
    # Trade execution state
    "trade_enabled": False,
    "trades_placed": 0,
    "trades_filled": 0,
    "trades_cancelled": 0,
    "trades_rejected": 0,
    "trade_history": [],
    # Live indicator values (auto-detected from strategy)
    "indicator_values": {},
    "indicator_time": None,
}
_live_lock = threading.Lock()

# ---------------------------------------------------------------------------
# App startup time (used by health endpoint)
# ---------------------------------------------------------------------------
_app_start_time = time.time()
