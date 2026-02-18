"""
Fix D02 database versions: remove CBT code that causes AmiBroker to hang at 67%.

Strips SetCustomBacktestProc, StaticVarSet, and the entire CBT if-block from
all affected strategy_versions rows.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "strategies.db"

conn = sqlite3.connect(str(DB_PATH))

# Find ALL versions that have CBT code
rows = conn.execute(
    "SELECT id, afl_content FROM strategy_versions WHERE afl_content LIKE '%SetCustomBacktestProc%'"
).fetchall()

print(f"Found {len(rows)} version(s) with SetCustomBacktestProc")

for version_id, afl in rows:
    original_len = len(afl)
    lines = afl.splitlines(keepends=True)
    new_lines = []
    skip_until_closing_brace = 0
    skip_staticvar_block = False
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip the StaticVarSet block (including temaSlope and comment)
        if stripped.startswith("// ---- Store indicator arrays for CBT"):
            # Skip this comment + next 4 lines (temaSlope + 3 StaticVarSet)
            skip_count = 0
            while i < len(lines) and skip_count < 6:
                check = lines[i].strip()
                if check.startswith("StaticVarSet(") or check.startswith("temaSlope") or check.startswith("// ---- Store"):
                    i += 1
                    skip_count += 1
                elif check == "":
                    i += 1
                    skip_count += 1
                else:
                    break
            continue

        # Skip SetCustomBacktestProc line
        if "SetCustomBacktestProc(" in stripped:
            i += 1
            continue

        # Skip the entire CBT block: "// ---- Custom Backtest Procedure..." through end
        if stripped.startswith("// ---- Custom Backtest Procedure"):
            # Skip all lines from here through the matching closing brace
            # of if (Status("action") == actionPortfolio) { ... }
            brace_depth = 0
            found_if = False
            while i < len(lines):
                check = lines[i].strip()
                if 'Status("action")' in check and "actionPortfolio" in check:
                    found_if = True
                if found_if:
                    brace_depth += check.count("{") - check.count("}")
                    i += 1
                    if brace_depth <= 0 and found_if:
                        break
                else:
                    i += 1
            continue

        # Skip standalone if (Status("action") == actionPortfolio) block
        if 'Status("action")' in stripped and "actionPortfolio" in stripped:
            brace_depth = 0
            while i < len(lines):
                check = lines[i].strip()
                brace_depth += check.count("{") - check.count("}")
                i += 1
                if brace_depth <= 0:
                    break
            continue

        new_lines.append(line)
        i += 1

    new_afl = "".join(new_lines).rstrip() + "\n"

    # Verify
    has_cbt = "SetCustomBacktestProc" in new_afl
    has_sv = "StaticVarSet(" in new_afl
    has_action = "actionPortfolio" in new_afl

    print(f"\nVersion {version_id}:")
    print(f"  Before: {original_len} chars")
    print(f"  After:  {len(new_afl)} chars")
    print(f"  CBT removed: {not has_cbt}")
    print(f"  StaticVar removed: {not has_sv}")
    print(f"  actionPortfolio removed: {not has_action}")

    if not has_cbt and not has_sv and not has_action:
        conn.execute(
            "UPDATE strategy_versions SET afl_content = ? WHERE id = ?",
            (new_afl, version_id)
        )
        print(f"  -> UPDATED in database")
    else:
        print(f"  -> WARNING: Some problematic code remains!")
        for j, line in enumerate(new_afl.splitlines()):
            if any(kw in line for kw in ["SetCustomBacktestProc", "StaticVarSet(", "actionPortfolio"]):
                print(f"     Line {j+1}: {line.strip()}")

conn.commit()
conn.close()
print("\nDone! All D02 versions in the database have been fixed.")
