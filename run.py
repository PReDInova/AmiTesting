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
)
from scripts.afl_validator import validate_afl_file
from scripts.apx_builder import build_apx
from scripts.ole_backtest import OLEBacktester
from scripts.strategy_db import (
    init_db,
    seed_default_strategies,
    list_strategies,
    get_latest_version,
    create_run,
    update_run,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(strategy_id: str = None, version_id: str = None) -> int:
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
    run_id = create_run(
        version_id=version_id,
        strategy_id=strategy_id,
        apx_file=str(APX_OUTPUT),
    )
    output_dir = RESULTS_DIR / run_id
    logger.info("Run ID: %s", run_id)
    logger.info("Output dir: %s", output_dir)

    # Mark as running
    update_run(run_id, status="running")

    try:
        # --- Step 1a: Validate AFL -----------------------------------------
        logger.info("Step 1a — Validating AFL ...")
        afl_ok, afl_errors = validate_afl_file(str(AFL_STRATEGY_FILE))
        if not afl_ok:
            for err in afl_errors:
                logger.error("AFL validation: %s", err)
            logger.error("AFL validation failed — aborting pipeline.")
            update_run(run_id, status="failed",
                       completed_at=datetime.now(timezone.utc).isoformat())
            return 1
        logger.info("AFL validation passed.")

        # --- Step 1b: Build .apx file --------------------------------------
        logger.info("Step 1b — Building .apx file ...")
        apx_path = build_apx(
            str(AFL_STRATEGY_FILE),
            str(APX_OUTPUT),
            str(APX_TEMPLATE),
        )
        logger.info("APX file ready: %s", apx_path)

        # --- Step 2: Run OLE backtest --------------------------------------
        logger.info("Step 2 — Running OLE backtest ...")
        backtester = OLEBacktester()
        result = backtester.run_full_test(output_dir=str(output_dir))
        logger.info("Backtest completed.")

        # --- Step 3: Update run record -------------------------------------
        now = datetime.now(timezone.utc).isoformat()
        if result:
            # Compute basic metrics from CSV for the run record
            metrics = {}
            csv_path = output_dir / "results.csv"
            if csv_path.exists():
                try:
                    import pandas as pd
                    df = pd.read_csv(csv_path, encoding="utf-8")
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
                except Exception as exc:
                    logger.warning("Could not compute run metrics: %s", exc)

            update_run(
                run_id,
                status="completed",
                results_csv="results.csv",
                results_html="results.html",
                metrics_json=json.dumps(metrics),
                completed_at=now,
            )
            logger.info("Run %s completed successfully.", run_id)
        else:
            update_run(run_id, status="failed", completed_at=now)
            logger.warning("Backtest reported failure.")

        # --- Step 4: Summary -----------------------------------------------
        logger.info("Step 4 — Summary")
        logger.info("-" * 40)

        if result:
            logger.info("Backtest SUCCEEDED.")
        else:
            logger.warning("Backtest reported failure or returned no result.")

        logger.info("Run ID   : %s", run_id)
        logger.info("Results  : %s", output_dir)
        logger.info("=" * 60)

        return 0 if result else 1

    except Exception:
        logger.exception("Pipeline failed with an unhandled exception.")
        update_run(run_id, status="failed",
                   completed_at=datetime.now(timezone.utc).isoformat())
        return 1


if __name__ == "__main__":
    # Support optional CLI arguments: run.py [strategy_id] [version_id]
    sid = sys.argv[1] if len(sys.argv) > 1 else None
    vid = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(main(strategy_id=sid, version_id=vid))
