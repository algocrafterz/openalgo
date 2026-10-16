"""Late entry fills: an entry order that fills AFTER the timeout recovery window is still found,
tracked and protected, and no second entry is sent while it might still land.

2026-10-15 11:16: NTPC (BREAKOUT-PY, 961 shares), AXISBANK and KOTAKBANK entries timed out, the
engine read the order book three times in about six seconds, saw nothing, and wrote the entry off
as "a genuine failure". The orders landed seconds later. Nothing tracked them and no stop was
placed. They sat unprotected for over an hour.

Rules under test:
  * A clean "not on the book" read right after a timeout is NOT proof the order never lands. The
    entry stays on a bounded watch: new entries for that strategy and symbol are held, and the
    sweep adopts a fill that appears (stop included) without ever resending the entry.
  * After the watch window passes with a clean book, the hold is released.
  * An unreadable book never releases anything.
  * After a process restart the in-memory watch is gone: the startup restore adopts a broker
    position that only an unconfirmed (TIMEOUT) entry row explains, and the tracker re-protects it.
  * Cancellation logs say what the order book proved, not what was merely requested.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import db, main, order_reconcile, startup
from signal_engine.executor import StopSweep
from signal_engine.models import (
    Action,
    Direction,
    Order,
    OrderStatus,
    Signal,
    TradeResult,
)
from signal_engine.timeutils import IST


def _signal(symbol="NTPC", strategy="BREAKOUT-PY"):
    return Signal(
        strategy=strategy,
        direction=Direction.LONG,
        symbol=symbol,
        entry=315.25,
        sl=314.3,
        tp=0.0,
        sig_id=f"{strategy}-{symbol}-1",
    )


def _order(symbol="NTPC", strategy="BREAKOUT-PY", qty=961):
    return Order(
        symbol=symbol,
        exchange="NSE",
        action=Action.BUY,
        quantity=qty,
        price=0.0,
        order_type="MARKET",
        product="MIS",
        strategy_tag=strategy,
    )


def _filled(symbol="NTPC", qty=961, order_id="L1", status="complete", action="BUY"):
    return {
        "symbol": symbol,
        "action": action,
        "quantity": qty,
        "order_status": status,
        "orderid": order_id,
        "timestamp": "2099-01-01 00:00:00",
        "strategy": "BREAKOUT-PY",
    }


@pytest.fixture(autouse=True)
def _clean_registry():
    def wipe():
        order_reconcile._uncertain.clear()
        order_reconcile._last_checked.clear()
        order_reconcile._last_alerted.clear()

    wipe()
    yield
    wipe()


@pytest.fixture(autouse=True)
def _no_real_sleep():
    with patch("asyncio.sleep", AsyncMock()):
        yield


def _watch(signal=None, order=None, window=120.0, age=10.0):
    """A watched entry as _recover_ambiguous_entry_timeout registers it."""
    signal = signal or _signal()
    order = order or _order()
    now = datetime.now(IST)
    pending = order_reconcile.UncertainOrder(
        strategy=signal.strategy,
        symbol=signal.symbol,
        action=order.action.value,
        quantity=order.quantity,
        since=now - timedelta(seconds=age),
        kind="ENTRY",
        signal=signal,
        order=order,
        watch_until=now + timedelta(seconds=window),
    )
    order_reconcile.mark_uncertain(pending)
    return pending


class TestCleanAbsentReadStartsAWatch:
    @pytest.mark.asyncio
    async def test_a_clean_empty_book_after_a_timeout_registers_a_bounded_watch(self):
        signal, order = _signal(), _order()
        with (
            patch.object(main, "fetch_orderbook", AsyncMock(return_value=[])),
            patch.object(main, "fetch_order_ids_today", MagicMock(return_value=set())),
        ):
            got = await main._recover_ambiguous_entry_timeout(signal, order, datetime.now(IST))
        assert got is None  # still a failed send as far as the caller is concerned
        pending = order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC")
        assert pending is not None and pending.kind == "ENTRY"
        assert pending.watch_until is not None and pending.watch_until > datetime.now(IST)
        assert pending.signal is signal and pending.order is order

    @pytest.mark.asyncio
    async def test_a_fill_found_inside_the_window_registers_no_watch(self):
        with (
            patch.object(main, "fetch_orderbook", AsyncMock(return_value=[_filled()])),
            patch.object(main, "fetch_order_ids_today", MagicMock(return_value=set())),
        ):
            got = await main._recover_ambiguous_entry_timeout(
                _signal(), _order(), datetime.now(IST) - timedelta(seconds=5)
            )
        assert got is not None and got.order_id == "L1"
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is None

    @pytest.mark.asyncio
    async def test_an_unreadable_book_holds_without_a_release_time(self):
        with (
            patch.object(main, "fetch_orderbook", AsyncMock(return_value=None)),
            patch.object(main, "fetch_order_ids_today", MagicMock(return_value=set())),
            patch.object(main.notifier, "notify", AsyncMock()),
        ):
            await main._recover_ambiguous_entry_timeout(_signal(), _order(), datetime.now(IST))
        pending = order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC")
        assert pending is not None and pending.watch_until is None


class TestNewEntriesWaitForTheWatch:
    @pytest.mark.asyncio
    async def test_a_second_entry_is_held_while_the_watch_is_open_and_the_book_is_empty(self):
        _watch()
        with patch.object(main, "fetch_orderbook", AsyncMock(return_value=[])):
            held = await main._entry_held_by_unresolved_order(_signal())
        assert held is True
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is not None

    @pytest.mark.asyncio
    async def test_a_fill_that_appears_is_adopted_and_no_second_entry_goes_out(self):
        _watch()
        adopt = AsyncMock()
        with (
            patch.object(main, "fetch_orderbook", AsyncMock(return_value=[_filled()])),
            patch.object(main, "_adopt_untracked_entry", adopt),
        ):
            held = await main._entry_held_by_unresolved_order(_signal())
        assert held is True
        adopt.assert_awaited_once()
        assert adopt.await_args.args[1] == "L1"
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is None

    @pytest.mark.asyncio
    async def test_after_the_window_with_a_clean_book_the_hold_is_released(self):
        _watch(window=-1.0)
        with patch.object(main, "fetch_orderbook", AsyncMock(return_value=[])):
            held = await main._entry_held_by_unresolved_order(_signal())
        assert held is False
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is None

    @pytest.mark.asyncio
    async def test_an_unreadable_book_never_releases_the_hold(self):
        _watch(window=-1.0)
        with patch.object(main, "fetch_orderbook", AsyncMock(return_value=None)):
            held = await main._entry_held_by_unresolved_order(_signal())
        assert held is True
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is not None


class TestSweepAdoptsALateFill:
    @pytest.mark.asyncio
    async def test_a_fill_that_lands_after_the_recovery_window_is_adopted_by_the_sweep(self):
        _watch()
        adopt = AsyncMock()
        send = AsyncMock(side_effect=AssertionError("the entry must never be resent"))
        with (
            patch.object(main, "fetch_orderbook", AsyncMock(return_value=[_filled()])),
            patch.object(main, "_adopt_untracked_entry", adopt),
            patch.object(main, "send_order", send),
        ):
            await main.sweep_unresolved_orders()
        adopt.assert_awaited_once()
        assert adopt.await_args.args[1] == "L1"
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is None

    @pytest.mark.asyncio
    async def test_a_second_sweep_does_not_adopt_the_same_fill_again(self):
        _watch()
        adopt = AsyncMock()
        with (
            patch.object(main, "fetch_orderbook", AsyncMock(return_value=[_filled()])),
            patch.object(main, "_adopt_untracked_entry", adopt),
        ):
            await main.sweep_unresolved_orders()
            order_reconcile._last_checked.clear()
            await main.sweep_unresolved_orders()
        adopt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_empty_book_inside_the_window_keeps_the_watch(self):
        _watch()
        adopt = AsyncMock()
        with (
            patch.object(main, "fetch_orderbook", AsyncMock(return_value=[])),
            patch.object(main, "_adopt_untracked_entry", adopt),
        ):
            await main.sweep_unresolved_orders()
        adopt.assert_not_awaited()
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is not None

    @pytest.mark.asyncio
    async def test_an_empty_book_after_the_window_drops_the_watch(self):
        _watch(window=-1.0)
        adopt = AsyncMock()
        with (
            patch.object(main, "fetch_orderbook", AsyncMock(return_value=[])),
            patch.object(main, "_adopt_untracked_entry", adopt),
        ):
            await main.sweep_unresolved_orders()
        adopt.assert_not_awaited()
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is None

    @pytest.mark.asyncio
    async def test_an_unreadable_book_keeps_the_watch_even_after_the_window(self):
        _watch(window=-1.0)
        with patch.object(main, "fetch_orderbook", AsyncMock(return_value=None)):
            await main.sweep_unresolved_orders()
        assert order_reconcile.get_uncertain("BREAKOUT-PY", "NTPC") is not None

    @pytest.mark.asyncio
    async def test_a_rejected_order_seen_on_the_book_is_not_adopted(self):
        _watch()
        adopt = AsyncMock()
        with (
            patch.object(
                main, "fetch_orderbook", AsyncMock(return_value=[_filled(status="rejected")])
            ),
            patch.object(main, "_adopt_untracked_entry", adopt),
        ):
            await main.sweep_unresolved_orders()
        adopt.assert_not_awaited()


class TestRestartRestoresAnUnconfirmedEntry:
    def test_the_lookup_skips_unconfirmed_rows_unless_asked(self):
        signal, order = _signal(), _order()
        db.save(signal, order, TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out"))
        assert db.fetch_last_entry_trade("NTPC", "BREAKOUT-PY") is None
        row = db.fetch_last_entry_trade("NTPC", "BREAKOUT-PY", include_unconfirmed=True)
        assert row is not None and row["quantity"] == 961 and row["sl"] == 314.3

    def test_a_confirmed_row_wins_over_a_timeout_row(self):
        signal, order = _signal(), _order()
        db.save(signal, order, TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out"))
        db.save(
            signal, order, TradeResult(order_id="OK1", status=OrderStatus.SUCCESS, message="ok")
        )
        row = db.fetch_last_entry_trade("NTPC", "BREAKOUT-PY", include_unconfirmed=True)
        assert row["order_id"] == "OK1"

    def _restore(self, rows, broker_qty=961):
        tracker = MagicMock()
        tracker.find_position.return_value = None

        def fake(symbol, strategy, include_unconfirmed=False):
            row = rows.get(strategy)
            if row is None:
                return None
            if row.get("unconfirmed") and not include_unconfirmed:
                return None
            return {k: v for k, v in row.items() if k != "unconfirmed"}

        broker = [
            {"symbol": "NTPC", "exchange": "NSE", "quantity": broker_qty, "average_price": 315.3}
        ]
        with patch.object(startup, "fetch_last_entry_trade", side_effect=fake):
            n = startup._restore_tracker_positions(tracker, broker, "MIS", sl_orders={})
        return n, tracker

    def _row(self, **kw):
        base = {
            "entry": 315.25,
            "sl": 314.3,
            "tp": 0.0,
            "quantity": 961,
            "order_id": "",
            "direction": Direction.LONG,
            "executed_at": "2026-10-15T11:16:08",
            "sig_id": "S1",
            "unconfirmed": True,
        }
        base.update(kw)
        return base

    def test_a_broker_position_explained_only_by_a_timeout_row_is_restored_unprotected(self):
        n, tracker = self._restore({"BREAKOUT-PY": self._row()})
        assert n == 1
        pos = tracker.register.call_args.args[0]
        assert pos.symbol == "NTPC" and pos.strategy == "BREAKOUT-PY" and pos.quantity == 961
        assert pos.sl_order_id == ""  # the poll loop's missing-stop retry places the stop

    def test_a_confirmed_row_is_preferred_and_the_timeout_row_is_ignored(self):
        rows = {
            "BREAKOUT-PY": self._row(unconfirmed=True),
            "ORB": self._row(unconfirmed=False, order_id="OK9"),
        }
        n, tracker = self._restore(rows)
        assert n == 1
        assert tracker.register.call_args.args[0].strategy == "ORB"

    def test_a_timeout_row_of_the_opposite_direction_is_not_adopted(self):
        n, tracker = self._restore({"BREAKOUT-PY": self._row(direction=Direction.SHORT)})
        assert n == 0
        tracker.register.assert_not_called()


class TestCancellationLogsSeparateRequestedFromProven:
    @pytest.mark.asyncio
    async def test_the_exit_sweep_reports_confirmed_and_unresolved_apart_from_requested(self):
        pos = MagicMock(
            symbol="NTPC", strategy="BREAKOUT-PY", direction=Direction.LONG, sl_order_id="A"
        )
        sweep = StopSweep(
            clear=False, cancelled=("A", "B"), confirmed_cancelled=("A",), unresolved=("B",)
        )
        with (
            patch.object(main, "cancel_live_protective_orders", AsyncMock(return_value=sweep)),
            patch.object(main.logger, "warning") as warn,
            patch.object(main.logger, "error") as err,
        ):
            await main._sweep_protective_orders(pos)
        warned = " ".join(str(c.args[0]) for c in warn.call_args_list)
        erred = " ".join(str(c.args[0]) for c in err.call_args_list)
        assert "confirmed cancelled" in warned and "['A']" in warned
        assert "['A', 'B']" not in warned  # the request list is not presented as the result
        assert "unresolved" in erred and "['B']" in erred

    @pytest.mark.asyncio
    async def test_a_request_that_nothing_confirmed_is_not_logged_as_cancelled(self):
        pos = MagicMock(
            symbol="NTPC", strategy="BREAKOUT-PY", direction=Direction.LONG, sl_order_id="A"
        )
        sweep = StopSweep(clear=False, cancelled=("A",), unresolved=("A",))
        with (
            patch.object(main, "cancel_live_protective_orders", AsyncMock(return_value=sweep)),
            patch.object(main.logger, "warning") as warn,
            patch.object(main.logger, "error"),
        ):
            await main._sweep_protective_orders(pos)
        assert not any("cancelled live stop" in str(c.args[0]) for c in warn.call_args_list)

    @pytest.mark.asyncio
    async def test_the_tracker_sweep_reports_confirmed_apart_from_requested(self):
        from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
        from signal_engine.tracker import PositionTracker

        tracker = PositionTracker(_make_engine())
        pos = _make_position()
        sweep = StopSweep(
            clear=False, cancelled=("A", "B"), confirmed_cancelled=("A",), unresolved=("B",)
        )
        import signal_engine.tracker as tracker_mod

        with (
            patch.object(
                tracker_mod, "cancel_live_protective_orders", AsyncMock(return_value=sweep)
            ),
            patch.object(tracker_mod.logger, "warning") as warn,
            patch.object(tracker_mod.logger, "error") as err,
        ):
            await tracker._cancel_untracked_stops("k", pos)
        warned = " ".join(str(c.args[0]) for c in warn.call_args_list)
        erred = " ".join(str(c.args[0]) for c in err.call_args_list)
        assert "['A']" in warned and "['A', 'B']" not in warned
        assert "unresolved" in erred and "['B']" in erred
