"""Nothing user-facing may be sourced from counters a restart wipes.

The EOD summary was moved to trades.db already. day_context_line() was the remaining one -
the "Day: 4/10 trades (W:3 L:1) | P&L: +Rs 986" line that rides on EVERY close notification.
It read _day_trades / _day_wins / _day_pnl, all of which PositionTracker.__init__ sets to
zero, so after a restart every close message understated the day. On 2026-09-11 the engine
restarted twice.

The in-memory counters remain, but only as an internal running tally and as a LABELLED
fallback: if trades.db cannot be read, a stale number presented as the day is worse than
saying so.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import db
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.tracker import PositionTracker


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
    db.reset_connection()
    db.set_trade_mode("analyze")
    yield
    db.reset_connection()


def _closed(symbol, pnl, types=("TP1",)):
    db.save(
        Signal(strategy="ORB", direction=Direction.LONG, symbol=symbol,
               entry=800.0, sl=796.0, tp=810.0, raw_message="x"),
        Order(symbol=symbol, exchange="NSE", action=Action.BUY, quantity=10, price=0.0,
              order_type="MARKET", product="MIS", strategy_tag="ORB"),
        TradeResult(status=OrderStatus.SUCCESS, order_id=f"E-{symbol}"),
    )
    db.save_tracker_exit(strategy="ORB", symbol=symbol, entry=800.0, sl=796.0, tp=810.0,
                         quantity=10, exit_price=805.0, pnl=pnl, exit_types=list(types))


def _tracker():
    return PositionTracker(MagicMock())


class TestDayContextComesFromTheDatabase:
    def test_a_restart_does_not_reset_the_running_day(self):
        """The tracker below is brand new - exactly the post-restart state."""
        _closed("SBIN", 50.0)
        _closed("TCS", -20.0)
        line = _tracker().day_context_line()
        assert "2 trades" in line
        assert "W:1" in line and "L:1" in line

    def test_the_pnl_shown_is_the_database_total(self):
        _closed("SBIN", 500.0)
        assert "500" in _tracker().day_context_line()

    def test_the_trade_cap_is_still_rendered(self):
        _closed("SBIN", 50.0)
        assert "1/10" in _tracker().day_context_line(max_trades=10)

    def test_a_time_exit_counts_as_a_trade_but_not_a_win(self):
        _closed("SBIN", 50.0, types=("TIME",))
        line = _tracker().day_context_line()
        assert "1 trades" in line and "W:0" in line and "L:0" in line

    def test_an_empty_day_reads_as_zero(self):
        assert "0 trades" in _tracker().day_context_line()

    def test_an_unreadable_database_does_not_raise(self):
        with patch("signal_engine.db.fetch_day_trades", side_effect=RuntimeError("locked")):
            _tracker().day_context_line()


class TestADegradedSummarySaysSo:
    @pytest.mark.asyncio
    async def test_the_fallback_labels_itself_rather_than_posing_as_the_day(self):
        """A stale number presented as the day is worse than an explicit gap."""
        tracker = _tracker()
        tracker._day_trades, tracker._day_pnl, tracker._day_wins = 2, 75.0, 2
        with patch("signal_engine.db.fetch_day_trades", side_effect=RuntimeError("locked")), \
             patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await tracker.send_day_summary()
        assert notify.await_args.kwargs["degraded"] is True

    @pytest.mark.asyncio
    async def test_a_healthy_summary_is_not_labelled(self):
        _closed("SBIN", 50.0)
        with patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await _tracker().send_day_summary()
        assert notify.await_args.kwargs["degraded"] is False

    def test_the_warning_is_rendered_into_the_message(self):
        from signal_engine import notifier

        lines = notifier._day_summary_header(
            "11-Sep-2026", trades=2, wins=2, losses=0, net_pnl=75.0, capital=100_000,
            time_exits=0, trade_records=[], degraded=True,
        )
        assert any("INCOMPLETE" in ln for ln in lines)


class TestStartupMessagesAreNotDropped:
    """Startup reconciliation runs before listener.set_client(), so its close notifications
    went straight to the floor - all four of 2026-09-11's closes, which is why nothing
    appeared in the admin channel for the day's trades."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from signal_engine import notifier

        notifier._pending.clear()
        notifier._client = None
        yield
        notifier._pending.clear()
        notifier._client = None

    @pytest.mark.asyncio
    async def test_a_message_raised_before_connect_is_queued(self):
        from signal_engine import notifier

        await notifier.notify_event("position_closed", "CLOSED | SBIN")
        assert len(notifier._pending) == 1

    @pytest.mark.asyncio
    async def test_the_queue_is_delivered_on_connect(self):
        from signal_engine import notifier

        await notifier.notify_event("position_closed", "CLOSED | SBIN")
        with patch.object(notifier, "notify", AsyncMock(return_value=True)) as send:
            notifier._client = MagicMock()
            assert await notifier.flush_pending() == 1
        send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flushing_twice_does_not_resend(self):
        from signal_engine import notifier

        await notifier.notify_event("position_closed", "CLOSED | SBIN")
        with patch.object(notifier, "notify", AsyncMock(return_value=True)):
            notifier._client = MagicMock()
            await notifier.flush_pending()
            assert await notifier.flush_pending() == 0

    @pytest.mark.asyncio
    async def test_the_queue_is_bounded(self):
        from signal_engine import notifier

        for i in range(notifier._PENDING_LIMIT + 20):
            await notifier.notify_event("position_closed", f"msg {i}")
        assert len(notifier._pending) == notifier._PENDING_LIMIT
