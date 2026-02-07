"""
Main entry point for the AmiTesting Sprint 1 pipeline.

Orchestrates the full backtest workflow:
  1. Build the .apx project file from the AFL strategy and template.
  2. Run the OLE-based AmiBroker backtest.
  3. Report results.
"""

import sys
import logging
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
    RESULTS_HTML,
    RESULTS_CSV,
)
from scripts.apx_builder import build_apx
from scripts.ole_backtest import OLEBacktester

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the full Sprint 1 backtest pipeline.

    Returns
    -------
    int
        0 on success, 1 on failure.
    """

    # --- Logging -----------------------------------------------------------
    setup_logging()
    logger.info("=" * 60)
    logger.info("AmiTesting Sprint 1 — OLE Interface Verification")
    logger.info("=" * 60)

    # --- Validate configuration --------------------------------------------
    if not AMIBROKER_DB_PATH:
        logger.error(
            "AMIBROKER_DB_PATH is not configured. "
            "Please set it in config/settings.py to the full path of your "
            "AmiBroker database directory, e.g.\n"
            '  AMIBROKER_DB_PATH = r"C:\\AmiBroker\\Databases\\GCZ25"'
        )
        return 1

    try:
        # --- Step 1: Build .apx file ---------------------------------------
        logger.info("Step 1 — Building .apx file ...")
        apx_path = build_apx(
            str(AFL_STRATEGY_FILE),
            str(APX_OUTPUT),
            str(APX_TEMPLATE),
        )
        logger.info("APX file ready: %s", apx_path)

        # --- Step 2: Run OLE backtest --------------------------------------
        logger.info("Step 2 — Running OLE backtest ...")
        backtester = OLEBacktester()
        result = backtester.run_full_test()
        logger.info("Backtest completed.")

        # --- Step 3: Summary -----------------------------------------------
        logger.info("Step 3 — Summary")
        logger.info("-" * 40)

        if result:
            logger.info("Backtest SUCCEEDED.")
        else:
            logger.warning("Backtest reported failure or returned no result.")

        logger.info("Result files:")
        logger.info("  HTML : %s", RESULTS_HTML)
        logger.info("  CSV  : %s", RESULTS_CSV)
        logger.info("=" * 60)

        return 0 if result else 1

    except Exception:
        logger.exception("Pipeline failed with an unhandled exception.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
