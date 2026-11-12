"""Critical data problems are loud, sent once per day, and announce recovery once."""

from __future__ import annotations

from datetime import datetime, timedelta

from signal_engine.analysis.breakingtrade.critical import (
    CriticalNotifier,
    format_critical,
)


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 11, 10, 0)

    def __call__(self):
        return self.now

    def advance(self, minutes: int):
        self.now += timedelta(minutes=minutes)


def _notifier():
    sent = []
    clock = _Clock()
    return CriticalNotifier(deliver=sent.append, clock=clock), sent, clock


def test_banner_is_unmissable_and_plain_text():
    text = format_critical("no fresh data", "Poll 10:05 failed.", "sign in by hand")
    assert text.startswith("!!!!") and text.rstrip().endswith("!!!!")
    assert "CRITICAL" in text and "NO FRESH DATA" in text and "ACTION NEEDED: sign in" in text
    assert text.isascii()


def test_issue_sends_once_per_day_then_again_next_day():
    notifier, sent, clock = _notifier()
    assert notifier.raise_issue("login", "t", "d", "a") is True
    clock.advance(60)
    assert notifier.raise_issue("login", "t", "d", "a") is False
    clock.advance(5 * 60)  # still 2026-11-11
    assert notifier.raise_issue("login", "t", "d", "a") is False
    assert len(sent) == 1
    clock.advance(24 * 60)  # next day
    assert notifier.raise_issue("login", "t", "d", "a") is True
    assert len(sent) == 2


def test_flapping_issue_stays_quiet_after_first_alert_of_the_day():
    notifier, sent, clock = _notifier()
    notifier.raise_issue("login", "t", "d", "a")
    assert notifier.clear("login", "back") is True
    clock.advance(30)
    assert notifier.raise_issue("login", "t", "d", "a") is False
    assert notifier.clear("login", "back") is False  # no alert was sent, so no recovery
    assert len(sent) == 2  # banner + one RECOVERED


def test_different_issues_do_not_suppress_each_other():
    notifier, sent, _ = _notifier()
    notifier.raise_issue("login", "t", "d", "a")
    notifier.raise_issue("format", "t", "d", "a")
    assert len(sent) == 2 and notifier.active() == ["format", "login"]


def test_clear_announces_recovery_once():
    notifier, sent, _ = _notifier()
    assert notifier.clear("login", "ok") is False  # nothing was reported
    notifier.raise_issue("login", "t", "d", "a")
    assert notifier.clear("login", "polls are back") is True
    assert notifier.clear("login", "polls are back") is False
    assert sent[-1].startswith("RECOVERED")


def test_delivery_failure_never_raises():
    def boom(_text):
        raise RuntimeError("telegram down")

    notifier = CriticalNotifier(deliver=boom, clock=_Clock())
    assert notifier.raise_issue("x", "t", "d", "a") is True
