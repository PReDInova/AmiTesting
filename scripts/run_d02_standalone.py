"""
Standalone D02 NQ backtest -- self-contained AFL, no includes.
Builds APX with Periodicity=5 (1-minute), runs via OLE, collects results.
"""
import sys
import time
import uuid
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    AMIBROKER_DB_PATH,
    AMIBROKER_EXE,
    AMIBROKER_EXE_PATH,
    APX_TEMPLATE,
    setup_logging,
)
from scripts.apx_builder import build_apx
from scripts.ole_backtest import OLEBacktester
from scripts.strategy_db import create_run, update_run

setup_logging()
logger = logging.getLogger(__name__)

# ---- Self-contained AFL (no #include, no Param) ----
AFL_CODE = r"""
// ===========================================
// D02 - NQ Derivative TEMA Zero-Cross (standalone)
// ===========================================
// Fully self-contained -- no includes, no Param() calls.

// ---- Symbol filter ----
isNQ = Name() == "NQ";

// ---- Hardcoded parameters ----
temaLength = 8;
lookback = 5;
minDerivSep = 1;
stopPts = 30;
targetPts = 60;
tradeStart = 153000;
tradeEnd = 190000;

// ---- TEMA(8) using built-in EMA ----
ema1 = EMA(Close, temaLength);
ema2 = EMA(ema1, temaLength);
ema3 = EMA(ema2, temaLength);
temas = 3 * ema1 - 3 * ema2 + ema3;

// ---- Derivatives ----
firstDeriv = (temas - Ref(temas, -lookback)) / lookback;
secondDeriv = firstDeriv - Ref(firstDeriv, -lookback);

// ---- Time filter (UTC) ----
tn = TimeNum();
inWindow = tn >= tradeStart AND tn <= tradeEnd;

// ---- TEMA slope ----
temaRising  = temas > Ref(temas, -1);
temaFalling = temas < Ref(temas, -1);

// ---- Zero-crossing signals ----
crossUp   = Cross(firstDeriv, 0);
crossDown = Cross(0, firstDeriv);

// ---- Derivative separation ----
derivSep = abs(firstDeriv - secondDeriv) >= minDerivSep;

// ---- Entry signals ----
Buy   = crossUp AND derivSep AND temaRising AND inWindow AND isNQ;
Sell  = 0;
Short = crossDown AND derivSep AND temaFalling AND inWindow AND isNQ;
Cover = 0;

// ---- Next-bar open entry ----
SetTradeDelays(1, 1, 1, 1);
BuyPrice   = Open;
SellPrice  = Open;
ShortPrice = Open;
CoverPrice = Open;

// ---- Stops ----
ApplyStop(stopTypeLoss, stopModePoint, stopPts);
ApplyStop(stopTypeProfit, stopModePoint, targetPts);

// ---- Position sizing: 1 contract ----
SetPositionSize(1, spsShares);

// ---- Remove duplicates ----
Buy   = ExRem(Buy, Short);
Short = ExRem(Short, Buy);

// ---- Visualization ----
Plot(Close, "Price", colorDefault, styleCandle);
Plot(temas, "TEMA", colorAqua, styleLine | styleThick);
PlotShapes(Buy * shapeUpArrow, colorGreen, 0, Low, -12);
PlotShapes(Short * shapeDownArrow, colorRed, 0, High, -12);
"""

# ---- Paths ----
PROJECT = Path(__file__).resolve().parent.parent
APX_DIR = PROJECT / "apx"
RESULTS_BASE = PROJECT / "results"

run_id = str(uuid.uuid4())
run_dir = RESULTS_BASE / run_id
run_dir.mkdir(parents=True, exist_ok=True)

# Write AFL to a file
afl_path = APX_DIR / f"strategy_{run_id}.afl"
afl_path.write_text(AFL_CODE, encoding="utf-8")
logger.info("Wrote standalone AFL: %s", afl_path)

# Build APX with Periodicity=5 (1-minute)
apx_path = APX_DIR / f"run_{run_id}.apx"
build_apx(
    afl_path=str(afl_path),
    output_apx_path=str(apx_path),
    template_apx_path=str(APX_TEMPLATE),
    run_id=run_id,
    periodicity=5,
    symbol="NQ",
)
logger.info("Built APX: %s", apx_path)

# Verify Periodicity in APX
apx_content = apx_path.read_text(encoding="iso-8859-1")
import re
per_match = re.search(r"<Periodicity>(\d+)</Periodicity>", apx_content)
if per_match:
    logger.info("APX Periodicity confirmed: %s", per_match.group(1))
else:
    logger.warning("Could not find Periodicity in APX!")

apply_matches = re.findall(r"<ApplyTo>(\d+)</ApplyTo>", apx_content)
logger.info("APX ApplyTo values: %s", apply_matches)

sym_match = re.search(r"<Symbol>(.*?)</Symbol>", apx_content)
if sym_match:
    logger.info("APX Symbol: '%s'", sym_match.group(1))

# Create database run record
D02_STRATEGY_ID = "2cf8394f-ecf9-40df-8e81-b2c1fc5d259b"
try:
    create_run(
        strategy_id=D02_STRATEGY_ID,
        run_id=run_id,
        params={"temaLength": 8, "lookback": 5, "minDerivSep": 1,
                "stopPoints": 30, "targetPoints": 60,
                "tradeStart": 153000, "tradeEnd": 190000,
                "note": "standalone AFL, no includes, periodicity=5"},
    )
except Exception as e:
    logger.warning("DB create_run failed (non-fatal): %s", e)

# Run backtest
print(f"\n{'='*60}")
print(f"Running D02 standalone backtest")
print(f"  Run ID: {run_id}")
print(f"  AFL: {afl_path}")
print(f"  APX: {apx_path}")
print(f"  Results: {run_dir}")
print(f"  Periodicity: 5 (1-minute)")
print(f"{'='*60}\n")

bt = OLEBacktester()
success = bt.run_full_test(
    apx_path=str(apx_path),
    output_dir=str(run_dir),
    run_mode=2,  # Portfolio backtest
)

# Check results
csv_path = run_dir / "results.csv"
if csv_path.exists():
    lines = csv_path.read_text(encoding="utf-8").strip().split("\n")
    trade_count = len(lines) - 1  # subtract header
    print(f"\n{'='*60}")
    print(f"Result: {trade_count} trades")
    if trade_count > 0:
        print("First 5 trades:")
        for line in lines[1:6]:
            print(f"  {line}")
    print(f"{'='*60}")
else:
    print("\nNo results.csv found!")

print(f"\nRun ID: {run_id}")
print(f"Dashboard: http://localhost:5000/results/{run_id}")

sys.exit(0 if success else 1)
