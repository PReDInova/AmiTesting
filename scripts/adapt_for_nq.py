"""
Adapt all named strategies for NQ backtesting.

Transformations:
1. Remove TimeFrameSet(in1Minute) - use native 1-minute data
2. Remove TimeFrameRestore()
3. Remove all TimeFrameExpand() lines
4. Keep xxx1m = xxx alias lines (they become simple copies)
5. Remove Asian session filter, replace with NQ trading window
6. Add Name() == "NQ" symbol filter
7. Update descriptions and titles
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"


def transform_afl(afl_content: str, strategy_name: str) -> str:
    """Transform an AFL strategy for NQ native 1-minute data."""
    lines = afl_content.split("\n")
    new_lines = []
    skip_next_empty = False

    # Track what we're changing for the description update
    had_timeframe_set = False
    had_asian_filter = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 1. Remove TimeFrameSet(in1Minute);
        if "TimeFrameSet(in1Minute)" in stripped or "TimeFrameSet( in1Minute )" in stripped:
            had_timeframe_set = True
            i += 1
            continue

        # 2. Remove sessionResetTime = 180000 (Asian session reset)
        if re.match(r'^sessionResetTime\s*=\s*180000\s*;', stripped):
            i += 1
            # Skip the comment on the same line or next line
            continue

        # 3. Remove TimeFrameRestore();
        if "TimeFrameRestore()" in stripped:
            i += 1
            continue

        # 4. Remove TimeFrameExpand() lines
        if "TimeFrameExpand(" in stripped:
            i += 1
            continue

        # 5. Remove/replace Asian session filter
        # Pattern: asianSession = (tn >= 180000) OR (tn <= 030000);
        if re.match(r'^asianSession\s*=', stripped):
            had_asian_filter = True
            i += 1
            continue

        # Remove "// ---- Asian Session Filter ----" comment
        if "Asian Session Filter" in stripped and stripped.startswith("//"):
            i += 1
            continue

        # Remove standalone "tn = TimeNum();" that was only used for Asian filter
        # But keep it if used for other time filters too
        if stripped == "tn = TimeNum();" and had_asian_filter:
            # Check if tn is used elsewhere beyond Asian filter
            remaining = "\n".join(lines[i+1:])
            if "tn " not in remaining and "tn)" not in remaining and "tn>" not in remaining and "tn<" not in remaining:
                i += 1
                continue

        # 6. Remove "AND asianSession" from entry signals
        if "asianSession" in line:
            line = re.sub(r'\s*AND\s+asianSession', '', line)
            line = re.sub(r'asianSession\s+AND\s*', '', line)

        # 7. Remove "// ---- Save computed arrays before restoring timeframe ----" comments
        if "Save computed arrays before restoring timeframe" in stripped:
            i += 1
            continue

        # 8. Remove "// ---- Expand to base (tick) timeframe ----" comments
        if "Expand to base" in stripped and "timeframe" in stripped:
            i += 1
            continue

        # 9. Remove "// ---- Aggregate ticks into 1-minute bars ----" comments
        if "Aggregate ticks into 1-minute" in stripped:
            i += 1
            continue

        # 10. Remove "// ---- Session reset for Asian session ----" comments
        if "Session reset for Asian session" in stripped:
            i += 1
            continue

        # Skip empty lines that result from removals (avoid triple blank lines)
        if stripped == "" and skip_next_empty:
            skip_next_empty = False
            i += 1
            continue

        new_lines.append(line)
        i += 1

    result = "\n".join(new_lines)

    # Clean up multiple consecutive blank lines
    result = re.sub(r'\n{4,}', '\n\n\n', result)

    # Add NQ symbol filter at the top (after the description comment block)
    # Find the end of the opening comment block
    insert_pos = 0
    in_comment_block = False
    result_lines = result.split("\n")
    for idx, line in enumerate(result_lines):
        stripped = line.strip()
        if stripped.startswith("//"):
            in_comment_block = True
        elif in_comment_block and not stripped.startswith("//") and stripped != "":
            insert_pos = idx
            break

    # Insert symbol filter
    symbol_filter = [
        "// ---- Symbol filter (NQ only) ----",
        'isTargetSymbol = Name() == "NQ";',
        "",
    ]
    result_lines = result_lines[:insert_pos] + symbol_filter + result_lines[insert_pos:]

    # Add isTargetSymbol to entry signal conditions
    # Find Buy = and Short = assignment lines that use signal variables
    for idx, line in enumerate(result_lines):
        stripped = line.strip()
        # Match entry signal definitions (buySignal = ... AND ...)
        if re.match(r'^buySignal\s*=', stripped) and "AND" in stripped:
            result_lines[idx] = line.rstrip().rstrip(";") + " AND isTargetSymbol;"
        elif re.match(r'^shortSignal\s*=', stripped) and "AND" in stripped:
            result_lines[idx] = line.rstrip().rstrip(";") + " AND isTargetSymbol;"
        # For strategies where Buy = directly has conditions
        elif re.match(r'^Buy\s*=.*AND', stripped) and "buySignal" not in stripped:
            result_lines[idx] = line.rstrip().rstrip(";") + " AND isTargetSymbol;"
        elif re.match(r'^Short\s*=.*AND', stripped) and "shortSignal" not in stripped:
            result_lines[idx] = line.rstrip().rstrip(";") + " AND isTargetSymbol;"

    result = "\n".join(result_lines)

    # Update description comments
    result = result.replace(
        "Designed for /GC (Gold Futures) tick data aggregated to 1-minute bars.",
        "Adapted for NQ (Nasdaq E-mini Futures) on native 1-minute data."
    )
    result = result.replace(
        "Designed for /GC (Gold Futures) Asian session (6PM-3AM EST).",
        "Adapted for NQ (Nasdaq E-mini Futures) on native 1-minute data."
    )
    result = result.replace(
        "/GC Gold Futures during the Asian session\n// (6 PM - 3 AM EST)",
        "NQ (Nasdaq E-mini Futures) on native 1-minute data"
    )
    result = result.replace(
        "for /GC Gold Futures during the Asian session\n// (6 PM - 3 AM EST).",
        "for NQ (Nasdaq E-mini Futures) on native 1-minute data."
    )
    result = result.replace(
        "for /GC Gold Futures during the Asian\n// session (6 PM - 3 AM EST).",
        "for NQ (Nasdaq E-mini Futures) on native 1-minute data."
    )
    result = result.replace(
        "/GC Gold Futures during the\n// Asian session (6 PM - 3 AM EST).",
        "NQ (Nasdaq E-mini Futures) on native 1-minute data."
    )

    # Generic GC -> NQ description replacements
    result = re.sub(
        r'for /GC.*?Asian session.*?\.',
        'for NQ (Nasdaq E-mini Futures) on native 1-minute data.',
        result,
        flags=re.DOTALL
    )
    result = re.sub(
        r'Designed for /GC.*?1-minute bars\.',
        'Adapted for NQ (Nasdaq E-mini Futures) on native 1-minute data.',
        result,
    )

    return result


def handle_special_strategies(afl_content: str, name: str) -> str:
    """Handle strategies with non-standard session logic."""

    if "B07" in name:
        # B07 has London session filter - remove it
        afl_content = re.sub(r'londonSession\s*=.*?;', '', afl_content)
        afl_content = re.sub(r'\s*AND\s+londonSession', '', afl_content)
        afl_content = re.sub(r'londonSession\s+AND\s*', '', afl_content)
        # Remove London session comments
        afl_content = re.sub(r'//.*London.*session.*\n', '\n', afl_content, flags=re.IGNORECASE)
        # Remove donchianStart/donchianEnd related to sessions
        afl_content = re.sub(r'//.*Donchian.*Asian.*\n', '\n', afl_content, flags=re.IGNORECASE)

    elif "B13" in name:
        # B13 has NY momentum carryover with earlyAsianSession filter
        afl_content = re.sub(r'earlyAsianSession\s*=.*?;', '', afl_content)
        afl_content = re.sub(r'\s*AND\s+earlyAsianSession', '', afl_content)
        afl_content = re.sub(r'earlyAsianSession\s+AND\s*', '', afl_content)
        # Remove NY session references
        afl_content = re.sub(r'nySessionEnd\s*=.*?;', '', afl_content)
        afl_content = re.sub(r'//.*NY.*session.*\n', '\n', afl_content, flags=re.IGNORECASE)
        afl_content = re.sub(r'//.*early Asian.*\n', '\n', afl_content, flags=re.IGNORECASE)
        # Remove prevNyXxx variables
        afl_content = re.sub(r'prevNy\w+\s*=.*?;', '', afl_content)
        afl_content = re.sub(r'\s*AND\s+prevNy\w+\s*[<>!=]+\s*[\d.]+', '', afl_content)

    elif "C05" in name:
        # C05 has two-phase logic with londonTransition
        afl_content = re.sub(r'londonTransition\s*=.*?;', '', afl_content)
        afl_content = re.sub(r'\s*AND\s+londonTransition', '', afl_content)
        afl_content = re.sub(r'londonTransition\s+AND\s*', '', afl_content)
        afl_content = re.sub(r'\s*AND\s+NOT\s+londonTransition', '', afl_content)
        afl_content = re.sub(r'//.*London.*transition.*\n', '\n', afl_content, flags=re.IGNORECASE)
        afl_content = re.sub(r'//.*Phase\s+\d.*\n', '\n', afl_content, flags=re.IGNORECASE)

    return afl_content


def process_all_strategies():
    """Process all named strategy files."""
    # Get all named strategy files (A*, B*, C*, D01 - not D02 which already works)
    strategy_files = sorted(STRATEGIES_DIR.glob("*.afl"))

    named_files = []
    for f in strategy_files:
        name = f.stem
        # Include A01-A10, B03-B13, C01-C09, D01 (NOT D02, T* test files)
        if (name.startswith(("A0", "A10", "B0", "B1", "C0")) or name == "D01_derivative_basic"):
            named_files.append(f)

    print(f"Found {len(named_files)} strategy files to transform:")
    for f in named_files:
        print(f"  {f.name}")

    print()

    for afl_file in named_files:
        name = afl_file.stem
        print(f"Transforming: {name}")

        original = afl_file.read_text(encoding="utf-8")

        # Apply standard transformations
        transformed = transform_afl(original, name)

        # Apply special handling
        transformed = handle_special_strategies(transformed, name)

        # Write back
        afl_file.write_text(transformed, encoding="utf-8")
        print(f"  Written: {afl_file}")

    print(f"\nDone! Transformed {len(named_files)} strategies.")


if __name__ == "__main__":
    process_all_strategies()
