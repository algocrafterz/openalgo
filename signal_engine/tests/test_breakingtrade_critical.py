"""Critical data problems are loud, recorded once per day, and record recovery once.

Nothing is sent to Telegram: a banner is logged and recorded (the `record` callable)."""

from __future__ import annotations

from datetime import datetime, timedelta

from signal_engine.analysis.breakingtrade.critical import (
    CriticalIssues,
    format_critical,
)


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 11, 10, 0)

    def __call__(self):
        return self.now

    def advance(self, minutes: int):
        self.now += timedelta(minutes=minutes)


def _issues():
    sent = []
    clock = _Clock()
    return CriticalIssues(record=sent.append, clock=clock), sent, clock


def test_banner_is_unmissable_and_plain_text():
    text = format_critical("no fresh data", "Poll 10:05 failed.", "sign in by hand")
    assert text.startswith("!!!!") and text.rstrip().endswith("!!!!")
    assert "CRITICAL" in text and "NO FRESH DATA" in text and "ACTION NEEDED: sign in" in text
    assert text.isascii()


def test_issue_is_recorded_once_per_day_then_again_next_day():
    issues, sent, clock = _issues()
    assert issues.raise_issue("login", "t", "d", "a") is True
    clock.advance(60)
    assert issues.raise_issue("login", "t", "d", "a") is False
    clock.advance(5 * 60)  # still 2026-11-11
    assert issues.raise_issue("login", "t", "d", "a") is False
    assert len(sent) == 1
    clock.advance(24 * 60)  # next day
    assert issues.raise_issue("login", "t", "d", "a") is True
    assert len(sent) == 2


def test_flapping_issue_stays_quiet_after_first_banner_of_the_day():
    issues, sent, clock = _issues()
    issues.raise_issue("login", "t", "d", "a")
    assert issues.clear("login", "back") is True
    clock.advance(30)
    assert issues.raise_issue("login", "t", "d", "a") is False
    assert issues.clear("login", "back") is False  # no banner was recorded, so no recovery
    assert len(sent) == 2  # banner + one RECOVERED


def test_different_issues_do_not_suppress_each_other():
    issues, sent, _ = _issues()
    issues.raise_issue("login", "t", "d", "a")
    issues.raise_issue("format", "t", "d", "a")
    assert len(sent) == 2 and issues.active() == ["format", "login"]


def test_clear_announces_recovery_once():
    issues, sent, _ = _issues()
    assert issues.clear("login", "ok") is False  # nothing was reported
    issues.raise_issue("login", "t", "d", "a")
    assert issues.clear("login", "polls are back") is True
    assert issues.clear("login", "polls are back") is False
    assert sent[-1].startswith("RECOVERED")


def test_a_failed_record_never_raises_and_is_logged(logged):
    def boom(_text):
        raise RuntimeError("alerts table unavailable")

    issues = CriticalIssues(record=boom, clock=_Clock())
    assert issues.raise_issue("x", "t", "d", "a") is True
    assert logged.has("CRITICAL t: d", "ERROR")  # the banner is logged whether or not it is stored
    assert logged.has("could not record the critical issue", "ERROR")
