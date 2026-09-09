"""send_day_summary() must only mark itself done when the summary actually reached Telegram.

Regression guard for 2026-09-09: the 14:45 time-exit fired send_day_summary(), the Telegram
client was not ready yet (a transient startup-timing state, not a real failure), notify()
silently no-op'd, and send_day_summary() STILL wrote the "already sent today" marker - so the
day summary was permanently lost for the rest of the day with no way to retry, and nothing in
the logs said so (the skip was logged at DEBUG, invisible at the file's INFO level).
"""

from datetime import datetime

import pytest

from signal_engine import tracker as tracker_module
from signal_engine.tests.tracker_fixtures import _make_engine
from signal_engine.tracker import PositionTracker


@pytest.fixture(autouse=True)
def isolated_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_module, "_DAY_SUMMARY_MARKER", str(tmp_path / "day_summary"))


def _tracker() -> PositionTracker:
    return PositionTracker(_make_engine())


def _async(return_value):
    async def _fn(**kwargs):
        return return_value
    return _fn


class TestSendDaySummaryMarker:
    @pytest.mark.asyncio
    async def test_successful_send_marks_the_day_done(self, monkeypatch):
        monkeypatch.setattr(tracker_module.notifier, "notify_day_summary", _async(True))
        t = _tracker()

        await t.send_day_summary()

        assert t._day_summary_date == datetime.now(tracker_module.IST).date()
        assert tracker_module._summary_already_sent_today() is True

    @pytest.mark.asyncio
    async def test_failed_send_does_not_mark_the_day_done(self, monkeypatch):
        """The 2026-09-09 bug: notify() returning False (client not ready) must not be
        treated as "delivered"."""
        monkeypatch.setattr(tracker_module.notifier, "notify_day_summary", _async(False))
        t = _tracker()

        await t.send_day_summary()

        assert t._day_summary_date is None
        assert tracker_module._summary_already_sent_today() is False

    @pytest.mark.asyncio
    async def test_a_failed_send_can_be_retried(self, monkeypatch):
        """Because nothing was marked, a second call (e.g. the next poll cycle, or a restart)
        must attempt delivery again rather than silently no-op forever."""
        calls = []

        async def _notify(**kwargs):
            calls.append(1)
            return len(calls) > 1  # fails first time, succeeds second

        monkeypatch.setattr(tracker_module.notifier, "notify_day_summary", _notify)
        t = _tracker()

        await t.send_day_summary()
        assert tracker_module._summary_already_sent_today() is False

        await t.send_day_summary()
        assert tracker_module._summary_already_sent_today() is True
        assert len(calls) == 2
