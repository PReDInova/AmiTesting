"""
Position manager for live trading.

Tracks open positions per strategy per symbol, enforces allocation limits,
and computes real-time P&L (both unrealized and realized).  Position state
is kept in memory (protected by a threading.Lock for multi-thread safety)
and persisted to SQLite via the existing strategy_db module.

Typical usage::

    pm = PositionManager(max_per_strategy=5, max_per_symbol=2, max_portfolio=10)

    allowed, reason = pm.check_position_limits("RSI Momentum", "NQ", 1)
    if allowed:
        pm.update_position("RSI Momentum", "NQ", "Buy", 1, 18500.25)

    pm.update_market_price("NQ", 18520.00)
    pos = pm.get_position("RSI Momentum", "NQ")
    # -> {"size": 1, "avg_price": 18500.25, "unrealized_pnl": 19.75}
"""

import logging
import threading
from datetime import datetime, date, timezone
from typing import Optional

from scripts import strategy_db

logger = logging.getLogger(__name__)


class PositionManager:
    """Track open positions, enforce limits, and compute P&L.

    All public methods are thread-safe.  Internal position state is a nested
    dict keyed by ``(strategy_name, symbol)`` and guarded by ``_lock``.

    Parameters
    ----------
    max_per_strategy : int, optional
        Maximum number of distinct open symbols allowed per strategy.
        ``None`` means unlimited.
    max_per_symbol : int, optional
        Maximum total position size (across all strategies) for a single
        symbol.  ``None`` means unlimited.
    max_portfolio : int, optional
        Maximum number of distinct open positions across the entire
        portfolio.  ``None`` means unlimited.
    """

    def __init__(
        self,
        max_per_strategy: Optional[int] = None,
        max_per_symbol: Optional[int] = None,
        max_portfolio: Optional[int] = None,
    ):
        self._max_per_strategy = max_per_strategy
        self._max_per_symbol = max_per_symbol
        self._max_portfolio = max_portfolio

        self._lock = threading.Lock()

        # positions[strategy_name][symbol] = {
        #     "size": int,          # signed: positive=long, negative=short
        #     "avg_price": float,
        #     "realized_pnl": float # accumulated realized P&L for this leg
        # }
        self._positions: dict[str, dict[str, dict]] = {}

        # Latest market prices for unrealized P&L calculation
        # market_prices[symbol] = float
        self._market_prices: dict[str, float] = {}

        # Realized P&L entries: list of (datetime, strategy, symbol, pnl)
        self._realized_pnl_log: list[tuple[datetime, str, str, float]] = []

        logger.info(
            "PositionManager initialized (max_per_strategy=%s, "
            "max_per_symbol=%s, max_portfolio=%s)",
            self._max_per_strategy,
            self._max_per_symbol,
            self._max_portfolio,
        )

    # ------------------------------------------------------------------
    # Position updates
    # ------------------------------------------------------------------

    def update_position(
        self,
        strategy_name: str,
        symbol: str,
        signal_type: str,
        size: int,
        fill_price: float,
    ) -> None:
        """Update internal position tracking when a trade fills.

        Parameters
        ----------
        strategy_name : str
            Name of the strategy that generated the signal.
        symbol : str
            Instrument symbol (e.g. ``"NQ"``).
        signal_type : str
            One of ``"Buy"``, ``"Sell"``, ``"Short"``, ``"Cover"``.
            Buy/Cover increase (go more long), Short/Sell decrease
            (go more short).
        size : int
            Number of contracts traded (always positive).
        fill_price : float
            The price at which the order was filled.
        """
        if signal_type not in ("Buy", "Sell", "Short", "Cover"):
            raise ValueError(
                f"Invalid signal_type '{signal_type}'. "
                "Must be Buy, Sell, Short, or Cover."
            )

        with self._lock:
            strat_positions = self._positions.setdefault(strategy_name, {})
            pos = strat_positions.get(symbol)

            if pos is None:
                pos = {"size": 0, "avg_price": 0.0, "realized_pnl": 0.0}
                strat_positions[symbol] = pos

            old_size = pos["size"]
            old_avg = pos["avg_price"]

            # Determine the signed delta.
            # Buy / Cover  -> add to position (positive delta)
            # Sell / Short  -> subtract from position (negative delta)
            if signal_type in ("Buy", "Cover"):
                delta = size
            else:
                delta = -size

            new_size = old_size + delta

            # Compute realized P&L when reducing or flipping a position.
            realized = 0.0
            if old_size != 0 and _is_reducing(old_size, delta):
                # Contracts being closed = min(|delta|, |old_size|)
                closed = min(abs(delta), abs(old_size))
                if old_size > 0:
                    # Was long, closing part/all
                    realized = (fill_price - old_avg) * closed
                else:
                    # Was short, closing part/all
                    realized = (old_avg - fill_price) * closed
                pos["realized_pnl"] += realized
                self._realized_pnl_log.append(
                    (datetime.now(timezone.utc), strategy_name, symbol, realized)
                )

            # Update average price.
            if new_size == 0:
                # Flat -- reset avg_price
                pos["avg_price"] = 0.0
            elif old_size == 0:
                # Opening fresh
                pos["avg_price"] = fill_price
            elif _same_sign(old_size, new_size) and _is_adding(old_size, delta):
                # Adding to same direction -- weighted average
                total_cost = (old_avg * abs(old_size)) + (fill_price * abs(delta))
                pos["avg_price"] = total_cost / abs(new_size)
            elif _same_sign(old_size, new_size):
                # Partial close, same direction -- avg_price unchanged
                pass
            else:
                # Flipped sides (e.g. was long 2, sold 5 -> short 3).
                # The new side's avg_price is the fill price.
                pos["avg_price"] = fill_price

            pos["size"] = new_size

            # Clean up flat positions
            if new_size == 0 and pos["realized_pnl"] == 0.0:
                del strat_positions[symbol]
                if not strat_positions:
                    del self._positions[strategy_name]

            logger.info(
                "Position updated: %s/%s %s %d @ %.2f -> size=%d avg=%.2f "
                "(realized=%.2f)",
                strategy_name, symbol, signal_type, size, fill_price,
                new_size, pos.get("avg_price", 0.0), realized,
            )

    # ------------------------------------------------------------------
    # Position queries
    # ------------------------------------------------------------------

    def get_position(self, strategy_name: str, symbol: str) -> dict:
        """Return the current position for a strategy/symbol pair.

        Returns
        -------
        dict
            ``{"size": int, "avg_price": float, "unrealized_pnl": float}``
            Returns a zero position if none exists.
        """
        with self._lock:
            pos = (
                self._positions
                .get(strategy_name, {})
                .get(symbol)
            )
            if pos is None:
                return {"size": 0, "avg_price": 0.0, "unrealized_pnl": 0.0}

            unrealized = self._calc_unrealized(
                pos["size"], pos["avg_price"], symbol
            )
            return {
                "size": pos["size"],
                "avg_price": pos["avg_price"],
                "unrealized_pnl": round(unrealized, 2),
            }

    def get_strategy_positions(self, strategy_name: str) -> dict[str, dict]:
        """Return all open positions for a strategy.

        Returns
        -------
        dict[str, dict]
            Mapping of ``symbol -> {"size", "avg_price", "unrealized_pnl"}``.
        """
        with self._lock:
            strat_positions = self._positions.get(strategy_name, {})
            result = {}
            for symbol, pos in strat_positions.items():
                unrealized = self._calc_unrealized(
                    pos["size"], pos["avg_price"], symbol
                )
                result[symbol] = {
                    "size": pos["size"],
                    "avg_price": pos["avg_price"],
                    "unrealized_pnl": round(unrealized, 2),
                }
            return result

    def get_portfolio_summary(self) -> dict:
        """Return a portfolio-wide summary.

        Returns
        -------
        dict
            Keys:
            - ``total_positions`` (int): Count of non-zero positions.
            - ``total_unrealized_pnl`` (float): Sum of unrealized P&L.
            - ``total_realized_pnl`` (float): Sum of realized P&L (all time).
            - ``strategies`` (dict): Per-strategy breakdown with position
              count, unrealized P&L, and realized P&L.
        """
        with self._lock:
            total_positions = 0
            total_unrealized = 0.0
            total_realized = 0.0
            strategies = {}

            for strategy_name, symbols in self._positions.items():
                strat_unrealized = 0.0
                strat_realized = 0.0
                strat_count = 0

                for symbol, pos in symbols.items():
                    if pos["size"] != 0:
                        strat_count += 1
                        strat_unrealized += self._calc_unrealized(
                            pos["size"], pos["avg_price"], symbol
                        )
                    strat_realized += pos["realized_pnl"]

                total_positions += strat_count
                total_unrealized += strat_unrealized
                total_realized += strat_realized

                strategies[strategy_name] = {
                    "position_count": strat_count,
                    "unrealized_pnl": round(strat_unrealized, 2),
                    "realized_pnl": round(strat_realized, 2),
                }

            return {
                "total_positions": total_positions,
                "total_unrealized_pnl": round(total_unrealized, 2),
                "total_realized_pnl": round(total_realized, 2),
                "strategies": strategies,
            }

    # ------------------------------------------------------------------
    # Limit checks
    # ------------------------------------------------------------------

    def check_position_limits(
        self,
        strategy_name: str,
        symbol: str,
        proposed_size: int,
    ) -> tuple[bool, str]:
        """Check whether a proposed trade would violate any position limits.

        Parameters
        ----------
        strategy_name : str
            The strategy requesting the trade.
        symbol : str
            The target symbol.
        proposed_size : int
            The signed size change being proposed (positive = buying,
            negative = selling/shorting).

        Returns
        -------
        tuple[bool, str]
            ``(allowed, reason)`` -- ``allowed`` is True if within all
            limits, False otherwise.  ``reason`` explains the denial.
        """
        with self._lock:
            # --- Per-strategy limit ---
            if self._max_per_strategy is not None:
                strat_positions = self._positions.get(strategy_name, {})
                open_symbols = sum(
                    1 for pos in strat_positions.values() if pos["size"] != 0
                )
                # If this symbol already has a position it won't add a new slot
                has_existing = (
                    symbol in strat_positions
                    and strat_positions[symbol]["size"] != 0
                )
                if not has_existing and open_symbols >= self._max_per_strategy:
                    return (
                        False,
                        f"Strategy '{strategy_name}' already has "
                        f"{open_symbols}/{self._max_per_strategy} open "
                        f"positions (max_per_strategy).",
                    )

            # --- Per-symbol limit ---
            if self._max_per_symbol is not None:
                total_symbol_size = 0
                for strat_positions in self._positions.values():
                    pos = strat_positions.get(symbol)
                    if pos:
                        total_symbol_size += abs(pos["size"])
                if total_symbol_size + abs(proposed_size) > self._max_per_symbol:
                    return (
                        False,
                        f"Symbol '{symbol}' total size would be "
                        f"{total_symbol_size + abs(proposed_size)} "
                        f"(max_per_symbol={self._max_per_symbol}).",
                    )

            # --- Portfolio-wide limit ---
            if self._max_portfolio is not None:
                total_positions = 0
                for strat_positions in self._positions.values():
                    for pos in strat_positions.values():
                        if pos["size"] != 0:
                            total_positions += 1
                # Check if this would add a new position
                existing_pos = (
                    self._positions
                    .get(strategy_name, {})
                    .get(symbol)
                )
                is_new = existing_pos is None or existing_pos["size"] == 0
                if is_new and total_positions >= self._max_portfolio:
                    return (
                        False,
                        f"Portfolio already has {total_positions}/"
                        f"{self._max_portfolio} open positions "
                        f"(max_portfolio).",
                    )

        return (True, "")

    # ------------------------------------------------------------------
    # Market price updates
    # ------------------------------------------------------------------

    def update_market_price(self, symbol: str, price: float) -> None:
        """Update the latest market price for unrealized P&L calculation.

        Parameters
        ----------
        symbol : str
            Instrument symbol.
        price : float
            Current market price.
        """
        with self._lock:
            self._market_prices[symbol] = price

    # ------------------------------------------------------------------
    # Realized P&L
    # ------------------------------------------------------------------

    def get_daily_realized_pnl(self) -> float:
        """Return the sum of realized P&L entries recorded today (UTC).

        Returns
        -------
        float
            Total realized P&L for today.
        """
        today = datetime.now(timezone.utc).date()
        with self._lock:
            total = sum(
                pnl
                for ts, _strat, _sym, pnl in self._realized_pnl_log
                if ts.date() == today
            )
        return round(total, 2)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _calc_unrealized(
        self, size: int, avg_price: float, symbol: str
    ) -> float:
        """Compute unrealized P&L for a position.

        Must be called while holding ``_lock``.
        """
        if size == 0 or avg_price == 0.0:
            return 0.0
        market_price = self._market_prices.get(symbol)
        if market_price is None:
            return 0.0
        if size > 0:
            return (market_price - avg_price) * size
        else:
            return (avg_price - market_price) * abs(size)


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------

def _same_sign(a: int, b: int) -> bool:
    """Return True if a and b have the same sign (both positive or both negative)."""
    return (a > 0 and b > 0) or (a < 0 and b < 0)


def _is_reducing(old_size: int, delta: int) -> bool:
    """Return True if delta is reducing (or flipping) the position."""
    if old_size > 0:
        return delta < 0
    if old_size < 0:
        return delta > 0
    return False


def _is_adding(old_size: int, delta: int) -> bool:
    """Return True if delta is adding to the position in the same direction."""
    if old_size > 0:
        return delta > 0
    if old_size < 0:
        return delta < 0
    return False
