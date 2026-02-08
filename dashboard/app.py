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
    BACKTEST_SETTINGS, LOG_FILE, AFL_DIR, CHART_SETTINGS
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
VERSIONS_DIR: Path = AFL_DIR / "versions"

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


@app.context_processor
def inject_backtest_state():
    """Make backtest_running available to all templates (for navbar spinner)."""
    with _backtest_lock:
        return {"backtest_running": _backtest_state["running"]}


# ---------------------------------------------------------------------------
# Strategy database (replaces hardcoded STRATEGY_DESCRIPTIONS)
# ---------------------------------------------------------------------------

from scripts.strategy_db import (
    init_db,
    seed_default_strategies,
    get_strategy_info,
    get_strategy_summary,
    get_run_with_context,
    create_strategy as db_create_strategy,
    update_strategy as db_update_strategy,
    get_strategy as db_get_strategy,
    list_strategies as db_list_strategies,
    delete_strategy as db_delete_strategy,
    create_version as db_create_version,
    get_version as db_get_version,
    list_versions as db_list_versions,
    get_latest_version as db_get_latest_version,
    create_run as db_create_run,
    update_run as db_update_run,
    get_run as db_get_run,
    list_runs as db_list_runs,
)

init_db()
seed_default_strategies()


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


def validate_afl_content(content: str) -> list[str]:
    """Run AFL pre-flight checks and return a list of warning strings.

    Returns an empty list when the AFL passes all checks.
    """
    from scripts.afl_validator import validate_afl
    ok, errors = validate_afl(content)
    return errors


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
    """Compute equity curve data supporting both trade-based and time-based views.

    Returns dict with:
    - trade_view: {labels, equity, dates, profits, colors} - one point per trade
    - time_view: {dates, equity, trade_counts, trade_dates, trade_equities, trade_colors, trade_profits} - daily timeline
    - summary: {total_days, active_days, trades_per_month, avg_holding_period}
    - error: None or error string
    """
    starting_capital = BACKTEST_SETTINGS.get("starting_capital", 100_000)

    result = {
        "trade_view": {"labels": [], "equity": [], "dates": [], "profits": [], "colors": []},
        "time_view": {"dates": [], "equity": [], "trade_counts": [], "trade_dates": [], "trade_equities": [], "trade_colors": [], "trade_profits": []},
        "summary": {},
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

    # Find profit column
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

    # Find date column
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

    # Find BarsInTrade column if available
    bars_col = None
    for col in df.columns:
        if col.lower() in ("barsintrade", "bars_in_trade", "bars", "duration"):
            bars_col = col
            break

    # --- TRADE VIEW (one point per trade) ---
    trade_labels = ["Start"]
    trade_equity = [starting_capital]
    trade_profits = [0]
    trade_colors = ["rgba(0,0,0,0)"]
    trade_dates = [""]

    dates_raw = []
    if date_col is not None:
        dates_raw = df[date_col].astype(str).fillna("").tolist()
    else:
        dates_raw = [""] * len(profits)

    current_equity = starting_capital
    for i, p in enumerate(profits):
        p_val = float(p)
        current_equity += p_val
        trade_labels.append(f"Trade {i + 1}")
        trade_equity.append(round(current_equity, 2))
        trade_profits.append(round(p_val, 2))
        trade_dates.append(dates_raw[i] if i < len(dates_raw) else "")
        if p_val > 0:
            trade_colors.append("rgba(25,135,84,0.8)")
        elif p_val < 0:
            trade_colors.append("rgba(220,53,69,0.8)")
        else:
            trade_colors.append("rgba(108,117,125,0.8)")

    result["trade_view"] = {
        "labels": trade_labels,
        "equity": trade_equity,
        "dates": trade_dates,
        "profits": trade_profits,
        "colors": trade_colors,
    }

    # --- TIME VIEW (daily timeline) ---
    if date_col is not None:
        try:
            trade_dates_parsed = pd.to_datetime(df[date_col], errors="coerce").dropna()
            if len(trade_dates_parsed) > 0:
                min_date = trade_dates_parsed.min()
                max_date = trade_dates_parsed.max()

                # Create daily date range
                all_dates = pd.date_range(start=min_date, end=max_date, freq="B")  # business days

                # Build a map of date -> list of profits
                date_profit_map = {}
                for i, row_date in enumerate(trade_dates_parsed):
                    d = row_date.normalize()
                    if d not in date_profit_map:
                        date_profit_map[d] = []
                    date_profit_map[d].append(float(profits.iloc[i]))

                time_dates = []
                time_equity = []
                time_trade_counts = []
                eq = starting_capital

                for d in all_dates:
                    d_norm = d.normalize()
                    time_dates.append(d.strftime("%Y-%m-%d"))
                    if d_norm in date_profit_map:
                        day_profit = sum(date_profit_map[d_norm])
                        eq += day_profit
                        time_trade_counts.append(len(date_profit_map[d_norm]))
                    else:
                        time_trade_counts.append(0)
                    time_equity.append(round(eq, 2))

                # Also provide the individual trade points for overlay markers
                time_trade_dates = []
                time_trade_equities = []
                time_trade_colors = []
                time_trade_profits_list = []
                eq2 = starting_capital
                for i, row_date in enumerate(trade_dates_parsed):
                    p_val = float(profits.iloc[i])
                    eq2 += p_val
                    time_trade_dates.append(row_date.strftime("%Y-%m-%d"))
                    time_trade_equities.append(round(eq2, 2))
                    time_trade_profits_list.append(round(p_val, 2))
                    if p_val > 0:
                        time_trade_colors.append("rgba(25,135,84,0.9)")
                    elif p_val < 0:
                        time_trade_colors.append("rgba(220,53,69,0.9)")
                    else:
                        time_trade_colors.append("rgba(108,117,125,0.9)")

                result["time_view"] = {
                    "dates": time_dates,
                    "equity": time_equity,
                    "trade_counts": time_trade_counts,
                    "trade_dates": time_trade_dates,
                    "trade_equities": time_trade_equities,
                    "trade_colors": time_trade_colors,
                    "trade_profits": time_trade_profits_list,
                }

                # --- SUMMARY STATS ---
                total_days = (max_date - min_date).days
                active_days = len(date_profit_map)
                total_months = max(total_days / 30.44, 1)
                avg_holding = 0
                if bars_col is not None:
                    bars = pd.to_numeric(df[bars_col], errors="coerce").dropna()
                    if len(bars) > 0:
                        avg_holding = round(float(bars.mean()), 1)

                result["summary"] = {
                    "total_days": total_days,
                    "active_trading_days": active_days,
                    "trades_per_month": round(len(df) / total_months, 1),
                    "avg_holding_period_bars": avg_holding,
                    "date_range": f"{min_date.strftime('%Y-%m-%d')} to {max_date.strftime('%Y-%m-%d')}",
                    "total_months": round(total_months, 1),
                }
        except Exception as exc:
            logger.warning("Failed to build time view: %s", exc)
            # time_view stays as default empty

    return result


def get_afl_versions() -> list:
    """Return list of saved AFL versions, newest first.

    Each version is a dict: {name, timestamp, filepath, size_kb, label}
    """
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    versions = []
    for f in sorted(VERSIONS_DIR.glob("*.afl"), reverse=True):
        stat = f.stat()
        # Parse version info from filename: v001_20260207_143022_label.afl
        parts = f.stem.split("_", 3)
        label = parts[3] if len(parts) > 3 else ""
        versions.append({
            "name": f.name,
            "timestamp": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "filepath": str(f),
            "size_kb": round(stat.st_size / 1024, 1),
            "label": label.replace("_", " "),
        })
    return versions


def save_afl_version(content: str, label: str = "") -> tuple:
    """Save a versioned snapshot of AFL content.

    Creates a timestamped copy in afl/versions/.
    Returns (True, version_filename) or (False, error_message).
    """
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Count existing versions to generate sequential number
    existing = sorted(VERSIONS_DIR.glob("*.afl"))
    next_num = len(existing) + 1

    now = datetime.now()
    safe_label = label.strip().replace(" ", "_")[:30] if label else ""
    if safe_label:
        version_name = f"v{next_num:03d}_{now.strftime('%Y%m%d_%H%M%S')}_{safe_label}.afl"
    else:
        version_name = f"v{next_num:03d}_{now.strftime('%Y%m%d_%H%M%S')}.afl"

    version_path = VERSIONS_DIR / version_name
    try:
        version_path.write_text(content, encoding="utf-8")
        return (True, version_name)
    except Exception as exc:
        return (False, str(exc))


def load_afl_version(version_name: str) -> tuple:
    """Load a specific AFL version content.

    Returns (True, content) or (False, error_message).
    """
    version_path = VERSIONS_DIR / version_name
    if not version_path.exists():
        return (False, f"Version not found: {version_name}")
    try:
        content = version_path.read_text(encoding="utf-8")
        return (True, content)
    except Exception as exc:
        return (False, str(exc))


def _run_backtest_background(strategy_id: str = None, version_id: str = None):
    """Run the backtest in a background thread via run.py.

    Passes strategy_id and version_id to the pipeline so the correct
    strategy/version is used and a GUID-based run record is created.
    """
    global _backtest_state
    with _backtest_lock:
        _backtest_state["running"] = True
        _backtest_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _backtest_state["finished_at"] = None
        _backtest_state["success"] = None
        _backtest_state["error"] = None

    try:
        cmd = [sys.executable, str(PROJECT_ROOT / "run.py")]
        if strategy_id:
            cmd.append(strategy_id)
        if version_id:
            cmd.append(version_id)

        result = subprocess.run(
            cmd,
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
    """Dashboard home -- list all strategies with version/run counts."""
    strategies = db_list_strategies()
    strategy_summaries = []
    for s in strategies:
        summary = get_strategy_summary(s["id"])
        if summary:
            strategy_summaries.append(summary)

    # Also keep flat file listing for any orphan CSVs not in the DB
    result_files = get_result_files()

    return render_template(
        "index.html",
        strategies=strategy_summaries,
        result_files=result_files,
        get_strategy_info=get_strategy_info,
        db_configured=bool(AMIBROKER_DB_PATH),
        backtest_state=_backtest_state,
    )


@app.route("/results/<filename>")
def results_detail(filename: str):
    """Detail view of a single CSV result set (legacy flat file route)."""
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
        afl_content=get_afl_content(),
        afl_path=str(AFL_STRATEGY_FILE),
        versions=get_afl_versions(),
        db_configured=bool(AMIBROKER_DB_PATH),
        run=None,
    )


@app.route("/run/<run_id>")
def run_detail(run_id: str):
    """Detail view of a GUID-based backtest run."""
    run = get_run_with_context(run_id)
    if run is None:
        flash(f"Run '{run_id}' not found.", "danger")
        return redirect(url_for("index"))

    # Resolve the CSV path: either in results/<run_id>/ or flat results/
    results_dir_path = PROJECT_ROOT / run["results_dir"] if run["results_dir"] else RESULTS_DIR
    csv_filename = run.get("results_csv", "results.csv")
    filepath = results_dir_path / csv_filename

    # Fallback: legacy runs may have results_csv="results.csv" with no results_dir
    if not filepath.exists() and not run["results_dir"]:
        filepath = RESULTS_DIR / csv_filename

    if not filepath.exists():
        parsed = {"trades": [], "metrics": {}, "columns": [], "error": f"Result file not found: {filepath}"}
        status = "pending"
        has_html = False
    else:
        parsed = parse_results_csv(filepath)
        status = get_status(filepath)
        html_companion = filepath.with_suffix(".html")
        has_html = html_companion.exists()

    strategy = run.get("strategy") or get_strategy_info(run["strategy_id"])
    version = run.get("version")

    return render_template(
        "results_detail.html",
        filename=csv_filename,
        parsed=parsed,
        status=status,
        has_html=has_html,
        strategy=strategy,
        afl_content=version.get("afl_content", "") if version else get_afl_content(),
        afl_path=str(AFL_STRATEGY_FILE),
        versions=get_afl_versions(),
        db_configured=bool(AMIBROKER_DB_PATH),
        run=run,
    )


@app.route("/strategy/<strategy_id>")
def strategy_detail(strategy_id: str):
    """Strategy detail page showing versions and runs."""
    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        flash(f"Strategy '{strategy_id}' not found.", "danger")
        return redirect(url_for("index"))

    versions = db_list_versions(strategy_id)
    runs = db_list_runs(strategy_id=strategy_id)

    # Attach version info to each run for display
    version_map = {v["id"]: v for v in versions}
    for run in runs:
        run["version"] = version_map.get(run["version_id"])

    return render_template(
        "strategy_detail.html",
        strategy=strategy,
        versions=versions,
        runs=runs,
        db_configured=bool(AMIBROKER_DB_PATH),
    )


@app.route("/strategy/create", methods=["POST"])
def strategy_create():
    """Create a new strategy."""
    name = request.form.get("name", "").strip()
    summary = request.form.get("summary", "").strip()
    symbol = request.form.get("symbol", "").strip()

    if not name:
        flash("Strategy name is required.", "danger")
        return redirect(url_for("index"))

    strategy_id = db_create_strategy(name=name, summary=summary, symbol=symbol)
    flash(f"Strategy '{name}' created.", "success")
    return redirect(url_for("strategy_detail", strategy_id=strategy_id))


@app.route("/strategy/<strategy_id>/version/create", methods=["POST"])
def version_create(strategy_id: str):
    """Create a new version for a strategy with AFL content."""
    afl_content = request.form.get("afl_content", "")
    label = request.form.get("label", "").strip()

    if not afl_content.strip():
        flash("AFL content cannot be empty.", "danger")
        return redirect(url_for("strategy_detail", strategy_id=strategy_id))

    # Pre-flight validation
    afl_warnings = validate_afl_content(afl_content)
    for warning in afl_warnings:
        flash(f"AFL warning: {warning}", "warning")

    version_id = db_create_version(
        strategy_id=strategy_id,
        afl_content=afl_content,
        label=label,
    )
    flash(f"Version created (v{label or 'new'}).", "success")
    return redirect(url_for("strategy_detail", strategy_id=strategy_id))


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


@app.route("/run/<run_id>/download/<filename>")
def download_run_file(run_id: str, filename: str):
    """Serve a file from a GUID-based run results directory."""
    run = db_get_run(run_id)
    if run is None:
        return "Run not found", 404
    run_dir = PROJECT_ROOT / run["results_dir"]
    if not run_dir.exists():
        return "Results directory not found", 404
    return send_from_directory(str(run_dir), filename, as_attachment=True)


# ---------------------------------------------------------------------------
# API routes — strategies, versions, runs
# ---------------------------------------------------------------------------


@app.route("/api/strategies")
def api_strategies():
    """JSON API endpoint returning all strategies with summary info."""
    strategies = db_list_strategies()
    summaries = []
    for s in strategies:
        summary = get_strategy_summary(s["id"])
        if summary:
            summaries.append(summary)
    return jsonify(summaries)


@app.route("/api/strategy/<strategy_id>/versions")
def api_versions(strategy_id: str):
    """JSON API endpoint returning all versions for a strategy."""
    versions = db_list_versions(strategy_id)
    return jsonify(versions)


@app.route("/api/strategy/<strategy_id>/runs")
def api_runs(strategy_id: str):
    """JSON API endpoint returning all runs for a strategy."""
    runs = db_list_runs(strategy_id=strategy_id)
    return jsonify(runs)


@app.route("/api/run/<run_id>")
def api_run_detail(run_id: str):
    """JSON API endpoint returning full run details with context."""
    run = get_run_with_context(run_id)
    if run is None:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(run)


# ---------------------------------------------------------------------------
# Sprint 2 routes
# ---------------------------------------------------------------------------


@app.route("/afl")
def afl_editor():
    """AFL editor page -- view and edit the AFL strategy file."""
    content = get_afl_content()
    return render_template(
        "afl_editor.html",
        afl_content=content,
        afl_path=str(AFL_STRATEGY_FILE),
        versions=get_afl_versions(),
    )


@app.route("/afl/save", methods=["POST"])
def afl_save():
    """Save AFL content and optionally create a version."""
    content = request.form.get("afl_content", "")
    version_label = request.form.get("version_label", "").strip()
    create_version = request.form.get("create_version") == "on"
    redirect_to = request.form.get("redirect_to", "")

    if not content.strip():
        flash("AFL content cannot be empty.", "danger")
        return redirect(url_for("afl_editor"))

    # Pre-flight validation -- warn but don't block save
    afl_warnings = validate_afl_content(content)
    for warning in afl_warnings:
        flash(f"AFL warning: {warning}", "warning")

    # Always save the AFL and rebuild APX
    success, message = save_afl_content(content)
    if success:
        flash(f"AFL saved and APX rebuilt.", "success")
    else:
        flash(f"Error saving AFL: {message}", "danger")
        return redirect(url_for("afl_editor"))

    # Optionally create a version snapshot
    if create_version or version_label:
        v_ok, v_msg = save_afl_version(content, version_label or "")
        if v_ok:
            flash(f"Version saved: {v_msg}", "info")
        else:
            flash(f"Version save failed: {v_msg}", "warning")

    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("afl_editor"))


@app.route("/afl/versions/<version_name>/load", methods=["POST"])
def afl_load_version(version_name: str):
    """Load a specific AFL version into the editor."""
    ok, content_or_error = load_afl_version(version_name)
    if ok:
        # Write it as the active AFL
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
    return redirect(url_for("afl_editor"))


@app.route("/api/afl/versions/<version_name>")
def api_afl_version(version_name: str):
    """Return the content of a specific AFL version."""
    ok, content_or_error = load_afl_version(version_name)
    if ok:
        return jsonify({"content": content_or_error, "name": version_name})
    return jsonify({"error": content_or_error}), 404


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

    strategy_id = request.form.get("strategy_id", "").strip() or None
    version_id = request.form.get("version_id", "").strip() or None

    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, version_id),
        daemon=True,
    )
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


# ---------------------------------------------------------------------------
# Sprint 3 routes -- Trade candlestick charts
# ---------------------------------------------------------------------------


@app.route("/api/ohlcv/<symbol>")
def api_ohlcv(symbol: str):
    """Return 1-minute OHLCV candlestick data for *symbol* around a trade.

    Query parameters:
        entry_date  -- trade entry datetime (CSV format: ``7/21/2025 1:14:50 AM``)
        exit_date   -- trade exit datetime (same format)
    """
    from scripts.ole_stock_data import get_ohlcv_cached

    entry_date_str = request.args.get("entry_date", "").strip()
    exit_date_str = request.args.get("exit_date", "").strip()

    if not entry_date_str or not exit_date_str:
        return jsonify({"data": [], "error": "entry_date and exit_date are required."}), 400

    # Parse the CSV date format (e.g. "7/21/2025 1:14:50 AM")
    entry_dt = _parse_trade_date(entry_date_str)
    exit_dt = _parse_trade_date(exit_date_str)

    if entry_dt is None or exit_dt is None:
        return jsonify({"data": [], "error": f"Invalid date format. Got entry='{entry_date_str}', exit='{exit_date_str}'."}), 400

    result = get_ohlcv_cached(
        symbol=symbol,
        start_dt=entry_dt,
        end_dt=exit_dt,
        padding_before=CHART_SETTINGS["bars_before_entry"],
        padding_after=CHART_SETTINGS["bars_after_exit"],
    )

    if result["error"] and "not running" in result["error"].lower():
        return jsonify(result), 503
    if result["error"] and "not found" in result["error"].lower():
        return jsonify(result), 404
    if result["error"]:
        return jsonify(result), 503

    return jsonify(result)


def _parse_trade_date(date_str: str) -> datetime | None:
    """Try several date formats common in AmiBroker CSV exports."""
    formats = [
        "%m/%d/%Y %I:%M:%S %p",   # 7/21/2025 1:14:50 AM
        "%m/%d/%Y %H:%M:%S",       # 7/21/2025 13:14:50
        "%Y-%m-%d %H:%M:%S",       # 2025-07-21 13:14:50
        "%Y-%m-%dT%H:%M:%S",       # ISO format
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None
