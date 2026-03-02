"""
Update database versions with NQ-adapted AFL and re-run all named strategies
against NQ with 3-month date range.
"""

import json
import sys
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

# Map strategy name prefixes to AFL files
STRATEGY_FILE_MAP = {
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
}


def get_strategy_prefix(name: str) -> str:
    """Extract the strategy prefix (A01, B06, etc.) from the name."""
    for prefix in STRATEGY_FILE_MAP:
        if name.startswith(prefix):
            return prefix
    return None


def main():
    init_db()
    strategies = list_strategies()

    # Find named strategies
    named = []
    seen = set()
    for s in strategies:
        prefix = get_strategy_prefix(s["name"])
        if prefix and prefix not in seen and prefix in STRATEGY_FILE_MAP:
            seen.add(prefix)
            named.append(s)

    named.sort(key=lambda s: s["name"])

    print(f"\n{'='*80}")
    print(f"Updating {len(named)} strategies with NQ-adapted AFL and running backtests")
    print(f"{'='*80}\n")

    results = {}

    for i, strategy in enumerate(named):
        sid = strategy["id"]
        name = strategy["name"]
        prefix = get_strategy_prefix(name)
        afl_file = STRATEGIES_DIR / STRATEGY_FILE_MAP[prefix]

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
            label="NQ adaptation - native 1-min data",
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

    # Print summary
    print(f"\n\n{'='*80}")
    print("RESULTS SUMMARY - NQ 3m Backtest (Adapted Strategies)")
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
    output_path = Path(__file__).resolve().parent.parent / "results" / "nq_3m_adapted_summary.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_path}")

    # List zero-trade strategies
    if zero_trades > 0:
        print(f"\nStrategies needing further modification:")
        for name in sorted(results.keys()):
            if results[name].get("trade_count", 0) == 0:
                print(f"  - {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
