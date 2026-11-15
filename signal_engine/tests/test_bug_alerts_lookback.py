"""F2: new and reopened bug alerts survive a failed send or the cap for LOOKBACK_DAYS."""

import json

import pytest

from signal_engine.bugs import store
from signal_engine.ops import alerts, bugalerts

pytestmark = pytest.mark.filterwarnings("ignore")
DAY = "2026-11-16"
NEXT = "2026-11-17"


def _bug(path, title, seen=DAY):
    bug = store.add(
        title=title, kind="BUG", severity="HIGH", area="x", seen=seen, impact="i", path=path
    )
    data = store.load(path)  # created_at is the real clock; pin it to the bug's own day
    for b in data["bugs"]:
        if b["id"] == bug["id"]:
            b["created_at"] = f"{seen}T09:00:00+05:30"
    store._save(data, path)
    return bug


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "deliver", lambda text, **kw: calls.append(kw["condition"]) or True)
    return calls


def test_failed_send_is_retried_the_next_day(tmp_path, monkeypatch):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    monkeypatch.setattr(alerts, "deliver", lambda *a, **k: False)
    bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    calls = []
    monkeypatch.setattr(alerts, "deliver", lambda t, **kw: calls.append(kw["condition"]) or True)
    bugalerts.run(NEXT, dry_run=False, path=path, folder=folder)
    assert calls == ["BUG-001"]


def test_event_over_the_cap_is_delivered_the_next_day(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    for i in range(7):
        _bug(path, f"High bug number {i}")
    bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    assert len(sent) == 5
    bugalerts.run(NEXT, dry_run=False, path=path, folder=folder)
    assert len(sent) == 7 and len(set(sent)) == 7


def test_event_older_than_the_window_is_never_sent(tmp_path, sent, monkeypatch):
    # Owner decision: the calendar-day window is replaced by a business-day window (3 business
    # days, weekends and holidays not counted); see test_bug_alerts_weekday_lookback.py.
    monkeypatch.setattr(bugalerts, "_is_business_day", lambda d: d.weekday() < 5)
    path = str(tmp_path / "b.json")
    _bug(path, "Old high", seen="2026-11-10")  # Tuesday: outside the window from Sunday the 15th
    assert bugalerts.LOOKBACK_BUSINESS_DAYS == 3
    assert [e for e in bugalerts.events("2026-11-15", path) if e[0] == "bug-new"] == []
    assert [e for e in bugalerts.events("2026-11-14", path) if e[0] == "bug-new"] == []
    _bug(path, "Newer high", seen="2026-11-12")  # Thursday: 3rd business day back from Sunday
    assert [e for e in bugalerts.events("2026-11-15", path) if e[0] == "bug-new"] != []


def test_a_future_dated_bug_is_not_alerted_early(tmp_path, sent):
    path = str(tmp_path / "b.json")
    _bug(path, "Later high", seen="2026-11-20")
    assert [e for e in bugalerts.events(DAY, path) if e[0] == "bug-new"] == []


def test_no_duplicate_on_repeat_inside_the_window(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    for day in (DAY, DAY, NEXT, "2026-11-18"):
        bugalerts.run(day, dry_run=False, path=path, folder=folder)
    assert sent == ["BUG-001"]


def test_keys_already_stored_still_dedupe(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    (tmp_path / "bug-alert-state.json").write_text(json.dumps({"sent": ["bug-new|BUG-001"]}))
    bugalerts.run(NEXT, dry_run=False, path=path, folder=folder)
    assert sent == []


def test_reopen_is_retried_inside_the_window_only(tmp_path):
    path = str(tmp_path / "b.json")
    bug = _bug(path, "Flaky", seen="2026-11-01")
    data = store.load(path)
    data["bugs"][0]["status"] = "open"
    data["bugs"][0]["history"].append(
        {
            "at": f"{DAY}T09:00:00+05:30",
            "status": "open",
            "note": "seen: recurred after being fixed, reopened",
        }
    )
    store._save(data, path)
    cond = f"{bug['id']}:reopen1"
    for day, expected in ((DAY, True), (NEXT, True), ("2026-11-18", True), ("2026-11-19", False)):
        found = [e[1] for e in bugalerts.events(day, path) if e[0] == "bug-reopened"]
        assert (found == [cond]) is expected, day
