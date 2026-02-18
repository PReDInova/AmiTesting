"""
Batch backtest runner -- executes multiple strategies sequentially.

AmiBroker's COM interface supports only one backtest at a time, so this
module queues strategies and runs them one by one through the existing
run.py pipeline.
"""

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.strategy_db import (
    get_batch,
    update_batch,
    get_latest_version,
    get_run,
    get_strategy,
    list_strategies,
    create_batch,
)

logger = logging.getLogger(__name__)


class BatchRunner:
    """Manages sequential batch execution of backtests."""

    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        self._cancel_event = threading.Event()

    def cancel(self):
        """Signal the batch to stop after the current strategy finishes."""
        self._cancel_event.set()
        logger.info("Batch %s: cancel requested.", self.batch_id)

    def run_batch(self):
        """Run all strategies in the batch sequentially."""
        from run import main as run_pipeline

        batch = get_batch(self.batch_id)
        if batch is None:
            logger.error("Batch %s not found.", self.batch_id)
            return

        strategy_ids = batch["strategy_ids"]
        run_mode = batch.get("run_mode", 2)
        total = len(strategy_ids)

        logger.info("Batch %s: starting %d strategies (run_mode=%d)",
                     self.batch_id, total, run_mode)

        update_batch(self.batch_id,
                     status="running",
                     started_at=datetime.now(timezone.utc).isoformat())

        completed = 0
        failed = 0
        run_ids = []
        results = {}

        for i, strategy_id in enumerate(strategy_ids):
            # Check for cancellation
            if self._cancel_event.is_set():
                logger.info("Batch %s: cancelled at %d/%d.", self.batch_id, i, total)
                update_batch(self.batch_id,
                             status="cancelled",
                             completed_count=completed,
                             failed_count=failed,
                             run_ids=run_ids,
                             results_json=json.dumps(results),
                             completed_at=datetime.now(timezone.utc).isoformat())
                return

            strategy = get_strategy(strategy_id)
            strategy_name = strategy["name"] if strategy else strategy_id[:8]
            logger.info("Batch %s: [%d/%d] Running %s ...",
                        self.batch_id, i + 1, total, strategy_name)

            # Get latest version
            version = get_latest_version(strategy_id)
            version_id = version["id"] if version else None

            try:
                exit_code = run_pipeline(
                    strategy_id=strategy_id,
                    version_id=version_id,
                    run_mode=run_mode,
                )

                if exit_code == 0:
                    completed += 1
                    # Fetch the latest run for this strategy to get its ID and metrics
                    from scripts.strategy_db import get_latest_run
                    latest_run = get_latest_run(strategy_id)
                    if latest_run:
                        run_ids.append(latest_run["id"])
                        results[strategy_id] = {
                            "run_id": latest_run["id"],
                            "status": "completed",
                            "strategy_name": strategy_name,
                            "metrics": latest_run.get("metrics", {}),
                        }
                    logger.info("Batch %s: [%d/%d] %s completed.",
                                self.batch_id, i + 1, total, strategy_name)
                else:
                    failed += 1
                    results[strategy_id] = {
                        "status": "failed",
                        "strategy_name": strategy_name,
                        "error": f"Pipeline returned exit code {exit_code}",
                    }
                    logger.warning("Batch %s: [%d/%d] %s failed (exit_code=%d).",
                                   self.batch_id, i + 1, total, strategy_name, exit_code)

            except Exception as exc:
                failed += 1
                results[strategy_id] = {
                    "status": "failed",
                    "strategy_name": strategy_name,
                    "error": str(exc),
                }
                logger.exception("Batch %s: [%d/%d] %s raised exception.",
                                 self.batch_id, i + 1, total, strategy_name)

            # Update progress after each strategy
            update_batch(self.batch_id,
                         completed_count=completed,
                         failed_count=failed,
                         run_ids=run_ids,
                         results_json=json.dumps(results))

        # Batch complete
        final_status = "completed" if failed == 0 else "completed"  # completed even with some failures
        update_batch(self.batch_id,
                     status=final_status,
                     completed_count=completed,
                     failed_count=failed,
                     run_ids=run_ids,
                     results_json=json.dumps(results),
                     completed_at=datetime.now(timezone.utc).isoformat())

        logger.info("Batch %s: finished. %d completed, %d failed out of %d total.",
                     self.batch_id, completed, failed, total)


def start_batch(strategy_ids: list = None, run_mode: int = 2, name: str = "") -> str:
    """Create a batch record and return its ID.

    If strategy_ids is None or empty, queues ALL strategies in the database.
    Does NOT start execution -- call BatchRunner(batch_id).run_batch() to run.
    """
    if not strategy_ids:
        all_strategies = list_strategies()
        strategy_ids = [s["id"] for s in all_strategies]

    if not name:
        name = f"Batch run ({len(strategy_ids)} strategies)"

    batch_id = create_batch(
        name=name,
        strategy_ids=strategy_ids,
        run_mode=run_mode,
    )

    logger.info("Created batch %s: %d strategies, run_mode=%d",
                batch_id, len(strategy_ids), run_mode)
    return batch_id
