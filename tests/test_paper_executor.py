"""
Tests for scripts.paper_executor -- Paper trading executor.

Covers simulated fills with slippage, position tracking (long/short),
P&L calculation, the kill switch, and the queue interface.
"""

import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.trade_executor import TradeRequest, TradeResult
from scripts.paper_executor import PaperTradeExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(signal_type="Buy", symbol="NQ", size=1, price=18500.0,
                  strategy_name="TestStrat"):
    return TradeRequest(
        signal_type=signal_type,
        symbol=symbol,
        size=size,
        price=price,
        strategy_name=strategy_name,
        timestamp=datetime.now(),
    )


# ===========================================================================
# Simulated fills
# ===========================================================================

class TestSimulatedFills:
    """Verify that fills are produced with correct slippage behavior."""

    def test_buy_fill_price_includes_adverse_slippage(self):
        """For a Buy, slippage makes the fill >= signal price."""
        executor = PaperTradeExecutor(
            symbol="NQ",
            slippage_ticks=2.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        # Call _simulate_fill directly to test without threading
        req = _make_request("Buy", price=18500.0)
        result = executor._simulate_fill(req)

        assert result.success is True
        assert result.fill_price >= req.price  # adverse = higher for buy
        # Max slippage = 2 ticks * 0.25 = 0.50
        assert result.fill_price <= req.price + 0.50
        assert result.status == "filled"

    def test_sell_fill_price_includes_adverse_slippage(self):
        """For a Sell/Short, slippage makes the fill <= signal price."""
        executor = PaperTradeExecutor(
            symbol="NQ",
            slippage_ticks=2.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        req = _make_request("Sell", price=18500.0)
        result = executor._simulate_fill(req)

        assert result.success is True
        assert result.fill_price <= req.price
        assert result.fill_price >= req.price - 0.50

    def test_fill_price_rounded_to_tick_size(self):
        executor = PaperTradeExecutor(
            symbol="NQ",
            slippage_ticks=3.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        for _ in range(50):
            req = _make_request("Buy", price=18500.00)
            result = executor._simulate_fill(req)
            # Price should be a multiple of 0.25
            remainder = result.fill_price % 0.25
            assert remainder == pytest.approx(0.0, abs=1e-9)

    def test_zero_slippage(self):
        executor = PaperTradeExecutor(
            symbol="NQ",
            slippage_ticks=0.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        req = _make_request("Buy", price=18500.0)
        result = executor._simulate_fill(req)
        assert result.fill_price == pytest.approx(18500.0)

    def test_cover_treated_as_buy_direction(self):
        """Cover should apply the same slippage direction as Buy."""
        executor = PaperTradeExecutor(
            symbol="NQ",
            slippage_ticks=2.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        req = _make_request("Cover", price=18500.0)
        result = executor._simulate_fill(req)
        assert result.fill_price >= req.price

    def test_short_treated_as_sell_direction(self):
        """Short should apply the same slippage direction as Sell."""
        executor = PaperTradeExecutor(
            symbol="NQ",
            slippage_ticks=2.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        req = _make_request("Short", price=18500.0)
        result = executor._simulate_fill(req)
        assert result.fill_price <= req.price

    def test_fill_result_has_executed_at(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        req = _make_request("Buy", price=100.0)
        result = executor._simulate_fill(req)
        assert result.executed_at is not None
        assert isinstance(result.executed_at, datetime)


# ===========================================================================
# Position tracking
# ===========================================================================

class TestPositionTracking:
    """Verify internal position tracking via _update_position."""

    def test_open_long_position(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        pnl = executor._update_position("Buy", 2, 100.0)
        assert pnl is None  # No P&L on opening
        assert executor._position_size == 2
        assert executor._avg_entry_price == 100.0

    def test_close_long_position(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor._update_position("Buy", 2, 100.0)  # Open long
        pnl = executor._update_position("Sell", 2, 110.0)  # Close
        assert pnl == pytest.approx(20.0)  # 2 * (110 - 100)
        assert executor._position_size == 0
        assert executor._avg_entry_price == 0.0

    def test_partial_close_long(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor._update_position("Buy", 4, 100.0)
        pnl = executor._update_position("Sell", 2, 105.0)
        assert pnl == pytest.approx(10.0)  # 2 * (105 - 100)
        assert executor._position_size == 2
        assert executor._avg_entry_price == 100.0  # unchanged

    def test_open_short_position(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        pnl = executor._update_position("Short", 3, 200.0)
        assert pnl is None
        assert executor._position_size == -3
        assert executor._avg_entry_price == 200.0

    def test_close_short_position(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor._update_position("Short", 3, 200.0)
        pnl = executor._update_position("Cover", 3, 190.0)
        assert pnl == pytest.approx(30.0)  # 3 * (200 - 190)
        assert executor._position_size == 0
        assert executor._avg_entry_price == 0.0

    def test_close_short_at_loss(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor._update_position("Short", 2, 200.0)
        pnl = executor._update_position("Cover", 2, 210.0)
        assert pnl == pytest.approx(-20.0)  # 2 * (200 - 210) = -20

    def test_add_to_long_updates_avg_price(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor._update_position("Buy", 2, 100.0)
        executor._update_position("Buy", 2, 110.0)
        # Weighted avg: (100*2 + 110*2) / 4 = 105
        assert executor._position_size == 4
        assert executor._avg_entry_price == pytest.approx(105.0)

    def test_add_to_short_updates_avg_price(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor._update_position("Short", 2, 200.0)
        executor._update_position("Short", 2, 210.0)
        # Weighted avg: (200*2 + 210*2) / 4 = 205
        assert executor._position_size == -4
        assert executor._avg_entry_price == pytest.approx(205.0)

    def test_flip_from_long_to_short(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor._update_position("Buy", 2, 100.0)
        pnl = executor._update_position("Sell", 5, 110.0)
        # Realized P&L on 2 closed: 2 * (110 - 100) = 20
        assert pnl == pytest.approx(20.0)
        assert executor._position_size == -3
        assert executor._avg_entry_price == 110.0

    def test_flip_from_short_to_long(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor._update_position("Short", 2, 200.0)
        pnl = executor._update_position("Buy", 5, 190.0)
        # Realized P&L on 2 closed: 2 * (200 - 190) = 20
        assert pnl == pytest.approx(20.0)
        assert executor._position_size == 3
        assert executor._avg_entry_price == 190.0


# ===========================================================================
# P&L accumulation
# ===========================================================================

class TestPnlAccumulation:
    """Verify total P&L accumulates across multiple trades."""

    def test_total_pnl_accumulates(self):
        executor = PaperTradeExecutor(
            slippage_ticks=0.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        # Open and close a profitable trade
        req_buy = _make_request("Buy", price=100.0, size=1)
        executor._simulate_fill(req_buy)

        req_sell = _make_request("Sell", price=110.0, size=1)
        executor._simulate_fill(req_sell)

        stats = executor.get_stats()
        assert stats["total_pnl"] == pytest.approx(10.0)
        assert stats["total_trades"] == 2
        assert stats["mode"] == "paper"


# ===========================================================================
# Kill switch
# ===========================================================================

class TestKillSwitch:
    """Verify the kill switch disables all future trades."""

    def test_kill_disables_executor(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        assert executor.enabled is True

        executor.kill()
        assert executor.enabled is False

    def test_disabled_executor_returns_failure_results(self):
        """When killed, background thread produces disabled TradeResults."""
        executor = PaperTradeExecutor(fill_delay=0.0, slippage_ticks=0.0)
        executor.start()
        executor.kill()

        req = _make_request("Buy", price=100.0)
        executor.submit_trade(req)

        # Give the background thread time to process
        time.sleep(0.5)

        results = executor.get_results()
        executor.stop()

        assert len(results) >= 1
        result = results[0]
        assert result.success is False
        assert result.status == "disabled"
        assert "kill switch" in result.error_message.lower()


# ===========================================================================
# Queue interface (submit_trade, get_results)
# ===========================================================================

class TestQueueInterface:
    """Test the submit_trade / get_results queue-based API."""

    def test_submit_and_retrieve_single_trade(self):
        executor = PaperTradeExecutor(
            slippage_ticks=0.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        executor.start()

        req = _make_request("Buy", price=18500.0)
        executor.submit_trade(req)

        # Wait for processing
        time.sleep(0.5)
        results = executor.get_results()
        executor.stop()

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].request is req

    def test_submit_multiple_trades(self):
        executor = PaperTradeExecutor(
            slippage_ticks=0.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        executor.start()

        for i in range(5):
            executor.submit_trade(
                _make_request("Buy", price=100.0 + i, size=1)
            )

        time.sleep(2.0)
        results = executor.get_results()
        executor.stop()

        assert len(results) == 5
        assert all(r.success for r in results)

    def test_get_results_returns_empty_when_no_trades(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        results = executor.get_results()
        assert results == []

    def test_get_results_drains_queue(self):
        executor = PaperTradeExecutor(
            slippage_ticks=0.0,
            tick_size=0.25,
            fill_delay=0.0,
        )
        executor.start()

        executor.submit_trade(_make_request("Buy", price=100.0))
        time.sleep(0.5)

        first_batch = executor.get_results()
        second_batch = executor.get_results()
        executor.stop()

        assert len(first_batch) == 1
        assert len(second_batch) == 0


# ===========================================================================
# Start / stop lifecycle
# ===========================================================================

class TestLifecycle:
    """Test start/stop behavior."""

    def test_is_alive_after_start(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor.start()
        assert executor.is_alive is True
        executor.stop()

    def test_not_alive_after_stop(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        executor.start()
        executor.stop()
        # Thread should have joined
        assert executor.is_alive is False

    def test_not_alive_before_start(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        assert executor.is_alive is False


# ===========================================================================
# get_stats
# ===========================================================================

class TestGetStats:
    """Verify get_stats returns correct structure."""

    def test_initial_stats(self):
        executor = PaperTradeExecutor(fill_delay=0.0)
        stats = executor.get_stats()
        assert stats["total_trades"] == 0
        assert stats["total_pnl"] == 0.0
        assert stats["position_size"] == 0
        assert stats["avg_entry_price"] == 0.0
        assert stats["mode"] == "paper"
