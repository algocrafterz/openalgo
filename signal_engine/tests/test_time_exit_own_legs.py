"""The time exit closes each strategy's own legs, never the whole account.

2026-10-28: the 14:45 square-off sent OpenAlgo /closeposition once per strategy. That endpoint
closes every open position in the account whatever the strategy tag, so BREAKINGTRADE's call
also closed BREAKINGTRADE-RI's ANGELONE leg and three EVWAP-EARLY-PY positions, none of which
the engine booked. Each leg now gets its own MARKET order for its own quantity, capped by what
the broker's net position leaves for it.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker

BT, RI = "BREAKINGTRADE", "BREAKINGTRADE-RI"


def _tracker(*positions):
    tracker = PositionTracker(_make_engine())
    for pos in positions:
        tracker.register(pos)
    return tracker


def _ok(order_id):
    return TradeResult(status=OrderStatus.SUCCESS, order_id=order_id, message="ok")


async def _close(tracker, strategy, symbols, net, send=None):
    send = send or AsyncMock(side_effect=lambda order, pos: _ok(f"X-{pos.strategy}"))
    with (
        patch("signal_engine.tracker.fetch_net_position", AsyncMock(return_value=net)),
        patch("signal_engine.main._send_exit_with_retries", send),
        patch("signal_engine.api_client.close_all_positions", AsyncMock()) as close_all,
        patch("signal_engine.tracker.notifier", AsyncMock()) as notifier,
    ):
        await tracker._close_strategy(strategy, symbols)
    return send, close_all, notifier


class TestCloseOwnLegs:
    @pytest.mark.asyncio
    async def test_sells_only_this_strategys_quantity_on_a_shared_symbol(self):
        tracker = _tracker(
            _make_position(symbol="ANGELONE", strategy=BT, quantity=132),
            _make_position(symbol="ANGELONE", strategy=RI, quantity=132),
        )
        send, close_all, _ = await _close(tracker, BT, {"ANGELONE"}, net=264)

        close_all.assert_not_awaited()
        assert send.await_count == 1
        order, pos = send.await_args.args
        assert pos.strategy == BT
        assert order.quantity == 132
        assert order.strategy_tag == BT

    @pytest.mark.asyncio
    async def test_the_second_strategy_still_closes_after_the_first(self):
        tracker = _tracker(
            _make_position(symbol="ANGELONE", strategy=BT, quantity=132),
            _make_position(symbol="ANGELONE", strategy=RI, quantity=132),
        )
        await _close(tracker, BT, {"ANGELONE"}, net=264)
        # BT's sell has filled: the broker now nets to RI's 132 alone.
        send, _, _ = await _close(tracker, RI, {"ANGELONE"}, net=132)
        order, pos = send.await_args.args
        assert pos.strategy == RI and order.quantity == 132

    @pytest.mark.asyncio
    async def test_a_foreign_short_on_the_symbol_does_not_change_the_long_legs_close(self):
        # HDFCBANK shape: our long 131, an untracked short of 259 elsewhere in the account.
        tracker = _tracker(_make_position(symbol="HDFCBANK", strategy=BT, quantity=131))
        send, _, notifier = await _close(tracker, BT, {"HDFCBANK"}, net=131 - 259)
        # The broker shows no long left for this leg: sending a SELL would deepen the short.
        send.assert_not_awaited()
        notifier.notify.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_leg_whose_stop_already_filled_is_not_sold_again(self):
        tracker = _tracker(_make_position(symbol="RVNL", strategy=BT, quantity=312))
        send, _, notifier = await _close(tracker, BT, {"RVNL"}, net=0)
        send.assert_not_awaited()
        notifier.notify.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_partly_closed_leg_sells_only_what_is_left(self):
        tracker = _tracker(_make_position(symbol="RVNL", strategy=BT, quantity=312))
        send, _, _ = await _close(tracker, BT, {"RVNL"}, net=100)
        order, _ = send.await_args.args
        assert order.quantity == 100

    @pytest.mark.asyncio
    async def test_a_short_leg_buys_back_its_own_quantity(self):
        tracker = _tracker(
            _make_position(symbol="TCS", strategy=BT, quantity=70, direction=Direction.SHORT),
            _make_position(symbol="TCS", strategy=RI, quantity=30),
        )
        send, _, _ = await _close(tracker, BT, {"TCS"}, net=30 - 70)
        order, _ = send.await_args.args
        assert order.quantity == 70 and order.action == "BUY"

    @pytest.mark.asyncio
    async def test_an_unreadable_position_closes_the_tracked_quantity(self):
        tracker = _tracker(_make_position(symbol="RVNL", strategy=BT, quantity=312))
        send, _, _ = await _close(tracker, BT, {"RVNL"}, net=None)
        order, _ = send.await_args.args
        assert order.quantity == 312

    @pytest.mark.asyncio
    async def test_other_symbols_of_the_strategy_are_left_alone(self):
        tracker = _tracker(
            _make_position(symbol="ANGELONE", strategy=BT, quantity=132),
            _make_position(symbol="COLPAL", strategy=BT, quantity=52),
        )
        send, _, _ = await _close(tracker, BT, {"COLPAL"}, net=52)
        assert send.await_count == 1
        assert send.await_args.args[1].symbol == "COLPAL"

    @pytest.mark.asyncio
    async def test_the_closing_order_is_remembered_for_booking(self):
        tracker = _tracker(_make_position(symbol="COLPAL", strategy=BT, quantity=52))
        await _close(tracker, BT, {"COLPAL"}, net=52)
        assert tracker._square_off_orders[tracker._key("COLPAL", BT)] == f"X-{BT}"


class TestTimeExitEndToEnd:
    @pytest.mark.asyncio
    async def test_two_strategies_on_one_symbol_each_book_their_own_fill(self):
        tracker = _tracker(
            _make_position(symbol="ANGELONE", strategy=BT, quantity=132, fill_price=290.65),
            _make_position(symbol="ANGELONE", strategy=RI, quantity=132, fill_price=290.8),
        )
        nets = iter([264, 132])
        send = AsyncMock(side_effect=lambda order, pos: _ok(f"X-{pos.strategy}"))
        saved = []
        with (
            patch("signal_engine.tracker.cancel_all_orders", AsyncMock(return_value=True)),
            patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=[])),
            patch(
                "signal_engine.tracker.fetch_net_position",
                AsyncMock(side_effect=lambda *a: next(nets, 0)),
            ),
            patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=293.05)),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=[])),
            patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=0.0)),
            patch("signal_engine.main._send_exit_with_retries", send),
            patch("signal_engine.api_client.close_all_positions", AsyncMock()) as close_all,
            patch("signal_engine.tracker.notifier", AsyncMock()),
            patch.object(
                PositionTracker,
                "_save_close_row",
                lambda self, pos, **kw: saved.append((pos.strategy, kw)),
            ),
            patch("asyncio.sleep", AsyncMock()),
        ):
            await tracker.time_exit_all()

        close_all.assert_not_awaited()
        assert send.await_count == 2
        assert tracker.tracked_count == 0
        booked = dict(saved)
        assert booked[BT]["order_id"] == f"X-{BT}"
        assert booked[RI]["order_id"] == f"X-{RI}"
        assert round(booked[BT]["total_pnl"], 2) == round(132 * (293.05 - 290.65), 2)
        assert round(booked[RI]["total_pnl"], 2) == round(132 * (293.05 - 290.8), 2)
