"""
Tests for scripts.strategy_db — GUID-based strategy/version/run storage.

Tests the three-table schema: strategies > strategy_versions > backtest_runs.
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.strategy_db import (
    init_db,
    create_strategy,
    update_strategy,
    get_strategy,
    list_strategies,
    delete_strategy,
    create_version,
    get_version,
    list_versions,
    get_latest_version,
    create_run,
    update_run,
    get_run,
    list_runs,
    get_latest_run,
    delete_run,
    get_strategy_info,
    get_strategy_summary,
    get_run_with_context,
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
# Strategy CRUD
# ---------------------------------------------------------------------------

class TestStrategyCrud:
    def test_create_and_get(self, db):
        sid = create_strategy(name="Test Strategy", summary="A test.", symbol="GCZ25", db_path=db)
        assert sid  # UUID string
        row = get_strategy(sid, db)
        assert row is not None
        assert row["name"] == "Test Strategy"
        assert row["summary"] == "A test."
        assert row["symbol"] == "GCZ25"

    def test_update_strategy(self, db):
        sid = create_strategy(name="V1", db_path=db)
        update_strategy(sid, name="V2", summary="updated", db_path=db)
        row = get_strategy(sid, db)
        assert row["name"] == "V2"
        assert row["summary"] == "updated"

    def test_get_nonexistent_returns_none(self, db):
        assert get_strategy("nonexistent-uuid", db) is None

    def test_list_strategies(self, db):
        create_strategy(name="A", db_path=db)
        create_strategy(name="B", db_path=db)
        rows = list_strategies(db)
        assert len(rows) == 2
        names = {r["name"] for r in rows}
        assert names == {"A", "B"}

    def test_list_empty(self, db):
        assert list_strategies(db) == []

    def test_delete_strategy(self, db):
        sid = create_strategy(name="Doomed", db_path=db)
        assert delete_strategy(sid, db) is True
        assert get_strategy(sid, db) is None

    def test_delete_nonexistent(self, db):
        assert delete_strategy("missing-uuid", db) is False

    def test_delete_cascades_versions_and_runs(self, db):
        sid = create_strategy(name="Cascade", db_path=db)
        vid = create_version(sid, afl_content="Buy();", db_path=db)
        rid = create_run(vid, sid, db_path=db)
        delete_strategy(sid, db)
        assert get_version(vid, db) is None
        assert get_run(rid, db) is None


# ---------------------------------------------------------------------------
# Version CRUD
# ---------------------------------------------------------------------------

class TestVersionCrud:
    def test_create_and_get(self, db):
        sid = create_strategy(name="S", db_path=db)
        vid = create_version(sid, afl_content="Buy();", parameters=[{"n": "Period", "v": "10"}], label="initial", db_path=db)
        v = get_version(vid, db)
        assert v is not None
        assert v["strategy_id"] == sid
        assert v["version_number"] == 1
        assert v["afl_content"] == "Buy();"
        assert v["label"] == "initial"
        assert v["parameters"] == [{"n": "Period", "v": "10"}]

    def test_auto_increment_version_number(self, db):
        sid = create_strategy(name="S", db_path=db)
        v1 = create_version(sid, afl_content="v1", db_path=db)
        v2 = create_version(sid, afl_content="v2", db_path=db)
        assert get_version(v1, db)["version_number"] == 1
        assert get_version(v2, db)["version_number"] == 2

    def test_list_versions_newest_first(self, db):
        sid = create_strategy(name="S", db_path=db)
        create_version(sid, afl_content="v1", db_path=db)
        create_version(sid, afl_content="v2", db_path=db)
        versions = list_versions(sid, db)
        assert len(versions) == 2
        assert versions[0]["version_number"] == 2
        assert versions[1]["version_number"] == 1

    def test_get_latest_version(self, db):
        sid = create_strategy(name="S", db_path=db)
        create_version(sid, afl_content="v1", db_path=db)
        create_version(sid, afl_content="v2", label="latest", db_path=db)
        latest = get_latest_version(sid, db)
        assert latest["version_number"] == 2
        assert latest["label"] == "latest"

    def test_get_latest_version_empty(self, db):
        sid = create_strategy(name="S", db_path=db)
        assert get_latest_version(sid, db) is None


# ---------------------------------------------------------------------------
# Run CRUD
# ---------------------------------------------------------------------------

class TestRunCrud:
    def test_create_and_get(self, db):
        sid = create_strategy(name="S", db_path=db)
        vid = create_version(sid, afl_content="Buy();", db_path=db)
        rid = create_run(vid, sid, apx_file="test.apx", db_path=db)
        r = get_run(rid, db)
        assert r is not None
        assert r["version_id"] == vid
        assert r["strategy_id"] == sid
        assert r["status"] == "pending"
        assert r["results_dir"] == f"results/{rid}"
        assert r["apx_file"] == "test.apx"

    def test_update_run(self, db):
        sid = create_strategy(name="S", db_path=db)
        vid = create_version(sid, afl_content="Buy();", db_path=db)
        rid = create_run(vid, sid, db_path=db)

        update_run(rid, status="completed", results_csv="results.csv",
                   metrics_json='{"total_trades": 5}', db_path=db)

        r = get_run(rid, db)
        assert r["status"] == "completed"
        assert r["results_csv"] == "results.csv"
        assert r["metrics"] == {"total_trades": 5}

    def test_list_runs_by_strategy(self, db):
        sid = create_strategy(name="S", db_path=db)
        vid = create_version(sid, afl_content="Buy();", db_path=db)
        create_run(vid, sid, db_path=db)
        create_run(vid, sid, db_path=db)

        sid2 = create_strategy(name="S2", db_path=db)
        vid2 = create_version(sid2, afl_content="Sell();", db_path=db)
        create_run(vid2, sid2, db_path=db)

        runs_s = list_runs(strategy_id=sid, db_path=db)
        assert len(runs_s) == 2

        runs_s2 = list_runs(strategy_id=sid2, db_path=db)
        assert len(runs_s2) == 1

    def test_list_runs_by_version(self, db):
        sid = create_strategy(name="S", db_path=db)
        v1 = create_version(sid, afl_content="v1", db_path=db)
        v2 = create_version(sid, afl_content="v2", db_path=db)
        create_run(v1, sid, db_path=db)
        create_run(v2, sid, db_path=db)
        create_run(v2, sid, db_path=db)

        assert len(list_runs(version_id=v1, db_path=db)) == 1
        assert len(list_runs(version_id=v2, db_path=db)) == 2

    def test_get_latest_run(self, db):
        sid = create_strategy(name="S", db_path=db)
        vid = create_version(sid, afl_content="Buy();", db_path=db)
        r1 = create_run(vid, sid, db_path=db)
        r2 = create_run(vid, sid, db_path=db)
        latest = get_latest_run(sid, db)
        assert latest["id"] == r2

    def test_delete_run(self, db):
        sid = create_strategy(name="S", db_path=db)
        vid = create_version(sid, afl_content="Buy();", db_path=db)
        rid = create_run(vid, sid, db_path=db)
        assert delete_run(rid, db) is True
        assert get_run(rid, db) is None


# ---------------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------------

class TestDashboardHelpers:
    def test_get_strategy_info_found(self, db):
        sid = create_strategy(name="Info Test", symbol="NQ", db_path=db)
        info = get_strategy_info(sid, db)
        assert info["name"] == "Info Test"
        assert info["symbol"] == "NQ"

    def test_get_strategy_info_default(self, db):
        info = get_strategy_info("unknown-uuid", db)
        assert info["name"] == "Unknown Strategy"

    def test_get_strategy_summary(self, db):
        sid = create_strategy(name="Summary", db_path=db)
        v1 = create_version(sid, afl_content="v1", db_path=db)
        create_run(v1, sid, db_path=db)
        create_run(v1, sid, db_path=db)
        summary = get_strategy_summary(sid, db)
        assert summary["version_count"] == 1
        assert summary["run_count"] == 2

    def test_get_run_with_context(self, db):
        sid = create_strategy(name="Ctx", db_path=db)
        vid = create_version(sid, afl_content="Buy();", label="test ver", db_path=db)
        rid = create_run(vid, sid, db_path=db)
        ctx = get_run_with_context(rid, db)
        assert ctx is not None
        assert ctx["strategy"]["name"] == "Ctx"
        assert ctx["version"]["label"] == "test ver"

    def test_get_run_with_context_not_found(self, db):
        assert get_run_with_context("missing", db) is None


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

class TestSeed:
    def test_seed_populates_empty_db(self, db):
        seed_default_strategies(db)
        rows = list_strategies(db)
        assert len(rows) == 1
        assert "SMA Crossover" in rows[0]["name"]

        # Should also create a version
        versions = list_versions(rows[0]["id"], db)
        assert len(versions) >= 1
        assert versions[0]["afl_content"] is not None

    def test_seed_does_not_duplicate(self, db):
        seed_default_strategies(db)
        seed_default_strategies(db)
        assert len(list_strategies(db)) == 1
