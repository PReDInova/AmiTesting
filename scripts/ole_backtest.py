"""
OLE Backtest Script -- Sprint 1 Core Deliverable

Uses COM/OLE Automation to control AmiBroker: connects to the application,
loads a database, runs a backtest via an .apx project file, and exports
the results to HTML and CSV.
"""

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pythoncom
import win32com.client

from scripts.dialog_handler import DialogHandler

# ---------------------------------------------------------------------------
# Import project-wide configuration from the parent config package
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import *  # noqa: E402, F403

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------------

def list_symbols(db_path: str = None) -> list:
    """Enumerate all symbols in an AmiBroker database via COM automation.

    Parameters
    ----------
    db_path : str, optional
        Full path to the database directory.  Falls back to
        ``AMIBROKER_DB_PATH`` from settings if not provided.

    Returns
    -------
    list[str]
        Sorted list of ticker symbols.  Returns an empty list when
        AmiBroker is not available or any COM error occurs.
    """
    symbols = []
    try:
        pythoncom.CoInitialize()
        ab = win32com.client.Dispatch(AMIBROKER_EXE)
        ab.LoadDatabase(db_path or AMIBROKER_DB_PATH)
        for i in range(ab.Stocks.Count):
            symbols.append(ab.Stocks(i).Ticker)
    except Exception as exc:
        logger.error("list_symbols failed: %s", exc)
        return []
    finally:
        pythoncom.CoUninitialize()
    return sorted(symbols)


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
        """Launch the correct AmiBroker executable and connect via COM.

        Starts the AmiBroker instance at ``AMIBROKER_EXE_PATH`` before
        dispatching the COM object, ensuring the right installation is used
        when multiple versions are present on the system.

        Returns True on success, False on failure.
        """
        try:
            exe_path = AMIBROKER_EXE_PATH
            logger.info("Launching AmiBroker from: %s", exe_path)
            subprocess.Popen([exe_path])
            time.sleep(2)  # give AmiBroker time to start and register COM

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
    # APX / AFL Validation
    # ------------------------------------------------------------------

    def validate_apx(self, apx_path: str = None) -> tuple:
        """Validate an APX file by attempting to open it in AmiBroker.

        Must be connected first (call ``connect()`` and ``load_database()``).

        Returns
        -------
        tuple of (bool, str)
            ``(True, "success message")`` when AmiBroker can open the file,
            ``(False, "reason")`` when it cannot.
        """
        target = apx_path or str(APX_OUTPUT)
        logger.info("Validating APX file: %s", target)

        if not Path(target).exists():
            msg = f"APX file does not exist: {target}"
            logger.error(msg)
            return (False, msg)

        analysis_doc = None
        try:
            analysis_doc = self.ab.AnalysisDocs.Open(target)
            if analysis_doc is None:
                msg = (
                    f"AmiBroker rejected '{target}'. "
                    "The file may have incorrect XML format, unsupported "
                    "elements, or encoding issues."
                )
                logger.error(msg)
                return (False, msg)

            logger.info("APX validation passed: %s", target)
            return (True, f"APX file validated successfully: {target}")

        except Exception as exc:
            msg = f"APX validation failed with COM error: {exc}"
            logger.error(msg)
            return (False, msg)

        finally:
            if analysis_doc is not None:
                try:
                    analysis_doc.Close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Backtest execution
    # ------------------------------------------------------------------

    def run_backtest(self, apx_path: str = None, output_dir: str = None, run_mode: int = None) -> bool:
        """Open an .apx project, run the backtest, export results.

        Parameters
        ----------
        apx_path : str, optional
            Path to the .apx file.  Falls back to ``APX_OUTPUT`` from
            settings if not provided.
        output_dir : str, optional
            Directory to export results into.  Falls back to ``RESULTS_DIR``
            from settings if not provided.  When a GUID-based run is active,
            this will be ``results/<run_uuid>/``.

        Returns True on success, False on timeout/failure.
        """
        target_apx = apx_path or str(APX_OUTPUT)
        poll_interval = BACKTEST_SETTINGS["poll_interval"]
        max_wait = BACKTEST_SETTINGS["max_wait"]
        run_mode = run_mode if run_mode is not None else BACKTEST_SETTINGS["run_mode"]

        # Determine output paths
        if output_dir:
            out = Path(output_dir)
        else:
            out = RESULTS_DIR
        out.mkdir(parents=True, exist_ok=True)

        html_path = str(out / "results.html")
        csv_path = str(out / "results.csv")

        # --- Load optimization config if present ---
        opt_config = None
        opt_config_path = out / "opt_config.json"
        if opt_config_path.exists():
            try:
                opt_config = json.loads(opt_config_path.read_text(encoding="utf-8"))
                logger.info(
                    "Loaded opt_config: %d total combos", opt_config.get("total_combos", 0)
                )
            except Exception as exc:
                logger.warning("Could not load opt_config.json: %s", exc)

        analysis_doc = None
        dialog_handler = DialogHandler()
        try:
            # Start monitoring for blocking dialogs BEFORE opening the APX.
            # AmiBroker may show modal dialogs (e.g. "formula is different")
            # that block the COM call; the handler dismisses them automatically.
            dialog_handler.start()

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
            start_time = time.monotonic()
            while analysis_doc.IsBusy:
                if elapsed >= max_wait:
                    logger.error(
                        "Backtest timed out after %.1f seconds (max_wait=%d).",
                        elapsed,
                        max_wait,
                    )
                    return False

                # -- Optimization progress tracking --
                if opt_config:
                    self._update_opt_progress(opt_config, start_time)

                    # Check for abort request
                    abort_path = out / "abort_requested"
                    if abort_path.exists():
                        logger.warning("Abort requested — calling analysis_doc.Abort()")
                        try:
                            analysis_doc.Abort()
                        except Exception as abort_exc:
                            logger.warning("Abort() call failed: %s", abort_exc)
                        # Wait a moment for AmiBroker to finish aborting
                        time.sleep(2)
                        return False

                logger.info(
                    "Waiting for backtest to complete... (%.1fs elapsed)", elapsed
                )
                time.sleep(poll_interval)
                elapsed = time.monotonic() - start_time

            logger.info("Backtest completed in %.1f seconds.", elapsed)

            # Write final progress update
            if opt_config:
                self._update_opt_progress(opt_config, start_time)

            # --- Export results ---
            # Export directly in the main thread.  COM objects cannot be
            # used across threads without marshaling, which causes
            # "<unknown>.Export" errors.  The dialog handler is already
            # running to dismiss blocking dialogs, and the parent process
            # has a timeout as a safety net against hangs.
            for label, fpath in [("HTML", html_path), ("CSV", csv_path)]:
                logger.info("Exporting %s results to: %s", label, fpath)
                try:
                    analysis_doc.Export(fpath)
                except Exception as exc:
                    logger.error("%s export failed: %s", label, exc)

            # Log file sizes if the exports landed on disk
            for label, fpath_str in [("HTML", html_path), ("CSV", csv_path)]:
                fpath = Path(fpath_str)
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
            dialog_handler.stop()
            if analysis_doc is not None:
                try:
                    analysis_doc.Close()
                    logger.info("Analysis document closed.")
                except Exception as close_exc:
                    logger.warning(
                        "Could not close analysis document: %s", close_exc
                    )

    # ------------------------------------------------------------------
    # Optimization progress helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _update_opt_progress(opt_config: dict, start_time: float) -> None:
        """Read the AFL-written combo counter and write opt_status.json."""
        progress_file = opt_config.get("progress_file", "")
        status_file = opt_config.get("status_file", "")
        total = opt_config.get("total_combos", 0)

        combo = 0
        if progress_file:
            try:
                raw = Path(progress_file).read_text(encoding="utf-8").strip()
                combo = int(float(raw))
            except (OSError, ValueError):
                pass  # file not yet created or mid-write

        elapsed = time.monotonic() - start_time
        rate = combo / elapsed if elapsed > 0 else 0
        pct = (combo / total * 100) if total > 0 else 0
        eta_seconds = ((total - combo) / rate) if rate > 0 else 0

        status = {
            "combo": combo,
            "total": total,
            "elapsed": round(elapsed, 1),
            "pct": round(pct, 1),
            "eta_seconds": round(eta_seconds, 1),
            "rate": round(rate, 2),
        }

        if status_file:
            try:
                Path(status_file).write_text(
                    json.dumps(status), encoding="utf-8"
                )
            except OSError:
                pass

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

    def run_full_test(self, db_path: str = None, apx_path: str = None, output_dir: str = None, run_mode: int = None) -> bool:
        """Orchestrate the complete workflow: connect, load, validate, backtest, disconnect.

        Parameters
        ----------
        output_dir : str, optional
            Directory to export results into (e.g. ``results/<run_uuid>/``).

        Returns True when every step succeeds.
        """
        try:
            if not self.connect():
                return False

            if not self.load_database(db_path):
                return False

            # Skip separate validate_apx() — opening the APX twice (validate
            # then backtest) causes Run() to block on the second open.
            # run_backtest() handles None/open failures internally.
            if not self.run_backtest(apx_path, output_dir=output_dir, run_mode=run_mode):
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
