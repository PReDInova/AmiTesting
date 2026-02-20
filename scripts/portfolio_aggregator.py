"""
Portfolio aggregator for multi-strategy live trading.

Sits between signal scanners and the trade executor.  Collects signals
from multiple strategy scanners, detects conflicts across strategies
for the same symbol, enforces position limits, and outputs a filtered
list of net trade actions (TradeRequests) ready for execution.

Resolution modes
-----------------
additive  -- Each strategy trades independently; all signals pass through.
priority  -- For each symbol, only the signal from the highest-priority
             strategy (lowest ``priority`` value in ``strategy_priorities``)
             is kept.  Ties are broken alphabetically by strategy name.
veto      -- Any Short signal for a symbol blocks all Buy signals for
             that symbol in the same batch.  Sell and Cover pass through
             unchanged.
"""

import logging
from collections import defaultdict
from datetime import datetime

from scripts.signal_scanner import Signal
from scripts.trade_executor import TradeRequest

logger = logging.getLogger(__name__)


class PortfolioAggregator:
    """Aggregate signals from multiple strategy scanners into net trade actions.

    Parameters
    ----------
    resolution_mode : str
        Conflict-resolution strategy.  One of ``"additive"``,
        ``"priority"``, or ``"veto"``.
    strategy_priorities : dict[str, int] | None
        Mapping of strategy name to priority value (lower = higher
        priority).  Only used in ``"priority"`` mode.  Strategies not
        listed default to priority ``999``.
    default_size : int
        Default number of contracts when building a TradeRequest.
    max_position_per_strategy : int | None
        Maximum open position (in contracts) a single strategy may hold
        for any one symbol.  ``None`` means unlimited.
    max_position_per_symbol : int | None
        Maximum aggregate open position across all strategies for any
        one symbol.  ``None`` means unlimited.
    max_portfolio_position : int | None
        Maximum total open position across all symbols and strategies.
        ``None`` means unlimited.
    """

    _VALID_MODES = {"additive", "priority", "veto"}

    def __init__(
        self,
        resolution_mode: str = "additive",
        strategy_priorities: dict[str, int] | None = None,
        default_size: int = 1,
        max_position_per_strategy: int | None = None,
        max_position_per_symbol: int | None = None,
        max_portfolio_position: int | None = None,
    ):
        if resolution_mode not in self._VALID_MODES:
            raise ValueError(
                f"Invalid resolution_mode '{resolution_mode}'. "
                f"Must be one of {sorted(self._VALID_MODES)}."
            )

        self.resolution_mode = resolution_mode
        self.strategy_priorities = strategy_priorities or {}
        self.default_size = default_size
        self.max_position_per_strategy = max_position_per_strategy
        self.max_position_per_symbol = max_position_per_symbol
        self.max_portfolio_position = max_portfolio_position

        logger.info(
            "PortfolioAggregator initialised: mode=%s, default_size=%d, "
            "max_per_strategy=%s, max_per_symbol=%s, max_portfolio=%s",
            resolution_mode, default_size,
            max_position_per_strategy, max_position_per_symbol,
            max_portfolio_position,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_signals(
        self,
        signals: list[Signal],
        current_positions: dict,
    ) -> list[TradeRequest]:
        """Resolve conflicts and apply position limits to incoming signals.

        Parameters
        ----------
        signals : list[Signal]
            Raw signals from all active strategy scanners.
        current_positions : dict
            Mapping of ``(symbol, strategy_name) -> int`` representing
            the current signed position size (positive = long, negative
            = short, 0 = flat).

        Returns
        -------
        list[TradeRequest]
            Filtered, conflict-resolved trade requests ready for the
            executor.
        """
        if not signals:
            return []

        logger.debug(
            "Processing %d signal(s) with mode='%s'",
            len(signals), self.resolution_mode,
        )

        # Step 1: conflict resolution
        resolver = {
            "additive": self._resolve_additive,
            "priority": self._resolve_priority,
            "veto": self._resolve_veto,
        }[self.resolution_mode]

        requests = resolver(signals, current_positions)

        # Step 2: enforce position limits
        requests = self._apply_position_limits(requests, current_positions)

        logger.info(
            "Aggregation complete: %d signal(s) in -> %d request(s) out",
            len(signals), len(requests),
        )
        return requests

    # ------------------------------------------------------------------
    # Resolution strategies
    # ------------------------------------------------------------------

    def _resolve_additive(
        self,
        signals: list[Signal],
        current_positions: dict,
    ) -> list[TradeRequest]:
        """Each strategy trades independently -- all signals pass through.

        Every incoming signal is converted to a TradeRequest without any
        cross-strategy filtering.
        """
        requests = []
        for sig in signals:
            requests.append(self._signal_to_request(sig))
        logger.debug("Additive: %d signal(s) -> %d request(s)",
                      len(signals), len(requests))
        return requests

    def _resolve_priority(
        self,
        signals: list[Signal],
        current_positions: dict,
    ) -> list[TradeRequest]:
        """Highest-priority strategy wins for each symbol.

        For every symbol that has signals, only keep the signal(s) from
        the strategy with the lowest priority value.  Ties are broken
        alphabetically by strategy name.
        """
        # Group signals by symbol
        by_symbol: dict[str, list[Signal]] = defaultdict(list)
        for sig in signals:
            by_symbol[sig.symbol].append(sig)

        requests = []
        for symbol, sym_signals in by_symbol.items():
            # Find the winning strategy (lowest priority number)
            def _sort_key(s: Signal) -> tuple[int, str]:
                prio = self.strategy_priorities.get(s.strategy_name, 999)
                return (prio, s.strategy_name)

            sym_signals.sort(key=_sort_key)
            winner = _sort_key(sym_signals[0])

            for sig in sym_signals:
                if _sort_key(sig) == winner:
                    requests.append(self._signal_to_request(sig))
                else:
                    logger.debug(
                        "Priority: dropped %s %s from '%s' "
                        "(outranked by '%s')",
                        sig.signal_type, sig.symbol,
                        sig.strategy_name, sym_signals[0].strategy_name,
                    )

        logger.debug("Priority: %d signal(s) -> %d request(s)",
                      len(signals), len(requests))
        return requests

    def _resolve_veto(
        self,
        signals: list[Signal],
        current_positions: dict,
    ) -> list[TradeRequest]:
        """Any Short signal for a symbol blocks all Buy signals for that symbol.

        Sell and Cover signals always pass through.  If at least one
        Short exists for a given symbol, all Buy signals for that symbol
        are dropped.
        """
        # Collect symbols that have a Short signal
        shorted_symbols: set[str] = set()
        for sig in signals:
            if sig.signal_type == "Short":
                shorted_symbols.add(sig.symbol)

        requests = []
        for sig in signals:
            if sig.signal_type == "Buy" and sig.symbol in shorted_symbols:
                logger.debug(
                    "Veto: blocked Buy %s from '%s' "
                    "(Short signal exists for symbol)",
                    sig.symbol, sig.strategy_name,
                )
                continue
            requests.append(self._signal_to_request(sig))

        logger.debug("Veto: %d signal(s) -> %d request(s)",
                      len(signals), len(requests))
        return requests

    # ------------------------------------------------------------------
    # Position-limit enforcement
    # ------------------------------------------------------------------

    def _apply_position_limits(
        self,
        requests: list[TradeRequest],
        current_positions: dict,
    ) -> list[TradeRequest]:
        """Filter out requests that would breach position limits.

        Exit signals (Sell, Cover) are never blocked -- only entry
        signals (Buy, Short) are subject to limit checks.
        """
        filtered: list[TradeRequest] = []

        # Running tallies (start from current state)
        per_strategy: dict[tuple[str, str], int] = {}
        per_symbol: dict[str, int] = defaultdict(int)
        portfolio_total: int = 0

        for (sym, strat), size in current_positions.items():
            per_strategy[(sym, strat)] = abs(size)
            per_symbol[sym] += abs(size)
            portfolio_total += abs(size)

        for req in requests:
            # Exit signals always pass
            if req.signal_type in ("Sell", "Cover"):
                filtered.append(req)
                continue

            key = (req.symbol, req.strategy_name)
            new_strategy_pos = per_strategy.get(key, 0) + req.size
            new_symbol_pos = per_symbol.get(req.symbol, 0) + req.size
            new_portfolio_pos = portfolio_total + req.size

            # Check per-strategy limit
            if (self.max_position_per_strategy is not None
                    and new_strategy_pos > self.max_position_per_strategy):
                logger.debug(
                    "Limit: blocked %s %s from '%s' "
                    "(strategy position %d would exceed max %d)",
                    req.signal_type, req.symbol, req.strategy_name,
                    new_strategy_pos, self.max_position_per_strategy,
                )
                continue

            # Check per-symbol limit
            if (self.max_position_per_symbol is not None
                    and new_symbol_pos > self.max_position_per_symbol):
                logger.debug(
                    "Limit: blocked %s %s from '%s' "
                    "(symbol position %d would exceed max %d)",
                    req.signal_type, req.symbol, req.strategy_name,
                    new_symbol_pos, self.max_position_per_symbol,
                )
                continue

            # Check portfolio-wide limit
            if (self.max_portfolio_position is not None
                    and new_portfolio_pos > self.max_portfolio_position):
                logger.debug(
                    "Limit: blocked %s %s from '%s' "
                    "(portfolio position %d would exceed max %d)",
                    req.signal_type, req.symbol, req.strategy_name,
                    new_portfolio_pos, self.max_portfolio_position,
                )
                continue

            # Passed all limits -- update running tallies
            per_strategy[key] = new_strategy_pos
            per_symbol[req.symbol] = new_symbol_pos
            portfolio_total = new_portfolio_pos
            filtered.append(req)

        if len(filtered) < len(requests):
            logger.info(
                "Position limits removed %d request(s)",
                len(requests) - len(filtered),
            )

        return filtered

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _signal_to_request(self, signal: Signal) -> TradeRequest:
        """Convert a Signal dataclass into a TradeRequest dataclass."""
        return TradeRequest(
            signal_type=signal.signal_type,
            symbol=signal.symbol,
            size=self.default_size,
            price=signal.close_price,
            strategy_name=signal.strategy_name,
            timestamp=signal.timestamp,
        )
