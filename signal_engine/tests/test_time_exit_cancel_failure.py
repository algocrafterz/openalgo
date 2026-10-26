"""Time exit (14:45): a stop must not be left working after the flatten.

Call path: tracker.time_exit_all -> _square_off_strategies (cancel_all_orders, one retry if it
fails; the order book must then show no working order of the strategy, re-cancelling by id, BEFORE
close_all_positions) -> _verify_positions_closed (re-check each symbol; the same confirmation
before any re-close) -> _confirm_orders_cancelled.

A strategy whose stops cannot be confirmed cancelled is NOT closed (owner decision 2026-10-25): a
close beside a live stop can leave the stop to open a reversed position, and the engine cannot
then know the remaining position. The position keeps its stop and its tracking, and a CRITICAL
alert asks for a manual close before the broker square-off.

close_all_positions is OpenAlgo /closeposition, which is position-based: it reads each live net
quantity and sends one opposite order, so a flat position gets no order and the time exit never
sends a duplicate close. The hazard is a stop left WORKING after the flatten: if it triggers later
it opens a new, reversed position. _confirm_orders_cancelled therefore reads the broker ORDER BOOK
(the cancel API's "success" is not proof), re-cancels while an order of the squared-off strategies
is still working, and raises a critical log and Telegram alert if that cannot be confirmed, even
when the position itself is flat.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker

STRAT = "EVWAP-EARLY-PY"


def _order(status="trigger pending", strategy=STRAT, symbol="PFC", product="MIS"):
    return {
        "orderid": "SL1",
        "symbol": symbol,
        "action": "BUY",
        "pricetype": "SL-M",
        "product": product,
        "order_status": status,
        "strategy": strategy,
    }


async def _time_exit(cancel_results, books, broker_qty=0):
    """books: successive fetch_orderbook results (None = unreadable); the last one repeats."""
    engine = _make_engine()
    engine._state(STRAT).open_positions = 1
    tracker = PositionTracker(engine)
    tracker.register(_make_position(symbol="PFC", strategy=STRAT, product="MIS"))
    cancel = AsyncMock(side_effect=list(cancel_results) + [True] * 10)
    cancel_one = AsyncMock(return_value=True)  # re-cancels of working orders go by order id
    close = AsyncMock(return_value=True)
    seq = list(books)

    async def book():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    with (
        patch("signal_engine.tracker.cancel_all_orders", cancel),
        patch("signal_engine.tracker.cancel_order", cancel_one),
        patch("signal_engine.tracker.close_all_positions", close),
        patch(
            "signal_engine.tracker.fetch_open_position",
            new_callable=AsyncMock,
            return_value=broker_qty,
        ),
        patch("signal_engine.tracker.fetch_orderbook", side_effect=book),
        patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
        patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as notifier,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await tracker.time_exit_all()
    tracker.cancel_one = cancel_one  # test handle on the by-id cancels
    return tracker, cancel, close, notifier


def _alerts(notifier) -> str:
    return " ".join(str(c.args[0]) for c in notifier.notify.await_args_list if c.args)


@pytest.mark.asyncio
class TestTimeExitStopNotLeftWorking:
    async def test_clean_cancel_is_unchanged_one_cancel_one_close(self):
        tracker, cancel, close, notifier = await _time_exit([True], books=[[_order("cancelled")]])
        assert cancel.await_count == 1 and close.await_count == 1
        assert tracker.tracked_count == 0
        assert "not confirmed cancelled" not in _alerts(notifier)

    async def test_api_success_is_not_proof_the_book_decides(self):
        # cancel API says success, but the order book still shows the SL-M working once
        tracker, cancel, close, notifier = await _time_exit(
            [True], books=[[_order()], [_order("cancelled")]]
        )
        assert cancel.await_count == 1
        assert tracker.cancel_one.await_count == 1  # re-cancelled by id: the book disagreed
        assert close.await_count == 1
        assert "not confirmed cancelled" not in _alerts(notifier)

    async def test_stop_still_working_on_a_flat_position_alerts(self):
        # position flat (broker qty 0) yet the stop never leaves the book: no close, alert
        tracker, cancel, close, notifier = await _time_exit(
            [True], books=[[_order()]], broker_qty=0
        )
        assert close.await_count == 0
        assert cancel.await_count == 1
        assert tracker.cancel_one.await_count == 3  # three re-cancels by id, then escalate
        alert = _alerts(notifier)
        assert "not confirmed cancelled" in alert and "id=SL1" in alert

    async def test_cancel_api_failed_but_stop_already_gone_needs_nothing_more(self):
        # the stop filled before the cancel: API fails twice (incl. the pre-close retry), book is clean
        _, cancel, close, notifier = await _time_exit([False, False], books=[[_order("complete")]])
        assert cancel.await_count == 2 and close.await_count == 1
        assert "not confirmed cancelled" not in _alerts(notifier)

    async def test_unreadable_order_book_is_unconfirmed_and_alerts(self):
        tracker, cancel, close, notifier = await _time_exit([True], books=[None])
        assert close.await_count == 0
        assert cancel.await_count == 1 + 3  # no order ids to use: falls back to cancel-all
        assert tracker.cancel_one.await_count == 0
        assert "could not be read" in _alerts(notifier)

    async def test_other_strategies_and_finished_orders_are_ignored(self):
        book = [
            _order(strategy="BREAKOUT-PY", symbol="TCS"),
            _order("rejected"),
            _order("complete"),
        ]
        _, cancel, close, notifier = await _time_exit([True], books=[book])
        assert cancel.await_count == 1
        assert "not confirmed cancelled" not in _alerts(notifier)

    async def test_untagged_mis_stop_on_a_squared_off_symbol_counts_as_ours(self):
        tracker, cancel, close, notifier = await _time_exit([True], books=[[_order(strategy="")]])
        assert cancel.await_count == 1 and tracker.cancel_one.await_count == 3
        assert close.await_count == 0
        assert "not confirmed cancelled" in _alerts(notifier)


@pytest.mark.asyncio
class TestCancelAllSkipsTriggerPending:
    """2026-10-12: OpenAlgo's sandbox cancelallorder filters status "trigger_pending" while it stores
    "trigger pending", so it reports success and leaves every SL-M working. PAYTM's stop then opened a
    long at 15:12. The confirmation step must cancel the working orders it sees by id."""

    async def test_working_stop_is_cancelled_by_its_order_id(self):
        engine = _make_engine()
        engine._state(STRAT).open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="PAYTM", strategy=STRAT, product="MIS"))
        book = {"rows": [_order(symbol="PAYTM") | {"orderid": "26101208861159"}]}

        async def fetch_book():
            return list(book["rows"])

        async def cancel_by_id(order_id, strategy):
            book["rows"] = [o for o in book["rows"] if o["orderid"] != order_id]
            return True

        cancel_all = AsyncMock(return_value=True)  # "success", but cancels nothing (the core bug)
        cancel_one = AsyncMock(side_effect=cancel_by_id)
        with (
            patch("signal_engine.tracker.cancel_all_orders", cancel_all),
            patch("signal_engine.tracker.cancel_order", cancel_one),
            patch(
                "signal_engine.tracker.close_all_positions",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=0
            ),
            patch("signal_engine.tracker.fetch_orderbook", side_effect=fetch_book),
            patch(
                "signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0
            ),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as notifier,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()
        cancel_one.assert_awaited_once_with("26101208861159", STRAT)
        assert book["rows"] == []
        assert "not confirmed cancelled" not in _alerts(notifier)


@pytest.mark.asyncio
class TestNoCloseBesideAnUnconfirmedStop:
    """The close goes out only after the order book shows no working stop of the strategy."""

    async def test_the_stop_is_cancelled_and_confirmed_before_the_close(self):
        calls = []
        rows = [_order()]

        async def fetch_book():
            return list(rows)

        async def cancel_by_id(order_id, strategy):
            calls.append("cancel " + order_id)
            rows.clear()
            return True

        async def close(strategy):
            calls.append("close")
            return True

        engine = _make_engine()
        engine._state(STRAT).open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="PFC", strategy=STRAT, product="MIS"))
        with (
            patch("signal_engine.tracker.cancel_all_orders", AsyncMock(return_value=True)),
            patch("signal_engine.tracker.cancel_order", AsyncMock(side_effect=cancel_by_id)),
            patch("signal_engine.tracker.close_all_positions", AsyncMock(side_effect=close)),
            patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=0),
            patch("signal_engine.tracker.fetch_orderbook", side_effect=fetch_book),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()
        assert calls == ["cancel SL1", "close"]

    async def test_a_skipped_position_keeps_its_stop_and_its_tracking(self):
        tracker, cancel, close, notifier = await _time_exit([True], books=[[_order()]], broker_qty=50)
        assert close.await_count == 0
        assert tracker.cancel_one.await_count == 3  # the pre-close attempts only: no later sweep
        assert tracker.tracked_count == 1  # left for the poll to book the stop or broker close
        alert = _alerts(notifier)
        assert "TIME EXIT SKIPPED" in alert and STRAT in alert and "id=SL1" in alert
        notifier.notify_time_exit.assert_not_awaited()  # no close is claimed

    async def test_one_strategy_skipped_the_other_still_closed(self):
        other = "BREAKOUT-PY"
        engine = _make_engine()
        engine._state(STRAT).open_positions = 1
        engine._state(other).open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="PFC", strategy=STRAT, product="MIS"))
        tracker.register(_make_position(symbol="TCS", strategy=other, product="MIS"))
        close = AsyncMock(return_value=True)

        async def fetch_book():
            return [_order()]  # only STRAT's stop on PFC is stuck

        open_qty = {"PFC": 50, "TCS": 0}

        async def qty(symbol, *_a, **_k):
            return open_qty[symbol]

        with (
            patch("signal_engine.tracker.cancel_all_orders", AsyncMock(return_value=True)),
            patch("signal_engine.tracker.cancel_order", AsyncMock(return_value=True)),
            patch("signal_engine.tracker.close_all_positions", close),
            patch("signal_engine.tracker.fetch_open_position", AsyncMock(side_effect=qty)),
            patch("signal_engine.tracker.fetch_orderbook", side_effect=fetch_book),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()
        assert [c.args[0] for c in close.await_args_list] == [other]
        assert tracker.find_position("PFC", STRAT) is not None
        assert tracker.find_position("TCS", other) is None

    async def test_a_retry_close_also_waits_for_a_confirmed_cancel(self):
        """First close leaves the position open; a stop reappears before the retry: no re-close."""
        books = [[], [_order()]]  # clean before the first close; a stop has landed by the retry
        tracker, cancel, close, notifier = await _time_exit([True], books=books, broker_qty=50)
        assert close.await_count == 1
        assert "TIME EXIT SKIPPED" in _alerts(notifier)
