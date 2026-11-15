"""Phase 2: lifecycle transitions, owner, fix evidence, history, computed age/overdue."""

import json
import os
import shutil
from datetime import date

import pytest

from signal_engine.bugs import store
from signal_engine.bugs.__main__ import main

REAL = os.path.join(os.path.dirname(store.__file__), "bugs.json")


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "bugs.json")


def _add(path, **kw):
    fields = {
        "title": "T",
        "kind": "BUG",
        "severity": "HIGH",
        "area": "ops",
        "seen": "2026-11-01",
        "impact": "i",
        "path": path,
    }
    return store.add(**{**fields, **kw})


def _walk(path, *steps):
    for status, extra in steps:
        store.update("BUG-001", status=status, path=path, **extra)


ALL = store.STATUSES


@pytest.mark.parametrize("src", ALL)
@pytest.mark.parametrize("dst", ALL)
def test_transition_table(path, src, dst):
    _add(path)
    data = store.load(path)
    data["bugs"][0]["status"] = src
    data["bugs"][0].update(fix="f", verification="v")
    store._save(data, path)
    if dst == src or dst in store.TRANSITIONS[src]:
        store.update("BUG-001", status=dst, note="because", path=path)
        assert store.load(path)["bugs"][0]["status"] == dst
    else:
        with pytest.raises(store.TrackerError, match="cannot go from"):
            store.update("BUG-001", status=dst, note="because", path=path)
        assert store.load(path)["bugs"][0]["status"] == src


def test_expected_table():
    assert store.TRANSITIONS == {
        "open": ("in_progress", "wontfix"),
        "in_progress": ("open", "fixed", "wontfix"),
        "fixed": ("verified", "open", "in_progress"),
        "verified": ("open",),
        "wontfix": ("open",),
    }


@pytest.mark.parametrize("src", ["fixed", "verified", "wontfix"])
def test_manual_reopen_needs_a_reason(path, src):
    _add(path)
    data = store.load(path)
    data["bugs"][0].update(status=src, fix="f", verification="v")
    store._save(data, path)
    with pytest.raises(store.TrackerError, match="needs --note"):
        store.update("BUG-001", status="open", note="  ", path=path)
    bug = store.update("BUG-001", status="open", note="broke again", path=path)
    assert bug["status"] == "open" and "broke again" in bug["history"][-1]["note"]


def test_wontfix_needs_a_reason(path):
    _add(path)
    with pytest.raises(store.TrackerError, match="needs --note"):
        store.update("BUG-001", status="wontfix", path=path)
    assert store.update("BUG-001", status="wontfix", note="by design", path=path)["status"] == (
        "wontfix"
    )


def test_automatic_reopen_needs_no_reason_and_says_recurrence(path):
    _add(path)
    _walk(path, ("in_progress", {}), ("fixed", {"fix": "f"}), ("verified", {"verification": "v"}))
    bug = store.seen("BUG-001", "2099-01-01", path=path)
    assert bug["status"] == "open"
    assert "recurrence" in bug["history"][-1]["note"]


def test_owner_defaults_empty_can_be_set_and_is_logged(path):
    bug = _add(path)
    assert bug["owner"] == ""
    bug = store.update("BUG-001", owner="  anand ", path=path)
    assert bug["owner"] == "anand" and "owner changed" in bug["history"][-1]["note"]
    assert _add(path, title="U", owner="x")["owner"] == "x"


def test_history_records_severity_fix_and_verification_changes(path):
    _add(path)
    bug = store.update("BUG-001", severity="LOW", note="why", path=path)
    assert "severity HIGH -> LOW" in bug["history"][-1]["note"]
    n = len(bug["history"])
    bug = store.update("BUG-001", severity="LOW", path=path)  # unchanged: plain update line
    assert bug["history"][-1]["note"] == "updated" and len(bug["history"]) == n + 1
    store.update("BUG-001", status="in_progress", path=path)
    bug = store.update("BUG-001", status="fixed", fix="sweep", fix_commit="abc1234", path=path)
    note = bug["history"][-1]["note"]
    assert "fix changed" in note and "fix_commit changed" in note
    bug = store.update("BUG-001", verification="regression test passed", path=path)
    assert "verification changed" in bug["history"][-1]["note"]
    bug = store.update("BUG-001", fix_explanation="fixed before commits were tracked", path=path)
    assert "fix_explanation changed" in bug["history"][-1]["note"]
    assert bug["fix_commit"] == "abc1234"


def test_fix_without_commit_stays_blank_and_verified_needs_evidence(path):
    _add(path)
    _walk(path, ("in_progress", {}), ("fixed", {"fix": "f", "fix_explanation": "old fix"}))
    bug = store.load(path)["bugs"][0]
    assert bug["fix_commit"] == "" and bug["fix_explanation"] == "old fix"
    with pytest.raises(store.TrackerError, match="needs --verification"):
        store.update("BUG-001", status="verified", path=path)
    # evidence supplied in the same call counts
    assert store.update("BUG-001", status="verified", verification="v", path=path)["status"] == (
        "verified"
    )


def test_age_overdue_and_awaiting():
    bug = {"severity": "HIGH", "status": "open", "first_seen": "2026-11-01"}
    assert store.age_days(bug, date(2026, 11, 4)) == 3
    assert store.age_days(bug, "2026-11-05") == 4
    assert not store.is_overdue(bug, "2026-11-04")
    assert store.is_overdue(bug, "2026-11-05")
    assert store.is_overdue({**bug, "status": "in_progress"}, "2026-11-05")
    for status in ("fixed", "verified", "wontfix"):
        assert not store.is_overdue({**bug, "status": status}, "2026-12-01")
    assert not store.is_overdue({**bug, "severity": "MEDIUM"}, "2026-12-01")
    assert store.awaiting_verification({"status": "fixed"})
    assert not store.awaiting_verification({"status": "verified"})


def test_cannot_create_a_bug_already_fixed_or_verified(path):
    for status in ("fixed", "verified", "wontfix"):
        with pytest.raises(store.TrackerError):
            _add(path, status=status)


def test_unattended_actions_unchanged_and_cannot_verify_or_close(path):
    assert store.UNATTENDED_ACTIONS == ("add", "seen", "note")
    _add(path)
    out = store.apply_actions(
        [
            {"action": "update", "id": "BUG-001", "status": "verified"},
            {"action": "verify", "id": "BUG-001"},
            {"action": "wontfix", "id": "BUG-001"},
        ],
        origin="t",
        path=path,
    )
    assert all(line.startswith("refused") for line in out)
    assert store.load(path)["bugs"][0]["status"] == "open"


def test_cli_owner_and_list_json_computed_fields(path, monkeypatch, capsys):
    monkeypatch.setattr(store, "TRACKER_PATH", path)
    base = ["add", "--title", "T", "--kind", "BUG", "--severity", "HIGH", "--area", "ops"]
    assert main([*base, "--seen", "2026-01-01", "--impact", "i", "--owner", "me"]) == 0
    assert main(["update", "BUG-001", "--owner", "you"]) == 0
    assert store.load(path)["bugs"][0]["owner"] == "you"
    assert main(["update", "BUG-001", "--status", "verified"]) == 2
    capsys.readouterr()
    assert main(["list", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["overdue"] is True and row["age_days"] > 3 and row["awaiting_verification"] is False
    assert "age_days" not in store.load(path)["bugs"][0]  # computed, not stored


def test_the_real_tracker_copy_still_loads_and_lists(tmp_path, monkeypatch, capsys):
    copy = str(tmp_path / "bugs.json")
    shutil.copy(REAL, copy)
    data = store.load(copy)
    assert len(data["bugs"]) == 23
    monkeypatch.setattr(store, "TRACKER_PATH", copy)
    today = date(2026, 11, 15)
    for bug in data["bugs"]:
        assert store.age_days(bug, today) >= 0
        store.is_overdue(bug, today)
        store.awaiting_verification(bug)
        assert bug.get("owner", "") == ""
    assert main(["list", "--all", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 23
    assert main(["list", "--all"]) == 0
    assert "23 bug(s)" in capsys.readouterr().out
    # an old record takes an edit without losing history
    before = len(data["bugs"][0]["history"])
    bug = store.update(data["bugs"][0]["id"], owner="x", path=copy)
    assert len(bug["history"]) == before + 1


def _stamp(path, bug_id, index, day):
    data = store.load(path)
    bug = next(b for b in data["bugs"] if b["id"] == bug_id)
    bug["history"][index]["at"] = f"{day}T10:00:00+05:30"
    store._save(data, path)


def test_fixed_since_ignores_notes_and_seen_and_reopen_after_review_note(path):
    bug = _add(path)
    bid = bug["id"]
    store.update(bid, status="in_progress", path=path)
    store.update(bid, status="fixed", fix="x", path=path)
    fix_index = len(store.load(path)["bugs"][0]["history"]) - 1
    _stamp(path, bid, fix_index, "2026-11-02")
    store.note(bid, "review", path=path)
    _stamp(path, bid, fix_index + 1, "2026-11-05")
    assert store.fixed_since(store.load(path)["bugs"][0]) == "2026-11-02"
    # on or before the fix day: stays fixed
    store.seen(bid, "2026-11-02", path=path)
    assert store.load(path)["bugs"][0]["status"] == "fixed"
    # the day after the fix reopens even though a review note exists that same day
    store.note(bid, "review", path=path)
    _stamp(path, bid, len(store.load(path)["bugs"][0]["history"]) - 1, "2026-11-06")
    store.seen(bid, "2026-11-03", path=path)
    assert store.load(path)["bugs"][0]["status"] == "open"


def test_fixed_since_without_history_is_none():
    assert store.fixed_since({"history": []}) is None
    assert store.fixed_since({}) is None


def test_assign_same_owner_is_a_no_op(path):
    bug = _add(path)
    store.update(bug["id"], owner="asha", path=path)
    n = len(store.load(path)["bugs"][0]["history"])
    store.update(bug["id"], owner="asha", path=path)
    assert len(store.load(path)["bugs"][0]["history"]) == n
