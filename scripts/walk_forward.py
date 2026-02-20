"""
Walk-Forward Analysis for Strategy Validation.

Splits historical data into multiple in-sample (IS) / out-of-sample (OOS)
windows and runs the backtest pipeline on each.  This provides a robust
estimate of strategy performance by testing on data the optimiser never saw.

Two windowing modes are supported:

* **Anchored** -- The IS window grows from a fixed start date while the OOS
  window slides forward.  This mirrors a practitioner who re-optimises with
  all available history before each live period.

* **Rolling** -- Both the IS and OOS windows slide forward with a fixed IS
  length.  This tests parameter stability under regime change.

Usage::

    from scripts.walk_forward import WalkForwardAnalyzer

    wfa = WalkForwardAnalyzer(
        strategy_id="<uuid>",
        version_id="<uuid>",
        symbol="GC",
        total_period="2y",
        in_sample_pct=0.70,
        num_windows=5,
        optimization_mode=4,
    )
    summary = wfa.run()
"""

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from dateutil.relativedelta import relativedelta

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.settings import (
    AMIBROKER_DB_PATH,
    AFL_STRATEGY_FILE,
    APX_OUTPUT,
    APX_TEMPLATE,
    DEFAULT_SYMBOL,
    RESULTS_DIR,
    setup_logging,
)
from scripts.strategy_db import (
    init_db,
    get_strategy,
    get_version,
    get_latest_version,
    create_run,
    update_run,
    get_run,
    get_optimization_combos,
    create_walk_forward_run,
    get_walk_forward_results,
    _get_connection,
    _new_uuid,
)
from scripts.apx_builder import build_apx, _compute_date_range

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Period parsing helper
# ---------------------------------------------------------------------------

_PERIOD_MONTHS = {
    "1m": 1, "2m": 2, "3m": 3, "6m": 6, "9m": 9,
    "1y": 12, "2y": 24, "3y": 36, "5y": 60,
}


def _period_to_months(code: str) -> int:
    """Convert a period code like '2y' or '6m' to months.

    Supports codes such as ``1m``, ``3m``, ``6m``, ``1y``, ``2y``, ``3y``,
    ``5y``.  Falls back to 12 months for unrecognised codes.
    """
    return _PERIOD_MONTHS.get(code.lower().strip(), 12)


# ---------------------------------------------------------------------------
# Walk-Forward Analyzer
# ---------------------------------------------------------------------------

class WalkForwardAnalyzer:
    """Orchestrates walk-forward analysis across multiple IS/OOS windows.

    Parameters
    ----------
    strategy_id : str
        UUID of the strategy to analyse.
    version_id : str
        UUID of the specific strategy version.
    symbol : str
        Ticker symbol to run against.
    total_period : str
        Total lookback period code (e.g. ``'2y'``).  The analysis will span
        this many months of data counting back from the dataset end.
    in_sample_pct : float
        Fraction of each window devoted to in-sample optimisation
        (0 < in_sample_pct < 1).
    num_windows : int
        Number of IS/OOS window pairs to generate.
    optimization_mode : int
        AmiBroker run mode for the IS leg.  4 = exhaustive optimisation.
    anchored : bool
        If ``True`` use anchored windowing (IS grows); if ``False`` use
        rolling windows (fixed IS length).
    """

    def __init__(
        self,
        strategy_id: str,
        version_id: str,
        symbol: str = "",
        total_period: str = "2y",
        in_sample_pct: float = 0.70,
        num_windows: int = 5,
        optimization_mode: int = 4,
        anchored: bool = False,
    ):
        if not (0.0 < in_sample_pct < 1.0):
            raise ValueError(f"in_sample_pct must be between 0 and 1, got {in_sample_pct}")
        if num_windows < 1:
            raise ValueError(f"num_windows must be >= 1, got {num_windows}")

        self.strategy_id = strategy_id
        self.version_id = version_id
        self.symbol = symbol or DEFAULT_SYMBOL
        self.total_period = total_period
        self.in_sample_pct = in_sample_pct
        self.num_windows = num_windows
        self.optimization_mode = optimization_mode
        self.anchored = anchored

        # Populated during run()
        self._wf_run_id: Optional[str] = None
        self._strategy: Optional[dict] = None
        self._version: Optional[dict] = None

    # ------------------------------------------------------------------
    # Window generation
    # ------------------------------------------------------------------

    def generate_windows(
        self,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """Split a date range into IS/OOS window pairs.

        Parameters
        ----------
        start_date : date
            First date of the analysis period.
        end_date : date
            Last date of the analysis period.

        Returns
        -------
        list[dict]
            Each dict contains:
            ``window_num``, ``is_start``, ``is_end``, ``oos_start``,
            ``oos_end``.
        """
        total_days = (end_date - start_date).days
        if total_days <= 0:
            raise ValueError(
                f"end_date ({end_date}) must be after start_date ({start_date})"
            )

        windows: list[dict] = []

        if self.anchored:
            # Anchored: IS starts at start_date and grows; OOS slides forward.
            # Total OOS region = (1 - is_sample_pct) * total_days.
            # Each OOS slice = oos_total / num_windows.
            oos_total_days = int(total_days * (1.0 - self.in_sample_pct))
            oos_slice_days = max(1, oos_total_days // self.num_windows)
            # First IS ends where the first OOS begins.
            first_is_days = total_days - oos_total_days

            for i in range(self.num_windows):
                oos_start = start_date + relativedelta(days=first_is_days + i * oos_slice_days)
                oos_end = start_date + relativedelta(days=first_is_days + (i + 1) * oos_slice_days)
                # Clamp final window to end_date
                if i == self.num_windows - 1:
                    oos_end = end_date

                # IS always starts at the anchor; ends just before OOS.
                is_start = start_date
                is_end = oos_start - relativedelta(days=1)

                windows.append({
                    "window_num": i + 1,
                    "is_start": is_start,
                    "is_end": is_end,
                    "oos_start": oos_start,
                    "oos_end": oos_end,
                })
        else:
            # Rolling: fixed-length IS and OOS that slide forward together.
            step_days = max(1, total_days // self.num_windows)
            is_days = int(step_days * self.in_sample_pct)
            oos_days = step_days - is_days

            for i in range(self.num_windows):
                is_start = start_date + relativedelta(days=i * step_days)
                is_end = is_start + relativedelta(days=is_days - 1)
                oos_start = is_end + relativedelta(days=1)
                oos_end = oos_start + relativedelta(days=oos_days - 1)

                # Clamp final window to end_date
                if i == self.num_windows - 1:
                    oos_end = min(oos_end, end_date)

                # Skip degenerate windows
                if is_start >= end_date:
                    break

                windows.append({
                    "window_num": i + 1,
                    "is_start": is_start,
                    "is_end": min(is_end, end_date),
                    "oos_start": min(oos_start, end_date),
                    "oos_end": oos_end,
                })

        logger.info(
            "Generated %d %s windows from %s to %s",
            len(windows),
            "anchored" if self.anchored else "rolling",
            start_date,
            end_date,
        )
        return windows

    # ------------------------------------------------------------------
    # Run the full walk-forward analysis
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Execute walk-forward analysis across all windows.

        Workflow for each window:

        1. Run an optimisation (``run_mode=4``) on the IS period to find the
           best parameter set.
        2. Run a standard backtest (``run_mode=2``) on the OOS period using
           those best parameters.
        3. Collect IS and OOS metrics for comparison.

        Returns
        -------
        dict
            Summary containing ``wf_run_id``, per-window results, and
            aggregate IS-vs-OOS metric comparison.
        """
        setup_logging()
        init_db()

        # Resolve strategy and version
        self._strategy = get_strategy(self.strategy_id)
        if self._strategy is None:
            raise ValueError(f"Strategy not found: {self.strategy_id}")

        self._version = get_version(self.version_id)
        if self._version is None:
            raise ValueError(f"Version not found: {self.version_id}")

        logger.info("=" * 60)
        logger.info("Walk-Forward Analysis")
        logger.info("  Strategy : %s (v%d)",
                     self._strategy["name"],
                     self._version["version_number"])
        logger.info("  Symbol   : %s", self.symbol)
        logger.info("  Period   : %s  |  IS%%: %.0f%%  |  Windows: %d",
                     self.total_period, self.in_sample_pct * 100, self.num_windows)
        logger.info("  Mode     : %s", "Anchored" if self.anchored else "Rolling")
        logger.info("=" * 60)

        # Determine the analysis date range.  We need to connect to AmiBroker
        # briefly to query dataset boundaries, then compute the total window.
        dataset_start, dataset_end = self._query_dataset_dates()
        if dataset_start is None or dataset_end is None:
            raise RuntimeError(
                "Could not determine dataset date range.  Ensure AmiBroker "
                "is running and the database contains data for the symbol."
            )

        total_months = _period_to_months(self.total_period)
        analysis_end = datetime.strptime(dataset_end, "%Y-%m-%d").date()
        analysis_start = analysis_end - relativedelta(months=total_months)
        ds_start = datetime.strptime(dataset_start, "%Y-%m-%d").date()
        analysis_start = max(analysis_start, ds_start)

        logger.info(
            "Analysis window: %s to %s (dataset: %s to %s)",
            analysis_start, analysis_end, dataset_start, dataset_end,
        )

        windows = self.generate_windows(analysis_start, analysis_end)
        if not windows:
            raise RuntimeError("No valid windows could be generated.")

        # Create a walk-forward run record in the database
        self._wf_run_id = create_walk_forward_run(
            strategy_id=self.strategy_id,
            version_id=self.version_id,
            symbol=self.symbol,
            total_period=self.total_period,
            in_sample_pct=self.in_sample_pct,
            num_windows=self.num_windows,
            anchored=self.anchored,
            windows_json=json.dumps(
                [_window_to_serializable(w) for w in windows]
            ),
        )
        logger.info("Walk-forward run ID: %s", self._wf_run_id)

        # Execute each window
        window_results: list[dict] = []
        for window in windows:
            logger.info("-" * 40)
            logger.info(
                "Window %d/%d  |  IS: %s to %s  |  OOS: %s to %s",
                window["window_num"], len(windows),
                window["is_start"], window["is_end"],
                window["oos_start"], window["oos_end"],
            )
            try:
                result = self._run_window(window)
                window_results.append(result)
            except Exception:
                logger.exception(
                    "Window %d failed with an exception.", window["window_num"]
                )
                window_results.append({
                    "window_num": window["window_num"],
                    "status": "failed",
                    "error": "Exception during window execution",
                    "is_metrics": {},
                    "oos_metrics": {},
                    "best_params": {},
                })

        # Build summary
        summary = self._build_summary(window_results)

        # Persist results
        _update_walk_forward_run(
            self._wf_run_id,
            status="completed",
            results_json=json.dumps(summary, default=str),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

        logger.info("=" * 60)
        logger.info("Walk-Forward Analysis Complete")
        logger.info("  Run ID         : %s", self._wf_run_id)
        logger.info("  Windows passed : %d / %d",
                     sum(1 for r in window_results if r.get("status") == "completed"),
                     len(window_results))
        if summary.get("oos_aggregate"):
            agg = summary["oos_aggregate"]
            logger.info("  OOS net profit : %.2f", agg.get("total_net_profit", 0))
            logger.info("  OOS avg trades : %.1f", agg.get("avg_trades", 0))
        logger.info("=" * 60)

        summary["wf_run_id"] = self._wf_run_id
        return summary

    # ------------------------------------------------------------------
    # Single window execution
    # ------------------------------------------------------------------

    def _run_window(self, window: dict) -> dict:
        """Run a single IS/OOS window pair.

        Steps:
        1. Run optimisation on the IS period.
        2. Extract best parameter set from the IS optimisation results.
        3. Run a backtest with those params on the OOS period.
        4. Return combined metrics.

        Parameters
        ----------
        window : dict
            Window definition from :meth:`generate_windows`.

        Returns
        -------
        dict
            Per-window results including IS metrics, OOS metrics, best
            params, and run IDs.
        """
        result = {
            "window_num": window["window_num"],
            "is_start": str(window["is_start"]),
            "is_end": str(window["is_end"]),
            "oos_start": str(window["oos_start"]),
            "oos_end": str(window["oos_end"]),
            "status": "pending",
            "is_run_id": None,
            "oos_run_id": None,
            "is_metrics": {},
            "oos_metrics": {},
            "best_params": {},
        }

        # --- Step 1: IS Optimisation ------------------------------------------
        logger.info("  [IS] Running optimisation (%s to %s) ...",
                     window["is_start"], window["is_end"])

        is_date_range = _dates_to_range_code(window["is_start"], window["is_end"])
        try:
            is_run_id = self._execute_backtest(
                date_from=str(window["is_start"]),
                date_to=str(window["is_end"]),
                run_mode=self.optimization_mode,
                label=f"WF-W{window['window_num']}-IS",
            )
            result["is_run_id"] = is_run_id
        except Exception:
            logger.exception("  [IS] Optimisation failed.")
            result["status"] = "is_failed"
            return result

        # Fetch IS run metrics
        is_run = get_run(is_run_id)
        if is_run:
            result["is_metrics"] = is_run.get("metrics", {})

        # --- Step 2: Extract best parameters from IS --------------------------
        best_params = self._extract_best_params(is_run_id)
        result["best_params"] = best_params
        if not best_params:
            logger.warning("  [IS] No best parameters found.  Skipping OOS.")
            result["status"] = "no_params"
            return result

        logger.info("  [IS] Best params: %s", best_params)

        # --- Step 3: OOS Backtest with best params ----------------------------
        logger.info("  [OOS] Running backtest (%s to %s) ...",
                     window["oos_start"], window["oos_end"])

        try:
            oos_run_id = self._execute_backtest(
                date_from=str(window["oos_start"]),
                date_to=str(window["oos_end"]),
                run_mode=2,  # standard backtest
                label=f"WF-W{window['window_num']}-OOS",
                param_overrides=best_params,
            )
            result["oos_run_id"] = oos_run_id
        except Exception:
            logger.exception("  [OOS] Backtest failed.")
            result["status"] = "oos_failed"
            return result

        # Fetch OOS run metrics
        oos_run = get_run(oos_run_id)
        if oos_run:
            result["oos_metrics"] = oos_run.get("metrics", {})

        result["status"] = "completed"
        return result

    # ------------------------------------------------------------------
    # Fetch stored results
    # ------------------------------------------------------------------

    def get_results(self, wf_run_id: str) -> dict:
        """Fetch stored walk-forward results by run ID.

        Parameters
        ----------
        wf_run_id : str
            UUID of the walk-forward run.

        Returns
        -------
        dict
            The walk-forward run record including parsed results.
        """
        return get_walk_forward_results(wf_run_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query_dataset_dates(self) -> tuple[Optional[str], Optional[str]]:
        """Query AmiBroker via COM for the dataset start/end dates.

        Returns
        -------
        tuple[str | None, str | None]
            ``(start_date, end_date)`` as ``'YYYY-MM-DD'`` strings, or
            ``(None, None)`` if the query fails.
        """
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            try:
                ab = win32com.client.Dispatch("Broker.Application")
                ab.LoadDatabase(AMIBROKER_DB_PATH)

                from scripts.ole_stock_data import _com_date_to_datetime

                stock = ab.Stocks(self.symbol)
                if stock is None:
                    logger.warning(
                        "Symbol '%s' not found in database.", self.symbol
                    )
                    return None, None

                quotations = stock.Quotations
                count = quotations.Count
                if count == 0:
                    return None, None

                first_dt = _com_date_to_datetime(quotations(0).Date)
                last_dt = _com_date_to_datetime(quotations(count - 1).Date)
                return (
                    first_dt.strftime("%Y-%m-%d"),
                    last_dt.strftime("%Y-%m-%d"),
                )
            finally:
                pythoncom.CoUninitialize()
        except Exception as exc:
            logger.warning("Failed to query dataset dates: %s", exc)
            return None, None

    def _execute_backtest(
        self,
        date_from: str,
        date_to: str,
        run_mode: int,
        label: str = "",
        param_overrides: Optional[dict] = None,
    ) -> str:
        """Run a single backtest via the pipeline and return the run_id.

        This delegates to ``run.main()`` after configuring the AFL and date
        range.  For OOS runs with ``param_overrides``, the AFL Optimize()
        calls are replaced with fixed values before execution.

        Parameters
        ----------
        date_from : str
            Start date ``'YYYY-MM-DD'``.
        date_to : str
            End date ``'YYYY-MM-DD'``.
        run_mode : int
            AmiBroker run mode (2=backtest, 4=optimisation).
        label : str
            Human-readable label for logging.
        param_overrides : dict, optional
            For OOS runs, a mapping of parameter names to fixed values
            that replace Optimize() calls in the AFL.

        Returns
        -------
        str
            The ``run_id`` UUID of the created backtest run.
        """
        # Build a compound date range code for the apx_builder.
        # We compute the offset from dataset start to date_from, then
        # the duration from date_from to date_to, and encode as an
        # explicit from/to pair passed directly into the pipeline.
        #
        # The simplest approach: call run.main() with the right date_range.
        # However, run.main() expects period codes, not absolute dates.
        # Instead, we drive the pipeline components directly for maximum
        # control.

        from scripts.ole_backtest import OLEBacktester

        version = self._version
        afl_content = version.get("afl_content", "")
        if not afl_content:
            afl_content = AFL_STRATEGY_FILE.read_text(encoding="utf-8") if AFL_STRATEGY_FILE.exists() else ""

        # If param_overrides provided, replace Optimize() calls with constants
        if param_overrides and run_mode != self.optimization_mode:
            afl_content = _apply_param_overrides(afl_content, param_overrides)

        # Write AFL to disk
        AFL_STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
        AFL_STRATEGY_FILE.write_text(afl_content, encoding="utf-8")

        # Create a run record
        run_params = {
            "run_mode": run_mode,
            "date_from": date_from,
            "date_to": date_to,
            "wf_label": label,
            "wf_run_id": self._wf_run_id,
        }
        if param_overrides:
            run_params["param_overrides"] = param_overrides

        run_id = create_run(
            version_id=self.version_id,
            strategy_id=self.strategy_id,
            apx_file=str(APX_OUTPUT),
            afl_content=afl_content,
            params_json=json.dumps(run_params, default=str),
            symbol=self.symbol,
            date_range=f"{date_from}:{date_to}",
        )

        output_dir = RESULTS_DIR / run_id
        output_dir.mkdir(parents=True, exist_ok=True)

        update_run(run_id, status="running")

        # Inject AFL date filter
        from datetime import datetime as _dt
        from_dn_dt = _dt.strptime(date_from, "%Y-%m-%d")
        to_dn_dt = _dt.strptime(date_to, "%Y-%m-%d")
        from_datenum = (from_dn_dt.year - 1900) * 10000 + from_dn_dt.month * 100 + from_dn_dt.day
        to_datenum = (to_dn_dt.year - 1900) * 10000 + to_dn_dt.month * 100 + to_dn_dt.day

        date_filter_afl = (
            f"\n// --- Date range filter (injected by walk-forward) ---\n"
            f"_pipelineDN = DateNum();\n"
            f"_pipelineInRange = (_pipelineDN >= {from_datenum}) * (_pipelineDN <= {to_datenum});\n"
            f"Buy   = Buy * _pipelineInRange;\n"
            f"Short = Short * _pipelineInRange;\n"
        )
        afl_current = AFL_STRATEGY_FILE.read_text(encoding="utf-8")
        AFL_STRATEGY_FILE.write_text(afl_current + date_filter_afl, encoding="utf-8")
        logger.info("    Injected date filter: DateNum %d to %d", from_datenum, to_datenum)

        # Build the APX file
        run_apx_path = APX_OUTPUT.parent / f"wf_{run_id}.apx"
        apx_path = build_apx(
            str(AFL_STRATEGY_FILE),
            str(run_apx_path),
            str(APX_TEMPLATE),
            run_id=run_id,
            symbol=self.symbol,
        )

        # Run the backtest via OLE
        import pythoncom
        pythoncom.CoInitialize()
        try:
            backtester = OLEBacktester()
            result = backtester.run_full_test(
                apx_path=str(run_apx_path),
                output_dir=str(output_dir),
                run_mode=run_mode,
            )
        finally:
            pythoncom.CoUninitialize()

        # Parse results
        now = datetime.now(timezone.utc).isoformat()
        if result:
            metrics = self._parse_run_metrics(output_dir, is_optimization=(run_mode == self.optimization_mode))
            update_kwargs = dict(
                status="completed",
                results_csv="results.csv",
                metrics_json=json.dumps(metrics, default=str),
                completed_at=now,
            )
            if run_mode == self.optimization_mode:
                update_kwargs["is_optimization"] = 1
                update_kwargs["total_combos"] = metrics.get("combos_tested", 0)
            update_run(run_id, **update_kwargs)
            logger.info("    Run %s completed.", run_id)
        else:
            update_run(
                run_id,
                status="failed",
                metrics_json=json.dumps({"error": "Backtest/optimisation failed"}),
                completed_at=now,
            )
            logger.warning("    Run %s failed.", run_id)

        # Cleanup APX
        try:
            run_apx_path.unlink(missing_ok=True)
        except Exception:
            pass

        return run_id

    def _parse_run_metrics(self, output_dir: Path, is_optimization: bool = False) -> dict:
        """Parse metrics from a completed run's results CSV.

        Parameters
        ----------
        output_dir : Path
            Directory containing ``results.csv``.
        is_optimization : bool
            If ``True``, parse as an optimisation CSV (one row per combo)
            rather than a trade list.

        Returns
        -------
        dict
            Parsed metrics.
        """
        import pandas as pd

        csv_path = output_dir / "results.csv"
        if not csv_path.exists():
            return {"error": "results.csv not found"}

        try:
            df = pd.read_csv(csv_path, encoding="utf-8")
        except Exception as exc:
            return {"error": f"Failed to read CSV: {exc}"}

        metrics: dict = {}

        if is_optimization:
            metrics["combos_tested"] = len(df)
            net_profit_col = None
            for col in df.columns:
                cl = col.lower().strip()
                if cl in ("net profit", "net profit %", "profit") and "%" not in cl:
                    net_profit_col = col
                    break
            if net_profit_col:
                vals = pd.to_numeric(df[net_profit_col], errors="coerce").dropna()
                if len(vals) > 0:
                    metrics["best_net_profit"] = round(float(vals.max()), 2)
                    metrics["worst_net_profit"] = round(float(vals.min()), 2)
                    metrics["avg_net_profit"] = round(float(vals.mean()), 2)
                    metrics["profitable_combos"] = int((vals > 0).sum())
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
                    "win_rate": round(
                        float((profits > 0).sum() / len(df) * 100), 1
                    ) if len(df) > 0 else 0,
                }
            else:
                metrics["total_trades"] = len(df)

        return metrics

    def _extract_best_params(self, is_run_id: str) -> dict:
        """Extract the best parameter set from an IS optimisation run.

        Queries the ``optimization_combos`` table for the run and returns
        the parameters of the combo with the highest ``net_profit``.

        Parameters
        ----------
        is_run_id : str
            UUID of the IS optimisation run.

        Returns
        -------
        dict
            Parameter name-to-value mapping, or empty dict if unavailable.
        """
        combos = get_optimization_combos(is_run_id, order_by="net_profit", ascending=False, limit=1)
        if combos:
            return combos[0].get("params", {})

        # Fallback: parse the CSV directly
        logger.info("    No SQL combos found, attempting CSV fallback ...")
        csv_path = RESULTS_DIR / is_run_id / "results.csv"
        if not csv_path.exists():
            return {}

        try:
            import pandas as pd
            df = pd.read_csv(csv_path, encoding="utf-8")
            if df.empty:
                return {}

            # Find net profit column
            net_profit_col = None
            for col in df.columns:
                cl = col.lower().strip()
                if cl in ("net profit", "net profit %", "profit") and "%" not in cl:
                    net_profit_col = col
                    break
            if net_profit_col is None:
                return {}

            # Find the row with max net profit
            df[net_profit_col] = pd.to_numeric(df[net_profit_col], errors="coerce")
            best_idx = df[net_profit_col].idxmax()
            best_row = df.loc[best_idx]

            # Classify columns to identify parameters
            metric_keywords = {
                "net profit", "profit", "# trades", "all trades", "avg. profit",
                "avg. bars", "drawdown", "max. trade", "winners", "losers",
                "profit factor", "sharpe", "ulcer", "recovery", "payoff",
                "cagr", "rar", "exposure", "risk", "% profitable",
            }
            params = {}
            for col in df.columns:
                cl = col.lower().strip()
                if cl == "symbol":
                    continue
                if not any(kw in cl for kw in metric_keywords):
                    val = best_row[col]
                    try:
                        params[col] = float(val) if pd.notna(val) else None
                    except (ValueError, TypeError):
                        params[col] = str(val) if pd.notna(val) else None

            return params
        except Exception as exc:
            logger.warning("    CSV param extraction failed: %s", exc)
            return {}

    def _build_summary(self, window_results: list[dict]) -> dict:
        """Aggregate per-window results into a walk-forward summary.

        Parameters
        ----------
        window_results : list[dict]
            Results from each window execution.

        Returns
        -------
        dict
            Summary with ``windows``, ``is_aggregate``, ``oos_aggregate``,
            and ``efficiency_ratio``.
        """
        summary = {
            "strategy_id": self.strategy_id,
            "version_id": self.version_id,
            "symbol": self.symbol,
            "total_period": self.total_period,
            "in_sample_pct": self.in_sample_pct,
            "num_windows": self.num_windows,
            "anchored": self.anchored,
            "windows": window_results,
            "is_aggregate": {},
            "oos_aggregate": {},
            "efficiency_ratio": None,
        }

        # Aggregate IS metrics
        is_profits = []
        is_trade_counts = []
        for wr in window_results:
            m = wr.get("is_metrics", {})
            if "best_net_profit" in m:
                is_profits.append(m["best_net_profit"])
            if "combos_tested" in m:
                is_trade_counts.append(m["combos_tested"])

        if is_profits:
            summary["is_aggregate"] = {
                "total_net_profit": round(sum(is_profits), 2),
                "avg_net_profit": round(sum(is_profits) / len(is_profits), 2),
                "best_window_profit": round(max(is_profits), 2),
                "worst_window_profit": round(min(is_profits), 2),
                "windows_profitable": sum(1 for p in is_profits if p > 0),
            }

        # Aggregate OOS metrics
        oos_profits = []
        oos_trade_counts = []
        oos_win_rates = []
        for wr in window_results:
            m = wr.get("oos_metrics", {})
            if "total_profit" in m:
                oos_profits.append(m["total_profit"])
            if "total_trades" in m:
                oos_trade_counts.append(m["total_trades"])
            if "win_rate" in m:
                oos_win_rates.append(m["win_rate"])

        if oos_profits:
            summary["oos_aggregate"] = {
                "total_net_profit": round(sum(oos_profits), 2),
                "avg_net_profit": round(sum(oos_profits) / len(oos_profits), 2),
                "best_window_profit": round(max(oos_profits), 2),
                "worst_window_profit": round(min(oos_profits), 2),
                "windows_profitable": sum(1 for p in oos_profits if p > 0),
                "avg_trades": round(
                    sum(oos_trade_counts) / len(oos_trade_counts), 1
                ) if oos_trade_counts else 0,
                "avg_win_rate": round(
                    sum(oos_win_rates) / len(oos_win_rates), 1
                ) if oos_win_rates else 0,
            }

        # Walk-forward efficiency ratio = OOS avg profit / IS avg profit
        is_avg = summary["is_aggregate"].get("avg_net_profit")
        oos_avg = summary["oos_aggregate"].get("avg_net_profit")
        if is_avg and oos_avg and is_avg != 0:
            summary["efficiency_ratio"] = round(oos_avg / is_avg, 4)

        return summary


# ---------------------------------------------------------------------------
# AFL parameter override helper
# ---------------------------------------------------------------------------

def _apply_param_overrides(afl_content: str, overrides: dict) -> str:
    """Replace Optimize() calls in AFL with fixed parameter values.

    For each parameter name in *overrides*, find the corresponding
    ``Optimize("name", ...)`` call and replace it with the fixed value.

    Parameters
    ----------
    afl_content : str
        The original AFL source code.
    overrides : dict
        Mapping of parameter names to their fixed values.

    Returns
    -------
    str
        Modified AFL with Optimize() calls replaced by constants.
    """
    import re

    modified = afl_content
    for param_name, value in overrides.items():
        # Match: Optimize("param_name", default, min, max, step)
        # Replace the entire Optimize() call with the fixed value.
        pattern = (
            r'Optimize\(\s*"'
            + re.escape(str(param_name))
            + r'"\s*,\s*[^)]*\)'
        )
        replacement = str(value)
        modified, count = re.subn(pattern, replacement, modified)
        if count > 0:
            logger.info("    Replaced Optimize(\"%s\", ...) with %s", param_name, value)
        else:
            logger.warning("    Could not find Optimize(\"%s\", ...) in AFL", param_name)

    return modified


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _window_to_serializable(window: dict) -> dict:
    """Convert a window dict with date objects to JSON-safe strings."""
    return {
        k: v.isoformat() if isinstance(v, date) else v
        for k, v in window.items()
    }


def _dates_to_range_code(start: date, end: date) -> str:
    """Build a human-readable date range string from two dates."""
    return f"{start}:{end}"


# ---------------------------------------------------------------------------
# Database helper (update, called internally)
# ---------------------------------------------------------------------------

def _update_walk_forward_run(
    wf_run_id: str,
    status: str = None,
    results_json: str = None,
    completed_at: str = None,
) -> bool:
    """Update a walk_forward_runs record.  Thin wrapper around strategy_db."""
    from scripts.strategy_db import _get_connection

    conn = _get_connection()
    fields = []
    values = []
    for col, val in [("status", status), ("results_json", results_json),
                     ("completed_at", completed_at)]:
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)
    if not fields:
        return True
    values.append(wf_run_id)
    cursor = conn.execute(
        f"UPDATE walk_forward_runs SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Walk-Forward Analysis for AmiBroker strategies"
    )
    parser.add_argument("--strategy-id", required=True,
                        help="UUID of the strategy")
    parser.add_argument("--version-id", default=None,
                        help="UUID of the version (default: latest)")
    parser.add_argument("--symbol", default=None,
                        help="Ticker symbol")
    parser.add_argument("--total-period", default="2y",
                        help="Total analysis period (e.g. 1y, 2y)")
    parser.add_argument("--in-sample-pct", type=float, default=0.70,
                        help="In-sample percentage (0-1)")
    parser.add_argument("--num-windows", type=int, default=5,
                        help="Number of IS/OOS windows")
    parser.add_argument("--anchored", action="store_true",
                        help="Use anchored windowing (IS grows)")
    parser.add_argument("--optimization-mode", type=int, default=4,
                        help="AmiBroker optimisation mode")

    args = parser.parse_args()

    setup_logging()
    init_db()

    version_id = args.version_id
    if version_id is None:
        v = get_latest_version(args.strategy_id)
        if v is None:
            logger.error("No versions found for strategy: %s", args.strategy_id)
            sys.exit(1)
        version_id = v["id"]

    analyzer = WalkForwardAnalyzer(
        strategy_id=args.strategy_id,
        version_id=version_id,
        symbol=args.symbol or DEFAULT_SYMBOL,
        total_period=args.total_period,
        in_sample_pct=args.in_sample_pct,
        num_windows=args.num_windows,
        optimization_mode=args.optimization_mode,
        anchored=args.anchored,
    )

    summary = analyzer.run()
    print(json.dumps(summary, indent=2, default=str))
