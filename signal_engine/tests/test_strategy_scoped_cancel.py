"""BUG-019: a strategy's time exit cancels only its own working orders, by order id.

On 2026-11-23 one strategy's broker-wide cancel-all cancelled other strategies' stops and exits
(analyzer_logs 9209 at 14:45:03 cancelled 9 orders across 5 strategies; 9233 ORB-PY cancelled ORB
orders 26112353134603 and 26112353130809). The four tracker call sites (time exit cancel, the
unreadable-book and no-id fallbacks of _clear_working_orders, the verify retry) now cancel by id.
An unreadable order book cancels nothing and keeps the position managed; the retry re-reads it.
The scheduler's 14:55 square-off still uses cancel-all (test_scheduler_squareoff.py).
"""

import ast
import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker


def _row(oid, strategy, symbol="PFC", status="trigger pending", product="MIS", pricetype="SL-M"):
    return {
        "orderid": oid,
        "symbol": symbol,
        "action": "SELL",
        "pricetype": pricetype,
        "product": product,
        "order_status": status,
        "strategy": strategy,
    }


class FakeBroker:
    """Lock-protected shared order book. Records every by-id cancel and any cancel-all."""

    def __init__(self, rows, unreadable_reads=0, fill_before_cancel=()):
        self.rows = {r["orderid"]: dict(r) for r in rows}
        self.lock = threading.Lock()
        self.cancelled: list[str] = []
        self.cancel_calls: list[str] = []
        self.cancel_all_calls: list[str] = []
        self.unreadable_reads = unreadable_reads
        self.fill_before_cancel = set(fill_before_cancel)
        self.read_barrier: threading.Barrier | None = None
        self.reads = 0

    def working(self, strategy=None):
        with self.lock:
            return sorted(
                o
                for o, r in self.rows.items()
                if r["order_status"] in ("trigger pending", "open", "pending")
                and (strategy is None or r["strategy"] == strategy)
            )

    async def fetch_orderbook(self):
        with self.lock:
            self.reads += 1
            if self.unreadable_reads > 0:
                self.unreadable_reads -= 1
                return None
            snap = [dict(r) for r in self.rows.values()]
        if self.read_barrier is not None:
            self.read_barrier.wait(timeout=10)  # both threads hold a snapshot before any cancel
        return snap

    async def cancel_order(self, order_id, strategy):
        with self.lock:
            self.cancel_calls.append(order_id)
            if order_id in self.fill_before_cancel:
                self.rows[order_id]["order_status"] = "complete"  # the fill lands after the read
            row = self.rows.get(order_id)
            if row is None or row["order_status"] not in ("trigger pending", "open", "pending"):
                return False  # "Cannot cancel order in complete status"
            row["order_status"] = "cancelled"
            self.cancelled.append(order_id)
            return True

    async def fetch_order_status(self, order_id, strategy):
        with self.lock:
            row = self.rows.get(order_id)
            return row["order_status"] if row else ""

    async def post_tolerant(self, path, payload=None, *args, **kwargs):
        """Stands in for the HTTP post under api_client: any cancel-all request is recorded."""
        if path == "cancelallorder":
            self.cancel_all_calls.append((payload or {}).get("strategy"))
        raise AssertionError(f"unexpected broker post: {path}")


def _patches(broker, net_qty=0, tracker_notifier=None):
    return (
        # The cancelallorder endpoint itself: a cancel-all from any module is recorded, not only
        # one made through a name imported into tracker.py.
        patch(
            "signal_engine.api_client._post_tolerant",
            AsyncMock(side_effect=broker.post_tolerant),
        ),
        patch("signal_engine.tracker.cancel_order", AsyncMock(side_effect=broker.cancel_order)),
        patch(
            "signal_engine.tracker.fetch_order_status",
            AsyncMock(side_effect=broker.fetch_order_status),
        ),
        patch(
            "signal_engine.tracker.fetch_orderbook", AsyncMock(side_effect=broker.fetch_orderbook)
        ),
        patch(
            "signal_engine.tracker.fetch_net_position", new_callable=AsyncMock, return_value=net_qty
        ),
        patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
        patch("signal_engine.tracker.notifier", tracker_notifier or AsyncMock()),
        patch("asyncio.sleep", new_callable=AsyncMock),
    )


def _tracker(*legs):
    """legs: (symbol, strategy). Returns a tracker holding one MIS leg for each."""
    engine = _make_engine()
    tracker = PositionTracker(engine)
    for symbol, strategy in legs:
        engine._state(strategy).open_positions += 1
        tracker.register(_make_position(symbol=symbol, strategy=strategy, product="MIS"))
    return tracker


async def _run_time_exit(tracker, broker, net_qty=0):
    close = AsyncMock(return_value=True)
    note = AsyncMock()
    ps = _patches(broker, net_qty, note)
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
        with patch.object(PositionTracker, "_close_strategy", close):
            await tracker.time_exit_all()
    return close, note


@pytest.mark.asyncio
class TestOnlyOwnOrdersAreCancelled:
    async def test_three_strategies_one_time_exit_cancels_only_its_own(self):
        # (a) three strategies with a stop, an exit and an entry each, all working at once
        rows = []
        for strat, sym in (("ORB", "PFC"), ("BREAKINGTRADE", "TCS"), ("EMA-VWAP", "INFY")):
            rows += [
                _row(f"{strat}-STOP", strat, sym, "trigger pending", pricetype="SL-M"),
                _row(f"{strat}-EXIT", strat, sym, "open", pricetype="LIMIT"),
                _row(f"{strat}-ENTRY", strat, sym, "pending", pricetype="LIMIT"),
            ]
        broker = FakeBroker(rows)
        tracker = _tracker(("PFC", "ORB"))  # only ORB is time-exited
        close, _ = await _run_time_exit(tracker, broker)
        assert sorted(broker.cancelled) == ["ORB-ENTRY", "ORB-EXIT", "ORB-STOP"]
        assert len(broker.working("BREAKINGTRADE")) == 3
        assert len(broker.working("EMA-VWAP")) == 3
        assert broker.cancel_all_calls == []
        assert close.await_count == 1

    async def test_other_strategy_on_same_symbol_is_never_cancelled(self):
        # (b) same symbol PFC, tagged with another strategy: kept. Untagged MIS on PFC: ours.
        rows = [
            _row("OWN", "ORB", "PFC"),
            _row("OTHER", "BREAKINGTRADE", "PFC"),
            _row("UNTAGGED", "", "PFC", product="MIS"),
            _row("UNTAGGED-OTHER-SYMBOL", "", "TCS", product="MIS"),
            _row("UNTAGGED-CNC", "", "PFC", product="CNC"),
        ]
        broker = FakeBroker(rows)
        await _run_time_exit(_tracker(("PFC", "ORB")), broker)
        assert sorted(broker.cancelled) == ["OWN", "UNTAGGED"]
        assert "OTHER" not in broker.cancel_calls
        assert "UNTAGGED-OTHER-SYMBOL" not in broker.cancel_calls
        assert "UNTAGGED-CNC" not in broker.cancel_calls

    @pytest.mark.parametrize(
        "mine,other",
        [
            ("ORB", "ORB-PY"),
            ("ORB-PY", "ORB"),
            ("BREAKINGTRADE", "BREAKINGTRADE-RI"),
            ("BREAKINGTRADE-RI", "BREAKINGTRADE"),
        ],
    )
    async def test_similar_tags_are_not_confused(self, mine, other):
        # (c) exact tag match only
        broker = FakeBroker([_row("MINE", mine), _row("THEIRS", other)])
        await _run_time_exit(_tracker(("PFC", mine)), broker)
        assert broker.cancelled == ["MINE"]
        assert broker.working(other) == ["THEIRS"]

    async def test_replay_2026_11_23_orb_py_time_exit_leaves_orb_and_breakingtrade(self):
        # (g) analyzer_logs 9233: ORB-PY cancelled ORB orders 26112353134603 / 26112353130809
        rows = [
            _row("26112353134603", "ORB", "PFC", "trigger pending"),
            _row("26112353130809", "ORB", "TCS", "open", pricetype="LIMIT"),
            _row("26112353199999", "BREAKINGTRADE", "INFY", "trigger pending"),
            _row("26112353111111", "ORB-PY", "SBIN", "trigger pending"),
        ]
        broker = FakeBroker(rows)
        await _run_time_exit(_tracker(("SBIN", "ORB-PY")), broker)
        assert broker.cancelled == ["26112353111111"]
        for oid in ("26112353134603", "26112353130809", "26112353199999"):
            assert oid not in broker.cancel_calls
            assert broker.rows[oid]["order_status"] in ("trigger pending", "open")


@pytest.mark.asyncio
class TestLateFill:
    async def test_complete_status_counts_as_filled_and_the_loop_continues(self):
        # (d) the stop fills before its cancel; the other two orders are still cancelled
        rows = [_row("A", "ORB"), _row("FILLED", "ORB"), _row("C", "ORB")]
        broker = FakeBroker(rows, fill_before_cancel={"FILLED"})
        tracker = _tracker(("PFC", "ORB"))
        ps = _patches(broker)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            complete, filled = await tracker._cancel_strategy_orders("ORB", {"PFC"})
        assert complete is True
        assert filled == ["FILLED"]
        assert sorted(broker.cancelled) == ["A", "C"]
        assert broker.cancel_calls == ["A", "FILLED", "C"]

    async def test_other_failure_continues_and_reports_incomplete(self):
        rows = [_row("A", "ORB"), _row("B", "ORB"), _row("C", "ORB")]
        broker = FakeBroker(rows)
        real = broker.cancel_order

        async def flaky(order_id, strategy):
            if order_id == "A":
                return False  # refused, and the status stays working: a real failure
            return await real(order_id, strategy)

        broker.cancel_order = flaky
        tracker = _tracker(("PFC", "ORB"))
        ps = _patches(broker)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            complete, filled = await tracker._cancel_strategy_orders("ORB", {"PFC"})
        assert complete is False and filled == []
        assert sorted(broker.cancelled) == ["B", "C"]


class TestConcurrentCancels:
    def _run_two(self, broker, jobs):
        """jobs: [(strategy, symbol)]; each runs in its own thread and event loop."""
        barrier = threading.Barrier(len(jobs))
        broker.read_barrier = threading.Barrier(len(jobs))
        results, errors = {}, []
        ps = _patches(broker)

        def worker(strategy, symbol):
            try:
                tracker = _tracker((symbol, strategy))
                barrier.wait(timeout=10)
                results[strategy] = asyncio.run(tracker._cancel_strategy_orders(strategy, {symbol}))
            except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(e)

        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            threads = [threading.Thread(target=worker, args=j) for j in jobs]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        assert not errors, errors
        return results

    def test_two_strategies_cancel_concurrently_each_only_its_own(self):
        # (e) shared lock-protected book; both threads hold a snapshot before either cancels
        rows = [
            _row(f"{s}-{i}", s, sym)
            for s, sym in (("ORB", "PFC"), ("ORB-PY", "PFC"))
            for i in range(5)
        ]
        broker = FakeBroker(rows)
        results = self._run_two(broker, [("ORB", "PFC"), ("ORB-PY", "PFC")])
        assert results["ORB"] == (True, []) and results["ORB-PY"] == (True, [])
        assert sorted(broker.cancelled) == sorted(r["orderid"] for r in rows)
        assert len(broker.cancelled) == len(set(broker.cancelled))  # none twice
        assert len(broker.cancel_calls) == len(rows)  # no strategy touched the other's order
        assert broker.cancel_all_calls == []

    def test_fill_between_book_read_and_cancel_under_concurrency(self):
        rows = [_row(f"{s}-{i}", s, "PFC") for s in ("ORB", "ORB-PY") for i in range(3)]
        broker = FakeBroker(rows, fill_before_cancel={"ORB-1", "ORB-PY-2"})
        results = self._run_two(broker, [("ORB", "PFC"), ("ORB-PY", "PFC")])
        assert results["ORB"] == (True, ["ORB-1"])
        assert results["ORB-PY"] == (True, ["ORB-PY-2"])
        assert len(broker.cancelled) == 4 and len(set(broker.cancelled)) == 4
        assert broker.cancel_all_calls == []


@pytest.mark.asyncio
class TestUnreadableBookAndNoId:
    async def test_unreadable_then_readable_book_never_uses_cancel_all(self):
        # (f) attempt 1: book unreadable -> nothing cancelled, position stays managed.
        broker = FakeBroker([_row("OWN", "ORB"), _row("OTHER", "ORB-PY")], unreadable_reads=100)
        tracker = _tracker(("PFC", "ORB"))
        close, note = await _run_time_exit(tracker, broker, net_qty=50)
        assert broker.cancelled == [] and broker.cancel_calls == []
        assert broker.cancel_all_calls == []
        assert close.await_count == 0
        assert tracker.tracked_count == 1  # still tracked and managed
        assert "could not be read" in " ".join(
            str(c.args[0]) for c in note.notify.await_args_list if c.args
        )

        # attempt 2: the book is readable -> only its own order is cancelled, then the close goes out
        broker2 = FakeBroker([_row("OWN", "ORB"), _row("OTHER", "ORB-PY")], unreadable_reads=1)
        tracker2 = _tracker(("PFC", "ORB"))
        close2, _ = await _run_time_exit(tracker2, broker2)
        assert broker2.cancelled == ["OWN"]
        assert broker2.cancel_all_calls == []
        assert close2.await_count == 1
        assert tracker2.tracked_count == 0
        assert broker2.working("ORB-PY") == ["OTHER"]

    async def test_working_order_with_no_id_is_skipped_not_cancel_all(self):
        rows = [_row("", "ORB"), _row("HASID", "ORB")]
        broker = FakeBroker(rows)
        broker.rows[""] = dict(rows[0])
        tracker = _tracker(("PFC", "ORB"))
        ps = _patches(broker)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            complete, filled = await tracker._cancel_strategy_orders("ORB", {"PFC"})
        assert complete is False  # incomplete: the retry path re-reads the book
        assert broker.cancelled == ["HASID"]  # the other order is still cancelled
        assert broker.cancel_all_calls == []

    async def test_no_id_order_leaves_position_unclosed_and_managed(self):
        broker = FakeBroker([_row("", "ORB")])
        tracker = _tracker(("PFC", "ORB"))
        close, _ = await _run_time_exit(tracker, broker, net_qty=50)
        assert broker.cancel_all_calls == [] and broker.cancel_calls == []
        assert close.await_count == 0
        assert tracker.tracked_count == 1


@pytest.mark.asyncio
class TestCancelAllNeverCalledFromTracker:
    async def test_verify_retry_cancels_by_id(self):
        # (h) the 2719 site: first close leaves the position open, retry cancels by id only
        broker = FakeBroker([_row("OWN", "ORB"), _row("OTHER", "BREAKINGTRADE")])
        tracker = _tracker(("PFC", "ORB"))
        qty = AsyncMock(side_effect=[50, 0])
        close = AsyncMock(return_value=True)
        ps = _patches(broker)
        with ps[0], ps[1], ps[2], ps[3], ps[5], ps[6], ps[7]:
            with patch("signal_engine.tracker.fetch_net_position", qty):
                with patch.object(PositionTracker, "_close_strategy", close):
                    await tracker.time_exit_all()
        assert broker.cancel_all_calls == []
        assert broker.cancelled == ["OWN"]
        assert broker.working("BREAKINGTRADE") == ["OTHER"]
        assert close.await_count == 2

    async def test_unreadable_book_during_confirmation_never_cancel_all(self):
        # The book is readable and clean until the close goes out, then dies. The strategy is
        # closed, so the post-close confirmation stage really runs against an unreadable book.
        broker = FakeBroker([_row("OWN", "ORB"), _row("OTHER", "ORB-PY")])
        tracker = _tracker(("PFC", "ORB"))

        reads_at_close = []

        def close_then_book_dies(strategy, symbols):
            reads_at_close.append(broker.reads)
            broker.unreadable_reads = 10_000

        close = AsyncMock(side_effect=close_then_book_dies)
        note = AsyncMock()
        ps = _patches(broker, 0, note)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            with patch.object(PositionTracker, "_close_strategy", close):
                await tracker.time_exit_all()
        assert close.await_count == 1  # the strategy was closed: the stage after it is reached
        assert broker.reads - reads_at_close[0] >= 4  # confirmation retried an unreadable book
        assert broker.cancelled == ["OWN"]  # only the pre-close by-id cancel
        assert broker.cancel_all_calls == []
        assert broker.working("ORB-PY") == ["OTHER"]
        alerts = " ".join(str(c.args[0]) for c in note.notify.await_args_list if c.args)
        assert "not confirmed cancelled" in alerts
        assert "could not be read" in alerts


@pytest.mark.asyncio
class TestTagMatching:
    """Review #4: exact match after .strip() only. No case folding."""

    async def test_padded_tag_matches_exact_strategy(self):
        broker = FakeBroker([_row("PADDED", " ORB "), _row("OTHER", "ORB-PY")])
        await _run_time_exit(_tracker(("PFC", "ORB")), broker)
        assert broker.cancelled == ["PADDED"]

    async def test_lowercase_tag_is_a_different_strategy(self):
        # "orb" is not "ORB": not cancelled, not treated as ours, not confirmed away either
        broker = FakeBroker([_row("LOWER", "orb"), _row("OWN", "ORB")])
        await _run_time_exit(_tracker(("PFC", "ORB")), broker)
        assert broker.cancelled == ["OWN"]
        assert "LOWER" not in broker.cancel_calls
        assert broker.rows["LOWER"]["order_status"] == "trigger pending"

    async def test_none_tag_cancelled_under_the_strategys_tag(self):
        broker = FakeBroker([_row("NOTAG", None)])
        seen = []
        real = broker.cancel_order

        async def spy(order_id, strategy):
            seen.append((order_id, strategy))
            return await real(order_id, strategy)

        broker.cancel_order = spy
        await _run_time_exit(_tracker(("PFC", "ORB")), broker)
        assert broker.cancelled == ["NOTAG"]
        assert seen == [("NOTAG", "ORB")]

    async def test_row_without_orderid_key_is_skipped_and_incomplete(self):
        row = _row("X", "ORB")
        del row["orderid"]
        broker = FakeBroker([_row("HASID", "ORB")])
        broker.rows["nokey"] = row  # keyed apart: the row itself has no orderid
        tracker = _tracker(("PFC", "ORB"))
        ps = _patches(broker)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            complete, filled = await tracker._cancel_strategy_orders("ORB", {"PFC"})
        assert complete is False and filled == []
        assert broker.cancelled == ["HASID"]
        assert broker.cancel_all_calls == []

    async def test_untagged_mis_stop_on_shared_symbol_is_ours_by_owner_decision(self):
        # Accepted behaviour: an untagged MIS working order on a symbol this strategy closes is
        # cancelled, even when it belongs to a strategy that is not in mis_positions.
        broker = FakeBroker(
            [
                _row("UNTAGGED-STOP", "", "PFC", product="MIS"),
                _row("TAGGED-OTHER", "BREAKINGTRADE", "PFC"),
            ]
        )
        await _run_time_exit(_tracker(("PFC", "ORB")), broker)
        assert broker.cancelled == ["UNTAGGED-STOP"]
        assert broker.working("BREAKINGTRADE") == ["TAGGED-OTHER"]


class TestWorkingOrdersTagRules:
    def test_working_orders_unit_cases(self):
        rows = [
            _row("A", " ORB "),
            _row("B", "orb"),
            _row("C", None),  # no tag: an MIS order on our symbol is ours
            _row("D", "ORB-PY"),
        ]
        got = PositionTracker._working_orders(rows, {"ORB"}, {"PFC"})
        assert [o["orderid"] for o in got] == ["A", "C"]


@pytest.mark.asyncio
class TestOrderBookReads:
    """Review #5: how many order-book reads one strategy costs."""

    async def test_healthy_book_with_working_orders_two_reads(self):
        broker = FakeBroker([_row("OWN", "ORB")])
        tracker = _tracker(("PFC", "ORB"))
        ps = _patches(broker)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            with patch.object(PositionTracker, "_close_strategy", AsyncMock()) as close:
                _, skipped = await tracker._square_off_strategies(dict(tracker._positions))
        assert skipped == set() and close.await_count == 1
        assert broker.cancelled == ["OWN"]
        assert broker.reads == 2  # the cancel pass, then the confirmation before the close

    async def test_nothing_working_reuses_the_clean_read(self):
        broker = FakeBroker([_row("OTHER", "ORB-PY")])
        tracker = _tracker(("PFC", "ORB"))
        ps = _patches(broker)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            skipped = await tracker._square_off_strategies(dict(tracker._positions))
        assert skipped == (set(), set())
        assert broker.reads == 1

    async def test_one_time_unreadable_book_three_reads_one_cancel(self):
        broker = FakeBroker([_row("OWN", "ORB")], unreadable_reads=1)
        tracker = _tracker(("PFC", "ORB"))
        ps = _patches(broker)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            with patch.object(PositionTracker, "_close_strategy", AsyncMock()) as close:
                _, skipped = await tracker._square_off_strategies(dict(tracker._positions))
        assert skipped == set() and close.await_count == 1
        assert broker.reads == 3
        assert broker.cancelled == ["OWN"] and broker.cancel_calls == ["OWN"]

    async def test_permanently_unreadable_book_five_reads_no_close(self):
        broker = FakeBroker([_row("OWN", "ORB")], unreadable_reads=10_000)
        tracker = _tracker(("PFC", "ORB"))
        ps = _patches(broker, 0, AsyncMock())
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7]:
            with patch.object(PositionTracker, "_close_strategy", AsyncMock()) as close:
                _, skipped = await tracker._square_off_strategies(dict(tracker._positions))
        assert skipped == {"ORB"} and close.await_count == 0
        assert broker.reads == 5  # one pre-cancel read and the four of the confirmation loop
        assert broker.cancel_calls == [] and broker.cancel_all_calls == []


class _Clock:
    def __init__(self):
        self.t = 5_000.0

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


@pytest.mark.asyncio
class TestTimeExitTimeBudget:
    """Review #5: a per-pass monotonic budget; a dead order book cannot hold the pass past it."""

    READ_COST_S = 19.0  # api.timeout 5 s x 3 tries on a dead book

    def _timed(self, broker, clock):
        real = broker.fetch_orderbook

        async def slow_read():
            clock.t += self.READ_COST_S
            return await real()

        return slow_read

    async def test_five_strategies_dead_book_pass_ends_inside_the_budget(self):
        from signal_engine import tracker as tracker_mod

        strategies = [f"S{i}" for i in range(5)]
        symbols = ["PFC", "TCS", "INFY", "SBIN", "ITC"]
        broker = FakeBroker([_row(f"{s}-STOP", s, sym) for s, sym in zip(strategies, symbols, strict=True)], 10_000)
        tracker = _tracker(*zip(symbols, strategies, strict=True))
        clock = _Clock()
        start = clock.t
        close = AsyncMock()
        note = AsyncMock()
        ps = _patches(broker, 50, note)
        with ps[0], ps[1], ps[2], ps[4], ps[5], ps[6]:
            with patch("signal_engine.tracker.fetch_orderbook", AsyncMock(side_effect=self._timed(broker, clock))):
                with patch("asyncio.sleep", AsyncMock(side_effect=clock.sleep)):
                    with patch.object(tracker_mod, "_monotonic", clock):
                        with patch.object(PositionTracker, "_close_strategy", close):
                            await tracker.time_exit_all()
        elapsed = clock.t - start
        budget = tracker_mod._TIME_EXIT_BUDGET_S
        # one read and one wait may be in flight when the budget runs out
        assert elapsed <= budget + self.READ_COST_S + 2
        assert broker.reads == 15  # three strategies read five times; two are skipped unread
        assert close.await_count == 0  # no close without a confirmed cancel
        assert broker.cancel_all_calls == [] and broker.cancel_calls == []
        assert tracker.tracked_count == 5  # all stay tracked and managed
        alerts = [str(c.args[0]) for c in note.notify.await_args_list if c.args]
        for s in strategies:
            assert any("TIME EXIT SKIPPED for " + s in a for a in alerts), s
        assert any("time budget" in a for a in alerts)

    async def test_strategies_after_the_deadline_make_no_reads(self):
        from signal_engine import tracker as tracker_mod

        broker = FakeBroker([_row("A-STOP", "A", "PFC")])
        tracker = _tracker(("PFC", "A"))
        clock = _Clock()
        tracker._time_exit_deadline = clock.t - 1  # already past
        ps = _patches(broker, 0, AsyncMock())
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6]:
            with patch("asyncio.sleep", AsyncMock(side_effect=clock.sleep)):
                with patch.object(tracker_mod, "_monotonic", clock):
                    with patch.object(PositionTracker, "_close_strategy", AsyncMock()) as close:
                        _, skipped = await tracker._square_off_strategies(dict(tracker._positions))
        assert skipped == {"A"} and close.await_count == 0
        assert broker.reads == 0 and broker.cancel_calls == []

    async def test_verify_retry_stops_when_the_budget_is_gone(self):
        from signal_engine import tracker as tracker_mod

        broker = FakeBroker([_row("OWN", "ORB")])
        tracker = _tracker(("PFC", "ORB"))
        clock = _Clock()
        note = AsyncMock()

        def close_slowly(strategy, symbols):
            clock.t += tracker_mod._TIME_EXIT_BUDGET_S + 1

        close = AsyncMock(side_effect=close_slowly)
        ps = _patches(broker, 50, note)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6]:
            with patch("asyncio.sleep", AsyncMock(side_effect=clock.sleep)):
                with patch.object(tracker_mod, "_monotonic", clock):
                    with patch.object(PositionTracker, "_close_strategy", close):
                        await tracker.time_exit_all()
        assert close.await_count == 1  # the re-close was not sent: its cancel cannot be confirmed
        assert broker.cancel_all_calls == []
        assert tracker.tracked_count == 1


class TestBudgetAgainstFailsafe:
    READ_COST_S = TestTimeExitTimeBudget.READ_COST_S

    def test_budget_leaves_room_before_the_failsafe_square_off(self):
        # The 14:55 openAlgoSquareOff task is independent of the time exit and still cancels and
        # closes with the broker-wide calls; the time exit's budget must end well before it.
        from signal_engine import tracker as tracker_mod
        from signal_engine.config import settings

        failsafe_s = 14 * 3600 + 55 * 60
        start_s = settings.time_exit_hour * 3600 + settings.time_exit_minute * 60 + 5  # 5 s poll
        in_flight = self.READ_COST_S + 2
        later_stages = 3 * 3 + 2 * 3 * self.READ_COST_S  # verify waits + two position reads/try
        assert start_s + tracker_mod._TIME_EXIT_BUDGET_S + in_flight + later_stages < failsafe_s
        scheduler = Path(__file__).resolve().parents[1] / "scripts" / "openalgoscheduler.py"
        text = scheduler.read_text(encoding="utf-8")
        assert "_attempt(cancel_all_orders, strategy" in text
        assert "_attempt(close_all_positions, strategy" in text


class TestTrackerHasNoCancelAll:
    def test_tracker_source_never_references_cancel_all_orders(self):
        path = Path(__file__).resolve().parents[1] / "tracker.py"
        source = path.read_text(encoding="utf-8")
        assert "cancel_all_orders" not in source
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.alias):
                names += [node.name, node.asname or ""]
            assert "cancel_all_orders" not in names
