"""
Pre-trade risk management for the live signal alert system.

Sits between the signal aggregation layer and the trade executor.
Every TradeRequest must pass through the RiskManager before reaching
the executor.  Checks position limits, daily loss limits, and provides
drawdown circuit breakers.
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class RiskManager:
    """Pre-trade risk gate with position limits and drawdown circuit breakers.

    Parameters
    ----------
    max_position_per_strategy : int or None
        Maximum open contracts per strategy.
    max_position_per_symbol : int or None
        Maximum open contracts per symbol (summed across strategies).
    max_portfolio_position : int or None
        Maximum total open contracts across all strategies and symbols.
    max_daily_loss : float or None
        Maximum daily realized loss (negative number, e.g. -500.0).
        If breached, the circuit breaker trips and blocks all trades.
    max_loss_per_trade : float or None
        Maximum loss allowed per single trade (not currently enforceable
        for market orders, but logged as a warning).
    """

    def __init__(
        self,
        max_position_per_strategy: Optional[int] = None,
        max_position_per_symbol: Optional[int] = None,
        max_portfolio_position: Optional[int] = None,
        max_daily_loss: Optional[float] = None,
        max_loss_per_trade: Optional[float] = None,
    ):
        self._max_per_strategy = max_position_per_strategy
        self._max_per_symbol = max_position_per_symbol
        self._max_portfolio = max_portfolio_position
        self._max_daily_loss = max_daily_loss
        self._max_loss_per_trade = max_loss_per_trade

        # Circuit breaker state
        self._circuit_breaker_tripped = False
        self._circuit_breaker_reason = ""
        self._lock = threading.Lock()

        # Callbacks for circuit breaker events
        self._on_circuit_break = None

        # Track daily P&L
        self._daily_pnl = 0.0
        self._daily_pnl_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        logger.info(
            "RiskManager initialized: max_per_strategy=%s, max_per_symbol=%s, "
            "max_portfolio=%s, max_daily_loss=%s",
            max_position_per_strategy, max_position_per_symbol,
            max_portfolio_position, max_daily_loss,
        )

    @property
    def is_tripped(self) -> bool:
        """True if the circuit breaker has tripped."""
        return self._circuit_breaker_tripped

    @property
    def circuit_breaker_reason(self) -> str:
        return self._circuit_breaker_reason

    def set_circuit_break_callback(self, callback) -> None:
        """Set a callback for circuit breaker events: callback(reason: str)."""
        self._on_circuit_break = callback

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker (e.g. after review)."""
        with self._lock:
            self._circuit_breaker_tripped = False
            self._circuit_breaker_reason = ""
            logger.warning("Circuit breaker manually reset.")

    # ------------------------------------------------------------------
    # Pre-trade check
    # ------------------------------------------------------------------

    def check_trade(
        self,
        signal_type: str,
        symbol: str,
        size: int,
        strategy_name: str,
        current_positions: dict,
    ) -> tuple[bool, str]:
        """Check if a proposed trade is allowed by risk rules.

        Parameters
        ----------
        signal_type : str
            "Buy", "Sell", "Short", or "Cover".
        symbol : str
            The trading symbol.
        size : int
            Number of contracts.
        strategy_name : str
            Originating strategy name.
        current_positions : dict
            Nested dict: {strategy_name: {symbol: {"size": int, ...}}}.

        Returns
        -------
        (allowed, reason) : tuple[bool, str]
            If allowed is False, reason explains why.
        """
        with self._lock:
            # Rotate daily P&L first (may auto-reset circuit breaker on new day)
            self._rotate_daily_pnl()

            # Check circuit breaker
            if self._circuit_breaker_tripped:
                return (False, f"Circuit breaker tripped: {self._circuit_breaker_reason}")

            # Check daily loss limit
            if self._max_daily_loss is not None:
                if self._daily_pnl <= self._max_daily_loss:
                    self._trip_circuit_breaker(
                        f"Daily loss limit breached: ${self._daily_pnl:.2f} "
                        f"<= ${self._max_daily_loss:.2f}"
                    )
                    return (False, self._circuit_breaker_reason)

            # Position-increasing signals need limit checks
            is_increasing = signal_type in ("Buy", "Short")

            if not is_increasing:
                # Sell/Cover reduces exposure — always allowed
                return (True, "")

            # Per-strategy position limit
            if self._max_per_strategy is not None:
                strat_positions = current_positions.get(strategy_name, {})
                total_strat = sum(
                    abs(p.get("size", 0)) for p in strat_positions.values()
                )
                if total_strat + size > self._max_per_strategy:
                    return (
                        False,
                        f"Strategy '{strategy_name}' position limit: "
                        f"{total_strat} + {size} > {self._max_per_strategy}",
                    )

            # Per-symbol position limit (across all strategies)
            if self._max_per_symbol is not None:
                total_symbol = 0
                for strat_pos in current_positions.values():
                    sym_pos = strat_pos.get(symbol, {})
                    total_symbol += abs(sym_pos.get("size", 0))
                if total_symbol + size > self._max_per_symbol:
                    return (
                        False,
                        f"Symbol '{symbol}' position limit: "
                        f"{total_symbol} + {size} > {self._max_per_symbol}",
                    )

            # Portfolio-wide position limit
            if self._max_portfolio is not None:
                total_portfolio = 0
                for strat_pos in current_positions.values():
                    for sym_pos in strat_pos.values():
                        total_portfolio += abs(sym_pos.get("size", 0))
                if total_portfolio + size > self._max_portfolio:
                    return (
                        False,
                        f"Portfolio position limit: "
                        f"{total_portfolio} + {size} > {self._max_portfolio}",
                    )

            return (True, "")

    # ------------------------------------------------------------------
    # P&L tracking for drawdown circuit breakers
    # ------------------------------------------------------------------

    def record_trade_pnl(self, pnl: float) -> None:
        """Record a realized trade P&L for daily tracking."""
        with self._lock:
            self._rotate_daily_pnl()
            self._daily_pnl += pnl
            logger.debug("Daily P&L updated: %.2f (trade: %.2f)",
                         self._daily_pnl, pnl)

            # Check if daily loss limit is breached
            if (self._max_daily_loss is not None
                    and self._daily_pnl <= self._max_daily_loss):
                self._trip_circuit_breaker(
                    f"Daily loss limit breached: ${self._daily_pnl:.2f} "
                    f"<= ${self._max_daily_loss:.2f}"
                )

    def check_drawdown(self) -> tuple[bool, str]:
        """Check if daily drawdown limits have been breached.

        Returns (breached, reason).
        """
        with self._lock:
            self._rotate_daily_pnl()
            if (self._max_daily_loss is not None
                    and self._daily_pnl <= self._max_daily_loss):
                return (True, f"Daily P&L: ${self._daily_pnl:.2f} "
                              f"<= limit ${self._max_daily_loss:.2f}")
            return (False, "")

    @property
    def daily_pnl(self) -> float:
        with self._lock:
            self._rotate_daily_pnl()
            return self._daily_pnl

    def _rotate_daily_pnl(self) -> None:
        """Reset daily P&L if the date has changed (must hold lock)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_pnl_date:
            self._daily_pnl = 0.0
            self._daily_pnl_date = today
            # Reset circuit breaker on new day
            if self._circuit_breaker_tripped:
                logger.info("New trading day — circuit breaker auto-reset.")
                self._circuit_breaker_tripped = False
                self._circuit_breaker_reason = ""

    def _trip_circuit_breaker(self, reason: str) -> None:
        """Trip the circuit breaker (must hold lock)."""
        self._circuit_breaker_tripped = True
        self._circuit_breaker_reason = reason
        logger.critical("CIRCUIT BREAKER TRIPPED: %s", reason)
        if self._on_circuit_break:
            try:
                self._on_circuit_break(reason)
            except Exception:
                logger.exception("Circuit break callback error")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Get current risk manager status."""
        with self._lock:
            self._rotate_daily_pnl()
            return {
                "circuit_breaker_tripped": self._circuit_breaker_tripped,
                "circuit_breaker_reason": self._circuit_breaker_reason,
                "daily_pnl": self._daily_pnl,
                "daily_pnl_date": self._daily_pnl_date,
                "limits": {
                    "max_per_strategy": self._max_per_strategy,
                    "max_per_symbol": self._max_per_symbol,
                    "max_portfolio": self._max_portfolio,
                    "max_daily_loss": self._max_daily_loss,
                },
            }
