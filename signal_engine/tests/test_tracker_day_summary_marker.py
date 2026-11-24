"""send_day_summary() marks the trading day done, exactly as it did before the Telegram removal.

History: on 2026-09-09 the marker was written even though the Telegram summary never left, so
the day was lost with nothing to retry. The send then gated the marker. Since 2026-10-31 the
configured notify level suppressed the summary, and the send path treated that as delivered, so
production always wrote the marker. Outgoing Telegram notifications were removed in November
2026 and that production behaviour was kept: the marker is written whenever send_day_summary()
runs and nothing newer is already marked. These tests pin that behaviour. No Telegram send can
happen (the autouse conftest fixture refuses any real one).
"""

from datetime import datetime

import pytest

from signal_engine import tracker as tracker_module
from signal_engine.tests.tracker_fixtures import _make_engine
from signal_engine.tracker import PositionTracker


@pytest.fixture(autouse=True)
def isolated_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_module, "_DAY_SUMMARY_MARKER", str(tmp_path / "day_summary"))


@pytest.fixture
def canary(monkeypatch):
    """Count the reconciliation canary runs; the summary path must keep calling it."""
    from signal_engine import reconcile

    calls: list[int] = []

    async def _canary():
        calls.append(1)

    monkeypatch.setattr(reconcile, "check_and_alert", _canary)
    return calls


def _tracker() -> PositionTracker:
    return PositionTracker(_make_engine())


class TestSendDaySummaryMarker:
    @pytest.mark.asyncio
    async def test_the_summary_step_marks_the_day_done(self, canary):
        t = _tracker()

        await t.send_day_summary()

        assert t._day_summary_date == datetime.now(tracker_module.IST).date()
        assert tracker_module._summary_already_sent_today() is True
        assert canary == [1]

    @pytest.mark.asyncio
    async def test_a_second_call_with_nothing_new_is_a_no_op(self, canary):
        t = _tracker()

        await t.send_day_summary()
        await t.send_day_summary()

        assert tracker_module._summary_already_sent_today() is True
        assert canary == [1]  # the canary does not run again for the same day

    @pytest.mark.asyncio
    async def test_a_restart_reads_the_marker_and_does_not_mark_again(self, canary):
        await _tracker().send_day_summary()

        restarted = _tracker()  # a new process: in-memory state is empty, the file is not
        await restarted.send_day_summary()

        assert restarted._day_summary_date is None
        assert canary == [1]
