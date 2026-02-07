"""
OLE Backtest Script -- Sprint 1 Core Deliverable

Uses COM/OLE Automation to control AmiBroker: connects to the application,
loads a database, runs a backtest via an .apx project file, and exports
the results to HTML and CSV.
"""

import logging
import sys
import time
from pathlib import Path

import win32com.client

# ---------------------------------------------------------------------------
# Import project-wide configuration from the parent config package
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import *  # noqa: E402, F403

logger = logging.getLogger(__name__)


class OLEBacktester:
    """Drive AmiBroker through its COM/OLE interface."""

    def __init__(self) -> None:
        setup_logging()
        self.ab = None
        logger.info("OLEBacktester initialised.")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Dispatch the AmiBroker COM object and make it visible.

        Returns True on success, False on failure.
        """
        try:
            logger.info("Connecting to AmiBroker via COM (%s) ...", AMIBROKER_EXE)
            self.ab = win32com.client.Dispatch(AMIBROKER_EXE)
            self.ab.Visible = 1
            logger.info("Connected to AmiBroker successfully. Application is visible.")
            return True
        except Exception as exc:
            logger.error("Failed to connect to AmiBroker: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def load_database(self, db_path: str = None) -> bool:
        """Load an AmiBroker database.

        Parameters
        ----------
        db_path : str, optional
            Full path to the database directory.  Falls back to
            ``AMIBROKER_DB_PATH`` from settings if not provided.

        Returns True on success, False on failure.
        """
        path = db_path or AMIBROKER_DB_PATH
        try:
            logger.info("Loading database: %s", path)
            self.ab.LoadDatabase(path)
            logger.info("Database loaded successfully.")
            return True
        except Exception as exc:
            logger.error("Failed to load database '%s': %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # Backtest execution
    # ------------------------------------------------------------------

    def run_backtest(self, apx_path: str = None) -> bool:
        """Open an .apx project, run the backtest, export results.

        Parameters
        ----------
        apx_path : str, optional
            Path to the .apx file.  Falls back to ``APX_OUTPUT`` from
            settings if not provided.

        Returns True on success, False on timeout/failure.
        """
        target_apx = apx_path or str(APX_OUTPUT)
        poll_interval = BACKTEST_SETTINGS["poll_interval"]
        max_wait = BACKTEST_SETTINGS["max_wait"]
        run_mode = BACKTEST_SETTINGS["run_mode"]

        analysis_doc = None
        try:
            # --- Open the analysis project ---
            logger.info("Opening analysis project: %s", target_apx)
            analysis_doc = self.ab.AnalysisDocs.Open(target_apx)
            if analysis_doc is None:
                logger.error("AnalysisDocs.Open returned None for '%s'.", target_apx)
                return False
            logger.info("Analysis project opened successfully.")

            # --- Kick off the backtest ---
            logger.info("Starting backtest (run_mode=%d) ...", run_mode)
            analysis_doc.Run(run_mode)

            # --- Poll until finished or timed out ---
            elapsed = 0.0
            while analysis_doc.IsBusy:
                if elapsed >= max_wait:
                    logger.error(
                        "Backtest timed out after %.1f seconds (max_wait=%d).",
                        elapsed,
                        max_wait,
                    )
                    return False
                logger.info(
                    "Waiting for backtest to complete... (%.1fs elapsed)", elapsed
                )
                time.sleep(poll_interval)
                elapsed += poll_interval

            logger.info("Backtest completed in %.1f seconds.", elapsed)

            # --- Export results ---
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)

            html_path = str(RESULTS_HTML)
            csv_path = str(RESULTS_CSV)

            logger.info("Exporting HTML results to: %s", html_path)
            analysis_doc.Export(html_path)

            logger.info("Exporting CSV results to: %s", csv_path)
            analysis_doc.Export(csv_path)

            # Log file sizes if the exports landed on disk
            for label, fpath in [("HTML", RESULTS_HTML), ("CSV", RESULTS_CSV)]:
                if fpath.exists():
                    size_kb = fpath.stat().st_size / 1024
                    logger.info(
                        "%s export OK: %s (%.1f KB)", label, fpath, size_kb
                    )
                else:
                    logger.warning("%s export file not found: %s", label, fpath)

            return True

        except Exception as exc:
            logger.error("Backtest execution failed: %s", exc)
            return False

        finally:
            if analysis_doc is not None:
                try:
                    analysis_doc.Close()
                    logger.info("Analysis document closed.")
                except Exception as close_exc:
                    logger.warning(
                        "Could not close analysis document: %s", close_exc
                    )

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Quit the AmiBroker COM application (if connected)."""
        if self.ab is not None:
            try:
                self.ab.Quit()
                logger.info("AmiBroker disconnected (Quit sent).")
            except Exception as exc:
                logger.warning("Error while quitting AmiBroker: %s", exc)
            finally:
                self.ab = None

    # ------------------------------------------------------------------
    # Full orchestration
    # ------------------------------------------------------------------

    def run_full_test(self, db_path: str = None, apx_path: str = None) -> bool:
        """Orchestrate the complete workflow: connect, load, backtest, disconnect.

        Returns True when every step succeeds.
        """
        try:
            if not self.connect():
                return False

            if not self.load_database(db_path):
                return False

            if not self.run_backtest(apx_path):
                return False

            return True

        finally:
            self.disconnect()


# ======================================================================
# Entry point
# ======================================================================

if __name__ == "__main__":
    backtester = OLEBacktester()

    # --- Pre-flight checks ---
    if not AMIBROKER_DB_PATH:
        print(
            "[ERROR] AMIBROKER_DB_PATH is not set in config/settings.py.\n"
            "        Please set it to the full path of your AmiBroker database\n"
            "        directory before running this script."
        )
        sys.exit(1)

    apx_file = Path(APX_OUTPUT)
    if not apx_file.exists():
        print(
            f"[ERROR] APX file not found: {APX_OUTPUT}\n"
            "        Run scripts/apx_builder.py first to generate the .apx project."
        )
        sys.exit(1)

    # --- Run ---
    success = backtester.run_full_test()

    # --- Summary ---
    print("\n" + "=" * 60)
    if success:
        print("BACKTEST COMPLETED SUCCESSFULLY")
        print(f"  HTML results : {RESULTS_HTML}")
        print(f"  CSV  results : {RESULTS_CSV}")
        for label, fpath in [("HTML", RESULTS_HTML), ("CSV", RESULTS_CSV)]:
            if fpath.exists():
                size_kb = fpath.stat().st_size / 1024
                print(f"    -> {label} file size: {size_kb:.1f} KB")
            else:
                print(f"    -> {label} file NOT found on disk.")
    else:
        print("BACKTEST FAILED -- check logs for details.")
        print(f"  Log file: {LOG_FILE}")
    print("=" * 60)

    sys.exit(0 if success else 1)
