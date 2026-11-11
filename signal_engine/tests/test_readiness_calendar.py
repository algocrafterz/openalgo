"""The 09:12 readiness phase must notice that the NSE calendar is about to run out.

Without 2027 in nse_calendar.yaml the Python runner cannot warm up on 2027-01-01, and nothing
said so in advance (finding D-01).
"""

import pytest

from signal_engine.ops import readiness
from signal_engine.ops.model import FAIL, OK, WARN, verdict


class StubCalendar:
    """Weekdays trade, weekends do not, and any date outside the covered years raises."""

    def __init__(self, first=2024, last=2026):
        self.coverage = (first, last)

    def classify(self, d):
        from signal_engine.backtest.data_readiness.calendar import CalendarError

        if not self.coverage[0] <= d.year <= self.coverage[1]:
            raise CalendarError(f"{d} is outside the calendar coverage")
        return "weekend" if d.weekday() >= 5 else "trading"


def test_the_check_is_part_of_the_open_day_phase(monkeypatch):
    from signal_engine.ops.model import Check

    for name in ("_services", "_health", "_engine_startup", "_poller_started", "_clock",
                 "_overnight_errors", "_yesterday_close"):
        monkeypatch.setattr(readiness, name, lambda *a, **k: Check("stub", OK, ""))
    for name in ("_broker_and_mode", "_ports", "_disk_and_databases", "_morning_tasks"):
        monkeypatch.setattr(readiness, name, lambda *a, **k: [])
    monkeypatch.setattr(
        readiness, "_calendar_coverage", lambda day: Check("NSE calendar coverage", WARN, "x")
    )
    written = {}
    monkeypatch.setattr(
        readiness, "write_phase", lambda phase, day, checks, summary: written.update(c=checks)
    )

    readiness.open_day("2026-11-10")

    assert [c.name for c in written["c"]].count("NSE calendar coverage") == 1


def test_green_when_today_next_day_and_next_year_are_not_needed_yet():
    check = readiness._calendar_coverage("2026-11-10", StubCalendar(2024, 2026))

    assert check.status == OK
    assert "2024" in check.evidence and "2026" in check.evidence


def test_green_in_december_when_next_year_is_covered():
    check = readiness._calendar_coverage("2026-12-15", StubCalendar(2024, 2027))

    assert check.status == OK


def test_amber_from_the_first_of_december_when_next_year_is_missing():
    check = readiness._calendar_coverage("2026-12-01", StubCalendar(2024, 2026))

    assert check.status == WARN
    assert "NSE calendar has no 2027 holidays yet" in check.evidence
    assert "before the first 2027 session" in check.evidence
    assert verdict([check]) == "AMBER"


def test_still_green_on_the_last_day_of_november():
    check = readiness._calendar_coverage("2026-11-30", StubCalendar(2024, 2026))

    assert check.status == OK


def test_red_when_today_is_not_covered():
    check = readiness._calendar_coverage("2027-01-04", StubCalendar(2024, 2026))

    assert check.status == FAIL
    assert verdict([check]) == "RED"
    assert "2027" in check.evidence


def test_red_when_the_next_trading_day_is_not_covered():
    # Thursday 2026-12-31: tomorrow is in 2027.
    check = readiness._calendar_coverage("2026-12-31", StubCalendar(2024, 2026))

    assert check.status == FAIL
    assert "next trading day" in check.evidence


def test_red_when_the_calendar_file_cannot_be_read(monkeypatch):
    from signal_engine.backtest.data_readiness import calendar as cal_module

    def broken(*_a, **_k):
        raise cal_module.CalendarError("file is missing")

    monkeypatch.setattr(cal_module, "load_calendar", broken)

    check = readiness._calendar_coverage("2026-11-10")

    assert check.status == FAIL


def test_the_real_calendar_is_loadable_and_green_today_in_november():
    check = readiness._calendar_coverage("2026-11-10")

    assert check.status == OK, check.evidence


@pytest.mark.parametrize("status", [OK, WARN, FAIL])
def test_the_check_has_a_plain_sentence_for_the_report(status):
    from signal_engine.ops.plain import plain_for

    assert plain_for("NSE calendar coverage", status)
