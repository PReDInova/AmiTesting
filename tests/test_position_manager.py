"""
Tests for scripts.position_manager -- Position tracking and P&L calculation.

Covers position updates (buy, sell, short, cover), position limit
enforcement, portfolio summary, and market price updates for
unrealized P&L.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Mock strategy_db before importing PositionManager so it does not
# attempt to connect to a real database.
sys.modules.setdefault("scripts.strategy_db", MagicMock())

from scripts.position_manager import (
    PositionManager,
    _same_sign,
    _is_reducing,
    _is_adding,
)


# ===========================================================================
# Module-level helpers
# ===========================================================================

class TestHelperFunctions:
    """Test the module-level helper functions."""

    def test_same_sign_both_positive(self):
        assert _same_sign(3, 5) is True

    def test_same_sign_both_negative(self):
        assert _same_sign(-3, -5) is True

    def test_same_sign_different_signs(self):
        assert _same_sign(3, -5) is False
        assert _same_sign(-3, 5) is False

    def test_same_sign_zero(self):
        assert _same_sign(0, 5) is False
        assert _same_sign(5, 0) is False
        assert _same_sign(0, 0) is False

    def test_is_reducing_long_with_negative_delta(self):
        assert _is_reducing(5, -2) is True

    def test_is_reducing_long_with_positive_delta(self):
        assert _is_reducing(5, 2) is False

    def test_is_reducing_short_with_positive_delta(self):
        assert _is_reducing(-5, 2) is True

    def test_is_reducing_short_with_negative_delta(self):
        assert _is_reducing(-5, -2) is False

    def test_is_reducing_flat(self):
        assert _is_reducing(0, 5) is False
        assert _is_reducing(0, -5) is False

    def test_is_adding_long_with_positive_delta(self):
        assert _is_adding(5, 2) is True

    def test_is_adding_long_with_negative_delta(self):
        assert _is_adding(5, -2) is False

    def test_is_adding_short_with_negative_delta(self):
        assert _is_adding(-5, -2) is True

    def test_is_adding_short_with_positive_delta(self):
        assert _is_adding(-5, 2) is False

    def test_is_adding_flat(self):
        assert _is_adding(0, 5) is False
        assert _is_adding(0, -5) is False


# ===========================================================================
# Position updates -- Buy
# ===========================================================================

class TestBuyUpdates:
    """Test position updates for Buy signals."""

    def test_open_long_from_flat(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 18500.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 2
        assert pos["avg_price"] == 18500.0

    def test_add_to_long_updates_weighted_avg(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_position("StratA", "NQ", "Buy", 2, 110.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 4
        # Weighted avg: (100*2 + 110*2) / 4 = 105
        assert pos["avg_price"] == pytest.approx(105.0)

    def test_buy_to_cover_short(self):
        """Buy/Cover on a short position should realize P&L."""
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 2, 200.0)
        pm.update_position("StratA", "NQ", "Cover", 2, 190.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 0
        # P&L: 2 * (200 - 190) = 20

    def test_invalid_signal_type_raises(self):
        pm = PositionManager()
        with pytest.raises(ValueError, match="Invalid signal_type"):
            pm.update_position("StratA", "NQ", "Invalid", 1, 100.0)


# ===========================================================================
# Position updates -- Sell
# ===========================================================================

class TestSellUpdates:
    """Test position updates for Sell signals."""

    def test_close_long_at_profit(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 2, 120.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 0

    def test_partial_close_long(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 4, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 2, 110.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 2
        assert pos["avg_price"] == pytest.approx(100.0)  # unchanged

    def test_sell_more_than_position_flips_to_short(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 5, 120.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == -3
        assert pos["avg_price"] == pytest.approx(120.0)


# ===========================================================================
# Position updates -- Short
# ===========================================================================

class TestShortUpdates:
    """Test position updates for Short signals."""

    def test_open_short_from_flat(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 3, 200.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == -3
        assert pos["avg_price"] == 200.0

    def test_add_to_short_updates_weighted_avg(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 2, 200.0)
        pm.update_position("StratA", "NQ", "Short", 2, 210.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == -4
        # Weighted avg: (200*2 + 210*2) / 4 = 205
        assert pos["avg_price"] == pytest.approx(205.0)


# ===========================================================================
# Position updates -- Cover
# ===========================================================================

class TestCoverUpdates:
    """Test position updates for Cover signals."""

    def test_cover_short_at_profit(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 3, 200.0)
        pm.update_position("StratA", "NQ", "Cover", 3, 190.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 0

    def test_cover_short_at_loss(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 2, 200.0)
        pm.update_position("StratA", "NQ", "Cover", 2, 210.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 0

    def test_partial_cover_short(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 4, 200.0)
        pm.update_position("StratA", "NQ", "Cover", 2, 190.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == -2
        assert pos["avg_price"] == pytest.approx(200.0)

    def test_cover_more_than_short_flips_to_long(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 2, 200.0)
        pm.update_position("StratA", "NQ", "Cover", 5, 190.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 3
        assert pos["avg_price"] == pytest.approx(190.0)


# ===========================================================================
# Realized P&L
# ===========================================================================

class TestRealizedPnl:
    """Verify realized P&L is calculated correctly on position reduction."""

    def test_long_close_positive_pnl(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 2, 120.0)
        # P&L = 2 * (120 - 100) = 40
        daily_pnl = pm.get_daily_realized_pnl()
        assert daily_pnl == pytest.approx(40.0)

    def test_long_close_negative_pnl(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 2, 90.0)
        # P&L = 2 * (90 - 100) = -20
        daily_pnl = pm.get_daily_realized_pnl()
        assert daily_pnl == pytest.approx(-20.0)

    def test_short_close_positive_pnl(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 3, 200.0)
        pm.update_position("StratA", "NQ", "Cover", 3, 190.0)
        # P&L = 3 * (200 - 190) = 30
        daily_pnl = pm.get_daily_realized_pnl()
        assert daily_pnl == pytest.approx(30.0)

    def test_short_close_negative_pnl(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 2, 200.0)
        pm.update_position("StratA", "NQ", "Cover", 2, 210.0)
        # P&L = 2 * (200 - 210) = -20
        daily_pnl = pm.get_daily_realized_pnl()
        assert daily_pnl == pytest.approx(-20.0)

    def test_partial_close_pnl(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 4, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 2, 110.0)
        # P&L on 2 closed = 2 * (110 - 100) = 20
        daily_pnl = pm.get_daily_realized_pnl()
        assert daily_pnl == pytest.approx(20.0)

    def test_accumulated_pnl_across_trades(self):
        pm = PositionManager()
        # Trade 1: +20
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 2, 110.0)
        # Trade 2: +30
        pm.update_position("StratA", "ES", "Short", 3, 200.0)
        pm.update_position("StratA", "ES", "Cover", 3, 190.0)

        daily_pnl = pm.get_daily_realized_pnl()
        assert daily_pnl == pytest.approx(50.0)


# ===========================================================================
# Position limit enforcement
# ===========================================================================

class TestPositionLimits:
    """Test check_position_limits method."""

    def test_per_strategy_limit_allows_within(self):
        pm = PositionManager(max_per_strategy=3)
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        allowed, reason = pm.check_position_limits("StratA", "ES", 1)
        assert allowed is True
        assert reason == ""

    def test_per_strategy_limit_blocks_at_max(self):
        pm = PositionManager(max_per_strategy=2)
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        pm.update_position("StratA", "ES", "Buy", 1, 200.0)
        allowed, reason = pm.check_position_limits("StratA", "YM", 1)
        assert allowed is False
        assert "max_per_strategy" in reason

    def test_per_strategy_limit_existing_symbol_allowed(self):
        """Adding to an existing symbol should not count as a new slot."""
        pm = PositionManager(max_per_strategy=1)
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        # NQ already has a position, so this should be allowed
        allowed, reason = pm.check_position_limits("StratA", "NQ", 1)
        assert allowed is True

    def test_per_symbol_limit_allows_within(self):
        pm = PositionManager(max_per_symbol=5)
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        allowed, reason = pm.check_position_limits("StratB", "NQ", 2)
        assert allowed is True

    def test_per_symbol_limit_blocks_excess(self):
        pm = PositionManager(max_per_symbol=3)
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        allowed, reason = pm.check_position_limits("StratB", "NQ", 2)
        # 2 + 2 = 4 > 3
        assert allowed is False
        assert "max_per_symbol" in reason

    def test_portfolio_limit_allows_within(self):
        pm = PositionManager(max_portfolio=5)
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        pm.update_position("StratA", "ES", "Buy", 1, 200.0)
        allowed, reason = pm.check_position_limits("StratA", "YM", 1)
        assert allowed is True

    def test_portfolio_limit_blocks_at_max(self):
        pm = PositionManager(max_portfolio=2)
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        pm.update_position("StratB", "ES", "Buy", 1, 200.0)
        allowed, reason = pm.check_position_limits("StratC", "YM", 1)
        assert allowed is False
        assert "max_portfolio" in reason

    def test_no_limits_set_allows_everything(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 10, 100.0)
        allowed, reason = pm.check_position_limits("StratA", "ES", 100)
        assert allowed is True
        assert reason == ""


# ===========================================================================
# Portfolio summary
# ===========================================================================

class TestPortfolioSummary:
    """Test get_portfolio_summary."""

    def test_empty_portfolio(self):
        pm = PositionManager()
        summary = pm.get_portfolio_summary()
        assert summary["total_positions"] == 0
        assert summary["total_unrealized_pnl"] == 0.0
        assert summary["total_realized_pnl"] == 0.0
        assert summary["strategies"] == {}

    def test_single_open_position(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_market_price("NQ", 110.0)

        summary = pm.get_portfolio_summary()
        assert summary["total_positions"] == 1
        # Unrealized: 2 * (110 - 100) = 20
        assert summary["total_unrealized_pnl"] == pytest.approx(20.0)
        assert "StratA" in summary["strategies"]
        assert summary["strategies"]["StratA"]["position_count"] == 1

    def test_multiple_strategies_and_symbols(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        pm.update_position("StratA", "ES", "Short", 1, 200.0)
        pm.update_position("StratB", "NQ", "Buy", 1, 105.0)

        summary = pm.get_portfolio_summary()
        assert summary["total_positions"] == 3
        assert "StratA" in summary["strategies"]
        assert "StratB" in summary["strategies"]
        assert summary["strategies"]["StratA"]["position_count"] == 2
        assert summary["strategies"]["StratB"]["position_count"] == 1

    def test_realized_pnl_in_summary(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 2, 120.0)
        # Realized P&L: 2 * (120 - 100) = 40

        summary = pm.get_portfolio_summary()
        assert summary["total_realized_pnl"] == pytest.approx(40.0)

    def test_closed_position_cleanup(self):
        """When a position is fully closed with zero realized P&L,
        it should be cleaned up from internal state."""
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        pm.update_position("StratA", "NQ", "Sell", 1, 100.0)
        # Realized P&L is 0, position is flat -> cleaned up
        # But realized_pnl_log still has an entry (of 0.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["size"] == 0


# ===========================================================================
# Market price updates for unrealized P&L
# ===========================================================================

class TestMarketPriceAndUnrealizedPnl:
    """Verify unrealized P&L responds to market price updates."""

    def test_long_position_unrealized_gain(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_market_price("NQ", 110.0)

        pos = pm.get_position("StratA", "NQ")
        # Unrealized: 2 * (110 - 100) = 20
        assert pos["unrealized_pnl"] == pytest.approx(20.0)

    def test_long_position_unrealized_loss(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        pm.update_market_price("NQ", 90.0)

        pos = pm.get_position("StratA", "NQ")
        # Unrealized: 2 * (90 - 100) = -20
        assert pos["unrealized_pnl"] == pytest.approx(-20.0)

    def test_short_position_unrealized_gain(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 3, 200.0)
        pm.update_market_price("NQ", 190.0)

        pos = pm.get_position("StratA", "NQ")
        # Unrealized: 3 * (200 - 190) = 30
        assert pos["unrealized_pnl"] == pytest.approx(30.0)

    def test_short_position_unrealized_loss(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Short", 2, 200.0)
        pm.update_market_price("NQ", 215.0)

        pos = pm.get_position("StratA", "NQ")
        # Unrealized: 2 * (200 - 215) = -30
        assert pos["unrealized_pnl"] == pytest.approx(-30.0)

    def test_no_market_price_returns_zero_unrealized(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 2, 100.0)
        # No market price update
        pos = pm.get_position("StratA", "NQ")
        assert pos["unrealized_pnl"] == 0.0

    def test_flat_position_returns_zero_unrealized(self):
        pm = PositionManager()
        pm.update_market_price("NQ", 110.0)
        pos = pm.get_position("StratA", "NQ")
        assert pos["unrealized_pnl"] == 0.0

    def test_market_price_update_changes_unrealized(self):
        """Multiple market price updates should be reflected."""
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)

        pm.update_market_price("NQ", 105.0)
        assert pm.get_position("StratA", "NQ")["unrealized_pnl"] == pytest.approx(5.0)

        pm.update_market_price("NQ", 95.0)
        assert pm.get_position("StratA", "NQ")["unrealized_pnl"] == pytest.approx(-5.0)

        pm.update_market_price("NQ", 100.0)
        assert pm.get_position("StratA", "NQ")["unrealized_pnl"] == pytest.approx(0.0)


# ===========================================================================
# get_position / get_strategy_positions queries
# ===========================================================================

class TestPositionQueries:
    """Verify position query methods."""

    def test_get_position_nonexistent_returns_zero(self):
        pm = PositionManager()
        pos = pm.get_position("NoStrat", "NoSymbol")
        assert pos["size"] == 0
        assert pos["avg_price"] == 0.0
        assert pos["unrealized_pnl"] == 0.0

    def test_get_strategy_positions_empty(self):
        pm = PositionManager()
        result = pm.get_strategy_positions("StratA")
        assert result == {}

    def test_get_strategy_positions_multiple_symbols(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        pm.update_position("StratA", "ES", "Short", 2, 200.0)
        pm.update_market_price("NQ", 105.0)
        pm.update_market_price("ES", 195.0)

        result = pm.get_strategy_positions("StratA")
        assert "NQ" in result
        assert "ES" in result
        assert result["NQ"]["size"] == 1
        assert result["ES"]["size"] == -2
        assert result["NQ"]["unrealized_pnl"] == pytest.approx(5.0)
        assert result["ES"]["unrealized_pnl"] == pytest.approx(10.0)

    def test_get_strategy_positions_does_not_include_other_strategies(self):
        pm = PositionManager()
        pm.update_position("StratA", "NQ", "Buy", 1, 100.0)
        pm.update_position("StratB", "NQ", "Buy", 1, 100.0)

        result = pm.get_strategy_positions("StratA")
        assert len(result) == 1
