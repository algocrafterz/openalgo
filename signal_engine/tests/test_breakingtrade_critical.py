"""Critical data problems are loud, repeat until fixed, and announce recovery once."""

from __future__ import annotations

from datetime import datetime, timedelta

from signal_engine.analysis.breakingtrade.critical import (
    REPEAT_MINUTES,
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


def test_first_issue_sends_and_repeats_only_after_interval():
    notifier, sent, clock = _notifier()
    assert notifier.raise_issue("login", "t", "d", "a") is True
    clock.advance(REPEAT_MINUTES - 1)
    assert notifier.raise_issue("login", "t", "d", "a") is False
    clock.advance(2)
    assert notifier.raise_issue("login", "t", "d", "a") is True
    assert len(sent) == 2


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
