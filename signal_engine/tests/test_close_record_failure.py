"""F1: a close is counted exactly once, even when recording it fails.

check_positions() survives an exception from one position and polls again, so a failure after
record_close() used to book the same close again every poll. The risk counters must also survive
a store that cannot be written (memory is authoritative for the session).
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import tracker as tracker_module
from signal_engine.risk_store import RiskStore
from signal_engine.strategies import ORB
from signal_engine.tests.risk_fixtures import _engine as _risk_engine
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker

_FLAT_BOOK = [{"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 0}]


@pytest.mark.asyncio
async def test_record_failure_after_record_close_counts_the_close_once(logged):
    engine = _make_engine()
    engine._state(ORB).open_positions = 1
    t = PositionTracker(engine)
    t._last_realised_pnl = 1000.0
    t.register(_make_position(fill_price=2500.0))
    real_record_close = engine.record_close
    engine.record_close = MagicMock(side_effect=real_record_close)
    t._record_closed_trade = AsyncMock(side_effect=[RuntimeError("disk full"), None])

    with (
        patch.object(
            tracker_module, "fetch_positionbook", AsyncMock(return_value=_FLAT_BOOK)
        ),
        patch.object(
            tracker_module, "fetch_realised_pnl", AsyncMock(return_value=500.0)
        ),
    ):
        # start() survives a failed poll, so a raise here must not stop the second poll.
        with contextlib.suppress(RuntimeError):
            await t.check_positions()
        with contextlib.suppress(RuntimeError):
            await t.check_positions()

    assert engine.record_close.call_count == 1
    assert t.tracked_count == 0
    assert engine._state(ORB).open_positions == 0
    assert engine._state(ORB).daily_realised_loss == 500.0  # counted once, not twice
    # The former Telegram alert is now this ERROR line, logged once.
    lines = [m for m in logged.messages("ERROR") if "Tracker poll alert (close_record_failed)" in m]
    assert len(lines) == 1
    text = lines[0]
    assert "RELIANCE" in text and "disk full" not in text and "RuntimeError" not in text


@pytest.mark.asyncio
async def test_cancelled_error_in_recording_is_not_swallowed():
    import asyncio

    engine = _make_engine()
    engine._state(ORB).open_positions = 1
    t = PositionTracker(engine)
    t._last_realised_pnl = 1000.0
    t.register(_make_position())
    t._record_closed_trade = AsyncMock(side_effect=asyncio.CancelledError)
    with (
        patch.object(
            tracker_module, "fetch_positionbook", AsyncMock(return_value=_FLAT_BOOK)
        ),
        patch.object(
            tracker_module, "fetch_realised_pnl", AsyncMock(return_value=500.0)
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await t.check_positions()


class _BrokenStore(RiskStore):
    def save(self, *a, **k):
        raise RuntimeError("database is locked")


def _engine_with_broken_store(tmp_path):
    store = _BrokenStore(str(tmp_path / "risk.db"))
    return _risk_engine(store=store)


def test_record_close_with_failing_store_does_not_raise(tmp_path):
    engine = _engine_with_broken_store(tmp_path)
    engine.record_trade(ORB, "RELIANCE")
    engine.record_close(-250.0, strategy=ORB, symbol="RELIANCE")
    state = engine._state(ORB)
    assert state.daily_realised_loss == 250.0
    assert state.open_positions == 0


def test_record_trade_and_rejection_with_failing_store_do_not_raise(tmp_path):
    engine = _engine_with_broken_store(tmp_path)
    engine.record_trade(ORB, "RELIANCE")
    assert engine._state(ORB).trades_today == 1
    engine.record_rejection(ORB, "RELIANCE")
    assert engine._state(ORB).trades_today == 0


def test_store_failure_is_logged_at_most_once_per_minute_per_strategy(tmp_path):
    engine = _engine_with_broken_store(tmp_path)
    with patch("signal_engine.risk.logger") as log:
        engine.record_trade(ORB, "A")
        engine.record_trade(ORB, "B")
        engine.record_trade(ORB, "C")
    assert log.exception.call_count == 1


# --- G8b: the other two sites that count a close and then keep recording ---------------------


def _one_position_tracker(**pos_kw):
    engine = _make_engine()
    engine._state(ORB).open_positions = 1
    real_record_close = engine.record_close
    engine.record_close = MagicMock(side_effect=real_record_close)
    t = PositionTracker(engine)
    t._last_realised_pnl = 0.0
    pos = _make_position(fill_price=2500.0, **pos_kw)
    t.register(pos)
    return engine, t, pos


@pytest.mark.asyncio
async def test_no_progress_close_whose_record_fails_is_counted_once_and_unregistered(logged):
    from datetime import timedelta

    from signal_engine.models import OrderStatus, TradeResult

    engine, t, pos = _one_position_tracker()
    pos.sl_order_id = "SL_OLD"
    t.book_close = AsyncMock(side_effect=RuntimeError("disk full"))
    t.maybe_send_day_summary = AsyncMock()
    ok = TradeResult(order_id="EXNP", status=OrderStatus.SUCCESS, message="ok")
    with (
        patch("signal_engine.main._send_exit_with_retries", AsyncMock(return_value=ok)),
        patch.object(tracker_module, "fetch_order_fill_price", AsyncMock(return_value=2490.0)),
        patch.object(tracker_module, "fetch_realised_pnl", AsyncMock(return_value=-100.0))
    ):
        await t._no_progress_market_exit(pos, timedelta(minutes=40), 2490.0, 2500.0, 0.1)
    assert engine.record_close.call_count == 1
    assert t.tracked_count == 0  # not left registered to be booked again
    # The former Telegram alert is now this ERROR line, logged once.
    lines = [m for m in logged.messages("ERROR") if "Tracker poll alert (close_record_failed)" in m]
    assert len(lines) == 1
    text = lines[0]
    assert "RELIANCE" in text and "disk full" not in text and "RuntimeError" not in text


@pytest.mark.asyncio
async def test_a_confirmed_time_exit_is_counted_once_and_removed(logged):
    from types import SimpleNamespace

    engine, t, pos = _one_position_tracker()
    t._save_close_row = MagicMock()
    key = t._key(pos.symbol, pos.strategy)
    await t._book_one_time_exit(
        key, pos, 0.0, SimpleNamespace(max_trades_per_day=5), confirmed_closed=True
    )
    assert engine.record_close.call_count == 1
    assert t.tracked_count == 0
    # A confirmed close that books cleanly logs no record-failure line.
    assert logged.count("close_record_failed", "ERROR") == 0
