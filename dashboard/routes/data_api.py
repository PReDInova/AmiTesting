"""
Data API blueprint -- symbols, dataset dates, OHLCV, health.
"""

import json
import logging
import time
import threading as _threading

from flask import Blueprint, jsonify, request

from config.settings import DEFAULT_SYMBOL, CHART_SETTINGS
from dashboard.state import (
    _backtest_state, _backtest_lock,
    _live_state, _live_lock,
    _app_start_time,
)
from dashboard.helpers import _parse_trade_date, VALID_INTERVALS

logger = logging.getLogger(__name__)

data_api_bp = Blueprint("data_api_bp", __name__)


@data_api_bp.route("/api/symbols")
def api_symbols():
    """Return available symbols from the AmiBroker database (cached)."""
    try:
        from scripts.ole_backtest import get_cached_symbols
        refresh = request.args.get("refresh") == "1"
        result = get_cached_symbols(refresh=refresh)
        symbols = [s for s in result["symbols"] if not s.startswith("~~~")]
        return jsonify({
            "symbols": symbols,
            "default": DEFAULT_SYMBOL,
            "stale": result["stale"],
        })
    except Exception as exc:
        logger.warning("Failed to list symbols: %s", exc)
        return jsonify({"symbols": [], "default": DEFAULT_SYMBOL, "stale": True})


@data_api_bp.route("/api/dataset-dates")
def api_dataset_dates():
    """Return the first and last available dates for the default symbol."""
    try:
        from scripts.ole_stock_data import get_dataset_date_range
        symbol = request.args.get("symbol", "").strip() or None
        refresh = request.args.get("refresh") == "1"
        result = get_dataset_date_range(symbol=symbol, refresh=refresh)
        return jsonify(result)
    except Exception as exc:
        logger.warning("Failed to get dataset dates: %s", exc)
        return jsonify({"first_date": None, "last_date": None, "error": str(exc), "stale": True})


@data_api_bp.route("/api/ohlcv/<symbol>")
def api_ohlcv(symbol: str):
    """Return OHLCV candlestick data for *symbol* around a trade."""
    from scripts.ole_stock_data import get_ohlcv_cached

    entry_date_str = request.args.get("entry_date", "").strip()
    exit_date_str = request.args.get("exit_date", "").strip()

    if not entry_date_str or not exit_date_str:
        return jsonify({"data": [], "error": "entry_date and exit_date are required."}), 400

    try:
        interval = int(request.args.get("interval", "60"))
    except ValueError:
        interval = 60
    if interval not in VALID_INTERVALS:
        interval = 60

    indicators_str = request.args.get("indicators", "").strip()
    indicator_configs = []
    if indicators_str:
        try:
            indicator_configs = json.loads(indicators_str)
            if not isinstance(indicator_configs, list):
                indicator_configs = []
        except json.JSONDecodeError:
            indicator_configs = []

    entry_dt = _parse_trade_date(entry_date_str)
    exit_dt = _parse_trade_date(exit_date_str)

    if entry_dt is None or exit_dt is None:
        return jsonify({"data": [], "error": f"Invalid date format. Got entry='{entry_date_str}', exit='{exit_date_str}'."}), 400

    interval_minutes = max(interval // 60, 1)
    padding_before = CHART_SETTINGS["bars_before_entry"] * interval_minutes
    padding_after = CHART_SETTINGS["bars_after_exit"] * interval_minutes

    result = get_ohlcv_cached(
        symbol=symbol,
        start_dt=entry_dt,
        end_dt=exit_dt,
        padding_before=padding_before,
        padding_after=padding_after,
        interval=interval,
    )

    if result.get("error"):
        if "not running" in result["error"].lower():
            return jsonify(result), 503
        if "not found" in result["error"].lower():
            return jsonify(result), 404
        return jsonify(result), 503

    if indicator_configs and result["data"]:
        from scripts.indicators import compute_indicators
        result["indicators"] = compute_indicators(result["data"], indicator_configs)
    else:
        result["indicators"] = []

    return jsonify(result)


# ---------------------------------------------------------------------------
# Health monitoring endpoint
# ---------------------------------------------------------------------------


@data_api_bp.route("/api/health")
def api_health():
    """System health check endpoint for monitoring."""
    health = {
        "status": "ok",
        "uptime_seconds": time.time() - _app_start_time,
        "active_threads": _threading.active_count(),
        "thread_names": [t.name for t in _threading.enumerate()],
    }

    # Memory usage
    try:
        import psutil
        proc = psutil.Process()
        mem = proc.memory_info()
        health["memory_mb"] = round(mem.rss / 1024 / 1024, 1)
    except ImportError:
        health["memory_mb"] = None

    # Database status
    try:
        from scripts.strategy_db import _get_connection
        conn = _get_connection()
        conn.execute("SELECT 1")
        health["database"] = "connected"
    except Exception as e:
        health["database"] = f"error: {e}"

    # Live session status
    with _live_lock:
        health["live_session"] = {
            "running": _live_state["running"],
            "feed_connected": _live_state.get("feed_connected", False),
            "bars_injected": _live_state.get("bars_injected", 0),
            "scans_run": _live_state.get("scans_run", 0),
            "last_scan_time": _live_state.get("last_scan_time"),
            "trade_enabled": _live_state.get("trade_enabled", False),
        }

    # Backtest status
    with _backtest_lock:
        health["backtest"] = {
            "running": _backtest_state["running"],
        }

    return jsonify(health)
