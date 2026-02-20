"""
Tests for Trade Intelligence features (Section D):
  D1: Persistent trade/signal logging
  D2: Proximity-to-signal with AFL Param() parsing
  D3: Signal accuracy tracking
  D4: P&L attribution
  D5: Signal replay capability
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
    create_version,
    create_live_session,
    update_live_session,
    record_live_trade,
    record_live_signal,
    list_live_trades,
    list_live_sessions,
    list_live_signals,
    get_live_session,
    get_signal_accuracy,
    get_pnl_attribution,
    get_daily_pnl,
)
from scripts.signal_scanner import (
    parse_afl_params,
    parse_afl_conditions,
    compute_proximity,
)
from scripts.trade_replay import TradeReplay


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Create an isolated test database."""
    db_path = tmp_path / "test_ti.db"
    init_db(db_path)
    return db_path


@pytest.fixture
def strategy_with_session(db):
    """Create a strategy + version + live session with signals/trades."""
    sid = create_strategy("Test Strategy", summary="Testing", db_path=db)
    vid = create_version(sid, afl_content=SAMPLE_AFL, db_path=db)

    session_id = create_live_session(
        strategy_id=sid,
        version_id=vid,
        account_id="12345",
        account_name="TestAccount",
        symbol="NQH6",
        ami_symbol="NQ",
        bar_interval=1,
        config={"trade_enabled": True},
        db_path=db,
    )

    # Record signals
    record_live_signal(
        session_id=session_id,
        signal_type="Buy",
        symbol="NQ",
        close_price=18500.0,
        was_traded=True,
        indicators={"ADXvalue": 28.5, "TEMAvalue": 18490.0},
        strategy_id=sid,
        strategy_name="Test Strategy",
        signal_at="2025-01-10T10:30:00",
        db_path=db,
    )
    record_live_signal(
        session_id=session_id,
        signal_type="Short",
        symbol="NQ",
        close_price=18550.0,
        was_traded=False,
        was_deduped=True,
        indicators={"ADXvalue": 15.0, "TEMAvalue": 18560.0},
        strategy_id=sid,
        strategy_name="Test Strategy",
        signal_at="2025-01-10T11:00:00",
        db_path=db,
    )
    record_live_signal(
        session_id=session_id,
        signal_type="Buy",
        symbol="NQ",
        close_price=18600.0,
        was_traded=True,
        indicators={"ADXvalue": 30.0, "TEMAvalue": 18580.0},
        strategy_id=sid,
        strategy_name="Test Strategy",
        signal_at="2025-01-10T14:00:00",
        db_path=db,
    )

    # Record trades
    record_live_trade(
        session_id=session_id,
        signal_type="Buy",
        symbol="NQ",
        size=1,
        signal_price=18500.0,
        fill_price=18501.0,
        order_id="ORD001",
        status="filled",
        pnl=50.0,
        elapsed_seconds=2.5,
        indicators={"ADXvalue": 28.5},
        strategy_id=sid,
        version_id=vid,
        strategy_name="Test Strategy",
        signal_at="2025-01-10T10:30:00",
        executed_at="2025-01-10T10:30:03",
        db_path=db,
    )
    record_live_trade(
        session_id=session_id,
        signal_type="Buy",
        symbol="NQ",
        size=1,
        signal_price=18600.0,
        fill_price=18602.0,
        order_id="ORD002",
        status="filled",
        pnl=-25.0,
        elapsed_seconds=1.8,
        indicators={"ADXvalue": 30.0},
        strategy_id=sid,
        version_id=vid,
        strategy_name="Test Strategy",
        signal_at="2025-01-10T14:00:00",
        executed_at="2025-01-10T14:00:02",
        db_path=db,
    )

    return {
        "strategy_id": sid,
        "version_id": vid,
        "session_id": session_id,
        "db_path": db,
    }


SAMPLE_AFL = """
// Sample strategy AFL
temaLen = Param("TEMA Length", 20, 5, 50, 1);
adxLen = Param("ADX Length", 14, 5, 30, 1);
adxThreshold = Param("ADX Threshold", 25, 10, 40, 1);

TEMAvalue = TEMA(Close, temaLen);
ADXvalue = ADX(adxLen);

Buy = TEMAvalue > Ref(TEMAvalue, -1) AND ADXvalue > adxThreshold;
Short = TEMAvalue < Ref(TEMAvalue, -1) AND ADXvalue > adxThreshold;
"""


# ===========================================================================
# D1: Persistent trade/signal logging
# ===========================================================================

class TestD1PersistentLogging:
    """D1: Persistent trade journal - signals and trades recorded to DB."""

    def test_create_live_session(self, db):
        sid = create_strategy("S1", db_path=db)
        vid = create_version(sid, afl_content="Buy = 1;", db_path=db)
        session_id = create_live_session(
            strategy_id=sid, version_id=vid,
            account_id="999", account_name="Acc",
            symbol="NQ", ami_symbol="NQ",
            bar_interval=1, config={"x": 1},
            db_path=db,
        )
        assert session_id
        sess = get_live_session(session_id, db_path=db)
        assert sess is not None
        assert sess["status"] == "running"
        assert sess["account_name"] == "Acc"
        assert sess["config"]["x"] == 1

    def test_update_live_session(self, db):
        sid = create_strategy("S2", db_path=db)
        vid = create_version(sid, afl_content="Buy = 1;", db_path=db)
        session_id = create_live_session(
            strategy_id=sid, version_id=vid,
            db_path=db,
        )
        update_live_session(
            session_id, status="stopped",
            bars_injected=100, scans_run=50,
            alerts_fired=3, trades_placed=2,
            stopped_at="2025-01-10T15:00:00",
            db_path=db,
        )
        sess = get_live_session(session_id, db_path=db)
        assert sess["status"] == "stopped"
        assert sess["bars_injected"] == 100
        assert sess["scans_run"] == 50

    def test_record_and_list_signals(self, strategy_with_session):
        ctx = strategy_with_session
        signals = list_live_signals(
            session_id=ctx["session_id"],
            db_path=ctx["db_path"],
        )
        assert len(signals) == 3
        buy_signals = [s for s in signals if s["signal_type"] == "Buy"]
        assert len(buy_signals) == 2
        short_signals = [s for s in signals if s["signal_type"] == "Short"]
        assert len(short_signals) == 1
        assert short_signals[0]["was_deduped"]

    def test_record_and_list_trades(self, strategy_with_session):
        ctx = strategy_with_session
        trades = list_live_trades(
            session_id=ctx["session_id"],
            db_path=ctx["db_path"],
        )
        assert len(trades) == 2
        assert all(t["status"] == "filled" for t in trades)
        assert trades[0]["pnl"] is not None

    def test_list_live_sessions(self, strategy_with_session):
        ctx = strategy_with_session
        sessions = list_live_sessions(db_path=ctx["db_path"])
        assert len(sessions) >= 1
        assert sessions[0]["id"] == ctx["session_id"]

    def test_signal_has_indicators(self, strategy_with_session):
        ctx = strategy_with_session
        signals = list_live_signals(
            session_id=ctx["session_id"],
            db_path=ctx["db_path"],
        )
        sig = signals[0]
        assert "indicators" in sig
        assert isinstance(sig["indicators"], dict)

    def test_trade_has_indicators(self, strategy_with_session):
        ctx = strategy_with_session
        trades = list_live_trades(
            session_id=ctx["session_id"],
            db_path=ctx["db_path"],
        )
        trade = trades[0]
        assert "indicators" in trade
        assert isinstance(trade["indicators"], dict)


# ===========================================================================
# D2: Proximity-to-signal with AFL Param() parsing
# ===========================================================================

class TestD2ProximityToSignal:
    """D2: Proximity dashboard with AFL parsing."""

    def test_parse_afl_params(self):
        params = parse_afl_params(SAMPLE_AFL)
        assert "temaLen" in params
        assert params["temaLen"]["label"] == "TEMA Length"
        assert params["temaLen"]["default"] == 20.0
        assert params["temaLen"]["min"] == 5.0
        assert params["temaLen"]["max"] == 50.0
        assert params["temaLen"]["step"] == 1.0

        assert "adxThreshold" in params
        assert params["adxThreshold"]["default"] == 25.0

    def test_parse_afl_conditions(self):
        conds = parse_afl_conditions(SAMPLE_AFL)
        assert len(conds) >= 2
        buy_conds = [c for c in conds if c["signal"] == "Buy"]
        short_conds = [c for c in conds if c["signal"] == "Short"]
        assert len(buy_conds) >= 1
        assert len(short_conds) >= 1
        # Should find ADXvalue > adxThreshold
        adx_cond = [c for c in buy_conds if "ADXvalue" in c["lhs"]]
        assert len(adx_cond) >= 1
        assert adx_cond[0]["operator"] == ">"
        assert adx_cond[0]["rhs"] == "adxThreshold"

    def test_compute_proximity_above_threshold(self):
        conditions = [
            {"signal": "Buy", "lhs": "ADXvalue", "operator": ">",
             "rhs": "adxThreshold", "description": "ADXvalue > adxThreshold"},
        ]
        params = {"adxThreshold": {"default": 25.0}}
        indicators = {"ADXvalue": 30.0}  # above threshold

        result = compute_proximity(conditions, params, indicators)
        assert len(result) == 1
        assert result[0]["met"] is True
        assert result[0]["current_value"] == 30.0
        assert result[0]["threshold"] == 25.0

    def test_compute_proximity_below_threshold(self):
        conditions = [
            {"signal": "Buy", "lhs": "ADXvalue", "operator": ">",
             "rhs": "adxThreshold", "description": "ADXvalue > adxThreshold"},
        ]
        params = {"adxThreshold": {"default": 25.0}}
        indicators = {"ADXvalue": 15.0}  # below threshold

        result = compute_proximity(conditions, params, indicators)
        assert len(result) == 1
        assert result[0]["met"] is False
        assert result[0]["proximity_pct"] < 100.0

    def test_compute_proximity_literal_rhs(self):
        conditions = [
            {"signal": "Buy", "lhs": "RSI", "operator": "<",
             "rhs": "30", "description": "RSI < 30"},
        ]
        indicators = {"RSI": 25.0}  # below 30

        result = compute_proximity(conditions, {}, indicators)
        assert len(result) == 1
        assert result[0]["met"] is True
        assert result[0]["direction"] == "below"

    def test_compute_proximity_missing_indicator(self):
        conditions = [
            {"signal": "Buy", "lhs": "MACD", "operator": ">",
             "rhs": "0", "description": "MACD > 0"},
        ]
        indicators = {}  # no MACD value

        result = compute_proximity(conditions, {}, indicators)
        assert len(result) == 0

    def test_parse_params_empty_afl(self):
        params = parse_afl_params("")
        assert params == {}

    def test_parse_conditions_empty_afl(self):
        conds = parse_afl_conditions("")
        assert conds == []


# ===========================================================================
# D3: Signal accuracy tracking
# ===========================================================================

class TestD3SignalAccuracy:
    """D3: Signal accuracy with profitability analysis."""

    def test_signal_accuracy_totals(self, strategy_with_session):
        ctx = strategy_with_session
        acc = get_signal_accuracy(
            strategy_id=ctx["strategy_id"],
            days=365,
            db_path=ctx["db_path"],
        )
        assert acc["total_signals"] == 3
        assert acc["traded_signals"] == 2
        assert acc["deduped_signals"] == 1
        assert acc["days"] == 365

    def test_signal_accuracy_by_type(self, strategy_with_session):
        ctx = strategy_with_session
        acc = get_signal_accuracy(
            strategy_id=ctx["strategy_id"],
            days=365,
            db_path=ctx["db_path"],
        )
        assert "Buy" in acc["by_type"]
        assert acc["by_type"]["Buy"]["count"] == 2
        assert "Short" in acc["by_type"]
        assert acc["by_type"]["Short"]["count"] == 1

    def test_signal_accuracy_by_hour(self, strategy_with_session):
        ctx = strategy_with_session
        acc = get_signal_accuracy(
            strategy_id=ctx["strategy_id"],
            days=365,
            db_path=ctx["db_path"],
        )
        assert isinstance(acc["by_hour"], dict)
        # Should have entries for hours 10 and 14 (and possibly 11)
        hours = list(acc["by_hour"].keys())
        assert len(hours) >= 2

    def test_signal_accuracy_profitability(self, strategy_with_session):
        ctx = strategy_with_session
        acc = get_signal_accuracy(
            strategy_id=ctx["strategy_id"],
            days=365,
            db_path=ctx["db_path"],
        )
        assert "profitability" in acc
        if "Buy" in acc["profitability"]:
            buy_prof = acc["profitability"]["Buy"]
            assert "wins" in buy_prof
            assert "losses" in buy_prof
            assert "total_pnl" in buy_prof
            assert "win_rate" in buy_prof

    def test_signal_accuracy_no_data(self, db):
        acc = get_signal_accuracy(days=1, db_path=db)
        assert acc["total_signals"] == 0


# ===========================================================================
# D4: P&L attribution
# ===========================================================================

class TestD4PnlAttribution:
    """D4: P&L attribution by strategy, symbol, hour, weekday."""

    def test_pnl_by_strategy(self, strategy_with_session):
        ctx = strategy_with_session
        data = get_pnl_attribution(
            days=365, group_by="strategy",
            db_path=ctx["db_path"],
        )
        assert isinstance(data, list)
        if data:
            assert "group_key" in data[0]
            assert "total_pnl" in data[0]
            assert "trade_count" in data[0]

    def test_pnl_by_symbol(self, strategy_with_session):
        ctx = strategy_with_session
        data = get_pnl_attribution(
            days=365, group_by="symbol",
            db_path=ctx["db_path"],
        )
        assert isinstance(data, list)

    def test_pnl_by_hour(self, strategy_with_session):
        ctx = strategy_with_session
        data = get_pnl_attribution(
            days=365, group_by="hour",
            db_path=ctx["db_path"],
        )
        assert isinstance(data, list)

    def test_pnl_by_weekday(self, strategy_with_session):
        ctx = strategy_with_session
        data = get_pnl_attribution(
            days=365, group_by="weekday",
            db_path=ctx["db_path"],
        )
        assert isinstance(data, list)

    def test_daily_pnl(self, strategy_with_session):
        ctx = strategy_with_session
        pnl = get_daily_pnl(
            date_str="2025-01-10",
            db_path=ctx["db_path"],
        )
        assert isinstance(pnl, float)
        assert pnl == 25.0  # 50.0 + (-25.0)

    def test_daily_pnl_no_trades(self, db):
        pnl = get_daily_pnl(date_str="2020-01-01", db_path=db)
        assert pnl == 0.0

    def test_pnl_attribution_no_data(self, db):
        data = get_pnl_attribution(days=1, db_path=db)
        assert data == []


# ===========================================================================
# D5: Signal replay capability
# ===========================================================================

class TestD5SignalReplay:
    """D5: Signal replay — step through recorded sessions."""

    def test_replay_init(self):
        replay = TradeReplay(session_id="fake-session")
        assert replay.session_id == "fake-session"
        assert replay.progress["total"] == 0
        assert replay.progress["pct"] == 0

    def test_replay_load_session(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        count = replay.load_bars(source="session", db_path=ctx["db_path"])
        assert count == 5  # 3 signals + 2 trades

    def test_replay_step_forward(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        events = replay.step(1)
        assert len(events) == 1
        assert events[0]["index"] == 0
        assert replay.progress["current"] == 1

    def test_replay_step_multiple(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        events = replay.step(3)
        assert len(events) == 3
        assert replay.progress["current"] == 3

    def test_replay_step_back(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        replay.step(3)
        events = replay.step_back(1)
        assert len(events) == 1
        assert replay.progress["current"] == 2

    def test_replay_step_back_at_start(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        events = replay.step_back(1)
        assert len(events) == 0

    def test_replay_jump_to(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        events = replay.jump_to(3)
        assert len(events) == 1
        assert events[0]["index"] == 3

    def test_replay_step_to_end(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        count = replay.load_bars(source="session", db_path=ctx["db_path"])

        events = replay.step_to_end()
        assert len(events) == count
        assert replay.progress["done"]

    def test_replay_reset(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])
        replay.step(3)

        replay.reset()
        assert replay.progress["current"] == 0

    def test_replay_get_all_events(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        events = replay.get_all_events()
        assert len(events) == 5
        # Events should be sorted by timestamp
        timestamps = [e["timestamp"] for e in events if e["timestamp"]]
        assert timestamps == sorted(timestamps)

    def test_replay_get_summary(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        summary = replay.get_summary()
        assert summary["total_events"] == 5
        assert summary["actual_signals"] == 3
        assert summary["actual_trades"] == 2
        assert summary["total_pnl"] == 25.0
        assert "Buy" in summary["signal_types"]
        assert summary["session_id"] == ctx["session_id"]

    def test_replay_event_types(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        events = replay.get_all_events()
        signal_events = [e for e in events if e["event_type"] == "signal"]
        trade_events = [e for e in events if e["event_type"] == "trade"]
        assert len(signal_events) == 3
        assert len(trade_events) == 2

    def test_replay_with_afl_reevaluation(self, strategy_with_session, tmp_path):
        ctx = strategy_with_session
        # Write AFL to a temp file
        afl_path = tmp_path / "test.afl"
        afl_path.write_text(SAMPLE_AFL, encoding="utf-8")

        replay = TradeReplay(
            session_id=ctx["session_id"],
            strategy_afl_path=str(afl_path),
        )
        replay.load_bars(source="session", db_path=ctx["db_path"])

        events = replay.get_all_events()
        # Events with indicators should have proximity data
        has_prox = [e for e in events if e["proximity"]]
        # At least some events should have proximity calculations
        assert len(has_prox) >= 1

    def test_replay_with_param_overrides(self, strategy_with_session, tmp_path):
        ctx = strategy_with_session
        afl_path = tmp_path / "test.afl"
        afl_path.write_text(SAMPLE_AFL, encoding="utf-8")

        replay = TradeReplay(
            session_id=ctx["session_id"],
            strategy_afl_path=str(afl_path),
            param_overrides={"adxThreshold": 50.0},  # Very high threshold
        )
        replay.load_bars(source="session", db_path=ctx["db_path"])

        summary = replay.get_summary()
        # With threshold at 50, ADX values of 28.5 and 30 won't trigger
        assert summary["total_events"] == 5

    def test_replay_empty_session(self, db):
        sid = create_strategy("Empty", db_path=db)
        vid = create_version(sid, afl_content="Buy = 1;", db_path=db)
        session_id = create_live_session(
            strategy_id=sid, version_id=vid, db_path=db,
        )

        replay = TradeReplay(session_id=session_id)
        count = replay.load_bars(source="session", db_path=db)
        assert count == 0
        assert replay.get_summary()["total_events"] == 0

    def test_replay_progress_percentage(self, strategy_with_session):
        ctx = strategy_with_session
        replay = TradeReplay(session_id=ctx["session_id"])
        replay.load_bars(source="session", db_path=ctx["db_path"])

        assert replay.progress["pct"] == 0
        replay.step(2)
        assert replay.progress["pct"] == 40.0  # 2/5 = 40%
        replay.step(3)
        assert replay.progress["pct"] == 100.0
        assert replay.progress["done"]


# ===========================================================================
# Dashboard route tests
# ===========================================================================

class TestDashboardRoutes:
    """Test Flask routes for trade intelligence pages."""

    @pytest.fixture
    def client(self):
        from dashboard.app import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c

    def test_trades_page(self, client):
        resp = client.get("/trades")
        assert resp.status_code == 200

    def test_analytics_page(self, client):
        resp = client.get("/analytics")
        assert resp.status_code == 200

    def test_replay_page(self, client):
        resp = client.get("/replay")
        assert resp.status_code == 200

    def test_api_trades(self, client):
        resp = client.get("/api/trades")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_api_trades_pnl(self, client):
        resp = client.get("/api/trades/pnl?group_by=strategy&days=30")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_api_signal_accuracy(self, client):
        resp = client.get("/api/signals/accuracy?days=30")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "total_signals" in data

    def test_api_signals(self, client):
        resp = client.get("/api/signals")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_api_daily_pnl(self, client):
        resp = client.get("/api/trades/daily-pnl?date=2025-01-10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "pnl" in data

    def test_api_replay_no_session(self, client):
        resp = client.get("/api/replay/summary")
        assert resp.status_code == 409

    def test_api_replay_start_requires_session_id(self, client):
        resp = client.post(
            "/api/replay/start",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "session_id" in data.get("error", "")


# ===========================================================================
# Template existence tests
# ===========================================================================

class TestTemplatesExist:
    """Verify all trade intelligence templates exist."""

    def test_analytics_template(self):
        path = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "analytics.html"
        assert path.exists(), "analytics.html template missing"

    def test_replay_template(self):
        path = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "replay.html"
        assert path.exists(), "replay.html template missing"

    def test_trades_template(self):
        path = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "trades.html"
        assert path.exists(), "trades.html template missing"

    def test_live_dashboard_template(self):
        path = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "live_dashboard.html"
        assert path.exists(), "live_dashboard.html template missing"


class TestNavLinks:
    """Verify navigation links in base.html."""

    def test_base_has_trades_link(self):
        path = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "base.html"
        content = path.read_text(encoding="utf-8")
        assert "/trades" in content, "base.html missing /trades nav link"

    def test_base_has_analytics_link(self):
        path = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "base.html"
        content = path.read_text(encoding="utf-8")
        assert "/analytics" in content, "base.html missing /analytics nav link"

    def test_base_has_replay_link(self):
        path = Path(__file__).resolve().parent.parent / "dashboard" / "templates" / "base.html"
        content = path.read_text(encoding="utf-8")
        assert "/replay" in content, "base.html missing /replay nav link"
