"""notify_level `alerts`: the staged Telegram trim (2026-10-23).

Per-trade outcomes and the day summary are on the OpenAlgo GUI (Strategy P&L, Daily
Performance), but the GUI is pull-only and down whenever the stack is. `alerts` therefore drops
every event in notifier.EVENT_LEVELS (outcomes and steps; by design that table holds no failure)
and keeps everything else: failures, operational alerts and any event nobody classified. The
inbound signal path (listener.py) does not go through the notifier and is untouched.
"""

import pytest

from signal_engine import notifier
from signal_engine import tracker as tracker_module
from signal_engine.notifier import EVENT_LEVELS, should_notify
from signal_engine.tests.tracker_fixtures import _make_engine
from signal_engine.tracker import PositionTracker


class TestTheAlertsLevel:
    def test_every_outcome_and_step_event_is_suppressed(self):
        assert EVENT_LEVELS  # the table is the definition of "routine"
        assert not any(should_notify(event, "alerts") for event in EVENT_LEVELS)

    @pytest.mark.parametrize("event", [
        "sl_failed", "exit_failed", "order_rejected", "orphaned_position", "risk_limit_hit",
        "listener_down", "listener_degraded", "positionbook_outage", "reconciliation_mismatch",
        "runner_warmup_rejected", "", "an_event_added_later",
    ])
    def test_failures_and_unclassified_events_still_go_out(self, event):
        assert should_notify(event, "alerts")

    def test_quiet_is_unchanged(self):
        assert should_notify("position_closed", "quiet") and should_notify("day_summary", "quiet")
        assert not should_notify("order_placed", "quiet")


def _fake_channels(monkeypatch, level):
    sent = []

    class _Client:
        @staticmethod
        async def send_message(chat_id, text):
            sent.append((chat_id, text))

    async def _phase():
        return "analyze"

    channel = type("N", (), {"id": -1, "name": "intraday-orb-analyze"})()
    monkeypatch.setattr(notifier, "_client", _Client())
    monkeypatch.setattr(notifier, "_current_phase", _phase)
    monkeypatch.setattr(notifier, "_channel_for_strategy", lambda strategy, phase: channel)
    monkeypatch.setattr(
        notifier, "settings",
        type("S", (), {"notify_channel": {"analyze": type("N", (), {"id": -2})()},
                       "notify_level": level})(),
    )
    return sent


class TestStrategyChannelMirror:
    @pytest.mark.asyncio
    async def test_an_outcome_is_not_mirrored_at_alerts(self, monkeypatch):
        sent = _fake_channels(monkeypatch, "alerts")
        await notifier.notify_event("entry_filled", "filled", strategy="ORB")
        assert sent == []

    @pytest.mark.asyncio
    async def test_a_failure_is_still_mirrored_at_alerts(self, monkeypatch):
        sent = _fake_channels(monkeypatch, "alerts")
        await notifier.notify_event("sl_failed", "stop failed", strategy="ORB")
        assert {chat for chat, _ in sent} == {-1, -2}  # strategy channel and admin channel

    @pytest.mark.asyncio
    async def test_quiet_still_mirrors_outcomes(self, monkeypatch):
        sent = _fake_channels(monkeypatch, "quiet")
        await notifier.notify_event("entry_filled", "filled", strategy="ORB")
        assert {chat for chat, _ in sent} == {-1, -2}


class TestDaySummaryAtAlerts:
    @pytest.fixture(autouse=True)
    def _marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tracker_module, "_DAY_SUMMARY_MARKER", str(tmp_path / "day_summary"))

    @pytest.mark.asyncio
    async def test_the_summary_is_skipped_marked_and_the_canary_still_runs(self, monkeypatch):
        from signal_engine import reconcile

        calls = []

        async def _never(**kw):
            calls.append("send")
            return True

        async def _canary():
            calls.append("canary")

        monkeypatch.setattr(notifier, "day_summary_enabled", lambda: False)
        monkeypatch.setattr(notifier, "notify_day_summary", _never)
        monkeypatch.setattr(reconcile, "check_and_alert", _canary)
        t = PositionTracker(_make_engine())

        await t.send_day_summary()

        assert calls == ["canary"]
        assert tracker_module._summary_already_sent_today() is True
