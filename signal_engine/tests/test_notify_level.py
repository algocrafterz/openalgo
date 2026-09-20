"""Notification volume control for the signal-engine channel.

One entry currently sends three messages (order placed, entry filled, SL placed) and one exit
sends two or more, plus an "exit signal received" ack that reports nothing about an outcome.
At 24 paper trades a day across two strategies that is ~120 messages, in which the ones that
matter — a rejection, a failed SL, a risk lockout — are the easiest to miss.

`notify_level` filters by event. It never filters failures: the whole point of trimming the
routine traffic is that the exceptional traffic becomes visible.
"""

import pytest

from signal_engine.notifier import EVENT_LEVELS, should_notify


class TestLevels:
    def test_quiet_keeps_outcomes_and_drops_acknowledgements(self):
        assert should_notify("entry_filled", "quiet")
        assert should_notify("position_closed", "quiet")
        assert not should_notify("exit_signal_received", "quiet")
        assert not should_notify("order_placed", "quiet")

    def test_normal_is_the_default_middle_ground(self):
        assert should_notify("order_placed", "normal")
        assert should_notify("partial_exit", "normal")
        assert not should_notify("exit_signal_received", "normal")

    def test_verbose_passes_everything(self):
        for event in EVENT_LEVELS:
            assert should_notify(event, "verbose"), event

    def test_failures_are_never_filtered_at_any_level(self):
        """A trimmed channel that can also hide a failed SL would be worse than a noisy one."""
        critical = [
            "order_rejected", "sl_failed", "exit_failed", "risk_limit_hit",
            "orphaned_position", "engine_started", "engine_stopped", "startup_result",
        ]
        for event in critical:
            for level in ("quiet", "normal", "verbose"):
                assert should_notify(event, level), f"{event} suppressed at {level}"

    def test_an_unknown_event_is_delivered_rather_than_dropped(self):
        """A new notify_* added later must appear until someone classifies it, not vanish."""
        assert should_notify("some_future_event", "quiet")

    def test_an_unknown_level_falls_back_to_delivering(self):
        assert should_notify("order_placed", "nonsense")

    def test_day_summary_survives_quiet(self):
        """The one message a quiet channel most needs to keep."""
        assert should_notify("day_summary", "quiet")


class TestWiring:
    @pytest.mark.asyncio
    async def test_suppressed_event_sends_nothing(self, monkeypatch):
        from signal_engine import notifier

        sent = []
        monkeypatch.setattr(notifier, "_client", type("C", (), {
            "send_message": staticmethod(lambda *a, **k: sent.append(a))})())
        monkeypatch.setattr(
            notifier, "settings",
            type("S", (), {"notify_channel": {"analyze": type("N", (), {"id": -1})()},
                           "notify_level": "quiet"})(),
        )
        await notifier.notify("ack", event="exit_signal_received")
        assert sent == []

    @pytest.mark.asyncio
    async def test_permitted_event_is_sent(self, monkeypatch):
        from signal_engine import notifier

        sent = []

        class _Client:
            @staticmethod
            async def send_message(chat_id, text):
                sent.append(text)

        async def _fake_phase():
            return "analyze"

        monkeypatch.setattr(notifier, "_client", _Client())
        monkeypatch.setattr(
            notifier, "settings",
            type("S", (), {"notify_channel": {"analyze": type("N", (), {"id": -1})()},
                           "notify_level": "quiet"})(),
        )
        # notify() resolves the channel via _current_phase(), which otherwise calls OpenAlgo
        # over HTTP - bypass it directly so this test stays offline and deterministic.
        monkeypatch.setattr(notifier, "_current_phase", _fake_phase)
        await notifier.notify("filled", event="entry_filled")
        assert sent == ["[PAPER] filled"]
