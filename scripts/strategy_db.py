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
                afl_content  TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                started_at   TIMESTAMP,
                completed_at TIMESTAMP,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS batch_runs (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'pending',
                total_count     INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count    INTEGER NOT NULL DEFAULT 0,
                run_mode        INTEGER NOT NULL DEFAULT 2,
                strategy_ids    TEXT NOT NULL DEFAULT '[]',
                run_ids         TEXT NOT NULL DEFAULT '[]',
                results_json    TEXT NOT NULL DEFAULT '{}',
                started_at      TIMESTAMP,
                completed_at    TIMESTAMP,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS param_tooltips (
                name       TEXT PRIMARY KEY,
                indicator  TEXT NOT NULL DEFAULT '',
                math       TEXT NOT NULL DEFAULT '',
                param      TEXT NOT NULL DEFAULT '',
                typical    TEXT NOT NULL DEFAULT '',
                guidance   TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS indicator_tooltips (
                keyword     TEXT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                math        TEXT NOT NULL DEFAULT '',
                usage       TEXT NOT NULL DEFAULT '',
                key_params  TEXT NOT NULL DEFAULT '',
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

        # Add afl_content column to existing backtest_runs tables (migration)
        try:
            conn.execute("ALTER TABLE backtest_runs ADD COLUMN afl_content TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Add params_json column to existing backtest_runs tables (migration)
        try:
            conn.execute("ALTER TABLE backtest_runs ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Add optimization-related columns to backtest_runs (migration)
        for col_def in [
            "is_optimization INTEGER DEFAULT 0",
            "total_combos INTEGER DEFAULT 0",
            "columns_json TEXT DEFAULT '[]'",
        ]:
            try:
                conn.execute(f"ALTER TABLE backtest_runs ADD COLUMN {col_def}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Add symbol column to backtest_runs (migration)
        try:
            conn.execute("ALTER TABLE backtest_runs ADD COLUMN symbol TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Create optimization_combos table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS optimization_combos (
                id           TEXT PRIMARY KEY,
                run_id       TEXT NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
                combo_index  INTEGER NOT NULL,
                params_json  TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                net_profit   REAL,
                num_trades   INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_opt_combos_run ON optimization_combos(run_id);
            CREATE INDEX IF NOT EXISTS idx_opt_combos_profit ON optimization_combos(run_id, net_profit DESC);
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
                afl_content  TEXT NOT NULL DEFAULT '',
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
            afl_content  TEXT NOT NULL DEFAULT '',
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
    afl_content: str = "",
    params_json: str = "{}",
    symbol: str = "",
    db_path: Path = None,
) -> str:
    """Create a new backtest run record. Returns the run UUID.

    The results_dir is automatically set to ``results/<run_id>``.
    ``afl_content`` should be the actual AFL code used for this run so the
    results page can display exactly what was executed.
    ``params_json`` stores a JSON string of run parameters (e.g. run_mode).
    """
    run_id = _new_uuid()
    results_dir = f"results/{run_id}"
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO backtest_runs (id, version_id, strategy_id, results_dir, apx_file, afl_content, params_json, symbol, status, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
            (run_id, version_id, strategy_id, results_dir, apx_file, afl_content, params_json, symbol),
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
    is_optimization: int = None,
    total_combos: int = None,
    columns_json: str = None,
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
                         ("completed_at", completed_at),
                         ("is_optimization", is_optimization),
                         ("total_combos", total_combos),
                         ("columns_json", columns_json)]:
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
        d["params"] = json.loads(d.pop("params_json", "{}"))
        d["columns"] = json.loads(d.pop("columns_json", "[]") or "[]")
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
            d["run_params"] = json.loads(d.pop("params_json", "{}"))
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
# Optimization combo CRUD
# ---------------------------------------------------------------------------

def store_optimization_combos(
    run_id: str,
    df,
    param_columns: list[str],
    metric_columns: list[str],
    db_path: Path = None,
) -> int:
    """Bulk-insert all optimization combo rows from a DataFrame.

    Parameters
    ----------
    run_id : str
        The backtest run UUID these combos belong to.
    df : pandas.DataFrame
        The optimization results DataFrame (one row per combo).
    param_columns : list[str]
        Column names that are strategy parameters.
    metric_columns : list[str]
        Column names that are result metrics.

    Returns
    -------
    int
        Number of rows inserted.
    """
    import pandas as pd

    if df is None or df.empty:
        return 0

    # Find net profit and # trades columns for denormalized fields
    net_profit_col = None
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ("net profit", "net profit %", "profit") and "%" not in cl:
            net_profit_col = col
            break
    if net_profit_col is None:
        for col in df.columns:
            if "net profit" in col.lower().strip():
                net_profit_col = col
                break

    trades_col = None
    for col in df.columns:
        if col.strip().lower() in ("# trades", "trades", "all trades"):
            trades_col = col
            break

    rows = []
    for idx, row in df.iterrows():
        combo_id = _new_uuid()
        params = {c: _safe_json_value(row.get(c)) for c in param_columns if c in df.columns}
        metrics = {c: _safe_json_value(row.get(c)) for c in metric_columns if c in df.columns}

        net_profit = None
        if net_profit_col:
            try:
                net_profit = float(pd.to_numeric(row[net_profit_col], errors="coerce"))
            except (ValueError, TypeError):
                pass

        num_trades = None
        if trades_col:
            try:
                num_trades = int(pd.to_numeric(row[trades_col], errors="coerce"))
            except (ValueError, TypeError):
                pass

        rows.append((
            combo_id,
            run_id,
            int(idx),
            json.dumps(params),
            json.dumps(metrics),
            net_profit,
            num_trades,
        ))

    conn = _get_connection(db_path)
    try:
        conn.executemany(
            """INSERT INTO optimization_combos
               (id, run_id, combo_index, params_json, metrics_json, net_profit, num_trades)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        logger.info("Stored %d optimization combos for run %s", len(rows), run_id)
        return len(rows)
    finally:
        conn.close()


def _safe_json_value(val):
    """Convert a pandas/numpy value to a JSON-safe Python type."""
    import math
    if val is None:
        return None
    try:
        import numpy as np
        if isinstance(val, (np.integer,)):
            return int(val)
        if isinstance(val, (np.floating,)):
            f = float(val)
            return None if math.isnan(f) else f
        if isinstance(val, (np.bool_,)):
            return bool(val)
    except ImportError:
        pass
    if isinstance(val, float) and math.isnan(val):
        return None
    return val


def get_optimization_combos(
    run_id: str,
    order_by: str = "net_profit",
    ascending: bool = False,
    limit: int = None,
    db_path: Path = None,
) -> list[dict]:
    """Fetch optimization combo rows for a run.

    Returns a list of dicts with deserialized params and metrics.
    """
    direction = "ASC" if ascending else "DESC"
    # Only allow safe column names for ORDER BY
    safe_cols = {"net_profit", "num_trades", "combo_index"}
    order_col = order_by if order_by in safe_cols else "net_profit"

    query = f"SELECT * FROM optimization_combos WHERE run_id = ? ORDER BY {order_col} {direction}"
    params = [run_id]
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    conn = _get_connection(db_path)
    try:
        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d.pop("params_json", "{}"))
            d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
            result.append(d)
        return result
    finally:
        conn.close()


def reconstruct_optimization_parsed(run_id: str, db_path: Path = None) -> dict | None:
    """Rebuild the exact dict shape that ``_parse_optimization_results()`` produces.

    Returns None if no SQL combo data exists for this run, so callers can
    fall back to CSV parsing.

    The returned dict has keys:
        trades, metrics, columns, error, is_optimization
    matching the shape expected by ``results_detail.html``.
    """
    import pandas as pd

    run = get_run(run_id, db_path)
    if run is None:
        return None

    # Check if this run has SQL combo data
    if not run.get("is_optimization") or not run.get("total_combos"):
        return None

    combos = get_optimization_combos(run_id, db_path=db_path)
    if not combos:
        return None

    # Recover ordered column list from the run record
    all_columns = run.get("columns", [])

    # Classify columns into param vs metric using the same keyword set as app.py
    metric_keywords = {
        "net profit", "profit", "# trades", "all trades", "avg. profit",
        "avg. bars", "drawdown", "max. trade", "winners", "losers",
        "profit factor", "sharpe", "ulcer", "recovery", "payoff",
        "cagr", "rar", "exposure", "risk", "% profitable",
    }
    metric_cols = []
    param_cols = []
    for col in all_columns:
        cl = col.lower().strip()
        if cl == "symbol":
            continue
        is_metric = any(kw in cl for kw in metric_keywords)
        if is_metric:
            metric_cols.append(col)
        else:
            param_cols.append(col)

    # Reconstruct row dicts in the original column order
    trades = []
    for combo in combos:
        row = {}
        row.update(combo["params"])
        row.update(combo["metrics"])
        trades.append(row)

    # Build summary metrics (same shape as _parse_optimization_results)
    metrics = {
        "combos_tested": len(combos),
        "param_columns": param_cols,
        "metric_columns": metric_cols,
    }

    # Compute summary stats from denormalized net_profit
    net_profits = [c["net_profit"] for c in combos if c.get("net_profit") is not None]
    if net_profits:
        metrics["best_net_profit"] = round(max(net_profits), 2)
        metrics["worst_net_profit"] = round(min(net_profits), 2)
        metrics["avg_net_profit"] = round(sum(net_profits) / len(net_profits), 2)
        metrics["profitable_combos"] = sum(1 for p in net_profits if p > 0)
        # Find the net profit column name
        for col in all_columns:
            cl = col.lower().strip()
            if cl in ("net profit", "net profit %", "profit") and "%" not in cl:
                metrics["net_profit_column"] = col
                break

    # Compute avg trades
    trade_counts = [c.get("num_trades") for c in combos if c.get("num_trades") is not None]
    if trade_counts:
        metrics["avg_trades"] = round(sum(trade_counts) / len(trade_counts), 1)

    return {
        "trades": trades,
        "metrics": metrics,
        "columns": all_columns,
        "error": None,
        "is_optimization": True,
    }


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

def seed_strategies_from_dir(db_path: Path = None) -> int:
    """Import strategy AFL files from the strategies/ directory.

    Scans ``strategies/*.afl``, parses each file's header comments to extract
    a name and description, then creates a strategy + version-1 record for any
    file whose name is not already in the database.

    Returns the number of newly imported strategies.
    """
    from config.settings import STRATEGIES_DIR

    if not STRATEGIES_DIR.exists():
        return 0

    afl_files = sorted(STRATEGIES_DIR.glob("*.afl"))
    if not afl_files:
        return 0

    existing_names = {s["name"] for s in list_strategies(db_path)}
    imported = 0

    for afl_path in afl_files:
        try:
            content = afl_path.read_text(encoding="utf-8")
        except Exception:
            continue

        # Parse header: line 2 is the name, lines after the separator are description
        lines = content.splitlines()
        name = afl_path.stem  # fallback
        description_lines = []
        in_description = False

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Line 2 (index 1) is typically the name line: "// A01 - TEMA + ADX Trend Filter"
            if i == 1 and stripped.startswith("//"):
                candidate = stripped.lstrip("/").strip()
                if candidate:
                    name = candidate
            # After the second separator (index 2), collect description lines
            elif i > 2 and stripped.startswith("//"):
                text = stripped.lstrip("/").strip()
                if text:
                    in_description = True
                    description_lines.append(text)
                elif in_description:
                    description_lines.append("")  # preserve paragraph breaks
            elif i > 2 and not stripped.startswith("//"):
                break  # end of header comment block

        if name in existing_names:
            continue

        description = "\n".join(description_lines).strip()

        strategy_id = create_strategy(
            name=name,
            summary=description_lines[0] if description_lines else "",
            description=description,
            symbol="/GC Gold Futures (Asian Session)",
            db_path=db_path,
        )

        create_version(
            strategy_id=strategy_id,
            afl_content=content,
            label="Initial version",
            db_path=db_path,
        )

        existing_names.add(name)
        imported += 1
        logger.info("Imported strategy from %s: %s", afl_path.name, name)

    if imported:
        logger.info("Imported %d strategies from %s", imported, STRATEGIES_DIR)
    return imported


def seed_default_strategies(db_path: Path = None) -> None:
    """Populate the DB with the default SMA crossover strategy if empty."""
    if list_strategies(db_path):
        # DB already has strategies -- still check for new files in strategies/
        seed_strategies_from_dir(db_path)
        return

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

    # Also import any strategies from the strategies/ directory
    seed_strategies_from_dir(db_path)


# ---------------------------------------------------------------------------
# Batch run CRUD
# ---------------------------------------------------------------------------

def create_batch(name: str = "", strategy_ids: list = None, run_mode: int = 2, db_path: Path = None) -> str:
    """Create a new batch run record. Returns the batch UUID."""
    batch_id = _new_uuid()
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO batch_runs (id, name, status, total_count, run_mode, strategy_ids)
               VALUES (?, ?, 'pending', ?, ?, ?)""",
            (batch_id, name, len(strategy_ids or []), run_mode, json.dumps(strategy_ids or [])),
        )
        conn.commit()
        return batch_id
    finally:
        conn.close()


def update_batch(batch_id: str, status: str = None, completed_count: int = None, failed_count: int = None, run_ids: list = None, results_json: str = None, started_at: str = None, completed_at: str = None, db_path: Path = None) -> bool:
    """Update a batch run record. Only non-None fields are updated."""
    conn = _get_connection(db_path)
    try:
        fields = []
        values = []
        for col, val in [("status", status), ("completed_count", completed_count),
                         ("failed_count", failed_count),
                         ("started_at", started_at), ("completed_at", completed_at),
                         ("results_json", results_json)]:
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        if run_ids is not None:
            fields.append("run_ids = ?")
            values.append(json.dumps(run_ids))
        if not fields:
            return True
        values.append(batch_id)
        cursor = conn.execute(
            f"UPDATE batch_runs SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_batch(batch_id: str, db_path: Path = None) -> dict | None:
    """Fetch a single batch run by UUID."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM batch_runs WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["strategy_ids"] = json.loads(d.get("strategy_ids", "[]"))
        d["run_ids"] = json.loads(d.get("run_ids", "[]"))
        d["results"] = json.loads(d.pop("results_json", "{}"))
        return d
    finally:
        conn.close()


def list_batches(limit: int = 20, db_path: Path = None) -> list[dict]:
    """Return batch runs, newest first."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM batch_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["strategy_ids"] = json.loads(d.get("strategy_ids", "[]"))
            d["run_ids"] = json.loads(d.get("run_ids", "[]"))
            d["results"] = json.loads(d.pop("results_json", "{}"))
            result.append(d)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Param tooltips CRUD
# ---------------------------------------------------------------------------

def list_param_tooltips(db_path: Path = None) -> list[dict]:
    """Return all param tooltip rows, ordered by name."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM param_tooltips ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_param_tooltip(name: str, db_path: Path = None) -> dict | None:
    """Fetch a single param tooltip by parameter name."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM param_tooltips WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_param_tooltips_dict(db_path: Path = None) -> dict[str, dict]:
    """Return all tooltips as {name: {indicator, math, param, typical, guidance}}.

    This matches the shape of the old hardcoded PARAM_INFO dict so it can
    be used as a drop-in replacement in templates.
    """
    rows = list_param_tooltips(db_path)
    result = {}
    for r in rows:
        result[r["name"]] = {
            "indicator": r["indicator"],
            "math": r["math"],
            "param": r["param"],
            "typical": r["typical"],
            "guidance": r["guidance"],
        }
    return result


def upsert_param_tooltip(
    name: str,
    indicator: str = "",
    math: str = "",
    param: str = "",
    typical: str = "",
    guidance: str = "",
    db_path: Path = None,
) -> bool:
    """Insert or replace a param tooltip row."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO param_tooltips
               (name, indicator, math, param, typical, guidance, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (name, indicator, math, param, typical, guidance),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_param_tooltip(name: str, db_path: Path = None) -> bool:
    """Delete a param tooltip row. Returns True if a row was deleted."""
    conn = _get_connection(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM param_tooltips WHERE name = ?", (name,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def seed_param_tooltips(db_path: Path = None) -> int:
    """Seed param_tooltips table from the hardcoded PARAM_INFO dictionary.

    Only inserts rows that don't already exist (preserves user edits).
    Returns the number of rows inserted.
    """
    from scripts.param_info import PARAM_INFO

    conn = _get_connection(db_path)
    inserted = 0
    try:
        for name, info in PARAM_INFO.items():
            existing = conn.execute(
                "SELECT 1 FROM param_tooltips WHERE name = ?", (name,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO param_tooltips
                       (name, indicator, math, param, typical, guidance)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        name,
                        info.get("indicator", ""),
                        info.get("math", ""),
                        info.get("param", ""),
                        info.get("typical", ""),
                        info.get("guidance", ""),
                    ),
                )
                inserted += 1
        conn.commit()
        if inserted:
            logger.info("Seeded %d param tooltips", inserted)
        return inserted
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Indicator tooltips CRUD
# ---------------------------------------------------------------------------

def get_all_indicator_tooltips_dict(db_path: Path = None) -> dict[str, dict]:
    """Return all indicator tooltips as {keyword: {name, description, math, usage, key_params}}."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM indicator_tooltips ORDER BY keyword"
        ).fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            result[d["keyword"]] = {
                "name": d["name"],
                "description": d["description"],
                "math": d["math"],
                "usage": d["usage"],
                "key_params": d["key_params"],
            }
        return result
    finally:
        conn.close()


def get_indicator_tooltip(keyword: str, db_path: Path = None) -> dict | None:
    """Fetch a single indicator tooltip by keyword."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM indicator_tooltips WHERE keyword = ?", (keyword,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_indicator_tooltip(
    keyword: str,
    name: str = "",
    description: str = "",
    math: str = "",
    usage: str = "",
    key_params: str = "",
    db_path: Path = None,
) -> bool:
    """Insert or replace an indicator tooltip row."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO indicator_tooltips
               (keyword, name, description, math, usage, key_params, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (keyword, name, description, math, usage, key_params),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_indicator_tooltip(keyword: str, db_path: Path = None) -> bool:
    """Delete an indicator tooltip row. Returns True if a row was deleted."""
    conn = _get_connection(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM indicator_tooltips WHERE keyword = ?", (keyword,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def seed_indicator_tooltips(db_path: Path = None) -> int:
    """Seed indicator_tooltips table from INDICATOR_INFO.

    Only inserts rows that don't already exist (preserves user edits).
    Returns the number of rows inserted.
    """
    from scripts.param_info import INDICATOR_INFO

    conn = _get_connection(db_path)
    inserted = 0
    try:
        for keyword, info in INDICATOR_INFO.items():
            existing = conn.execute(
                "SELECT 1 FROM indicator_tooltips WHERE keyword = ?", (keyword,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO indicator_tooltips
                       (keyword, name, description, math, usage, key_params)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        keyword,
                        info.get("name", ""),
                        info.get("description", ""),
                        info.get("math", ""),
                        info.get("usage", ""),
                        info.get("key_params", ""),
                    ),
                )
                inserted += 1
        conn.commit()
        if inserted:
            logger.info("Seeded %d indicator tooltips", inserted)
        return inserted
    finally:
        conn.close()
