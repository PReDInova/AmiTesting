"""
Live trading blueprint -- start/stop/kill/status, accounts, proximity, replay.
"""

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from config.settings import DEFAULT_SYMBOL, PROJECT_ROOT
from dashboard.state import _live_state, _live_lock
from scripts.strategy_db import (
    get_strategy as db_get_strategy,
    list_strategies as db_list_strategies,
    get_latest_version as db_get_latest_version,
    create_live_session as db_create_live_session,
    update_live_session as db_update_live_session,
    record_live_trade as db_record_live_trade,
    record_live_signal as db_record_live_signal,
    list_live_sessions as db_list_live_sessions,
    get_live_session as db_get_live_session,
)

logger = logging.getLogger(__name__)

live_bp = Blueprint("live_bp", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_strategy_afl(strategy_id: str) -> str:
    """Get the AFL file path for a strategy, writing to disk if needed."""
    strategy = db_get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"Strategy not found: {strategy_id}")

    version = db_get_latest_version(strategy_id)
    if not version or not version.get("afl_content"):
        raise ValueError(f"No AFL content found for strategy: {strategy['name']}")

    strategies_dir = PROJECT_ROOT / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r'[^\w\-]', '_', strategy["name"])
    afl_path = strategies_dir / f"{safe_name}.afl"

    afl_path.write_text(version["afl_content"], encoding="utf-8")
    return str(afl_path)


def _live_status_callback(event_type: str, data: dict) -> None:
    """Callback fired by the orchestrator to update live state.

    Persists trades, signals, and session updates to the database
    in addition to updating the in-memory state dict.
    """
    with _live_lock:
        session_id = _live_state.get("session_id")
        strategy_id = _live_state.get("_strategy_id")
        version_id = _live_state.get("_version_id")

        if event_type == "bar_injected":
            _live_state["bars_injected"] = data.get("count", 0)
        elif event_type == "scan_complete":
            _live_state["scans_run"] = data.get("scan_num", 0)
            _live_state["last_scan_time"] = datetime.now(timezone.utc).isoformat()
            if session_id and _live_state["scans_run"] % 10 == 0:
                try:
                    db_update_live_session(
                        session_id,
                        bars_injected=_live_state["bars_injected"],
                        scans_run=_live_state["scans_run"],
                        alerts_fired=_live_state["alerts_dispatched"],
                        trades_placed=_live_state["trades_placed"],
                        trades_filled=_live_state["trades_filled"],
                    )
                except Exception as exc:
                    logger.debug("Failed to update live session: %s", exc)
        elif event_type == "alert":
            _live_state["alerts_dispatched"] += 1
            alert_entry = {
                "signal_type": data.get("signal_type"),
                "symbol": data.get("symbol"),
                "price": data.get("price"),
                "timestamp": data.get("timestamp"),
                "strategy": data.get("strategy"),
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
            _live_state["alert_history"].insert(0, alert_entry)
            _live_state["alert_history"] = _live_state["alert_history"][:100]

            if session_id:
                try:
                    db_record_live_signal(
                        session_id=session_id,
                        signal_type=data.get("signal_type", ""),
                        symbol=data.get("symbol", ""),
                        close_price=data.get("price"),
                        was_traded=False,
                        was_deduped=False,
                        indicators=_live_state.get("indicator_values", {}),
                        strategy_id=strategy_id,
                        strategy_name=data.get("strategy", ""),
                        signal_at=data.get("timestamp"),
                    )
                except Exception as exc:
                    logger.debug("Failed to persist signal: %s", exc)
        elif event_type == "indicators":
            _live_state["indicator_values"] = data.get("values", {})
            _live_state["indicator_time"] = data.get("bar_time")
        elif event_type == "feed_status":
            _live_state["feed_status"] = data.get("message", "")
            _live_state["feed_connected"] = data.get("connected", False)
        elif event_type == "error":
            _live_state["error"] = data.get("message", "Unknown error")
        elif event_type == "trade":
            _live_state["trades_placed"] += 1
            status = data.get("status", "")
            if status == "filled":
                _live_state["trades_filled"] += 1
            elif status == "cancelled" or status == "timeout":
                _live_state["trades_cancelled"] += 1
            elif status == "rejected" or status == "error":
                _live_state["trades_rejected"] += 1
            trade_entry = {
                "signal_type": data.get("signal_type"),
                "symbol": data.get("symbol"),
                "size": data.get("size"),
                "order_id": data.get("order_id"),
                "fill_price": data.get("fill_price"),
                "status": status,
                "error": data.get("error"),
                "elapsed": data.get("elapsed"),
                "timestamp": data.get("timestamp"),
                "strategy": data.get("strategy"),
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
            _live_state["trade_history"].insert(0, trade_entry)
            _live_state["trade_history"] = _live_state["trade_history"][:100]

            if session_id:
                try:
                    db_record_live_trade(
                        session_id=session_id,
                        signal_type=data.get("signal_type", ""),
                        symbol=data.get("symbol", ""),
                        size=data.get("size", 1),
                        signal_price=data.get("price"),
                        fill_price=data.get("fill_price"),
                        order_id=str(data.get("order_id", "")),
                        status=status,
                        elapsed_seconds=data.get("elapsed"),
                        error_message=data.get("error", ""),
                        indicators=_live_state.get("indicator_values", {}),
                        strategy_id=strategy_id,
                        version_id=version_id,
                        strategy_name=data.get("strategy", ""),
                        signal_at=data.get("signal_timestamp"),
                        executed_at=data.get("timestamp"),
                    )
                except Exception as exc:
                    logger.debug("Failed to persist trade: %s", exc)
        elif event_type == "stopped":
            _live_state["running"] = False
            _live_state["stopped_at"] = datetime.now(timezone.utc).isoformat()
            _live_state["bars_injected"] = data.get("bars_injected", 0)
            _live_state["scans_run"] = data.get("scans_run", 0)
            _live_state["alerts_dispatched"] = data.get("alerts_dispatched", 0)
            if session_id:
                try:
                    db_update_live_session(
                        session_id,
                        status="stopped",
                        bars_injected=data.get("bars_injected", 0),
                        scans_run=data.get("scans_run", 0),
                        alerts_fired=data.get("alerts_dispatched", 0),
                        trades_placed=_live_state.get("trades_placed", 0),
                        trades_filled=_live_state.get("trades_filled", 0),
                        stopped_at=_live_state["stopped_at"],
                    )
                except Exception as exc:
                    logger.debug("Failed to update session on stop: %s", exc)

    # Emit SocketIO events if available
    try:
        from dashboard.app import socketio
        socketio.emit(event_type, data, namespace='/live')
    except Exception:
        pass


def _run_live_background(orchestrator) -> None:
    """Run the live orchestrator in a background thread."""
    try:
        orchestrator.start()
    except Exception as exc:
        logger.exception("Live orchestrator crashed: %s", exc)
        with _live_lock:
            _live_state["running"] = False
            _live_state["error"] = str(exc)
            _live_state["stopped_at"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@live_bp.route("/live")
def live_dashboard():
    """Live Signal Alert dashboard."""
    strategies = db_list_strategies()
    strategy_list = []
    for s in strategies:
        version = db_get_latest_version(s["id"])
        if version and version.get("afl_content"):
            strategy_list.append({
                "id": s["id"],
                "name": s["name"],
                "summary": s.get("summary", ""),
            })

    with _live_lock:
        state = {k: v for k, v in _live_state.items()
                 if k not in ("orchestrator", "_strategy_id", "_version_id")}

    return render_template(
        "live_dashboard.html",
        strategies=strategy_list,
        live_state=state,
    )


@live_bp.route("/api/live/accounts")
def api_live_accounts():
    """Fetch ProjectX accounts via REST API."""
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    async def _fetch():
        from project_x_py import ProjectX
        async with ProjectX.from_env() as client:
            await client.authenticate()
            accounts = await client.list_accounts()
            return [{"id": a.id, "name": a.name, "balance": a.balance,
                     "canTrade": a.canTrade, "simulated": a.simulated}
                    for a in accounts]

    loop = asyncio.new_event_loop()
    try:
        accounts = loop.run_until_complete(_fetch())
        return jsonify({"accounts": accounts})
    except Exception as exc:
        logger.error("Failed to fetch ProjectX accounts: %s", exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        loop.close()


@live_bp.route("/api/live/start", methods=["POST"])
def api_live_start():
    """Start a live signal alert session."""
    with _live_lock:
        if _live_state["running"]:
            return jsonify({"error": "A live session is already running"}), 409

    data = request.get_json(silent=True) or {}

    strategy_id = data.get("strategy_id")
    if not strategy_id:
        return jsonify({"error": "strategy_id is required"}), 400

    account_id = data.get("account_id")
    account_name = data.get("account_name", "Unknown")
    symbol = data.get("symbol", "NQH6")
    ami_symbol = data.get("ami_symbol", "NQ")
    bar_interval = int(data.get("bar_interval", 1))
    alert_channels = data.get("alert_channels", ["log"])
    trade_enabled = bool(data.get("trade_enabled", False))
    trade_size = int(data.get("trade_size", 1))
    trade_timeout = float(data.get("trade_timeout", 30))

    try:
        afl_path = _resolve_strategy_afl(strategy_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    strategy = db_get_strategy(strategy_id)
    strategy_name = strategy["name"] if strategy else "Unknown"

    version = db_get_latest_version(strategy_id)
    version_id = version["id"] if version else None

    session_id = db_create_live_session(
        strategy_id=strategy_id,
        version_id=version_id,
        account_id=str(account_id) if account_id else "",
        account_name=account_name,
        symbol=symbol,
        ami_symbol=ami_symbol,
        bar_interval=bar_interval,
        config={
            "alert_channels": alert_channels,
            "trade_enabled": trade_enabled,
            "trade_size": trade_size,
            "trade_timeout": trade_timeout,
        },
    )
    logger.info("Created persistent live session: %s", session_id)

    from scripts.live_signal_alert import LiveAlertOrchestrator

    orchestrator = LiveAlertOrchestrator(
        symbols=[symbol],
        ami_symbol=ami_symbol,
        interval=bar_interval,
        strategy_afl_path=afl_path,
        alert_channels=alert_channels,
        account_id=int(account_id) if account_id else None,
        status_callback=_live_status_callback,
        trade_enabled=trade_enabled,
        trade_symbol=symbol,
        trade_size=trade_size,
        trade_timeout=trade_timeout,
    )

    with _live_lock:
        _live_state.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stopped_at": None,
            "account_name": account_name,
            "account_id": account_id,
            "strategy_name": strategy_name,
            "strategy_afl_path": afl_path,
            "symbol": symbol,
            "ami_symbol": ami_symbol,
            "bar_interval": f"{bar_interval} min",
            "bars_injected": 0,
            "scans_run": 0,
            "alerts_dispatched": 0,
            "alert_history": [],
            "feed_status": "Starting...",
            "feed_connected": False,
            "last_scan_time": None,
            "error": None,
            "orchestrator": orchestrator,
            "trade_enabled": trade_enabled,
            "trades_placed": 0,
            "trades_filled": 0,
            "trades_cancelled": 0,
            "trades_rejected": 0,
            "trade_history": [],
            "indicator_values": {},
            "indicator_time": None,
            "session_id": session_id,
            "_strategy_id": strategy_id,
            "_version_id": version_id,
        })

    thread = threading.Thread(
        target=_run_live_background,
        args=(orchestrator,),
        daemon=True,
        name="LiveAlertSession",
    )
    thread.start()

    return jsonify({"status": "started", "strategy": strategy_name})


@live_bp.route("/api/live/status")
def api_live_status():
    """Return current live session state as JSON."""
    with _live_lock:
        state = {k: v for k, v in _live_state.items()
                 if k not in ("orchestrator", "_strategy_id", "_version_id")}
        orch = _live_state.get("orchestrator")
        if orch and not orch.is_running and _live_state["running"]:
            _live_state["running"] = False
            if not _live_state["stopped_at"]:
                _live_state["stopped_at"] = datetime.now(timezone.utc).isoformat()
            state["running"] = False
            state["stopped_at"] = _live_state["stopped_at"]
    return jsonify(state)


@live_bp.route("/api/live/stop", methods=["POST"])
def api_live_stop():
    """Stop the current live session."""
    with _live_lock:
        if not _live_state["running"]:
            return jsonify({"error": "No live session is running"}), 409
        orch = _live_state.get("orchestrator")

    if orch:
        orch._running = False
        return jsonify({"status": "stopping"})
    else:
        with _live_lock:
            _live_state["running"] = False
            _live_state["stopped_at"] = datetime.now(timezone.utc).isoformat()
        return jsonify({"status": "stopped"})


@live_bp.route("/api/live/proximity")
def api_live_proximity():
    """Get proximity-to-signal data for live strategies."""
    with _live_lock:
        if not _live_state["running"]:
            return jsonify({"error": "No live session running"}), 409

        indicator_values = dict(_live_state.get("indicator_values", {}))
        strategy_afl_path = _live_state.get("strategy_afl_path", "")

    thresholds = {}
    if strategy_afl_path:
        try:
            afl_content = Path(strategy_afl_path).read_text(encoding="utf-8")
            param_re = re.compile(
                r'Param\s*\(\s*"([^"]+)"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)',
            )
            for m in param_re.finditer(afl_content):
                name = m.group(1)
                default = float(m.group(2))
                thresholds[name] = {
                    "default": default,
                    "min": float(m.group(3)),
                    "max": float(m.group(4)),
                }

            condition_re = re.compile(
                r'(\w+)\s*([><=!]+)\s*([\d.]+)'
            )
            for m in condition_re.finditer(afl_content):
                var_name = m.group(1)
                op = m.group(2)
                value = float(m.group(3))
                if var_name not in thresholds:
                    thresholds[var_name] = {
                        "threshold": value,
                        "operator": op,
                    }
        except Exception:
            pass

    proximity = []
    for ind_name, ind_value in indicator_values.items():
        if not isinstance(ind_value, (int, float)):
            continue

        entry = {
            "name": ind_name,
            "value": round(ind_value, 4),
            "threshold": None,
            "proximity_pct": None,
            "direction": None,
        }

        clean_name = ind_name.replace("ind_", "").strip()
        for thresh_name, thresh_info in thresholds.items():
            if clean_name.lower() in thresh_name.lower() or thresh_name.lower() in clean_name.lower():
                if "threshold" in thresh_info:
                    threshold = thresh_info["threshold"]
                    entry["threshold"] = threshold
                    if threshold != 0:
                        entry["proximity_pct"] = round(
                            (ind_value / threshold) * 100, 1
                        )
                    entry["operator"] = thresh_info.get("operator", ">")
                    entry["direction"] = "above" if ">" in entry.get("operator", ">") else "below"
                elif "default" in thresh_info:
                    entry["threshold"] = thresh_info["default"]
                break

        proximity.append(entry)

    return jsonify({
        "indicators": proximity,
        "thresholds": thresholds,
        "timestamp": _live_state.get("indicator_time"),
    })


@live_bp.route("/api/live/kill", methods=["POST"])
def api_live_kill():
    """Emergency kill switch -- disable trade execution immediately."""
    flatten = request.args.get("flatten", "false").lower() == "true"
    with _live_lock:
        orch = _live_state.get("orchestrator")
        _live_state["trade_enabled"] = False

    if orch and orch.trade_executor:
        if flatten:
            orch.trade_executor.flatten_all()
            return jsonify({"status": "killed",
                            "message": "Trade execution disabled and "
                                       "position flatten requested."})
        else:
            orch.trade_executor.kill()
            return jsonify({"status": "killed",
                            "message": "Trade execution disabled. "
                                       "No further trades will be placed."})
    else:
        return jsonify({"status": "ok",
                        "message": "No active trade executor to kill."})


# ---------------------------------------------------------------------------
# Signal replay endpoints (D5)
# ---------------------------------------------------------------------------


@live_bp.route("/replay")
def replay_page():
    """Signal replay page — step through recorded sessions."""
    sessions = db_list_live_sessions(limit=50)
    strategies = db_list_strategies()
    return render_template("replay.html", sessions=sessions, strategies=strategies)


@live_bp.route("/api/replay/start", methods=["POST"])
def api_replay_start():
    """Start a replay session by loading events from a recorded live session."""
    from flask import current_app
    from scripts.trade_replay import TradeReplay

    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    afl_path = data.get("afl_path", "")
    symbol = data.get("symbol", "NQ")
    bar_interval = int(data.get("bar_interval", 1))
    param_overrides = data.get("param_overrides", {})

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    # Resolve AFL path from session's strategy if not provided
    if not afl_path:
        session_info = db_get_live_session(session_id)
        if session_info:
            strategy_id = session_info.get("strategy_id")
            if strategy_id:
                try:
                    afl_path = _resolve_strategy_afl(strategy_id)
                except ValueError:
                    afl_path = ""
            symbol = session_info.get("ami_symbol") or symbol
            bar_interval = session_info.get("bar_interval") or bar_interval

    replay = TradeReplay(
        strategy_afl_path=afl_path,
        symbol=symbol,
        bar_interval=bar_interval,
        session_id=session_id,
        param_overrides=param_overrides,
    )
    event_count = replay.load_bars(source="session")

    current_app.config["_replay"] = replay

    return jsonify({
        "status": "loaded",
        "event_count": event_count,
        "progress": replay.progress,
        "summary": replay.get_summary(),
    })


@live_bp.route("/api/replay/step", methods=["POST"])
def api_replay_step():
    """Step through the replay."""
    from flask import current_app
    replay = current_app.config.get("_replay")
    if not replay:
        return jsonify({"error": "No replay session active"}), 409

    data = request.get_json(silent=True) or {}
    num_bars = int(data.get("num_bars", 1))
    direction = data.get("direction", "forward")

    if direction == "back":
        events = replay.step_back(num_bars)
    elif direction == "jump":
        position = int(data.get("position", 0))
        events = replay.jump_to(position)
    elif direction == "reset":
        replay.reset()
        events = []
    elif direction == "end":
        events = replay.step_to_end()
    else:
        events = replay.step(num_bars)

    return jsonify({
        "events": events,
        "progress": replay.progress,
    })


@live_bp.route("/api/replay/events")
def api_replay_events():
    """Get all events from the loaded replay."""
    from flask import current_app
    replay = current_app.config.get("_replay")
    if not replay:
        return jsonify({"error": "No replay session active"}), 409
    return jsonify({
        "events": replay.get_all_events(),
        "progress": replay.progress,
    })


@live_bp.route("/api/replay/summary")
def api_replay_summary():
    """Get replay summary."""
    from flask import current_app
    replay = current_app.config.get("_replay")
    if not replay:
        return jsonify({"error": "No replay session active"}), 409
    return jsonify(replay.get_summary())
