"""
Flask application for the AmiTesting Results Dashboard.

Provides a web interface to browse, review, stage, and approve/reject
backtest result CSV files produced by AmiBroker OLE automation.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
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
    BACKTEST_SETTINGS, LOG_FILE, AFL_DIR, CHART_SETTINGS, INDICATORS_DIR,
    GCZ25_SYMBOL,
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
    "run_id": None,
}
_backtest_lock = threading.Lock()

_batch_state = {
    "running": False,
    "batch_id": None,
    "runner": None,  # reference to BatchRunner for cancel support
}
_batch_lock = threading.Lock()


@app.context_processor
def inject_backtest_state():
    """Make backtest_running and default_symbol available to all templates."""
    with _backtest_lock:
        return {
            "backtest_running": _backtest_state["running"],
            "default_symbol": GCZ25_SYMBOL,
        }


# ---------------------------------------------------------------------------
# Strategy database (replaces hardcoded STRATEGY_DESCRIPTIONS)
# ---------------------------------------------------------------------------

from scripts.strategy_db import (
    init_db,
    seed_default_strategies,
    seed_param_tooltips,
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
    create_batch as db_create_batch,
    update_batch as db_update_batch,
    get_batch as db_get_batch,
    list_batches as db_list_batches,
    get_all_param_tooltips_dict as db_get_all_param_tooltips_dict,
    get_param_tooltip as db_get_param_tooltip,
    upsert_param_tooltip as db_upsert_param_tooltip,
    delete_param_tooltip as db_delete_param_tooltip,
    seed_indicator_tooltips,
    get_all_indicator_tooltips_dict as db_get_all_indicator_tooltips_dict,
    get_indicator_tooltip as db_get_indicator_tooltip,
    upsert_indicator_tooltip as db_upsert_indicator_tooltip,
    delete_indicator_tooltip as db_delete_indicator_tooltip,
    reconstruct_optimization_parsed,
    find_strategy_by_name as db_find_strategy_by_name,
)
from scripts.afl_reverser import reverse_afl

init_db()
seed_default_strategies()
seed_param_tooltips()
seed_indicator_tooltips()


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


def _is_optimization_csv(df) -> bool:
    """Heuristic: does this CSV look like AmiBroker optimization output?

    Optimization CSVs have metric summary columns ('Net Profit', '# Trades')
    but do NOT have trade-specific columns ('Ex. date', 'Ex. Price').
    """
    cols_lower = {c.lower().strip() for c in df.columns}
    has_metric_cols = (
        any("net profit" in c for c in cols_lower)
        or "# trades" in cols_lower
        or "all trades" in cols_lower
    )
    has_trade_cols = (
        "ex. date" in cols_lower
        or "ex. price" in cols_lower
        or "exit date" in cols_lower
    )
    return has_metric_cols and not has_trade_cols


def parse_results_csv(filepath: Path, force_optimization: bool = False) -> dict:
    """Parse a backtest or optimization CSV into a dict.

    Returns
    -------
    dict
        ``trades``  -- list of row-dicts (trade rows or optimization combos).
        ``metrics`` -- dict of summary metrics.
        ``columns`` -- list of column names.
        ``error``   -- error message if parsing failed, else None.
        ``is_optimization`` -- True if parsed as optimization results.
    """
    result: dict = {
        "trades": [],
        "metrics": {},
        "columns": [],
        "error": None,
        "is_optimization": False,
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

    # Detect if this is optimization output
    is_opt = force_optimization or _is_optimization_csv(df)

    if is_opt:
        return _parse_optimization_results(df)

    # ------------------------------------------------------------------
    # Standard backtest (trade-by-trade) parsing
    # ------------------------------------------------------------------
    result["columns"] = list(df.columns)
    result["trades"] = df.fillna("").to_dict(orient="records")

    metrics: dict = {}
    metrics["total_trades"] = len(df)

    # Look for a profit-related column (case-insensitive)
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


def _parse_optimization_results(df) -> dict:
    """Parse an AmiBroker optimization CSV (one row per parameter combo)."""
    result: dict = {
        "trades": [],
        "metrics": {},
        "columns": list(df.columns),
        "error": None,
        "is_optimization": True,
    }

    # Identify metric columns vs parameter columns
    # AmiBroker optimization columns typically include:
    # Net Profit, Net Profit %, # Trades, Avg. Profit/Loss, Avg. Bars Held,
    # Max. system drawdown, Max. system % drawdown, etc.
    # Everything else is a parameter column.
    metric_keywords = {
        "net profit", "profit", "# trades", "all trades", "avg. profit",
        "avg. bars", "drawdown", "max. trade", "winners", "losers",
        "profit factor", "sharpe", "ulcer", "recovery", "payoff",
        "cagr", "rar", "exposure", "risk", "% profitable",
    }

    metric_cols = []
    param_cols = []
    for col in df.columns:
        cl = col.lower().strip()
        if cl == "symbol":
            continue  # skip symbol column
        is_metric = any(kw in cl for kw in metric_keywords)
        if is_metric:
            metric_cols.append(col)
        else:
            param_cols.append(col)

    result["metrics"] = {
        "combos_tested": len(df),
        "param_columns": param_cols,
        "metric_columns": metric_cols,
    }

    # Find net profit column for sorting / best combo detection
    net_profit_col = None
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ("net profit", "net profit %", "profit") and "%" not in cl:
            net_profit_col = col
            break
    if net_profit_col is None:
        for col in df.columns:
            if "net profit" in col.lower().strip():
                net_profit_col = col
                break

    if net_profit_col:
        vals = pd.to_numeric(df[net_profit_col], errors="coerce")
        # Sort by net profit descending for display
        df = df.sort_values(net_profit_col, ascending=False, key=lambda x: pd.to_numeric(x, errors="coerce"))
        clean_vals = vals.dropna()
        if len(clean_vals) > 0:
            result["metrics"]["best_net_profit"] = round(float(clean_vals.max()), 2)
            result["metrics"]["worst_net_profit"] = round(float(clean_vals.min()), 2)
            result["metrics"]["avg_net_profit"] = round(float(clean_vals.mean()), 2)
            result["metrics"]["profitable_combos"] = int((clean_vals > 0).sum())
            result["metrics"]["net_profit_column"] = net_profit_col

    # Find trades column for summary
    trades_col = None
    for col in df.columns:
        if col.strip().lower() in ("# trades", "trades", "all trades"):
            trades_col = col
            break
    if trades_col:
        tvals = pd.to_numeric(df[trades_col], errors="coerce").dropna()
        if len(tvals) > 0:
            result["metrics"]["avg_trades"] = round(float(tvals.mean()), 1)

    # Store rows (already sorted by best profit)
    result["trades"] = df.fillna("").to_dict(orient="records")

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


def _run_backtest_background(strategy_id: str = None, version_id: str = None, run_mode: int = None, symbol: str = None):
    """Run the backtest in a background thread via run.py.

    Uses ``Popen`` instead of ``subprocess.run`` so that the run_id can be
    discovered from the sentinel file while the process is still running.
    This enables real-time optimization progress polling from the dashboard.
    """
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
    # Clear any stale sentinel
    if sentinel_path.exists():
        try:
            sentinel_path.unlink()
        except OSError:
            pass

    proc = None
    # Redirect subprocess stdout/stderr to a file instead of PIPE.
    # Using PIPE without draining causes a deadlock when the OS pipe
    # buffer fills up (~4-8 KB on Windows), blocking the subprocess.
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

        proc_log_file = open(proc_log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=proc_log_file,
            stderr=proc_log_file,
            text=True,
            cwd=str(PROJECT_ROOT),
        )

        # Poll for sentinel file to discover run_id early
        deadline = time.time() + 600  # 10 minute overall timeout
        while proc.poll() is None:
            # Try to pick up run_id from sentinel
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
                    # Mark the DB run as failed so the UI doesn't get stuck
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

        # Process has finished
        returncode = proc.returncode

        # Read subprocess output from log file for error reporting
        proc_log_file.close()
        proc_log_file = None
        try:
            proc_output = proc_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            proc_output = ""

        with _backtest_lock:
            _backtest_state["success"] = returncode == 0
            if returncode == 0:
                # Try sentinel first, then fall back to log parsing
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

        # Safety net: if the subprocess failed/crashed but run.py didn't get a
        # chance to update the DB, the run record would be stuck as "running"
        # forever.  Mark it as failed so the UI correctly shows the outcome.
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


def extract_indicators(afl_content: str) -> list[str]:
    """Detect which technical indicators an AFL strategy uses.

    Scans the AFL source for include directives and built-in function calls
    and returns a sorted list of human-readable indicator names.
    """
    if not afl_content:
        return []
    indicators = set()
    checks = [
        ("TEMA",                 ["tema.afl", "temas"]),
        ("ADX",                  ["ADXvalue", "adxPer", "plusDI", "minusDI"]),
        ("VWAP Clouds",          ["vwap_clouds.afl", "VWAP"]),
        ("StdDev Exit",          ["stdev_exit.afl", "exitStdDev", "exitDistance"]),
        ("Consolidation Zones",  ["consolidation_zones.afl", "czBreakout", "isConsolidating"]),
        ("Derivative Lookback",  ["derivative_lookback.afl", "firstDeriv", "secondDeriv"]),
        ("Range Bound",          ["range_bound.afl", "isRangeBound"]),
        ("RSI",                  ["RSI("]),
        ("Bollinger Bands",      ["BBandTop", "BBandBot"]),
        ("EMA",                  ["EMA("]),
        ("MACD",                 ["MACD("]),
        ("Stochastic",           ["StochK", "StochD"]),
        ("Donchian Channel",     ["donchianHigh", "donchianLow"]),
        ("SMA",                  ["MA(", "smaFast", "smaSlow"]),
    ]
    for name, keywords in checks:
        for kw in keywords:
            if kw in afl_content:
                indicators.add(name)
                break
    return sorted(indicators)


def count_params(afl_content: str) -> int:
    """Count the number of Param() calls in AFL source code."""
    if not afl_content:
        return 0
    import re
    return len(re.findall(r'\bParam\s*\(', afl_content))


@app.route("/")
def index():
    """Dashboard home -- list all strategies with version/run counts."""
    strategies = db_list_strategies()
    strategy_summaries = []
    all_indicators = set()
    for s in strategies:
        summary = get_strategy_summary(s["id"])
        if summary:
            # Extract indicator tags from the latest version's AFL
            afl = ""
            if summary.get("latest_version"):
                afl = summary["latest_version"].get("afl_content", "")
            summary["indicators"] = extract_indicators(afl)
            summary["param_count"] = count_params(afl)
            all_indicators.update(summary["indicators"])
            strategy_summaries.append(summary)

    # Also keep flat file listing for any orphan CSVs not in the DB
    result_files = get_result_files()

    # Recent batch runs for the dashboard
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
        is_optimization=parsed.get("is_optimization", False),
        indicator_configs=[],
        symbol_runs={},
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

    # Try SQL-first for optimization runs (survives CSV deletion)
    sql_parsed = None
    if run.get("is_optimization") and run.get("total_combos", 0) > 0:
        try:
            sql_parsed = reconstruct_optimization_parsed(run_id)
        except Exception as exc:
            logger.warning("SQL optimization reconstruction failed: %s", exc)

    if not filepath.exists():
        if sql_parsed:
            # CSV is gone but we have SQL data — use it
            parsed = sql_parsed
            status = run.get("status", "completed")
            has_html = False
        else:
            # Check if the run failed with stored error messages
            run_metrics = run.get("metrics") or {}
            error_msg = run_metrics.get("error")
            validation_errors = run_metrics.get("validation_errors", [])
            if error_msg:
                full_error = error_msg
                if validation_errors:
                    full_error += "\n" + "\n".join(f"  - {e}" for e in validation_errors)
            elif run.get("status") == "failed":
                full_error = "Run failed — no results were produced."
            else:
                full_error = f"Result file not found: {filepath}"
            parsed = {"trades": [], "metrics": {}, "columns": [], "error": full_error, "is_optimization": False}
            status = run.get("status", "pending")
            has_html = False
    else:
        if sql_parsed:
            # Prefer SQL data even when CSV exists (consistent source)
            parsed = sql_parsed
        else:
            # Detect if this was an optimization run via stored params
            run_params = run.get("params") or {}
            force_opt = (run_params.get("run_mode") == 4) or (run.get("metrics", {}).get("run_mode") == 4)
            parsed = parse_results_csv(filepath, force_optimization=force_opt)
        status = get_status(filepath)
        html_companion = filepath.with_suffix(".html")
        has_html = html_companion.exists()

    is_optimization = parsed.get("is_optimization", False)
    strategy = run.get("strategy") or get_strategy_info(run["strategy_id"])
    version = run.get("version")

    # Extract strategy indicator configs from AFL for the trade chart modal
    from scripts.afl_parser import extract_strategy_indicators

    _afl = run.get("afl_content") or (version.get("afl_content", "") if version else "")
    indicator_configs = extract_strategy_indicators(_afl) if _afl else []

    # Build symbol switcher: completed runs of this strategy for the SAME version.
    # Scoping to version_id ensures that switching symbols stays within the
    # same code snapshot — results are always (version, symbol) pairs.
    symbol_runs = {}
    current_version_id = run.get("version_id")
    sibling_runs = db_list_runs(strategy_id=run["strategy_id"])
    for r in sibling_runs:
        if r.get("status") != "completed":
            continue
        if r.get("version_id") != current_version_id:
            continue
        sym = r.get("symbol") or GCZ25_SYMBOL
        if sym not in symbol_runs:
            symbol_runs[sym] = {"run_id": r["id"], "symbol": sym}

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
        default_symbol=GCZ25_SYMBOL,
        symbol_runs=symbol_runs,
        indicator_configs=indicator_configs,
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

    # Parse params from latest version
    try:
        from scripts.afl_parser import parse_afl_params
    except ImportError:
        parse_afl_params = lambda _: []

    params = []
    if versions:
        params = parse_afl_params(versions[0].get("afl_content", ""))

    # Attach version info to each run for display
    version_map = {v["id"]: v for v in versions}
    for run in runs:
        run["version"] = version_map.get(run["version_id"])
        # Parse params from run's AFL content for display
        run_afl = run.get("afl_content", "")
        if run_afl:
            run["params"] = parse_afl_params(run_afl)
        else:
            run["params"] = []

    return render_template(
        "strategy_detail.html",
        strategy=strategy,
        versions=versions,
        runs=runs,
        params=params,
        db_configured=bool(AMIBROKER_DB_PATH),
        default_symbol=GCZ25_SYMBOL,
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


@app.route("/strategy/reverse", methods=["POST"])
def strategy_reverse():
    """Create a reversed copy of a strategy (Buy<->Short, Sell<->Cover) and run backtest."""
    run_id = request.form.get("run_id", "").strip()
    if not run_id:
        flash("No run specified for reversal.", "danger")
        return redirect(url_for("index"))

    run = get_run_with_context(run_id)
    if run is None:
        flash(f"Run '{run_id}' not found.", "danger")
        return redirect(url_for("index"))

    # Get AFL content from the run or its version
    version = run.get("version")
    afl_content = run.get("afl_content") or (version.get("afl_content", "") if version else "")
    if not afl_content.strip():
        flash("No AFL content available to reverse.", "danger")
        return redirect(url_for("run_detail", run_id=run_id))

    # Compute reversed strategy name
    original_strategy = run.get("strategy") or {}
    original_name = original_strategy.get("name", "Unknown")
    reversed_name = f"{original_name}_reverse"

    # Reverse the AFL
    reversed_afl = reverse_afl(afl_content)

    # Find or create the reversed strategy
    existing = db_find_strategy_by_name(reversed_name)
    if existing:
        strategy_id = existing["id"]
    else:
        strategy_id = db_create_strategy(
            name=reversed_name,
            summary=f"Reversed signals from: {original_name}",
            description=f"Auto-generated reversed strategy. Buy/Short and Sell/Cover signals swapped from {original_name}.",
            symbol=original_strategy.get("symbol", ""),
        )

    # Create a new version with the reversed AFL
    version_id = db_create_version(
        strategy_id=strategy_id,
        afl_content=reversed_afl,
        label="Reversed signals",
    )

    # Launch backtest in background
    with _backtest_lock:
        if _backtest_state["running"]:
            flash(f"Reversed strategy '{reversed_name}' created but a backtest is already running.", "warning")
            return redirect(url_for("strategy_detail", strategy_id=strategy_id))

    symbol = run.get("symbol") or None
    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, version_id, None, symbol),
        daemon=True,
    )
    thread.start()
    flash(f"Reversed strategy '{reversed_name}' created. Backtest started.", "info")
    return redirect(url_for("backtest_status_page"))


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


@app.route("/strategy/<strategy_id>/run-with-params", methods=["POST"])
def run_with_params(strategy_id: str):
    """Run a backtest with modified parameter values."""
    try:
        from scripts.afl_parser import parse_afl_params, modify_afl_params
    except ImportError:
        flash("AFL parser not available.", "danger")
        return redirect(url_for("strategy_detail", strategy_id=strategy_id))

    version_id = request.form.get("version_id")
    run_mode = int(request.form.get("run_mode", "2"))
    symbol = request.form.get("symbol", "").strip() or None

    # Get the version's AFL content
    version = db_get_version(version_id)
    if version is None:
        flash("Version not found.", "danger")
        return redirect(url_for("strategy_detail", strategy_id=strategy_id))

    afl_content = version.get("afl_content", "")
    if not afl_content:
        flash("Version has no AFL content.", "danger")
        return redirect(url_for("strategy_detail", strategy_id=strategy_id))

    # Collect parameter overrides, min/max/step overrides, and optimize flags
    current_params = parse_afl_params(afl_content)
    overrides = {}
    min_overrides = {}
    max_overrides = {}
    step_overrides = {}
    optimize_names = set()

    for p in current_params:
        form_key = f"param_{p['name']}"
        form_val = request.form.get(form_key)
        if form_val is not None:
            try:
                overrides[p["name"]] = float(form_val)
            except ValueError:
                pass

        opt_key = f"optimize_{p['name']}"
        if request.form.get(opt_key) == "on":
            optimize_names.add(p["name"])

            # Collect min/max/step overrides for optimized params
            for prefix, target in [("min_", min_overrides), ("max_", max_overrides), ("step_", step_overrides)]:
                val = request.form.get(f"{prefix}{p['name']}")
                if val is not None:
                    try:
                        target[p["name"]] = float(val)
                    except ValueError:
                        pass

    # Modify AFL with overrides
    modified_afl = modify_afl_params(
        afl_content, overrides=overrides, optimize_names=optimize_names,
        min_overrides=min_overrides, max_overrides=max_overrides,
        step_overrides=step_overrides,
    )

    # Create a new version with the modified AFL
    mode_label = "Optimization" if run_mode == 4 else "Backtest"
    changed_params = [
        f"{k}={v}" for k, v in overrides.items()
        if current_params and any(p["name"] == k and p["default"] != v for p in current_params)
    ]
    label = f"Parameter {mode_label.lower()}"
    if changed_params:
        label += f": {', '.join(changed_params[:3])}"
        if len(changed_params) > 3:
            label += f" (+{len(changed_params) - 3} more)"

    new_version_id = db_create_version(
        strategy_id=strategy_id,
        afl_content=modified_afl,
        label=label,
    )

    # Run backtest in background
    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, new_version_id, run_mode, symbol),
        daemon=True,
    )
    thread.start()

    flash(f"{mode_label} started with modified parameters.", "info")
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


@app.route("/api/run/<run_id>/equity-curve")
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
    symbol = request.form.get("symbol", "").strip() or None

    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, version_id, None, symbol),
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
    # Cross-check DB when in-memory state says running — run.py may have
    # already updated the DB to completed/failed before the background
    # thread clears the running flag.
    if state.get("running") and state.get("run_id"):
        run_record = db_get_run(state["run_id"])
        if run_record and run_record.get("status") in ("completed", "failed"):
            state["running"] = False
            state["success"] = run_record["status"] == "completed"
            if not state.get("finished_at"):
                state["finished_at"] = run_record.get("completed_at",
                    datetime.now(timezone.utc).isoformat())
    # Look up run record for symbol/strategy context (for "Run Again" form)
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


@app.route("/api/backtest/status")
def api_backtest_status():
    """JSON API endpoint returning current backtest state."""
    with _backtest_lock:
        state = dict(_backtest_state)
    return jsonify(state)


@app.route("/api/symbols")
def api_symbols():
    """Return available symbols from the AmiBroker database (cached)."""
    try:
        from scripts.ole_backtest import get_cached_symbols
        refresh = request.args.get("refresh") == "1"
        result = get_cached_symbols(refresh=refresh)
        # Filter out AmiBroker internal symbols (~~~EQUITY, etc.)
        symbols = [s for s in result["symbols"] if not s.startswith("~~~")]
        return jsonify({
            "symbols": symbols,
            "default": GCZ25_SYMBOL,
            "stale": result["stale"],
        })
    except Exception as exc:
        logger.warning("Failed to list symbols: %s", exc)
        return jsonify({"symbols": [], "default": GCZ25_SYMBOL, "stale": True})


@app.route("/api/run/<run_id>/opt-progress")
def api_opt_progress(run_id: str):
    """Return real-time optimization progress for a running backtest.

    Reads ``opt_status.json`` from the run's results directory (written by
    the OLE polling loop in ``ole_backtest.py``).
    """
    run_dir = RESULTS_DIR / run_id
    status_file = run_dir / "opt_status.json"

    # Determine overall run status — always check DB for ground truth first
    # to avoid race condition where _backtest_state["running"] is still True
    # but run.py has already updated the DB to "completed".
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


@app.route("/api/run/<run_id>/abort", methods=["POST"])
def api_abort_run(run_id: str):
    """Request abort of a running optimization/backtest.

    Writes an ``abort_requested`` sentinel file that the OLE polling loop
    picks up to call ``analysis_doc.Abort()``.
    """
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
# Sprint 3 / Sprint 4 routes -- Trade candlestick charts
# ---------------------------------------------------------------------------

VALID_INTERVALS = set(CHART_SETTINGS.get("valid_intervals", [60, 300, 600, 86400]))


@app.route("/api/ohlcv/<symbol>")
def api_ohlcv(symbol: str):
    """Return OHLCV candlestick data for *symbol* around a trade.

    Query parameters:
        entry_date  -- trade entry datetime (CSV format: ``7/21/2025 1:14:50 AM``)
        exit_date   -- trade exit datetime (same format)
        interval    -- bar interval in seconds (60, 300, 600, 86400). Default: 60
        indicators  -- JSON array of ``{"type": str, "params": dict}`` configs
    """
    from scripts.ole_stock_data import get_ohlcv_cached

    entry_date_str = request.args.get("entry_date", "").strip()
    exit_date_str = request.args.get("exit_date", "").strip()

    if not entry_date_str or not exit_date_str:
        return jsonify({"data": [], "error": "entry_date and exit_date are required."}), 400

    # --- Parse interval ---
    try:
        interval = int(request.args.get("interval", "60"))
    except ValueError:
        interval = 60
    if interval not in VALID_INTERVALS:
        interval = 60

    # --- Parse indicators ---
    indicators_str = request.args.get("indicators", "").strip()
    indicator_configs = []
    if indicators_str:
        try:
            indicator_configs = json.loads(indicators_str)
            if not isinstance(indicator_configs, list):
                indicator_configs = []
        except json.JSONDecodeError:
            indicator_configs = []

    # --- Parse dates ---
    entry_dt = _parse_trade_date(entry_date_str)
    exit_dt = _parse_trade_date(exit_date_str)

    if entry_dt is None or exit_dt is None:
        return jsonify({"data": [], "error": f"Invalid date format. Got entry='{entry_date_str}', exit='{exit_date_str}'."}), 400

    # Scale padding to match timeframe
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

    # --- Compute indicators ---
    if indicator_configs and result["data"]:
        from scripts.indicators import compute_indicators
        result["indicators"] = compute_indicators(result["data"], indicator_configs)
    else:
        result["indicators"] = []

    return jsonify(result)


@app.route("/api/run/<run_id>/indicators")
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


@app.route("/api/indicators")
def api_indicators():
    """Return list of available indicator types."""
    from scripts.indicators import get_available_indicators
    return jsonify(get_available_indicators())


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


# ---------------------------------------------------------------------------
# Indicator Library routes
# ---------------------------------------------------------------------------


@app.route("/indicators")
def indicator_list():
    """Indicator library list page."""
    from scripts.indicator_library import list_indicators

    indicators = list_indicators()

    # Show import button if external source dirs have files to import
    show_import = False
    ext_dir = Path(r"C:\Users\prestondinova\Documents\indicators")
    ext_file = Path(r"C:\Users\prestondinova\Documents\market_sessions.afl")
    if ext_dir.exists() or ext_file.exists():
        show_import = True

    return render_template(
        "indicator_list.html",
        indicators=indicators,
        show_import=show_import,
    )


@app.route("/indicators/create", methods=["GET", "POST"])
def indicator_create():
    """Create a new indicator file."""
    if request.method == "GET":
        return render_template(
            "indicator_editor.html",
            filename="",
            content="",
            meta=None,
        )

    from scripts.indicator_library import save_indicator

    filename = request.form.get("filename", "").strip()
    content = request.form.get("afl_content", "")

    if not filename:
        flash("Filename is required.", "danger")
        return render_template(
            "indicator_editor.html",
            filename=filename,
            content=content,
            meta=None,
        )

    # Ensure .afl extension
    if not filename.endswith(".afl"):
        filename += ".afl"

    ok, msg = save_indicator(filename, content)
    if ok:
        flash(f"Indicator '{filename}' created.", "success")
        return redirect(url_for("indicator_edit", filename=filename))
    else:
        flash(f"Error: {msg}", "danger")
        return render_template(
            "indicator_editor.html",
            filename=filename,
            content=content,
            meta=None,
        )


@app.route("/indicators/<filename>")
def indicator_edit(filename: str):
    """Indicator editor page -- view/edit a single indicator AFL file."""
    from scripts.indicator_library import read_indicator

    content, meta = read_indicator(filename)
    if meta is None:
        flash(f"Indicator '{filename}' not found.", "danger")
        return redirect(url_for("indicator_list"))
    return render_template(
        "indicator_editor.html",
        filename=filename,
        content=content,
        meta=meta,
    )


@app.route("/indicators/<filename>/save", methods=["POST"])
def indicator_save(filename: str):
    """Save indicator content."""
    from scripts.indicator_library import save_indicator

    content = request.form.get("afl_content", "")
    ok, msg = save_indicator(filename, content)
    if ok:
        flash(f"Indicator '{filename}' saved.", "success")
    else:
        flash(f"Error saving: {msg}", "danger")
    return redirect(url_for("indicator_edit", filename=filename))


@app.route("/indicators/<filename>/delete", methods=["POST"])
def indicator_delete(filename: str):
    """Delete an indicator file."""
    from scripts.indicator_library import delete_indicator

    ok, msg = delete_indicator(filename)
    if ok:
        flash(f"Indicator '{filename}' deleted.", "success")
    else:
        flash(f"Error: {msg}", "danger")
    return redirect(url_for("indicator_list"))


@app.route("/indicators/import", methods=["POST"])
def indicator_import():
    """Import indicators from external directories."""
    from scripts.indicator_library import import_indicators

    source_paths = []
    ext_dir = Path(r"C:\Users\prestondinova\Documents\indicators")
    ext_file = Path(r"C:\Users\prestondinova\Documents\market_sessions.afl")
    if ext_dir.exists():
        source_paths.append(ext_dir)
    if ext_file.exists():
        source_paths.append(ext_file)

    if not source_paths:
        flash("No external indicator sources found.", "warning")
        return redirect(url_for("indicator_list"))

    results = import_indicators(source_paths)
    imported = sum(1 for _, ok, _ in results if ok)
    skipped = sum(1 for _, ok, msg in results if not ok and "skip" in msg.lower())
    flash(f"Imported {imported} indicator(s), {skipped} skipped (already exist).", "success")
    return redirect(url_for("indicator_list"))


# ---------------------------------------------------------------------------
# Indicator Library API routes
# ---------------------------------------------------------------------------


@app.route("/api/indicators/library")
def api_indicator_library():
    """JSON API: list all indicators with parsed metadata."""
    from scripts.indicator_library import list_indicators
    from dataclasses import asdict

    indicators = list_indicators()
    return jsonify([asdict(ind) for ind in indicators])


@app.route("/api/indicators/library/<filename>")
def api_indicator_detail(filename: str):
    """JSON API: single indicator content + metadata."""
    from scripts.indicator_library import read_indicator
    from dataclasses import asdict

    content, meta = read_indicator(filename)
    if meta is None:
        return jsonify({"error": f"Indicator '{filename}' not found"}), 404
    result = asdict(meta)
    result["content"] = content
    return jsonify(result)


@app.route("/api/indicators/generate-include", methods=["POST"])
def api_generate_include():
    """JSON API: generate #include AFL block from a configuration.

    Request body JSON:
        {"indicators": [
            {"filename": "tema.afl", "params": {"smoothingLength": "14", "sourcePrice": "Close"}},
            {"filename": "vwap_clouds.afl", "params": {"vwStdev1": "1.0"}}
        ]}

    Response JSON:
        {"afl_block": "// ---- Indicator Includes ...\\n...", "warnings": []}
    """
    from scripts.indicator_library import generate_include_block

    data = request.get_json(silent=True) or {}
    indicators = data.get("indicators", [])

    if not indicators:
        return jsonify({"error": "No indicators specified"}), 400

    try:
        afl_block = generate_include_block(indicators)
        return jsonify({"afl_block": afl_block, "warnings": []})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


# ---------------------------------------------------------------------------
# Batch backtest routes
# ---------------------------------------------------------------------------


@app.route("/api/batch/backtest", methods=["POST"])
def api_batch_start():
    """Start a batch backtest across multiple strategies."""
    with _batch_lock:
        if _batch_state["running"]:
            return jsonify({"error": "A batch is already running."}), 409

    data = request.get_json(silent=True) or {}
    strategy_ids = data.get("strategy_ids", [])
    run_mode = data.get("run_mode", 2)
    name = data.get("name", "")

    # Default to all strategies if none specified
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


@app.route("/api/batch/<batch_id>/status")
def api_batch_status(batch_id: str):
    """Poll batch progress."""
    batch = db_get_batch(batch_id)
    if batch is None:
        return jsonify({"error": "Batch not found"}), 404

    # Fetch individual run details for each run_id in the batch
    runs = []
    for run_id in batch.get("run_ids", []):
        run = db_get_run(run_id)
        if run:
            runs.append(run)
    batch["runs"] = runs

    return jsonify(batch)


@app.route("/api/batch/<batch_id>/cancel", methods=["POST"])
def api_batch_cancel(batch_id: str):
    """Cancel a running batch."""
    with _batch_lock:
        if _batch_state["batch_id"] != batch_id:
            return jsonify({"error": "Batch is not currently running."}), 409
        runner = _batch_state.get("runner")
        if runner:
            runner.cancel()

    return jsonify({"status": "cancelling"})


@app.route("/api/batch/list")
def api_batch_list():
    """List all batch runs."""
    batches = db_list_batches()
    return jsonify(batches)


@app.route("/batch/<batch_id>")
def batch_dashboard(batch_id: str):
    """Batch dashboard HTML page."""
    batch = db_get_batch(batch_id)
    if batch is None:
        flash(f"Batch '{batch_id}' not found.", "danger")
        return redirect(url_for("index"))

    # Build strategy lookup dict for the template
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


@app.route("/batch/history")
def batch_history():
    """Batch history page listing all batches."""
    batches = db_list_batches()
    return render_template(
        "batch_history.html",
        batches=batches,
        db_configured=bool(AMIBROKER_DB_PATH),
    )


# ---------------------------------------------------------------------------
# Optimization routes
# ---------------------------------------------------------------------------


@app.route("/api/strategy/<strategy_id>/optimize", methods=["POST"])
def api_strategy_optimize(strategy_id: str):
    """Run an optimization backtest for a strategy."""
    try:
        from scripts.afl_parser import parse_afl_params, modify_afl_params
    except ImportError:
        return jsonify({"error": "AFL parser not available."}), 500

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found."}), 404

    data = request.get_json(silent=True) or {}
    params_to_optimize = data.get("params_to_optimize", [])
    min_overrides = data.get("min_overrides", {})
    max_overrides = data.get("max_overrides", {})
    step_overrides = data.get("step_overrides", {})

    # Get latest version's AFL content
    version = db_get_latest_version(strategy_id)
    if version is None:
        return jsonify({"error": "No versions found for strategy."}), 404

    afl_content = version.get("afl_content", "")
    if not afl_content:
        return jsonify({"error": "Version has no AFL content."}), 400

    # Modify AFL: convert Param -> Optimize for selected params
    optimize_names = set(params_to_optimize)
    modified_afl = modify_afl_params(
        afl_content,
        optimize_names=optimize_names,
        min_overrides=min_overrides,
        max_overrides=max_overrides,
        step_overrides=step_overrides,
    )

    # Create a new version with the modified AFL
    param_list = ", ".join(params_to_optimize[:3])
    if len(params_to_optimize) > 3:
        param_list += f" (+{len(params_to_optimize) - 3} more)"
    label = f"Optimization: {param_list}" if param_list else "Optimization run"

    new_version_id = db_create_version(
        strategy_id=strategy_id,
        afl_content=modified_afl,
        label=label,
    )

    # Run backtest with run_mode=4 (optimization) in background
    thread = threading.Thread(
        target=_run_backtest_background,
        args=(strategy_id, new_version_id, 4),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "version_id": new_version_id,
        "status": "running",
        "params_optimized": params_to_optimize,
    })


@app.route("/api/strategy/<strategy_id>/param-analysis")
def api_strategy_param_analysis(strategy_id: str):
    """Get parameter optimization suggestions for a strategy."""
    from scripts.param_advisor import analyze_strategy_params

    analysis = analyze_strategy_params(strategy_id)
    return jsonify(analysis)


# ---------------------------------------------------------------------------
# Indicator Explorer
# ---------------------------------------------------------------------------

# In-memory cache of OHLCV bars for the explorer (avoids re-fetching from
# AmiBroker on every parameter slider change).
_explorer_bars_cache: dict = {}  # key: (strategy_id, interval) -> bars list


@app.route("/strategy/<strategy_id>/explore")
def strategy_explore(strategy_id: str):
    """Render the interactive indicator explorer page for a strategy."""
    from scripts.afl_parser import (
        parse_afl_params, extract_strategy_indicators, build_code_map,
    )

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        flash("Strategy not found.", "error")
        return redirect("/")

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""

    params = parse_afl_params(afl_content) if afl_content else []
    indicator_configs = extract_strategy_indicators(afl_content) if afl_content else []
    description = strategy.get("description", "") if strategy else ""
    code_map = build_code_map(description, afl_content) if afl_content else []

    explore_symbol = request.args.get("symbol") or GCZ25_SYMBOL

    return render_template(
        "indicator_explorer.html",
        strategy=strategy,
        params=params,
        indicator_configs=indicator_configs,
        symbol=explore_symbol,
        afl_content=afl_content,
        code_map=code_map,
        db_configured=bool(AMIBROKER_DB_PATH),
    )


@app.route("/api/strategy/<strategy_id>/explorer-data")
def api_strategy_explorer_data(strategy_id: str):
    """Fetch OHLCV bars + computed indicators for the indicator explorer.

    Query parameters:
        interval  -- bar interval in seconds (default 60)
        symbol    -- ticker symbol to fetch data for (default GCZ25_SYMBOL)
        param_*   -- strategy parameter overrides (e.g. param_TEMA Length=30)
    """
    import time as _perf_time
    _t_total_start = _perf_time.perf_counter()

    from scripts.ole_stock_data import get_latest_bars
    from scripts.indicators import compute_indicators
    from scripts.afl_parser import parse_afl_params, extract_strategy_indicators

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found"}), 404

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        return jsonify({"error": "No AFL content in latest version"}), 400

    # Parse interval
    try:
        interval = int(request.args.get("interval", "60"))
    except ValueError:
        interval = 60
    if interval not in VALID_INTERVALS:
        interval = 60

    # Parse days (date range for data window)
    default_days = CHART_SETTINGS.get("explorer_default_days", 5)
    try:
        days = int(request.args.get("days", str(default_days)))
    except ValueError:
        days = default_days
    # Clamp to reasonable range (1-365)
    days = max(1, min(days, 365))

    # Optional end_date -- lets users sample data around a specific date
    end_date = request.args.get("end_date")  # YYYY-MM-DD or None

    # Parse param overrides from query string
    param_overrides = {}
    for key, val in request.args.items():
        if key.startswith("param_"):
            param_name = key[6:]  # strip "param_" prefix
            try:
                param_overrides[param_name] = float(val)
            except ValueError:
                pass

    _t_afl_parse_start = _perf_time.perf_counter()
    # Get indicator configs from AFL
    indicator_configs = extract_strategy_indicators(afl_content)

    # Apply param overrides to indicator configs
    for cfg in indicator_configs:
        mapping = cfg.get("param_mapping", {})
        for ind_param, afl_param_name in mapping.items():
            if afl_param_name in param_overrides:
                cfg["params"][ind_param] = param_overrides[afl_param_name]
    _t_afl_parse_ms = (_perf_time.perf_counter() - _t_afl_parse_start) * 1000

    # Resolve symbol from query string (default to GCZ25_SYMBOL)
    explore_symbol = request.args.get("symbol") or GCZ25_SYMBOL

    # Fetch OHLCV bars -- use get_latest_bars with days-based filtering
    from datetime import datetime

    cache_key = (strategy_id, explore_symbol, interval, days, end_date)
    cached = _explorer_bars_cache.get(cache_key)

    data_range = None
    _bar_source = "cache"
    _t_bars_start = _perf_time.perf_counter()
    if cached and (datetime.now() - cached["fetched_at"]).total_seconds() < 300:
        # Use cached bars (valid for 5 minutes)
        bars = cached["bars"]
        data_range = cached.get("data_range")
    else:
        _bar_source = "amiBroker_COM"
        result = get_latest_bars(
            symbol=explore_symbol,
            interval=interval,
            days=days,
            end_date=end_date,
        )
        if result.get("error"):
            return jsonify({"error": result["error"]}), 503
        bars = result.get("data", [])
        data_range = result.get("data_range")
        _explorer_bars_cache[cache_key] = {
            "bars": bars,
            "data_range": data_range,
            "fetched_at": datetime.now(),
        }
    _t_bars_ms = (_perf_time.perf_counter() - _t_bars_start) * 1000

    if not bars:
        return jsonify({
            "error": "No bar data available for this date range.",
            "data_range": data_range,
        }), 404

    # Compute indicators
    _t_indicators_start = _perf_time.perf_counter()
    ind_configs_for_compute = [
        {"type": cfg["type"], "params": cfg["params"]}
        for cfg in indicator_configs
    ]
    computed = compute_indicators(bars, ind_configs_for_compute)

    # Merge overlay/color info back into computed results
    for i, ind in enumerate(computed):
        if i < len(indicator_configs):
            ind["overlay"] = indicator_configs[i].get("overlay", True)
            ind["color"] = indicator_configs[i].get("color", "#FF6D00")
    _t_indicators_ms = (_perf_time.perf_counter() - _t_indicators_start) * 1000

    _t_total_ms = (_perf_time.perf_counter() - _t_total_start) * 1000
    _timing = {
        "total_ms": round(_t_total_ms, 1),
        "afl_parse_ms": round(_t_afl_parse_ms, 1),
        "bar_fetch_ms": round(_t_bars_ms, 1),
        "bar_source": _bar_source,
        "bar_count": len(bars),
        "indicator_compute_ms": round(_t_indicators_ms, 1),
        "indicator_count": len(computed),
    }
    app.logger.info("explorer-data timing: %s", _timing)

    return jsonify({
        "bars": bars,
        "indicators": computed,
        "indicator_configs": indicator_configs,
        "data_range": data_range,
        "_timing": _timing,
    })


@app.route("/api/strategy/<strategy_id>/recalculate", methods=["POST"])
def api_strategy_recalculate(strategy_id: str):
    """Recalculate indicators with new parameter values (no OHLCV re-fetch).

    Request body:
        {"params": {"TEMA Length": 30, ...}, "interval": 60}
    """
    from scripts.indicators import compute_indicators
    from scripts.afl_parser import extract_strategy_indicators

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found"}), 404

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        return jsonify({"error": "No AFL content"}), 400

    body = request.get_json(silent=True) or {}
    param_overrides = body.get("params", {})
    interval = body.get("interval", 60)
    days = body.get("days", CHART_SETTINGS.get("explorer_default_days", 5))
    end_date = body.get("end_date")  # may be None
    recalc_symbol = body.get("symbol") or GCZ25_SYMBOL

    # Get cached bars -- cache key must match what explorer-data used
    cache_key = (strategy_id, recalc_symbol, interval, days, end_date)
    cached = _explorer_bars_cache.get(cache_key)
    if not cached or not cached.get("bars"):
        # Fallback: try to find any cached bars for this strategy+symbol
        for k, v in _explorer_bars_cache.items():
            if k[0] == strategy_id and k[1] == recalc_symbol and v.get("bars"):
                cached = v
                break
    if not cached or not cached.get("bars"):
        return jsonify({"error": "No cached bars. Load the explorer first."}), 400

    bars = cached["bars"]

    # Get indicator configs and apply overrides
    indicator_configs = extract_strategy_indicators(afl_content)
    for cfg in indicator_configs:
        mapping = cfg.get("param_mapping", {})
        for ind_param, afl_param_name in mapping.items():
            if afl_param_name in param_overrides:
                try:
                    cfg["params"][ind_param] = float(param_overrides[afl_param_name])
                except (ValueError, TypeError):
                    pass

    # Compute indicators
    ind_configs_for_compute = [
        {"type": cfg["type"], "params": cfg["params"]}
        for cfg in indicator_configs
    ]
    computed = compute_indicators(bars, ind_configs_for_compute)

    # Merge overlay/color info
    for i, ind in enumerate(computed):
        if i < len(indicator_configs):
            ind["overlay"] = indicator_configs[i].get("overlay", True)
            ind["color"] = indicator_configs[i].get("color", "#FF6D00")

    return jsonify({"indicators": computed})


# ---------------------------------------------------------------------------
# Signal Computation via AmiBroker Exploration
# ---------------------------------------------------------------------------

@app.route("/api/strategy/<strategy_id>/signals", methods=["POST"])
def api_strategy_signals(strategy_id: str):
    """Compute Buy/Short/Sell/Cover signals via AmiBroker OLE Exploration.

    Runs the full strategy AFL through AmiBroker's native engine with current
    slider parameter values.  Returns signal timestamps for chart markers.

    Request body:
        {"params": {...}, "symbol": "NQ", "interval": 60, "days": 1, "end_date": null}
    """
    from scripts.ole_bar_analyzer import compute_signals_via_exploration

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found"}), 404

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        return jsonify({"error": "No AFL content"}), 400

    body = request.get_json(silent=True) or {}
    param_overrides = body.get("params", {})
    interval = body.get("interval", 60)
    days = body.get("days", CHART_SETTINGS.get("explorer_default_days", 5))
    end_date = body.get("end_date")
    sig_symbol = body.get("symbol") or GCZ25_SYMBOL

    # Convert param values to float
    pv = {}
    for k, v in param_overrides.items():
        try:
            pv[k] = float(v)
        except (ValueError, TypeError):
            pass

    result = compute_signals_via_exploration(
        afl_content=afl_content,
        param_values=pv,
        symbol=sig_symbol,
        interval=interval,
    )

    if result.get("error"):
        app.logger.warning("Signal computation error: %s", result["error"])
        return jsonify(result), 503

    # Filter signals to the visible chart range (based on cached bars)
    from datetime import datetime
    cache_key = (strategy_id, sig_symbol, interval, days, end_date)
    cached = _explorer_bars_cache.get(cache_key)
    if cached and cached.get("bars"):
        bars = cached["bars"]
        min_time = bars[0]["time"]
        max_time = bars[-1]["time"]
        for key in ("buy", "short", "sell", "cover"):
            result[key] = [
                s for s in result[key]
                if min_time <= s["time"] <= max_time
            ]

    app.logger.info(
        "Signals for %s: %d Buy, %d Short, %d Sell, %d Cover (%dms)",
        strategy_id[:8],
        len(result.get("buy", [])), len(result.get("short", [])),
        len(result.get("sell", [])), len(result.get("cover", [])),
        result.get("elapsed_ms", 0),
    )

    return jsonify(result)


# ---------------------------------------------------------------------------
# Bar Analysis API (OLE Exploration)
# ---------------------------------------------------------------------------

@app.route("/api/strategy/<strategy_id>/analyze-bar", methods=["POST"])
def api_analyze_bar(strategy_id: str):
    """Analyze signal conditions at a specific bar using AmiBroker OLE.

    Runs an Exploration through AmiBroker's actual AFL engine to determine
    which Buy/Short sub-conditions pass or fail at the clicked bar.

    Request body:
        {
          "bar_time": int,     // Unix timestamp of the bar
          "params": {},        // Current slider parameter values
          "interval": 60,
          "days": 5,
          "end_date": null
        }
    """
    from scripts.ole_bar_analyzer import analyze_bar

    strategy = db_get_strategy(strategy_id)
    if strategy is None:
        return jsonify({"error": "Strategy not found"}), 404

    version = db_get_latest_version(strategy_id)
    afl_content = version.get("afl_content", "") if version else ""
    if not afl_content:
        return jsonify({"error": "No AFL content"}), 400

    body = request.get_json(silent=True) or {}
    bar_time = body.get("bar_time")
    if bar_time is None:
        return jsonify({"error": "bar_time is required"}), 400

    param_overrides = body.get("params", {})

    # Convert param values to float
    pv = {}
    for k, v in param_overrides.items():
        try:
            pv[k] = float(v)
        except (ValueError, TypeError):
            pass

    try:
        result = analyze_bar(
            afl_content=afl_content,
            target_unix_ts=int(bar_time),
            strategy_id=strategy_id,
            param_values=pv,
        )
        return jsonify({"analysis": result})
    except Exception as exc:
        logger.exception("Bar analysis failed: %s", exc)
        return jsonify({"error": f"Analysis failed: {exc}"}), 500


# ---------------------------------------------------------------------------
# Param tooltips API
# ---------------------------------------------------------------------------

@app.route("/api/param-tooltips")
def api_param_tooltips():
    """Return all parameter tooltips as a dict keyed by parameter name."""
    return jsonify({"tooltips": db_get_all_param_tooltips_dict()})


@app.route("/api/param-tooltips/<name>")
def api_param_tooltip_get(name: str):
    """Return a single parameter tooltip by name."""
    tip = db_get_param_tooltip(name)
    if tip is None:
        return jsonify({"error": f"No tooltip for '{name}'"}), 404
    return jsonify(tip)


@app.route("/api/param-tooltips/<name>", methods=["PUT"])
def api_param_tooltip_upsert(name: str):
    """Create or update a parameter tooltip."""
    data = request.get_json(silent=True) or {}
    db_upsert_param_tooltip(
        name=name,
        indicator=data.get("indicator", ""),
        math=data.get("math", ""),
        param=data.get("param", ""),
        typical=data.get("typical", ""),
        guidance=data.get("guidance", ""),
    )
    return jsonify({"ok": True, "name": name})


@app.route("/api/param-tooltips/<name>", methods=["DELETE"])
def api_param_tooltip_delete(name: str):
    """Delete a parameter tooltip."""
    deleted = db_delete_param_tooltip(name)
    if not deleted:
        return jsonify({"error": f"No tooltip for '{name}'"}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Indicator tooltips API
# ---------------------------------------------------------------------------

@app.route("/api/indicator-tooltips")
def api_indicator_tooltips():
    """Return all indicator tooltips as a dict keyed by keyword."""
    return jsonify({"tooltips": db_get_all_indicator_tooltips_dict()})


@app.route("/api/indicator-tooltips/<keyword>")
def api_indicator_tooltip_get(keyword: str):
    """Return a single indicator tooltip by keyword."""
    tip = db_get_indicator_tooltip(keyword)
    if tip is None:
        return jsonify({"error": f"No tooltip for '{keyword}'"}), 404
    return jsonify(tip)


@app.route("/api/indicator-tooltips/<keyword>", methods=["PUT"])
def api_indicator_tooltip_upsert(keyword: str):
    """Create or update an indicator tooltip."""
    data = request.get_json(silent=True) or {}
    db_upsert_indicator_tooltip(
        keyword=keyword,
        name=data.get("name", ""),
        description=data.get("description", ""),
        math=data.get("math", ""),
        usage=data.get("usage", ""),
        key_params=data.get("key_params", ""),
    )
    return jsonify({"ok": True, "keyword": keyword})


@app.route("/api/indicator-tooltips/<keyword>", methods=["DELETE"])
def api_indicator_tooltip_delete(keyword: str):
    """Delete an indicator tooltip."""
    deleted = db_delete_indicator_tooltip(keyword)
    if not deleted:
        return jsonify({"error": f"No tooltip for '{keyword}'"}), 404
    return jsonify({"ok": True})
