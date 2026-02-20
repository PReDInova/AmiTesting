"""
Batch blueprint -- batch runs, status, cancel, history.
"""

import logging
import threading

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from config.settings import AMIBROKER_DB_PATH
from dashboard.state import _batch_state, _batch_lock
from scripts.strategy_db import (
    get_strategy as db_get_strategy,
    list_strategies as db_list_strategies,
    get_run as db_get_run,
    create_batch as db_create_batch,
    update_batch as db_update_batch,
    get_batch as db_get_batch,
    list_batches as db_list_batches,
)

logger = logging.getLogger(__name__)

batch_bp = Blueprint("batch_bp", __name__)


# ---------------------------------------------------------------------------
# Background batch runner
# ---------------------------------------------------------------------------


def _run_batch_background(batch_id: str):
    """Run a batch backtest in a background thread."""
    from scripts.batch_runner import BatchRunner
    runner = BatchRunner(batch_id)
    with _batch_lock:
        _batch_state["running"] = True
        _batch_state["batch_id"] = batch_id
        _batch_state["runner"] = runner
    try:
        runner.run_batch()
    finally:
        with _batch_lock:
            _batch_state["running"] = False
            _batch_state["batch_id"] = None
            _batch_state["runner"] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@batch_bp.route("/api/batch/backtest", methods=["POST"])
def api_batch_start():
    """Start a batch backtest across multiple strategies."""
    with _batch_lock:
        if _batch_state["running"]:
            return jsonify({"error": "A batch is already running."}), 409

    data = request.get_json(silent=True) or {}
    strategy_ids = data.get("strategy_ids", [])
    run_mode = data.get("run_mode", 2)
    name = data.get("name", "")

    if not strategy_ids:
        strategy_ids = [s["id"] for s in db_list_strategies()]

    if not strategy_ids:
        return jsonify({"error": "No strategies found to run."}), 400

    if not name:
        name = f"Batch run ({len(strategy_ids)} strategies)"

    batch_id = db_create_batch(
        name=name,
        strategy_ids=strategy_ids,
        run_mode=run_mode,
    )

    thread = threading.Thread(
        target=_run_batch_background,
        args=(batch_id,),
        daemon=True,
    )
    thread.start()

    return jsonify({"batch_id": batch_id, "total": len(strategy_ids), "status": "running"})


@batch_bp.route("/api/batch/<batch_id>/status")
def api_batch_status(batch_id: str):
    """Poll batch progress."""
    batch = db_get_batch(batch_id)
    if batch is None:
        return jsonify({"error": "Batch not found"}), 404

    runs = []
    for run_id in batch.get("run_ids", []):
        run = db_get_run(run_id)
        if run:
            runs.append(run)
    batch["runs"] = runs

    return jsonify(batch)


@batch_bp.route("/api/batch/<batch_id>/cancel", methods=["POST"])
def api_batch_cancel(batch_id: str):
    """Cancel a running batch."""
    with _batch_lock:
        if _batch_state["batch_id"] != batch_id:
            return jsonify({"error": "Batch is not currently running."}), 409
        runner = _batch_state.get("runner")
        if runner:
            runner.cancel()

    return jsonify({"status": "cancelling"})


@batch_bp.route("/api/batch/list")
def api_batch_list():
    """List all batch runs."""
    batches = db_list_batches()
    return jsonify(batches)


@batch_bp.route("/batch/<batch_id>")
def batch_dashboard(batch_id: str):
    """Batch dashboard HTML page."""
    batch = db_get_batch(batch_id)
    if batch is None:
        flash(f"Batch '{batch_id}' not found.", "danger")
        return redirect(url_for("backtest_bp.index"))

    strategies_map = {}
    for sid in batch.get("strategy_ids", []):
        s = db_get_strategy(sid)
        if s:
            strategies_map[sid] = s

    return render_template(
        "batch_dashboard.html",
        batch=batch,
        strategies=strategies_map,
        db_configured=bool(AMIBROKER_DB_PATH),
    )


@batch_bp.route("/batch/history")
def batch_history():
    """Batch history page listing all batches."""
    batches = db_list_batches()
    return render_template(
        "batch_history.html",
        batches=batches,
        db_configured=bool(AMIBROKER_DB_PATH),
    )
