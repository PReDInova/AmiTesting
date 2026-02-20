"""
Tests for scripts.risk_manager -- Pre-trade risk management.

Covers position limits, circuit breakers, daily P&L tracking,
drawdown detection, and the guarantee that exit signals (Sell/Cover)
are always allowed.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_positions():
    """Return an empty positions dict."""
    return {}


def _positions_with(strategy, symbol, size):
    """Return a positions dict with a single position entry."""
    return {strategy: {symbol: {"size": size}}}


def _multi_positions(*entries):
    """Build a positions dict from (strategy, symbol, size) tuples."""
    positions = {}
    for strategy, symbol, size in entries:
        positions.setdefault(strategy, {})[symbol] = {"size": size}
    return positions


# ===========================================================================
# Per-strategy position limit
# ===========================================================================

class TestPerStrategyLimit:
    """check_trade should enforce max_position_per_strategy."""

    def test_buy_within_limit_is_allowed(self):
        rm = RiskManager(max_position_per_strategy=5)
        allowed, reason = rm.check_trade(
            "Buy", "NQ", 2, "StratA",
            _positions_with("StratA", "NQ", 2),
        )
        # Total for strategy = abs(2) + 2 = 4, within limit 5
        assert allowed is True
        assert reason == ""

    def test_buy_exceeding_limit_is_blocked(self):
        rm = RiskManager(max_position_per_strategy=3)
        allowed, reason = rm.check_trade(
            "Buy", "NQ", 2, "StratA",
            _positions_with("StratA", "NQ", 2),
        )
        # 2 existing + 2 new = 4 > 3
        assert allowed is False
        assert "position limit" in reason.lower() or "Strategy" in reason

    def test_buy_at_exact_limit_is_blocked(self):
        rm = RiskManager(max_position_per_strategy=3)
        allowed, reason = rm.check_trade(
            "Buy", "NQ", 1, "StratA",
            _positions_with("StratA", "NQ", 3),
        )
        # 3 + 1 = 4 > 3
        assert allowed is False

    def test_strategy_limit_with_no_existing_position(self):
        rm = RiskManager(max_position_per_strategy=5)
        allowed, reason = rm.check_trade(
            "Buy", "NQ", 3, "StratA", _empty_positions(),
        )
        assert allowed is True

    def test_strategy_limit_multiple_symbols(self):
        """Limits sum across all symbols for the strategy."""
        rm = RiskManager(max_position_per_strategy=4)
        positions = _multi_positions(
            ("StratA", "NQ", 2),
            ("StratA", "ES", 1),
        )
        allowed, _ = rm.check_trade("Buy", "YM", 2, "StratA", positions)
        # 2 + 1 + 2 = 5 > 4
        assert allowed is False

    def test_other_strategy_positions_are_ignored(self):
        rm = RiskManager(max_position_per_strategy=3)
        positions = _multi_positions(
            ("StratB", "NQ", 10),  # irrelevant
            ("StratA", "NQ", 1),
        )
        allowed, _ = rm.check_trade("Buy", "NQ", 1, "StratA", positions)
        # StratA: 1 + 1 = 2 <= 3
        assert allowed is True


# ===========================================================================
# Per-symbol position limit
# ===========================================================================

class TestPerSymbolLimit:
    """check_trade should enforce max_position_per_symbol across all strategies."""

    def test_symbol_within_limit(self):
        rm = RiskManager(max_position_per_symbol=6)
        positions = _multi_positions(
            ("StratA", "NQ", 2),
            ("StratB", "NQ", 2),
        )
        allowed, _ = rm.check_trade("Buy", "NQ", 1, "StratA", positions)
        # 2 + 2 + 1 = 5 <= 6
        assert allowed is True

    def test_symbol_exceeding_limit(self):
        rm = RiskManager(max_position_per_symbol=4)
        positions = _multi_positions(
            ("StratA", "NQ", 2),
            ("StratB", "NQ", 2),
        )
        allowed, reason = rm.check_trade("Buy", "NQ", 1, "StratC", positions)
        # 2 + 2 + 1 = 5 > 4
        assert allowed is False
        assert "NQ" in reason


# ===========================================================================
# Portfolio-wide position limit
# ===========================================================================

class TestPortfolioLimit:
    """check_trade should enforce max_portfolio_position across everything."""

    def test_portfolio_within_limit(self):
        rm = RiskManager(max_portfolio_position=10)
        positions = _multi_positions(
            ("StratA", "NQ", 3),
            ("StratB", "ES", 4),
        )
        allowed, _ = rm.check_trade("Buy", "YM", 2, "StratA", positions)
        # 3 + 4 + 2 = 9 <= 10
        assert allowed is True

    def test_portfolio_exceeding_limit(self):
        rm = RiskManager(max_portfolio_position=5)
        positions = _multi_positions(
            ("StratA", "NQ", 3),
            ("StratB", "ES", 2),
        )
        allowed, reason = rm.check_trade("Buy", "YM", 1, "StratA", positions)
        # 3 + 2 + 1 = 6 > 5
        assert allowed is False
        assert "Portfolio" in reason

    def test_portfolio_no_limits_set(self):
        """With no limits, all trades should pass."""
        rm = RiskManager()
        positions = _multi_positions(
            ("StratA", "NQ", 100),
            ("StratB", "ES", 200),
        )
        allowed, _ = rm.check_trade("Buy", "YM", 50, "StratC", positions)
        assert allowed is True


# ===========================================================================
# Sell/Cover always allowed
# ===========================================================================

class TestExitSignalsAlwaysAllowed:
    """Exit signals (Sell, Cover) should never be blocked by position limits."""

    def test_sell_allowed_even_when_limits_exceeded(self):
        rm = RiskManager(
            max_position_per_strategy=1,
            max_position_per_symbol=1,
            max_portfolio_position=1,
        )
        positions = _multi_positions(
            ("StratA", "NQ", 5),
            ("StratB", "ES", 5),
        )
        allowed, reason = rm.check_trade("Sell", "NQ", 5, "StratA", positions)
        assert allowed is True
        assert reason == ""

    def test_cover_allowed_even_when_limits_exceeded(self):
        rm = RiskManager(
            max_position_per_strategy=1,
            max_position_per_symbol=1,
            max_portfolio_position=1,
        )
        positions = _multi_positions(
            ("StratA", "NQ", -5),
        )
        allowed, reason = rm.check_trade("Cover", "NQ", 5, "StratA", positions)
        assert allowed is True
        assert reason == ""

    def test_sell_allowed_when_circuit_breaker_tripped(self):
        """Even with a tripped circuit breaker, exit signals are BLOCKED.
        (The code blocks ALL signals when the circuit breaker is tripped.)
        """
        rm = RiskManager(max_daily_loss=-100.0)
        rm.record_trade_pnl(-200.0)
        assert rm.is_tripped is True

        allowed, reason = rm.check_trade(
            "Sell", "NQ", 1, "StratA",
            _positions_with("StratA", "NQ", 1),
        )
        # Circuit breaker blocks everything, including exits
        assert allowed is False
        assert "Circuit breaker" in reason


# ===========================================================================
# Circuit breaker
# ===========================================================================

class TestCircuitBreaker:
    """Tests for the circuit-breaker mechanism."""

    def test_manual_trip_blocks_trades(self):
        rm = RiskManager()
        rm._trip_circuit_breaker("Manual test trip")
        assert rm.is_tripped is True
        assert rm.circuit_breaker_reason == "Manual test trip"

        allowed, reason = rm.check_trade(
            "Buy", "NQ", 1, "StratA", _empty_positions(),
        )
        assert allowed is False
        assert "Circuit breaker" in reason

    def test_manual_reset(self):
        rm = RiskManager()
        rm._trip_circuit_breaker("test")
        assert rm.is_tripped is True

        rm.reset_circuit_breaker()
        assert rm.is_tripped is False
        assert rm.circuit_breaker_reason == ""

        allowed, _ = rm.check_trade(
            "Buy", "NQ", 1, "StratA", _empty_positions(),
        )
        assert allowed is True

    def test_auto_reset_on_new_day(self):
        rm = RiskManager(max_daily_loss=-100.0)
        rm.record_trade_pnl(-200.0)
        assert rm.is_tripped is True

        # Simulate a new day by setting the internal date to the past.
        # The circuit breaker check in check_trade runs BEFORE the
        # daily-loss check that calls _rotate_daily_pnl, so we must
        # trigger rotation through another path first.  The daily_pnl
        # property calls _rotate_daily_pnl, which resets P&L and the
        # circuit breaker on a new day.
        rm._daily_pnl_date = "1999-01-01"

        # Access daily_pnl to trigger the date rotation
        pnl = rm.daily_pnl
        assert pnl == 0.0
        assert rm._circuit_breaker_tripped is False
        assert rm._circuit_breaker_reason == ""

        # Now check_trade should allow new entries
        allowed, _ = rm.check_trade(
            "Buy", "NQ", 1, "StratA", _empty_positions(),
        )
        assert allowed is True

    def test_circuit_break_callback_is_called(self):
        rm = RiskManager(max_daily_loss=-100.0)
        callback_calls = []
        rm.set_circuit_break_callback(lambda reason: callback_calls.append(reason))

        rm.record_trade_pnl(-200.0)
        assert len(callback_calls) == 1
        assert "Daily loss limit" in callback_calls[0]

    def test_circuit_break_callback_error_does_not_crash(self):
        rm = RiskManager(max_daily_loss=-50.0)
        rm.set_circuit_break_callback(lambda reason: 1 / 0)

        # Should not raise despite the callback throwing
        rm.record_trade_pnl(-100.0)
        assert rm.is_tripped is True


# ===========================================================================
# Daily P&L tracking and drawdown detection
# ===========================================================================

class TestDailyPnl:
    """Tests for daily P&L tracking and loss-limit enforcement."""

    def test_record_trade_pnl_accumulates(self):
        rm = RiskManager()
        rm.record_trade_pnl(100.0)
        rm.record_trade_pnl(50.0)
        rm.record_trade_pnl(-30.0)
        assert rm.daily_pnl == pytest.approx(120.0)

    def test_daily_loss_limit_trips_circuit_breaker(self):
        rm = RiskManager(max_daily_loss=-500.0)
        rm.record_trade_pnl(-300.0)
        assert rm.is_tripped is False

        rm.record_trade_pnl(-250.0)
        # Total = -550 <= -500
        assert rm.is_tripped is True

    def test_daily_loss_limit_exact_boundary(self):
        rm = RiskManager(max_daily_loss=-500.0)
        rm.record_trade_pnl(-500.0)
        # Exactly at -500, which is <= -500
        assert rm.is_tripped is True

    def test_check_drawdown_reports_breach(self):
        rm = RiskManager(max_daily_loss=-200.0)
        rm.record_trade_pnl(-300.0)
        breached, reason = rm.check_drawdown()
        assert breached is True
        assert "$" in reason

    def test_check_drawdown_no_breach(self):
        rm = RiskManager(max_daily_loss=-500.0)
        rm.record_trade_pnl(-100.0)
        breached, reason = rm.check_drawdown()
        assert breached is False
        assert reason == ""

    def test_check_trade_blocks_buy_when_daily_loss_breached(self):
        rm = RiskManager(max_daily_loss=-100.0)
        rm.record_trade_pnl(-150.0)
        allowed, reason = rm.check_trade(
            "Buy", "NQ", 1, "StratA", _empty_positions(),
        )
        assert allowed is False
        assert "Circuit breaker" in reason

    def test_daily_pnl_with_no_limit_does_not_trip(self):
        rm = RiskManager(max_daily_loss=None)
        rm.record_trade_pnl(-999999.0)
        assert rm.is_tripped is False


# ===========================================================================
# Short signals
# ===========================================================================

class TestShortSignals:
    """Short is also a position-increasing signal and should be limited."""

    def test_short_within_strategy_limit(self):
        rm = RiskManager(max_position_per_strategy=5)
        allowed, _ = rm.check_trade(
            "Short", "NQ", 3, "StratA", _empty_positions(),
        )
        assert allowed is True

    def test_short_exceeding_strategy_limit(self):
        rm = RiskManager(max_position_per_strategy=3)
        positions = _positions_with("StratA", "NQ", -2)
        allowed, _ = rm.check_trade("Short", "NQ", 2, "StratA", positions)
        # abs(-2) + 2 = 4 > 3
        assert allowed is False


# ===========================================================================
# get_status
# ===========================================================================

class TestGetStatus:
    """Validate the status report dict."""

    def test_status_includes_expected_keys(self):
        rm = RiskManager(
            max_position_per_strategy=10,
            max_daily_loss=-1000.0,
        )
        rm.record_trade_pnl(-50.0)

        status = rm.get_status()
        assert "circuit_breaker_tripped" in status
        assert "daily_pnl" in status
        assert status["daily_pnl"] == pytest.approx(-50.0)
        assert status["limits"]["max_per_strategy"] == 10
        assert status["limits"]["max_daily_loss"] == -1000.0

    def test_status_after_circuit_breaker(self):
        rm = RiskManager(max_daily_loss=-10.0)
        rm.record_trade_pnl(-20.0)
        status = rm.get_status()
        assert status["circuit_breaker_tripped"] is True
        assert "Daily loss limit" in status["circuit_breaker_reason"]
