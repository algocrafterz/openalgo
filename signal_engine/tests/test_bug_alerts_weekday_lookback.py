"""Weekday-aware alert lookback: N business days, weekends and holidays do not count.

All paths are tmp paths; the NSE calendar is replaced, never read from the real database.
"""

import pytest

from signal_engine.bugs import store
from signal_engine.ops import alerts, automation, bugalerts

pytestmark = pytest.mark.filterwarnings("ignore")
THU, FRI, SAT, SUN, MON, TUE, WED = (
    "2026-11-12",
    "2026-11-13",
    "2026-11-14",
    "2026-11-15",
    "2026-11-16",
    "2026-11-17",
    "2026-11-18",
)


def _bug(path, title, seen):
    bug = store.add(
        title=title, kind="BUG", severity="HIGH", area="x", seen=seen, impact="i", path=path
    )
    data = store.load(path)
    for b in data["bugs"]:
        if b["id"] == bug["id"]:
            b["created_at"] = f"{seen}T09:00:00+05:30"
    store._save(data, path)
    return bug


@pytest.fixture(autouse=True)
def _weekdays_only(monkeypatch):
    monkeypatch.setattr(automation, "DB", "/nonexistent/none.db")  # unreadable: weekday rule
    monkeypatch.setattr(bugalerts, "_is_business_day", lambda d: d.weekday() < 5)


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "deliver", lambda text, **kw: calls.append(kw["condition"]) or True)
    return calls


def _new_only(conditions):
    """Drop the weekly overdue/unverified keys (they carry ':<week>'); keep new-bug alerts."""
    return [c for c in conditions if ":" not in c]


def _new(day, path):
    return [e for e in bugalerts.events(day, path) if e[0] == "bug-new"]


def test_constant_is_three_business_days():
    assert bugalerts.LOOKBACK_BUSINESS_DAYS == 3


def test_friday_failed_send_is_delivered_on_monday(tmp_path, monkeypatch):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck", FRI)
    monkeypatch.setattr(alerts, "deliver", lambda *a, **k: False)
    bugalerts.run(FRI, dry_run=False, path=path, folder=folder)
    calls = []
    monkeypatch.setattr(alerts, "deliver", lambda t, **kw: calls.append(kw["condition"]) or True)
    bugalerts.run(MON, dry_run=False, path=path, folder=folder)
    assert _new_only(calls) == ["BUG-001"]


def test_friday_capped_alerts_are_delivered_on_monday(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    for i in range(7):
        _bug(path, f"High bug number {i}", FRI)
    bugalerts.run(FRI, dry_run=False, path=path, folder=folder)
    assert len(sent) == 5
    bugalerts.run(MON, dry_run=False, path=path, folder=folder)
    assert len(_new_only(sent)) == 7 and len(set(_new_only(sent))) == 7


@pytest.mark.parametrize("weekend_day", [SAT, SUN])
def test_weekend_runs_see_friday_and_weekend_events(tmp_path, weekend_day):
    path = str(tmp_path / "b.json")
    _bug(path, "Friday bug", FRI)
    _bug(path, "Saturday bug", SAT)
    found = {e[1] for e in _new(weekend_day, path)}
    assert found == {"BUG-001", "BUG-002"}


def test_weekend_event_is_still_new_on_monday(tmp_path):
    path = str(tmp_path / "b.json")
    _bug(path, "Sunday bug", SUN)
    assert _new(MON, path) != []


def test_event_four_business_days_old_is_not_sent(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Thursday bug", THU)
    assert _new(MON, path) != []  # Mon, Fri, Thu = 3 business days
    assert _new(TUE, path) == []  # Tue, Mon, Fri: Thursday is the 4th business day back
    assert _new(WED, path) == []
    bugalerts.run(TUE, dry_run=False, path=path, folder=folder)
    assert _new_only(sent) == []


def test_four_business_days_old_over_a_weekend_is_dropped(tmp_path):
    path = str(tmp_path / "b.json")
    _bug(path, "Wednesday bug", "2026-11-11")
    assert _new(SUN, path) != []  # Fri, Thu, Wed
    assert _new(MON, path) == []  # Mon, Fri, Thu


def test_friday_holiday_keeps_thursday_eligible_on_monday(tmp_path, monkeypatch):
    path = str(tmp_path / "b.json")
    _bug(path, "Thursday bug", THU)
    _bug(path, "Wednesday bug", "2026-11-11")
    monkeypatch.setattr(
        bugalerts, "_is_business_day", lambda d: d.weekday() < 5 and d.isoformat() != FRI
    )
    found = {e[1] for e in _new(MON, path)}
    # Mon, Thu, Wed are the 3 business days: Friday (holiday) and the weekend do not count.
    assert found == {"BUG-001", "BUG-002"}
    monkeypatch.setattr(bugalerts, "_is_business_day", lambda d: d.weekday() < 5)
    assert {e[1] for e in _new(MON, path)} == {"BUG-001"}  # no holiday: Wednesday is out


def test_already_sent_alert_is_not_resent(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck", FRI)
    for day in (FRI, SAT, SUN, MON, TUE):
        bugalerts.run(day, dry_run=False, path=path, folder=folder)
    assert _new_only(sent) == ["BUG-001"]


def test_calendar_failure_falls_back_to_weekdays(tmp_path, monkeypatch):
    monkeypatch.undo()  # the real _is_business_day, with a calendar that raises
    monkeypatch.setattr(
        automation, "is_trading_day", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    path = str(tmp_path / "b.json")
    _bug(path, "Friday bug", FRI)
    _bug(path, "Wednesday bug", "2026-11-11")
    assert bugalerts._is_business_day(__import__("datetime").date(2026, 11, 16)) is True
    assert bugalerts._is_business_day(__import__("datetime").date(2026, 11, 14)) is False
    found = {e[1] for e in _new(MON, path)}
    assert found == {"BUG-001"}


def test_real_calendar_reads_the_holiday_table(tmp_path, monkeypatch):
    import sqlite3
    from datetime import date

    monkeypatch.undo()
    db = tmp_path / "cal.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE market_holidays (id INTEGER, holiday_date TEXT, description TEXT);"
        "CREATE TABLE market_holiday_exchanges (holiday_id INTEGER, exchange_code TEXT);"
        f"INSERT INTO market_holidays VALUES (1, '{FRI}', 'x');"
        "INSERT INTO market_holiday_exchanges VALUES (1, 'NSE');"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(automation, "DB", str(db))
    assert bugalerts._is_business_day(date.fromisoformat(FRI)) is False
    assert bugalerts._is_business_day(date.fromisoformat(THU)) is True
