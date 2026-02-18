"""
Final D02 NQ backtest -- uses ApplyTo=0 (all symbols) + NQ filter in AFL.
Two approaches tested:
1. Periodicity=5 (native 1-min), no TimeFrameSet
2. Periodicity=0 (Tick) + TimeFrameSet(in1Minute) -- known working from test 5
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
from scripts.strategy_db import create_run, update_run

setup_logging()
logger = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parent.parent
APX_DIR = PROJECT / "apx"
RESULTS_BASE = PROJECT / "results"

# ---- D02 AFL: Native 1-min (no TimeFrameSet) with NQ filter ----
AFL_NATIVE = r"""
// ===========================================
// D02 - NQ Derivative TEMA Zero-Cross
// ===========================================
// Self-contained, no includes. Runs on native 1-min bars.

isNQ = Name() == "NQ";

temaLength = 8;
lookback = 5;
minDerivSep = 1;
stopPts = 30;
targetPts = 60;
tradeStart = 153000;
tradeEnd = 190000;

ema1 = EMA(Close, temaLength);
ema2 = EMA(ema1, temaLength);
ema3 = EMA(ema2, temaLength);
temas = 3 * ema1 - 3 * ema2 + ema3;

firstDeriv = (temas - Ref(temas, -lookback)) / lookback;
secondDeriv = firstDeriv - Ref(firstDeriv, -lookback);

tn = TimeNum();
inWindow = tn >= tradeStart AND tn <= tradeEnd;

temaRising  = temas > Ref(temas, -1);
temaFalling = temas < Ref(temas, -1);

crossUp   = Cross(firstDeriv, 0);
crossDown = Cross(0, firstDeriv);
derivSep = abs(firstDeriv - secondDeriv) >= minDerivSep;

Buy   = crossUp AND derivSep AND temaRising AND inWindow AND isNQ;
Sell  = 0;
Short = crossDown AND derivSep AND temaFalling AND inWindow AND isNQ;
Cover = 0;

SetTradeDelays(1, 1, 1, 1);
BuyPrice   = Open;
SellPrice  = Open;
ShortPrice = Open;
CoverPrice = Open;

ApplyStop(stopTypeLoss, stopModePoint, stopPts);
ApplyStop(stopTypeProfit, stopModePoint, targetPts);
SetPositionSize(1, spsShares);

Buy   = ExRem(Buy, Short);
Short = ExRem(Short, Buy);

Plot(Close, "Price", colorDefault, styleCandle);
Plot(temas, "TEMA", colorAqua, styleLine | styleThick);
PlotShapes(Buy * shapeUpArrow, colorGreen, 0, Low, -12);
PlotShapes(Short * shapeDownArrow, colorRed, 0, High, -12);

Title = Name() + " | D02 NQ Deriv TEMA Zero-Cross" +
        " | TEMA(8) Deriv(5) Sep=1" +
        " | Stop=30 Target=60" +
        " | Window=153000-190000 UTC";
"""

# ---- D02 AFL: TimeFrameSet approach (Periodicity=0) with NQ filter ----
AFL_TFS = r"""
// ===========================================
// D02 - NQ Derivative TEMA Zero-Cross (TimeFrameSet)
// ===========================================
// Uses TimeFrameSet(in1Minute) with base Periodicity=0.

isNQ = Name() == "NQ";

temaLength = 8;
lookback = 5;
minDerivSep = 1;
stopPts = 30;
targetPts = 60;
tradeStart = 153000;
tradeEnd = 190000;

TimeFrameSet(in1Minute);

ema1 = EMA(Close, temaLength);
ema2 = EMA(ema1, temaLength);
ema3 = EMA(ema2, temaLength);
temas = 3 * ema1 - 3 * ema2 + ema3;

firstDeriv = (temas - Ref(temas, -lookback)) / lookback;
secondDeriv = firstDeriv - Ref(firstDeriv, -lookback);

tn = TimeNum();
inWindow = tn >= tradeStart AND tn <= tradeEnd;

temaRising  = temas > Ref(temas, -1);
temaFalling = temas < Ref(temas, -1);

crossUp   = Cross(firstDeriv, 0);
crossDown = Cross(0, firstDeriv);
derivSep = abs(firstDeriv - secondDeriv) >= minDerivSep;

rawBuy   = crossUp AND derivSep AND temaRising AND inWindow AND isNQ;
rawShort = crossDown AND derivSep AND temaFalling AND inWindow AND isNQ;

TimeFrameRestore();

Buy   = TimeFrameExpand(rawBuy, in1Minute, expandFirst);
Sell  = 0;
Short = TimeFrameExpand(rawShort, in1Minute, expandFirst);
Cover = 0;

SetTradeDelays(1, 1, 1, 1);
BuyPrice   = Open;
SellPrice  = Open;
ShortPrice = Open;
CoverPrice = Open;

ApplyStop(stopTypeLoss, stopModePoint, stopPts);
ApplyStop(stopTypeProfit, stopModePoint, targetPts);
SetPositionSize(1, spsShares);

Buy   = ExRem(Buy, Short);
Short = ExRem(Short, Buy);

temas1m = TimeFrameExpand(temas, in1Minute);
Plot(Close, "Price", colorDefault, styleCandle);
Plot(temas1m, "TEMA", colorAqua, styleLine | styleThick);
PlotShapes(Buy * shapeUpArrow, colorGreen, 0, Low, -12);
PlotShapes(Short * shapeDownArrow, colorRed, 0, High, -12);

Title = Name() + " | D02 NQ Deriv TEMA Zero-Cross" +
        " | TEMA(8) Deriv(5) Sep=1" +
        " | Stop=30 Target=60" +
        " | Window=153000-190000 UTC";
"""


def run_test(name, afl_code, periodicity):
    """Run a backtest and report results."""
    run_id = str(uuid.uuid4())
    run_dir = RESULTS_BASE / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    afl_path = APX_DIR / f"strategy_{run_id}.afl"
    afl_path.write_text(afl_code, encoding="utf-8")

    apx_path = APX_DIR / f"run_{run_id}.apx"
    build_apx(
        afl_path=str(afl_path),
        output_apx_path=str(apx_path),
        template_apx_path=str(APX_TEMPLATE),
        run_id=run_id,
        periodicity=periodicity,
        symbol="__ALL__",  # ApplyTo=0 -- process ALL symbols
    )

    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"  Run ID: {run_id}")
    print(f"  Periodicity: {periodicity}")
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
        # Count NQ vs GC trades
        nq_trades = [l for l in lines[1:] if l.startswith("NQ,")]
        gc_trades = [l for l in lines[1:] if l.startswith("GC,")]
        print(f"  Total trades: {trade_count} ({len(nq_trades)} NQ, {len(gc_trades)} GC) in {elapsed:.1f}s")
        if nq_trades:
            print(f"  First NQ trades:")
            for line in nq_trades[:5]:
                print(f"    {line}")
    else:
        print(f"  No results.csv! ({elapsed:.1f}s)")

    return run_id

# Run both approaches
id1 = run_test("D02 Native 1-min (Period=5)", AFL_NATIVE, periodicity=5)
id2 = run_test("D02 TimeFrameSet (Period=0)", AFL_TFS, periodicity=0)

print(f"\nNative 1-min run: http://localhost:5000/results/{id1}")
print(f"TimeFrameSet run: http://localhost:5000/results/{id2}")
