"""
Backtest blueprint -- running backtests, status, results viewing,
stage/approve/reject, downloads, equity curves, and the index page.
"""

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request,
    send_from_directory, url_for,
)

from config.settings import (
    RESULTS_DIR, AFL_STRATEGY_FILE, AMIBROKER_DB_PATH, LOGS_DIR,
    PROJECT_ROOT, DEFAULT_SYMBOL,
)
from dashboard.state import (
    _backtest_state, _backtest_lock,
)
from dashboard.helpers import (
    get_status, get_result_files, parse_results_csv, compute_equity_curve,
    get_afl_content, get_afl_versions, extract_indicators, count_params,
    STAGED_DIR,
)
from scripts.strategy_db import (
    get_strategy_info, get_strategy_summary, get_run_with_context,
    get_strategy as db_get_strategy, list_strategies as db_list_strategies,
    list_versions as db_list_versions, list_runs as db_list_runs,
    get_version as db_get_version, get_latest_version as db_get_latest_version,
    get_run as db_get_run, update_run as db_update_run,
    list_batches as db_list_batches,
    reconstruct_optimization_parsed,
)

logger = logging.getLogger(__name__)

backtest_bp = Blueprint("backtest_bp", __name__)


# ---------------------------------------------------------------------------
# Background backtest runner
# ---------------------------------------------------------------------------


def _run_backtest_background(strategy_id: str = None, version_id: str = None,
                             run_mode: int = None, symbol: str = None,
                             date_range: str = None):
    """Run the backtest in a background thread via run.py."""
    global _backtest_state
    is_optimization = (run_mode == 4)
    with _backtest_lock:
        _backtest_state["running"] = True
        _backtest_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _backtest_state["finished_at"] = None
        _backtest_state["success"] = None
        _backtest_state["error"] = None
        _backtest_state["run_id"] = None
        _backtest_state["is_optimization"] = is_optimization

    sentinel_path = RESULTS_DIR / ".current_run_id"
    if sentinel_path.exists():
        try:
            sentinel_path.unlink()
        except OSError:
            pass

    proc = None
    proc_log_path = LOGS_DIR / "run_subprocess.log"
    proc_log_file = None
    try:
        cmd = [sys.executable, str(PROJECT_ROOT / "run.py")]
        if strategy_id:
            cmd.extend(["--strategy-id", strategy_id])
        if version_id:
            cmd.extend(["--version-id", version_id])
        if run_mode is not None:
            cmd.extend(["--run-mode", str(run_mode)])
        if symbol:
            cmd.extend(["--symbol", symbol])
        if date_range:
            cmd.extend(["--date-range", date_range])

        proc_log_file = open(proc_log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=proc_log_file,
            stderr=proc_log_file,
            text=True,
            cwd=str(PROJECT_ROOT),
        )

        deadline = time.time() + 600
        while proc.poll() is None:
            if _backtest_state["run_id"] is None and sentinel_path.exists():
                try:
                    rid = sentinel_path.read_text(encoding="utf-8").strip()
                    if rid:
                        with _backtest_lock:
                            _backtest_state["run_id"] = rid
                        logger.info("Discovered run_id from sentinel: %s", rid)
                except OSError:
                    pass

            if time.time() > deadline:
                proc.kill()
                with _backtest_lock:
                    _backtest_state["success"] = False
                    _backtest_state["error"] = "Backtest timed out after 10 minutes"
                    rid = _backtest_state.get("run_id")
                if rid:
                    try:
                        db_update_run(
                            rid,
                            status="failed",
                            metrics_json=json.dumps({"error": "Subprocess timed out after 10 minutes"}),
                            completed_at=datetime.now(timezone.utc).isoformat(),
                        )
                        logger.warning("Marked run %s as failed (timeout)", rid)
                    except Exception:
                        logger.exception("Failed to mark timed-out run as failed in DB")
                return

            time.sleep(1)

        returncode = proc.returncode

        proc_log_file.close()
        proc_log_file = None
        try:
            proc_output = proc_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            proc_output = ""

        with _backtest_lock:
            _backtest_state["success"] = returncode == 0
            if returncode == 0:
                if _backtest_state["run_id"] is None and sentinel_path.exists():
                    try:
                        rid = sentinel_path.read_text(encoding="utf-8").strip()
                        if rid:
                            _backtest_state["run_id"] = rid
                    except OSError:
                        pass
                if _backtest_state["run_id"] is None:
                    match = re.search(r"Run ID:\s+([0-9a-f-]{36})", proc_output)
                    if match:
                        _backtest_state["run_id"] = match.group(1)
            else:
                _backtest_state["error"] = proc_output[-500:] if proc_output else "Unknown error"

    except Exception as e:
        with _backtest_lock:
            _backtest_state["success"] = False
            _backtest_state["error"] = str(e)
    finally:
        if proc_log_file is not None:
            try:
                proc_log_file.close()
            except Exception:
                pass
        with _backtest_lock:
            _backtest_state["running"] = False
            _backtest_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            rid = _backtest_state.get("run_id")
            success = _backtest_state.get("success")

        if rid and not success:
            try:
                run_record = db_get_run(rid)
                if run_record and run_record.get("status") == "running":
                    error_msg = _backtest_state.get("error") or "Subprocess exited unexpectedly"
                    db_update_run(
                        rid,
                        status="failed",
                        metrics_json=json.dumps({"error": error_msg}),
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    logger.warning("Safety net: marked run %s as failed in DB", rid)
            except Exception:
                logger.exception("Failed to update DB status for crashed run")


# ---------------------------------------------------------------------------
# Index / results routes
# ---------------------------------------------------------------------------


@backtest_bp.route("/")
def index():
    """Dashboard home -- list all strategies with version/run counts."""
    strategies = db_list_strategies()
    strategy_summaries = []
    all_indicators = set()
    for s in strategies:
        summary = get_strategy_summary(s["id"])
        if summary:
            afl = ""
            if summary.get("latest_version"):
                afl = summary["latest_version"].get("afl_content", "")
            summary["indicators"] = extract_indicators(afl)
            summary["param_count"] = count_params(afl)
            all_indicators.update(summary["indicators"])
            strategy_summaries.append(summary)

    result_files = get_result_files()
    batches = db_list_batches(limit=5)

    return render_template(
        "index.html",
        strategies=strategy_summaries,
        result_files=result_files,
        all_indicators=sorted(all_indicators),
        get_strategy_info=get_strategy_info,
        db_configured=bool(AMIBROKER_DB_PATH),
        backtest_state=_backtest_state,
        batches=batches,
    )


@backtest_bp.route("/results/<filename>")
def results_detail(filename: str):
    """Detail view of a single CSV result set (legacy flat file route)."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists() or not filepath.suffix == ".csv":
        flash(f"Result file '{filename}' not found.", "danger")
        return redirect(url_for("backtest_bp.index"))

    parsed = parse_results_csv(filepath)
    status = get_status(filepath)

    html_companion = filepath.with_suffix(".html")
    has_html = html_companion.exists()

    return render_template(
        "results_detail.html",
        filename=filename,
        parsed=parsed,
        status=status,
        has_html=has_html,
        strategy=get_strategy_info(filename),
        afl_content=get_afl_content(),
        afl_path=str(AFL_STRATEGY_FILE),
        versions=get_afl_versions(),
        db_configured=bool(AMIBROKER_DB_PATH),
        run=None,
        is_optimization=parsed.get("is_optimization", False),
        indicator_configs=[],
        symbol_runs={},
    )


@backtest_bp.route("/run/<run_id>")
def run_detail(run_id: str):
    """Detail view of a GUID-based backtest run."""
    run = get_run_with_context(run_id)
    if run is None:
        flash(f"Run '{run_id}' not found.", "danger")
        return redirect(url_for("backtest_bp.index"))

    results_dir_path = PROJECT_ROOT / run["results_dir"] if run["results_dir"] else RESULTS_DIR
    csv_filename = run.get("results_csv", "results.csv")
    filepath = results_dir_path / csv_filename

    if not filepath.exists() and not run["results_dir"]:
        filepath = RESULTS_DIR / csv_filename

    sql_parsed = None
    if run.get("is_optimization") and run.get("total_combos", 0) > 0:
        try:
            sql_parsed = reconstruct_optimization_parsed(run_id)
        except Exception as exc:
            logger.warning("SQL optimization reconstruction failed: %s", exc)

    if not filepath.exists():
        if sql_parsed:
            parsed = sql_parsed
            status = run.get("status", "completed")
            has_html = False
        else:
            run_metrics = run.get("metrics") or {}
            error_msg = run_metrics.get("error")
            validation_errors = run_metrics.get("validation_errors", [])
            if error_msg:
                full_error = error_msg
                if validation_errors:
                    full_error += "\n" + "\n".join(f"  - {e}" for e in validation_errors)
            elif run.get("status") == "failed":
                full_error = "Run failed -- no results were produced."
            else:
                full_error = f"Result file not found: {filepath}"
            parsed = {"trades": [], "metrics": {}, "columns": [], "error": full_error, "is_optimization": False}
            status = run.get("status", "pending")
            has_html = False
    else:
        if sql_parsed:
            parsed = sql_parsed
        else:
            run_params = run.get("params") or {}
            force_opt = (run_params.get("run_mode") == 4) or (run.get("metrics", {}).get("run_mode") == 4)
            run_symbol = run.get("symbol") or ""
            selected_symbol = request.args.get("symbol", "").strip()
            if run_symbol == "__ALL__" and selected_symbol:
                effective_filter = selected_symbol
            else:
                effective_filter = run_symbol
            parsed = parse_results_csv(filepath, force_optimization=force_opt, symbol_filter=effective_filter)
        status = get_status(filepath)
        html_companion = filepath.with_suffix(".html")
        has_html = html_companion.exists()

    is_optimization = parsed.get("is_optimization", False)
    strategy = run.get("strategy") or get_strategy_info(run["strategy_id"])
    version = run.get("version")

    from scripts.afl_parser import extract_strategy_indicators

    _afl = run.get("afl_content") or (version.get("afl_content", "") if version else "")
    indicator_configs = extract_strategy_indicators(_afl) if _afl else []

    symbol_runs = {}
    current_version_id = run.get("version_id")
    sibling_runs = db_list_runs(strategy_id=run["strategy_id"])
    for r in sibling_runs:
        if r.get("status") != "completed":
            continue
        if r.get("version_id") != current_version_id:
            continue
        sym = r.get("symbol") or DEFAULT_SYMBOL
        if sym not in symbol_runs:
            symbol_runs[sym] = {"run_id": r["id"], "symbol": sym}

    run_symbol = run.get("symbol") or ""
    selected_symbol = request.args.get("symbol", "").strip()
    if run_symbol == "__ALL__" and filepath.exists():
        try:
            _csv_df = pd.read_csv(filepath, encoding="utf-8")
            _sym_col = None
            for col in _csv_df.columns:
                if col.strip().lower() == "symbol":
                    _sym_col = col
                    break
            if _sym_col is not None:
                csv_symbols = sorted(
                    _csv_df[_sym_col].astype(str).str.strip().str.upper().unique()
                )
                for csv_sym in csv_symbols:
                    if csv_sym:
                        symbol_runs[csv_sym] = {
                            "run_id": run_id,
                            "symbol": csv_sym,
                            "query": f"?symbol={csv_sym}",
                        }
                symbol_runs["__ALL__"] = {
                    "run_id": run_id,
                    "symbol": "__ALL__",
                }
        except Exception:
            pass

    active_symbol = selected_symbol if selected_symbol else (run_symbol or DEFAULT_SYMBOL)

    return render_template(
        "results_detail.html",
        filename=csv_filename,
        parsed=parsed,
        status=status,
        has_html=has_html,
        strategy=strategy,
        afl_content=run.get("afl_content") or (version.get("afl_content", "") if version else get_afl_content()),
        afl_path=str(AFL_STRATEGY_FILE),
        versions=get_afl_versions(),
        db_configured=bool(AMIBROKER_DB_PATH),
        run=run,
        is_optimization=is_optimization,
        default_symbol=DEFAULT_SYMBOL,
        symbol_runs=symbol_runs,
        indicator_configs=indicator_configs,
        active_symbol=active_symbol,
        selected_symbol=selected_symbol,
    )


# ---------------------------------------------------------------------------
# Stage / Approve / Reject
# ---------------------------------------------------------------------------


@backtest_bp.route("/results/<filename>/stage", methods=["POST"])
def stage_result(filename: str):
    """Copy the result CSV (and HTML if present) to results/staged/."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists():
        flash(f"Result file '{filename}' not found.", "danger")
        return redirect(url_for("backtest_bp.index"))

    try:
        STAGED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(filepath, STAGED_DIR / filename)

        html_companion = filepath.with_suffix(".html")
        if html_companion.exists():
            shutil.copy2(html_companion, STAGED_DIR / html_companion.name)

        flash(f"'{filename}' has been staged successfully.", "success")
    except Exception as exc:
        flash(f"Failed to stage '{filename}': {exc}", "danger")

    return redirect(url_for("backtest_bp.results_detail", filename=filename))


@backtest_bp.route("/results/<filename>/approve", methods=["POST"])
def approve_result(filename: str):
    """Mark a result set as approved via a JSON sidecar file."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists():
        flash(f"Result file '{filename}' not found.", "danger")
        return redirect(url_for("backtest_bp.index"))

    sidecar = filepath.parent / f"{filename}.status.json"
    payload = {
        "status": "approved",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reviewer": "user",
    }
    try:
        sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        flash(f"'{filename}' has been approved.", "success")
    except Exception as exc:
        flash(f"Failed to approve '{filename}': {exc}", "danger")

    return redirect(url_for("backtest_bp.results_detail", filename=filename))


@backtest_bp.route("/results/<filename>/reject", methods=["POST"])
def reject_result(filename: str):
    """Mark a result set as rejected via a JSON sidecar file."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists():
        flash(f"Result file '{filename}' not found.", "danger")
        return redirect(url_for("backtest_bp.index"))

    sidecar = filepath.parent / f"{filename}.status.json"
    payload = {
        "status": "rejected",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reviewer": "user",
    }
    try:
        sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        flash(f"'{filename}' has been rejected.", "warning")
    except Exception as exc:
        flash(f"Failed to reject '{filename}': {exc}", "danger")

    return redirect(url_for("backtest_bp.results_detail", filename=filename))


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@backtest_bp.route("/logs")
def logs():
    """Display the latest log file content."""
    log_file = LOGS_DIR / "ole_backtest.log"
    log_content = ""
    if log_file.exists():
        try:
            log_content = log_file.read_text(encoding="utf-8")
        except Exception as exc:
            log_content = f"Error reading log file: {exc}"
    else:
        log_content = "No log file found. Run a backtest first to generate logs."
    return render_template("logs.html", log_content=log_content)


# ---------------------------------------------------------------------------
# API Results / Download / Equity Curve
# ---------------------------------------------------------------------------


@backtest_bp.route("/api/results/<filename>")
def api_results(filename: str):
    """JSON API endpoint returning parsed CSV data and metrics."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists() or not filepath.suffix == ".csv":
        return jsonify({"error": f"Result file '{filename}' not found."}), 404

    parsed = parse_results_csv(filepath)
    status = get_status(filepath)

    return jsonify(
        {
            "filename": filename,
            "status": status,
            "strategy": get_strategy_info(filename),
            "metrics": parsed["metrics"],
            "columns": parsed["columns"],
            "trades": parsed["trades"],
            "error": parsed["error"],
            "equity_curve_url": url_for("backtest_bp.api_equity_curve", filename=filename),
        }
    )


@backtest_bp.route("/download/<filename>")
def download_file(filename: str):
    """Serve a file from the results directory for download."""
    return send_from_directory(str(RESULTS_DIR), filename, as_attachment=True)


@backtest_bp.route("/run/<run_id>/download/<filename>")
def download_run_file(run_id: str, filename: str):
    """Serve a file from a GUID-based run results directory."""
    run = db_get_run(run_id)
    if run is None:
        return "Run not found", 404
    run_dir = PROJECT_ROOT / run["results_dir"]
    if not run_dir.exists():
        return "Results directory not found", 404
    return send_from_directory(str(run_dir), filename, as_attachment=True)


@backtest_bp.route("/api/strategies")
def api_strategies():
    """JSON API endpoint returning all strategies with summary info."""
    strategies = db_list_strategies()
    summaries = []
    for s in strategies:
        summary = get_strategy_summary(s["id"])
        if summary:
            summaries.append(summary)
    return jsonify(summaries)


@backtest_bp.route("/api/strategy/<strategy_id>/versions")
def api_versions(strategy_id: str):
    """JSON API endpoint returning all versions for a strategy."""
    versions = db_list_versions(strategy_id)
    return jsonify(versions)


@backtest_bp.route("/api/strategy/<strategy_id>/runs")
def api_runs(strategy_id: str):
    """JSON API endpoint returning all runs for a strategy."""
    runs = db_list_runs(strategy_id=strategy_id)
    return jsonify(runs)


@backtest_bp.route("/api/run/<run_id>")
def api_run_detail(run_id: str):
    """JSON API endpoint returning full run details with context."""
    run = get_run_with_context(run_id)
    if run is None:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(run)


@backtest_bp.route("/api/results/<filename>/equity-curve")
def api_equity_curve(filename: str):
    """JSON API endpoint returning equity curve data for a result CSV."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404
    data = compute_equity_curve(filepath)
    return jsonify(data)


@backtest_bp.route("/api/run/<run_id>/equity-curve")
def api_run_equity_curve(run_id: str):
    """JSON API endpoint returning equity curve data for a GUID-based run."""
    run = db_get_run(run_id)
    if run is None:
        return jsonify({"error": "Run not found"}), 404

    results_dir_path = PROJECT_ROOT / run["results_dir"] if run.get("results_dir") else RESULTS_DIR
    csv_filename = run.get("results_csv", "results.csv")
    filepath = results_dir_path / csv_filename

    if not filepath.exists() and not run.get("results_dir"):
        filepath = RESULTS_DIR / csv_filename

    if not filepath.exists():
        return jsonify({"error": f"Result file not found: {filepath}"}), 404

    run_symbol = run.get("symbol") or ""
    selected_symbol = request.args.get("symbol", "").strip()
    if run_symbol == "__ALL__" and selected_symbol:
        effective_filter = selected_symbol
    else:
        effective_filter = run_symbol
    data = compute_equity_curve(filepath, symbol_filter=effective_filter)
    return jsonify(data)


# ---------------------------------------------------------------------------
# Backtest run / status / abort / optimization progress
# ---------------------------------------------------------------------------


@backtest_bp.route("/backtest/run", methods=["POST"])
def backtest_run():
    """Start a backtest in a background thread."""
    with _backtest_lock:
        if _backtest_state["running"]:
            flash("A backtest is already running. Please wait.", "warning")
            return redirect(url_for("backtest_bp.logs"))

    if not AMIBROKER_DB_PATH:
        flash("AMIBROKER_DB_PATH not configured in settings.", "danger")
        return redirect(url_for("backtest_bp.index"))

    strategy_id = request.form.get("strategy_id", "").strip() or None
    version_id = request.form.get("version_id", "").strip() or None
    symbol = request.form.get("symbol", "").strip() or None
    date_range = request.form.get("date_range", "").strip() or None

    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, version_id, None, symbol, date_range),
        daemon=True,
    )
    thread.start()
    flash("Backtest started. Monitoring progress...", "info")
    return redirect(url_for("backtest_bp.backtest_status_page"))


@backtest_bp.route("/backtest/status")
def backtest_status_page():
    """Backtest status page with log output."""
    log_content = ""
    log_file = LOGS_DIR / "ole_backtest.log"
    if log_file.exists():
        try:
            log_content = log_file.read_text(encoding="utf-8")
        except Exception:
            log_content = "Error reading log file."
    with _backtest_lock:
        state = dict(_backtest_state)

    if state.get("running") and state.get("run_id"):
        run_record = db_get_run(state["run_id"])
        if run_record and run_record.get("status") in ("completed", "failed"):
            state["running"] = False
            state["success"] = run_record["status"] == "completed"
            if not state.get("finished_at"):
                state["finished_at"] = run_record.get("completed_at",
                    datetime.now(timezone.utc).isoformat())

    run_symbol = ""
    run_strategy_id = ""
    if state.get("run_id"):
        run_record = db_get_run(state["run_id"])
        if run_record:
            run_symbol = run_record.get("symbol", "")
            run_strategy_id = run_record.get("strategy_id", "")

    return render_template(
        "backtest_status.html",
        state=state,
        log_content=log_content,
        is_optimization=state.get("is_optimization", False),
        run_symbol=run_symbol,
        run_strategy_id=run_strategy_id,
    )


@backtest_bp.route("/api/backtest/status")
def api_backtest_status():
    """JSON API endpoint returning current backtest state."""
    with _backtest_lock:
        state = dict(_backtest_state)
    return jsonify(state)


@backtest_bp.route("/api/run/<run_id>/opt-progress")
def api_opt_progress(run_id: str):
    """Return real-time optimization progress for a running backtest."""
    run_dir = RESULTS_DIR / run_id
    status_file = run_dir / "opt_status.json"

    with _backtest_lock:
        is_running = _backtest_state["running"] and _backtest_state.get("run_id") == run_id
        run_success = _backtest_state.get("success")

    run_record = db_get_run(run_id)
    db_status = run_record["status"] if run_record else None

    if db_status in ("completed", "failed"):
        run_status = db_status
    elif is_running:
        run_status = "running"
    elif run_success is not None:
        run_status = "completed" if run_success else "failed"
    else:
        run_status = db_status or "unknown"

    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            data["run_status"] = run_status
            return jsonify(data)
        except (json.JSONDecodeError, OSError):
            pass

    return jsonify({
        "combo": 0,
        "total": 0,
        "elapsed": 0,
        "pct": 0,
        "eta_seconds": 0,
        "rate": 0,
        "run_status": run_status,
    })


@backtest_bp.route("/api/run/<run_id>/abort", methods=["POST"])
def api_abort_run(run_id: str):
    """Request abort of a running optimization/backtest."""
    run_dir = RESULTS_DIR / run_id
    if not run_dir.exists():
        return jsonify({"error": "Run directory not found"}), 404

    abort_path = run_dir / "abort_requested"
    try:
        abort_path.write_text("1", encoding="utf-8")
        return jsonify({"status": "abort_requested"})
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# AFL editor routes
# ---------------------------------------------------------------------------


@backtest_bp.route("/afl")
def afl_editor():
    """AFL editor page -- view and edit the AFL strategy file."""
    from dashboard.helpers import get_afl_content, get_afl_versions
    content = get_afl_content()
    return render_template(
        "afl_editor.html",
        afl_content=content,
        afl_path=str(AFL_STRATEGY_FILE),
        versions=get_afl_versions(),
    )


@backtest_bp.route("/afl/save", methods=["POST"])
def afl_save():
    """Save AFL content and optionally create a version."""
    from dashboard.helpers import (
        validate_afl_content, save_afl_content, save_afl_version,
    )
    content = request.form.get("afl_content", "")
    version_label = request.form.get("version_label", "").strip()
    create_version = request.form.get("create_version") == "on"
    redirect_to = request.form.get("redirect_to", "")

    if not content.strip():
        flash("AFL content cannot be empty.", "danger")
        return redirect(url_for("backtest_bp.afl_editor"))

    afl_warnings = validate_afl_content(content)
    for warning in afl_warnings:
        flash(f"AFL warning: {warning}", "warning")

    success, message = save_afl_content(content)
    if success:
        flash("AFL saved and APX rebuilt.", "success")
    else:
        flash(f"Error saving AFL: {message}", "danger")
        return redirect(url_for("backtest_bp.afl_editor"))

    if create_version or version_label:
        v_ok, v_msg = save_afl_version(content, version_label or "")
        if v_ok:
            flash(f"Version saved: {v_msg}", "info")
        else:
            flash(f"Version save failed: {v_msg}", "warning")

    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("backtest_bp.afl_editor"))


@backtest_bp.route("/afl/versions/<version_name>/load", methods=["POST"])
def afl_load_version(version_name: str):
    """Load a specific AFL version into the editor."""
    from dashboard.helpers import load_afl_version, save_afl_content
    ok, content_or_error = load_afl_version(version_name)
    if ok:
        success, message = save_afl_content(content_or_error)
        if success:
            flash(f"Loaded version '{version_name}' and rebuilt APX.", "success")
        else:
            flash(f"Loaded version but APX rebuild failed: {message}", "warning")
    else:
        flash(f"Failed to load version: {content_or_error}", "danger")

    redirect_to = request.form.get("redirect_to", "")
    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("backtest_bp.afl_editor"))


@backtest_bp.route("/api/afl/versions/<version_name>")
def api_afl_version(version_name: str):
    """Return the content of a specific AFL version."""
    from dashboard.helpers import load_afl_version
    ok, content_or_error = load_afl_version(version_name)
    if ok:
        return jsonify({"content": content_or_error, "name": version_name})
    return jsonify({"error": content_or_error}), 404


# ---------------------------------------------------------------------------
# Run indicators API
# ---------------------------------------------------------------------------


@backtest_bp.route("/api/run/<run_id>/indicators")
def api_run_indicators(run_id: str):
    """Return indicator presets parsed from a backtest run's AFL content."""
    from scripts.afl_parser import parse_afl_indicators

    run = get_run_with_context(run_id)
    if run is None:
        return jsonify({"error": "Run not found"}), 404

    afl_content = run.get("afl_content") or ""
    if not afl_content and run.get("version"):
        afl_content = run["version"].get("afl_content", "")

    indicators = parse_afl_indicators(afl_content)
    return jsonify({"indicators": indicators, "run_id": run_id})


@backtest_bp.route("/api/indicators")
def api_indicators():
    """Return list of available indicator types."""
    from scripts.indicators import get_available_indicators
    return jsonify(get_available_indicators())
