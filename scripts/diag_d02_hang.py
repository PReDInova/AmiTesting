"""
Diagnostic script: D02 hang at 67% investigation.

Tests four variants to isolate the cause:
  Test 1: D02 without CBT (no SetCustomBacktestProc) -- should complete fine
  Test 2: D02 with CBT but NO inner for-loop (vectorized min/max) -- should complete
  Test 3: D02 with CBT + inner for-loop but guarded bar indices -- may hang
  Test 4: D02 without StaticVarSet (no per-symbol storage) + no CBT -- baseline

The 67% hang point corresponds to AmiBroker entering the Custom Backtest
Procedure phase.  If Test 1 passes and Test 3 hangs, the CBT is confirmed
as the root cause.
"""

import os
import re
import sys
import time
import uuid
import signal
import logging
import subprocess
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

setup_logging()
logger = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parent.parent
APX_DIR = PROJECT / "apx"
RESULTS_BASE = PROJECT / "results"

# ============================================================
# Shared AFL base (no CBT, no StaticVarSet)
# ============================================================
AFL_BASE = r"""
// D02 - NQ Derivative TEMA Zero-Cross (diagnostic variant)

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
"""

# ============================================================
# Test 1: No CBT, no StaticVarSet (clean baseline)
# ============================================================
AFL_TEST1_NO_CBT = AFL_BASE + r"""
// TEST 1: No CBT, no StaticVarSet -- clean baseline
Plot(Close, "Price", colorDefault, styleCandle);
Title = Name() + " | D02 TEST1 (no CBT)";
"""

# ============================================================
# Test 2: With StaticVarSet but NO CBT
# ============================================================
AFL_TEST2_STATICVAR_NO_CBT = AFL_BASE + r"""
// TEST 2: StaticVarSet per symbol but NO CBT
temaSlope = temas - Ref(temas, -1);
StaticVarSet("d02_fd_" + Name(), firstDeriv);
StaticVarSet("d02_sd_" + Name(), secondDeriv);
StaticVarSet("d02_ts_" + Name(), temaSlope);

Plot(Close, "Price", colorDefault, styleCandle);
Title = Name() + " | D02 TEST2 (StaticVar, no CBT)";
"""

# ============================================================
# Test 3: CBT with vectorized min/max (no inner for-loop)
# ============================================================
AFL_TEST3_CBT_NO_LOOP = AFL_BASE + r"""
// TEST 3: CBT but WITHOUT the bar-by-bar for loop
temaSlope = temas - Ref(temas, -1);
StaticVarSet("d02_fd_" + Name(), firstDeriv);
StaticVarSet("d02_sd_" + Name(), secondDeriv);
StaticVarSet("d02_ts_" + Name(), temaSlope);

SetCustomBacktestProc("");

if (Status("action") == actionPortfolio)
{
    bo = GetBacktesterObject();
    bo.Backtest();

    _metricsPath = "C:\\Users\\prestondinova\\Documents\\AmiTesting\\results\\d02_diag_test3.csv";

    fh = fopen(_metricsPath, "w");
    if (fh)
    {
        fputs("EntryDate,1stDeriv@Entry,2ndDeriv@Entry,TEMASlope@Entry,1stDeriv@Exit,2ndDeriv@Exit\n", fh);

        for (trade = bo.GetFirstTrade(); trade; trade = bo.GetNextTrade())
        {
            sym = trade.Symbol;
            SetForeign(sym);

            _fd = StaticVarGet("d02_fd_" + sym);
            _sd = StaticVarGet("d02_sd_" + sym);
            _ts = StaticVarGet("d02_ts_" + sym);

            eDT = trade.EntryDateTime;
            xDT = trade.ExitDateTime;

            fdEntry = Nz(Lookup(_fd, eDT, 1));
            sdEntry = Nz(Lookup(_sd, eDT, 1));
            tsEntry = Nz(Lookup(_ts, eDT, 1));
            fdExit  = Nz(Lookup(_fd, xDT, 1));
            sdExit  = Nz(Lookup(_sd, xDT, 1));

            // NO inner for-loop -- skip min/max calculation
            RestorePriceArrays();

            fputs(DateTimeToStr(eDT) + "," +
                  NumToStr(fdEntry, 1.6) + "," +
                  NumToStr(sdEntry, 1.6) + "," +
                  NumToStr(tsEntry, 1.6) + "," +
                  NumToStr(fdExit, 1.6) + "," +
                  NumToStr(sdExit, 1.6) + "\n", fh);
        }

        fclose(fh);
    }
}

Plot(Close, "Price", colorDefault, styleCandle);
Title = Name() + " | D02 TEST3 (CBT, no loop)";
"""

# ============================================================
# Test 4: Full original CBT WITH inner for-loop (expected to hang)
# ============================================================
AFL_TEST4_CBT_WITH_LOOP = AFL_BASE + r"""
// TEST 4: Full CBT WITH bar-by-bar inner for-loop (ORIGINAL CODE)
temaSlope = temas - Ref(temas, -1);
StaticVarSet("d02_fd_" + Name(), firstDeriv);
StaticVarSet("d02_sd_" + Name(), secondDeriv);
StaticVarSet("d02_ts_" + Name(), temaSlope);

SetCustomBacktestProc("");

if (Status("action") == actionPortfolio)
{
    bo = GetBacktesterObject();
    bo.Backtest();

    _metricsPath = "C:\\Users\\prestondinova\\Documents\\AmiTesting\\results\\d02_diag_test4.csv";

    fh = fopen(_metricsPath, "w");
    if (fh)
    {
        fputs("EntryDate,1stDeriv@Entry,2ndDeriv@Entry,TEMASlope@Entry,1stDeriv@Exit,2ndDeriv@Exit,1stDeriv Min,1stDeriv Max,2ndDeriv Min,2ndDeriv Max\n", fh);

        for (trade = bo.GetFirstTrade(); trade; trade = bo.GetNextTrade())
        {
            sym = trade.Symbol;
            SetForeign(sym);

            _fd = StaticVarGet("d02_fd_" + sym);
            _sd = StaticVarGet("d02_sd_" + sym);
            _ts = StaticVarGet("d02_ts_" + sym);

            eDT = trade.EntryDateTime;
            xDT = trade.ExitDateTime;

            fdEntry = Nz(Lookup(_fd, eDT, 1));
            sdEntry = Nz(Lookup(_sd, eDT, 1));
            tsEntry = Nz(Lookup(_ts, eDT, 1));
            fdExit  = Nz(Lookup(_fd, xDT, 1));
            sdExit  = Nz(Lookup(_sd, xDT, 1));

            // Bar-by-bar min/max (THIS IS THE SUSPECTED HANG)
            eIdx = Nz(Lookup(BarIndex(), eDT, 1));
            xIdx = Nz(Lookup(BarIndex(), xDT, 1));

            fdMin = fdEntry; fdMax = fdEntry;
            sdMin = sdEntry; sdMax = sdEntry;
            if (eIdx < xIdx)
            {
                for (i = eIdx; i <= xIdx; i++)
                {
                    tmpFd = Nz(_fd[i]);
                    if (tmpFd < fdMin) fdMin = tmpFd;
                    if (tmpFd > fdMax) fdMax = tmpFd;
                    tmpSd = Nz(_sd[i]);
                    if (tmpSd < sdMin) sdMin = tmpSd;
                    if (tmpSd > sdMax) sdMax = tmpSd;
                }
            }

            RestorePriceArrays();

            fputs(DateTimeToStr(eDT) + "," +
                  NumToStr(fdEntry, 1.6) + "," +
                  NumToStr(sdEntry, 1.6) + "," +
                  NumToStr(tsEntry, 1.6) + "," +
                  NumToStr(fdExit, 1.6) + "," +
                  NumToStr(sdExit, 1.6) + "," +
                  NumToStr(fdMin, 1.6) + "," +
                  NumToStr(fdMax, 1.6) + "," +
                  NumToStr(sdMin, 1.6) + "," +
                  NumToStr(sdMax, 1.6) + "\n", fh);
        }

        fclose(fh);
    }
}

Plot(Close, "Price", colorDefault, styleCandle);
Title = Name() + " | D02 TEST4 (CBT + for-loop, ORIGINAL)";
"""

# ============================================================
# Test 5: CBT with FIXED for-loop (capped iterations + guarded indices)
# ============================================================
AFL_TEST5_CBT_FIXED_LOOP = AFL_BASE + r"""
// TEST 5: CBT with FIXED for-loop -- capped iterations and guarded bar indices
temaSlope = temas - Ref(temas, -1);
StaticVarSet("d02_fd_" + Name(), firstDeriv);
StaticVarSet("d02_sd_" + Name(), secondDeriv);
StaticVarSet("d02_ts_" + Name(), temaSlope);

SetCustomBacktestProc("");

if (Status("action") == actionPortfolio)
{
    bo = GetBacktesterObject();
    bo.Backtest();

    _metricsPath = "C:\\Users\\prestondinova\\Documents\\AmiTesting\\results\\d02_diag_test5.csv";

    fh = fopen(_metricsPath, "w");
    if (fh)
    {
        fputs("EntryDate,1stDeriv@Entry,2ndDeriv@Entry,TEMASlope@Entry,1stDeriv@Exit,2ndDeriv@Exit,1stDeriv Min,1stDeriv Max,2ndDeriv Min,2ndDeriv Max\n", fh);

        for (trade = bo.GetFirstTrade(); trade; trade = bo.GetNextTrade())
        {
            sym = trade.Symbol;
            SetForeign(sym);

            _fd = StaticVarGet("d02_fd_" + sym);
            _sd = StaticVarGet("d02_sd_" + sym);
            _ts = StaticVarGet("d02_ts_" + sym);

            eDT = trade.EntryDateTime;
            xDT = trade.ExitDateTime;

            fdEntry = Nz(Lookup(_fd, eDT, 1));
            sdEntry = Nz(Lookup(_sd, eDT, 1));
            tsEntry = Nz(Lookup(_ts, eDT, 1));
            fdExit  = Nz(Lookup(_fd, xDT, 1));
            sdExit  = Nz(Lookup(_sd, xDT, 1));

            // FIXED: Guard bar index lookups and cap maximum iterations
            eIdx = Nz(Lookup(BarIndex(), eDT, 1));
            xIdx = Nz(Lookup(BarIndex(), xDT, 1));
            maxBars = BarCount;

            fdMin = fdEntry; fdMax = fdEntry;
            sdMin = sdEntry; sdMax = sdEntry;

            // Guard: both indices must be valid and the span must be reasonable
            // Max 5000 bars between entry/exit (at 1-min, ~3.5 trading days)
            _maxSpan = 5000;
            if (eIdx > 0 AND xIdx > eIdx AND xIdx < maxBars AND (xIdx - eIdx) < _maxSpan)
            {
                for (i = eIdx; i <= xIdx; i++)
                {
                    tmpFd = Nz(_fd[i]);
                    if (tmpFd < fdMin) fdMin = tmpFd;
                    if (tmpFd > fdMax) fdMax = tmpFd;
                    tmpSd = Nz(_sd[i]);
                    if (tmpSd < sdMin) sdMin = tmpSd;
                    if (tmpSd > sdMax) sdMax = tmpSd;
                }
            }

            RestorePriceArrays();

            fputs(DateTimeToStr(eDT) + "," +
                  NumToStr(fdEntry, 1.6) + "," +
                  NumToStr(sdEntry, 1.6) + "," +
                  NumToStr(tsEntry, 1.6) + "," +
                  NumToStr(fdExit, 1.6) + "," +
                  NumToStr(sdExit, 1.6) + "," +
                  NumToStr(fdMin, 1.6) + "," +
                  NumToStr(fdMax, 1.6) + "," +
                  NumToStr(sdMin, 1.6) + "," +
                  NumToStr(sdMax, 1.6) + "\n", fh);
        }

        fclose(fh);
    }
}

Plot(Close, "Price", colorDefault, styleCandle);
Title = Name() + " | D02 TEST5 (CBT + fixed loop)";
"""


def kill_amibroker():
    """Force-kill all AmiBroker processes."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "Broker.exe"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            logger.info("Force-killed AmiBroker: %s", result.stdout.strip())
        else:
            logger.info("No AmiBroker process to kill: %s", result.stderr.strip())
        time.sleep(2)  # let process fully terminate
    except Exception as exc:
        logger.warning("kill_amibroker error: %s", exc)


def run_test(name, afl_code, timeout_seconds=120):
    """Run a single diagnostic test with a hard timeout.

    Returns dict with test results.
    """
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
        periodicity=5,       # 1-minute native
        symbol="__ALL__",    # ApplyTo=0
    )

    print(f"\n{'='*70}")
    print(f"  TEST: {name}")
    print(f"  Run ID: {run_id}")
    print(f"  Timeout: {timeout_seconds}s")
    print(f"{'='*70}")

    bt = OLEBacktester()
    start = time.time()
    success = False
    timed_out = False

    try:
        # Override max_wait so OLEBacktester times out before our hard kill
        from config import settings
        original_max_wait = settings.BACKTEST_SETTINGS["max_wait"]
        settings.BACKTEST_SETTINGS["max_wait"] = timeout_seconds

        success = bt.run_full_test(
            apx_path=str(apx_path),
            output_dir=str(run_dir),
            run_mode=2,
        )

        settings.BACKTEST_SETTINGS["max_wait"] = original_max_wait

    except Exception as exc:
        logger.error("Test '%s' exception: %s", name, exc)
        timed_out = True

    elapsed = time.time() - start

    # If it took longer than timeout, it hung
    if elapsed >= timeout_seconds - 5:
        timed_out = True

    # Check results
    trade_count = 0
    csv_path = run_dir / "results.csv"
    if csv_path.exists():
        lines = csv_path.read_text(encoding="utf-8").strip().split("\n")
        trade_count = max(0, len(lines) - 1)

    result = {
        "name": name,
        "success": success,
        "timed_out": timed_out,
        "elapsed": elapsed,
        "trades": trade_count,
        "run_id": run_id,
    }

    status = "PASS" if success else ("TIMEOUT/HANG" if timed_out else "FAIL")
    print(f"\n  Result: {status}")
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"  Trades: {trade_count}")
    if timed_out:
        print(f"  ** HUNG at/near 67% -- killing AmiBroker **")
        kill_amibroker()
    print()

    return result


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 70)
    print("D02 HANG DIAGNOSTIC")
    print("Testing what causes the 67% hang on NQ backtests")
    print("=" * 70)

    # Kill any lingering AmiBroker first
    kill_amibroker()

    results = []
    test_timeout = 90  # seconds per test

    # Test 1: No CBT (should pass quickly)
    results.append(run_test(
        "TEST 1: No CBT, no StaticVarSet (clean baseline)",
        AFL_TEST1_NO_CBT,
        timeout_seconds=test_timeout,
    ))
    kill_amibroker()
    time.sleep(3)

    # Test 2: StaticVarSet but no CBT
    results.append(run_test(
        "TEST 2: StaticVarSet per symbol, NO CBT",
        AFL_TEST2_STATICVAR_NO_CBT,
        timeout_seconds=test_timeout,
    ))
    kill_amibroker()
    time.sleep(3)

    # Test 3: CBT without inner for-loop
    results.append(run_test(
        "TEST 3: CBT (no inner for-loop)",
        AFL_TEST3_CBT_NO_LOOP,
        timeout_seconds=test_timeout,
    ))
    kill_amibroker()
    time.sleep(3)

    # Test 4: Full original CBT (expected to hang)
    results.append(run_test(
        "TEST 4: CBT + inner for-loop (ORIGINAL -- expect hang)",
        AFL_TEST4_CBT_WITH_LOOP,
        timeout_seconds=test_timeout,
    ))
    kill_amibroker()
    time.sleep(3)

    # Test 5: CBT with fixed for-loop
    results.append(run_test(
        "TEST 5: CBT + FIXED for-loop (guarded + capped)",
        AFL_TEST5_CBT_FIXED_LOOP,
        timeout_seconds=test_timeout,
    ))
    kill_amibroker()

    # ============================================================
    # Summary
    # ============================================================
    print("\n" + "=" * 70)
    print("DIAGNOSTIC SUMMARY")
    print("=" * 70)
    print(f"{'Test':<55} {'Status':<15} {'Time':>8} {'Trades':>7}")
    print("-" * 70)
    for r in results:
        status = "PASS" if r["success"] else ("TIMEOUT" if r["timed_out"] else "FAIL")
        print(f"{r['name']:<55} {status:<15} {r['elapsed']:>7.1f}s {r['trades']:>7}")

    print("\n" + "=" * 70)
    print("DIAGNOSIS:")

    # Determine root cause based on results
    t1_ok = results[0]["success"]
    t2_ok = results[1]["success"]
    t3_ok = results[2]["success"]
    t4_ok = results[3]["success"]
    t5_ok = results[4]["success"]

    if t1_ok and t2_ok and t3_ok and not t4_ok and t5_ok:
        print("  ROOT CAUSE: The bar-by-bar for-loop in the CBT is hanging AmiBroker.")
        print("  The inner `for (i = eIdx; i <= xIdx; i++)` loop iterates over too many")
        print("  bars on 1-minute NQ data, or eIdx/xIdx are invalid (Nz returns 0).")
        print("  FIX: Use the guarded/capped loop from Test 5, or switch to vectorized")
        print("  operations (HighestSince/LowestSince).")
    elif t1_ok and t2_ok and not t3_ok:
        print("  ROOT CAUSE: The CBT itself is the problem (even without the for-loop).")
        print("  SetForeign + StaticVarGet per trade may be too expensive.")
    elif t1_ok and not t2_ok:
        print("  ROOT CAUSE: StaticVarSet for all symbols is consuming too much memory.")
    elif not t1_ok:
        print("  ROOT CAUSE: The base D02 strategy has issues even without CBT.")
        print("  Check NQ data integrity and time filter parameters.")
    elif t1_ok and t2_ok and t3_ok and not t4_ok and not t5_ok:
        print("  ROOT CAUSE: The for-loop hangs even with guards. The BarIndex lookup")
        print("  after SetForeign may return incorrect values, or BarCount is huge.")
        print("  FIX: Remove the for-loop entirely; use vectorized AFL functions.")
    else:
        print(f"  Unexpected result pattern. t1={t1_ok} t2={t2_ok} t3={t3_ok} t4={t4_ok} t5={t5_ok}")
        print("  Manual investigation needed.")

    print("=" * 70)
