"""
Minimal test: does AmiBroker execute AFL on NQ at all?
Test 1: Buy on every bar for NQ only
Test 2: Buy on every bar for ALL symbols
"""
import sys
import uuid
import time
import re
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    AMIBROKER_DB_PATH, AMIBROKER_EXE, AMIBROKER_EXE_PATH,
    APX_TEMPLATE, setup_logging,
)
from scripts.apx_builder import build_apx
from scripts.ole_backtest import OLEBacktester

setup_logging()
logger = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parent.parent
APX_DIR = PROJECT / "apx"
RESULTS_BASE = PROJECT / "results"

def run_test(name, afl_code, periodicity, apply_to_all=False):
    """Run a single test and report results."""
    run_id = str(uuid.uuid4())
    run_dir = RESULTS_BASE / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    afl_path = APX_DIR / f"strategy_{run_id}.afl"
    afl_path.write_text(afl_code, encoding="utf-8")

    apx_path = APX_DIR / f"run_{run_id}.apx"

    # Build APX - use __ALL__ to set ApplyTo=0 if requested
    symbol = "__ALL__" if apply_to_all else "NQ"
    build_apx(
        afl_path=str(afl_path),
        output_apx_path=str(apx_path),
        template_apx_path=str(APX_TEMPLATE),
        run_id=run_id,
        periodicity=periodicity,
        symbol=symbol,
    )

    # Verify APX settings
    apx_content = apx_path.read_text(encoding="iso-8859-1")
    per_match = re.search(r"<Periodicity>(\d+)</Periodicity>", apx_content)
    at_matches = re.findall(r"<ApplyTo>(\d+)</ApplyTo>", apx_content)
    sym_match = re.search(r"<Symbol>(.*?)</Symbol>", apx_content)
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"  Periodicity: {per_match.group(1) if per_match else '?'}")
    print(f"  ApplyTo: {at_matches}")
    print(f"  Symbol: '{sym_match.group(1) if sym_match else '?'}'")
    print(f"{'='*60}")

    bt = OLEBacktester()
    start = time.time()
    success = bt.run_full_test(
        apx_path=str(apx_path),
        output_dir=str(run_dir),
        run_mode=2,
    )
    elapsed = time.time() - start

    csv_path = run_dir / "results.csv"
    if csv_path.exists():
        lines = csv_path.read_text(encoding="utf-8").strip().split("\n")
        trade_count = len(lines) - 1
        print(f"  Result: {trade_count} trades in {elapsed:.1f}s")
        if trade_count > 0:
            # Show symbols
            symbols = set()
            for line in lines[1:]:
                symbols.add(line.split(",")[0])
            print(f"  Symbols: {symbols}")
            for line in lines[1:3]:
                print(f"    {line}")
    else:
        print(f"  No results.csv! ({elapsed:.1f}s)")

    return run_id

# ---- Test 1: Buy every bar, NQ only, Periodicity=5 ----
run_test("NQ-only buy-all, Period=5", """
Buy = 1;
Sell = 0;
Short = 0;
Cover = 0;
SetPositionSize(1, spsShares);
""", periodicity=5, apply_to_all=False)

# ---- Test 2: Buy every bar, ALL symbols, Periodicity=5 ----
run_test("ALL symbols buy-all, Period=5", """
Buy = 1;
Sell = 0;
Short = 0;
Cover = 0;
SetPositionSize(1, spsShares);
""", periodicity=5, apply_to_all=True)

# ---- Test 3: Buy every bar, ALL symbols, Periodicity=0 (Tick) ----
run_test("ALL symbols buy-all, Period=0 (Tick)", """
Buy = 1;
Sell = 0;
Short = 0;
Cover = 0;
SetPositionSize(1, spsShares);
""", periodicity=0, apply_to_all=True)

# ---- Test 4: Buy every bar, ALL symbols, Periodicity=4 (maybe 1-min?) ----
run_test("ALL symbols buy-all, Period=4", """
Buy = 1;
Sell = 0;
Short = 0;
Cover = 0;
SetPositionSize(1, spsShares);
""", periodicity=4, apply_to_all=True)

# ---- Test 5: Full D02 logic, ALL symbols, Periodicity=0 + TimeFrameSet ----
run_test("D02 full logic, ALL, Period=0 + TFS", r"""
TimeFrameSet(in1Minute);

ema1 = EMA(Close, 8);
ema2 = EMA(ema1, 8);
ema3 = EMA(ema2, 8);
temas = 3 * ema1 - 3 * ema2 + ema3;

firstDeriv = (temas - Ref(temas, -5)) / 5;
secondDeriv = firstDeriv - Ref(firstDeriv, -5);

crossUp = Cross(firstDeriv, 0);
crossDown = Cross(0, firstDeriv);
derivSep = abs(firstDeriv - secondDeriv) >= 1;
temaRising = temas > Ref(temas, -1);
temaFalling = temas < Ref(temas, -1);

rawBuy = crossUp AND derivSep AND temaRising;
rawShort = crossDown AND derivSep AND temaFalling;

TimeFrameRestore();

rawBuyX = TimeFrameExpand(rawBuy, in1Minute, expandFirst);
rawShortX = TimeFrameExpand(rawShort, in1Minute, expandFirst);

Buy = rawBuyX;
Sell = 0;
Short = rawShortX;
Cover = 0;

SetTradeDelays(1, 1, 1, 1);
BuyPrice = Open;
SellPrice = Open;
ShortPrice = Open;
CoverPrice = Open;

ApplyStop(stopTypeLoss, stopModePoint, 30);
ApplyStop(stopTypeProfit, stopModePoint, 60);
SetPositionSize(1, spsShares);

Buy = ExRem(Buy, Short);
Short = ExRem(Short, Buy);
""", periodicity=0, apply_to_all=True)

print("\nDone.")
