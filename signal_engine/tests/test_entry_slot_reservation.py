"""Position-cap admission is atomic: concurrent entries cannot both take the last slot.

2026-10-15 09:51: BREAKOUT-PY dispatched two entries (HAL and ADANIPORTS) in the same cycle against
a cap of 1. The cap gate and the slot take (record_trade) were separated by the order round trip,
so both passed the gate, both filled, and the log showed "Capacity: 2/1 positions open".

Each entry now reserves its slot at the gate (no await between the check and the reservation) and
releases it immediately before record_trade() or on any path that ends without a position.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import main
from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.risk_fixtures import _engine

OK = TradeResult(order_id="E1", status=OrderStatus.SUCCESS, message="ok")


class TestReservationSemantics:
    def test_a_reservation_takes_the_last_slot(self):
        engine = _engine(max_open_positions=1)
        assert engine.check_exposure("ORB")
        engine.reserve_entry("sig-a", "ORB")
        assert not engine.check_exposure("ORB")
        assert engine.exposure_block_reason("ORB").startswith("Max positions")

    def test_a_reservation_below_the_cap_leaves_room(self):
        engine = _engine(max_open_positions=2)
        engine.reserve_entry("sig-a", "ORB")
        assert engine.check_exposure("ORB")
        engine.reserve_entry("sig-b", "ORB")
        assert not engine.check_exposure("ORB")

    def test_release_frees_the_slot(self):
        engine = _engine(max_open_positions=1)
        engine.reserve_entry("sig-a", "ORB")
        engine.release_entry("sig-a")
        assert engine.check_exposure("ORB")

    def test_release_is_idempotent_and_never_frees_another_signals_slot(self):
        engine = _engine(max_open_positions=2)
        engine.reserve_entry("sig-a", "ORB")
        engine.reserve_entry("sig-b", "ORB")
        engine.release_entry("sig-a")
        engine.release_entry("sig-a")
        assert engine.pending_entries_for("ORB") == 1

    def test_release_of_an_unknown_token_is_a_no_op(self):
        engine = _engine(max_open_positions=1)
        engine.release_entry("never-reserved")
        assert engine.check_exposure("ORB")

    def test_re_reserving_a_token_does_not_take_a_second_slot(self):
        engine = _engine(max_open_positions=2)
        engine.reserve_entry("sig-a", "ORB")
        engine.reserve_entry("sig-a", "ORB")
        assert engine.pending_entries_for("ORB") == 1
        assert engine.check_exposure("ORB")

    def test_the_daily_trade_cap_counts_reservations_too(self):
        engine = _engine(max_open_positions=0, max_trades_per_day=1)
        engine.reserve_entry("sig-a", "ORB")
        assert not engine.check_exposure("ORB")
        assert engine.exposure_block_reason("ORB").startswith("Max trades/day")

    def test_reservations_of_one_strategy_do_not_block_another_in_analyze_mode(self):
        engine = _engine(max_open_positions=1)
        engine.reserve_entry("sig-a", "ORB")
        assert engine.check_exposure("BREAKOUT-PY")

    def test_reservations_pool_across_strategies_in_live_mode(self):
        engine = _engine(max_open_positions=1, trade_mode="live")
        engine.reserve_entry("sig-a", "ORB")
        assert not engine.check_exposure("BREAKOUT-PY")

    def test_a_reserved_symbol_blocks_a_second_entry_in_the_same_name(self):
        engine = _engine(max_open_positions=5, max_positions_per_symbol=1)
        engine.reserve_entry("sig-a", "ORB", symbol="TCS")
        assert not engine.can_trade_symbol("TCS", "ORB")
        assert engine.can_trade_symbol("INFY", "ORB")
        assert engine.can_trade_symbol("TCS", "BREAKOUT-PY")  # a different strategy may take it

    def test_reserved_sector_entries_count_against_the_sector_cap(self):
        engine = _engine(
            max_open_positions=5, max_positions_per_symbol=1, max_positions_per_sector=2
        )
        engine.reserve_entry("sig-a", "ORB", symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN", "ORB")
        engine.reserve_entry("sig-b", "ORB", symbol="SBIN")
        assert not engine.can_trade_sector("HDFCBANK", "ORB")

    def test_releasing_a_reservation_releases_its_symbol_and_sector_too(self):
        engine = _engine(max_open_positions=5, max_positions_per_symbol=1)
        engine.reserve_entry("sig-a", "ORB", symbol="TCS")
        engine.release_entry("sig-a")
        assert engine.can_trade_symbol("TCS", "ORB")


def _signal(symbol: str, strategy: str = "ORB"):
    sig = MagicMock()
    sig.strategy = strategy
    sig.symbol = symbol
    sig.direction = Direction.LONG
    sig.entry = 100.0
    sig.sl = 99.0
    sig.tp = 0.0
    sig.exchange = "NSE"
    sig.product = "MIS"
    sig.sig_id = f"{strategy}-{symbol}"
    sig.context = {}
    return sig


class _Pipeline:
    """Runs real _handle_entry (real risk gates) with the I/O stages mocked."""

    def __init__(self, engine, send_result=OK, sizing=(10, True, 10)):
        self.engine = engine
        self.sends = []
        self.established = []
        self.declined = []
        self._send_result = send_result
        self._sizing = sizing

    async def _send(self, order):
        self.sends.append(order)
        await asyncio.sleep(0)  # the order round trip: other entries run here
        await asyncio.sleep(0)
        if isinstance(self._send_result, Exception):
            raise self._send_result
        return self._send_result

    async def _establish(self, signal, order, quantity, trade_result, entry_row_id=None):
        # the real one starts with record_trade; the reservation must be gone by now
        self.engine.record_trade(strategy=signal.strategy, symbol=signal.symbol)
        self.established.append(signal.symbol)
        return True

    async def run(self, *signals):
        async def decline(signal, stage, reason):
            self.declined.append((signal.symbol, stage, reason))

        with (
            patch.object(main, "risk_engine", self.engine),
            patch.object(main, "_entry_halted", AsyncMock(return_value=False)),
            patch.object(main, "_entry_rejected_by_symbol_rules", AsyncMock(return_value=False)),
            patch.object(main, "_entry_rejected_by_trade_policy", AsyncMock(return_value=False)),
            patch.object(main, "_entry_held_by_unresolved_order", AsyncMock(return_value=False)),
            patch.object(main, "_resolve_entry_capital", AsyncMock(return_value=100000.0)),
            patch.object(main, "_resolve_entry_quantity", AsyncMock(return_value=self._sizing)),
            patch.object(main, "_log_entry_sizing", MagicMock(return_value=1.0)),
            patch.object(
                main, "_build_entry_order", MagicMock(return_value=MagicMock(quantity=10))
            ),
            patch.object(main, "send_order", self._send),
            patch.object(main, "_notify_entry_outcome", AsyncMock()),
            patch.object(main, "_establish_position", self._establish),
            patch.object(main, "save", MagicMock()),
            patch.object(main, "_decline", decline),
            patch.object(main.notifier, "notify_risk_limit_hit", AsyncMock()),
        ):
            return await asyncio.gather(
                *(main._handle_entry(s) for s in signals), return_exceptions=True
            )


class TestConcurrentEntriesUnderTheCap:
    @pytest.mark.asyncio
    async def test_two_simultaneous_signals_under_a_cap_of_one_send_one_order(self):
        """The 2026-10-15 09:51 race."""
        engine = _engine(max_open_positions=1, max_positions_per_symbol=1)
        pipe = _Pipeline(engine)
        await pipe.run(_signal("HAL"), _signal("ADANIPORTS"))
        assert len(pipe.sends) == 1
        assert engine.open_positions_for("ORB") == 1
        assert [d[1] for d in pipe.declined] == ["risk_gates"]

    @pytest.mark.asyncio
    async def test_three_signals_under_a_cap_of_two_send_two_orders(self):
        engine = _engine(max_open_positions=2, max_trades_per_day=10)
        pipe = _Pipeline(engine)
        await pipe.run(_signal("HAL"), _signal("BEL"), _signal("NTPC"))
        assert len(pipe.sends) == 2
        assert engine.open_positions_for("ORB") == 2

    @pytest.mark.asyncio
    async def test_the_slot_is_counted_once_after_a_successful_entry(self):
        engine = _engine(max_open_positions=2)
        pipe = _Pipeline(engine)
        await pipe.run(_signal("HAL"))
        assert engine.open_positions_for("ORB") == 1
        assert engine.pending_entries_for("ORB") == 0

    @pytest.mark.asyncio
    async def test_a_rejected_entry_gives_its_slot_back(self):
        engine = _engine(max_open_positions=1)
        pipe = _Pipeline(engine, send_result=TradeResult(status=OrderStatus.REJECTED, message="no"))
        await pipe.run(_signal("HAL"))
        assert engine.pending_entries_for("ORB") == 0
        assert engine.open_positions_for("ORB") == 0
        assert engine.check_exposure("ORB")

    @pytest.mark.asyncio
    async def test_an_exception_mid_entry_gives_its_slot_back(self):
        engine = _engine(max_open_positions=1)
        pipe = _Pipeline(engine, send_result=RuntimeError("boom"))
        results = await pipe.run(_signal("HAL"))
        assert isinstance(results[0], RuntimeError)
        assert engine.pending_entries_for("ORB") == 0
        assert engine.check_exposure("ORB")

    @pytest.mark.asyncio
    async def test_an_entry_that_stops_before_sending_gives_its_slot_back(self):
        engine = _engine(max_open_positions=1)
        pipe = _Pipeline(engine, sizing=None)  # sizing declines the trade
        await pipe.run(_signal("HAL"))
        assert pipe.sends == []
        assert engine.pending_entries_for("ORB") == 0
        assert engine.check_exposure("ORB")

    @pytest.mark.asyncio
    async def test_a_slot_freed_by_a_failed_entry_is_usable_by_the_next_signal(self):
        engine = _engine(max_open_positions=1)
        failed = _Pipeline(
            engine, send_result=TradeResult(status=OrderStatus.REJECTED, message="no")
        )
        await failed.run(_signal("HAL"))
        ok = _Pipeline(engine)
        await ok.run(_signal("BEL"))
        assert len(ok.sends) == 1

    @pytest.mark.asyncio
    async def test_two_simultaneous_signals_for_one_symbol_send_one_order(self):
        engine = _engine(max_open_positions=5, max_positions_per_symbol=1)
        pipe = _Pipeline(engine)
        await pipe.run(_signal("TCS"), _signal("TCS"))
        assert len(pipe.sends) == 1

    @pytest.mark.asyncio
    async def test_two_simultaneous_signals_in_a_full_sector_send_one_order(self):
        engine = _engine(
            max_open_positions=5, max_positions_per_symbol=1, max_positions_per_sector=1
        )
        pipe = _Pipeline(engine)
        await pipe.run(_signal("HDFCBANK"), _signal("SBIN"))
        assert len(pipe.sends) == 1

    @pytest.mark.asyncio
    async def test_different_strategies_do_not_consume_each_others_slots_in_analyze_mode(self):
        engine = _engine(max_open_positions=1)
        pipe = _Pipeline(engine)
        await pipe.run(_signal("HAL", "ORB"), _signal("BEL", "BREAKOUT-PY"))
        assert len(pipe.sends) == 2

    @pytest.mark.asyncio
    async def test_live_mode_shares_one_cap_across_strategies(self):
        engine = _engine(max_open_positions=1, trade_mode="live")
        pipe = _Pipeline(engine)
        await pipe.run(_signal("HAL", "ORB"), _signal("BEL", "BREAKOUT-PY"))
        assert len(pipe.sends) == 1
