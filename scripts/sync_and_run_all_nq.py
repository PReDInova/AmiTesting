"""
Sync all named strategy AFL files from disk to the database, then re-run
each one against NQ with 3-month date range. Uses delays between runs to
avoid AmiBroker COM connection failures.
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
DELAY_BETWEEN_RUNS = 15  # seconds - longer to avoid COM instability

# Map strategy prefix -> (canonical DB name prefix, AFL filename)
STRATEGY_MAP = {
    "A01": "A01_tema_adx_trend.afl",
    "A02": "A02_tema_adx_mean_reversion.afl",
    "A03": "A03_consolidation_breakout.afl",
    "A04": "A04_consolidation_fade.afl",
    "A05": "A05_vwap_band_bounce.afl",
    "A06": "A06_vwap_tema_confluence.afl",
    "A07": "A07_rangebound_vwap_reversion.afl",
    "A08": "A08_derivative_reversal.afl",
    "A09": "A09_derivative_adx_weakening.afl",
    "A10": "A10_full_stack.afl",
    "B03": "B03_bb_rsi_reversion.afl",
    "B04": "B04_stochastic_range.afl",
    "B06": "B06_vwap_enhanced_reversion.afl",
    "B07": "B07_donchian_breakout.afl",
    "B09": "B09_adx_ema_crossover.afl",
    "B13": "B13_ny_momentum_carryover.afl",
    "C01": "C01_triple_filter.afl",
    "C02": "C02_vwap_derivative_precision.afl",
    "C03": "C03_rangebound_stochastic.afl",
    "C05": "C05_tema_vwap_session_fade.afl",
    "C06": "C06_adx_regime_switch.afl",
    "C08": "C08_vwap_cloud_breakout_adx.afl",
    "C09": "C09_double_touch_reversal.afl",
    "D01": "D01_derivative_basic.afl",
    "D02": "D02_nq_deriv_tema_zerocross.afl",
}


def get_prefix(name: str) -> str:
    """Extract strategy prefix from database name."""
    for prefix in sorted(STRATEGY_MAP.keys(), key=len, reverse=True):
        if name.startswith(prefix):
            return prefix
    return None


def main():
    init_db()
    strategies = list_strategies()

    # For each prefix, pick the FIRST matching strategy (canonical entry)
    canonical = {}
    for s in sorted(strategies, key=lambda x: x["name"]):
        prefix = get_prefix(s["name"])
        if prefix and prefix in STRATEGY_MAP and prefix not in canonical:
            canonical[prefix] = s

    # Sort by prefix
    ordered = sorted(canonical.items())

    print(f"\n{'='*80}")
    print(f"Syncing & running {len(ordered)} strategies against NQ with 3m date range")
    print(f"{'='*80}")
    print(f"{'Prefix':<6} {'Strategy Name':<55} {'AFL File'}")
    print(f"{'-'*6} {'-'*55} {'-'*40}")
    for prefix, s in ordered:
        print(f"{prefix:<6} {s['name']:<55} {STRATEGY_MAP[prefix]}")
    print()

    results = {}

    for i, (prefix, strategy) in enumerate(ordered):
        sid = strategy["id"]
        name = strategy["name"]
        afl_file = STRATEGIES_DIR / STRATEGY_MAP[prefix]

        print(f"\n{'-'*60}")
        print(f"[{i+1}/{len(ordered)}] {name}")
        print(f"  Strategy ID: {sid}")
        print(f"  AFL file: {afl_file.name}")
        print(f"{'-'*60}")

        if not afl_file.exists():
            print(f"  ERROR: AFL file not found: {afl_file}")
            results[prefix] = {
                "name": name,
                "status": "FILE_NOT_FOUND",
                "trade_count": 0,
            }
            continue

        # Read current disk AFL
        afl_content = afl_file.read_text(encoding="utf-8")

        # Create new version in database from disk file
        new_version_id = create_version(
            strategy_id=sid,
            afl_content=afl_content,
            label="NQ sync - current disk AFL",
        )
        print(f"  Synced version: {new_version_id[:8]}")

        # Run backtest
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
                results[prefix] = {
                    "name": name,
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
                results[prefix] = {
                    "name": name,
                    "strategy_id": sid,
                    "version_id": new_version_id,
                    "exit_code": exit_code,
                    "trade_count": 0,
                    "status": "NO_RUN_FOUND",
                }

        except Exception as exc:
            results[prefix] = {
                "name": name,
                "strategy_id": sid,
                "version_id": new_version_id,
                "exit_code": -1,
                "trade_count": 0,
                "status": f"ERROR: {str(exc)[:100]}",
            }
            print(f"  ERROR: {exc}")

        # Delay between runs
        if i < len(ordered) - 1:
            print(f"  Waiting {DELAY_BETWEEN_RUNS}s before next run...")
            time.sleep(DELAY_BETWEEN_RUNS)

    # Print summary
    print(f"\n\n{'='*80}")
    print("SYNC & RUN RESULTS - NQ 3m (All Named Strategies)")
    print(f"{'='*80}")
    print(f"{'Prefix':<6} {'Strategy':<52} {'Trades':>7} {'Profit':>12} {'Win%':>6} {'Status'}")
    print(f"{'-'*6} {'-'*52} {'-'*7} {'-'*12} {'-'*6} {'-'*15}")

    has_trades = 0
    zero_trades = 0

    for prefix in sorted(results.keys()):
        r = results[prefix]
        tc = r.get("trade_count", 0)
        tp = r.get("total_profit", 0)
        wr = r.get("win_rate", 0)
        status = r["status"]
        name = r["name"]

        if tc > 0:
            has_trades += 1
        else:
            zero_trades += 1

        print(f"{prefix:<6} {name:<52} {tc:>7d} {tp:>12.2f} {wr:>5.1f}% {status}")

    print(f"\n{'-'*80}")
    print(f"Total: {len(results)} strategies | {has_trades} with trades | {zero_trades} with zero trades")

    # Save results
    output_path = Path(__file__).resolve().parent.parent / "results" / "nq_3m_synced_summary.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_path}")

    if zero_trades > 0:
        print(f"\nStrategies needing modification:")
        for prefix in sorted(results.keys()):
            if results[prefix].get("trade_count", 0) == 0:
                print(f"  - {prefix}: {results[prefix]['name']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
