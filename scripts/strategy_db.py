"""
SQLite database for strategy metadata with GUID-based result storage.

Schema: Strategy > Versions > Runs
- **strategies**: Top-level strategy container (UUID primary key)
- **strategy_versions**: Versioned AFL code snapshots (UUID primary key)
- **backtest_runs**: Individual backtest executions (UUID primary key, results in results/<run_id>/)

Each backtest run is stored separately with its own UUID so multiple
strategies and versions can be worked on simultaneously.
"""

import json
import sqlite3
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default DB lives alongside the project data
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "strategies.db"


def _get_connection(db_path: Path = None) -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection."""
    path = db_path or _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _new_uuid() -> str:
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db(db_path: Path = None) -> None:
    """Create the three-table schema if it does not exist."""
    conn = _get_connection(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS strategies (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                summary     TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                symbol      TEXT NOT NULL DEFAULT '',
                risk_notes  TEXT NOT NULL DEFAULT '',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS strategy_versions (
                id              TEXT PRIMARY KEY,
                strategy_id     TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
                version_number  INTEGER NOT NULL,
                afl_content     TEXT NOT NULL DEFAULT '',
                parameters_json TEXT NOT NULL DEFAULT '[]',
                label           TEXT NOT NULL DEFAULT '',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(strategy_id, version_number)
            );

            CREATE TABLE IF NOT EXISTS backtest_runs (
                id           TEXT PRIMARY KEY,
                version_id   TEXT NOT NULL REFERENCES strategy_versions(id) ON DELETE CASCADE,
                strategy_id  TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
                results_dir  TEXT NOT NULL DEFAULT '',
                results_csv  TEXT NOT NULL DEFAULT '',
                results_html TEXT NOT NULL DEFAULT '',
                apx_file     TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                started_at   TIMESTAMP,
                completed_at TIMESTAMP,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

        # Migrate legacy data if the old single-table schema exists
        _migrate_legacy_if_needed(conn)

    finally:
        conn.close()


def _migrate_legacy_if_needed(conn: sqlite3.Connection) -> None:
    """Detect the old single-table schema and migrate data to the new schema.

    The old schema had columns: id, results_file, name, summary, description,
    parameters_json, symbol, risk_notes, afl_file, apx_file, created_at, updated_at.
    """
    # Check if old table exists by looking for the 'results_file' column
    cursor = conn.execute("PRAGMA table_info(strategies)")
    columns = {row["name"] for row in cursor.fetchall()}

    if "results_file" not in columns:
        return  # Already on new schema or fresh DB

    logger.info("Detected legacy single-table schema — migrating to GUID schema...")

    # Read all legacy rows
    legacy_rows = conn.execute(
        "SELECT * FROM strategies ORDER BY id"
    ).fetchall()

    if not legacy_rows:
        # Empty old table — just drop and recreate
        conn.execute("DROP TABLE strategies")
        conn.executescript("""
            CREATE TABLE strategies (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                summary     TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                symbol      TEXT NOT NULL DEFAULT '',
                risk_notes  TEXT NOT NULL DEFAULT '',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS strategy_versions (
                id              TEXT PRIMARY KEY,
                strategy_id     TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
                version_number  INTEGER NOT NULL,
                afl_content     TEXT NOT NULL DEFAULT '',
                parameters_json TEXT NOT NULL DEFAULT '[]',
                label           TEXT NOT NULL DEFAULT '',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(strategy_id, version_number)
            );

            CREATE TABLE IF NOT EXISTS backtest_runs (
                id           TEXT PRIMARY KEY,
                version_id   TEXT NOT NULL REFERENCES strategy_versions(id) ON DELETE CASCADE,
                strategy_id  TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
                results_dir  TEXT NOT NULL DEFAULT '',
                results_csv  TEXT NOT NULL DEFAULT '',
                results_html TEXT NOT NULL DEFAULT '',
                apx_file     TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                started_at   TIMESTAMP,
                completed_at TIMESTAMP,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        logger.info("Legacy schema migrated (empty — recreated fresh).")
        return

    # Save legacy data, drop old table, create new tables, insert migrated data
    legacy_data = [dict(row) for row in legacy_rows]

    conn.execute("DROP TABLE strategies")
    conn.executescript("""
        CREATE TABLE strategies (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            symbol      TEXT NOT NULL DEFAULT '',
            risk_notes  TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS strategy_versions (
            id              TEXT PRIMARY KEY,
            strategy_id     TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            version_number  INTEGER NOT NULL,
            afl_content     TEXT NOT NULL DEFAULT '',
            parameters_json TEXT NOT NULL DEFAULT '[]',
            label           TEXT NOT NULL DEFAULT '',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, version_number)
        );

        CREATE TABLE IF NOT EXISTS backtest_runs (
            id           TEXT PRIMARY KEY,
            version_id   TEXT NOT NULL REFERENCES strategy_versions(id) ON DELETE CASCADE,
            strategy_id  TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            results_dir  TEXT NOT NULL DEFAULT '',
            results_csv  TEXT NOT NULL DEFAULT '',
            results_html TEXT NOT NULL DEFAULT '',
            apx_file     TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL DEFAULT 'pending',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            started_at   TIMESTAMP,
            completed_at TIMESTAMP,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Read AFL content from disk for migration
    project_root = Path(__file__).resolve().parent.parent

    for old_row in legacy_data:
        strategy_id = _new_uuid()
        version_id = _new_uuid()
        run_id = _new_uuid()

        # Try to read AFL file content
        afl_content = ""
        afl_file = old_row.get("afl_file", "")
        if afl_file:
            afl_path = project_root / afl_file
            if afl_path.exists():
                try:
                    afl_content = afl_path.read_text(encoding="utf-8")
                except Exception:
                    pass

        created = old_row.get("created_at", datetime.now(timezone.utc).isoformat())
        updated = old_row.get("updated_at", created)

        # Create strategy
        conn.execute(
            """INSERT INTO strategies (id, name, summary, description, symbol, risk_notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy_id, old_row["name"], old_row.get("summary", ""),
             old_row.get("description", ""), old_row.get("symbol", ""),
             old_row.get("risk_notes", ""), created, updated),
        )

        # Create version 1
        conn.execute(
            """INSERT INTO strategy_versions (id, strategy_id, version_number, afl_content, parameters_json, label, created_at)
               VALUES (?, ?, 1, ?, ?, 'Migrated from legacy', ?)""",
            (version_id, strategy_id, afl_content,
             old_row.get("parameters_json", "[]"), created),
        )

        # Create a run referencing the old results file
        results_file = old_row.get("results_file", "")
        conn.execute(
            """INSERT INTO backtest_runs (id, version_id, strategy_id, results_dir, results_csv, results_html, apx_file, status, created_at)
               VALUES (?, ?, ?, '', ?, ?, ?, 'completed', ?)""",
            (run_id, version_id, strategy_id, results_file,
             results_file.replace(".csv", ".html") if results_file else "",
             old_row.get("apx_file", ""), created),
        )

    conn.commit()
    logger.info("Migrated %d legacy strategies to GUID schema.", len(legacy_data))


# ---------------------------------------------------------------------------
# Strategy CRUD
# ---------------------------------------------------------------------------

def create_strategy(
    name: str,
    summary: str = "",
    description: str = "",
    symbol: str = "",
    risk_notes: str = "",
    db_path: Path = None,
) -> str:
    """Create a new strategy. Returns the new strategy UUID."""
    strategy_id = _new_uuid()
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO strategies (id, name, summary, description, symbol, risk_notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (strategy_id, name, summary, description, symbol, risk_notes),
        )
        conn.commit()
        return strategy_id
    finally:
        conn.close()


def update_strategy(
    strategy_id: str,
    name: str = None,
    summary: str = None,
    description: str = None,
    symbol: str = None,
    risk_notes: str = None,
    db_path: Path = None,
) -> bool:
    """Update a strategy's metadata. Only non-None fields are updated."""
    conn = _get_connection(db_path)
    try:
        fields = []
        values = []
        for col, val in [("name", name), ("summary", summary),
                         ("description", description), ("symbol", symbol),
                         ("risk_notes", risk_notes)]:
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        if not fields:
            return True
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(strategy_id)
        cursor = conn.execute(
            f"UPDATE strategies SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_strategy(strategy_id: str, db_path: Path = None) -> dict | None:
    """Fetch a single strategy by UUID."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_strategies(db_path: Path = None) -> list[dict]:
    """Return all strategies ordered by most recently updated."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM strategies ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_strategy(strategy_id: str, db_path: Path = None) -> bool:
    """Delete a strategy and all its versions and runs (cascade)."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM strategies WHERE id = ?", (strategy_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Version CRUD
# ---------------------------------------------------------------------------

def create_version(
    strategy_id: str,
    afl_content: str,
    parameters: list = None,
    label: str = "",
    db_path: Path = None,
) -> str:
    """Create a new version for a strategy. Returns the new version UUID.

    Automatically assigns the next sequential version_number.
    """
    version_id = _new_uuid()
    params_json = json.dumps(parameters or [])
    conn = _get_connection(db_path)
    try:
        # Get next version number
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_num FROM strategy_versions WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        next_num = row["next_num"]

        conn.execute(
            """INSERT INTO strategy_versions (id, strategy_id, version_number, afl_content, parameters_json, label)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (version_id, strategy_id, next_num, afl_content, params_json, label),
        )
        # Touch the parent strategy's updated_at
        conn.execute(
            "UPDATE strategies SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (strategy_id,),
        )
        conn.commit()
        return version_id
    finally:
        conn.close()


def get_version(version_id: str, db_path: Path = None) -> dict | None:
    """Fetch a single version by UUID."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM strategy_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["parameters"] = json.loads(d.pop("parameters_json", "[]"))
        return d
    finally:
        conn.close()


def list_versions(strategy_id: str, db_path: Path = None) -> list[dict]:
    """Return all versions for a strategy, newest first."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM strategy_versions WHERE strategy_id = ? ORDER BY version_number DESC",
            (strategy_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["parameters"] = json.loads(d.pop("parameters_json", "[]"))
            result.append(d)
        return result
    finally:
        conn.close()


def get_latest_version(strategy_id: str, db_path: Path = None) -> dict | None:
    """Fetch the latest (highest version_number) version for a strategy."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM strategy_versions WHERE strategy_id = ? ORDER BY version_number DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["parameters"] = json.loads(d.pop("parameters_json", "[]"))
        return d
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Run CRUD
# ---------------------------------------------------------------------------

def create_run(
    version_id: str,
    strategy_id: str,
    apx_file: str = "",
    db_path: Path = None,
) -> str:
    """Create a new backtest run record. Returns the run UUID.

    The results_dir is automatically set to ``results/<run_id>``.
    """
    run_id = _new_uuid()
    results_dir = f"results/{run_id}"
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO backtest_runs (id, version_id, strategy_id, results_dir, apx_file, status, started_at)
               VALUES (?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
            (run_id, version_id, strategy_id, results_dir, apx_file),
        )
        conn.commit()
        return run_id
    finally:
        conn.close()


def update_run(
    run_id: str,
    status: str = None,
    results_csv: str = None,
    results_html: str = None,
    metrics_json: str = None,
    completed_at: str = None,
    db_path: Path = None,
) -> bool:
    """Update a run record. Only non-None fields are updated."""
    conn = _get_connection(db_path)
    try:
        fields = []
        values = []
        for col, val in [("status", status), ("results_csv", results_csv),
                         ("results_html", results_html),
                         ("metrics_json", metrics_json),
                         ("completed_at", completed_at)]:
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        if not fields:
            return True
        values.append(run_id)
        cursor = conn.execute(
            f"UPDATE backtest_runs SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_run(run_id: str, db_path: Path = None) -> dict | None:
    """Fetch a single run by UUID."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
        return d
    finally:
        conn.close()


def list_runs(
    strategy_id: str = None,
    version_id: str = None,
    db_path: Path = None,
) -> list[dict]:
    """Return runs filtered by strategy or version, newest first."""
    conn = _get_connection(db_path)
    try:
        if version_id:
            rows = conn.execute(
                "SELECT * FROM backtest_runs WHERE version_id = ? ORDER BY created_at DESC, rowid DESC",
                (version_id,),
            ).fetchall()
        elif strategy_id:
            rows = conn.execute(
                "SELECT * FROM backtest_runs WHERE strategy_id = ? ORDER BY created_at DESC, rowid DESC",
                (strategy_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM backtest_runs ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
            result.append(d)
        return result
    finally:
        conn.close()


def get_latest_run(strategy_id: str, db_path: Path = None) -> dict | None:
    """Fetch the most recent run for a strategy."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE strategy_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
        return d
    finally:
        conn.close()


def delete_run(run_id: str, db_path: Path = None) -> bool:
    """Delete a single run record."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM backtest_runs WHERE id = ?", (run_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------------

_DEFAULT_STRATEGY = {
    "name": "Unknown Strategy",
    "summary": "Backtest results from an unregistered strategy.",
    "description": (
        "No description is available for this result set. "
        "It may have been generated by a custom or experimental strategy."
    ),
    "parameters": [],
    "symbol": "Unknown",
    "risk_notes": "Review results carefully \u2014 no strategy metadata is available.",
}


def get_strategy_info(strategy_id: str, db_path: Path = None) -> dict:
    """Fetch strategy metadata, falling back to a default for unknown IDs."""
    row = get_strategy(strategy_id, db_path)
    if row is None:
        return dict(_DEFAULT_STRATEGY)
    return row


def get_run_with_context(run_id: str, db_path: Path = None) -> dict | None:
    """Fetch a run with its parent strategy and version info attached."""
    run = get_run(run_id, db_path)
    if run is None:
        return None
    run["strategy"] = get_strategy(run["strategy_id"], db_path)
    run["version"] = get_version(run["version_id"], db_path)
    return run


def get_strategy_summary(strategy_id: str, db_path: Path = None) -> dict | None:
    """Fetch a strategy with counts of versions and runs."""
    strategy = get_strategy(strategy_id, db_path)
    if strategy is None:
        return None
    versions = list_versions(strategy_id, db_path)
    runs = list_runs(strategy_id=strategy_id, db_path=db_path)
    strategy["version_count"] = len(versions)
    strategy["run_count"] = len(runs)
    strategy["latest_version"] = versions[0] if versions else None
    strategy["latest_run"] = runs[0] if runs else None
    return strategy


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------

def seed_default_strategies(db_path: Path = None) -> None:
    """Populate the DB with the default SMA crossover strategy if empty."""
    if list_strategies(db_path):
        return  # already seeded

    # Read AFL content from disk
    project_root = Path(__file__).resolve().parent.parent
    afl_path = project_root / "afl" / "ma_crossover.afl"
    afl_content = ""
    if afl_path.exists():
        try:
            afl_content = afl_path.read_text(encoding="utf-8")
        except Exception:
            pass

    strategy_id = create_strategy(
        name="SMA Crossover \u2014 Tick Data (10/30 min)",
        summary=(
            "Aggregates /GC tick data into 1-minute bars and trades a "
            "10-min / 30-min SMA crossover during the Asian session."
        ),
        description=(
            "This strategy consumes raw tick data for gold futures (/GC) during the Asian trading session "
            "and aggregates it into 1-minute OHLC bars using AmiBroker's TimeFrameSet function.\n\n"
            "A 10-minute 'fast' SMA and a 30-minute 'slow' SMA are computed on the 1-minute bars. "
            "When the fast SMA crosses above the slow SMA, the system buys one gold futures contract. "
            "When the fast SMA crosses below the slow SMA, the system exits the position.\n\n"
            "The signals are then expanded back to the native tick timeframe so the backtest engine "
            "processes entries and exits at tick-level precision.\n\n"
            "When reviewing results, look at:\n"
            "- Win rate: Trend-following strategies typically win 40-60% of trades\n"
            "- Average win vs average loss: Winners should be significantly larger than losers\n"
            "- Max drawdown: The worst peak-to-trough decline \u2014 indicates risk\n"
            "- Total profit: Net P&L after all trades, in dollars ($100 per point for /GC)"
        ),
        symbol="/GC Gold Futures (Asian Session)",
        risk_notes=(
            "This strategy is designed for tick-level data during the Asian session only. "
            "Results should be reviewed for technical correctness (trades execute, metrics compute) "
            "rather than profitability. No commissions are included, which overstates real-world "
            "performance. Tick data strategies are sensitive to data quality and gaps."
        ),
        db_path=db_path,
    )

    version_id = create_version(
        strategy_id=strategy_id,
        afl_content=afl_content,
        parameters=[
            {"name": "Fast MA Period", "value": "10 minutes"},
            {"name": "Slow MA Period", "value": "30 minutes"},
            {"name": "Bar Aggregation", "value": "1-minute (from tick data)"},
            {"name": "Position Size", "value": "1 contract"},
            {"name": "Entry Signal", "value": "Fast SMA crosses above Slow SMA"},
            {"name": "Exit Signal", "value": "Slow SMA crosses above Fast SMA"},
            {"name": "Symbol", "value": "/GC Gold Futures (Asian Session)"},
            {"name": "Starting Capital", "value": "$100,000"},
            {"name": "Commissions", "value": "None (clean test)"},
            {"name": "Point Value", "value": "$100 per point"},
        ],
        label="Initial version",
        db_path=db_path,
    )

    # If there's an existing results.csv, create a legacy run pointing to it
    results_csv = project_root / "results" / "results.csv"
    if results_csv.exists():
        run_id = create_run(
            version_id=version_id,
            strategy_id=strategy_id,
            apx_file="apx/gcz25_test.apx",
            db_path=db_path,
        )
        update_run(
            run_id=run_id,
            status="completed",
            results_csv="results.csv",
            results_html="results.html",
            completed_at=datetime.now(timezone.utc).isoformat(),
            db_path=db_path,
        )

    logger.info("Seeded default SMA crossover strategy into database.")
