"""
Trades blueprint -- trade journal, P&L, signal accuracy, signals.
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from scripts.strategy_db import (
    list_live_trades as db_list_live_trades,
    list_live_sessions as db_list_live_sessions,
    list_live_signals as db_list_live_signals,
    list_strategies as db_list_strategies,
    get_signal_accuracy as db_get_signal_accuracy,
    get_pnl_attribution as db_get_pnl_attribution,
    get_daily_pnl as db_get_daily_pnl,
)

logger = logging.getLogger(__name__)

trades_bp = Blueprint("trades_bp", __name__)


@trades_bp.route("/trades")
def trades_page():
    """Trade journal page showing all live trades."""
    trades = db_list_live_trades(limit=500)
    sessions = db_list_live_sessions(limit=20)
    return render_template("trades.html", trades=trades, sessions=sessions)


@trades_bp.route("/api/trades")
def api_trades():
    """API endpoint for live trades with filtering."""
    session_id = request.args.get("session_id")
    strategy_id = request.args.get("strategy_id")
    symbol = request.args.get("symbol")
    limit = int(request.args.get("limit", 200))
    trades = db_list_live_trades(
        session_id=session_id, strategy_id=strategy_id,
        symbol=symbol, limit=limit,
    )
    return jsonify(trades)


@trades_bp.route("/api/trades/pnl")
def api_trades_pnl():
    """Get P&L attribution data."""
    days = int(request.args.get("days", 30))
    group_by = request.args.get("group_by", "strategy")
    data = db_get_pnl_attribution(days=days, group_by=group_by)
    return jsonify(data)


@trades_bp.route("/api/trades/daily-pnl")
def api_daily_pnl():
    """Get daily realized P&L."""
    date_str = request.args.get("date")
    strategy_id = request.args.get("strategy_id")
    pnl = db_get_daily_pnl(date_str=date_str, strategy_id=strategy_id)
    return jsonify({"date": date_str, "pnl": pnl})


@trades_bp.route("/api/signals/accuracy")
def api_signal_accuracy():
    """Get signal accuracy metrics."""
    strategy_id = request.args.get("strategy_id")
    days = int(request.args.get("days", 30))
    data = db_get_signal_accuracy(strategy_id=strategy_id, days=days)
    return jsonify(data)


@trades_bp.route("/api/signals")
def api_signals():
    """API endpoint for live signals with filtering."""
    session_id = request.args.get("session_id")
    strategy_id = request.args.get("strategy_id")
    signal_type = request.args.get("signal_type")
    limit = int(request.args.get("limit", 500))
    signals = db_list_live_signals(
        session_id=session_id, strategy_id=strategy_id,
        signal_type=signal_type, limit=limit,
    )
    return jsonify(signals)


@trades_bp.route("/analytics")
def analytics_page():
    """Trade Intelligence analytics page with P&L attribution and signal accuracy."""
    strategies = db_list_strategies()
    return render_template("analytics.html", strategies=strategies)
