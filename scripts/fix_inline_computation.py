"""
Replace custom TEMA/derivative include files with inline computation.

D02 works because it computes TEMA inline using AmiBroker's built-in EMA:
  ema1 = EMA(Close, temaLength);
  ema2 = EMA(ema1, temaLength);
  ema3 = EMA(ema2, temaLength);
  temas = 3 * ema1 - 3 * ema2 + ema3;

The custom tema.afl include uses session-reset loops that may not work
correctly on NQ native 1-minute data. This script replaces all includes
with inline computation.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"

# All named strategies to fix
AFL_FILES = [
    "A01_tema_adx_trend.afl",
    "A02_tema_adx_mean_reversion.afl",
    "A03_consolidation_breakout.afl",
    "A04_consolidation_fade.afl",
    "A05_vwap_band_bounce.afl",
    "A06_vwap_tema_confluence.afl",
    "A07_rangebound_vwap_reversion.afl",
    "A08_derivative_reversal.afl",
    "A09_derivative_adx_weakening.afl",
    "A10_full_stack.afl",
    "B03_bb_rsi_reversion.afl",
    "B04_stochastic_range.afl",
    "B06_vwap_enhanced_reversion.afl",
    "B07_donchian_breakout.afl",
    "B09_adx_ema_crossover.afl",
    "B13_ny_momentum_carryover.afl",
    "C01_triple_filter.afl",
    "C02_vwap_derivative_precision.afl",
    "C03_rangebound_stochastic.afl",
    "C05_tema_vwap_session_fade.afl",
    "C06_adx_regime_switch.afl",
    "C08_vwap_cloud_breakout_adx.afl",
    "C09_double_touch_reversal.afl",
    "D01_derivative_basic.afl",
]

# Inline TEMA computation (replaces tema.afl include)
INLINE_TEMA = """// ---- TEMA Indicator (inline, using AmiBroker built-in EMA) ----
ema1 = EMA(Close, {tema_var});
ema2 = EMA(ema1, {tema_var});
ema3 = EMA(ema2, {tema_var});
temas = 3 * ema1 - 3 * ema2 + ema3;"""

# Inline StdDev exit computation (replaces stdev_exit.afl include)
INLINE_STDEV = """// ---- StdDev Exit Levels (inline) ----
exitStdDev = StDev(Close, sdLookback);
exitDistance = sdMultiplier * exitStdDev;"""

# Inline derivative computation with peak/trough detection
INLINE_DERIVATIVE = """// ---- Derivative Calculation (inline) ----
firstDeriv  = (temas - Ref(temas, -lookback)) / lookback;
secondDeriv = firstDeriv - Ref(firstDeriv, -lookback);

// ---- Peak/Trough Detection ----
slopeChangePosToNeg = Ref(firstDeriv, -1) > 0 AND firstDeriv <= 0;
slopeChangeNegToPos = Ref(firstDeriv, -1) < 0 AND firstDeriv >= 0;
isPeak   = slopeChangePosToNeg AND (secondDeriv < 0);
isTrough = slopeChangeNegToPos AND (secondDeriv > 0);

// ---- Min-separation filter ----
validPeak   = isPeak AND BarsSince(Ref(isPeak, -1)) >= minSeparation AND BarsSince(Ref(isTrough, -1)) >= minSeparation;
validTrough = isTrough AND BarsSince(Ref(isTrough, -1)) >= minSeparation AND BarsSince(Ref(isPeak, -1)) >= minSeparation;

// ---- Peak/Trough levels ----
peakLevel = ValueWhen(validPeak, temas);
troughLevel = ValueWhen(validTrough, temas);"""


def replace_tema_include(content: str) -> str:
    """Replace tema.afl include with inline TEMA computation."""
    # Find the TEMA length variable name
    tema_var = "temaLength"
    m = re.search(r'smoothingLength\s*=\s*(\w+)\s*;', content)
    if m:
        tema_var = m.group(1)

    # Remove smoothingLength and sourcePrice assignments
    content = re.sub(r'smoothingLength\s*=\s*\w+\s*;\n', '', content)
    content = re.sub(r'sourcePrice\s*=\s*\w+\s*;\n', '', content)

    # Replace the include with inline computation
    content = re.sub(
        r'//.*TEMA Indicator.*\n.*\n?.*#include_once.*tema\.afl["\n]',
        INLINE_TEMA.format(tema_var=tema_var) + '\n',
        content
    )
    # Try simpler pattern if above didn't match
    content = re.sub(
        r'#include_once\s+".*?indicators[/\\]tema\.afl"',
        INLINE_TEMA.format(tema_var=tema_var),
        content
    )

    return content


def replace_stdev_include(content: str) -> str:
    """Replace stdev_exit.afl include with inline computation."""
    content = re.sub(
        r'#include_once\s+".*?indicators[/\\]stdev_exit\.afl"',
        INLINE_STDEV,
        content
    )
    return content


def replace_derivative_include(content: str) -> str:
    """Replace derivative_lookback.afl include with inline computation."""
    # Remove old include comments and line
    content = re.sub(
        r'//.*Derivative Lookback.*\n//.*Requires.*\n.*#include_once.*derivative_lookback\.afl["\n]',
        INLINE_DERIVATIVE + '\n',
        content
    )
    content = re.sub(
        r'#include_once\s+".*?indicators[/\\]derivative_lookback\.afl"',
        INLINE_DERIVATIVE,
        content
    )
    return content


def ensure_target_symbol_in_signals(content: str) -> str:
    """Ensure isTargetSymbol is in all entry signal conditions."""
    lines = content.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Check buy/short signal assignments
        if re.match(r'^buySignal\s*=', stripped) and 'isTargetSymbol' not in stripped:
            if 'AND' in stripped:
                lines[i] = line.rstrip().rstrip(';') + ' AND isTargetSymbol;'
            else:
                # Simple assignment like "buySignal = validTrough;"
                val = re.match(r'^(buySignal\s*=\s*)(.+);', stripped)
                if val:
                    lines[i] = f'{val.group(1)}{val.group(2)} AND isTargetSymbol;'
        elif re.match(r'^shortSignal\s*=', stripped) and 'isTargetSymbol' not in stripped:
            if 'AND' in stripped:
                lines[i] = line.rstrip().rstrip(';') + ' AND isTargetSymbol;'
            else:
                val = re.match(r'^(shortSignal\s*=\s*)(.+);', stripped)
                if val:
                    lines[i] = f'{val.group(1)}{val.group(2)} AND isTargetSymbol;'
    return '\n'.join(lines)


def remove_leftover_session_filters(content: str) -> str:
    """Remove any remaining session filter references."""
    # Remove earlyAsian
    content = re.sub(r'earlyAsian\s*=\s*\([^)]+\)\s*;\n?', '', content)
    content = re.sub(r'\s*AND\s+earlyAsian', '', content)

    # Remove londonSession
    content = re.sub(r'londonSession\s*=\s*\([^)]+\)\s*;\n?', '', content)
    content = re.sub(r'\s*AND\s+londonSession', '', content)

    # Remove londonTransition
    content = re.sub(r'londonTransition\s*=\s*\([^)]+\)\s*;\n?', '', content)
    content = re.sub(r'\s*AND\s+londonTransition', '', content)
    content = re.sub(r'\s*AND\s+NOT\s+londonTransition', '', content)

    return content


def clean_unused_vars(content: str) -> str:
    """Remove unused tn = TimeNum() if no longer referenced."""
    # Check if tn is used anywhere beyond the assignment
    tn_uses = len(re.findall(r'\btn\b', content))
    tn_assigns = len(re.findall(r'tn\s*=\s*TimeNum\(\)', content))
    if tn_uses <= tn_assigns:
        content = re.sub(r'tn\s*=\s*TimeNum\(\);\n?', '', content)
    return content


def main():
    print(f"Fixing {len(AFL_FILES)} strategy files with inline computation...")

    for filename in AFL_FILES:
        filepath = STRATEGIES_DIR / filename
        if not filepath.exists():
            print(f"  SKIP: {filename} not found")
            continue

        content = filepath.read_text(encoding="utf-8")
        original = content

        # Apply fixes
        if "tema.afl" in content:
            content = replace_tema_include(content)
        if "stdev_exit.afl" in content:
            content = replace_stdev_include(content)
        if "derivative_lookback.afl" in content:
            content = replace_derivative_include(content)

        content = ensure_target_symbol_in_signals(content)
        content = remove_leftover_session_filters(content)
        content = clean_unused_vars(content)

        # Clean up multiple blank lines
        content = re.sub(r'\n{4,}', '\n\n\n', content)

        if content != original:
            filepath.write_text(content, encoding="utf-8")
            print(f"  FIXED: {filename}")
        else:
            print(f"  NO CHANGE: {filename}")

    print("\nDone!")


if __name__ == "__main__":
    main()
