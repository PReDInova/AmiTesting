"""
Main entry point for the AmiTesting backtest pipeline.

Orchestrates the full backtest workflow:
  1. Resolve the strategy and version to run.
  2. Create a GUID-based backtest run record.
  3. Build the .apx project file from the AFL strategy and template.
  4. Run the OLE-based AmiBroker backtest (results go to results/<run_id>/).
  5. Update the run record with status and metrics.
"""

import json
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so that package imports work regardless
# of the working directory the script is launched from.
# ---------------------------------------------------------------------------
PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from config.settings import (
    setup_logging,
    AMIBROKER_DB_PATH,
    AFL_STRATEGY_FILE,
    APX_OUTPUT,
    APX_TEMPLATE,
    RESULTS_DIR,
    GCZ25_SYMBOL,
)
from scripts.afl_validator import validate_afl_file, auto_fix_afl
from scripts.afl_parser import calculate_optimization_combos, inject_progress_tracker
from scripts.apx_builder import build_apx
from scripts.ole_backtest import OLEBacktester
from scripts.strategy_db import (
    init_db,
    seed_default_strategies,
    seed_param_tooltips,
    seed_indicator_tooltips,
    list_strategies,
    get_latest_version,
    create_run,
    update_run,
    store_optimization_combos,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_optimization_metrics(df) -> dict:
    """Extract summary metrics from an AmiBroker optimization CSV.

    Optimization CSVs have one row per parameter combination with columns
    like 'Net Profit', '# Trades', 'Max. system % drawdown', etc.
    """
    import pandas as pd

    metrics: dict = {"combos_tested": len(df)}

    # Find the net profit column (case-insensitive partial match)
    net_profit_col = None
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ("net profit", "net profit %", "profit"):
            net_profit_col = col
            if "%" not in cl:
                break  # prefer the raw dollar column

    if net_profit_col:
        vals = pd.to_numeric(df[net_profit_col], errors="coerce").dropna()
        if len(vals) > 0:
            best_idx = vals.idxmax()
            metrics["best_net_profit"] = round(float(vals.loc[best_idx]), 2)
            metrics["worst_net_profit"] = round(float(vals.min()), 2)
            metrics["avg_net_profit"] = round(float(vals.mean()), 2)
            metrics["profitable_combos"] = int((vals > 0).sum())
            metrics["net_profit_column"] = net_profit_col

    # Find # trades column
    trades_col = None
    for col in df.columns:
        if col.strip().lower() in ("# trades", "trades", "all trades"):
            trades_col = col
            break
    if trades_col:
        vals = pd.to_numeric(df[trades_col], errors="coerce").dropna()
        if len(vals) > 0:
            metrics["avg_trades"] = round(float(vals.mean()), 1)
            metrics["max_trades"] = int(vals.max())

    # Find drawdown column
    dd_col = None
    for col in df.columns:
        if "drawdown" in col.lower():
            dd_col = col
            break
    if dd_col:
        vals = pd.to_numeric(df[dd_col], errors="coerce").dropna()
        if len(vals) > 0:
            metrics["best_drawdown"] = round(float(vals.max()), 2)  # least negative
            metrics["worst_drawdown"] = round(float(vals.min()), 2)

    return metrics


def _classify_optimization_columns(df) -> tuple[list[str], list[str]]:
    """Classify DataFrame columns into parameter vs metric columns.

    Uses the same keyword heuristic as app.py's ``_parse_optimization_results``.

    Returns
    -------
    tuple[list[str], list[str]]
        (param_columns, metric_columns)
    """
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
    return param_cols, metric_cols


def main(strategy_id: str = None, version_id: str = None, run_mode: int = None, symbol: str = None) -> int:
    """Run the full backtest pipeline.

    Parameters
    ----------
    strategy_id : str, optional
        UUID of the strategy to run. If not provided, uses the first
        strategy in the database.
    version_id : str, optional
        UUID of the specific version to run. If not provided, uses the
        latest version of the selected strategy.

    Returns
    -------
    int
        0 on success, 1 on failure.
    """

    # --- Logging -----------------------------------------------------------
    setup_logging()
    logger.info("=" * 60)
    logger.info("AmiTesting — OLE Backtest Pipeline")
    logger.info("=" * 60)

    # --- Initialise database -----------------------------------------------
    init_db()
    seed_default_strategies()
    seed_param_tooltips()
    seed_indicator_tooltips()

    # --- Validate configuration --------------------------------------------
    if not AMIBROKER_DB_PATH:
        logger.error(
            "AMIBROKER_DB_PATH is not configured. "
            "Please set it in config/settings.py to the full path of your "
            "AmiBroker database directory, e.g.\n"
            '  AMIBROKER_DB_PATH = r"C:\\AmiBroker\\Databases\\GCZ25"'
        )
        return 1

    # --- Resolve strategy & version ----------------------------------------
    if strategy_id is None:
        strategies = list_strategies()
        if not strategies:
            logger.error("No strategies found in database. Cannot run backtest.")
            return 1
        strategy_id = strategies[0]["id"]
        logger.info("Using default strategy: %s (%s)", strategies[0]["name"], strategy_id)

    from scripts.strategy_db import get_strategy, get_version
    strategy = get_strategy(strategy_id)
    if strategy is None:
        logger.error("Strategy not found: %s", strategy_id)
        return 1

    if version_id is None:
        version = get_latest_version(strategy_id)
        if version is None:
            logger.error("No versions found for strategy: %s", strategy_id)
            return 1
        version_id = version["id"]
    else:
        version = get_version(version_id)
        if version is None:
            logger.error("Version not found: %s", version_id)
            return 1

    logger.info("Strategy: %s (v%d — %s)",
                strategy["name"], version["version_number"],
                version.get("label", ""))

    # --- Create run record -------------------------------------------------
    # Use the version's stored AFL content (preferred) so the correct
    # strategy code is executed.  Fall back to the on-disk file for
    # backwards compatibility with the original SMA crossover workflow.
    actual_afl = version.get("afl_content", "")
    if not actual_afl:
        actual_afl = AFL_STRATEGY_FILE.read_text(encoding="utf-8") if AFL_STRATEGY_FILE.exists() else ""

    # Write the AFL to the standard file path so the validator, APX builder,
    # and OLE backtester all operate on the correct strategy code.
    if actual_afl:
        AFL_STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
        AFL_STRATEGY_FILE.write_text(actual_afl, encoding="utf-8")
        logger.info("Wrote version AFL to %s (%d chars)", AFL_STRATEGY_FILE, len(actual_afl))

    effective_symbol = symbol or GCZ25_SYMBOL

    run_id = create_run(
        version_id=version_id,
        strategy_id=strategy_id,
        apx_file=str(APX_OUTPUT),
        afl_content=actual_afl,
        params_json=json.dumps({"run_mode": run_mode or 2}),
        symbol=effective_symbol,
    )
    output_dir = RESULTS_DIR / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Run ID: %s", run_id)
    logger.info("Output dir: %s", output_dir)

    # Mark as running
    update_run(run_id, status="running")

    # --- Optimization progress tracking ------------------------------------
    # For optimization runs (run_mode 4), inject AFL code that writes the
    # current combo counter to a file so the dashboard can display progress.
    is_optimization = (run_mode == 4)
    if is_optimization and actual_afl:
        total_combos = calculate_optimization_combos(actual_afl)
        if total_combos > 0:
            progress_file = str(output_dir / "opt_progress.txt")
            actual_afl = inject_progress_tracker(actual_afl, progress_file)
            AFL_STRATEGY_FILE.write_text(actual_afl, encoding="utf-8")
            logger.info(
                "Injected optimization progress tracker (%d total combos, file=%s)",
                total_combos, progress_file,
            )

            # Write opt_config.json so ole_backtest.py and the dashboard can
            # find the progress file and total combo count.
            opt_config = {
                "total_combos": total_combos,
                "progress_file": progress_file,
                "status_file": str(output_dir / "opt_status.json"),
            }
            (output_dir / "opt_config.json").write_text(
                json.dumps(opt_config, indent=2), encoding="utf-8"
            )
        else:
            logger.info("Optimization run but no Optimize() calls found — skipping tracker injection.")

    # Write a sentinel so the dashboard can discover the run_id early
    # (while the subprocess is still running).
    sentinel_path = RESULTS_DIR / ".current_run_id"
    sentinel_path.write_text(run_id, encoding="utf-8")
    logger.info("Wrote current run sentinel: %s", sentinel_path)

    try:
        # --- Step 1a: Validate AFL (auto-fix known errors) -----------------
        logger.info("Step 1a — Validating AFL ...")
        afl_ok, afl_errors = validate_afl_file(str(AFL_STRATEGY_FILE))
        if not afl_ok:
            logger.warning("AFL validation found issues — attempting auto-fix...")
            afl_text = AFL_STRATEGY_FILE.read_text(encoding="utf-8")
            fixed_afl, fix_changes = auto_fix_afl(afl_text)
            if fix_changes:
                for change in fix_changes:
                    logger.info("Auto-fix: %s", change)
                AFL_STRATEGY_FILE.write_text(fixed_afl, encoding="utf-8")
                logger.info("AFL auto-fixed and saved. Re-validating...")
                afl_ok, afl_errors = validate_afl_file(str(AFL_STRATEGY_FILE))
            if not afl_ok:
                for err in afl_errors:
                    logger.error("AFL validation: %s", err)
                logger.error("AFL validation failed — aborting pipeline.")
                error_metrics = {"error": "AFL validation failed", "validation_errors": afl_errors}
                update_run(run_id, status="failed",
                           metrics_json=json.dumps(error_metrics),
                           completed_at=datetime.now(timezone.utc).isoformat())
                return 1
        logger.info("AFL validation passed.")

        # --- Step 1b: Build .apx file --------------------------------------
        logger.info("Step 1b — Building .apx file ...")

        # Auto-detect periodicity from AFL content.  Strategies that use
        # TimeFrameSet(in1Minute) are designed for tick data compressed to
        # 1-minute bars — they need Periodicity=0 (Tick).  Without this,
        # the session filter and timeframe compression fail silently,
        # producing zero trades.
        afl_text_final = AFL_STRATEGY_FILE.read_text(encoding="utf-8")
        periodicity = None
        if "TimeFrameSet(" in afl_text_final:
            periodicity = 0  # Tick
            logger.info("AFL uses TimeFrameSet — setting Periodicity=0 (Tick)")

        # Use a unique APX filename per run.  AmiBroker caches the formula
        # associated with an APX file across sessions; reusing the same
        # filename (gcz25_test.apx) causes a "formula is different" dialog
        # that blocks COM automation.  A fresh filename avoids this entirely.
        run_apx_path = APX_OUTPUT.parent / f"run_{run_id}.apx"
        apx_path = build_apx(
            str(AFL_STRATEGY_FILE),
            str(run_apx_path),
            str(APX_TEMPLATE),
            run_id=run_id,
            periodicity=periodicity,
            symbol=effective_symbol,
        )
        logger.info("APX file ready: %s", apx_path)

        # --- Step 2: Run OLE backtest --------------------------------------
        logger.info("Step 2 — Running OLE backtest ...")
        backtester = OLEBacktester()
        result = backtester.run_full_test(
            apx_path=str(run_apx_path), output_dir=str(output_dir), run_mode=run_mode
        )
        logger.info("Backtest completed.")

        # --- Step 3: Update run record -------------------------------------
        now = datetime.now(timezone.utc).isoformat()
        trade_count = 0
        is_optimization = (run_mode == 4)
        if result:
            # Compute basic metrics from CSV for the run record
            metrics = {}
            opt_columns_json = None
            opt_total_combos = 0
            csv_path = output_dir / "results.csv"
            if csv_path.exists():
                try:
                    import pandas as pd
                    df = pd.read_csv(csv_path, encoding="utf-8")

                    if is_optimization:
                        # Optimization CSV: one row per parameter combination
                        # with metric columns like "Net Profit", "# Trades", etc.
                        metrics = _parse_optimization_metrics(df)
                        trade_count = metrics.get("combos_tested", 0)
                        opt_total_combos = trade_count
                        opt_columns_json = json.dumps(list(df.columns))

                        # Persist combo detail rows in SQL
                        try:
                            param_cols, metric_cols = _classify_optimization_columns(df)
                            store_optimization_combos(run_id, df, param_cols, metric_cols)
                        except Exception as exc:
                            logger.warning("Failed to store optimization combos in SQL: %s", exc)
                    else:
                        profit_col = None
                        for col in df.columns:
                            if "profit" in col.lower() and "%" not in col.lower():
                                profit_col = col
                                break
                        if profit_col:
                            profits = pd.to_numeric(df[profit_col], errors="coerce").dropna()
                            metrics = {
                                "total_trades": len(df),
                                "total_profit": round(float(profits.sum()), 2),
                                "win_rate": round(float((profits > 0).sum() / len(df) * 100), 1) if len(df) > 0 else 0,
                            }
                        trade_count = metrics.get("total_trades", 0)
                except Exception as exc:
                    logger.warning("Could not compute run metrics: %s", exc)

            if is_optimization:
                metrics["run_mode"] = 4
                if trade_count == 0:
                    logger.warning("Optimization produced ZERO result rows.")
            else:
                # Warn when zero trades are detected
                if trade_count == 0:
                    logger.warning(
                        "ZERO TRADES generated. Possible causes:\n"
                        "  - The data periodicity may not match the strategy "
                        "(tick-based strategies need Periodicity=0).\n"
                        "  - The date range may not contain data for this symbol.\n"
                        "  - The session filter may exclude all bars "
                        "(Asian session = 6PM-3AM EST).\n"
                        "  - Entry conditions (ADX threshold, TEMA crossover) "
                        "may not be met in this data window."
                    )

            update_kwargs = dict(
                status="completed",
                results_csv="results.csv",
                results_html="results.html",
                metrics_json=json.dumps(metrics),
                completed_at=now,
            )
            if is_optimization:
                update_kwargs["is_optimization"] = 1
                update_kwargs["total_combos"] = opt_total_combos
                if opt_columns_json:
                    update_kwargs["columns_json"] = opt_columns_json
            update_run(run_id, **update_kwargs)
            if is_optimization:
                logger.info("Run %s completed — %d optimization combos.", run_id, trade_count)
            else:
                logger.info("Run %s completed — %d trades.", run_id, trade_count)
        else:
            error_msg = "AmiBroker backtest/optimization failed. Check AmiBroker for formula errors or dialog prompts that may have blocked execution."
            update_run(run_id, status="failed",
                       metrics_json=json.dumps({"error": error_msg}),
                       completed_at=now)
            logger.warning("Backtest reported failure.")

        # --- Step 4: Summary -----------------------------------------------
        logger.info("Step 4 — Summary")
        logger.info("-" * 40)

        if result and trade_count > 0:
            logger.info("Backtest SUCCEEDED — %d trades.", trade_count)
        elif result and trade_count == 0:
            logger.warning("Backtest completed but generated ZERO trades.")
        else:
            logger.warning("Backtest reported failure or returned no result.")

        logger.info("Run ID   : %s", run_id)
        logger.info("Results  : %s", output_dir)
        logger.info("=" * 60)

        return 0 if result else 1

    except Exception as exc:
        logger.exception("Pipeline failed with an unhandled exception.")
        update_run(run_id, status="failed",
                   metrics_json=json.dumps({"error": f"Pipeline exception: {str(exc)}"}),
                   completed_at=datetime.now(timezone.utc).isoformat())
        return 1

    finally:
        # Clean up per-run APX file (the snapshot AFL is kept for reference).
        try:
            run_apx_path.unlink(missing_ok=True)
        except (NameError, Exception):
            pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AmiTesting backtest pipeline")
    parser.add_argument("--strategy-id", default=None)
    parser.add_argument("--version-id", default=None)
    parser.add_argument("--run-mode", type=int, default=None)
    parser.add_argument("--symbol", default=None,
                        help="Ticker symbol to backtest against")
    # Support legacy positional args for backwards compatibility
    parser.add_argument("legacy_args", nargs="*", default=[])
    args = parser.parse_args()

    # Fall back to positional args if named args not provided
    sid = args.strategy_id or (args.legacy_args[0] if len(args.legacy_args) > 0 else None)
    vid = args.version_id or (args.legacy_args[1] if len(args.legacy_args) > 1 else None)
    rm = args.run_mode if args.run_mode is not None else (int(args.legacy_args[2]) if len(args.legacy_args) > 2 else None)

    sys.exit(main(strategy_id=sid, version_id=vid, run_mode=rm, symbol=args.symbol))
