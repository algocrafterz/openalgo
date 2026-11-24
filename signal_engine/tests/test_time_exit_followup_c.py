"""Final follow-up to the time exit's hard deadline: four review findings.

F1  _verify_positions_closed: the early return at the deadline left the held legs out of the set
    of legs still open, so a leg whose exit order might still fill was booked as a closed TIME
    exit and unregistered. The held legs are now counted as left open, as in every other return.
F2  The final cancel sweep was skipped for a whole strategy when one leg was held, so its other
    flat legs kept stops or LIMIT targets working until 14:55. The sweep now skips only the held
    leg's own orders and still alerts when another leg's order cannot be confirmed cancelled.
F3  A stop key the time exit marked unconfirmed could be cleared by the duplicate-stop resolver
    (0 or 1 live stop on the book) before anything scanned for a stop that landed late, and the
    next placement then sent a second stop. The mark now stays until place_sl_order scanned.
F4  Comment only.

Clocks are fake (World moves the wall and the monotonic clock together); every broker call is a
fake. No network, no real order.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import executor, order_reconcile
from signal_engine import tracker as tracker_mod
from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.test_time_exit_followup_a import _left_open_rig
from signal_engine.tests.test_time_exit_hard_deadline import (
    STRAT,
    Broker,
    Harness,
    World,
    _row,
    _tracker,
)
from signal_engine.tracker import PositionTracker

IST = tracker_mod.IST
_REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _clean_state():
    def reset():
        order_reconcile._uncertain.clear()
        executor._UNCONFIRMED_SL.clear()
        executor._PENDING_DUPLICATE_STOPS.clear()
        executor._EPISODE_START.clear()
        executor._LAST_UNKNOWN_SEND.clear()
        getattr(executor, "_SCAN_BEFORE_CLEAR", set()).clear()

    reset()
    yield
    reset()


def _mark_held(w, strategy, symbol, qty=50):
    order_reconcile.mark_uncertain(order_reconcile.UncertainOrder(
        strategy=strategy, symbol=symbol, action="SELL", quantity=qty,
        since=w.now, kind="EXIT", watch_until=w.now + timedelta(seconds=900),
    ))


# ---------------------------------------------------------------------------------------------
# F1: the held leg stays left open when the verify loop runs out of time
# ---------------------------------------------------------------------------------------------


def _verify_rig(*, held: bool, start=(14, 53, 52)):
    """A strategy with PFC (exit sent, result unknown when `held`) and INFY (its close never
    shows a fill, so it is still open). The verify loop is entered directly, as the pass does."""
    w = World(*start)
    b = Broker(w, [_row("SL1", STRAT, "PFC"), _row("SL2", STRAT, "INFY")],
               qty={"PFC": 50, "INFY": 50})
    tracker = _tracker(("PFC", STRAT), ("INFY", STRAT))
    return w, b, tracker


async def _run_verify(w, b, tracker, *, held: bool, spend):
    """Run _verify_positions_closed with a cancel already sent for the strategy. `spend` seconds
    pass during the first attempt's reads, so attempt 2 starts in the 17-24 s band."""
    notes = []
    with Harness(w, b, tracker) as h:
        tracker._time_exit_active = True
        tracker._time_exit_hard_deadline = w.t + 40.0  # 14:54:32 on this clock; band set by spend
        tracker._time_exit_deadline = w.t + 300.0
        tracker._cancel_sent = {STRAT: {"SL1": {}}}
        tracker._cancel_accepted = {STRAT: {"SL1": {}}}
        tracker._skip_alerted = set()
        tracker._square_off_orders = {}
        if held:
            _mark_held(w, STRAT, "PFC")
        real_net = b.net

        async def net(symbol, strategy, exchange, product):
            out = await real_net(symbol, strategy, exchange, product)
            if symbol == "INFY" and not notes:
                w.advance(spend)  # the first attempt's reads take this long
                notes.append(tracker._time_exit_hard_deadline - w.t)
            return out

        with patch("signal_engine.tracker.fetch_net_position", AsyncMock(side_effect=net)):
            positions = dict(tracker._positions)
            result = await tracker._verify_positions_closed(positions, set())
            notes.append(tracker._past_hard_deadline())
            notes.append(tracker._time_exit_hard_deadline - w.t)
    return h, result, positions, notes


@pytest.mark.asyncio
class TestF1_HeldLegAtTheDeadline:
    async def test_the_held_leg_counts_as_left_open_and_is_not_booked_as_closed(self):
        w, b, tracker = _verify_rig(held=True)
        h, result, positions, notes = await _run_verify(w, b, tracker, held=True, spend=14.0)
        past, left_at_attempt_2 = notes[-2], notes[-1]
        assert past is False, "the scenario must be before the hard deadline"
        assert 17.0 <= left_at_attempt_2 <= 24.0, left_at_attempt_2
        pfc_key = next(k for k, p in positions.items() if p.symbol == "PFC")
        infy_key = next(k for k, p in positions.items() if p.symbol == "INFY")
        assert pfc_key in result, f"the held leg was left out of the open legs: {result}"
        assert infy_key in result

    async def test_through_the_whole_pass_the_held_leg_is_not_booked_time_and_stays_tracked(self):
        w, b, tracker = _verify_rig(held=True, start=(14, 50, 0))
        booked: list = []
        real_book = PositionTracker._book_one_time_exit

        async def spy(self, key, pos, pnl, _settings, **kw):
            booked.append((pos.symbol, kw.get("confirmed_closed")))
            return await real_book(self, key, pos, pnl, _settings, **kw)

        pfc = tracker.find_position("PFC", STRAT)
        infy = tracker.find_position("INFY", STRAT)

        async def sent(order, pos):
            w.advance(1.0)
            if pos.symbol == "PFC":
                _mark_held(w, STRAT, "PFC")
                return TradeResult(status=OrderStatus.TIMEOUT, message="UNCERTAIN: sent, outcome unknown")
            return TradeResult(status=OrderStatus.ERROR, message="rejected")  # INFY stays open

        real_net = b.net
        reads: list = []

        async def net(symbol, strategy, exchange, product):
            out = await real_net(symbol, strategy, exchange, product)
            if symbol == "INFY":
                reads.append(1)
            if symbol == "INFY" and len(reads) == 2:  # the verify loop's read, not the close sizing
                w.advance((w.at(14, 54, 8) - w.now).total_seconds())  # 22 s before the hard deadline
            return out

        with Harness(w, b, tracker), patch(
            "signal_engine.main._send_exit_with_retries", AsyncMock(side_effect=sent)
        ), patch("signal_engine.tracker.fetch_net_position", AsyncMock(side_effect=net)), patch.object(
            PositionTracker, "_book_one_time_exit", spy
        ):
            await tracker.time_exit_all()
        assert len(reads) >= 2, "the scenario never reached the verify loop read"
        assert True not in [c for s, c in booked if s == "PFC"], f"the held leg was booked as a TIME close: {booked}"
        assert tracker.find_position("PFC", STRAT) is pfc, "the held leg was unregistered"
        assert pfc.time_exit_left_open is True
        assert tracker.find_position("INFY", STRAT) is infy, "the unheld open leg stays tracked too"

    async def test_control_without_a_held_leg_both_legs_are_left_open_as_before(self):
        w, b, tracker = _verify_rig(held=False)
        h, result, positions, notes = await _run_verify(w, b, tracker, held=False, spend=14.0)
        assert set(result) == set(positions), result
        assert any("closes not verified before the hard deadline" in a for a in h.alerts), h.alerts


# ---------------------------------------------------------------------------------------------
# F2: the final sweep still covers the strategy's other legs
# ---------------------------------------------------------------------------------------------


def _sweep_rig(*, y_cancel_works):
    """STRAT holds PFC (exit sent, result unknown: working MARKET EXIT1) and has a flat INFY leg
    whose stop SL2 is still working on the book after the first cancel failed."""
    w = World(14, 50, 0)
    rows = [
        {"orderid": "EXIT1", "symbol": "PFC", "action": "SELL", "pricetype": "MARKET",
         "product": "MIS", "order_status": "open", "strategy": STRAT},
        _row("SL2", STRAT, "INFY"),
    ]
    b = Broker(w, rows, qty={"PFC": 50, "INFY": 0})
    real_cancel = b.cancel

    async def cancel(order_id, strategy):
        if order_id == "SL2" and not y_cancel_works:
            b.log.append(("cancel", order_id))
            return False
        return await real_cancel(order_id, strategy)

    b.cancel = cancel
    tracker = _tracker(("PFC", STRAT), ("INFY", STRAT))
    _mark_held(w, STRAT, "PFC")
    return w, b, tracker


async def _run_pass(w, b, tracker):
    keys = dict(tracker._positions)
    with Harness(w, b, tracker) as h:
        tracker._time_exit_active = True
        tracker._time_exit_hard_deadline = w.t + 600.0
        tracker._time_exit_deadline = w.t + 600.0
        tracker._cancel_sent = {STRAT: {"SL2": {}}}
        tracker._cancel_accepted = {}
        held_key = next(k for k, p in keys.items() if p.symbol == "PFC")
        with patch.object(PositionTracker, "_square_off_strategies",
                          AsyncMock(return_value=({"INFY"}, set()))), patch.object(
            PositionTracker, "_verify_positions_closed", AsyncMock(return_value={held_key})
        ), patch.object(PositionTracker, "_book_time_exit_trades", AsyncMock()):
            await tracker.time_exit_all()
    return h


@pytest.mark.asyncio
class TestF2_SweepKeepsTheOtherLegs:
    async def test_the_flat_legs_order_is_cancelled_and_the_held_exit_order_never(self):
        w, b, tracker = _sweep_rig(y_cancel_works=True)
        h = await _run_pass(w, b, tracker)
        cancels = [d for k, d in b.log if k == "cancel"]
        assert "SL2" in cancels, f"the flat leg's stop was left working: {cancels}"
        assert "EXIT1" not in cancels, f"the held exit order was cancelled: {cancels}"
        assert b.rows["SL2"]["order_status"] == "cancelled"
        assert b.rows["EXIT1"]["order_status"] == "open"
        assert not any("not confirmed cancelled" in a for a in h.alerts), h.alerts

    async def test_an_unconfirmed_cancel_of_the_flat_leg_still_alerts_and_names_only_its_order(self):
        w, b, tracker = _sweep_rig(y_cancel_works=False)
        h = await _run_pass(w, b, tracker)
        cancels = [d for k, d in b.log if k == "cancel"]
        assert "EXIT1" not in cancels, cancels
        alerts = [a for a in h.alerts if "not confirmed cancelled" in a]
        assert len(alerts) == 1, h.alerts
        assert "INFY" in alerts[0] and "EXIT1" not in alerts[0] and "PFC" not in alerts[0], alerts[0]
        assert b.rows["EXIT1"]["order_status"] == "open"

    async def test_control_when_every_leg_is_held_nothing_is_swept(self):
        w, b, tracker = _sweep_rig(y_cancel_works=True)
        _mark_held(w, STRAT, "INFY", qty=1)
        h = await _run_pass(w, b, tracker)
        cancels = [d for k, d in b.log if k == "cancel"]
        assert cancels == [], cancels
        assert not any("not confirmed cancelled" in a for a in h.alerts), h.alerts
        assert b.rows["SL2"]["order_status"] == "trigger pending"


# ---------------------------------------------------------------------------------------------
# F3: a time-exit-marked stop key is scanned for before it can be cleared or a stop re-sent
# ---------------------------------------------------------------------------------------------

KEY = (STRAT, "PFC", "SELL")


def _stop(oid, status="trigger pending", **extra):
    row = {
        "orderid": oid, "symbol": "PFC", "action": "SELL", "pricetype": "SL-M", "product": "MIS",
        "order_status": status, "strategy": STRAT, "quantity": 10, "trigger_price": 100.0,
    }
    row.update(extra)
    return row


def _mark(started=None):
    tracker_mod.PositionTracker._mark_stop_unconfirmed(KEY, started or World().now)


class _Book:
    """A mutable order book behind api_client.fetch_orderbook; None makes it unreadable."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.readable = True
        self.reads = 0

    async def __call__(self):
        self.reads += 1
        return [dict(r) for r in self.rows] if self.readable else None


async def _place(send, book, strategy=STRAT):
    with patch("signal_engine.executor.send_order", send), patch(
        "signal_engine.api_client.fetch_orderbook", book
    ), patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=True)), patch.object(
        executor.asyncio, "sleep", AsyncMock()
    ):
        return await executor.place_sl_order(
            symbol="PFC", exchange="NSE", direction=Direction.LONG, quantity=10,
            sl_price=100.0, product="MIS", strategy_tag=strategy,
        )


def _send_ok():
    return AsyncMock(return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="NEW", message="ok"))


async def _resolve(book):
    with patch("signal_engine.api_client.fetch_orderbook", book), patch(
        "signal_engine.api_client.cancel_order", AsyncMock(return_value=True)
    ), patch.object(executor.asyncio, "sleep", AsyncMock()):
        return await executor.resolve_pending_duplicate_stops(force=True)


@pytest.mark.asyncio
class TestF3_ScanBeforeClear:
    async def test_a_empty_book_does_not_clear_the_mark_and_a_late_stop_is_adopted_not_doubled(self):
        _mark()
        book = _Book([])
        await _resolve(book)  # the poll's reconcile: 0 live stops
        assert KEY in executor._UNCONFIRMED_SL, "the resolver cleared the mark before any scan"
        book.rows = [_stop("LANDED")]  # the stop lands after that read
        send = _send_ok()
        result = await _place(send, book)
        send.assert_not_awaited()
        assert result.order_id == "LANDED"
        assert KEY not in executor._UNCONFIRMED_SL and KEY not in executor._PENDING_DUPLICATE_STOPS

    async def test_exactly_one_live_stop_is_adopted_and_nothing_is_sent(self):
        _mark()
        book = _Book([_stop("ONE")])
        await _resolve(book)
        assert KEY in executor._UNCONFIRMED_SL
        send = _send_ok()
        result = await _place(send, book)
        send.assert_not_awaited()
        assert result.order_id == "ONE"

    async def test_an_unreadable_book_sends_nothing_and_keeps_the_mark(self):
        _mark()
        book = _Book([])
        await _resolve(book)
        book.readable = False
        send = _send_ok()
        result = await _place(send, book)
        send.assert_not_awaited()
        assert result.status == OrderStatus.TIMEOUT
        assert KEY in executor._UNCONFIRMED_SL

    async def test_an_empty_readable_book_inside_the_grace_holds_and_after_it_sends_once(self):
        _mark()
        book = _Book([])
        await _resolve(book)
        send = _send_ok()
        held = await _place(send, book)
        assert held.message == executor.HOLD_MESSAGE
        send.assert_not_awaited()
        executor._LAST_UNKNOWN_SEND[KEY] -= executor._RESEND_GRACE_S + 1
        result = await _place(send, book)
        assert send.await_count == 1 and result.order_id == "NEW"

    async def test_the_15_00_retry_of_a_flagged_leg_whose_stop_landed_late_places_one_stop(self):
        w, b, tracker = _left_open_rig(
            ("PFC", STRAT, Direction.LONG, 10, ""), net=10, accepted={}, now=(14, 54, 45),
        )
        pos = tracker.find_position("PFC", STRAT)
        pos.sl_order_id = ""
        pos.time_exit_left_open = True
        _mark(w.now)
        book = _Book([])
        await _resolve(book)  # the poll's resolver, same poll as the retry
        assert KEY in executor._UNCONFIRMED_SL
        book.rows = [_stop("LANDED")]
        send = _send_ok()
        w.now = w.at(15, 0, 5)
        from signal_engine.config import settings

        with patch("signal_engine.executor.send_order", send), patch(
            "signal_engine.api_client.fetch_orderbook", book
        ), patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=True)), patch.object(
            executor.asyncio, "sleep", AsyncMock()
        ), patch("signal_engine.tracker.place_sl_order", executor.place_sl_order), patch.object(
            tracker_mod, "_now_ist", w.wall, create=True
        ), patch.object(tracker_mod, "_monotonic", w.mono):
            for _ in range(2):
                pb = [{"symbol": "PFC", "exchange": "NSE", "product": "MIS", "quantity": 10, "ltp": 100.0}]
                await tracker._retry_missing_sl(tracker_mod._index_positionbook(pb), settings)
        send.assert_not_awaited()
        assert pos.sl_order_id == "LANDED"

    async def test_a_key_the_time_exit_did_not_mark_behaves_as_before(self):
        """Executor TIMEOUT path: flagged by place_sl_order itself, not _mark_stop_unconfirmed.
        With an empty readable book the resolver clears the flag, as it always did."""
        send = AsyncMock(return_value=TradeResult(status=OrderStatus.TIMEOUT, message="timed out"))
        book = _Book([])
        with patch.object(executor, "_RESEND_GRACE_S", 0.0):
            await _place(send, book)
        assert KEY in executor._UNCONFIRMED_SL
        assert getattr(executor, "_SCAN_BEFORE_CLEAR", set()) == set()
        sent = send.await_count
        done = await _resolve(book)
        assert done == [KEY]
        assert KEY not in executor._UNCONFIRMED_SL and KEY not in executor._PENDING_DUPLICATE_STOPS
        # no flag left: the next placement sends without a scan, exactly as before
        reads = book.reads
        ok = _send_ok()
        result = await _place(ok, book)
        assert ok.await_count == 1 and result.order_id == "NEW"
        assert book.reads == reads, "an unmarked key must not be scanned after the flag is cleared"
        assert sent >= 1
