"""
One-shot test: place a market order on the Practice account via ProjectX SDK,
poll for fill, and report the result.

Usage:
    python3.13 scripts/test_trade_flow.py

This buys 1 contract of NQH6, waits for fill, then immediately sells to
close the position. Uses the Practice account only.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_trade_flow")

ACCOUNT_ID = 19092348  # Practice account
SYMBOL = "NQH6"
SIZE = 1
TIMEOUT = 30
POLL_INTERVAL = 0.5


async def main():
    load_dotenv()

    from project_x_py import ProjectX
    from project_x_py.event_bus import EventBus
    from project_x_py.order_manager import OrderManager

    logger.info("=" * 60)
    logger.info("Trade Flow Test — Practice Account")
    logger.info("  Account ID: %d", ACCOUNT_ID)
    logger.info("  Symbol: %s", SYMBOL)
    logger.info("  Size: %d", SIZE)
    logger.info("=" * 60)

    async with ProjectX.from_env() as client:
        # 1. Authenticate
        logger.info("Step 1: Authenticating...")
        await client.authenticate()
        logger.info("  Authenticated as: %s", client.account_info.name)

        # 2. Select account
        logger.info("Step 2: Selecting account %d...", ACCOUNT_ID)
        accounts = await client.list_accounts()
        logger.info("  Available accounts: %s",
                     [(a.id, a.name, a.simulated) for a in accounts])

        match = [a for a in accounts if a.id == ACCOUNT_ID]
        if not match:
            logger.error("Account %d not found!", ACCOUNT_ID)
            return
        client.account_info = match[0]
        logger.info("  Selected: %s (simulated=%s)",
                     match[0].name, match[0].simulated)

        # 3. Resolve contract
        logger.info("Step 3: Resolving contract for %s...", SYMBOL)
        instruments = await client.search_instruments(SYMBOL)
        if not instruments:
            logger.error("No instruments found for %s!", SYMBOL)
            return
        contract_id = instruments[0].id
        logger.info("  Contract: %s (%s)", contract_id,
                     getattr(instruments[0], 'name', ''))
        logger.info("  Tick size: %s, Tick value: %s",
                     getattr(instruments[0], 'tickSize', '?'),
                     getattr(instruments[0], 'tickValue', '?'))

        # 4. Create OrderManager
        logger.info("Step 4: Creating OrderManager...")
        event_bus = EventBus()
        order_mgr = OrderManager(client, event_bus)
        logger.info("  OrderManager created.")

        # Helper: get current position for contract
        async def get_position():
            """Returns (size, avgPrice). size>0 = long, <0 = short."""
            try:
                positions = await client.search_open_positions(
                    account_id=ACCOUNT_ID)
                for p in positions:
                    if p.contractId == contract_id:
                        sz = p.size if p.type == 1 else -p.size
                        return (sz, p.averagePrice)
            except Exception as exc:
                logger.debug("  Position check error: %s", exc)
            return (0, 0.0)

        # Helper: place order and confirm fill via position change
        async def place_and_confirm(side, label):
            """Place market order, confirm fill via position change.

            The SDK's get_order_by_id() only searches open orders.
            Market orders fill instantly and leave the open queue,
            so we confirm fills by checking position changes instead.

            Returns (fill_price, order_id) or (None, order_id) on failure.
            """
            side_str = "BUY" if side == 0 else "SELL"

            # Snapshot position before
            pos_before, price_before = await get_position()
            logger.info("  [%s] Position before: size=%d, avgPrice=%.2f",
                         label, pos_before, price_before)

            # Place order
            start = time.monotonic()
            try:
                resp = await order_mgr.place_market_order(
                    contract_id=contract_id,
                    side=side,
                    size=SIZE,
                    account_id=ACCOUNT_ID,
                )
            except Exception as exc:
                logger.error("  [%s] place_market_order raised: %s",
                             label, exc, exc_info=True)
                return (None, None)

            elapsed = time.monotonic() - start
            logger.info("  [%s] Response in %.2fs: success=%s, orderId=%s",
                         label, elapsed, resp.success, resp.orderId)

            if not resp.success:
                logger.error("  [%s] Order FAILED: %s",
                             label,
                             getattr(resp, 'errorMessage', 'unknown'))
                return (None, resp.orderId)

            oid = resp.orderId

            # Poll for fill confirmation
            while time.monotonic() - start < TIMEOUT:
                # Check if order still in open orders
                try:
                    order = await order_mgr.get_order_by_id(oid)
                except Exception:
                    order = None

                if order is not None:
                    logger.info("  [%s] Order still open: status=%s",
                                label, order.status)
                    if order.is_filled:
                        elapsed = time.monotonic() - start
                        logger.info("  [%s] FILLED (from open orders) "
                                    "@ %.2f in %.2fs",
                                    label, order.filledPrice or 0, elapsed)
                        return (order.filledPrice, oid)
                    if order.is_rejected:
                        logger.error("  [%s] Order REJECTED!", label)
                        return (None, oid)
                    if order.is_cancelled:
                        logger.error("  [%s] Order CANCELLED!", label)
                        return (None, oid)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # Not in open orders — confirm via position change
                pos_after, price_after = await get_position()
                if pos_after != pos_before:
                    # Position changed → order filled
                    if pos_before == 0:
                        fill_price = price_after
                    elif pos_after == 0:
                        fill_price = price_before
                    else:
                        added = abs(pos_after) - abs(pos_before)
                        if added > 0:
                            fill_price = ((price_after * abs(pos_after)
                                          - price_before * abs(pos_before))
                                          / added)
                        else:
                            fill_price = price_after

                    elapsed = time.monotonic() - start
                    logger.info("  [%s] FILLED (confirmed via position) "
                                "@ %.2f in %.2fs",
                                label, fill_price, elapsed)
                    logger.info("  [%s] Position after: size=%d, "
                                "avgPrice=%.2f",
                                label, pos_after, price_after)
                    return (fill_price, oid)

                await asyncio.sleep(POLL_INTERVAL)

            # Timeout
            logger.error("  [%s] TIMEOUT — no fill in %ds!", label, TIMEOUT)
            try:
                await order_mgr.cancel_order(oid, ACCOUNT_ID)
                logger.info("  [%s] Cancel request sent.", label)
            except Exception as exc:
                logger.error("  [%s] Cancel failed: %s", label, exc)
            return (None, oid)

        # 5. Place BUY market order and confirm fill
        logger.info("Step 5: Placing BUY market order — %s x%d...",
                     SYMBOL, SIZE)
        fill_price, buy_order_id = await place_and_confirm(0, "BUY")
        if fill_price is None:
            logger.error("BUY order did not fill. Aborting.")
            return

        # 6. Close position — place SELL market order and confirm fill
        logger.info("Step 6: Closing position — SELL x%d...", SIZE)
        sell_price, sell_order_id = await place_and_confirm(1, "SELL")
        if sell_price is None:
            logger.error("SELL order did not fill!")
            logger.error("WARNING: Position may still be OPEN!")
            return

    logger.info("=" * 60)
    logger.info("TEST COMPLETE")
    logger.info("  BUY filled @ %s (order %s)", fill_price, buy_order_id)
    logger.info("  SELL filled @ %s (order %s)", sell_price, sell_order_id)
    logger.info("  Position closed.")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
