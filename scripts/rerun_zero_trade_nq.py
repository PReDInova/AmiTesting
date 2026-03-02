"""
Re-run only the 14 zero-trade strategies with delays between runs to avoid
AmiBroker COM connection failures.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.strategy_db import (
    init_db,
    list_strategies,
    get_latest_version,
    get_latest_run,
    create_version,
)
from run import main as run_pipeline

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"

# Round 10: D02 only (last COM retry)
ZERO_TRADE_MAP = {
    "D02": "D02_nq_deriv_tema_zerocross.afl",
}

DELAY_BETWEEN_RUNS = 20  # seconds


def get_strategy_prefix(name: str) -> str:
    for prefix in ZERO_TRADE_MAP:
        if name.startswith(prefix):
            return prefix
    return None


def main():
    init_db()
    strategies = list_strategies()

    # Find the target strategies
    named = []
    seen = set()
    for s in strategies:
        prefix = get_strategy_prefix(s["name"])
        if prefix and prefix not in seen and prefix in ZERO_TRADE_MAP:
            seen.add(prefix)
            named.append(s)

    named.sort(key=lambda s: s["name"])

    print(f"\n{'='*80}")
    print(f"Re-running {len(named)} zero-trade strategies with {DELAY_BETWEEN_RUNS}s delay")
    print(f"{'='*80}\n")

    results = {}

    for i, strategy in enumerate(named):
        sid = strategy["id"]
        name = strategy["name"]
        prefix = get_strategy_prefix(name)
        afl_file = STRATEGIES_DIR / ZERO_TRADE_MAP[prefix]

        print(f"\n{'-'*60}")
        print(f"[{i+1}/{len(named)}] {name}")
        print(f"  Strategy ID: {sid}")
        print(f"  AFL file: {afl_file.name}")
        print(f"{'-'*60}")

        if not afl_file.exists():
            print(f"  ERROR: AFL file not found: {afl_file}")
            results[name] = {"status": "FILE_NOT_FOUND", "trade_count": 0}
            continue

        # Read the new AFL content
        afl_content = afl_file.read_text(encoding="utf-8")

        # Create a new version in the database
        new_version_id = create_version(
            strategy_id=sid,
            afl_content=afl_content,
            label="NQ round 10 - D02 COM retry",
        )
        print(f"  Created version: {new_version_id[:8]}")

        # Run the backtest
        try:
            exit_code = run_pipeline(
                strategy_id=sid,
                version_id=new_version_id,
                symbol="NQ",
                date_range="3m",
                run_mode=2,
            )

            latest_run = get_latest_run(sid)
            if latest_run:
                metrics = latest_run.get("metrics", {})
                if isinstance(metrics, str):
                    metrics = json.loads(metrics)
                trade_count = metrics.get("total_trades", 0)
                total_profit = metrics.get("total_profit", 0)
                win_rate = metrics.get("win_rate", 0)
                results[name] = {
                    "strategy_id": sid,
                    "version_id": new_version_id,
                    "run_id": latest_run["id"],
                    "exit_code": exit_code,
                    "trade_count": trade_count,
                    "total_profit": total_profit,
                    "win_rate": win_rate,
                    "status": "HAS_TRADES" if trade_count > 0 else "ZERO_TRADES",
                }
            else:
                results[name] = {
                    "strategy_id": sid,
                    "version_id": new_version_id,
                    "exit_code": exit_code,
                    "trade_count": 0,
                    "status": "NO_RUN_FOUND",
                }

        except Exception as exc:
            results[name] = {
                "strategy_id": sid,
                "version_id": new_version_id,
                "exit_code": -1,
                "trade_count": 0,
                "status": f"ERROR: {str(exc)[:100]}",
            }
            print(f"  ERROR: {exc}")

        # Delay between runs to let AmiBroker fully close
        if i < len(named) - 1:
            print(f"  Waiting {DELAY_BETWEEN_RUNS}s before next run...")
            time.sleep(DELAY_BETWEEN_RUNS)

    # Print summary
    print(f"\n\n{'='*80}")
    print("ROUND 9 RESULTS - NQ 3m (Zero-Trade Strategies)")
    print(f"{'='*80}")
    print(f"{'Strategy':<55s} {'Trades':>7s} {'Profit':>12s} {'Win%':>6s} {'Status'}")
    print(f"{'-'*55} {'-'*7} {'-'*12} {'-'*6} {'-'*15}")

    has_trades = 0
    zero_trades = 0

    for name in sorted(results.keys()):
        r = results[name]
        tc = r.get("trade_count", 0)
        tp = r.get("total_profit", 0)
        wr = r.get("win_rate", 0)
        status = r["status"]

        if tc > 0:
            has_trades += 1
        else:
            zero_trades += 1

        print(f"{name:<55s} {tc:>7d} {tp:>12.2f} {wr:>5.1f}% {status}")

    print(f"\n{'-'*80}")
    print(f"Total: {len(results)} strategies | {has_trades} with trades | {zero_trades} with zero trades")

    # Save results
    output_path = Path(__file__).resolve().parent.parent / "results" / "nq_3m_round9_summary.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_path}")

    if zero_trades > 0:
        print(f"\nStrategies STILL needing modification:")
        for name in sorted(results.keys()):
            if results[name].get("trade_count", 0) == 0:
                print(f"  - {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
