"""The day summary must arrive exactly once per session, and must not lie afterwards.

Two ways it currently goes wrong, both ending in a FALSE "No trades taken today." landing
after the real summary — which is worse than no summary at all during a week whose entire
purpose is the daily review.

1. `time_exit_all()` sends the summary and then calls `_reset_day_counters()`, which sets
   `_day_summary_sent = False` and zeroes the counters. The one-shot guard is dead for the
   rest of the day, so any later full-close path that reaches `maybe_send_day_summary()`
   (positions empty, within 30 min of the time exit) sends a second summary reading zero.

2. The watchdog restarts the engine every 5 minutes until 15:25. A restart after 14:45 gives
   a fresh scheduler with `_fired_today = False`, whose catch-up branch fires `time_exit_all()`
   immediately — against counters that are empty because the process just started.
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    from signal_engine.risk_store import RiskStore
    from signal_engine.runtime import build_risk_engine
    from signal_engine.tracker import PositionTracker

    monkeypatch.setattr(
        "signal_engine.tracker._DAY_SUMMARY_MARKER", str(tmp_path / "day_summary")
    )
    engine = build_risk_engine(RiskStore(db_path=str(tmp_path / "risk.db")))
    t = PositionTracker(engine)
    t._day_trades, t._day_wins, t._day_pnl = 3, 2, 1500.0
    return t


class TestSendsOncePerDay:
    @pytest.mark.asyncio
    async def test_first_call_sends(self, tracker):
        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as n:
            await tracker.send_day_summary()
            assert n.notify_day_summary.await_count == 1

    @pytest.mark.asyncio
    async def test_second_call_same_day_is_suppressed(self, tracker):
        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as n:
            await tracker.send_day_summary()
            await tracker.send_day_summary()
            assert n.notify_day_summary.await_count == 1

    @pytest.mark.asyncio
    async def test_counter_reset_does_not_re_arm_the_summary(self, tracker):
        """The regression. time_exit_all() sends, then resets counters; if that reset also
        clears the guard, the next empty-book moment sends a zeroed summary that contradicts
        the real one minutes earlier."""
        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as n:
            await tracker.send_day_summary()
            tracker._reset_day_counters()
            await tracker.send_day_summary()
            assert n.notify_day_summary.await_count == 1

    @pytest.mark.asyncio
    async def test_a_new_day_sends_again(self, tracker, tmp_path):
        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as n:
            await tracker.send_day_summary()
            # A new day moves BOTH guards on: the in-process date and the marker written
            # by the previous session.
            tracker._day_summary_date = date.today() - timedelta(days=1)
            with open(str(tmp_path / "day_summary"), "w") as fh:
                fh.write((date.today() - timedelta(days=1)).isoformat())
            await tracker.send_day_summary()
            assert n.notify_day_summary.await_count == 2


class TestSurvivesRestart:
    @pytest.mark.asyncio
    async def test_a_restart_after_the_summary_does_not_send_a_second(self, tracker, tmp_path):
        """In-memory state is lost on restart, so the guard has to outlive the process or
        the watchdog's 14:50 restart reports 'No trades taken today' over a real summary."""
        from signal_engine.risk_store import RiskStore
        from signal_engine.runtime import build_risk_engine
        from signal_engine.tracker import PositionTracker

        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as n:
            await tracker.send_day_summary()

            restarted = PositionTracker(build_risk_engine(RiskStore(db_path=str(tmp_path / "r2.db"))))
            await restarted.send_day_summary()

            assert n.notify_day_summary.await_count == 1

    @pytest.mark.asyncio
    async def test_a_restart_on_a_later_day_still_sends(self, tracker, tmp_path):
        from signal_engine.risk_store import RiskStore
        from signal_engine.runtime import build_risk_engine
        from signal_engine.tracker import PositionTracker

        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as n:
            await tracker.send_day_summary()
            with open(str(tmp_path / "day_summary"), "w") as fh:
                fh.write((date.today() - timedelta(days=1)).isoformat())

            restarted = PositionTracker(build_risk_engine(RiskStore(db_path=str(tmp_path / "r2.db"))))
            await restarted.send_day_summary()
            assert n.notify_day_summary.await_count == 2

    @pytest.mark.asyncio
    async def test_an_unreadable_marker_sends_rather_than_stays_silent(self, tracker, tmp_path):
        """A corrupt marker must not be able to suppress the summary indefinitely."""
        with open(str(tmp_path / "day_summary"), "w") as fh:
            fh.write("not-a-date")
        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as n:
            await tracker.send_day_summary()
            assert n.notify_day_summary.await_count == 1
