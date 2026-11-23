"""The time exit has a hard wall-clock deadline (14:54:30 IST) and never leaves a cancelled stop
without protection.

Two review findings, both about one pass of PositionTracker.time_exit_all:

MAJOR A  The cancels were accepted, the 300 s budget then ran out before the close was
         confirmed, the strategy was skipped, and the alert said it "keeps its stop". The stop
         was gone. Now: a strategy's stops are cancelled only when enough time remains to
         confirm the cancel and send the close; after the first cancel the confirmation and the
         close run to the hard deadline; a strategy left open is read (stop status, net
         position) and the alert says what was read; a stop this pass cancelled on a position
         that is still open is handed to the missing-stop retry, which places it again.
MAJOR B  The deadline was start + 300 s and ignored the clock, so a catch-up pass after 14:50
         ran past the 14:55 failsafe square-off. Now: deadline = min(start + 300 s, 14:54:30
         IST), and no read, cancel, wait or close begins after it.
MINOR C  SQUAREOFF is a time exit for the counts and is not a win or a loss.

All clocks are fake: one World moves the wall clock and the monotonic clock together, and every
broker call charges its worst-case cost to it.
"""

import contextlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import db, notifier
from signal_engine import tracker as tracker_mod
from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker

IST = tracker_mod.IST
STRAT = "EVWAP-EARLY-PY"
READ_DEAD_S = 19.0  # api.timeout 5 s x 3 tries + two 2 s waits
HARD = (14, 54, 30)


class World:
    """One wall clock and one monotonic clock that always move together."""

    def __init__(self, hh=14, mm=50, ss=0):
        self.now = datetime(2026, 11, 24, hh, mm, ss, tzinfo=IST)
        self.t = 10_000.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.t += seconds

    def mono(self):
        return self.t

    def wall(self):
        return self.now

    @property
    def hard(self):
        return self.now.replace(hour=HARD[0], minute=HARD[1], second=HARD[2], microsecond=0)

    def at(self, hh, mm, ss=0):
        return self.now.replace(hour=hh, minute=mm, second=ss, microsecond=0)


def _row(oid, strategy=STRAT, symbol="PFC", status="trigger pending", pricetype="SL-M"):
    return {
        "orderid": oid, "symbol": symbol, "action": "SELL", "pricetype": pricetype,
        "product": "MIS", "order_status": status, "strategy": strategy,
    }


class Broker:
    """Order book and net positions with costed calls. `dead(world)` makes the book unreadable."""

    def __init__(self, world, rows, qty=50, dead=lambda w: False, cancel_cost=0.0,
                 cancel_works=True, net_dead=lambda w: False, read_cost=0.0):
        self.w = world
        self.rows = {r["orderid"]: dict(r) for r in rows}
        self.qty = qty if isinstance(qty, dict) else {"*": qty}
        self.dead = dead
        self.net_dead = net_dead
        self.cancel_cost = cancel_cost
        self.cancel_works = cancel_works
        self.read_cost = read_cost
        self.log: list[tuple] = []  # (kind, detail), in call order
        self.started = world.now
        self.cancelled_at = None

    def since_cancel(self):
        """Seconds since the first accepted cancel (-1 before it)."""
        return -1.0 if self.cancelled_at is None else (self.w.now - self.cancelled_at).total_seconds()

    def count(self, kind):
        return sum(1 for k, _ in self.log if k == kind)

    def qty_of(self, symbol):
        return self.qty.get(symbol, self.qty.get("*", 0))

    def working(self):
        return sorted(o for o, r in self.rows.items()
                      if r["order_status"] in ("trigger pending", "open", "pending"))

    async def orderbook(self):
        self.log.append(("orderbook", self.w.now))
        if self.dead(self.w):
            self.w.advance(READ_DEAD_S)
            return None
        self.w.advance(self.read_cost)
        return [dict(r) for r in self.rows.values()]

    async def cancel(self, order_id, strategy):
        self.log.append(("cancel", order_id))
        self.w.advance(self.cancel_cost)
        row = self.rows.get(order_id)
        if row is None or row["order_status"] not in ("trigger pending", "open", "pending"):
            return False
        if self.cancel_works:
            row["order_status"] = "cancelled"
            if self.cancelled_at is None:
                self.cancelled_at = self.w.now
        return True

    async def status(self, order_id, strategy):
        self.log.append(("status", order_id))
        row = self.rows.get(order_id)
        return row["order_status"] if row else ""

    async def net(self, symbol, strategy, exchange, product):
        self.log.append(("net", symbol))
        if self.net_dead(self.w):
            self.w.advance(5.0)
            return None
        return self.qty_of(symbol)

    async def positionbook(self):
        self.log.append(("positionbook", None))
        return [{"symbol": s, "exchange": "NSE", "product": "MIS", "quantity": q, "ltp": 100.0}
                for s, q in self.qty.items() if s != "*"] or [
                {"symbol": "PFC", "exchange": "NSE", "product": "MIS",
                 "quantity": self.qty["*"], "ltp": 100.0}]


def _tracker(*legs, sl_order_id="SL1"):
    """legs: (symbol, strategy). One MIS leg each, holding its stop id."""
    engine = _make_engine()
    tracker = PositionTracker(engine)
    for symbol, strategy in legs:
        engine._state(strategy).open_positions += 1
        tracker.register(
            _make_position(symbol=symbol, strategy=strategy, product="MIS", sl_order_id=sl_order_id)
        )
    return tracker


class Harness:
    def __init__(self, world, broker, tracker, *, send_cost=1.0, close=None, budget=None,
                 place_result=None):
        self.w, self.b, self.tracker = world, broker, tracker
        self.notifier = AsyncMock()
        self.place = AsyncMock(return_value=place_result or TradeResult(
            status=OrderStatus.SUCCESS, order_id="NEWSTOP", message="ok"))
        self.sleeps: list[float] = []
        self.sends: list[tuple] = []
        self.close = close
        self.budget = budget
        self.send_cost = send_cost
        self.stack = contextlib.ExitStack()

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.w.advance(seconds)

    async def _send(self, order, pos):
        self.w.advance(self.send_cost)
        self.sends.append((order.symbol, order.quantity))
        self.b.qty[order.symbol] = 0
        self.b.log.append(("send", order.symbol))
        return TradeResult(status=OrderStatus.SUCCESS, order_id=f"X{len(self.sends)}", message="ok")

    def __enter__(self):
        w, b = self.w, self.b
        s = self.stack
        s.enter_context(patch.object(tracker_mod, "_monotonic", w.mono))
        s.enter_context(patch.object(tracker_mod, "_now_ist", w.wall, create=True))
        if self.budget is not None:
            s.enter_context(patch.object(tracker_mod, "_TIME_EXIT_BUDGET_S", self.budget))
        s.enter_context(patch("asyncio.sleep", AsyncMock(side_effect=self._sleep)))
        s.enter_context(patch("signal_engine.tracker.fetch_orderbook", AsyncMock(side_effect=b.orderbook)))
        s.enter_context(patch("signal_engine.tracker.cancel_order", AsyncMock(side_effect=b.cancel)))
        s.enter_context(patch("signal_engine.tracker.fetch_order_status", AsyncMock(side_effect=b.status)))
        s.enter_context(patch("signal_engine.tracker.fetch_net_position", AsyncMock(side_effect=b.net)))
        s.enter_context(patch("signal_engine.tracker.fetch_positionbook", AsyncMock(side_effect=b.positionbook)))
        s.enter_context(patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=0.0)))
        s.enter_context(patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=100.0)))
        s.enter_context(patch("signal_engine.tracker.place_sl_order", self.place))
        s.enter_context(patch("signal_engine.tracker.notifier", self.notifier))
        s.enter_context(patch("signal_engine.main._send_exit_with_retries", AsyncMock(side_effect=self._send)))
        if self.close is not None:
            s.enter_context(patch.object(PositionTracker, "_close_strategy", self.close))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    @property
    def alerts(self):
        return [str(c.args[0]) for c in self.notifier.notify.await_args_list if c.args]

    def skip_alerts(self, strategy=None):
        return [a for a in self.alerts if a.startswith("TIME EXIT SKIPPED")
                and (strategy is None or f"for {strategy} " in a)]


def _positions_for(tracker):
    return dict(tracker._positions)


# ---------------------------------------------------------------------------------------------
# MAJOR A: an accepted cancel never leaves an open position without protection
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAcceptedCancelBudgetGone:
    async def test_a_budget_gone_after_accepted_cancel_confirmation_still_runs_and_closes(self):
        """Soft budget (100 s here) ends during the cancel; the book is then unreadable for 70 s."""
        w = World(14, 50, 0)
        b = Broker(w, [_row("SL1")], qty=50, dead=lambda wd: 0 <= b.since_cancel() < 70,
                   cancel_cost=50.0)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker, budget=100.0) as h:
            await tracker.time_exit_all()
        # No close without a confirmed cancel: the send happened only after the book was read clean.
        sends = [i for i, (k, _) in enumerate(b.log) if k == "send"]
        cancels = [i for i, (k, _) in enumerate(b.log) if k == "cancel"]
        assert cancels and (not sends or sends[0] > cancels[0])
        assert b.working() == []  # nothing is left working beside the close
        # The position ends closed, or protected. Never open with its stop gone and no repair.
        # Exact outcome: the position is still open (50 shares), no exit order went out, and the
        # stop was placed again once, directly in the pass, and its id is tracked.
        pos = tracker.find_position("PFC", STRAT)
        assert b.qty_of("PFC") == 50
        assert b.count("send") == 0
        assert h.place.await_count == 1
        assert pos is not None and pos.sl_order_id == "NEWSTOP"
        assert not any("keeps its stop" in a for a in h.alerts)

    async def test_a_hard_deadline_with_the_stop_cancelled_hands_the_stop_to_the_retry(self):
        """The book is dead through every confirmation attempt and readable again for the final
        read; the position is open and its stop is gone: the stop is placed again by the
        missing-stop retry, once, at the trigger and quantity of the cancelled stop."""
        w = World(14, 52, 50)  # 100 s to 14:54:30
        b = Broker(w, [_row("SL1")], qty=50, dead=lambda wd: 0 <= b.since_cancel() < 62)
        tracker = _tracker(("PFC", STRAT))
        pos = tracker.find_position("PFC", STRAT)
        sl, qty = pos.sl, pos.quantity
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
            assert b.count("send") == 0  # no close without a confirmed cancel
            assert pos.sl_order_id == "", "the cancelled stop must be handed to the missing-stop retry"
            await _poll_retry(tracker, b)
            await _poll_retry(tracker, b)  # a second poll must not place a second stop
        assert h.place.await_count == 1
        kwargs = h.place.await_args.kwargs
        assert (kwargs["sl_price"], kwargs["quantity"], kwargs["strategy_tag"]) == (sl, qty, STRAT)
        assert pos.sl_order_id == "NEWSTOP"
        assert len(h.skip_alerts(STRAT)) == 1

    async def test_a_throttled_poll_does_not_leave_the_cancelled_stop_unplaced(self):
        """What the poll does today: its dead-stop repair runs at most every 120 s. A pass that
        ends right after an audit leaves the position open and stopless until the next one."""
        w = World(14, 52, 50)
        b = Broker(w, [_row("SL1")], qty=50, dead=lambda wd: 0 <= b.since_cancel() < 62)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
            tracker._last_stop_audit = tracker_mod.time.monotonic()  # an audit ran a moment ago
            await tracker._reconcile_stops()
            await _poll_retry(tracker, b)
        assert h.place.await_count == 1, "the first poll after the pass must restore the stop"


async def _poll_retry(tracker, broker):
    """The part of one poll that places a missing stop: positionbook, then _retry_missing_sl."""
    from signal_engine.config import settings

    book = await broker.positionbook()
    await tracker._retry_missing_sl(tracker_mod._index_positionbook(book), settings)


@pytest.mark.asyncio
class TestTodayTheDeadStopRepair:
    async def test_the_existing_poll_repair_replaces_a_cancelled_stop_when_not_throttled(self):
        """Trace of the existing mechanism (_release_dead_stops then _retry_missing_sl): it does
        re-place a stop whose id the book shows cancelled, but only when the book is read and the
        120 s audit is due. Not a bounded repair, so the pass does not rely on it alone."""
        w = World()
        b = Broker(w, [_row("SL1", status="cancelled")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            tracker._last_stop_audit = None
            await tracker._reconcile_stops()
            await _poll_retry(tracker, b)
        assert h.place.await_count == 1


# ---------------------------------------------------------------------------------------------
# start gate
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStartGate:
    @pytest.mark.parametrize(
        "start,budget",
        [((14, 53, 30), None), ((14, 45, 0), 80.0)],
        ids=["hard-deadline-60s-away", "soft-budget-80s"],
    )
    async def test_too_little_budget_sends_no_cancel_and_leaves_the_stop_alone(self, start, budget):
        w = World(*start)
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker, budget=budget) as h:
            await tracker.time_exit_all()
        assert b.count("cancel") == 0
        assert b.count("send") == 0
        assert b.working() == ["SL1"]  # the stop is untouched
        assert tracker.tracked_count == 1
        assert len(h.skip_alerts(STRAT)) == 1
        assert not any("keeps its stop" in a for a in h.alerts)

    def test_the_minimum_is_the_sum_of_the_worst_case_costs(self):
        # two cancels with a status check (2 x (5+5)), the confirmation read (19), one wait (2),
        # the net position read (5), the exit order timeout (20) and the final-state reads (19+5)
        assert tracker_mod._CANCEL_START_MIN_S == 2 * 10 + 19 + 2 + 5 + 20 + (19 + 5)


# ---------------------------------------------------------------------------------------------
# MAJOR B: the hard wall-clock deadline
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHardDeadline:
    async def test_a_pass_starting_after_14_54_30_does_nothing_and_alerts_once_per_strategy(self):
        w = World(14, 54, 31)
        b = Broker(w, [_row("A1", "A", "PFC"), _row("B1", "B", "TCS")], qty=50)
        tracker = _tracker(("PFC", "A"), ("TCS", "B"))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert b.log == []  # no read, no cancel, no net position, no close
        assert h.sleeps == []
        assert len(h.skip_alerts("A")) == 1 and len(h.skip_alerts("B")) == 1
        assert tracker.tracked_count == 2

    @pytest.mark.parametrize("n", [1, 3, 5])
    async def test_catch_up_at_14_52_with_a_dead_book_ends_by_the_hard_deadline(self, n):
        w = World(14, 52, 0)
        syms = ["PFC", "TCS", "INFY", "SBIN", "ITC"][:n]
        b = Broker(w, [_row(f"S{i}-1", f"S{i}", sym) for i, sym in enumerate(syms)], qty=50,
                   dead=lambda wd: True)
        tracker = _tracker(*[(sym, f"S{i}") for i, sym in enumerate(syms)])
        hard = w.hard
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        # stated worst case: one 19 s book read in flight at the deadline; the 14:55:00 failsafe
        # keeps 30 - 19 = 11 s
        assert w.now <= hard + timedelta(seconds=READ_DEAD_S)
        assert w.now <= w.at(14, 55, 0) - timedelta(seconds=11)
        assert b.count("cancel") == 0 and b.count("send") == 0
        assert tracker.tracked_count == n
        for i in range(n):
            assert len(h.skip_alerts(f"S{i}")) == 1

    async def test_several_healthy_strategies_at_14_52_are_all_closed(self):
        w = World(14, 52, 0)
        syms = ["PFC", "TCS", "INFY"]
        b = Broker(w, [_row(f"S{i}-1", f"S{i}", sym) for i, sym in enumerate(syms)],
                   qty=dict.fromkeys(syms, 50))
        tracker = _tracker(*[(sym, f"S{i}") for i, sym in enumerate(syms)])
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert b.count("send") == 3
        assert b.working() == []
        assert h.skip_alerts() == []


@pytest.mark.asyncio
class TestEverySiteIsBounded:
    """One test per place that can wait or send. The deadline is already past in each."""

    def _past(self, tracker, w):
        tracker._time_exit_deadline = w.t - 1
        tracker._time_exit_hard_deadline = w.t - 1

    async def test_first_confirmation_read(self):
        w = World()
        b = Broker(w, [_row("SL1")])
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker):
            self._past(tracker, w)
            cleared, detail = await tracker._clear_working_orders({STRAT}, {"PFC"})
        assert b.count("orderbook") == 0
        assert cleared is False and "time budget" in detail

    async def test_cancel_loop_per_order(self):
        w = World(14, 52, 50)  # 100 s left, 50 s per cancel call
        rows = [_row(f"O{i}", pricetype="LIMIT") for i in range(4)]
        b = Broker(w, rows, cancel_cost=50.0)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker):
            tracker._time_exit_hard_deadline = w.t + 100
            tracker._time_exit_deadline = w.t + 100
            complete, _ = await tracker._cancel_working_by_id(list(map(dict, rows)), {STRAT})
        assert complete is False
        assert b.count("cancel") == 2  # the third and fourth were not begun past the deadline
        assert w.now <= w.at(14, 54, 30) + timedelta(seconds=5)

    async def test_exit_lock_wait(self):
        import asyncio

        from signal_engine.main import _get_exit_lock

        tracker = _tracker(("PFC", STRAT))
        lock = _get_exit_lock("PFC", STRAT)
        await lock.acquire()  # a signal exit is mid-flight and never finishes
        real = tracker_mod.time.monotonic
        tracker._time_exit_hard_deadline = real() + 0.2
        tracker._time_exit_deadline = real() + 0.2
        try:
            with patch("signal_engine.tracker.notifier", AsyncMock()):
                await asyncio.wait_for(tracker._close_strategy(STRAT, {"PFC"}), timeout=3)
        finally:
            lock.release()
        assert tracker._deadline_unclosed == {STRAT}

    async def test_send_exit_with_retries_stops_new_attempts(self):
        from signal_engine import main as main_mod

        w = World(14, 54, 0)  # 30 s left; a send times out after 20 s
        hard_mono = w.t + 30
        var = getattr(main_mod, "_exit_time_left", None)
        calls = []

        async def failing_send(order):
            calls.append(order)
            w.advance(20.0)
            return TradeResult(status=OrderStatus.ERROR, message="rejected")

        order = SimpleNamespace(symbol="PFC", exchange="NSE", quantity=50, action=SimpleNamespace(value="SELL"),
                                strategy_tag=STRAT, product="MIS")
        pos = _make_position(symbol="PFC", strategy=STRAT)
        token = var.set(lambda: hard_mono - w.t) if var is not None else None
        try:
            with (
                patch.object(main_mod, "send_order", failing_send),
                patch("asyncio.sleep", AsyncMock(side_effect=lambda s: w.advance(s))),
                patch.object(main_mod, "_settle_prior_uncertain_exit", AsyncMock(return_value=None)),
            ):
                await main_mod._send_exit_with_retries(order, pos)
        finally:
            if token is not None:
                var.reset(token)
        assert len(calls) == 1  # a second 20 s attempt would end past the deadline

    async def test_fetch_net_position_in_verify(self):
        w = World()
        b = Broker(w, [])
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker):
            self._past(tracker, w)
            await tracker._verify_positions_closed(dict(tracker._positions), set())
        assert b.count("net") == 0
        assert tracker.tracked_count == 1  # still tracked: not booked as closed without proof

    async def test_confirm_orders_cancelled(self):
        w = World()
        b = Broker(w, [_row("SL1")])
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            self._past(tracker, w)
            await tracker._confirm_orders_cancelled(dict(tracker._positions), set())
        assert b.count("orderbook") == 0 and b.count("cancel") == 0
        assert len(h.alerts) == 1


# ---------------------------------------------------------------------------------------------
# the alert says what was read
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAlertWording:
    def _bad_words(self, alert):
        for word in ("keeps its stop", "arm", "Exception", "HTTP", "status code"):
            assert word not in alert.replace("alarm", "").replace("warm", ""), word

    async def test_stop_still_working_and_position_open(self):
        w = World(14, 52, 50)
        b = Broker(w, [_row("SL1")], qty=50, cancel_works=False)  # the cancel is accepted, the stop stays
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        (alert,) = h.skip_alerts(STRAT)
        assert "still working" in alert and "open" in alert
        assert "cancelled and" not in alert and "new stop" not in alert
        self._bad_words(alert)
        assert h.place.await_count == 0  # never a second stop while one works
        assert tracker.find_position("PFC", STRAT).sl_order_id == "SL1"
        assert b.count("send") == 0

    async def test_stop_cancelled_and_placed_again(self):
        w = World(14, 52, 50)
        b = Broker(w, [_row("SL1")], qty=50, dead=lambda wd: 0 <= b.since_cancel() < 62)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        (alert,) = h.skip_alerts(STRAT)
        assert "cancelled" in alert and "new stop" in alert
        assert "still working" not in alert
        self._bad_words(alert)

    async def test_stop_cancelled_and_position_closed_places_no_stop(self):
        w = World(14, 52, 50)
        b = Broker(w, [_row("SL1")], qty=0, cancel_cost=55.0)  # flat; the close gate then fails
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
            await _poll_retry(tracker, b)
        (alert,) = h.skip_alerts(STRAT)
        assert "closed" in alert and "cancelled" in alert
        assert "new stop" not in alert
        self._bad_words(alert)
        assert h.place.await_count == 0  # never a stop for a flat position
        assert b.count("send") == 0

    async def test_stop_could_not_be_confirmed(self):
        w = World(14, 52, 50)
        b = Broker(w, [_row("SL1")], qty=50, dead=lambda wd: b.since_cancel() >= 0)
        tracker = _tracker(("PFC", STRAT))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        (alert,) = h.skip_alerts(STRAT)
        assert "could not be confirmed" in alert
        self._bad_words(alert)
        assert h.place.await_count == 0  # unreadable book: no stop is placed blind
        assert tracker.find_position("PFC", STRAT).sl_order_id == "SL1"

    async def test_one_alert_per_strategy_per_pass_with_two_strategies(self):
        w = World(14, 54, 31)
        b = Broker(w, [], qty=50)
        tracker = _tracker(("PFC", "A"), ("TCS", "B"))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        assert len(h.skip_alerts()) == 2


# ---------------------------------------------------------------------------------------------
# the healthy path is unchanged
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHealthyPathUnchanged:
    async def test_call_sequence_and_waits_on_a_healthy_book(self):
        """Recorded at 4ec613358 (before the change) and unchanged after it."""
        w = World(14, 45, 5)
        rows = [_row("A-STOP", "A", "PFC"), _row("A-TP", "A", "PFC", status="open", pricetype="LIMIT"),
                _row("B-STOP", "B", "TCS")]
        b = Broker(w, rows, qty={"PFC": 50, "TCS": 50})
        tracker = _tracker(("PFC", "A"), ("TCS", "B"))
        with Harness(w, b, tracker) as h:
            await tracker.time_exit_all()
        seq = [(k, d if k != "orderbook" else None) for k, d in b.log]
        assert seq == EXPECTED_HEALTHY_SEQUENCE
        assert h.sleeps == [3]
        assert h.sends == [("PFC", 50), ("TCS", 50)]
        assert h.skip_alerts() == []


EXPECTED_HEALTHY_SEQUENCE: list = [
    ("orderbook", None), ("cancel", "A-STOP"), ("cancel", "A-TP"), ("orderbook", None),
    ("net", "PFC"), ("send", "PFC"),
    ("orderbook", None), ("cancel", "B-STOP"), ("orderbook", None),
    ("net", "TCS"), ("send", "TCS"),
    ("orderbook", None), ("positionbook", None), ("orderbook", None),
]


# ---------------------------------------------------------------------------------------------
# MINOR C: SQUAREOFF counting
# ---------------------------------------------------------------------------------------------


def _four_rows():
    return [
        ("WIN", ["TP1"], 100.0),
        ("LOSS", ["SL"], -40.0),
        ("TIMED", ["TIME"], 25.5),
        ("SQ", ["SQUAREOFF"], -12.25),
    ]


class TestSquareoffCounting:
    @pytest.fixture(autouse=True)
    def _fresh_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
        db.reset_connection()
        db.set_trade_mode("analyze")
        yield
        db.reset_connection()

    def _file(self):
        from signal_engine.models import Action, Order, Signal

        for sym, types, pnl in _four_rows():
            db.save(
                Signal(strategy="ORB", direction=Direction.LONG, symbol=sym, entry=100.0, sl=96.0,
                       tp=110.0, raw_message="x"),
                Order(symbol=sym, exchange="NSE", action=Action.BUY, quantity=10, price=0.0,
                      order_type="MARKET", product="MIS", strategy_tag="ORB"),
                TradeResult(status=OrderStatus.SUCCESS, order_id=f"E-{sym}"),
            )
            db.save_tracker_exit(strategy="ORB", symbol=sym, entry=100.0, sl=96.0, tp=0.0,
                                 quantity=10, exit_price=100.0, pnl=pnl, exit_types=types)

    def test_database_counts_add_up_and_net_pnl_includes_every_row(self):
        self._file()
        _, counts = PositionTracker(_make_engine())._day_from_db()
        assert counts["trades"] == 4
        assert (counts["wins"], counts["losses"], counts["time_exits"]) == (1, 1, 2)
        assert counts["trades"] == counts["wins"] + counts["losses"] + counts["time_exits"]
        assert counts["net_pnl"] == pytest.approx(100.0 - 40.0 + 25.5 - 12.25)

    def test_the_summary_header_and_win_rate(self):
        self._file()
        records, counts = PositionTracker(_make_engine())._day_from_db()
        header = notifier._day_summary_header(
            "24-Nov-2026", counts["trades"], counts["wins"], counts["losses"], counts["net_pnl"],
            100000.0, counts["time_exits"], records)
        assert "Trades: 4 | W: 1  L: 1  T: 2 | Win Rate: 50%" in header[1]
        assert f"Net: {notifier._pnl(73.25)}" in header[2]

    def test_per_strategy_stats_count_time_and_squareoff_together(self):
        self._file()
        records, _ = PositionTracker(_make_engine())._day_from_db()
        stats = notifier._aggregate_strategy_stats(records)
        assert stats == {"trades": 4, "wins": 1, "losses": 1, "time_exits": 2}

    def test_a_close_carrying_both_types_counts_one_time_exit(self):
        rec = SimpleNamespace(exit_types=["TIME", "SQUAREOFF"], total_pnl=5.0)
        stats = notifier._aggregate_strategy_stats([rec])
        assert (stats["trades"], stats["wins"], stats["losses"], stats["time_exits"]) == (1, 0, 0, 1)

    @pytest.mark.asyncio
    async def test_in_memory_fallback_counters_leave_undecided_closes_out_of_wins_and_losses(self):
        tracker = PositionTracker(_make_engine())
        for sym, types, pnl in _four_rows():
            pos = _make_position(symbol=sym, strategy="ORB", product="MIS")
            with patch("signal_engine.tracker.notifier", AsyncMock()), patch.object(
                PositionTracker, "_save_close_row", lambda *a, **k: None
            ):
                await tracker.book_close(pos, pnl_delta=pnl, exit_price=100.0, exit_types=types,
                                         hold_minutes=5)
        assert tracker._day_trades == 4
        assert (tracker._day_wins, tracker._day_losses, tracker._day_time_exits) == (1, 1, 2)
        assert tracker._day_trades == tracker._day_wins + tracker._day_losses + tracker._day_time_exits
        assert tracker._day_pnl == pytest.approx(100.0 - 40.0 + 25.5 - 12.25)
