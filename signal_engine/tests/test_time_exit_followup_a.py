"""Follow-up to the time exit's hard deadline (Option A): no order after 14:54:30, no resend of an
uncertain exit, an honest and bounded stop repair, the strategy's own quantity, and a cancel
recorded only when the broker accepted it.

Findings of the review of 04f5d8320, each pinned here:
R1  _settle_prior_uncertain_exit read the order book for up to ~61 s before the first send.
R2  _reconcile_exit after a timed-out first attempt read the book for ~61 s and watched 120 s.
R3  the cancelled stop was "handed to the poll", which is paused during the pass; the alert
    promised a stop "within seconds".
R4a the final state read used the NET position (all strategies), not the strategy's own.
R4b a cancel was recorded before the call, so one that lost the race to a fill read "cancelled".

Clocks are fake: World moves the wall clock and the monotonic clock together; every broker call
charges its worst-case cost to it.
"""

import dataclasses
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import main as main_mod
from signal_engine import order_reconcile
from signal_engine import tracker as tracker_mod
from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.test_time_exit_hard_deadline import (
    EXPECTED_HEALTHY_SEQUENCE,
    STRAT,
    Broker,
    Harness,
    World,
    _row,
    _tracker,
)
from signal_engine.tests.tracker_fixtures import _make_position
from signal_engine.tracker import PositionTracker

IST = tracker_mod.IST
ORDER_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
def _clean_uncertain():
    order_reconcile._uncertain.clear()
    yield
    order_reconcile._uncertain.clear()


# ---------------------------------------------------------------------------------------------
# main._send_exit_with_retries, called directly (the order book and send_order are fakes)
# ---------------------------------------------------------------------------------------------


class MainRig:
    """Fake send_order and order book for main._send_exit_with_retries, on a World's clock."""

    def __init__(self, world, results, *, read_cost=19.0, send_cost=ORDER_TIMEOUT, book=None):
        self.w = world
        self.results = list(results)
        self.read_cost = read_cost
        self.send_cost = send_cost
        self.book = [] if book is None else book
        self.log: list[tuple] = []  # ("read" | "send", wall time)

    async def _send(self, order):
        self.log.append(("send", self.w.now))
        self.w.advance(self.send_cost)
        return self.results.pop(0)

    async def _read(self):
        self.log.append(("read", self.w.now))
        self.w.advance(self.read_cost)
        return list(self.book)

    async def _sleep(self, seconds):
        self.w.advance(seconds)

    def kinds(self):
        return [k for k, _ in self.log]

    def patches(self):
        return [
            patch.object(main_mod, "send_order", self._send),
            patch.object(main_mod, "fetch_orderbook", self._read),
            patch.object(main_mod, "fetch_order_ids_today", lambda symbol: []),
            patch("asyncio.sleep", AsyncMock(side_effect=self._sleep)),
        ]

    async def run(self, pos, *, hard=None, order=None):
        """Run the real function. `hard` (monotonic) switches the time-exit contextvar on."""
        order = order or _order(pos)
        token = None
        if hard is not None:
            token = main_mod._exit_time_left.set(lambda: hard - self.w.t)
        from contextlib import ExitStack

        try:
            with ExitStack() as stack:
                for p in self.patches():
                    stack.enter_context(p)
                return await main_mod._send_exit_with_retries(order, pos)
        finally:
            if token is not None:
                main_mod._exit_time_left.reset(token)


def _order(pos, qty=None):
    return SimpleNamespace(
        symbol=pos.symbol, exchange=pos.exchange, quantity=qty or pos.quantity,
        action=SimpleNamespace(value="SELL" if pos.direction == Direction.LONG else "BUY"),
        strategy_tag=pos.strategy, product=pos.product,
    )


def _timeout():
    return TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")


def _ok(order_id="X1"):
    return TradeResult(status=OrderStatus.SUCCESS, order_id=order_id, message="ok")


def _mark_earlier_exit(pos, *, w):
    order_reconcile.mark_uncertain(order_reconcile.UncertainOrder(
        strategy=pos.strategy, symbol=pos.symbol, action="SELL", quantity=pos.quantity,
        since=w.now - timedelta(minutes=2), kind="EXIT", watch_until=None,
    ))


@pytest.mark.asyncio
class TestA_EarlierUncertainExitIsHeldWithoutAReadNearTheDeadline:
    """R1: a close starting at 14:54:05 for a leg with an earlier uncertain exit."""

    async def test_no_read_no_send_and_the_held_timeout_is_returned(self):
        w = World(14, 54, 5)
        pos = _make_position(symbol="PFC", strategy=STRAT)
        _mark_earlier_exit(pos, w=w)
        rig = MainRig(w, [_ok()])
        result = await rig.run(pos, hard=w.t + 25)  # 14:54:30
        assert rig.kinds() == []  # the settle step read nothing and nothing was sent
        assert result.status == OrderStatus.TIMEOUT
        assert order_reconcile.get_uncertain(STRAT, "PFC") is not None  # the hold stays
        assert w.now <= w.hard  # and nothing ran past 14:54:30

    async def test_without_the_time_exit_the_same_call_still_settles_and_sends(self):
        """(c) The settle read and the send are the old sequence for every other caller."""
        w = World(14, 54, 5)
        pos = _make_position(symbol="PFC", strategy=STRAT)
        _mark_earlier_exit(pos, w=w)
        rig = MainRig(w, [_ok()], read_cost=1.0)
        result = await rig.run(pos)
        assert rig.kinds() == ["read", "read", "read", "send"]  # entry_timeout_recovery_attempts
        assert result.status == OrderStatus.SUCCESS


@pytest.mark.asyncio
class TestB_FirstAttemptTimeoutUnderTheDeadline:
    """R2: the first attempt times out under the time exit."""

    async def test_marks_uncertain_and_does_not_reconcile_or_resend(self):
        w = World(14, 53, 0)
        pos = _make_position(symbol="PFC", strategy=STRAT)
        rig = MainRig(w, [_timeout(), _ok("X2")])
        before = w.now
        result = await rig.run(pos, hard=w.t + 90)
        assert rig.kinds() == ["send"]  # no book read (no _reconcile_exit), no second send
        assert result.status == OrderStatus.TIMEOUT
        held = order_reconcile.get_uncertain(STRAT, "PFC")
        assert held is not None and held.kind == "EXIT"
        assert (held.action, held.quantity) == ("SELL", pos.quantity)
        assert held.watch_until is not None and held.watch_until > datetime.now(IST)
        assert (w.now - before).total_seconds() == ORDER_TIMEOUT  # one send, nothing more

    async def test_an_in_flight_rejection_is_handled_the_same_way(self):
        w = World(14, 53, 0)
        pos = _make_position(symbol="PFC", strategy=STRAT)
        inflight = TradeResult(status=OrderStatus.REJECTED, message="duplicate order in flight", inflight=True)
        rig = MainRig(w, [inflight, _ok("X2")], send_cost=1.0)
        result = await rig.run(pos, hard=w.t + 90)
        assert rig.kinds() == ["send"]
        assert result.status == OrderStatus.TIMEOUT
        assert order_reconcile.get_uncertain(STRAT, "PFC").kind == "EXIT"

    async def test_the_leg_stays_tracked_and_a_late_fill_is_labelled_time(self):
        """Whole pass through the real function: one send, no read of the book from main, the
        leg stays tracked with time_exit_left_open, and the poll later labels the close TIME."""
        w = World(14, 50, 0)
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        rig = MainRig(w, [_timeout()], read_cost=0.0, send_cost=ORDER_TIMEOUT)
        original = main_mod._send_exit_with_retries
        with Harness(w, b, tracker) as h:
            async def real(order, pos):
                from contextlib import ExitStack
                with ExitStack() as stack:
                    for p in rig.patches():
                        stack.enter_context(p)
                    return await original(order, pos)

            h._send = real  # the class method is the mock's side_effect: rebind below
            main_patch = patch(
                "signal_engine.main._send_exit_with_retries", AsyncMock(side_effect=real)
            )
            with main_patch:
                await tracker.time_exit_all()
            pos = tracker.find_position("PFC", STRAT)
            assert pos is not None, "an unconfirmed close must not be booked"
            assert pos.time_exit_left_open is True
            assert rig.kinds() == ["send"]  # one send in the whole pass, main read no book
            assert b.count("send") == 0 and b.working() == []
            held = order_reconcile.get_uncertain(STRAT, "PFC")
            assert held is not None and held.kind == "EXIT"
            # the order lands late, tagged for this strategy: the close is a TIME exit
            stamp = (w.now + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            late = {
                "orderid": "LATE1", "symbol": "PFC", "action": "SELL", "quantity": pos.quantity,
                "order_status": "complete", "timestamp": stamp, "strategy": STRAT,
            }
            with patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=[late])):
                label = await tracker._closing_label(tracker._key("PFC", STRAT), pos, False)
        assert label == "TIME"


@pytest.mark.asyncio
class TestC_OtherExitsAreUnchanged:
    """Stop-loss, take-profit, trailing, no-progress and risk exits never set the contextvar."""

    async def test_a_timeout_is_reconciled_and_resent_as_before(self):
        w = World(14, 30, 0)
        pos = _make_position(symbol="PFC", strategy=STRAT)
        rig = MainRig(w, [_timeout(), _ok("X2")], read_cost=1.0, send_cost=1.0)
        with patch.object(main_mod, "_LATE_FILL_WATCH_SECONDS", 10.0):
            result = await rig.run(pos)
        kinds = rig.kinds()
        assert kinds[0] == "send" and kinds[-1] == "send"
        assert kinds.count("send") == 2  # the resend after the proven-absent copy
        assert kinds.count("read") >= 3  # the reconcile reads and the late-fill watch
        assert result.status == OrderStatus.SUCCESS and result.order_id == "X2"
        assert order_reconcile.get_uncertain(STRAT, "PFC") is None

    async def test_no_deadline_check_applies_to_a_late_clock(self):
        """Past 14:55 an ordinary exit still sends: only the time exit has a deadline."""
        w = World(14, 59, 0)
        pos = _make_position(symbol="PFC", strategy=STRAT)
        rig = MainRig(w, [_ok()])
        result = await rig.run(pos)
        assert rig.kinds() == ["send"] and result.status == OrderStatus.SUCCESS

    async def test_the_contextvar_is_not_left_set_after_a_pass(self):
        w = World(14, 50, 0)
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker):
            await tracker.time_exit_all()
        assert main_mod._exit_time_left.get() is None


@pytest.mark.asyncio
class TestD_NoSendThatCouldEndAfterTheDeadline:
    async def test_main_does_not_begin_a_send_with_less_than_order_timeout_left(self):
        w = World(14, 54, 12)  # 18 s to 14:54:30; the order timeout is 20 s
        pos = _make_position(symbol="PFC", strategy=STRAT)
        rig = MainRig(w, [_ok()])
        result = await rig.run(pos, hard=w.t + 18)
        assert rig.kinds() == []  # no send was begun
        assert result.status == OrderStatus.ERROR
        assert result.message == main_mod.DEADLINE_NOT_SENT

    async def test_the_time_exit_reports_the_leg_as_not_closed_in_time(self):
        """The close gate passed (25 s), the net read then used the time: the send is refused and
        the strategy is reported as not closed, with its stop looked after."""
        w = World(14, 53, 50)  # 40 s left; the first cancel start gate needs 90 s, so none is sent
        b = Broker(w, [], qty=50)
        tracker = _tracker(("PFC", STRAT))
        sends: list = []

        async def refuse(order, pos):
            sends.append(order)
            return TradeResult(status=OrderStatus.ERROR, message=main_mod.DEADLINE_NOT_SENT)

        with Harness(w, b, tracker, close=None) as h:
            with patch("signal_engine.main._send_exit_with_retries", AsyncMock(side_effect=refuse)):
                tracker._time_exit_hard_deadline = w.t + 40
                tracker._time_exit_deadline = w.t + 40
                pos = tracker.find_position("PFC", STRAT)
                await tracker._close_one_leg(tracker._key("PFC", STRAT), pos, STRAT, refuse)
        assert STRAT in tracker._deadline_unclosed
        assert h.alerts == []


# ---------------------------------------------------------------------------------------------
# the stop repair inside the pass (R3, R4a)
# ---------------------------------------------------------------------------------------------


def _with_place(h, b):
    """place_sl_order that really adds a working stop to the fake book and logs the call."""
    async def place(**kwargs):
        b.log.append(("place", (kwargs["symbol"], kwargs["quantity"])))
        b.w.advance(2.0)
        b.rows["NEWSTOP"] = _row("NEWSTOP", kwargs["strategy_tag"], kwargs["symbol"])
        return TradeResult(status=OrderStatus.SUCCESS, order_id="NEWSTOP", message="ok")

    h.place.side_effect = place


def _stopless_pass_broker(w):
    """The cancel is accepted, then the book is unreadable until 80 s later: the confirmation
    gives up and the final read finds the position open and no stop."""
    b = Broker(w, [_row("SL1")], qty=50, dead=lambda wd: 0 <= b.since_cancel() < 80)
    return b


@pytest.mark.asyncio
class TestE_StopPlacedDirectly:
    async def test_accepted_cancel_then_budget_gone_places_and_confirms_the_stop(self):
        w = World(14, 52, 20)  # 130 s to 14:54:30
        b = _stopless_pass_broker(w)
        tracker = _tracker(("PFC", STRAT))
        pos = tracker.find_position("PFC", STRAT)
        sl, qty = pos.sl, pos.quantity
        with Harness(w, b, tracker) as h:
            _with_place(h, b)
            await tracker.time_exit_all()
        assert b.count("send") == 0  # no exit order of any kind
        assert h.place.await_count == 1
        kwargs = h.place.await_args.kwargs
        assert (kwargs["symbol"], kwargs["quantity"], kwargs["sl_price"]) == ("PFC", qty, sl)
        assert (kwargs["strategy_tag"], kwargs["direction"]) == (STRAT, Direction.LONG)
        assert pos.sl_order_id == "NEWSTOP"
        # confirmed by an order book read made after the placement
        idx = [k for k, _ in b.log]
        assert "orderbook" in idx[idx.index("place"):]
        (alert,) = h.skip_alerts(STRAT)
        assert "placed" in alert and "confirmed" in alert and "NEWSTOP" in alert
        assert "within seconds" not in alert and "14:55" in alert
        assert w.now <= w.hard

    async def test_placement_refused_says_no_stop_could_be_confirmed(self):
        w = World(14, 52, 20)
        b = _stopless_pass_broker(w)
        tracker = _tracker(("PFC", STRAT))
        pos = tracker.find_position("PFC", STRAT)
        refused = TradeResult(status=OrderStatus.ERROR, message="rejected")
        with Harness(w, b, tracker, place_result=refused) as h:
            await tracker.time_exit_all()
        assert h.place.await_count == 1
        (alert,) = h.skip_alerts(STRAT)
        assert "could not be confirmed" in alert and "14:55" in alert
        assert "placed and confirmed" not in alert and "within seconds" not in alert
        assert pos.sl_order_id == ""
        assert b.count("send") == 0

    async def test_accepted_but_not_on_the_book_is_not_claimed_as_confirmed(self):
        w = World(14, 52, 20)
        b = _stopless_pass_broker(w)  # place returns an id, the fake book never shows it
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        (alert,) = h.skip_alerts(STRAT)
        assert "could not be confirmed" in alert and "placed and confirmed" not in alert

    async def test_no_time_left_makes_no_call_and_says_so(self):
        w = World(14, 52, 50)  # the final read ends with about 32 s left: below the 39 s needed
        b = Broker(w, [_row("SL1")], qty=50, dead=lambda wd: 0 <= b.since_cancel() < 62)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert h.place.await_count == 0
        (alert,) = h.skip_alerts(STRAT)
        assert "no time" in alert and "14:55" in alert
        assert "within seconds" not in alert and "placed and confirmed" not in alert

    async def test_bracket_enabled_off_places_nothing_and_says_so(self):
        from signal_engine import config as config_mod

        w = World(14, 52, 20)
        b = _stopless_pass_broker(w)
        tracker = _tracker(("PFC", STRAT))
        off = dataclasses.replace(config_mod.settings, bracket_enabled=False)
        with patch("signal_engine.config.settings", off), Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert h.place.await_count == 0
        (alert,) = h.skip_alerts(STRAT)
        assert "switched off" in alert and "14:55" in alert
        assert "placed and confirmed" not in alert and "within seconds" not in alert


def _left_open_rig(*legs, hard_left=100.0, now=(14, 53, 0), net=None, accepted=None, rows=()):
    """A tracker in the middle of a pass, for _left_open_state: (world, broker, tracker)."""
    w = World(*now)
    b = Broker(w, list(rows), qty=net if net is not None else 0)
    tracker = PositionTracker(_tracker_engine())
    for symbol, strategy, direction, qty, sl_id in legs:
        pos = _make_position(
            symbol=symbol, strategy=strategy, product="MIS", direction=direction, quantity=qty,
            sl_order_id=sl_id, sl=100.0 if direction == Direction.LONG else 110.0,
        )
        tracker.register(pos)
    tracker._time_exit_hard_deadline = w.t + hard_left
    tracker._time_exit_deadline = w.t + hard_left
    for strategy, ids in (accepted or {}).items():
        for oid in ids:
            tracker._cancel_sent.setdefault(strategy, {})[oid] = _row(oid, strategy)
            tracker._cancel_accepted.setdefault(strategy, {})[oid] = _row(oid, strategy)
    return w, b, tracker


def _tracker_engine():
    from signal_engine.tests.tracker_fixtures import _make_engine

    return _make_engine()


@pytest.mark.asyncio
class TestIJ_NoSecondStopNoStopForAFlatLeg:
    async def test_a_working_stop_means_no_second_stop(self):
        w, b, tracker = _left_open_rig(
            ("PFC", "A", Direction.LONG, 10, "SL1"), net=10, rows=[_row("SL1", "A", "PFC")],
            accepted={"A": ["OLD"]},
        )
        with Harness(w, b, tracker) as h:
            text = await tracker._left_open_state("A", {"PFC"})
        assert h.place.await_count == 0 and "still working" in text

    async def test_a_tracked_stop_id_that_this_pass_did_not_cancel_is_never_replaced(self):
        w, b, tracker = _left_open_rig(("PFC", "A", Direction.LONG, 10, "SL1"), net=10)
        with Harness(w, b, tracker) as h:
            text = await tracker._left_open_state("A", {"PFC"})
        assert h.place.await_count == 0 and "no working stop" in text
        assert tracker.find_position("PFC", "A").sl_order_id == "SL1"

    async def test_flat_own_position_gets_no_stop_and_no_exit(self):
        """Another strategy holds the same symbol: the net is non-zero, this strategy's is zero."""
        w, b, tracker = _left_open_rig(
            ("PFC", "A", Direction.LONG, 10, "SL1"), ("PFC", "B", Direction.LONG, 10, "SLB"),
            net=10, accepted={"A": ["SL1"]},
        )
        with Harness(w, b, tracker) as h:
            text = await tracker._left_open_state("A", {"PFC"})
        assert h.place.await_count == 0 and b.count("send") == 0
        assert "closed" in text and "new stop" not in text


@pytest.mark.asyncio
class TestK_SharedSymbolOppositePositionsStaySeparate:
    async def test_the_pass_sends_exactly_one_order_per_strategy_for_its_own_quantity(self):
        w = World(14, 45, 5)
        rows = [_row("A-STOP", "A", "PFC"), _row("B-STOP", "B", "PFC")]
        rows[1]["action"] = "BUY"
        b = Broker(w, rows, qty={"PFC": 0})  # long 10 and short 10 net to zero at the broker
        tracker = PositionTracker(_tracker_engine())
        for strategy, direction, sl_id in (("A", Direction.LONG, "A-STOP"), ("B", Direction.SHORT, "B-STOP")):
            tracker.register(_make_position(
                symbol="PFC", strategy=strategy, product="MIS", direction=direction, quantity=10,
                sl_order_id=sl_id, sl=100.0 if direction == Direction.LONG else 110.0,
            ))
        orders: list[tuple] = []

        async def send(order, pos):
            orders.append((pos.strategy, order.action.value, order.quantity))
            b.qty["PFC"] += order.quantity if order.action.value == "BUY" else -order.quantity
            return TradeResult(status=OrderStatus.SUCCESS, order_id=f"X{len(orders)}", message="ok")

        with Harness(w, b, tracker) as h, patch(
            "signal_engine.main._send_exit_with_retries", AsyncMock(side_effect=send)
        ):
            await tracker.time_exit_all()
        assert orders == [("A", "SELL", 10), ("B", "BUY", 10)]
        assert b.qty["PFC"] == 0
        assert h.skip_alerts() == []

    async def test_each_strategy_gets_a_stop_for_its_own_quantity_not_the_net(self):
        w, b, tracker = _left_open_rig(
            ("PFC", "A", Direction.LONG, 10, "SLA"), ("PFC", "B", Direction.SHORT, 10, "SLB"),
            net=0, accepted={"A": ["SLA"], "B": ["SLB"]}, hard_left=200.0,
        )
        calls: list[dict] = []
        with Harness(w, b, tracker) as h:
            async def place(**kwargs):
                calls.append(kwargs)
                b.rows[f"N{len(calls)}"] = _row(f"N{len(calls)}", kwargs["strategy_tag"], "PFC")
                return TradeResult(status=OrderStatus.SUCCESS, order_id=f"N{len(calls)}", message="ok")

            h.place.side_effect = place
            text_a = await tracker._left_open_state("A", {"PFC"})
            text_b = await tracker._left_open_state("B", {"PFC"})
        assert [(c["strategy_tag"], c["direction"], c["quantity"]) for c in calls] == [
            ("A", Direction.LONG, 10), ("B", Direction.SHORT, 10),
        ]
        assert "placed and confirmed" in text_a and "placed and confirmed" in text_b
        assert b.count("send") == 0


@pytest.mark.asyncio
class TestL_CancelRacingAFill:
    async def test_a_refused_cancel_on_a_completed_order_is_not_recorded_as_cancelled(self):
        w = World(14, 50, 0)
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        tracker._time_exit_hard_deadline = w.t + 300
        tracker._time_exit_deadline = w.t + 300

        async def racing_cancel(order_id, strategy):
            b.rows[order_id]["order_status"] = "complete"  # the fill won
            return False

        with Harness(w, b, tracker):
            with patch("signal_engine.tracker.cancel_order", AsyncMock(side_effect=racing_cancel)):
                complete, filled = await tracker._cancel_working_by_id([_row("SL1")], {STRAT})
        assert (complete, filled) == (True, ["SL1"])
        assert tracker._cancel_accepted == {}
        assert "SL1" in tracker._cancel_sent[STRAT]  # the deadline reserve still applies

    async def test_the_report_does_not_say_cancelled_when_the_fill_won(self):
        w = World(14, 52, 50)
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))

        async def racing_cancel(order_id, strategy):
            b.log.append(("cancel", order_id))
            w.advance(55.0)
            b.rows[order_id]["order_status"] = "complete"  # the stop filled: flat, not cancelled
            b.qty["*"] = 0
            return False

        with Harness(w, b, tracker) as h:
            with patch("signal_engine.tracker.cancel_order", AsyncMock(side_effect=racing_cancel)):
                await tracker.time_exit_all()
        (alert,) = h.skip_alerts(STRAT)
        assert "closed" in alert and "cancelled" not in alert
        assert h.place.await_count == 0 and b.count("send") == 0

    async def test_an_accepted_cancel_is_recorded_as_cancelled(self):
        w = World(14, 52, 50)
        b = Broker(w, [_row("SL1")], qty=0, cancel_cost=55.0)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        (alert,) = h.skip_alerts(STRAT)
        assert "closed" in alert and "cancelled" in alert


# ---------------------------------------------------------------------------------------------
# restart / catch-up (M), booking reads (N), healthy path (P)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestM_PassStartingAtOrAfterTheDeadline:
    @pytest.mark.parametrize("start", [(14, 54, 30), (14, 54, 31), (14, 54, 59)])
    async def test_nothing_is_read_cancelled_or_sent_and_one_honest_alert_goes_out(self, start):
        w = World(*start)
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert b.log == [] and h.sleeps == []
        (alert,) = h.skip_alerts(STRAT)
        assert "14:55 square-off closes" in alert
        assert "by hand before" not in alert
        assert tracker.tracked_count == 1

    async def test_after_the_14_55_failsafe_the_alert_says_it_has_run(self):
        w = World(14, 56, 0)  # an engine restart, after the failsafe
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert b.log == []
        (alert,) = h.skip_alerts(STRAT)
        assert "has already run" in alert and "closes any open position" not in alert

    async def test_after_the_market_close_there_is_no_alert_only_clean_up(self):
        w = World(15, 31, 0)
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT), ("TCS", "B"))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert b.log == []
        assert [a for a in h.alerts if a.startswith("TIME EXIT")] == []
        assert tracker._time_exit_active is False

    async def test_the_90_s_start_gate_alert_says_the_failsafe_closes_it(self):
        w = World(14, 53, 30)  # 60 s to the deadline: below the 90 s the first cancel needs
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert b.count("cancel") == 0 and b.count("send") == 0
        (alert,) = h.skip_alerts(STRAT)
        assert "14:55 square-off closes" in alert and "by hand before" not in alert


@pytest.mark.asyncio
class TestN_BookingReadsAreGatedOneByOne:
    async def test_after_the_deadline_mid_booking_the_remaining_reads_are_skipped(self):
        w = World(14, 54, 0)
        b = Broker(w, [], qty={"PFC": 0, "TCS": 0})
        tracker = PositionTracker(_tracker_engine())
        for symbol, strategy in (("PFC", "A"), ("TCS", "B")):
            tracker.register(_make_position(symbol=symbol, strategy=strategy, product="MIS"))
            tracker._square_off_orders[tracker._key(symbol, strategy)] = f"X-{symbol}"
        reads: list[tuple] = []

        def timed(name, cost, value):
            async def read(*args, **kwargs):
                reads.append((name, w.t, cost))
                w.advance(cost)
                return value
            return read

        hard = w.t + 70.0
        tracker._time_exit_hard_deadline = hard
        tracker._time_exit_deadline = hard
        tracker._time_exit_started = w.now
        with Harness(w, b, tracker) as h, patch.object(
            PositionTracker, "_save_close_row", lambda *a, **k: None
        ), patch("signal_engine.tracker.fetch_realised_pnl", timed("pnl", 5.0, 0.0)), patch(
            "signal_engine.tracker.fetch_positionbook", timed("positionbook", 19.0, [])
        ), patch("signal_engine.tracker.fetch_orderbook", timed("orderbook", 19.0, [])), patch(
            "signal_engine.tracker.fetch_order_fill_price", timed("fill", 17.0, 100.0)
        ):
            from signal_engine.config import settings

            await tracker._book_time_exit_trades(
                dict(tracker._positions), settings, w.now, frozenset()
            )
        for name, start, cost in reads:
            assert start + cost <= hard, f"{name} read began too late: ends {start + cost - hard:+.0f} s"
        assert [n for n, _, _ in reads] == ["pnl", "positionbook", "orderbook", "fill"]
        assert tracker.tracked_count == 1  # the second leg is left to the poll
        assert tracker.find_position("TCS", "B") is not None
        # a WARNING line since the Telegram alert was removed
        assert any("TCS" in a and "next position check" in a for a in h.logged)


@pytest.mark.asyncio
class TestP_HealthyPathUnchanged:
    async def test_call_sequence_with_the_real_send_function_matches_the_recorded_one(self):
        """The 14:45 pass on a healthy book, through the REAL _send_exit_with_retries (not the
        harness mock): the broker call sequence is the one recorded before the change, and the
        main module read no order book."""
        w = World(14, 45, 5)
        rows = [_row("A-STOP", "A", "PFC"), _row("A-TP", "A", "PFC", status="open", pricetype="LIMIT"),
                _row("B-STOP", "B", "TCS")]
        b = Broker(w, rows, qty={"PFC": 50, "TCS": 50})
        tracker = _tracker(("PFC", "A"), ("TCS", "B"))
        rig = MainRig(w, [_ok("X1"), _ok("X2")], read_cost=0.0, send_cost=1.0)
        original = main_mod._send_exit_with_retries
        sent: list[tuple] = []

        async def real(order, pos):
            from contextlib import ExitStack
            with ExitStack() as stack:
                for p in rig.patches():
                    stack.enter_context(p)
                result = await original(order, pos)
            sent.append((order.symbol, order.quantity))
            b.qty[order.symbol] = 0
            b.log.append(("send", order.symbol))
            return result

        with Harness(w, b, tracker) as h, patch(
            "signal_engine.main._send_exit_with_retries", AsyncMock(side_effect=real)
        ):
            await tracker.time_exit_all()
        seq = [(k, d if k != "orderbook" else None) for k, d in b.log]
        assert seq == EXPECTED_HEALTHY_SEQUENCE
        assert h.sleeps == [3]
        assert sent == [("PFC", 50), ("TCS", 50)]
        assert rig.kinds() == ["send", "send"]  # main: two sends, no book read
        assert h.skip_alerts() == []


class TestConstantsFollowTheSettings:
    def test_worst_case_costs_are_derived_from_api_timeout_and_order_timeout(self):
        from signal_engine.config import settings

        t, o = settings.api_timeout, settings.order_timeout
        assert tracker_mod._API_CALL_WORST_S == t
        assert tracker_mod._ORDERBOOK_READ_WORST_S == 3 * t + 4.0
        assert tracker_mod._CANCEL_CALL_WORST_S == 2 * t
        assert tracker_mod._FILL_READ_WORST_S == 3 * t + 2.0
        assert tracker_mod._CLOSE_BEGIN_MIN_S == t + o
        assert tracker_mod._STOP_PLACE_MIN_S == o + tracker_mod._ORDERBOOK_READ_WORST_S
