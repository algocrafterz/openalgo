"""2026-10-14 HINDALCO: an exit must cancel every live protective order, not the tracked id only.

The tracked stop (059042) was cancelled at 11:00. A twin stop (458462036) that landed late was
never tracked, stayed live, triggered at 11:16 on a flat position and opened a 319 short.
"""

from unittest.mock import AsyncMock

import pytest

from signal_engine.main import _cancel_sl_before_exit, _handle_exit
from signal_engine.tests.test_main_characterization import (
    _exit_signal,
    _position,
    _real_tracker,
    harness,
)


def _stop(order_id, status="trigger pending", strategy="ORB", **extra):
    row = {
        "orderid": order_id,
        "symbol": "RELIANCE",
        "action": "SELL",
        "quantity": 50,
        "pricetype": "SL-M",
        "trigger_price": 2485.0,
        "order_status": status,
        "strategy": strategy,
    }
    row.update(extra)
    return row


class TestCancelSlBeforeExit:
    @pytest.mark.asyncio
    async def test_untracked_twin_stop_is_cancelled_with_the_tracked_one(self):
        h = harness(
            fetch_orderbook=AsyncMock(side_effect=[[_stop("TWIN")], []]),
            cancel_order=AsyncMock(return_value=True),
        )
        with h.stack:
            assert await _cancel_sl_before_exit(_position(sl_order_id="SL1")) is True
        assert [c.args[0] for c in h.cancel_order.await_args_list] == ["SL1", "TWIN"]

    @pytest.mark.asyncio
    async def test_twin_is_found_even_when_no_stop_id_is_tracked(self):
        h = harness(
            fetch_orderbook=AsyncMock(side_effect=[[_stop("TWIN")], []]),
            cancel_order=AsyncMock(return_value=True),
        )
        with h.stack:
            assert await _cancel_sl_before_exit(_position(sl_order_id="")) is True
        assert [c.args[0] for c in h.cancel_order.await_args_list] == ["TWIN"]

    @pytest.mark.asyncio
    async def test_short_position_sweeps_buy_side_stops(self):
        from signal_engine.models import Direction

        buy_stop = _stop("S1", action="BUY")
        sell_stop = _stop("NOT-MINE", action="SELL")
        h = harness(
            fetch_orderbook=AsyncMock(side_effect=[[buy_stop, sell_stop], []]),
            cancel_order=AsyncMock(return_value=True),
        )
        with h.stack:
            await _cancel_sl_before_exit(_position(direction=Direction.SHORT, sl_order_id=""))
        assert [c.args[0] for c in h.cancel_order.await_args_list] == ["S1"]

    @pytest.mark.asyncio
    async def test_other_strategys_stop_on_the_symbol_is_left_alone(self):
        h = harness(
            fetch_orderbook=AsyncMock(
                side_effect=[[_stop("OTHER", strategy="BREAKOUT-PY")], []]
            ),
            cancel_order=AsyncMock(return_value=True),
        )
        with h.stack:
            await _cancel_sl_before_exit(_position(sl_order_id="SL1"))
        assert [c.args[0] for c in h.cancel_order.await_args_list] == ["SL1"]

    @pytest.mark.asyncio
    async def test_twin_that_will_not_cancel_reports_the_stop_as_still_working(self):
        h = harness(
            fetch_orderbook=AsyncMock(side_effect=[[_stop("TWIN")]] + [[_stop("TWIN")]] * 2),
            cancel_order=AsyncMock(return_value=True),
        )
        with h.stack:
            assert await _cancel_sl_before_exit(_position(sl_order_id="SL1")) is False

    @pytest.mark.asyncio
    async def test_failed_tracked_cancel_still_reports_false(self):
        h = harness(
            fetch_orderbook=AsyncMock(return_value=[]),
            cancel_order=AsyncMock(return_value=False),
        )
        with h.stack:
            assert await _cancel_sl_before_exit(_position(sl_order_id="SL1")) is False

    @pytest.mark.asyncio
    async def test_unreadable_book_does_not_block_the_exit_path(self):
        """The tracked stop is cancelled, the position needs its exit: the caller proceeds."""
        h = harness(
            fetch_orderbook=AsyncMock(return_value=None),
            cancel_order=AsyncMock(return_value=True),
        )
        with h.stack:
            assert await _cancel_sl_before_exit(_position(sl_order_id="SL1")) is True


class TestFullExitSweepsStragglers:
    @pytest.mark.asyncio
    async def test_stop_that_lands_after_the_exit_is_cancelled(self):
        """Pre-exit read is clean; a late-landing twin appears once the exit is booked."""
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(
            tracker=tracker,
            fetch_orderbook=AsyncMock(side_effect=[[], [_stop("LATE")], []]),
            cancel_order=AsyncMock(return_value=True),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        assert [c.args[0] for c in h.cancel_order.await_args_list] == ["SL1", "LATE"]
        h.send_order.assert_awaited_once()


class TestStopFilledDuringCancel:
    @pytest.mark.asyncio
    async def test_flat_broker_after_a_stop_filled_in_the_cancel_sends_no_exit(self):
        """The stop fired while being cancelled; a full-size exit would open a short."""
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(
            tracker=tracker,
            fetch_orderbook=AsyncMock(
                side_effect=[[_stop("TWIN")], [_stop("TWIN", status="complete")]]
            ),
            cancel_order=AsyncMock(return_value=True),
            fetch_open_position=AsyncMock(return_value=0),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_broker_still_holding_after_the_fill_still_exits(self):
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(
            tracker=tracker,
            fetch_orderbook=AsyncMock(
                side_effect=[[_stop("TWIN")], [_stop("TWIN", status="partially filled")], [], []]
            ),
            cancel_order=AsyncMock(return_value=True),
            fetch_open_position=AsyncMock(return_value=20),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        h.send_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_partly_filled_stop_exits_only_the_shares_still_held(self):
        """50 tracked, the stop sold 30 while being cancelled: a 50-share exit would short 30."""
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(
            tracker=tracker,
            fetch_orderbook=AsyncMock(
                side_effect=[[_stop("TWIN")], [_stop("TWIN", status="complete")], [], []]
            ),
            cancel_order=AsyncMock(return_value=True),
            fetch_open_position=AsyncMock(return_value=20),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        h.send_order.assert_awaited_once()
        assert h.build_exit_order.call_args.kwargs["quantity"] == 20


class TestTrackedStopAlreadyFilled:
    """The broker stop filled before the exit signal arrived, so its cancel fails.

    The sweep sees no live stop and reports clear; without a status check the full-size exit
    goes out on a flat position and opens a naked short.
    """

    @pytest.mark.asyncio
    async def test_filled_stop_and_flat_broker_sends_no_exit(self):
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(
            tracker=tracker,
            fetch_orderbook=AsyncMock(return_value=[]),
            cancel_order=AsyncMock(return_value=False),
            fetch_order_status=AsyncMock(return_value="complete"),
            fetch_open_position=AsyncMock(return_value=0),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        h.send_order.assert_not_awaited()
        assert tracker.find_position("RELIANCE", "ORB") is None

    @pytest.mark.asyncio
    async def test_cancel_failure_on_a_live_stop_does_not_flag_a_fill(self):
        """A cancel that failed for another reason leaves the exit path unchanged."""
        h = harness(
            fetch_orderbook=AsyncMock(return_value=[]),
            cancel_order=AsyncMock(return_value=False),
            fetch_order_status=AsyncMock(return_value="trigger pending"),
        )
        pos = _position(sl_order_id="SL1")
        with h.stack:
            assert await _cancel_sl_before_exit(pos) is False
        assert pos.stop_filled_in_cancel is False

    @pytest.mark.asyncio
    async def test_cancel_failure_on_a_filled_stop_flags_the_fill(self):
        h = harness(
            fetch_orderbook=AsyncMock(return_value=[]),
            cancel_order=AsyncMock(return_value=False),
            fetch_order_status=AsyncMock(return_value="partially filled"),
        )
        pos = _position(sl_order_id="SL1")
        with h.stack:
            await _cancel_sl_before_exit(pos)
        assert pos.stop_filled_in_cancel is True

    @pytest.mark.asyncio
    async def test_unreadable_status_on_a_failed_cancel_checks_the_broker_first(self):
        """Cancel failed and the status read failed too: the stop may have sold. A flat broker
        position means no exit order (it would be a naked short)."""
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(
            tracker=tracker,
            fetch_orderbook=AsyncMock(return_value=[]),
            cancel_order=AsyncMock(return_value=False),
            fetch_order_status=AsyncMock(return_value=""),
            fetch_open_position=AsyncMock(return_value=0),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreadable_status_with_the_position_still_held_still_exits(self):
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(
            tracker=tracker,
            fetch_orderbook=AsyncMock(return_value=[]),
            cancel_order=AsyncMock(return_value=False),
            fetch_order_status=AsyncMock(return_value=""),
            fetch_open_position=AsyncMock(return_value=50),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        h.send_order.assert_awaited_once()
