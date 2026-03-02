"""
Round 3: Fix remaining 9 zero-trade strategies.

Root cause: vwap_clouds.afl defaults to sessionResetTime=0 (midnight) when
included standalone. But C06 works because consolidation_zones.afl (included
first) sets sessionResetTime=93000 (9:30 AM EST / US market open).

Fix: Set sessionResetTime = 93000 before #include_once for vwap_clouds.afl
and range_bound.afl in all affected strategies.

Also fix B04/C03 stochastic timing: use Ref(sk,-1) to check OS/OB zone.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"


def fix_file(filename: str, fixes: list):
    """Apply a list of (old, new) replacements to a strategy file."""
    filepath = STRATEGIES_DIR / filename
    if not filepath.exists():
        print(f"  SKIP: {filename} not found")
        return False

    content = filepath.read_text(encoding="utf-8")
    original = content

    for old, new in fixes:
        if old in content:
            content = content.replace(old, new)
        else:
            print(f"  WARNING: Pattern not found in {filename}: {old[:80]}...")

    if content != original:
        filepath.write_text(content, encoding="utf-8")
        print(f"  FIXED: {filename}")
        return True
    else:
        print(f"  NO CHANGE: {filename}")
        return False


def main():
    print("Round 3: Fix session reset time + stochastic timing...\n")

    # ---- A05: VWAP Band Bounce -> set sessionResetTime before include ----
    print("A05: Set sessionResetTime = 93000 before VWAP include")
    fix_file("A05_vwap_band_bounce.afl", [
        (
            '// ---- VWAP Clouds Indicator (from indicator library) ----\n'
            'vwStdev1 = vwapSigma1;\n'
            'vwStdev2 = vwapSigma2;\n'
            'vwStdev3 = vwapSigma3;\n'
            '#include_once',
            '// ---- VWAP Clouds Indicator (from indicator library) ----\n'
            'sessionResetTime = 93000;  // Reset VWAP at US market open (9:30 AM EST)\n'
            'vwStdev1 = vwapSigma1;\n'
            'vwStdev2 = vwapSigma2;\n'
            'vwStdev3 = vwapSigma3;\n'
            '#include_once'
        ),
    ])

    # ---- A06: VWAP + TEMA Confluence -> set sessionResetTime ----
    print("A06: Set sessionResetTime = 93000 before VWAP include")
    fix_file("A06_vwap_tema_confluence.afl", [
        (
            '// ---- VWAP Clouds Indicator ----\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once',
            '// ---- VWAP Clouds Indicator ----\n'
            'sessionResetTime = 93000;  // Reset VWAP at US market open (9:30 AM EST)\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once'
        ),
    ])

    # ---- A07: RangeBound + VWAP -> set sessionResetTime before both includes ----
    print("A07: Set sessionResetTime = 93000 before range_bound and VWAP includes")
    fix_file("A07_rangebound_vwap_reversion.afl", [
        (
            '// ---- Range-Bound Detection Indicator ----\n'
            '// NQ is more volatile - widen range detection thresholds\n'
            'rangeThreshold = 3.0;',
            '// ---- Range-Bound Detection Indicator ----\n'
            '// NQ is more volatile - widen range detection thresholds\n'
            'sessionResetTime = 93000;  // Reset at US market open (9:30 AM EST)\n'
            'rangeThreshold = 3.0;'
        ),
    ])

    # ---- B04: Stochastic Range -> fix stochastic OS/OB timing ----
    print("B04: Use Ref(sk,-1) for oversold/overbought zone check")
    fix_file("B04_stochastic_range.afl", [
        (
            "buySignal   = Cross(sk, sd) AND sk < stochOS AND rangeConfirmed AND isTargetSymbol;\n"
            "shortSignal = Cross(sd, sk) AND sk > stochOB AND rangeConfirmed AND isTargetSymbol;",
            "buySignal   = Cross(sk, sd) AND Ref(sk, -1) < stochOS AND rangeConfirmed AND isTargetSymbol;\n"
            "shortSignal = Cross(sd, sk) AND Ref(sk, -1) > stochOB AND rangeConfirmed AND isTargetSymbol;"
        ),
    ])

    # ---- B06: VWAP Enhanced Reversion -> set sessionResetTime ----
    print("B06: Set sessionResetTime = 93000 before VWAP include")
    fix_file("B06_vwap_enhanced_reversion.afl", [
        (
            '// ---- VWAP Clouds Indicator (from indicator library) ----\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once',
            '// ---- VWAP Clouds Indicator (from indicator library) ----\n'
            'sessionResetTime = 93000;  // Reset VWAP at US market open (9:30 AM EST)\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once'
        ),
    ])

    # ---- C02: VWAP + Derivative Precision -> set sessionResetTime ----
    print("C02: Set sessionResetTime = 93000 before VWAP include")
    fix_file("C02_vwap_derivative_precision.afl", [
        (
            '// ---- VWAP Clouds Indicator (from indicator library) ----\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once',
            '// ---- VWAP Clouds Indicator (from indicator library) ----\n'
            'sessionResetTime = 93000;  // Reset VWAP at US market open (9:30 AM EST)\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once'
        ),
    ])

    # ---- C03: RangeBound + Stochastic -> set sessionResetTime + fix stoch timing ----
    print("C03: Set sessionResetTime + fix stochastic timing")
    fix_file("C03_rangebound_stochastic.afl", [
        (
            '// ---- Range-Bound Detection Indicator ----\n'
            '// NQ is more volatile - widen range detection thresholds\n'
            'rangeThreshold = 3.0;',
            '// ---- Range-Bound Detection Indicator ----\n'
            '// NQ is more volatile - widen range detection thresholds\n'
            'sessionResetTime = 93000;  // Reset at US market open (9:30 AM EST)\n'
            'rangeThreshold = 3.0;'
        ),
        # Fix stochastic timing: check previous bar for OS/OB zone
        (
            "buySignal   = isRangeBound AND Cross(sk, sd) AND sk < stochOS AND isTargetSymbol;\n"
            "shortSignal = isRangeBound AND Cross(sd, sk) AND sk > stochOB AND isTargetSymbol;",
            "buySignal   = isRangeBound AND Cross(sk, sd) AND Ref(sk, -1) < stochOS AND isTargetSymbol;\n"
            "shortSignal = isRangeBound AND Cross(sd, sk) AND Ref(sk, -1) > stochOB AND isTargetSymbol;"
        ),
    ])

    # ---- C05: TEMA + VWAP Session Fade -> set sessionResetTime ----
    print("C05: Set sessionResetTime = 93000 before VWAP include")
    fix_file("C05_tema_vwap_session_fade.afl", [
        (
            '// ---- VWAP Clouds Indicator ----\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once',
            '// ---- VWAP Clouds Indicator ----\n'
            'sessionResetTime = 93000;  // Reset VWAP at US market open (9:30 AM EST)\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once'
        ),
    ])

    # ---- C08: VWAP Cloud Breakout + ADX -> set sessionResetTime ----
    print("C08: Set sessionResetTime = 93000 before VWAP include")
    fix_file("C08_vwap_cloud_breakout_adx.afl", [
        (
            '// ---- VWAP Clouds Indicator ----\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once',
            '// ---- VWAP Clouds Indicator ----\n'
            'sessionResetTime = 93000;  // Reset VWAP at US market open (9:30 AM EST)\n'
            'vwStdev1 = 1;\n'
            'vwStdev2 = 2;\n'
            'vwStdev3 = 3;\n'
            '#include_once'
        ),
    ])

    print("\nDone! Round 3 fixes applied.")


if __name__ == "__main__":
    main()
