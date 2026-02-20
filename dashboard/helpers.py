"""
Shared helper functions used by multiple blueprint modules.

These functions are factored out of app.py so that any blueprint
can import them without circular dependencies.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from config.settings import (
    RESULTS_DIR, AFL_STRATEGY_FILE, APX_TEMPLATE, APX_OUTPUT,
    BACKTEST_SETTINGS, AFL_DIR, CHART_SETTINGS,
)

logger = logging.getLogger(__name__)

STAGED_DIR: Path = RESULTS_DIR / "staged"
VERSIONS_DIR: Path = AFL_DIR / "versions"
VALID_INTERVALS = set(CHART_SETTINGS.get("valid_intervals", [60, 300, 600, 86400]))


# ---------------------------------------------------------------------------
# Result file helpers
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
    """Heuristic: does this CSV look like AmiBroker optimization output?"""
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


def parse_results_csv(filepath: Path, force_optimization: bool = False, symbol_filter: str = None) -> dict:
    """Parse a backtest or optimization CSV into a dict."""
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

    is_opt = force_optimization or _is_optimization_csv(df)
    if is_opt:
        return _parse_optimization_results(df)

    # Symbol filtering
    if symbol_filter and symbol_filter != "__ALL__":
        sym_col = None
        for col in df.columns:
            if col.strip().lower() == "symbol":
                sym_col = col
                break
        if sym_col is not None:
            unique_symbols = df[sym_col].astype(str).str.strip().str.upper().unique()
            if len(unique_symbols) > 1:
                mask = df[sym_col].astype(str).str.strip().str.upper() == symbol_filter.strip().upper()
                df = df[mask].reset_index(drop=True)
                if df.empty:
                    result["error"] = f"No trades found for symbol '{symbol_filter}'."
                    return result

    result["columns"] = list(df.columns)
    result["trades"] = df.fillna("").to_dict(orient="records")

    metrics: dict = {}
    metrics["total_trades"] = len(df)

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
            continue
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
        df = df.sort_values(net_profit_col, ascending=False, key=lambda x: pd.to_numeric(x, errors="coerce"))
        clean_vals = vals.dropna()
        if len(clean_vals) > 0:
            result["metrics"]["best_net_profit"] = round(float(clean_vals.max()), 2)
            result["metrics"]["worst_net_profit"] = round(float(clean_vals.min()), 2)
            result["metrics"]["avg_net_profit"] = round(float(clean_vals.mean()), 2)
            result["metrics"]["profitable_combos"] = int((clean_vals > 0).sum())
            result["metrics"]["net_profit_column"] = net_profit_col

    trades_col = None
    for col in df.columns:
        if col.strip().lower() in ("# trades", "trades", "all trades"):
            trades_col = col
            break
    if trades_col:
        tvals = pd.to_numeric(df[trades_col], errors="coerce").dropna()
        if len(tvals) > 0:
            result["metrics"]["avg_trades"] = round(float(tvals.mean()), 1)

    result["trades"] = df.fillna("").to_dict(orient="records")
    return result


# ---------------------------------------------------------------------------
# AFL helpers
# ---------------------------------------------------------------------------


def get_afl_content() -> str:
    """Read the AFL strategy file and return its text content."""
    if AFL_STRATEGY_FILE.exists():
        try:
            return AFL_STRATEGY_FILE.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to read AFL file %s: %s", AFL_STRATEGY_FILE, exc)
            return ""
    return ""


def validate_afl_content(content: str) -> list[str]:
    """Run AFL pre-flight checks and return a list of warning strings."""
    from scripts.afl_validator import validate_afl
    ok, errors = validate_afl(content)
    return errors


def save_afl_content(content: str) -> tuple:
    """Write *content* to the AFL strategy file and rebuild the .apx file."""
    try:
        AFL_STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
        AFL_STRATEGY_FILE.write_text(content, encoding="utf-8")
        logger.info("AFL file saved: %s (%d chars)", AFL_STRATEGY_FILE, len(content))
    except Exception as exc:
        return (False, f"Failed to write AFL file: {exc}")

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


def compute_equity_curve(filepath: Path, symbol_filter: str = None) -> dict:
    """Compute equity curve data supporting both trade-based and time-based views."""
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

    if symbol_filter and symbol_filter != "__ALL__":
        sym_col = None
        for col in df.columns:
            if col.strip().lower() == "symbol":
                sym_col = col
                break
        if sym_col is not None:
            unique_symbols = df[sym_col].astype(str).str.strip().str.upper().unique()
            if len(unique_symbols) > 1:
                mask = df[sym_col].astype(str).str.strip().str.upper() == symbol_filter.strip().upper()
                df = df[mask].reset_index(drop=True)
                if df.empty:
                    result["error"] = f"No trades found for symbol '{symbol_filter}'."
                    return result

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

    bars_col = None
    for col in df.columns:
        if col.lower() in ("barsintrade", "bars_in_trade", "bars", "duration"):
            bars_col = col
            break

    # --- TRADE VIEW ---
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

    # --- TIME VIEW ---
    if date_col is not None:
        try:
            trade_dates_parsed = pd.to_datetime(df[date_col], errors="coerce").dropna()
            if len(trade_dates_parsed) > 0:
                min_date = trade_dates_parsed.min()
                max_date = trade_dates_parsed.max()

                all_dates = pd.date_range(start=min_date, end=max_date, freq="B")

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

    return result


# ---------------------------------------------------------------------------
# AFL version management helpers
# ---------------------------------------------------------------------------


def get_afl_versions() -> list:
    """Return list of saved AFL versions, newest first."""
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    versions = []
    for f in sorted(VERSIONS_DIR.glob("*.afl"), reverse=True):
        stat = f.stat()
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
    """Save a versioned snapshot of AFL content."""
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
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
    """Load a specific AFL version content."""
    version_path = VERSIONS_DIR / version_name
    if not version_path.exists():
        return (False, f"Version not found: {version_name}")
    try:
        content = version_path.read_text(encoding="utf-8")
        return (True, content)
    except Exception as exc:
        return (False, str(exc))


# ---------------------------------------------------------------------------
# Indicator / param extraction helpers
# ---------------------------------------------------------------------------


def extract_indicators(afl_content: str) -> list[str]:
    """Detect which technical indicators an AFL strategy uses."""
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
    return len(re.findall(r'\bParam\s*\(', afl_content))


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
