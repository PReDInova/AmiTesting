"""
Run all named strategies against NQ with 3-month date range.
Collects results and reports which strategies produced trades.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.strategy_db import init_db, list_strategies, get_latest_version, get_latest_run
from run import main as run_pipeline

# Named strategies (not test strategies)
NAMED_PREFIXES = ("A0", "A10", "B0", "B1", "C0", "D0")

def is_named_strategy(name: str) -> bool:
    """Check if a strategy name matches the named pattern (A01, B06, etc.)."""
    for prefix in NAMED_PREFIXES:
        if name.startswith(prefix) and " - " in name:
            return True
    return False

def main():
    init_db()
    strategies = list_strategies()

    named = [s for s in strategies if is_named_strategy(s["name"])]
    # Deduplicate by name (keep first occurrence)
    seen_names = set()
    unique_named = []
    for s in named:
        if s["name"] not in seen_names:
            seen_names.add(s["name"])
            unique_named.append(s)

    named = sorted(unique_named, key=lambda s: s["name"])

    print(f"\n{'='*80}")
    print(f"Running {len(named)} named strategies against NQ with 3m date range")
    print(f"{'='*80}\n")

    results = {}

    for i, strategy in enumerate(named):
        sid = strategy["id"]
        name = strategy["name"]
        print(f"\n{'-'*60}")
        print(f"[{i+1}/{len(named)}] {name}")
        print(f"  Strategy ID: {sid}")
        print(f"{'-'*60}")

        try:
            exit_code = run_pipeline(
                strategy_id=sid,
                symbol="NQ",
                date_range="3m",
                run_mode=2,
            )

            # Fetch the latest run to get metrics
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
                    "exit_code": exit_code,
                    "trade_count": 0,
                    "status": "NO_RUN_FOUND",
                }

        except Exception as exc:
            results[name] = {
                "strategy_id": sid,
                "exit_code": -1,
                "trade_count": 0,
                "status": f"ERROR: {str(exc)[:100]}",
            }
            print(f"  ERROR: {exc}")

    # Print summary
    print(f"\n\n{'='*80}")
    print("RESULTS SUMMARY")
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

    # Save results to JSON
    output_path = Path(__file__).resolve().parent.parent / "results" / "nq_3m_summary.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_path}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
