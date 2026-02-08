"""
Tests for scripts.strategy_db — SQLite strategy metadata storage.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.strategy_db import (
    init_db,
    upsert_strategy,
    get_strategy,
    list_strategies,
    delete_strategy,
    get_strategy_info,
    seed_default_strategies,
)


@pytest.fixture
def db(tmp_path):
    """Create an isolated test database and return its path."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_database_file(self, tmp_path):
        db_path = tmp_path / "new.db"
        assert not db_path.exists()
        init_db(db_path)
        assert db_path.exists()

    def test_idempotent(self, db):
        """Calling init_db twice should not raise."""
        init_db(db)


# ---------------------------------------------------------------------------
# Upsert / Get
# ---------------------------------------------------------------------------

class TestUpsertAndGet:
    def test_insert_and_retrieve(self, db):
        upsert_strategy(
            results_file="test.csv",
            name="Test Strategy",
            summary="A test.",
            symbol="GCZ25",
            db_path=db,
        )
        row = get_strategy("test.csv", db)
        assert row is not None
        assert row["name"] == "Test Strategy"
        assert row["summary"] == "A test."
        assert row["symbol"] == "GCZ25"

    def test_parameters_stored_as_json(self, db):
        params = [{"name": "Period", "value": "10"}]
        upsert_strategy(
            results_file="p.csv",
            name="Params Test",
            parameters=params,
            db_path=db,
        )
        row = get_strategy("p.csv", db)
        assert row["parameters"] == params

    def test_upsert_updates_existing(self, db):
        upsert_strategy(results_file="u.csv", name="V1", db_path=db)
        upsert_strategy(results_file="u.csv", name="V2", summary="updated", db_path=db)
        row = get_strategy("u.csv", db)
        assert row["name"] == "V2"
        assert row["summary"] == "updated"

    def test_get_nonexistent_returns_none(self, db):
        assert get_strategy("nope.csv", db) is None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TestList:
    def test_list_empty(self, db):
        assert list_strategies(db) == []

    def test_list_multiple(self, db):
        upsert_strategy(results_file="a.csv", name="A", db_path=db)
        upsert_strategy(results_file="b.csv", name="B", db_path=db)
        rows = list_strategies(db)
        assert len(rows) == 2
        names = {r["name"] for r in rows}
        assert names == {"A", "B"}


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_delete_existing(self, db):
        upsert_strategy(results_file="d.csv", name="Doomed", db_path=db)
        assert delete_strategy("d.csv", db) is True
        assert get_strategy("d.csv", db) is None

    def test_delete_nonexistent(self, db):
        assert delete_strategy("missing.csv", db) is False


# ---------------------------------------------------------------------------
# get_strategy_info (dashboard helper)
# ---------------------------------------------------------------------------

class TestGetStrategyInfo:
    def test_returns_stored_strategy(self, db):
        upsert_strategy(
            results_file="info.csv",
            name="Info Test",
            symbol="NQ",
            db_path=db,
        )
        info = get_strategy_info("info.csv", db)
        assert info["name"] == "Info Test"
        assert info["symbol"] == "NQ"

    def test_returns_default_for_unknown(self, db):
        info = get_strategy_info("unknown.csv", db)
        assert info["name"] == "Unknown Strategy"
        assert info["parameters"] == []


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

class TestSeed:
    def test_seed_populates_empty_db(self, db):
        seed_default_strategies(db)
        rows = list_strategies(db)
        assert len(rows) == 1
        assert rows[0]["results_file"] == "results.csv"
        assert "SMA Crossover" in rows[0]["name"]

    def test_seed_does_not_duplicate(self, db):
        seed_default_strategies(db)
        seed_default_strategies(db)
        assert len(list_strategies(db)) == 1
