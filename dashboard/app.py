"""
Flask application for the AmiTesting Results Dashboard.

Provides a web interface to browse, review, stage, and approve/reject
backtest result CSV files produced by AmiBroker OLE automation.

This module creates the Flask app, initializes the database, registers
all route blueprints, and sets up Flask-SocketIO for real-time events.
"""

import os
import sys
import logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so config.settings can be imported
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from config.settings import DEFAULT_SYMBOL

from flask import Flask
from flask_socketio import SocketIO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App creation and configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ami-testing-dashboard-dev-key")

# ---------------------------------------------------------------------------
# Flask-SocketIO initialization
# ---------------------------------------------------------------------------

socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*')

# ---------------------------------------------------------------------------
# Strategy database initialization
# ---------------------------------------------------------------------------

from scripts.strategy_db import (
    init_db,
    seed_default_strategies,
    seed_param_tooltips,
    seed_indicator_tooltips,
)

init_db()
seed_default_strategies()
seed_param_tooltips()
seed_indicator_tooltips()

# ---------------------------------------------------------------------------
# Context processor -- make backtest_running and default_symbol available
# to all templates regardless of which blueprint handles the request.
# ---------------------------------------------------------------------------

from dashboard.state import _backtest_state, _backtest_lock


@app.context_processor
def inject_backtest_state():
    """Make backtest_running and default_symbol available to all templates."""
    with _backtest_lock:
        return {
            "backtest_running": _backtest_state["running"],
            "default_symbol": DEFAULT_SYMBOL,
        }


# ---------------------------------------------------------------------------
# Register all route blueprints
# ---------------------------------------------------------------------------

from dashboard.routes import register_blueprints

register_blueprints(app)

# ---------------------------------------------------------------------------
# Backward-compatible re-exports for test_dashboard.py and any other code
# that imports helpers directly from dashboard.app.
# ---------------------------------------------------------------------------

from dashboard.helpers import (  # noqa: F401
    parse_results_csv,
    get_status,
    compute_equity_curve,
    get_result_files,
    get_afl_content,
    validate_afl_content,
    save_afl_content,
    get_afl_versions,
    save_afl_version,
    load_afl_version,
    extract_indicators,
    count_params,
    _parse_trade_date,
)
