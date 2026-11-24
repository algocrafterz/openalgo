"""Second follow-up to the time exit's hard deadline: the stop it puts back, and what it counts.

M1  A stop placement cut by wait_for (or by the outer task) while its order may already be at
    the broker left no mark, so the next poll sent a second stop blind. Now the key is marked
    unconfirmed (the executor's own registries) so the next place_sl_order reads the book first
    and adopts a stop that landed.
M2  (option a) A leg left open with no stop, no time, a refused or an unconfirmed stop is
    flagged time_exit_left_open; the missing-stop retry skips it until 15:00 IST, so only the
    14:55 square-off acts on it. The alert says so.
m1  The in-pass stop is placed only when NO working order of the strategy on that symbol is on
    the book just read.
m2  A leg whose exit order was sent but whose result is unknown (held) is not re-cancelled and
    not re-closed by the verify loop; exactly one time-exit alert goes out per strategy.
m3  A close order the pass sent counts as complete only when the order book shows it complete.

Clocks are fake (World moves the wall and the monotonic clock together); every broker call is a
fake. No network, no real stop is placed.
"""

import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from loguru import logger

from signal_engine import executor, order_reconcile
from signal_engine import main as main_mod
from signal_engine import tracker as tracker_mod
from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.test_time_exit_followup_a import (
    MainRig,
    _left_open_rig,
    _timeout,
    _tracker_engine,
    _with_place,
)
from signal_engine.tests.test_time_exit_hard_deadline import (
    STRAT,
    Broker,
    Harness,
    World,
    _poll_retry,
    _row,
    _tracker,
)
from signal_engine.tests.tracker_fixtures import _make_position
from signal_engine.tracker import PositionTracker

IST = tracker_mod.IST
_REAL_SLEEP = asyncio.sleep
_REAL_WAIT_FOR = asyncio.wait_for


@pytest.fixture(autouse=True)
def _clean_state():
    def reset():
        order_reconcile._uncertain.clear()
        executor._UNCONFIRMED_SL.clear()
        executor._PENDING_DUPLICATE_STOPS.clear()
        executor._EPISODE_START.clear()
        executor._LAST_UNKNOWN_SEND.clear()

    reset()
    yield
    reset()


def _stop_row(oid, strategy, symbol="PFC", action="SELL", qty=10, trigger=100.0, status="trigger pending"):
    return {
        "orderid": oid, "symbol": symbol, "action": action, "pricetype": "SL-M", "product": "MIS",
        "order_status": status, "strategy": strategy, "quantity": qty, "trigger_price": trigger,
    }


def _quick_wait_for(aw, timeout=None):
    """asyncio.wait_for with a 50 ms limit: stands for the pass's deadline running out."""
    return _REAL_WAIT_FOR(aw, timeout=0.05)


class _Cut:
    """A fake send_order whose request lands on the book and whose answer never arrives."""

    def __init__(self, broker, strategy, action, qty, trigger):
        self.b, self.strategy, self.action, self.qty, self.trigger = broker, strategy, action, qty, trigger
        self.sent: list = []

    async def __call__(self, order):
        self.sent.append(order)
        self.b.rows["LANDED"] = _stop_row(
            "LANDED", self.strategy, action=self.action, qty=self.qty, trigger=self.trigger
        )
        await asyncio.Event().wait()


def _real_executor_patches(b, cut):
    return [
        patch("signal_engine.executor.send_order", cut),
        patch("signal_engine.api_client.fetch_orderbook", AsyncMock(side_effect=b.orderbook)),
        patch("signal_engine.tracker.place_sl_order", executor.place_sl_order),
    ]


def _marked(key) -> dict:
    return {
        "unconfirmed": key in executor._UNCONFIRMED_SL,
        "episode": key in executor._EPISODE_START,
        "pending": key in executor._PENDING_DUPLICATE_STOPS,
        "last_send": key in executor._LAST_UNKNOWN_SEND,
    }


ALL_MARKED = {"unconfirmed": True, "episode": True, "pending": True, "last_send": True}

DIRECTIONS = [
    pytest.param(Direction.LONG, "SELL", 100.0, id="long"),
    pytest.param(Direction.SHORT, "BUY", 110.0, id="short"),
]


def _signed(direction, qty=10):
    return qty if direction == Direction.LONG else -qty


# ---------------------------------------------------------------------------------------------
# (1) M1: wait_for cuts place_sl_order mid-send; the next poll reads the book and adopts
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestM1_CutMidSend:
    @pytest.mark.parametrize("direction,action,sl", DIRECTIONS)
    async def test_a_cut_send_marks_the_key_and_the_next_poll_adopts_the_landed_stop(
        self, direction, action, sl
    ):
        w, b, tracker = _left_open_rig(
            ("PFC", "A", direction, 10, "SL1"), net=_signed(direction), accepted={"A": ["SL1"]},
            now=(14, 53, 0),
        )
        pos = tracker.find_position("PFC", "A")
        key = ("A", "PFC", action)
        cut = _Cut(b, "A", action, 10, sl)
        with Harness(w, b, tracker) as h:
            from contextlib import ExitStack

            with ExitStack() as stack:
                for p in _real_executor_patches(b, cut):
                    stack.enter_context(p)
                stack.enter_context(patch("signal_engine.tracker.asyncio.wait_for", _quick_wait_for))
                text = await tracker._restore_stop(pos, "PFC", 10)
                assert len(cut.sent) == 1
                assert "could not be confirmed" in text
                assert _marked(key) == ALL_MARKED
                assert isinstance(executor._EPISODE_START[key], datetime)
                assert pos.sl_order_id == ""
                # The 14:55 failsafe window is over: the poll may act again. It reads the book
                # first and adopts the stop that landed, with no second send.
                w.now = w.at(15, 0, 5)
                reads = b.count("orderbook")
                await _poll_retry(tracker, b)
        assert len(cut.sent) == 1, "a second stop was sent blind"
        assert b.count("orderbook") > reads, "the poll did not read the order book first"
        assert pos.sl_order_id == "LANDED"
        assert h.place.await_count == 0

    @pytest.mark.parametrize("direction,action,sl", DIRECTIONS)
    async def test_a_timeout_error_or_exception_from_place_marks_the_key(self, direction, action, sl):
        for outcome in (
            TradeResult(status=OrderStatus.TIMEOUT, message="timed out"),
            TradeResult(status=OrderStatus.ERROR, message="refused"),
            RuntimeError("boom"),
        ):
            executor._UNCONFIRMED_SL.clear()
            executor._PENDING_DUPLICATE_STOPS.clear()
            executor._EPISODE_START.clear()
            executor._LAST_UNKNOWN_SEND.clear()
            w, b, tracker = _left_open_rig(
                ("PFC", "A", direction, 10, "SL1"), net=_signed(direction), accepted={"A": ["SL1"]},
                now=(14, 53, 0),
            )
            pos = tracker.find_position("PFC", "A")
            key = ("A", "PFC", action)
            with Harness(w, b, tracker) as h:
                if isinstance(outcome, Exception):
                    h.place.side_effect = outcome
                else:
                    h.place.side_effect = None
                    h.place.return_value = outcome
                text = await tracker._restore_stop(pos, "PFC", 10)
            assert "could not be confirmed" in text, outcome
            assert _marked(key) == ALL_MARKED, outcome
            assert pos.time_exit_left_open is True, outcome
            assert pos.sl_order_id == "", outcome

    async def test_the_marked_key_makes_place_sl_order_scan_before_any_send(self):
        w, b, tracker = _left_open_rig(
            ("PFC", "A", Direction.LONG, 10, "SL1"), net=10, accepted={"A": ["SL1"]}
        )
        key = ("A", "PFC", "SELL")
        # an empty readable book right after an unknown send: hold, send nothing
        sent: list = []

        async def send(order):
            sent.append(order)
            return TradeResult(status=OrderStatus.SUCCESS, order_id="N1", message="ok")

        with Harness(w, b, tracker) as h:
            h.place.side_effect = RuntimeError("cut")
            await tracker._restore_stop(tracker.find_position("PFC", "A"), "PFC", 10)
            with patch("signal_engine.executor.send_order", send), patch(
                "signal_engine.api_client.fetch_orderbook", AsyncMock(side_effect=b.orderbook)
            ):
                result = await executor.place_sl_order(
                    symbol="PFC", exchange="NSE", direction=Direction.LONG, quantity=10,
                    sl_price=100.0, product="MIS", strategy_tag="A",
                )
        assert key in executor._UNCONFIRMED_SL
        assert sent == [], "sent a stop blind after a cut placement"
        assert result.status == OrderStatus.TIMEOUT and result.message == executor.HOLD_MESSAGE

    async def test_the_outer_task_cancelled_during_the_placement(self):
        """CancelledError is not an Exception: the key must still be marked, the stop lock
        released and the cancellation must propagate."""
        w, b, tracker = _left_open_rig(
            ("PFC", "A", Direction.LONG, 10, "SL1"), net=10, accepted={"A": ["SL1"]}
        )
        pos = tracker.find_position("PFC", "A")
        key = ("A", "PFC", "SELL")
        cut = _Cut(b, "A", "SELL", 10, 100.0)
        with Harness(w, b, tracker):
            from contextlib import ExitStack

            with ExitStack() as stack:
                for p in _real_executor_patches(b, cut):
                    stack.enter_context(p)
                task = asyncio.ensure_future(tracker._restore_stop(pos, "PFC", 10))
                for _ in range(200):
                    if cut.sent:
                        break
                    await _REAL_SLEEP(0)
                assert cut.sent, "the placement never started"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not executor._stop_lock(key).locked()
        assert _marked(key) == ALL_MARKED
        assert pos.sl_order_id == ""
        assert pos.time_exit_left_open is True


# ---------------------------------------------------------------------------------------------
# (2) M2: flagged legs are not retried before 15:00 IST
# ---------------------------------------------------------------------------------------------


def _path_rig(path):
    """A leg A/PFC whose cancelled stop could not be put back (`path`), and an unrelated leg
    B/INFY with no stop. Returns (world, broker, tracker, harness kwargs)."""
    w, b, tracker = _left_open_rig(
        ("PFC", "A", Direction.LONG, 10, "SL1"), ("INFY", "B", Direction.LONG, 10, ""),
        net={"PFC": 10, "INFY": 10}, accepted={"A": ["SL1"]},
        hard_left=30.0 if path == "no_time" else 200.0, now=(14, 54, 0),
    )
    return w, b, tracker


def _routed_place(h, calls, pfc_result):
    async def place(**kwargs):
        calls.append((kwargs["symbol"], kwargs["quantity"]))
        if kwargs["symbol"] == "PFC":
            return pfc_result[0]
        return TradeResult(status=OrderStatus.SUCCESS, order_id="OTHERSTOP", message="ok")

    h.place.side_effect = place


@pytest.mark.asyncio
class TestM2_FlaggedLegsWaitForTheFailsafe:
    @pytest.mark.parametrize("path", ["no_time", "refused", "timeout"])
    async def test_no_stop_between_14_54_30_and_15_00_then_exactly_one_after(self, path):
        w, b, tracker = _path_rig(path)
        pos = tracker.find_position("PFC", "A")
        other = tracker.find_position("INFY", "B")
        result = {
            "no_time": [TradeResult(status=OrderStatus.SUCCESS, order_id="NEWSTOP", message="ok")],
            "refused": [TradeResult(status=OrderStatus.ERROR, message="rejected")],
            "timeout": [TradeResult(status=OrderStatus.TIMEOUT, message="timed out")],
        }[path]
        calls: list = []
        with Harness(w, b, tracker) as h:
            _routed_place(h, calls, result)
            text = await tracker._left_open_state("A", {"PFC"})
            in_pass = [c for c in calls if c[0] == "PFC"]
            assert len(in_pass) == (0 if path == "no_time" else 1)
            assert pos.time_exit_left_open is True, text
            assert pos.sl_order_id == ""
            # 14:54:45: the poll runs twice; nothing for the flagged leg, the other leg is repaired
            w.advance(45)
            assert w.now == w.at(14, 54, 45)
            await _poll_retry(tracker, b)
            await _poll_retry(tracker, b)
            assert [c for c in calls if c[0] == "PFC"] == in_pass
            assert pos.sl_retry_count == 0
            assert [c for c in calls if c[0] == "INFY"] == [("INFY", 10)]
            assert other.sl_order_id == "OTHERSTOP"
            # after 15:00 the retry resumes: exactly one stop, never two
            result[0] = TradeResult(status=OrderStatus.SUCCESS, order_id="NEWSTOP", message="ok")
            w.now = w.at(15, 0, 5)
            await _poll_retry(tracker, b)
            await _poll_retry(tracker, b)
        assert len([c for c in calls if c[0] == "PFC"]) == len(in_pass) + 1
        assert pos.sl_order_id == "NEWSTOP"

    async def test_an_accepted_stop_that_is_not_on_the_book_is_flagged_and_never_doubled(self):
        w, b, tracker = _path_rig("unconfirmed")
        pos = tracker.find_position("PFC", "A")
        with Harness(w, b, tracker) as h:  # place returns NEWSTOP, the fake book never shows it
            text = await tracker._left_open_state("A", {"PFC"})
            assert "could not be confirmed" in text
            assert pos.time_exit_left_open is True and pos.sl_order_id == "NEWSTOP"
            for at in ((14, 54, 45), (15, 0, 5)):
                w.now = w.at(*at)
                await _poll_retry(tracker, b)
        pfc_calls = [c for c in h.place.await_args_list if c.kwargs["symbol"] == "PFC"]
        assert len(pfc_calls) == 1

    async def test_a_leg_flagged_by_the_booking_is_also_skipped_until_15_00(self):
        """Any time_exit_left_open leg, not only the ones the repair flagged."""
        w, b, tracker = _path_rig("refused")
        pos = tracker.find_position("PFC", "A")
        pos.sl_order_id = ""
        pos.time_exit_left_open = True
        w.now = w.at(14, 54, 40)
        with Harness(w, b, tracker) as h:
            await _poll_retry(tracker, b)
            assert [c for c in h.place.await_args_list if c.kwargs["symbol"] == "PFC"] == []
            w.now = w.at(15, 0, 1)
            await _poll_retry(tracker, b)
        assert len([c for c in h.place.await_args_list if c.kwargs["symbol"] == "PFC"]) == 1


# ---------------------------------------------------------------------------------------------
# (3) m1: no stop beside any working order
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestM1_NoStopBesideAWorkingOrder:
    async def test_a_working_limit_target_with_a_failed_cancel_means_no_stop_and_says_so(self):
        w = World(14, 50, 0)
        target = _row("TGT", STRAT, "PFC", status="open", pricetype="LIMIT")
        b = Broker(w, [_row("SL1"), target], qty=50)
        tracker = _tracker(("PFC", STRAT))
        pos = tracker.find_position("PFC", STRAT)

        async def cancel(order_id, strategy):
            b.log.append(("cancel", order_id))
            if order_id == "TGT":
                return False  # refused: the target stays working
            b.rows[order_id]["order_status"] = "cancelled"
            return True

        with Harness(w, b, tracker) as h:
            with patch("signal_engine.tracker.cancel_order", AsyncMock(side_effect=cancel)):
                await tracker.time_exit_all()
        assert h.place.await_count == 0
        assert b.count("send") == 0
        (alert,) = h.skip_alerts(STRAT)
        assert "still working" in alert and "TGT" in alert and "no new stop" in alert.lower()
        assert "placed and confirmed" not in alert
        assert pos.time_exit_left_open is True

    async def test_a_cancelled_target_does_not_block_the_stop(self):
        w, b, tracker = _left_open_rig(
            ("PFC", "A", Direction.LONG, 10, "SL1"), net=10, accepted={"A": ["SL1"]},
            rows=[_row("TGT", "A", "PFC", status="cancelled", pricetype="LIMIT")],
        )
        with Harness(w, b, tracker) as h:
            _with_place(h, b)
            text = await tracker._left_open_state("A", {"PFC"})
        assert h.place.await_count == 1 and "placed and confirmed" in text

    async def test_another_strategys_working_order_does_not_block_the_stop(self):
        w, b, tracker = _left_open_rig(
            ("PFC", "A", Direction.LONG, 10, "SL1"), net=10, accepted={"A": ["SL1"]},
            rows=[_row("OTHER", "B", "PFC", status="open", pricetype="LIMIT")],
        )
        with Harness(w, b, tracker) as h:
            await tracker._left_open_state("A", {"PFC"})
        assert h.place.await_count == 1


# ---------------------------------------------------------------------------------------------
# (4) m3: a close the pass sent counts as done only when the book shows it complete
# ---------------------------------------------------------------------------------------------


def _close_row(status):
    return {
        "orderid": "CB1", "symbol": "PFC", "action": "BUY", "pricetype": "MARKET", "product": "MIS",
        "order_status": status, "strategy": "B",
    }


def _ab_rig(book_rows, *, net=0, dead=lambda w: False):
    """A long 10 and B short 10 on PFC; B's close CB1 was sent by this pass."""
    w = World(14, 53, 0)
    b = Broker(w, list(book_rows), qty=net, dead=dead)
    tracker = PositionTracker(_tracker_engine())
    for strategy, direction in (("A", Direction.LONG), ("B", Direction.SHORT)):
        tracker.register(_make_position(
            symbol="PFC", strategy=strategy, product="MIS", direction=direction, quantity=10,
            sl_order_id="S" + strategy, sl=100.0 if direction == Direction.LONG else 110.0,
        ))
    tracker._square_off_orders[tracker._key("PFC", "B")] = "CB1"
    tracker._time_exit_hard_deadline = w.t + 150
    tracker._time_exit_deadline = w.t + 150
    tracker._cancel_sent["A"] = {"SA": _row("SA", "A")}
    tracker._cancel_accepted["A"] = {"SA": _row("SA", "A")}
    return w, b, tracker


@pytest.mark.asyncio
class TestM3_PendingCloseOfTheOppositeLeg:
    @pytest.mark.parametrize("status", ["open", "trigger pending", "pending", "partially filled"])
    async def test_a_working_close_leaves_the_other_leg_counted_open(self, status):
        w, b, tracker = _ab_rig([_close_row(status)], net=0)
        key_a = tracker._key("PFC", "A")
        pos_a = tracker.find_position("PFC", "A")
        with Harness(w, b, tracker):
            qty = await tracker._own_close_qty(key_a, pos_a)
        assert qty == 10, "B's close is incomplete: B still holds -10, so A's own share is 10"
        assert tracker._others_signed_qty(key_a, pos_a, [_close_row(status)]) == -10

    async def test_a_close_missing_from_the_book_counts_as_open(self):
        w, b, tracker = _ab_rig([], net=0)
        key_a = tracker._key("PFC", "A")
        pos_a = tracker.find_position("PFC", "A")
        with Harness(w, b, tracker):
            qty = await tracker._own_close_qty(key_a, pos_a)
        assert qty == 10
        assert tracker._others_signed_qty(key_a, pos_a, []) == -10

    async def test_an_unreadable_book_counts_as_open_and_is_never_netted(self):
        w, b, tracker = _ab_rig([_close_row("complete")], net=0, dead=lambda wd: True)
        key_a = tracker._key("PFC", "A")
        pos_a = tracker.find_position("PFC", "A")
        with Harness(w, b, tracker):
            qty = await tracker._own_close_qty(key_a, pos_a)
        assert b.count("orderbook") >= 1
        assert qty == 10  # B counted as open: A's own share is not zeroed on a guess

    async def test_a_complete_close_means_the_other_leg_is_gone(self):
        """Net 0 with B's close complete: A is flat too (its stop filled). Nothing to sell."""
        w, b, tracker = _ab_rig([_close_row("complete")], net=0)
        key_a = tracker._key("PFC", "A")
        pos_a = tracker.find_position("PFC", "A")
        with Harness(w, b, tracker):
            qty = await tracker._own_close_qty(key_a, pos_a)
        assert qty == 0
        assert tracker._others_signed_qty(key_a, pos_a, [_close_row("complete")]) == 0

    async def test_the_stop_decision_treats_a_pending_close_as_still_open(self):
        w, b, tracker = _ab_rig([_close_row("open")], net=0)
        with Harness(w, b, tracker) as h:
            text = await tracker._left_open_state("A", {"PFC"})
        assert h.place.await_count == 1, text
        assert h.place.await_args.kwargs["quantity"] == 10
        assert h.place.await_args.kwargs["direction"] == Direction.LONG
        assert "closed" not in text.replace("cancelled", "")

    async def test_the_stop_decision_treats_a_complete_close_as_gone(self):
        w, b, tracker = _ab_rig([_close_row("complete")], net=0)
        with Harness(w, b, tracker) as h:
            text = await tracker._left_open_state("A", {"PFC"})
        assert h.place.await_count == 0
        assert "the position is closed" in text

    async def test_an_unreadable_book_for_the_stop_decision_says_it_could_not_confirm(self):
        w, b, tracker = _ab_rig([_close_row("open")], net=0, dead=lambda wd: True)
        with Harness(w, b, tracker) as h:
            text = await tracker._left_open_state("A", {"PFC"})
        assert h.place.await_count == 0
        assert "could not be confirmed" in text


# ---------------------------------------------------------------------------------------------
# (5) m2: held legs in the verify loop
# ---------------------------------------------------------------------------------------------


def _held_send(w, b, sends, symbols=("PFC",), then=None):
    async def send(order, pos):
        sends.append(order.symbol)
        if order.symbol not in symbols:
            return await then(order, pos)
        order_reconcile.mark_uncertain(order_reconcile.UncertainOrder(
            strategy=pos.strategy, symbol=pos.symbol, action="SELL", quantity=order.quantity,
            since=w.now, kind="EXIT", watch_until=w.now + timedelta(seconds=120),
        ))
        b.rows["EXIT1"] = {
            "orderid": "EXIT1", "symbol": order.symbol, "action": "SELL", "pricetype": "MARKET",
            "product": "MIS", "order_status": "open", "strategy": pos.strategy,
        }
        return TradeResult(status=OrderStatus.TIMEOUT, message="UNCERTAIN: sent, outcome unknown")

    return send


@pytest.mark.asyncio
class TestM2_HeldLegInTheVerifyLoop:
    async def test_a_held_exit_is_not_cancelled_not_closed_again_and_alerted_once(self):
        w = World(14, 50, 0)
        b = Broker(w, [_row("SL1")], qty=50)
        tracker = _tracker(("PFC", STRAT))
        pos = tracker.find_position("PFC", STRAT)
        sends: list = []
        with Harness(w, b, tracker) as h, patch(
            "signal_engine.main._send_exit_with_retries", AsyncMock(side_effect=_held_send(w, b, sends))
        ):
            await tracker.time_exit_all()
        cancels = [d for k, d in b.log if k == "cancel"]
        assert cancels == ["SL1"], f"the held exit order was cancelled: {cancels}"
        assert sends == ["PFC"], "the held leg was closed again"
        words = [a for a in h.alerts if "result not known" in a]
        assert len(words) == 1, h.alerts
        assert "PFC" in words[0] and "held" in words[0]
        assert "while it may still fill" in words[0] and "14:55 square-off closes" in words[0]
        assert not any("place a stop by hand" in a or "TIME EXIT FAILED" in a for a in h.alerts), h.alerts
        assert h.skip_alerts() == []
        assert pos.time_exit_left_open is True
        assert b.rows["EXIT1"]["order_status"] == "open"

    async def test_a_held_leg_does_not_stop_the_other_leg_of_the_strategy_closing(self):
        w = World(14, 50, 0)
        b = Broker(w, [_row("SL1"), _row("SL2", STRAT, "INFY")], qty={"PFC": 50, "INFY": 50})
        tracker = _tracker(("PFC", STRAT), ("INFY", STRAT))
        sends: list = []
        with Harness(w, b, tracker) as h:
            async def other(order, pos):
                return await h._send(order, pos)

            with patch(
                "signal_engine.main._send_exit_with_retries",
                AsyncMock(side_effect=_held_send(w, b, sends, then=other)),
            ):
                await tracker.time_exit_all()
        assert sorted(sends) == ["INFY", "PFC"]
        assert [d for k, d in b.log if k == "cancel"] == ["SL1", "SL2"]
        assert len([a for a in h.alerts if "result not known" in a]) == 1, h.alerts
        assert b.qty_of("INFY") == 0

    async def test_the_time_exit_send_raises_no_alert_of_its_own(self):
        """main._send_exit_with_retries under the time exit: the tracker logs the one line."""
        w = World(14, 53, 0)
        pos = _make_position(symbol="PFC", strategy=STRAT)
        rig = MainRig(w, [_timeout()])
        logged: list[str] = []
        sink = logger.add(lambda m: logged.append(m.record["message"]), level="CRITICAL")
        try:
            result = await rig.run(pos, hard=w.t + 90)
        finally:
            logger.remove(sink)
        assert result.status == OrderStatus.TIMEOUT
        assert not any(m.startswith("ORDER UNCONFIRMED") for m in logged), logged
        assert order_reconcile.get_uncertain(STRAT, "PFC") is not None

    async def test_outside_the_time_exit_the_alert_text_is_unchanged(self):
        """The former Telegram text is now the CRITICAL log line, word for word."""
        pos = _make_position(symbol="PFC", strategy="A", quantity=10)
        order = SimpleNamespace(symbol="PFC", quantity=10, action=SimpleNamespace(value="SELL"))
        logged: list[str] = []
        sink = logger.add(lambda m: logged.append(m.record["message"]), level="CRITICAL")
        try:
            await main_mod._hold_uncertain_exit(order, pos, "no reply")
        finally:
            logger.remove(sink)
        (line,) = [m for m in logged if m.startswith("ORDER UNCONFIRMED")]
        assert line == (
            "ORDER UNCONFIRMED: UNCERTAIN: SELL 10 PFC [A] was sent but its outcome could not be "
            "confirmed (no reply); not resent. The stop was cancelled for this exit, so the position "
            "is UNPROTECTED. Candidate orders: none found on the book. No further orders go out for "
            "it until the order book confirms the exit. Check the broker order book and the PFC "
            "position now: if the exit filled, do nothing; if it did not, exit or place a stop by hand."
        )


# ---------------------------------------------------------------------------------------------
# (6) a short-direction restore through the whole pass
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestShortRestoreThroughThePass:
    async def test_a_short_leg_gets_a_buy_side_stop_for_its_own_quantity(self):
        w = World(14, 52, 20)
        b = Broker(w, [_row("SL1", "B", "PFC")], qty=-10, dead=lambda wd: 0 <= b.since_cancel() < 80)
        b.rows["SL1"]["action"] = "BUY"
        engine = _tracker_engine()
        tracker = PositionTracker(engine)
        engine._state("B").open_positions += 1
        tracker.register(_make_position(
            symbol="PFC", strategy="B", product="MIS", direction=Direction.SHORT, quantity=10,
            sl_order_id="SL1", sl=110.0, tp=90.0,
        ))
        with Harness(w, b, tracker) as h:
            async def place(**kwargs):
                b.rows["NEWSTOP"] = _row("NEWSTOP", kwargs["strategy_tag"], kwargs["symbol"])
                return TradeResult(status=OrderStatus.SUCCESS, order_id="NEWSTOP", message="ok")

            h.place.side_effect = place
            await tracker.time_exit_all()
        assert b.count("send") == 0
        assert h.place.await_count == 1
        kw = h.place.await_args.kwargs
        assert (kw["direction"], kw["quantity"], kw["sl_price"], kw["strategy_tag"]) == (
            Direction.SHORT, 10, 110.0, "B",
        )
        (alert,) = h.skip_alerts("B")
        assert "placed and confirmed" in alert and "NEWSTOP" in alert
        pos = tracker.find_position("PFC", "B")
        assert pos.sl_order_id == "NEWSTOP" and pos.time_exit_left_open is False


# ---------------------------------------------------------------------------------------------
# (8) alert texts and nits
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAlertTexts:
    @pytest.mark.parametrize("path,fragment", [
        ("no_time", "no time"), ("refused", "could not be confirmed"), ("timeout", "could not be confirmed"),
    ])
    async def test_the_flagged_paths_say_no_new_stop_before_14_55(self, path, fragment):
        w, b, tracker = _path_rig(path)
        result = {
            "no_time": [TradeResult(status=OrderStatus.SUCCESS, order_id="N", message="ok")],
            "refused": [TradeResult(status=OrderStatus.ERROR, message="rejected")],
            "timeout": [TradeResult(status=OrderStatus.TIMEOUT, message="timed out")],
        }[path]
        with Harness(w, b, tracker) as h:
            _routed_place(h, [], result)
            text = await tracker._left_open_state("A", {"PFC"})
        assert fragment in text
        assert "No new stop will be placed before 14:55" in text
        assert "the 14:55 square-off closes this position" in text
        assert "10 shares" in text
        assert "placed and confirmed" not in text and "arm" not in text.lower().split()

    async def test_the_working_order_text(self):
        w, b, tracker = _left_open_rig(
            ("PFC", "A", Direction.LONG, 10, "SL1"), net=10, accepted={"A": ["SL1"]},
            rows=[_row("TGT", "A", "PFC", status="open", pricetype="LIMIT")],
        )
        with Harness(w, b, tracker):
            text = await tracker._left_open_state("A", {"PFC"})
        assert "still working" in text and "TGT" in text and "No new stop is placed" in text
        assert "14:55 square-off" in text

    def test_the_docstring_and_comment_nits_are_fixed(self):
        source = inspect.getsource(PositionTracker._skip_time_exit)
        assert "_hand_back_stops" not in source
        assert "only the clean-up below runs" not in source
