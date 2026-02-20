"""
Tests for scripts.portfolio_aggregator -- Multi-strategy signal aggregation.

Covers additive, priority, and veto resolution modes, conflict detection
(Buy vs Short on the same symbol), and position-limit enforcement.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

# Ensure the project root is importable
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.portfolio_aggregator import PortfolioAggregator
from scripts.signal_scanner import Signal
from scripts.trade_executor import TradeRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(signal_type="Buy", symbol="NQ", strategy_name="StratA",
                 close_price=18500.0):
    return Signal(
        signal_type=signal_type,
        symbol=symbol,
        timestamp=datetime(2026, 2, 20, 10, 0, 0),
        close_price=close_price,
        strategy_name=strategy_name,
        indicator_values={},
    )


def _empty_positions():
    return {}


# ===========================================================================
# Constructor validation
# ===========================================================================

class TestConstructor:
    """Validate constructor and invalid mode rejection."""

    def test_valid_modes_accepted(self):
        for mode in ("additive", "priority", "veto"):
            agg = PortfolioAggregator(resolution_mode=mode)
            assert agg.resolution_mode == mode

    def test_invalid_mode_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid resolution_mode"):
            PortfolioAggregator(resolution_mode="invalid_mode")

    def test_default_values(self):
        agg = PortfolioAggregator()
        assert agg.resolution_mode == "additive"
        assert agg.default_size == 1
        assert agg.strategy_priorities == {}
        assert agg.max_position_per_strategy is None
        assert agg.max_position_per_symbol is None
        assert agg.max_portfolio_position is None


# ===========================================================================
# Additive resolution mode
# ===========================================================================

class TestAdditiveMode:
    """In additive mode, all signals pass through without filtering."""

    def test_all_signals_pass_through(self):
        agg = PortfolioAggregator(resolution_mode="additive")
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
            _make_signal("Buy", "ES", "StratA"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 3

    def test_conflicting_signals_both_pass(self):
        """Buy and Short on the same symbol both pass in additive mode."""
        agg = PortfolioAggregator(resolution_mode="additive")
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 2
        types = {r.signal_type for r in requests}
        assert types == {"Buy", "Short"}

    def test_empty_signals_returns_empty(self):
        agg = PortfolioAggregator(resolution_mode="additive")
        requests = agg.process_signals([], _empty_positions())
        assert requests == []

    def test_single_signal(self):
        agg = PortfolioAggregator(resolution_mode="additive")
        signals = [_make_signal("Buy", "NQ", "StratA", 18500.0)]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 1
        assert requests[0].signal_type == "Buy"
        assert requests[0].symbol == "NQ"
        assert requests[0].price == 18500.0

    def test_request_uses_default_size(self):
        agg = PortfolioAggregator(resolution_mode="additive", default_size=3)
        signals = [_make_signal("Buy", "NQ", "StratA")]
        requests = agg.process_signals(signals, _empty_positions())
        assert requests[0].size == 3


# ===========================================================================
# Priority resolution mode
# ===========================================================================

class TestPriorityMode:
    """Highest-priority strategy wins per symbol (lowest priority number)."""

    def test_highest_priority_wins(self):
        agg = PortfolioAggregator(
            resolution_mode="priority",
            strategy_priorities={"StratA": 1, "StratB": 2},
        )
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 1
        assert requests[0].strategy_name == "StratA"
        assert requests[0].signal_type == "Buy"

    def test_lower_priority_number_is_higher_priority(self):
        agg = PortfolioAggregator(
            resolution_mode="priority",
            strategy_priorities={"StratA": 10, "StratB": 1},
        )
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 1
        assert requests[0].strategy_name == "StratB"

    def test_unlisted_strategy_defaults_to_999(self):
        agg = PortfolioAggregator(
            resolution_mode="priority",
            strategy_priorities={"StratA": 5},
        )
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "UnlistedStrat"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 1
        assert requests[0].strategy_name == "StratA"

    def test_same_priority_tie_broken_alphabetically(self):
        agg = PortfolioAggregator(
            resolution_mode="priority",
            strategy_priorities={"AlphaStrat": 1, "BetaStrat": 1},
        )
        signals = [
            _make_signal("Buy", "NQ", "BetaStrat"),
            _make_signal("Short", "NQ", "AlphaStrat"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 1
        # "AlphaStrat" < "BetaStrat" alphabetically
        assert requests[0].strategy_name == "AlphaStrat"

    def test_different_symbols_independent(self):
        """Priority is resolved per symbol independently."""
        agg = PortfolioAggregator(
            resolution_mode="priority",
            strategy_priorities={"StratA": 1, "StratB": 2},
        )
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
            _make_signal("Buy", "ES", "StratB"),  # Only signal for ES
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 2

        nq_req = [r for r in requests if r.symbol == "NQ"][0]
        es_req = [r for r in requests if r.symbol == "ES"][0]
        assert nq_req.strategy_name == "StratA"
        assert es_req.strategy_name == "StratB"

    def test_multiple_signals_from_winning_strategy(self):
        """If the winning strategy has multiple signals for a symbol, all pass."""
        agg = PortfolioAggregator(
            resolution_mode="priority",
            strategy_priorities={"StratA": 1, "StratB": 2},
        )
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Buy", "NQ", "StratA"),  # Duplicate from same strategy
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        # Both StratA signals pass, StratB is dropped
        assert len(requests) == 2
        assert all(r.strategy_name == "StratA" for r in requests)


# ===========================================================================
# Veto resolution mode
# ===========================================================================

class TestVetoMode:
    """Any Short signal blocks all Buy signals for that symbol."""

    def test_short_blocks_buy_on_same_symbol(self):
        agg = PortfolioAggregator(resolution_mode="veto")
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        # Buy is blocked, Short passes
        assert len(requests) == 1
        assert requests[0].signal_type == "Short"
        assert requests[0].strategy_name == "StratB"

    def test_short_does_not_block_different_symbol(self):
        agg = PortfolioAggregator(resolution_mode="veto")
        signals = [
            _make_signal("Buy", "ES", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 2

    def test_sell_and_cover_always_pass(self):
        agg = PortfolioAggregator(resolution_mode="veto")
        signals = [
            _make_signal("Sell", "NQ", "StratA"),
            _make_signal("Cover", "NQ", "StratB"),
            _make_signal("Short", "NQ", "StratC"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        # All pass: Sell and Cover are not blocked, Short is the blocker
        assert len(requests) == 3

    def test_no_short_means_buy_passes(self):
        agg = PortfolioAggregator(resolution_mode="veto")
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Buy", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 2

    def test_multiple_buys_all_blocked_when_short_exists(self):
        agg = PortfolioAggregator(resolution_mode="veto")
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Buy", "NQ", "StratB"),
            _make_signal("Buy", "NQ", "StratC"),
            _make_signal("Short", "NQ", "StratD"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        # All 3 Buys blocked, 1 Short passes
        assert len(requests) == 1
        assert requests[0].signal_type == "Short"

    def test_veto_does_not_block_sell(self):
        """Sell on a symbol with a Short should still pass."""
        agg = PortfolioAggregator(resolution_mode="veto")
        signals = [
            _make_signal("Sell", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 2


# ===========================================================================
# Conflict detection (Buy vs Short on same symbol)
# ===========================================================================

class TestConflictDetection:
    """Verify how each mode handles Buy+Short conflicts."""

    def test_additive_allows_conflict(self):
        agg = PortfolioAggregator(resolution_mode="additive")
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 2

    def test_priority_resolves_conflict_by_rank(self):
        agg = PortfolioAggregator(
            resolution_mode="priority",
            strategy_priorities={"StratA": 2, "StratB": 1},
        )
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 1
        assert requests[0].strategy_name == "StratB"
        assert requests[0].signal_type == "Short"

    def test_veto_blocks_buy_keeps_short(self):
        agg = PortfolioAggregator(resolution_mode="veto")
        signals = [
            _make_signal("Buy", "NQ", "StratA"),
            _make_signal("Short", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 1
        assert requests[0].signal_type == "Short"


# ===========================================================================
# Position limit checking
# ===========================================================================

class TestPositionLimits:
    """Verify position limit enforcement in _apply_position_limits."""

    def test_per_strategy_limit_blocks_excess(self):
        """Per-strategy limit is checked per (symbol, strategy) key.
        Adding to an existing (symbol, strategy) pair that already meets
        the limit should be blocked."""
        agg = PortfolioAggregator(
            resolution_mode="additive",
            max_position_per_strategy=1,
        )
        # Existing: StratA already has 1 on NQ
        positions = {("NQ", "StratA"): 1}
        signals = [
            _make_signal("Buy", "NQ", "StratA"),  # Would make (NQ, StratA) = 2 > 1
        ]
        requests = agg.process_signals(signals, positions)
        assert len(requests) == 0

    def test_per_strategy_limit_allows_within_limit(self):
        agg = PortfolioAggregator(
            resolution_mode="additive",
            max_position_per_strategy=5,
        )
        positions = {("NQ", "StratA"): 1}
        signals = [_make_signal("Buy", "ES", "StratA")]
        requests = agg.process_signals(signals, positions)
        assert len(requests) == 1

    def test_per_symbol_limit_blocks_excess(self):
        agg = PortfolioAggregator(
            resolution_mode="additive",
            max_position_per_symbol=2,
        )
        positions = {("NQ", "StratA"): 1, ("NQ", "StratB"): 1}
        signals = [_make_signal("Buy", "NQ", "StratC")]
        requests = agg.process_signals(signals, positions)
        # NQ already has 2, adding 1 more exceeds limit
        assert len(requests) == 0

    def test_per_symbol_limit_allows_within_limit(self):
        agg = PortfolioAggregator(
            resolution_mode="additive",
            max_position_per_symbol=3,
        )
        positions = {("NQ", "StratA"): 1}
        signals = [_make_signal("Buy", "NQ", "StratB")]
        requests = agg.process_signals(signals, positions)
        assert len(requests) == 1

    def test_portfolio_limit_blocks_excess(self):
        agg = PortfolioAggregator(
            resolution_mode="additive",
            max_portfolio_position=2,
        )
        positions = {("NQ", "StratA"): 1, ("ES", "StratB"): 1}
        signals = [_make_signal("Buy", "YM", "StratC")]
        requests = agg.process_signals(signals, positions)
        assert len(requests) == 0

    def test_portfolio_limit_allows_within_limit(self):
        agg = PortfolioAggregator(
            resolution_mode="additive",
            max_portfolio_position=5,
        )
        positions = {("NQ", "StratA"): 1}
        signals = [_make_signal("Buy", "ES", "StratB")]
        requests = agg.process_signals(signals, positions)
        assert len(requests) == 1

    def test_exit_signals_never_blocked_by_limits(self):
        """Sell and Cover bypass all position limit checks."""
        agg = PortfolioAggregator(
            resolution_mode="additive",
            max_position_per_strategy=0,  # Would block everything
            max_position_per_symbol=0,
            max_portfolio_position=0,
        )
        signals = [
            _make_signal("Sell", "NQ", "StratA"),
            _make_signal("Cover", "NQ", "StratB"),
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 2

    def test_limits_accumulate_across_requests_in_batch(self):
        """When processing multiple requests in a batch, running tallies
        should account for earlier accepted requests."""
        agg = PortfolioAggregator(
            resolution_mode="additive",
            max_position_per_symbol=2,
        )
        signals = [
            _make_signal("Buy", "NQ", "StratA"),  # NQ: 0 + 1 = 1 (ok)
            _make_signal("Buy", "NQ", "StratB"),  # NQ: 1 + 1 = 2 (ok)
            _make_signal("Buy", "NQ", "StratC"),  # NQ: 2 + 1 = 3 (blocked)
        ]
        requests = agg.process_signals(signals, _empty_positions())
        assert len(requests) == 2

    def test_no_limits_allows_everything(self):
        agg = PortfolioAggregator(resolution_mode="additive")
        positions = {("NQ", "StratA"): 100}
        signals = [_make_signal("Buy", "NQ", "StratA")]
        requests = agg.process_signals(signals, positions)
        assert len(requests) == 1


# ===========================================================================
# Signal-to-request conversion
# ===========================================================================

class TestSignalToRequest:
    """Verify the signal-to-request conversion preserves fields."""

    def test_conversion_preserves_fields(self):
        agg = PortfolioAggregator(resolution_mode="additive", default_size=5)
        signal = _make_signal(
            signal_type="Buy",
            symbol="NQ",
            strategy_name="Momentum",
            close_price=18500.25,
        )
        requests = agg.process_signals([signal], _empty_positions())

        assert len(requests) == 1
        req = requests[0]
        assert isinstance(req, TradeRequest)
        assert req.signal_type == "Buy"
        assert req.symbol == "NQ"
        assert req.strategy_name == "Momentum"
        assert req.price == 18500.25
        assert req.size == 5
        assert req.timestamp == signal.timestamp
