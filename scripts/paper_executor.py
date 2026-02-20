"""
Paper trading executor for strategy validation without real capital.

Implements the same TradeRequest/TradeResult queue interface as
TradeExecutor, but simulates fills locally instead of placing real
orders through ProjectX.

Usage:
    executor = PaperTradeExecutor(symbol="NQH6", slippage_ticks=1)
    executor.start()
    executor.submit_trade(TradeRequest(...))
    results = executor.get_results()
    executor.stop()
"""

import logging
import queue
import random
import threading
import time
from datetime import datetime
from typing import Optional

from scripts.trade_executor import TradeRequest, TradeResult

logger = logging.getLogger(__name__)


class PaperTradeExecutor:
    """Simulated trade executor for paper trading.

    Implements the same interface as TradeExecutor so it can be used
    as a drop-in replacement.  Fills are simulated at the signal price
    plus configurable slippage.

    Parameters
    ----------
    symbol : str
        Trading symbol (for logging).
    size : int
        Default contracts per trade.
    slippage_ticks : float
        Maximum adverse slippage in ticks.  Actual slippage is random
        between 0 and this value.
    tick_size : float
        Minimum price increment (e.g. 0.25 for NQ futures).
    fill_delay : float
        Simulated fill delay in seconds (adds realism).
    """

    def __init__(
        self,
        symbol: str = "",
        size: int = 1,
        slippage_ticks: float = 1.0,
        tick_size: float = 0.25,
        fill_delay: float = 0.1,
    ):
        self._symbol = symbol
        self._size = size
        self._slippage_ticks = slippage_ticks
        self._tick_size = tick_size
        self._fill_delay = fill_delay

        # Same queue interface as TradeExecutor
        self._trade_queue: queue.Queue[TradeRequest] = queue.Queue()
        self._result_queue: queue.Queue[TradeResult] = queue.Queue()

        # Thread management
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._enabled = True

        # Paper trading stats
        self._total_trades = 0
        self._total_pnl = 0.0
        self._position_size = 0  # Net position (positive=long, negative=short)
        self._avg_entry_price = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API (same interface as TradeExecutor)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the paper executor background thread."""
        self._stop_event.clear()
        self._enabled = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="PaperTradeExecutor")
        self._thread.start()
        logger.info("PaperTradeExecutor started (symbol=%s, slippage=%.2f ticks, "
                     "tick_size=%.4f)", self._symbol, self._slippage_ticks,
                     self._tick_size)

    def stop(self) -> None:
        """Stop the paper executor."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("PaperTradeExecutor stopped. Total trades: %d, P&L: $%.2f",
                     self._total_trades, self._total_pnl)

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
        logger.warning("PaperTradeExecutor KILL SWITCH activated.")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Paper trading stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Get paper trading statistics."""
        with self._lock:
            return {
                "total_trades": self._total_trades,
                "total_pnl": round(self._total_pnl, 2),
                "position_size": self._position_size,
                "avg_entry_price": round(self._avg_entry_price, 4),
                "mode": "paper",
            }

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Process trade requests with simulated fills."""
        while not self._stop_event.is_set():
            try:
                req = self._trade_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if not self._enabled:
                self._result_queue.put(TradeResult(
                    request=req,
                    success=False,
                    order_id=None,
                    fill_price=None,
                    status="disabled",
                    error_message="Paper executor is disabled (kill switch active)",
                    elapsed_seconds=0.0,
                ))
                continue

            result = self._simulate_fill(req)
            self._result_queue.put(result)

    def _simulate_fill(self, req: TradeRequest) -> TradeResult:
        """Simulate a market order fill with slippage."""
        start_time = time.monotonic()

        # Simulate fill delay
        if self._fill_delay > 0:
            time.sleep(self._fill_delay)

        # Calculate slippage (adverse direction)
        slippage = random.uniform(0, self._slippage_ticks) * self._tick_size
        is_buy = req.signal_type in ("Buy", "Cover")
        fill_price = req.price + slippage if is_buy else req.price - slippage

        # Round to tick size
        fill_price = round(fill_price / self._tick_size) * self._tick_size

        elapsed = time.monotonic() - start_time

        # Update position tracking
        with self._lock:
            self._total_trades += 1
            pnl = self._update_position(req.signal_type, req.size, fill_price)
            if pnl is not None:
                self._total_pnl += pnl

        logger.info(
            "[PAPER] %s %s x%d @ %.2f (signal=%.2f, slippage=%.4f) %s",
            "BUY" if is_buy else "SELL",
            req.symbol or self._symbol,
            req.size,
            fill_price,
            req.price,
            abs(fill_price - req.price),
            f"P&L: ${pnl:.2f}" if pnl else "",
        )

        return TradeResult(
            request=req,
            success=True,
            order_id=self._total_trades,  # Use counter as pseudo order ID
            fill_price=fill_price,
            status="filled",
            error_message=None,
            elapsed_seconds=elapsed,
            executed_at=datetime.now(),
        )

    def _update_position(
        self, signal_type: str, size: int, fill_price: float
    ) -> Optional[float]:
        """Update position tracking and compute realized P&L.

        Returns realized P&L if a position is reduced/closed, else None.
        Must hold self._lock.
        """
        pnl = None

        if signal_type in ("Buy", "Cover"):
            if self._position_size < 0:
                # Closing/reducing a short position
                close_size = min(size, abs(self._position_size))
                pnl = close_size * (self._avg_entry_price - fill_price)
                self._position_size += size
                if self._position_size > 0:
                    # Flipped to long
                    self._avg_entry_price = fill_price
                elif self._position_size == 0:
                    self._avg_entry_price = 0.0
            else:
                # Adding to or opening a long position
                if self._position_size == 0:
                    self._avg_entry_price = fill_price
                else:
                    # Weighted average entry price
                    total_cost = (self._avg_entry_price * self._position_size
                                  + fill_price * size)
                    self._avg_entry_price = total_cost / (self._position_size + size)
                self._position_size += size

        elif signal_type in ("Sell", "Short"):
            if self._position_size > 0:
                # Closing/reducing a long position
                close_size = min(size, self._position_size)
                pnl = close_size * (fill_price - self._avg_entry_price)
                self._position_size -= size
                if self._position_size < 0:
                    # Flipped to short
                    self._avg_entry_price = fill_price
                elif self._position_size == 0:
                    self._avg_entry_price = 0.0
            else:
                # Adding to or opening a short position
                if self._position_size == 0:
                    self._avg_entry_price = fill_price
                else:
                    total_cost = (self._avg_entry_price * abs(self._position_size)
                                  + fill_price * size)
                    self._avg_entry_price = total_cost / (abs(self._position_size) + size)
                self._position_size -= size

        return pnl
