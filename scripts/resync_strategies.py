"""One-off utility: resync DB strategy versions from on-disk AFL files.

For each strategy in the database, reads the corresponding .afl file from
the strategies/ directory and updates the latest version's afl_content if
it differs.  This ensures the DB has the latest fixes (e.g. sdSource).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import STRATEGIES_DIR
from scripts.strategy_db import (
    init_db,
    list_strategies,
    get_latest_version,
    _get_connection,
)


def resync():
    init_db()

    # Build map: strategy name -> (path, content) from disk
    afl_files = {}
    for p in sorted(STRATEGIES_DIR.glob("*.afl")):
        content = p.read_text(encoding="utf-8")
        lines = content.splitlines()
        name = p.stem  # fallback
        if len(lines) > 1:
            stripped = lines[1].strip()
            if stripped.startswith("//"):
                candidate = stripped.lstrip("/").strip()
                if candidate:
                    name = candidate
        afl_files[name] = (p, content)

    strategies = list_strategies()
    updated = 0
    skipped = 0

    conn = _get_connection()
    for s in strategies:
        name = s["name"]
        if name not in afl_files:
            skipped += 1
            continue

        _afl_path, disk_content = afl_files[name]
        version = get_latest_version(s["id"])
        if version is None:
            skipped += 1
            continue

        db_content = version.get("afl_content", "")
        if db_content == disk_content:
            skipped += 1
            continue

        # Update the latest version's afl_content in-place
        conn.execute(
            "UPDATE strategy_versions SET afl_content = ? WHERE id = ?",
            (disk_content, version["id"]),
        )
        conn.commit()
        old_len = len(db_content) if db_content else 0
        new_len = len(disk_content)
        vnum = version["version_number"]
        print(f"  UPDATED: {name} (v{vnum}) {old_len} -> {new_len} chars")
        updated += 1

    print(f"\nDone: {updated} updated, {skipped} skipped (unchanged or no file)")


if __name__ == "__main__":
    resync()
