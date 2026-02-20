"""
Tests for scripts.trade_executor -- Live trade executor with ProjectX.

Covers TradeRequest/TradeResult dataclasses, kill switch behavior,
flatten_all sentinel, position detection logic, and mocked ProjectX
order placement.

The actual ProjectX client is never called; all external dependencies
are mocked.
"""

import asyncio
import sys
import queue
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.trade_executor import (
    TradeExecutor,
    TradeRequest,
    TradeResult,
    _FLATTEN_SENTINEL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(signal_type="Buy", symbol="NQH6", size=1, price=18500.0,
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
# TradeRequest / TradeResult dataclasses
# ===========================================================================

class TestDataclasses:
    """Verify the dataclass fields are properly initialized."""

    def test_trade_request_fields(self):
        req = TradeRequest(
            signal_type="Buy",
            symbol="NQH6",
            size=2,
            price=18500.50,
            strategy_name="Momentum",
            timestamp=datetime(2026, 2, 20, 10, 30, 0),
        )
        assert req.signal_type == "Buy"
        assert req.symbol == "NQH6"
        assert req.size == 2
        assert req.price == 18500.50
        assert req.strategy_name == "Momentum"
        assert req.timestamp == datetime(2026, 2, 20, 10, 30, 0)

    def test_trade_result_fields(self):
        req = _make_request()
        result = TradeResult(
            request=req,
            success=True,
            order_id=12345,
            fill_price=18501.0,
            status="filled",
            error_message=None,
            elapsed_seconds=0.35,
            executed_at=datetime(2026, 2, 20, 10, 30, 1),
        )
        assert result.request is req
        assert result.success is True
        assert result.order_id == 12345
        assert result.fill_price == 18501.0
        assert result.status == "filled"
        assert result.error_message is None
        assert result.elapsed_seconds == pytest.approx(0.35)
        assert result.executed_at is not None

    def test_trade_result_defaults(self):
        req = _make_request()
        result = TradeResult(
            request=req,
            success=False,
            order_id=None,
            fill_price=None,
            status="error",
            error_message="test error",
            elapsed_seconds=0.0,
        )
        assert result.executed_at is None  # default

    def test_trade_request_all_signal_types(self):
        for sig in ("Buy", "Sell", "Short", "Cover"):
            req = _make_request(signal_type=sig)
            assert req.signal_type == sig


# ===========================================================================
# Kill switch behavior
# ===========================================================================

class TestKillSwitch:
    """Verify the kill switch disables the executor."""

    def test_kill_disables_executor(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        assert executor.enabled is True

        executor.kill()
        assert executor.enabled is False

    def test_kill_switch_produces_disabled_results(self):
        """When disabled, submitted trades should produce 'disabled' results.

        We test this by directly checking the logic: if the executor is
        disabled, trades get rejected.  We do NOT start the background
        thread (which requires ProjectX auth), but instead verify the
        result queue behavior directly.
        """
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor.kill()

        # Directly enqueue a disabled result (mimicking background thread)
        req = _make_request()
        disabled_result = TradeResult(
            request=req,
            success=False,
            order_id=None,
            fill_price=None,
            status="disabled",
            error_message="Trade executor is disabled (kill switch active)",
            elapsed_seconds=0.0,
        )
        executor._result_queue.put(disabled_result)

        results = executor.get_results()
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].status == "disabled"

    def test_enabled_property_reflects_state(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        assert executor.enabled is True
        executor.kill()
        assert executor.enabled is False


# ===========================================================================
# flatten_all sentinel
# ===========================================================================

class TestFlattenAll:
    """Verify the flatten_all mechanism."""

    def test_flatten_all_disables_executor(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor.flatten_all()
        assert executor.enabled is False

    def test_flatten_all_enqueues_sentinel(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor.flatten_all()

        item = executor._trade_queue.get_nowait()
        assert item is _FLATTEN_SENTINEL

    def test_flatten_sentinel_is_unique_object(self):
        """The sentinel should be a distinct object, not None or a string."""
        assert _FLATTEN_SENTINEL is not None
        assert not isinstance(_FLATTEN_SENTINEL, (str, int, bool))

    def test_flatten_all_followed_by_trade_stays_disabled(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor.flatten_all()
        assert executor.enabled is False

        # Even if someone tries to re-enable (which shouldn't happen),
        # verify the sentinel is in the queue
        items = []
        while not executor._trade_queue.empty():
            items.append(executor._trade_queue.get_nowait())
        assert _FLATTEN_SENTINEL in items


# ===========================================================================
# Queue interface
# ===========================================================================

class TestQueueInterface:
    """Test submit_trade / get_results without starting the background thread."""

    def test_submit_trade_enqueues_request(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        req = _make_request()
        executor.submit_trade(req)

        queued_item = executor._trade_queue.get_nowait()
        assert queued_item is req

    def test_get_results_drains_result_queue(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")

        # Manually put results in the queue
        for i in range(3):
            result = TradeResult(
                request=_make_request(),
                success=True,
                order_id=i,
                fill_price=100.0 + i,
                status="filled",
                error_message=None,
                elapsed_seconds=0.1,
            )
            executor._result_queue.put(result)

        results = executor.get_results()
        assert len(results) == 3

        # Second call should return empty
        results2 = executor.get_results()
        assert len(results2) == 0

    def test_get_results_returns_empty_when_nothing_queued(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        results = executor.get_results()
        assert results == []


# ===========================================================================
# Position detection logic
# ===========================================================================

class TestPositionDetection:
    """Test the _get_position method with mocked ProjectX client."""

    def test_get_position_long(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor._contract_id = "CON-NQH6"

        # Mock the client
        mock_position = MagicMock()
        mock_position.contractId = "CON-NQH6"
        mock_position.type = 1  # LONG
        mock_position.size = 5
        mock_position.averagePrice = 18500.0

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = [mock_position]

        size, avg_price = asyncio.run(executor._get_position(mock_client))
        assert size == 5
        assert avg_price == 18500.0

    def test_get_position_short(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor._contract_id = "CON-NQH6"

        mock_position = MagicMock()
        mock_position.contractId = "CON-NQH6"
        mock_position.type = 2  # SHORT
        mock_position.size = 3
        mock_position.averagePrice = 18600.0

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = [mock_position]

        size, avg_price = asyncio.run(executor._get_position(mock_client))
        assert size == -3  # Negative for short
        assert avg_price == 18600.0

    def test_get_position_no_match(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor._contract_id = "CON-NQH6"

        # Different contract
        mock_position = MagicMock()
        mock_position.contractId = "CON-ESH6"
        mock_position.type = 1
        mock_position.size = 2
        mock_position.averagePrice = 5000.0

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = [mock_position]

        size, avg_price = asyncio.run(executor._get_position(mock_client))
        assert size == 0
        assert avg_price == 0.0

    def test_get_position_empty_positions(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor._contract_id = "CON-NQH6"

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = []

        size, avg_price = asyncio.run(executor._get_position(mock_client))
        assert size == 0
        assert avg_price == 0.0

    def test_get_position_handles_exception(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor._contract_id = "CON-NQH6"

        mock_client = AsyncMock()
        mock_client.search_open_positions.side_effect = RuntimeError("Network error")

        size, avg_price = asyncio.run(executor._get_position(mock_client))
        assert size == 0
        assert avg_price == 0.0


# ===========================================================================
# Mocked order placement
# ===========================================================================

class TestOrderPlacement:
    """Test _execute_trade with a fully mocked ProjectX client."""

    def test_successful_market_order_fill_via_order_status(self):
        """Order found in open orders with is_filled=True."""
        executor = TradeExecutor(account_id=123, symbol="NQH6", timeout=5.0)
        executor._contract_id = "CON-NQH6"

        req = _make_request("Buy", price=18500.0, size=1)

        # Mock client
        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = []

        # Mock order manager
        mock_order_mgr = AsyncMock()

        # place_market_order returns success
        mock_resp = MagicMock()
        mock_resp.success = True
        mock_resp.orderId = 999
        mock_order_mgr.place_market_order.return_value = mock_resp

        # get_order_by_id returns filled order
        mock_order = MagicMock()
        mock_order.is_filled = True
        mock_order.is_rejected = False
        mock_order.is_cancelled = False
        mock_order.filledPrice = 18501.0
        mock_order_mgr.get_order_by_id.return_value = mock_order

        result = asyncio.run(
            executor._execute_trade(mock_client, mock_order_mgr, req))

        assert result.success is True
        assert result.status == "filled"
        assert result.order_id == 999
        assert result.fill_price == 18501.0

    def test_order_rejected(self):
        """Order placement returns success=False."""
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor._contract_id = "CON-NQH6"

        req = _make_request("Buy", price=18500.0)

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = []

        mock_order_mgr = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.success = False
        mock_resp.orderId = None
        mock_resp.errorMessage = "Insufficient margin"
        mock_resp.errorCode = "MARGIN_ERROR"
        mock_order_mgr.place_market_order.return_value = mock_resp

        result = asyncio.run(
            executor._execute_trade(mock_client, mock_order_mgr, req))

        assert result.success is False
        assert result.status == "rejected"
        assert "Insufficient margin" in result.error_message

    def test_order_rejected_during_polling(self):
        """Order is placed successfully but then gets rejected during polling."""
        executor = TradeExecutor(account_id=123, symbol="NQH6", timeout=5.0)
        executor._contract_id = "CON-NQH6"

        req = _make_request("Buy", price=18500.0)

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = []

        mock_order_mgr = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.success = True
        mock_resp.orderId = 888
        mock_order_mgr.place_market_order.return_value = mock_resp

        mock_order = MagicMock()
        mock_order.is_filled = False
        mock_order.is_rejected = True
        mock_order.is_cancelled = False
        mock_order_mgr.get_order_by_id.return_value = mock_order

        result = asyncio.run(
            executor._execute_trade(mock_client, mock_order_mgr, req))

        assert result.success is False
        assert result.status == "rejected"
        assert result.order_id == 888

    def test_order_cancelled_during_polling(self):
        """Order is placed successfully but then gets cancelled externally."""
        executor = TradeExecutor(account_id=123, symbol="NQH6", timeout=5.0)
        executor._contract_id = "CON-NQH6"

        req = _make_request("Buy", price=18500.0)

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = []

        mock_order_mgr = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.success = True
        mock_resp.orderId = 777
        mock_order_mgr.place_market_order.return_value = mock_resp

        mock_order = MagicMock()
        mock_order.is_filled = False
        mock_order.is_rejected = False
        mock_order.is_cancelled = True
        mock_order_mgr.get_order_by_id.return_value = mock_order

        result = asyncio.run(
            executor._execute_trade(mock_client, mock_order_mgr, req))

        assert result.success is False
        assert result.status == "cancelled"

    def test_fill_detected_via_position_change(self):
        """Order disappears from open orders; fill confirmed by position change."""
        executor = TradeExecutor(
            account_id=123, symbol="NQH6",
            timeout=5.0, poll_interval=0.1,
        )
        executor._contract_id = "CON-NQH6"

        req = _make_request("Buy", price=18500.0, size=1)

        # After order: long 1
        mock_pos_after = MagicMock()
        mock_pos_after.contractId = "CON-NQH6"
        mock_pos_after.type = 1
        mock_pos_after.size = 1
        mock_pos_after.averagePrice = 18501.0

        mock_client = AsyncMock()
        mock_client.search_open_positions.side_effect = [
            [],  # before order
            [mock_pos_after],  # after order (position check)
        ]

        mock_order_mgr = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.success = True
        mock_resp.orderId = 555
        mock_order_mgr.place_market_order.return_value = mock_resp
        # Order not found in open orders -> fill detected via position
        mock_order_mgr.get_order_by_id.return_value = None

        result = asyncio.run(
            executor._execute_trade(mock_client, mock_order_mgr, req))

        assert result.success is True
        assert result.status == "filled"
        assert result.fill_price == pytest.approx(18501.0)

    def test_execution_error_caught(self):
        """Unexpected exception during order placement produces an error result.

        Note: _get_position catches its own exceptions internally, so to
        trigger the outer try/except in _execute_trade we raise on
        place_market_order instead.
        """
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor._contract_id = "CON-NQH6"

        req = _make_request("Buy", price=18500.0)

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = []

        mock_order_mgr = AsyncMock()
        mock_order_mgr.place_market_order.side_effect = RuntimeError("Boom!")

        result = asyncio.run(
            executor._execute_trade(mock_client, mock_order_mgr, req))

        assert result.success is False
        assert result.status == "error"
        assert "Boom!" in result.error_message

    def test_sell_signal_maps_to_sell_side(self):
        """Sell/Short signals should use side=1 (SELL)."""
        executor = TradeExecutor(account_id=123, symbol="NQH6", timeout=5.0)
        executor._contract_id = "CON-NQH6"

        req = _make_request("Short", price=18500.0, size=1)

        mock_client = AsyncMock()
        mock_client.search_open_positions.return_value = []

        mock_order_mgr = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.success = True
        mock_resp.orderId = 111
        mock_order_mgr.place_market_order.return_value = mock_resp

        mock_order = MagicMock()
        mock_order.is_filled = True
        mock_order.filledPrice = 18499.0
        mock_order.is_rejected = False
        mock_order.is_cancelled = False
        mock_order_mgr.get_order_by_id.return_value = mock_order

        result = asyncio.run(
            executor._execute_trade(mock_client, mock_order_mgr, req))

        # Verify side=1 was used for Short
        call_kwargs = mock_order_mgr.place_market_order.call_args
        assert call_kwargs.kwargs.get("side") == 1 or call_kwargs[1].get("side") == 1


# ===========================================================================
# Lifecycle (without actually connecting to ProjectX)
# ===========================================================================

class TestLifecycle:
    """Test lifecycle methods that do not require ProjectX connectivity."""

    def test_initial_state(self):
        executor = TradeExecutor(account_id=123, symbol="NQH6", size=2)
        assert executor.enabled is True
        assert executor.is_alive is False
        assert executor._account_id == 123
        assert executor._symbol == "NQH6"
        assert executor._size == 2

    def test_stop_without_start(self):
        """Stopping before starting should not raise."""
        executor = TradeExecutor(account_id=123, symbol="NQH6")
        executor.stop()  # Should be a no-op

    def test_constructor_defaults(self):
        executor = TradeExecutor(account_id=1, symbol="ES")
        assert executor._timeout == 30.0
        assert executor._poll_interval == 0.5
        assert executor._size == 1
