"""
Lightweight SQLite database for strategy metadata.

Stores strategy descriptions, parameters, result file locations, and
backtest configuration so the dashboard does not need hardcoded dicts.
"""

import json
import sqlite3
import logging
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
    return conn


def init_db(db_path: Path = None) -> None:
    """Create the strategies table if it does not exist."""
    conn = _get_connection(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                results_file    TEXT UNIQUE NOT NULL,
                name            TEXT NOT NULL,
                summary         TEXT NOT NULL DEFAULT '',
                description     TEXT NOT NULL DEFAULT '',
                parameters_json TEXT NOT NULL DEFAULT '[]',
                symbol          TEXT NOT NULL DEFAULT '',
                risk_notes      TEXT NOT NULL DEFAULT '',
                afl_file        TEXT NOT NULL DEFAULT '',
                apx_file        TEXT NOT NULL DEFAULT '',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def upsert_strategy(
    results_file: str,
    name: str,
    summary: str = "",
    description: str = "",
    parameters: list = None,
    symbol: str = "",
    risk_notes: str = "",
    afl_file: str = "",
    apx_file: str = "",
    db_path: Path = None,
) -> int:
    """Insert or update a strategy by its results_file key.

    Returns the row id.
    """
    params_json = json.dumps(parameters or [])
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO strategies
                (results_file, name, summary, description, parameters_json,
                 symbol, risk_notes, afl_file, apx_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(results_file) DO UPDATE SET
                name            = excluded.name,
                summary         = excluded.summary,
                description     = excluded.description,
                parameters_json = excluded.parameters_json,
                symbol          = excluded.symbol,
                risk_notes      = excluded.risk_notes,
                afl_file        = excluded.afl_file,
                apx_file        = excluded.apx_file,
                updated_at      = CURRENT_TIMESTAMP
            """,
            (results_file, name, summary, description, params_json,
             symbol, risk_notes, afl_file, apx_file),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_strategy(results_file: str, db_path: Path = None) -> dict | None:
    """Fetch a single strategy by results_file.  Returns None if not found."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM strategies WHERE results_file = ?",
            (results_file,),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_strategies(db_path: Path = None) -> list[dict]:
    """Return all strategies ordered by most recently updated."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM strategies ORDER BY updated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def delete_strategy(results_file: str, db_path: Path = None) -> bool:
    """Delete a strategy.  Returns True if a row was removed."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM strategies WHERE results_file = ?",
            (results_file,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
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


def get_strategy_info(results_file: str, db_path: Path = None) -> dict:
    """Fetch strategy metadata for a result file.

    Falls back to a default entry when the file is not registered — mirrors
    the old ``STRATEGY_DESCRIPTIONS["_default"]`` behaviour.
    """
    row = get_strategy(results_file, db_path)
    if row is None:
        return dict(_DEFAULT_STRATEGY)
    return row


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a database row to the dict format the dashboard expects."""
    d = dict(row)
    d["parameters"] = json.loads(d.pop("parameters_json", "[]"))
    return d


# ---------------------------------------------------------------------------
# Seed helper — migrate the old hardcoded strategy into the DB
# ---------------------------------------------------------------------------

def seed_default_strategies(db_path: Path = None) -> None:
    """Populate the DB with the default SMA crossover strategy if empty."""
    if list_strategies(db_path):
        return  # already seeded

    upsert_strategy(
        results_file="results.csv",
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
        symbol="/GC Gold Futures (Asian Session)",
        risk_notes=(
            "This strategy is designed for tick-level data during the Asian session only. "
            "Results should be reviewed for technical correctness (trades execute, metrics compute) "
            "rather than profitability. No commissions are included, which overstates real-world "
            "performance. Tick data strategies are sensitive to data quality and gaps."
        ),
        afl_file="afl/ma_crossover.afl",
        apx_file="apx/gcz25_test.apx",
        db_path=db_path,
    )
    logger.info("Seeded default SMA crossover strategy into database.")
