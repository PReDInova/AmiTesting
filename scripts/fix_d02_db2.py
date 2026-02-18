"""
Fix D02 database versions (pass 2): truncate all CBT remnants.

Finds the end of the real strategy code (after Title assignment or last Plot)
and removes everything after that.
"""
import sqlite3
import re
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "strategies.db"

conn = sqlite3.connect(str(DB_PATH))

# Get ALL D02 versions (v10, v11, v12, and _reverse v1)
version_ids = [
    'c7c806e6-4653-4e75-9323-0a0e761e02ac',  # v10
    '56c1fb5f-b176-4038-aea8-27692dd8602f',  # v11
    '569c8c99-49e2-4f6d-8943-c751f587b1ee',  # v12
    '359d67b3-198c-4c55-b3ec-c19664a01c73',  # _reverse v1
]

for vid in version_ids:
    row = conn.execute('SELECT afl_content FROM strategy_versions WHERE id = ?', (vid,)).fetchone()
    if not row:
        print(f"Version {vid} not found!")
        continue

    afl = row[0]
    original_len = len(afl)
    lines = afl.splitlines()

    # Find the last line of real strategy code.
    # The strategy ends with the Title = ... line (possibly multi-line with + continuation)
    # Everything after that is CBT code that should be removed.
    cut_after = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Title assignment is the last real line of the strategy
        if stripped.startswith("Title =") or stripped.startswith("Title="):
            # Title may span multiple lines with + continuation
            j = i
            while j < len(lines) - 1:
                next_stripped = lines[j].rstrip()
                if next_stripped.endswith("+") or next_stripped.endswith('"'):
                    if j + 1 < len(lines) and lines[j + 1].strip().startswith('"'):
                        j += 1
                    else:
                        break
                else:
                    break
            cut_after = j
            # Check for trailing semicolon on the final Title line
            if not lines[cut_after].rstrip().endswith(";"):
                # Look at next line for the semicolon
                if cut_after + 1 < len(lines) and lines[cut_after + 1].strip().endswith(";"):
                    cut_after += 1

    if cut_after == -1:
        print(f"Version {vid}: Could not find Title line, skipping")
        continue

    # Keep everything up to and including the Title line
    new_lines = lines[:cut_after + 1]
    new_afl = "\n".join(new_lines) + "\n"

    # Verify no CBT remnants
    bad_keywords = ["SetCustomBacktestProc", "actionPortfolio", "GetBacktesterObject",
                    "bo.Backtest", "bo.GetFirstTrade", "SetForeign(sym)",
                    "RestorePriceArrays"]
    remaining = [kw for kw in bad_keywords if kw in new_afl]

    # Also check StaticVarSet (but not StaticVarGet in comments)
    has_staticvarset = bool(re.search(r'StaticVarSet\(', new_afl))

    print(f"\nVersion {vid}:")
    print(f"  Before: {original_len} chars, {len(lines)} lines")
    print(f"  After:  {len(new_afl)} chars, {len(new_lines)} lines")
    print(f"  Cut after line {cut_after + 1}: {lines[cut_after].strip()[:60]}...")
    print(f"  Remaining bad keywords: {remaining}")
    print(f"  StaticVarSet present: {has_staticvarset}")

    if not remaining and not has_staticvarset:
        conn.execute('UPDATE strategy_versions SET afl_content = ? WHERE id = ?', (new_afl, vid))
        print(f"  -> UPDATED in database")
    else:
        print(f"  -> WARNING: problematic code still present!")

conn.commit()
conn.close()
print("\nDone!")
