"""W1: list_days includes trading days that have no status file (today, missed past days)."""

from datetime import datetime

import pytest

import blueprints.ops_workflow as mod
from signal_engine.ops import model, workflow_status

pytestmark = pytest.mark.filterwarnings("ignore")

IST = model.IST


def _at(day: str, clock: str) -> datetime:
    return datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)


def _seed(folder, day):
    (folder / f"open-day-{day}-status.tsv").write_text(
        "step\tstarted\tended\texit\ncheck\t09:12:00\t09:12:09\t0\n"
    )


@pytest.fixture
def weekdays_only(monkeypatch):
    """Mon-Fri calendar without reading the real holiday table."""
    monkeypatch.setattr(
        workflow_status,
        "_is_trading_day",
        lambda d: datetime.strptime(d, "%Y-%m-%d").weekday() <= 4,
    )


def test_today_is_listed_without_any_file(tmp_path, weekdays_only):
    # 2026-11-16 is a Monday
    assert mod.list_days(str(tmp_path), now=_at("2026-11-16", "08:00")) == ["2026-11-16"]


def test_today_is_not_listed_on_a_weekend_or_holiday(tmp_path, weekdays_only, monkeypatch):
    assert mod.list_days(str(tmp_path), now=_at("2026-11-15", "10:00")) == []
    monkeypatch.setattr(workflow_status, "_is_trading_day", lambda d: d != "2026-11-16")
    assert mod.list_days(str(tmp_path), now=_at("2026-11-16", "10:00")) == []


def test_missing_folder_still_lists_today(tmp_path, weekdays_only):
    gone = str(tmp_path / "nope")
    assert mod.list_days(gone, now=_at("2026-11-16", "08:00")) == ["2026-11-16"]


def test_a_missed_past_trading_day_in_the_window_is_listed(tmp_path, weekdays_only):
    _seed(tmp_path, "2026-11-09")  # Mon
    _seed(tmp_path, "2026-11-12")  # Thu
    days = mod.list_days(str(tmp_path), now=_at("2026-11-16", "16:00"))
    # Tue 10, Wed 11 and Fri 13 never ran; the weekend is skipped; today is added.
    assert days == [
        "2026-11-16",
        "2026-11-13",
        "2026-11-12",
        "2026-11-11",
        "2026-11-10",
        "2026-11-09",
    ]


def test_days_before_the_oldest_file_and_outside_the_window_are_not_invented(
    tmp_path, weekdays_only
):
    _seed(tmp_path, "2026-11-12")
    days = mod.list_days(str(tmp_path), now=_at("2026-11-16", "08:00"))
    assert days == ["2026-11-16", "2026-11-13", "2026-11-12"]
    _seed(tmp_path, "2026-09-01")  # old file: the window still bounds the additions
    days = mod.list_days(str(tmp_path), now=_at("2026-11-16", "08:00"), limit=100)
    assert "2026-10-01" not in days and "2026-11-02" in days and "2026-11-01" not in days
    assert "2026-10-30" not in days  # older than 14 calendar days from 2026-11-16 (Nov 2)


def test_a_calendar_failure_never_raises(tmp_path, monkeypatch):
    _seed(tmp_path, "2026-11-12")

    def boom(day):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(workflow_status, "is_trading_day", boom)
    assert mod.list_days(str(tmp_path), now=_at("2026-11-16", "08:00")) == ["2026-11-12"]


def test_the_real_calendar_helper_falls_back_to_weekdays(monkeypatch):
    from signal_engine.ops import automation

    def boom(day):
        raise RuntimeError("no db")

    monkeypatch.setattr(automation, "is_trading_day", boom)
    assert workflow_status.is_trading_day("2026-11-16") is True
    assert workflow_status.is_trading_day("2026-11-15") is False


def test_day_view_of_a_day_with_no_files_reads_missed_and_not_due(tmp_path, weekdays_only):
    early = mod.day_view("2026-11-16", str(tmp_path), now=_at("2026-11-16", "09:30"))
    states = {r["phase"]: r["state"] for r in early["run_status"]}
    assert states == {"open-day": "missed", "liveness": "not_due", "close-day": "not_due"}
    late = mod.day_view("2026-11-13", str(tmp_path), now=_at("2026-11-16", "09:30"))
    assert {r["state"] for r in late["run_status"]} == {"missed"}
    weekend = mod.day_view("2026-11-15", str(tmp_path), now=_at("2026-11-16", "09:30"))
    assert {r["state"] for r in weekend["run_status"]} == {"not_expected"}
    assert early["review"]["cause"] == "no_report"
