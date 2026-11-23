"""BUG-019: a strategy's time exit cancels only its own working orders, by order id.

On 2026-11-23 one strategy's broker-wide cancel-all cancelled other strategies' stops and exits
(analyzer_logs 9209 at 14:45:03 cancelled 9 orders across 5 strategies; 9233 ORB-PY cancelled ORB
orders 26112353134603 and 26112353130809). The four tracker call sites (time exit cancel, the
unreadable-book and no-id fallbacks of _clear_working_orders, the verify retry) now cancel by id.
An unreadable order book cancels nothing and keeps the position managed; the retry re-reads it.
The scheduler's 14:55 square-off still uses cancel-all (test_scheduler_squareoff.py).
"""

import asyncio
import threading
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

    async def cancel_all_orders(self, strategy):
        self.cancel_all_calls.append(strategy)
        raise AssertionError(f"cancel_all_orders called for {strategy}")


def _patches(broker, net_qty=0, tracker_notifier=None):
    return (
        patch(
            "signal_engine.tracker.cancel_all_orders",
            AsyncMock(side_effect=broker.cancel_all_orders),
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
        broker = FakeBroker([_row("OWN", "ORB")], unreadable_reads=100)
        tracker = _tracker(("PFC", "ORB"))
        await _run_time_exit(tracker, broker)
        assert broker.cancel_all_calls == []
