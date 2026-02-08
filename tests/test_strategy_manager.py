"""
Tests for scripts/strategy_manager.py -- strategy CRUD operations.

All filesystem operations use monkeypatched STRATEGIES_DIR (tmp_path).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _patch_strategies_dir(tmp_path, monkeypatch):
    """Redirect STRATEGIES_DIR to a temp directory for every test."""
    monkeypatch.setattr("scripts.strategy_manager.STRATEGIES_DIR", tmp_path)
    monkeypatch.setattr("scripts.strategy_manager.MANIFEST_PATH",
                        tmp_path / "manifest.json")


@pytest.fixture
def _create_sample_strategy(tmp_path):
    """Helper to create a sample strategy in the temp dir."""
    from scripts.strategy_manager import create_strategy
    sid = create_strategy(
        name="Test MA Crossover",
        description="A test strategy",
        source="manual",
        afl_content="Buy = Cross(MA(C,10), MA(C,50));\nSell = Cross(MA(C,50), MA(C,10));",
    )
    return sid


# ── Create ────────────────────────────────────────────────────────────────────────

class TestCreateStrategy:

    def test_create_strategy_returns_id(self, tmp_path):
        from scripts.strategy_manager import create_strategy
        sid = create_strategy(name="My Strategy", description="test")
        assert isinstance(sid, str)
        assert "my_strategy" in sid

    def test_create_strategy_creates_directory(self, tmp_path):
        from scripts.strategy_manager import create_strategy
        sid = create_strategy(name="My Strategy")
        assert (tmp_path / sid).is_dir()
        assert (tmp_path / sid / "versions").is_dir()

    def test_create_strategy_writes_metadata(self, tmp_path):
        from scripts.strategy_manager import create_strategy
        sid = create_strategy(name="My Strategy", description="A test")
        meta_path = tmp_path / sid / "strategy.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["name"] == "My Strategy"
        assert meta["description"] == "A test"
        assert meta["status"] == "draft"

    def test_create_strategy_writes_afl(self, tmp_path):
        from scripts.strategy_manager import create_strategy
        sid = create_strategy(name="My Strategy", afl_content="Buy = 1;")
        afl_path = tmp_path / sid / "strategy.afl"
        assert afl_path.exists()
        assert afl_path.read_text(encoding="utf-8") == "Buy = 1;"

    def test_create_strategy_updates_manifest(self, tmp_path):
        from scripts.strategy_manager import create_strategy
        sid = create_strategy(name="My Strategy")
        manifest = json.loads(
            (tmp_path / "manifest.json").read_text(encoding="utf-8")
        )
        ids = [s["id"] for s in manifest["strategies"]]
        assert sid in ids


# ── Read ──────────────────────────────────────────────────────────────────────────

class TestGetStrategy:

    def test_get_strategy_success(self, _create_sample_strategy):
        from scripts.strategy_manager import get_strategy
        meta = get_strategy(_create_sample_strategy)
        assert meta["name"] == "Test MA Crossover"
        assert meta["status"] == "draft"

    def test_get_strategy_not_found(self):
        from scripts.strategy_manager import get_strategy
        with pytest.raises(FileNotFoundError):
            get_strategy("nonexistent_strategy_id")


class TestListStrategies:

    def test_list_empty(self):
        from scripts.strategy_manager import list_strategies
        assert list_strategies() == []

    def test_list_with_strategies(self, tmp_path):
        from scripts.strategy_manager import create_strategy, list_strategies
        create_strategy(name="Strategy A")
        create_strategy(name="Strategy B")
        strategies = list_strategies()
        assert len(strategies) == 2
        names = [s["name"] for s in strategies]
        assert "Strategy A" in names
        assert "Strategy B" in names


# ── Update ────────────────────────────────────────────────────────────────────────

class TestUpdateStrategyStatus:

    def test_update_status(self, _create_sample_strategy):
        from scripts.strategy_manager import (
            get_strategy, update_strategy_status,
        )
        update_strategy_status(_create_sample_strategy, "tested")
        meta = get_strategy(_create_sample_strategy)
        assert meta["status"] == "tested"
        assert meta["last_tested_at"] is not None

    def test_update_manifest_entry(self, _create_sample_strategy, tmp_path):
        from scripts.strategy_manager import update_strategy_status
        update_strategy_status(_create_sample_strategy, "approved")
        manifest = json.loads(
            (tmp_path / "manifest.json").read_text(encoding="utf-8")
        )
        entry = [s for s in manifest["strategies"]
                 if s["id"] == _create_sample_strategy][0]
        assert entry["status"] == "approved"


# ── AFL I/O ───────────────────────────────────────────────────────────────────────

class TestStrategyAfl:

    def test_get_strategy_afl(self, _create_sample_strategy):
        from scripts.strategy_manager import get_strategy_afl
        afl = get_strategy_afl(_create_sample_strategy)
        assert "Cross(MA(C,10)" in afl

    def test_get_strategy_afl_empty(self, tmp_path):
        from scripts.strategy_manager import create_strategy, get_strategy_afl
        sid = create_strategy(name="Empty Strategy")
        assert get_strategy_afl(sid) == ""

    def test_save_strategy_afl(self, _create_sample_strategy):
        from scripts.strategy_manager import save_strategy_afl, get_strategy_afl
        ok, msg = save_strategy_afl(_create_sample_strategy, "Buy = RSI(14) < 30;")
        assert ok is True
        assert get_strategy_afl(_create_sample_strategy) == "Buy = RSI(14) < 30;"


# ── APX Build ─────────────────────────────────────────────────────────────────────

class TestBuildStrategyApx:

    def test_build_apx_success(self, _create_sample_strategy):
        from scripts.strategy_manager import build_strategy_apx, get_strategy
        ok, result = build_strategy_apx(_create_sample_strategy)
        assert ok is True
        meta = get_strategy(_create_sample_strategy)
        assert Path(meta["apx_path"]).exists()


# ── Versioning ────────────────────────────────────────────────────────────────────

class TestStrategyVersioning:

    def test_save_version(self, _create_sample_strategy):
        from scripts.strategy_manager import (
            save_strategy_version, get_strategy_versions,
        )
        ok, filename = save_strategy_version(
            _create_sample_strategy, "Buy = 1;", label="initial"
        )
        assert ok is True
        assert filename.startswith("v001_")
        assert "initial" in filename

        versions = get_strategy_versions(_create_sample_strategy)
        assert len(versions) == 1

    def test_version_numbering(self, _create_sample_strategy):
        from scripts.strategy_manager import save_strategy_version
        save_strategy_version(_create_sample_strategy, "Buy = 1;")
        ok, filename = save_strategy_version(_create_sample_strategy, "Buy = 2;")
        assert ok is True
        assert filename.startswith("v002_")


# ── Delete ────────────────────────────────────────────────────────────────────────

class TestDeleteStrategy:

    def test_delete_strategy(self, _create_sample_strategy, tmp_path):
        from scripts.strategy_manager import delete_strategy, list_strategies
        delete_strategy(_create_sample_strategy)
        assert not (tmp_path / _create_sample_strategy).exists()
        assert len(list_strategies()) == 0


# ── ID Generation ─────────────────────────────────────────────────────────────────

class TestGenerateStrategyId:

    def test_generates_safe_id(self):
        from scripts.strategy_manager import generate_strategy_id
        sid = generate_strategy_id("RSI Momentum (14)")
        assert " " not in sid
        assert "(" not in sid
        assert "rsi_momentum" in sid

    def test_truncates_long_names(self):
        from scripts.strategy_manager import generate_strategy_id
        sid = generate_strategy_id("A" * 100)
        # 40 chars for name + _ + timestamp
        parts = sid.split("_")
        name_part = "_".join(parts[:-2])  # everything except timestamp
        assert len(name_part) <= 40
