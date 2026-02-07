"""
Flask application for the AmiTesting Results Dashboard.

Provides a web interface to browse, review, stage, and approve/reject
backtest result CSV files produced by AmiBroker OLE automation.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so config.settings can be imported
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from config.settings import (
    RESULTS_DIR, RESULTS_HTML, RESULTS_CSV, PROJECT_ROOT, LOGS_DIR,
    AFL_STRATEGY_FILE, APX_TEMPLATE, APX_OUTPUT, AMIBROKER_DB_PATH,
    BACKTEST_SETTINGS, LOG_FILE
)

import pandas as pd
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App factory / configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ami-testing-dashboard-dev-key")

STAGED_DIR: Path = RESULTS_DIR / "staged"

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
}
_backtest_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Strategy descriptions registry
# ---------------------------------------------------------------------------

STRATEGY_DESCRIPTIONS = {
    "results.csv": {
        "name": "Moving Average Crossover (10/50)",
        "summary": "Buys gold futures when short-term momentum crosses above the longer-term trend, sells when it crosses below.",
        "description": (
            "This strategy tracks two moving averages of the daily closing price of gold futures (/GC). "
            "A 'fast' average looks at the last 10 trading days (about 2 weeks), while a 'slow' average "
            "looks at the last 50 trading days (about 2.5 months).\n\n"
            "When the fast average crosses above the slow average, it signals that recent prices are "
            "gaining momentum compared to the longer-term trend \u2014 the system interprets this as a bullish "
            "signal and buys one gold futures contract. When the fast average drops back below the slow "
            "average, momentum is fading and the system sells to exit the position.\n\n"
            "This is a 'trend-following' approach: it works best in markets with sustained directional "
            "moves and tends to struggle in choppy, sideways conditions. The 10/50 period combination "
            "is a moderate setting \u2014 fast enough to catch trends but not so fast that it generates "
            "excessive false signals.\n\n"
            "When reviewing results, look at:\n"
            "- Win rate: Trend-following strategies typically win 40-60% of trades\n"
            "- Average win vs average loss: Winners should be significantly larger than losers\n"
            "- Max drawdown: The worst peak-to-trough decline \u2014 indicates risk\n"
            "- Total profit: Net P&L after all trades, in dollars ($100 per point for /GC)"
        ),
        "parameters": [
            {"name": "Fast MA Period", "value": "10 days (~2 weeks)"},
            {"name": "Slow MA Period", "value": "50 days (~2.5 months)"},
            {"name": "Position Size", "value": "1 contract"},
            {"name": "Entry Signal", "value": "Fast MA crosses above Slow MA"},
            {"name": "Exit Signal", "value": "Slow MA crosses above Fast MA"},
            {"name": "Symbol", "value": "GCZ25 (Gold Futures, Dec 2025)"},
            {"name": "Timeframe", "value": "Daily bars"},
            {"name": "Starting Capital", "value": "$100,000"},
            {"name": "Commissions", "value": "None (clean test)"},
            {"name": "Point Value", "value": "$100 per point"},
        ],
        "symbol": "/GC Gold Futures (GCZ25)",
        "risk_notes": (
            "This is a Sprint 1 verification test \u2014 the goal is to validate that the OLE automation "
            "pipeline works, not to find a profitable strategy. Results should be reviewed for "
            "technical correctness (trades execute, metrics compute) rather than profitability. "
            "No commissions are included, which overstates real-world performance."
        ),
    },
    "_default": {
        "name": "Unknown Strategy",
        "summary": "Backtest results from an unregistered strategy.",
        "description": "No description is available for this result set. It may have been generated by a custom or experimental strategy.",
        "parameters": [],
        "symbol": "Unknown",
        "risk_notes": "Review results carefully \u2014 no strategy metadata is available.",
    },
}


def get_strategy_info(filename: str) -> dict:
    """Return strategy description metadata for the given result filename.

    Falls back to the ``_default`` entry when the filename is not registered.
    """
    return STRATEGY_DESCRIPTIONS.get(filename, STRATEGY_DESCRIPTIONS["_default"])


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_status(filepath: Path) -> str:
    """Read the JSON sidecar status file for a given CSV, if it exists.

    The sidecar is named ``<csv_filename>.status.json`` and lives in the same
    directory as the CSV.

    Returns one of ``"approved"``, ``"rejected"``, or ``"pending"``.
    """
    sidecar = filepath.parent / f"{filepath.name}.status.json"
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data.get("status", "pending")
        except (json.JSONDecodeError, OSError):
            return "pending"
    return "pending"


def get_result_files() -> list[dict]:
    """Scan *RESULTS_DIR* for CSV files and return metadata dicts.

    Each dict contains:
    - name: filename (str)
    - path: full Path object
    - size_kb: file size in KB (float, rounded to 1 decimal)
    - modified_date: last-modified time as a formatted string
    - status: review status from sidecar file
    """
    results = []
    if not RESULTS_DIR.exists():
        return results

    for csv_file in sorted(RESULTS_DIR.glob("*.csv")):
        if not csv_file.is_file():
            continue
        stat = csv_file.stat()
        results.append(
            {
                "name": csv_file.name,
                "path": csv_file,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified_date": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "status": get_status(csv_file),
            }
        )
    return results


def parse_results_csv(filepath: Path) -> dict:
    """Parse a backtest CSV into a dict with *trades* and *metrics*.

    Returns
    -------
    dict
        ``trades``  -- list of row-dicts (each row is a dict of column->value).
        ``metrics`` -- dict of summary metrics.
        ``columns`` -- list of column names.
        ``error``   -- error message if parsing failed, else None.
    """
    result: dict = {
        "trades": [],
        "metrics": {},
        "columns": [],
        "error": None,
    }

    if not filepath.exists():
        result["error"] = f"File not found: {filepath.name}"
        return result

    try:
        df = pd.read_csv(filepath, encoding="utf-8")
    except pd.errors.EmptyDataError:
        result["error"] = "CSV file is empty."
        return result
    except Exception as exc:
        result["error"] = f"Failed to parse CSV: {exc}"
        return result

    if df.empty:
        result["error"] = "CSV contains no data rows."
        return result

    # Store column names and trade rows
    result["columns"] = list(df.columns)
    result["trades"] = df.fillna("").to_dict(orient="records")

    # ------------------------------------------------------------------
    # Compute summary metrics
    # ------------------------------------------------------------------
    metrics: dict = {}

    metrics["total_trades"] = len(df)

    # Look for a profit-related column (case-insensitive)
    profit_col = None
    for col in df.columns:
        if "profit" in col.lower() and "pct" not in col.lower() and "%" not in col.lower():
            profit_col = col
            break
    # Fallback: accept pct profit if no raw profit column
    if profit_col is None:
        for col in df.columns:
            if "profit" in col.lower():
                profit_col = col
                break

    if profit_col is not None:
        try:
            profits = pd.to_numeric(df[profit_col], errors="coerce").dropna()
            metrics["total_profit"] = round(float(profits.sum()), 2)
            metrics["avg_profit_per_trade"] = (
                round(float(profits.mean()), 2) if len(profits) > 0 else 0.0
            )
            metrics["winning_trades"] = int((profits > 0).sum())
            metrics["losing_trades"] = int((profits < 0).sum())
            metrics["breakeven_trades"] = int((profits == 0).sum())
            metrics["win_rate"] = (
                round(metrics["winning_trades"] / metrics["total_trades"] * 100, 1)
                if metrics["total_trades"] > 0
                else 0.0
            )
            # Max drawdown approximation: cumulative profit peak-to-trough
            cumulative = profits.cumsum()
            running_max = cumulative.cummax()
            drawdown = cumulative - running_max
            metrics["max_drawdown"] = round(float(drawdown.min()), 2)
            metrics["profit_column_used"] = profit_col
        except Exception:
            pass

    # Trade direction counts (Long / Short)
    trade_col = None
    for col in df.columns:
        if col.lower() == "trade":
            trade_col = col
            break
    if trade_col is not None:
        try:
            trade_vals = df[trade_col].astype(str).str.strip().str.lower()
            metrics["long_trades"] = int(trade_vals.str.contains("long").sum())
            metrics["short_trades"] = int(trade_vals.str.contains("short").sum())
        except Exception:
            pass

    result["metrics"] = metrics
    return result


# ---------------------------------------------------------------------------
# New Sprint 2 helpers
# ---------------------------------------------------------------------------


def get_afl_content() -> str:
    """Read the AFL strategy file and return its text content.

    Returns an empty string if the file does not exist.
    """
    if AFL_STRATEGY_FILE.exists():
        try:
            return AFL_STRATEGY_FILE.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to read AFL file %s: %s", AFL_STRATEGY_FILE, exc)
            return ""
    return ""


def save_afl_content(content: str) -> tuple:
    """Write *content* to the AFL strategy file and rebuild the .apx file.

    Returns
    -------
    tuple of (bool, str)
        ``(True, "success message")`` on success, or
        ``(False, "error message")`` on failure.
    """
    try:
        AFL_STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
        AFL_STRATEGY_FILE.write_text(content, encoding="utf-8")
        logger.info("AFL file saved: %s (%d chars)", AFL_STRATEGY_FILE, len(content))
    except Exception as exc:
        return (False, f"Failed to write AFL file: {exc}")

    # Rebuild the APX file to embed the updated AFL content
    try:
        from scripts.apx_builder import build_apx

        build_apx(
            afl_path=str(AFL_STRATEGY_FILE),
            output_apx_path=str(APX_OUTPUT),
            template_apx_path=str(APX_TEMPLATE),
        )
        return (True, f"APX rebuilt at {APX_OUTPUT.name}")
    except Exception as exc:
        return (False, f"AFL saved but APX rebuild failed: {exc}")


def compute_equity_curve(filepath: Path) -> dict:
    """Compute a cumulative equity curve from a backtest CSV.

    Reads the CSV at *filepath*, finds the Profit column, and computes
    a running equity starting from $100,000 (or from BACKTEST_SETTINGS).

    Returns a dict with labels, equity, dates, profits, colors, and error.
    """
    starting_capital = BACKTEST_SETTINGS.get("starting_capital", 100_000)

    result = {
        "labels": [],
        "equity": [],
        "dates": [],
        "profits": [],
        "colors": [],
        "error": None,
    }

    if not filepath.exists():
        result["error"] = f"File not found: {filepath.name}"
        return result

    try:
        df = pd.read_csv(filepath, encoding="utf-8")
    except Exception as exc:
        result["error"] = f"Failed to read CSV: {exc}"
        return result

    if df.empty:
        result["error"] = "CSV contains no data rows."
        return result

    # Find the profit column
    profit_col = None
    for col in df.columns:
        if "profit" in col.lower() and "pct" not in col.lower() and "%" not in col.lower():
            profit_col = col
            break
    if profit_col is None:
        for col in df.columns:
            if "profit" in col.lower():
                profit_col = col
                break

    if profit_col is None:
        result["error"] = "No profit column found in CSV."
        return result

    profits = pd.to_numeric(df[profit_col], errors="coerce").fillna(0.0)

    # Find a date column if available
    date_col = None
    for col in df.columns:
        if col.lower() in ("date", "entry date", "exit date", "entrydate", "exitdate"):
            date_col = col
            break
    if date_col is None:
        for col in df.columns:
            if "date" in col.lower():
                date_col = col
                break

    dates = []
    if date_col is not None:
        dates = df[date_col].astype(str).fillna("").tolist()
    else:
        dates = [""] * len(profits)

    # Build the equity curve
    labels = ["Start"]
    equity = [starting_capital]
    profit_list = [0]
    colors = ["rgba(0,0,0,0)"]  # transparent for the starting point
    date_list = [""]

    current_equity = starting_capital
    for i, p in enumerate(profits):
        p_val = float(p)
        current_equity += p_val
        labels.append(f"Trade {i + 1}")
        equity.append(round(current_equity, 2))
        profit_list.append(round(p_val, 2))
        date_list.append(dates[i] if i < len(dates) else "")

        if p_val > 0:
            colors.append("rgba(25,135,84,0.8)")   # green for wins
        elif p_val < 0:
            colors.append("rgba(220,53,69,0.8)")    # red for losses
        else:
            colors.append("rgba(108,117,125,0.8)")  # gray for breakeven

    result["labels"] = labels
    result["equity"] = equity
    result["dates"] = date_list
    result["profits"] = profit_list
    result["colors"] = colors

    return result


def _run_backtest_background():
    """Run the backtest in a background thread via run.py."""
    global _backtest_state
    with _backtest_lock:
        _backtest_state["running"] = True
        _backtest_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _backtest_state["finished_at"] = None
        _backtest_state["success"] = None
        _backtest_state["error"] = None

    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "run.py")],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=600,
        )
        with _backtest_lock:
            _backtest_state["success"] = result.returncode == 0
            if result.returncode != 0:
                _backtest_state["error"] = result.stderr[-500:] if result.stderr else "Unknown error"
    except subprocess.TimeoutExpired:
        with _backtest_lock:
            _backtest_state["success"] = False
            _backtest_state["error"] = "Backtest timed out after 10 minutes"
    except Exception as e:
        with _backtest_lock:
            _backtest_state["success"] = False
            _backtest_state["error"] = str(e)
    finally:
        with _backtest_lock:
            _backtest_state["running"] = False
            _backtest_state["finished_at"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Dashboard home -- list all result CSV files."""
    result_files = get_result_files()
    return render_template(
        "index.html",
        result_files=result_files,
        get_strategy_info=get_strategy_info,
        db_configured=bool(AMIBROKER_DB_PATH),
        backtest_state=_backtest_state,
    )


@app.route("/results/<filename>")
def results_detail(filename: str):
    """Detail view of a single CSV result set."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists() or not filepath.suffix == ".csv":
        flash(f"Result file '{filename}' not found.", "danger")
        return redirect(url_for("index"))

    parsed = parse_results_csv(filepath)
    status = get_status(filepath)

    # Check if a companion HTML file exists
    html_companion = filepath.with_suffix(".html")
    has_html = html_companion.exists()

    return render_template(
        "results_detail.html",
        filename=filename,
        parsed=parsed,
        status=status,
        has_html=has_html,
        strategy=get_strategy_info(filename),
    )


@app.route("/results/<filename>/stage", methods=["POST"])
def stage_result(filename: str):
    """Copy the result CSV (and HTML if present) to results/staged/."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists():
        flash(f"Result file '{filename}' not found.", "danger")
        return redirect(url_for("index"))

    try:
        STAGED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(filepath, STAGED_DIR / filename)

        # Also copy the HTML companion if it exists
        html_companion = filepath.with_suffix(".html")
        if html_companion.exists():
            shutil.copy2(html_companion, STAGED_DIR / html_companion.name)

        flash(f"'{filename}' has been staged successfully.", "success")
    except Exception as exc:
        flash(f"Failed to stage '{filename}': {exc}", "danger")

    return redirect(url_for("results_detail", filename=filename))


@app.route("/results/<filename>/approve", methods=["POST"])
def approve_result(filename: str):
    """Mark a result set as approved via a JSON sidecar file."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists():
        flash(f"Result file '{filename}' not found.", "danger")
        return redirect(url_for("index"))

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

    return redirect(url_for("results_detail", filename=filename))


@app.route("/results/<filename>/reject", methods=["POST"])
def reject_result(filename: str):
    """Mark a result set as rejected via a JSON sidecar file."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists():
        flash(f"Result file '{filename}' not found.", "danger")
        return redirect(url_for("index"))

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

    return redirect(url_for("results_detail", filename=filename))


@app.route("/logs")
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


@app.route("/api/results/<filename>")
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
            "equity_curve_url": url_for("api_equity_curve", filename=filename),
        }
    )


@app.route("/download/<filename>")
def download_file(filename: str):
    """Serve a file from the results directory for download."""
    return send_from_directory(str(RESULTS_DIR), filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Sprint 2 routes
# ---------------------------------------------------------------------------


@app.route("/afl")
def afl_editor():
    """AFL editor page -- view and edit the AFL strategy file."""
    content = get_afl_content()
    return render_template("afl_editor.html", afl_content=content, afl_path=str(AFL_STRATEGY_FILE))


@app.route("/afl/save", methods=["POST"])
def afl_save():
    """Save AFL content from the editor form."""
    content = request.form.get("afl_content", "")
    if not content.strip():
        flash("AFL content cannot be empty.", "danger")
        return redirect(url_for("afl_editor"))
    success, message = save_afl_content(content)
    if success:
        flash(f"AFL saved and APX rebuilt. {message}", "success")
    else:
        flash(f"Error saving AFL: {message}", "danger")
    return redirect(url_for("afl_editor"))


@app.route("/api/results/<filename>/equity-curve")
def api_equity_curve(filename: str):
    """JSON API endpoint returning equity curve data for a result CSV."""
    filepath = RESULTS_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404
    data = compute_equity_curve(filepath)
    return jsonify(data)


@app.route("/backtest/run", methods=["POST"])
def backtest_run():
    """Start a backtest in a background thread."""
    with _backtest_lock:
        if _backtest_state["running"]:
            flash("A backtest is already running. Please wait.", "warning")
            return redirect(url_for("logs"))

    if not AMIBROKER_DB_PATH:
        flash("AMIBROKER_DB_PATH not configured in settings.", "danger")
        return redirect(url_for("index"))

    thread = threading.Thread(target=_run_backtest_background, daemon=True)
    thread.start()
    flash("Backtest started. Monitoring progress...", "info")
    return redirect(url_for("backtest_status_page"))


@app.route("/backtest/status")
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
    return render_template("backtest_status.html", state=state, log_content=log_content)


@app.route("/api/backtest/status")
def api_backtest_status():
    """JSON API endpoint returning current backtest state."""
    with _backtest_lock:
        state = dict(_backtest_state)
    return jsonify(state)
