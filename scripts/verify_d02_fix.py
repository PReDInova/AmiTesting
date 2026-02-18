"""
Verify the D02 fix: run the fixed strategy and confirm it completes in < 30 seconds.
"""
import sys
import time
import uuid
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import APX_TEMPLATE, setup_logging
from scripts.apx_builder import build_apx
from scripts.ole_backtest import OLEBacktester

setup_logging()
logger = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parent.parent
APX_DIR = PROJECT / "apx"
RESULTS_BASE = PROJECT / "results"

run_id = str(uuid.uuid4())
run_dir = RESULTS_BASE / run_id
run_dir.mkdir(parents=True, exist_ok=True)

# Use the FIXED strategy file
afl_path = PROJECT / "strategies" / "D02_nq_deriv_tema_zerocross.afl"

apx_path = APX_DIR / f"run_{run_id}.apx"
build_apx(
    afl_path=str(afl_path),
    output_apx_path=str(apx_path),
    template_apx_path=str(APX_TEMPLATE),
    run_id=run_id,
    periodicity=5,
    symbol="__ALL__",
)

print(f"\n{'='*60}")
print(f"VERIFYING D02 FIX")
print(f"  Run ID: {run_id}")
print(f"  AFL: {afl_path}")
print(f"  APX: {apx_path}")
print(f"{'='*60}\n")

bt = OLEBacktester()
start = time.time()
success = bt.run_full_test(
    apx_path=str(apx_path),
    output_dir=str(run_dir),
    run_mode=2,
)
elapsed = time.time() - start

csv_path = run_dir / "results.csv"
trade_count = 0
if csv_path.exists():
    lines = csv_path.read_text(encoding="utf-8").strip().split("\n")
    trade_count = max(0, len(lines) - 1)

print(f"\n{'='*60}")
print(f"RESULT: {'PASS' if success else 'FAIL'}")
print(f"  Elapsed: {elapsed:.1f}s")
print(f"  Trades: {trade_count}")
if elapsed < 30:
    print(f"  Completed in under 30 seconds -- FIX VERIFIED!")
else:
    print(f"  WARNING: Took more than 30 seconds ({elapsed:.1f}s)")
print(f"{'='*60}")

sys.exit(0 if success else 1)
