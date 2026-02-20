"""
Trade executor for the live signal alert system.

Places market orders into TopStep via ProjectX SDK when signals fire.
Runs on its own background thread with a dedicated async event loop
and ProjectX client (follows the ProjectXFeed pattern).

Communication with the main thread is queue-based:
- _trade_queue:  main → executor  (TradeRequest objects)
- _result_queue: executor → main  (TradeResult objects)
"""

import asyncio
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Sentinel object for flatten-all requests
_FLATTEN_SENTINEL = object()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TradeRequest:
    """A request to place a trade."""
    signal_type: str      # "Buy", "Sell", "Short", or "Cover"
    symbol: str           # ProjectX symbol (e.g. "NQH6")
    size: int             # Number of contracts
    price: float          # Signal price (for logging, not used as limit)
    strategy_name: str
    timestamp: datetime


@dataclass
class TradeResult:
    """The outcome of a trade request."""
    request: TradeRequest
    success: bool
    order_id: Optional[int]
    fill_price: Optional[float]
    status: str           # "filled", "cancelled", "rejected", "timeout",
                          # "disabled", "error"
    error_message: Optional[str]
    elapsed_seconds: float
    executed_at: Optional[datetime] = None  # actual wall-clock time of fill


# ---------------------------------------------------------------------------
# Trade Executor
# ---------------------------------------------------------------------------

class TradeExecutor:
    """Background-thread trade executor using ProjectX OrderManager.

    Parameters
    ----------
    account_id : int
        ProjectX account ID for order placement.
    symbol : str
        ProjectX instrument symbol (e.g. "NQH6") for contract resolution.
    size : int
        Default number of contracts per trade.
    timeout : float
        Seconds to wait for a fill before cancelling the order.
    poll_interval : float
        Seconds between fill-status polling calls.
    """

    def __init__(
        self,
        account_id: int,
        symbol: str,
        size: int = 1,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ):
        self._account_id = account_id
        self._symbol = symbol
        self._size = size
        self._timeout = timeout
        self._poll_interval = poll_interval

        # Queues for cross-thread communication
        self._trade_queue: queue.Queue[TradeRequest] = queue.Queue()
        self._result_queue: queue.Queue[TradeResult] = queue.Queue()

        # Thread management
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Kill switch — can be toggled from any thread
        self._enabled = True

        # Resolved at startup
        self._contract_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API (called from main thread)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background executor thread."""
        load_dotenv()
        self._stop_event.clear()
        self._enabled = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="TradeExecutor")
        self._thread.start()
        logger.info("TradeExecutor started (account=%d, symbol=%s, "
                     "size=%d, timeout=%.1fs).",
                     self._account_id, self._symbol,
                     self._size, self._timeout)

    def stop(self) -> None:
        """Signal the executor to stop and wait for the thread to exit."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("TradeExecutor stopped.")

    def submit_trade(self, request: TradeRequest) -> None:
        """Submit a trade request (non-blocking)."""
        self._trade_queue.put(request)

    def get_results(self) -> list[TradeResult]:
        """Drain and return all available trade results."""
        results = []
        while True:
            try:
                results.append(self._result_queue.get_nowait())
            except queue.Empty:
                break
        return results

    def kill(self) -> None:
        """Emergency kill switch — disable all future trades."""
        self._enabled = False
        logger.warning("TradeExecutor KILL SWITCH activated — "
                       "no further trades will be placed.")

    def flatten_all(self) -> None:
        """Emergency flatten: close all open positions and disable trading.

        Submits a special flatten request that the background thread handles.
        Also kills the executor to prevent any new trades.
        """
        self._enabled = False
        self._trade_queue.put(_FLATTEN_SENTINEL)
        logger.warning("FLATTEN ALL requested — closing all positions.")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Thread target: create event loop and run async processing."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:
            logger.exception("TradeExecutor thread crashed: %s", exc)
        finally:
            loop.close()

    async def _async_main(self) -> None:
        """Async main loop: authenticate, resolve contract, process orders."""
        from project_x_py import ProjectX
        from project_x_py.event_bus import EventBus
        from project_x_py.order_manager import OrderManager

        try:
            async with ProjectX.from_env() as client:
                await client.authenticate()

                # Select account (same pattern as ProjectXFeed)
                accounts = await client.list_accounts()
                acct_match = [a for a in accounts
                              if a.id == self._account_id]
                if acct_match:
                    client.account_info = acct_match[0]
                    logger.info("TradeExecutor using account: %s (ID %d)",
                                acct_match[0].name, self._account_id)
                else:
                    logger.error("Account ID %d not found. Available: %s",
                                 self._account_id,
                                 [a.id for a in accounts])
                    return

                # Resolve contract_id from symbol
                instruments = await client.search_instruments(self._symbol)
                if not instruments:
                    logger.error("No instruments found for symbol: %s",
                                 self._symbol)
                    return
                self._contract_id = instruments[0].id
                logger.info("Resolved contract: %s → %s",
                            self._symbol, self._contract_id)

                # Create OrderManager
                event_bus = EventBus()
                order_mgr = OrderManager(client, event_bus)

                # Process trade requests until stopped
                while not self._stop_event.is_set():
                    try:
                        req = self._trade_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue

                    # Handle flatten sentinel
                    if req is _FLATTEN_SENTINEL:
                        await self._flatten_positions(client, order_mgr)
                        continue

                    if not self._enabled:
                        self._result_queue.put(TradeResult(
                            request=req,
                            success=False,
                            order_id=None,
                            fill_price=None,
                            status="disabled",
                            error_message="Trade executor is disabled "
                                          "(kill switch active)",
                            elapsed_seconds=0.0,
                        ))
                        continue

                    result = await self._execute_trade(
                        client, order_mgr, req)
                    self._result_queue.put(result)

        except Exception as exc:
            logger.exception("TradeExecutor async main error: %s", exc)

    async def _get_position(self, client) -> tuple[int, float]:
        """Get current position for the target contract.

        Returns (size, avgPrice) — size is signed: positive=long, negative=short.
        Returns (0, 0.0) if no position.
        """
        try:
            positions = await client.search_open_positions(
                account_id=self._account_id)
            for p in positions:
                if p.contractId == self._contract_id:
                    # type: 1=LONG, 2=SHORT
                    size = p.size if p.type == 1 else -p.size
                    return (size, p.averagePrice)
        except Exception as exc:
            logger.debug("Position check failed: %s", exc)
        return (0, 0.0)

    async def _execute_trade(
        self, client, order_mgr, req: TradeRequest
    ) -> TradeResult:
        """Place a market order, confirm fill via position change.

        Fill detection strategy:
        1. Snapshot position BEFORE placing the order.
        2. Place the market order.
        3. Poll: check if order is still in open orders (get_order_by_id).
           - If found and still open → waiting for fill, keep polling.
           - If found and filled/rejected/cancelled → return immediately.
           - If NOT found → order left the open queue (market orders fill
             instantly on liquid instruments and disappear from open orders).
        4. When order is not in open orders, confirm via position change.
           - Compare position size before vs after to verify the fill.
           - Infer fill price from position's averagePrice.
        """
        # Map signal type to order side
        # Buy/Cover → BUY (side=0), Sell/Short → SELL (side=1)
        side = 0 if req.signal_type in ("Buy", "Cover") else 1
        side_str = "BUY" if side == 0 else "SELL"

        start_time = time.monotonic()

        logger.info("Placing %s market order: %s %s x%d (signal=%s @ %.2f)",
                    side_str, self._symbol, self._contract_id,
                    req.size, req.signal_type, req.price)

        try:
            # Snapshot position before order
            pos_size_before, pos_price_before = await self._get_position(
                client)
            logger.debug("Position before: size=%d, avgPrice=%.2f",
                         pos_size_before, pos_price_before)

            # Place market order
            resp = await order_mgr.place_market_order(
                contract_id=self._contract_id,
                side=side,
                size=req.size,
                account_id=self._account_id,
            )

            elapsed = time.monotonic() - start_time

            if not resp.success:
                logger.error("Order rejected: %s (code=%s)",
                             resp.errorMessage, resp.errorCode)
                return TradeResult(
                    request=req,
                    success=False,
                    order_id=getattr(resp, 'orderId', None),
                    fill_price=None,
                    status="rejected",
                    error_message=(resp.errorMessage
                                   or f"Error code: {resp.errorCode}"),
                    elapsed_seconds=elapsed,
                )

            order_id = resp.orderId
            logger.info("Order placed: ID=%d, polling for fill...", order_id)

            # Poll for fill
            while time.monotonic() - start_time < self._timeout:
                # Check if order is still in open orders
                order = await order_mgr.get_order_by_id(order_id)

                if order is not None:
                    # Order still in the open-orders list
                    if order.is_filled:
                        elapsed = time.monotonic() - start_time
                        fill_price = order.filledPrice
                        logger.info(
                            "ORDER FILLED: %s %s x%d @ %.2f (%.1fs)",
                            side_str, self._symbol, req.size,
                            fill_price or 0.0, elapsed)
                        return TradeResult(
                            request=req,
                            success=True,
                            order_id=order_id,
                            fill_price=fill_price,
                            status="filled",
                            error_message=None,
                            elapsed_seconds=elapsed,
                            executed_at=datetime.now(),
                        )

                    if order.is_rejected:
                        elapsed = time.monotonic() - start_time
                        logger.error("Order %d was rejected.", order_id)
                        return TradeResult(
                            request=req,
                            success=False,
                            order_id=order_id,
                            fill_price=None,
                            status="rejected",
                            error_message="Order rejected by broker",
                            elapsed_seconds=elapsed,
                        )

                    if order.is_cancelled:
                        elapsed = time.monotonic() - start_time
                        logger.warning("Order %d was cancelled externally.",
                                       order_id)
                        return TradeResult(
                            request=req,
                            success=False,
                            order_id=order_id,
                            fill_price=None,
                            status="cancelled",
                            error_message="Order cancelled externally",
                            elapsed_seconds=elapsed,
                        )

                    # Still open/pending — wait and retry
                    await asyncio.sleep(self._poll_interval)
                    continue

                # Order NOT in open orders — for a successfully-placed
                # market order on a liquid instrument, this means it filled.
                # Confirm by checking position change.
                pos_size_after, pos_price_after = await self._get_position(
                    client)

                position_changed = (pos_size_after != pos_size_before)
                if position_changed:
                    # Infer fill price from position averagePrice
                    if pos_size_before == 0:
                        # No prior position — fill price is the new avgPrice
                        fill_price = pos_price_after
                    elif pos_size_after == 0:
                        # Position fully closed — use the before price
                        # as approximate (exact fill unavailable)
                        fill_price = pos_price_before
                    else:
                        # Position changed size — compute from weighted avg
                        added = abs(pos_size_after) - abs(pos_size_before)
                        if added > 0:
                            total_cost = (pos_price_after
                                          * abs(pos_size_after))
                            prior_cost = (pos_price_before
                                          * abs(pos_size_before))
                            fill_price = ((total_cost - prior_cost)
                                          / added)
                        else:
                            fill_price = pos_price_after

                    elapsed = time.monotonic() - start_time
                    logger.info(
                        "ORDER FILLED (confirmed via position): "
                        "%s %s x%d @ %.2f (%.1fs)",
                        side_str, self._symbol, req.size,
                        fill_price or 0.0, elapsed)
                    return TradeResult(
                        request=req,
                        success=True,
                        order_id=order_id,
                        fill_price=fill_price,
                        status="filled",
                        error_message=None,
                        elapsed_seconds=elapsed,
                        executed_at=datetime.now(),
                    )

                # Position hasn't changed yet — keep polling
                await asyncio.sleep(self._poll_interval)

            # Timeout — attempt to cancel
            elapsed = time.monotonic() - start_time
            logger.warning("Order %d timed out after %.1fs, cancelling...",
                           order_id, elapsed)
            try:
                await order_mgr.cancel_order(order_id, self._account_id)
                logger.info("Order %d cancel request sent.", order_id)
            except Exception as cancel_exc:
                logger.error("Failed to cancel order %d: %s",
                             order_id, cancel_exc)

            return TradeResult(
                request=req,
                success=False,
                order_id=order_id,
                fill_price=None,
                status="timeout",
                error_message=f"No fill within {self._timeout:.0f}s",
                elapsed_seconds=elapsed,
            )

        except Exception as exc:
            elapsed = time.monotonic() - start_time
            logger.exception("Trade execution error: %s", exc)
            return TradeResult(
                request=req,
                success=False,
                order_id=None,
                fill_price=None,
                status="error",
                error_message=str(exc),
                elapsed_seconds=elapsed,
            )

    async def _flatten_positions(self, client, order_mgr) -> None:
        """Close all open positions for the configured account.

        Queries all open positions, then places market orders to
        close each one.  Confirms positions are flat.
        """
        logger.warning("FLATTEN: Querying all open positions...")
        try:
            positions = await client.search_open_positions(
                account_id=self._account_id)

            if not positions:
                logger.info("FLATTEN: No open positions found.")
                return

            for pos in positions:
                # type: 1=LONG → sell, 2=SHORT → buy to cover
                side = 1 if pos.type == 1 else 0
                side_str = "SELL" if side == 1 else "BUY"
                logger.warning(
                    "FLATTEN: Closing %s position: %s x%d @ avg %.2f",
                    "LONG" if pos.type == 1 else "SHORT",
                    pos.contractId, pos.size, pos.averagePrice)

                try:
                    resp = await order_mgr.place_market_order(
                        contract_id=pos.contractId,
                        side=side,
                        size=pos.size,
                        account_id=self._account_id,
                    )
                    if resp.success:
                        logger.info("FLATTEN: Close order placed (ID=%d)",
                                    resp.orderId)
                    else:
                        logger.error("FLATTEN: Close order rejected: %s",
                                     resp.errorMessage)
                except Exception as exc:
                    logger.exception("FLATTEN: Error closing position: %s", exc)

            # Verify positions are flat
            await asyncio.sleep(2.0)
            remaining = await client.search_open_positions(
                account_id=self._account_id)
            if remaining:
                logger.error("FLATTEN: %d positions still open after flatten!",
                             len(remaining))
            else:
                logger.info("FLATTEN: All positions confirmed closed.")

        except Exception as exc:
            logger.exception("FLATTEN: Error during position flattening: %s", exc)
