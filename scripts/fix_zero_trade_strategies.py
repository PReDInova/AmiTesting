"""
Fix all zero-trade strategies with targeted parameter and logic changes.

Root causes:
1. TEMA(21) too smooth for NQ 1-min data -> change to TEMA(8)
2. ADX thresholds too high -> lower to 15
3. Derivative detection too strict -> shorter lookback, lower min separation
4. BB/RSI/Stochastic thresholds too tight -> widen
5. Remaining session filters -> remove
6. Missing isTargetSymbol in some signals -> add
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"


def fix_a01(content: str) -> str:
    """A01 - TEMA + ADX Trend: shorter TEMA, lower ADX threshold."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    content = content.replace(
        'Param("ADX Threshold", 25, 10, 40, 1)',
        'Param("ADX Threshold", 15, 5, 40, 1)'
    )
    return content


def fix_a02(content: str) -> str:
    """A02 - TEMA + ADX Mean Reversion: shorter TEMA, higher ADX ceiling for no-trend."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    # A02 uses ADX < threshold to detect no-trend (ranging). Increase threshold.
    content = content.replace(
        'Param("ADX Threshold", 20, 10, 40, 1)',
        'Param("ADX Threshold", 25, 10, 40, 1)'
    )
    return content


def fix_a05(content: str) -> str:
    """A05 - VWAP Band Bounce: Use tighter bands for more signals."""
    # The 1-sigma VWAP band might be too far from price on NQ.
    # Let's use 0.5 sigma multipliers for more frequent touches.
    content = content.replace(
        'Param("VWAP Sigma 1", 1.0, 0.5, 2.0, 0.1)',
        'Param("VWAP Sigma 1", 0.5, 0.1, 2.0, 0.1)'
    )
    content = content.replace(
        'Param("VWAP Sigma 2", 2.0, 1.0, 3.0, 0.1)',
        'Param("VWAP Sigma 2", 1.0, 0.5, 3.0, 0.1)'
    )
    content = content.replace(
        'Param("VWAP Sigma 3", 3.0, 2.0, 4.0, 0.1)',
        'Param("VWAP Sigma 3", 1.5, 1.0, 4.0, 0.1)'
    )
    return content


def fix_a06(content: str) -> str:
    """A06 - VWAP + TEMA Confluence: shorter TEMA."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    return content


def fix_a07(content: str) -> str:
    """A07 - Range-Bound + VWAP Mean Reversion: loosen range-bound detection."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    # Increase range threshold for NQ (NQ is more volatile)
    content = content.replace(
        'Param("Range Threshold (ATR mult)", 1.5',
        'Param("Range Threshold (ATR mult)", 3.0'
    )
    # Widen the near-boundary percentage
    content = content.replace(
        'Param("Near Boundary %", 20, 5, 40, 5)',
        'Param("Near Boundary %", 35, 5, 50, 5)'
    )
    return content


def fix_a08(content: str) -> str:
    """A08 - Derivative Reversal: shorter TEMA, shorter lookback, lower separation."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    content = content.replace(
        'Param("Deriv Lookback", 8, 3, 21, 1)',
        'Param("Deriv Lookback", 3, 2, 21, 1)'
    )
    content = content.replace(
        'Param("Min Separation", 10, 3, 30, 1)',
        'Param("Min Separation", 3, 1, 30, 1)'
    )
    # Add isTargetSymbol to entry signals
    content = content.replace(
        'buySignal   = validTrough1m;',
        'buySignal   = validTrough1m AND isTargetSymbol;'
    )
    content = content.replace(
        'shortSignal = validPeak1m;',
        'shortSignal = validPeak1m AND isTargetSymbol;'
    )
    return content


def fix_a09(content: str) -> str:
    """A09 - Derivative + ADX Weakening: shorter TEMA, lower thresholds."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    content = content.replace(
        'Param("Deriv Lookback", 8, 3, 21, 1)',
        'Param("Deriv Lookback", 3, 2, 21, 1)'
    )
    content = content.replace(
        'Param("Min Separation", 10, 3, 30, 1)',
        'Param("Min Separation", 3, 1, 30, 1)'
    )
    content = content.replace(
        'Param("ADX Threshold", 25, 10, 40, 1)',
        'Param("ADX Threshold", 15, 5, 40, 1)'
    )
    return content


def fix_a10(content: str) -> str:
    """A10 - Full Stack (CZ + ADX + TEMA): shorter TEMA, lower ADX."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    content = content.replace(
        'Param("ADX Threshold", 20, 10, 35, 1)',
        'Param("ADX Threshold", 12, 5, 35, 1)'
    )
    return content


def fix_b03(content: str) -> str:
    """B03 - BB + RSI Reversion: widen RSI thresholds, tighter BB."""
    content = content.replace(
        'Param("RSI Overbought", 70, 60, 85, 5)',
        'Param("RSI Overbought", 60, 55, 85, 5)'
    )
    content = content.replace(
        'Param("RSI Oversold", 30, 15, 40, 5)',
        'Param("RSI Oversold", 40, 15, 45, 5)'
    )
    content = content.replace(
        'Param("BB StdDev", 2.0, 1.0, 3.0, 0.1)',
        'Param("BB StdDev", 1.5, 0.5, 3.0, 0.1)'
    )
    return content


def fix_b04(content: str) -> str:
    """B04 - Stochastic Range: widen Stochastic zones, raise ADX ceiling."""
    content = content.replace(
        'Param("Stoch Overbought", 80, 65, 90, 5)',
        'Param("Stoch Overbought", 70, 60, 90, 5)'
    )
    content = content.replace(
        'Param("Stoch Oversold", 20, 10, 35, 5)',
        'Param("Stoch Oversold", 30, 10, 40, 5)'
    )
    # ADX < threshold means ranging market. Raise ceiling.
    content = content.replace(
        'Param("ADX Threshold", 25, 10, 40, 1)',
        'Param("ADX Threshold", 30, 10, 40, 1)'
    )
    return content


def fix_b06(content: str) -> str:
    """B06 - VWAP Enhanced Reversion: shorter TEMA, tighter VWAP bands."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    # Tighter VWAP bands for NQ
    content = content.replace(
        'Param("Entry Band Sigma", 1, 1, 3, 1)',
        'Param("Entry Band Sigma", 1, 1, 3, 1)'
    )
    content = content.replace(
        'Param("VWAP Sigma 1", 1.0, 0.5, 2.0, 0.1)',
        'Param("VWAP Sigma 1", 0.5, 0.1, 2.0, 0.1)'
    )
    return content


def fix_b07(content: str) -> str:
    """B07 - Donchian Breakout: lower ADX threshold, smaller ATR buffer."""
    content = content.replace(
        'Param("ADX Threshold", 20, 10, 35, 1)',
        'Param("ADX Threshold", 12, 5, 35, 1)'
    )
    content = content.replace(
        'Param("ATR Buffer Mult", 0.5, 0.1, 2.0, 0.1)',
        'Param("ATR Buffer Mult", 0.2, 0.1, 2.0, 0.1)'
    )
    content = content.replace(
        'Param("Donchian Period", 32, 15, 60, 1)',
        'Param("Donchian Period", 20, 10, 60, 1)'
    )
    return content


def fix_b13(content: str) -> str:
    """B13 - NY Momentum Carryover: remove earlyAsian filter, shorter TEMA."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    # Remove the earlyAsian session filter
    content = re.sub(r'earlyAsian\s*=\s*\(tn >= 180000 AND tn <= 210000\);\n', '', content)
    content = re.sub(r'\s*AND\s+earlyAsian', '', content)
    return content


def fix_c01(content: str) -> str:
    """C01 - Triple Filter (CZ + VWAP + ADX): lower ADX, shorter ADX rise lookback."""
    content = content.replace(
        'Param("ADX Threshold", 20, 10, 35, 1)',
        'Param("ADX Threshold", 12, 5, 35, 1)'
    )
    content = content.replace(
        'Param("ADX Rise Lookback", 5, 2, 10, 1)',
        'Param("ADX Rise Lookback", 2, 1, 10, 1)'
    )
    return content


def fix_c02(content: str) -> str:
    """C02 - VWAP + Derivative Precision: shorter TEMA, tighter VWAP, lower min separation."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    content = content.replace(
        'Param("Deriv Lookback", 8, 3, 21, 1)',
        'Param("Deriv Lookback", 3, 2, 21, 1)'
    )
    content = content.replace(
        'Param("Min Separation", 10, 3, 30, 1)',
        'Param("Min Separation", 3, 1, 30, 1)'
    )
    # Tighter VWAP bands
    content = content.replace(
        'Param("VWAP Sigma 1", 1.0, 0.5, 2.0, 0.1)',
        'Param("VWAP Sigma 1", 0.5, 0.1, 2.0, 0.1)'
    )
    return content


def fix_c03(content: str) -> str:
    """C03 - Range-Bound Stochastic: loosen range detection, widen Stochastic zones."""
    content = content.replace(
        'Param("Range Threshold (ATR mult)", 1.5',
        'Param("Range Threshold (ATR mult)", 3.0'
    )
    content = content.replace(
        'Param("Stoch Overbought", 80, 65, 90, 5)',
        'Param("Stoch Overbought", 70, 60, 90, 5)'
    )
    content = content.replace(
        'Param("Stoch Oversold", 20, 10, 35, 5)',
        'Param("Stoch Oversold", 30, 10, 40, 5)'
    )
    return content


def fix_c05(content: str) -> str:
    """C05 - TEMA + VWAP Session Fade: shorter TEMA, add isTargetSymbol."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    # Add isTargetSymbol to combined entry signals
    content = content.replace(
        'buySignal   = buyPhase1 OR buyPhase2;',
        'buySignal   = (buyPhase1 OR buyPhase2) AND isTargetSymbol;'
    )
    content = content.replace(
        'shortSignal = shortPhase1 OR shortPhase2;',
        'shortSignal = (shortPhase1 OR shortPhase2) AND isTargetSymbol;'
    )
    return content


def fix_c08(content: str) -> str:
    """C08 - VWAP Cloud Breakout + ADX: shorter TEMA, lower ADX, tighter VWAP bands."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    content = content.replace(
        'Param("ADX Threshold", 20, 10, 35, 1)',
        'Param("ADX Threshold", 12, 5, 35, 1)'
    )
    # Use 1-sigma instead of 2/3-sigma for breakout bands
    content = content.replace(
        'Param("Breakout Band Min", 2, 1, 3, 1)',
        'Param("Breakout Band Min", 1, 1, 3, 1)'
    )
    return content


def fix_c09(content: str) -> str:
    """C09 - Double Touch Reversal: shorter TEMA, lower lookback/separation."""
    content = content.replace(
        'Param("TEMA Length", 21, 5, 100, 1)',
        'Param("TEMA Length", 8, 3, 50, 1)'
    )
    content = content.replace(
        'Param("Deriv Lookback", 8, 3, 21, 1)',
        'Param("Deriv Lookback", 3, 2, 21, 1)'
    )
    content = content.replace(
        'Param("Min Separation", 10, 3, 30, 1)',
        'Param("Min Separation", 3, 1, 30, 1)'
    )
    # Wider retest tolerance and more bars allowed for double touch
    content = content.replace(
        'Param("Max Retest Bars", 30, 10, 100, 5)',
        'Param("Max Retest Bars", 60, 10, 200, 5)'
    )
    content = content.replace(
        'Param("Level Tolerance", 0.5, 0.1, 2.0, 0.1)',
        'Param("Level Tolerance", 5.0, 0.1, 20.0, 0.5)'
    )
    return content


def fix_d01(content: str) -> str:
    """D01 - Derivative Basic: shorter TEMA, shorter lookback, lower separation."""
    # D01 uses Optimize() instead of Param() for some parameters
    content = content.replace(
        'Optimize("TEMA Length", 21, 3, 30, 1)',
        'Param("TEMA Length", 8, 3, 30, 1)'
    )
    content = content.replace(
        'Optimize("Deriv Lookback", 8, 2, 21, 1)',
        'Param("Deriv Lookback", 3, 2, 21, 1)'
    )
    content = content.replace(
        'Param("Min Separation", 10, 3, 30, 1)',
        'Param("Min Separation", 3, 1, 30, 1)'
    )
    # Add isTargetSymbol to entry signals
    content = content.replace(
        'buySignal   = validTrough;',
        'buySignal   = validTrough AND isTargetSymbol;'
    )
    content = content.replace(
        'shortSignal = validPeak;',
        'shortSignal = validPeak AND isTargetSymbol;'
    )
    return content


# Map strategy prefix to fix function
FIXES = {
    "A01": fix_a01,
    "A02": fix_a02,
    "A05": fix_a05,
    "A06": fix_a06,
    "A07": fix_a07,
    "A08": fix_a08,
    "A09": fix_a09,
    "A10": fix_a10,
    "B03": fix_b03,
    "B04": fix_b04,
    "B06": fix_b06,
    "B07": fix_b07,
    "B13": fix_b13,
    "C01": fix_c01,
    "C02": fix_c02,
    "C03": fix_c03,
    "C05": fix_c05,
    "C08": fix_c08,
    "C09": fix_c09,
    "D01": fix_d01,
}

# AFL filenames
AFL_FILES = {
    "A01": "A01_tema_adx_trend.afl",
    "A02": "A02_tema_adx_mean_reversion.afl",
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
    "B13": "B13_ny_momentum_carryover.afl",
    "C01": "C01_triple_filter.afl",
    "C02": "C02_vwap_derivative_precision.afl",
    "C03": "C03_rangebound_stochastic.afl",
    "C05": "C05_tema_vwap_session_fade.afl",
    "C08": "C08_vwap_cloud_breakout_adx.afl",
    "C09": "C09_double_touch_reversal.afl",
    "D01": "D01_derivative_basic.afl",
}


def main():
    print(f"Fixing {len(FIXES)} zero-trade strategies...")

    for prefix, fix_fn in sorted(FIXES.items()):
        afl_file = STRATEGIES_DIR / AFL_FILES[prefix]
        if not afl_file.exists():
            print(f"  SKIP: {afl_file.name} not found")
            continue

        content = afl_file.read_text(encoding="utf-8")
        fixed = fix_fn(content)

        if fixed != content:
            afl_file.write_text(fixed, encoding="utf-8")
            print(f"  FIXED: {prefix} ({afl_file.name})")
        else:
            print(f"  NO CHANGE: {prefix} (fix patterns not found)")

    print(f"\nDone! Fixed {len(FIXES)} strategy files.")


if __name__ == "__main__":
    main()
