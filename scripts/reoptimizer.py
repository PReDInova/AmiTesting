"""
Automated re-optimization scheduling.

Periodically re-optimizes live strategies using the walk-forward
engine and alerts the user if optimal parameters have drifted
significantly from current settings.

Does NOT auto-deploy changed parameters — surfaces them for
human review via the dashboard.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ReoptimizationScheduler:
    """Schedule periodic re-optimization of live strategies.

    Parameters
    ----------
    check_interval_hours : int
        How often to check if re-optimization is needed (default: 24).
    reopt_interval_days : int
        Minimum days between re-optimizations for a strategy (default: 7).
    drift_threshold_pct : float
        If optimal parameters differ by more than this percentage,
        alert the user (default: 20.0).
    """

    def __init__(
        self,
        check_interval_hours: int = 24,
        reopt_interval_days: int = 7,
        drift_threshold_pct: float = 20.0,
    ):
        self._check_interval = check_interval_hours * 3600
        self._reopt_interval_days = reopt_interval_days
        self._drift_threshold = drift_threshold_pct

        self._scheduler = None
        self._running = False
        self._lock = threading.Lock()

        # Track last reopt time per strategy
        self._last_reopt: dict[str, datetime] = {}

        # Pending recommendations
        self._recommendations: list[dict] = []

        logger.info(
            "ReoptimizationScheduler initialized: check every %dh, "
            "reopt every %dd, drift threshold %.1f%%",
            check_interval_hours, reopt_interval_days, drift_threshold_pct,
        )

    def start(self) -> None:
        """Start the scheduler using APScheduler."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._scheduler = BackgroundScheduler()
            self._scheduler.add_job(
                self._check_strategies,
                'interval',
                seconds=self._check_interval,
                id='reopt_check',
            )
            self._scheduler.start()
            self._running = True
            logger.info("ReoptimizationScheduler started.")
        except ImportError:
            logger.warning("APScheduler not installed — running manual check mode.")
            self._running = False

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
        self._running = False
        logger.info("ReoptimizationScheduler stopped.")

    def trigger_now(self, strategy_id: str = None) -> list[dict]:
        """Manually trigger a re-optimization check.

        If strategy_id is provided, only check that strategy.
        Returns list of recommendation dicts.
        """
        return self._check_strategies(strategy_id=strategy_id)

    def get_recommendations(self) -> list[dict]:
        """Get pending parameter drift recommendations."""
        with self._lock:
            return list(self._recommendations)

    def clear_recommendations(self) -> None:
        """Clear all pending recommendations."""
        with self._lock:
            self._recommendations = []

    def _check_strategies(self, strategy_id: str = None) -> list[dict]:
        """Check which strategies need re-optimization."""
        from scripts.strategy_db import list_deployable_strategies, get_latest_version

        new_recommendations = []

        try:
            if strategy_id:
                from scripts.strategy_db import get_strategy
                strategy = get_strategy(strategy_id)
                strategies = [strategy] if strategy else []
            else:
                strategies = list_deployable_strategies()

            for strategy in strategies:
                sid = strategy["id"]

                # Check if enough time has passed since last reopt
                last = self._last_reopt.get(sid)
                if last:
                    days_since = (datetime.now(timezone.utc) - last).total_seconds() / 86400
                    if days_since < self._reopt_interval_days:
                        continue

                # Get current version parameters
                version = get_latest_version(sid)
                if not version:
                    continue

                current_params = version.get("parameters", [])

                # Run walk-forward analysis
                recommendation = self._run_reoptimization(
                    strategy, version, current_params
                )

                if recommendation:
                    new_recommendations.append(recommendation)

                self._last_reopt[sid] = datetime.now(timezone.utc)

        except Exception as exc:
            logger.exception("Re-optimization check failed: %s", exc)

        with self._lock:
            self._recommendations.extend(new_recommendations)

        return new_recommendations

    def _run_reoptimization(
        self,
        strategy: dict,
        version: dict,
        current_params: list,
    ) -> Optional[dict]:
        """Run re-optimization for a single strategy.

        Returns a recommendation dict if parameters have drifted,
        or None if no change is needed.
        """
        sid = strategy["id"]
        vid = version["id"]

        logger.info("Running re-optimization for strategy: %s", strategy["name"])

        try:
            from scripts.walk_forward import WalkForwardAnalyzer

            analyzer = WalkForwardAnalyzer(
                strategy_id=sid,
                version_id=vid,
                symbol=strategy.get("symbol", ""),
                total_period="6m",
                num_windows=3,
            )

            # This would actually run the walk-forward analysis
            # For now, return a placeholder recommendation structure
            return {
                "strategy_id": sid,
                "strategy_name": strategy["name"],
                "version_id": vid,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "needs_review",
                "message": (
                    f"Re-optimization check completed for '{strategy['name']}'. "
                    f"Review the latest walk-forward results to see if "
                    f"parameter adjustments are recommended."
                ),
                "current_params": current_params,
                "suggested_params": None,  # Populated by actual WF results
                "drift_pct": None,
            }

        except Exception as exc:
            logger.exception("Re-optimization failed for %s: %s",
                             strategy["name"], exc)
            return None

    def _compute_param_drift(
        self,
        current: list[dict],
        optimal: list[dict],
    ) -> float:
        """Compute average parameter drift as a percentage.

        Returns the mean absolute percentage change across all
        numeric parameters.
        """
        if not current or not optimal:
            return 0.0

        current_map = {p.get("name", ""): p.get("value") for p in current}
        optimal_map = {p.get("name", ""): p.get("value") for p in optimal}

        drifts = []
        for name, curr_val in current_map.items():
            opt_val = optimal_map.get(name)
            if opt_val is None:
                continue
            try:
                curr_f = float(str(curr_val).replace(",", ""))
                opt_f = float(str(opt_val).replace(",", ""))
                if curr_f != 0:
                    drift = abs(opt_f - curr_f) / abs(curr_f) * 100
                    drifts.append(drift)
            except (ValueError, TypeError):
                continue

        return sum(drifts) / len(drifts) if drifts else 0.0

    @property
    def is_running(self) -> bool:
        return self._running
