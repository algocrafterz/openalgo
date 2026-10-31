"""The bug tracker file: one entry per defect, occurrences appended, fixes proven before closing."""

import json

import pytest

from signal_engine.bugs import store
from signal_engine.bugs.__main__ import main


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "bugs.json")


def _add(path, **overrides):
    fields = {
        "title": "Exit booked under the cancelled stop id",
        "kind": "BUG",
        "severity": "HIGH",
        "area": "signal_engine",
        "seen": "2026-10-27",
        "impact": "Ledger shows the wrong order",
        "strategies": ["EVWAP-EARLY-PY"],
        "path": path,
    }
    return store.add(**{**fields, **overrides})


def test_add_assigns_sequential_ids_and_starts_open(path):
    first = _add(path)
    second = _add(path, title="Another")
    assert (first["id"], second["id"]) == ("BUG-001", "BUG-002")
    assert first["status"] == "open" and first["occurrences"] == ["2026-10-27"]


def test_the_same_open_title_is_refused_so_a_recurrence_is_one_bug(path):
    _add(path)
    with pytest.raises(store.TrackerError, match="BUG-001 already tracks this"):
        _add(path, title="  exit booked under the cancelled stop id ")


def test_seen_appends_days_once_and_moves_last_seen(path):
    _add(path)
    store.seen("BUG-001", "2026-10-30", evidence=["rows 3392, 3404"], path=path)
    bug = store.seen("BUG-001", "2026-10-30", path=path)
    assert bug["occurrences"] == ["2026-10-27", "2026-10-30"]
    assert bug["last_seen"] == "2026-10-30" and bug["evidence"] == ["rows 3392, 3404"]


def test_a_fixed_bug_seen_again_reopens(path):
    _add(path)
    store.update("BUG-001", status="fixed", fix="set sl_order_id from the sweep", path=path)
    bug = store.seen("BUG-001", "2026-11-02", path=path)
    assert bug["status"] == "open"
    assert "reopened" in bug["history"][-1]["note"]


def test_fixed_needs_a_fix_and_verified_needs_a_verification(path):
    _add(path)
    with pytest.raises(store.TrackerError, match="needs --fix"):
        store.update("BUG-001", status="fixed", path=path)
    with pytest.raises(store.TrackerError, match="needs --verification"):
        store.update("BUG-001", status="verified", fix="x", path=path)


def test_bad_values_are_refused(path):
    with pytest.raises(store.TrackerError):
        _add(path, severity="CRITICAL")
    with pytest.raises(store.TrackerError):
        _add(path, seen="30-10-2026")
    _add(path)
    with pytest.raises(store.TrackerError, match="cannot edit"):
        store.update("BUG-001", path=path, occurrences="x")


def test_the_file_is_valid_json_after_every_write(path):
    _add(path)
    store.seen("BUG-001", "2026-10-28", path=path)
    with open(path) as handle:
        assert json.load(handle)["bugs"][0]["id"] == "BUG-001"


def test_cli_refusal_exits_2(path, monkeypatch, capsys):
    monkeypatch.setattr(store, "TRACKER_PATH", path)
    assert main(["show", "BUG-404"]) == 2
    assert "refused" in capsys.readouterr().err


def test_unattended_apply_adds_and_records_but_never_changes_status(path):
    _add(path)
    results = store.apply_actions(
        [
            {"action": "seen", "id": "BUG-001", "date": "2026-11-02", "evidence": ["row 4001"]},
            {
                "action": "add",
                "title": "New one",
                "kind": "EXECUTION",
                "severity": "LOW",
                "area": "broker",
                "date": "2026-11-02",
                "impact": "slow fills",
            },
            {"action": "note", "id": "BUG-001", "text": "clean session, no timed-out exits"},
            {"action": "update", "id": "BUG-001", "status": "verified"},
            {"action": "seen", "id": "BUG-404", "date": "2026-11-02"},
        ],
        origin="eod-cron-2026-11-02",
        path=path,
    )
    assert results[0].startswith("seen BUG-001")
    assert results[1].startswith("added BUG-002")
    assert results[2].startswith("noted BUG-001")
    assert results[3].startswith("refused update") and results[4].startswith("refused seen")
    bug = store._find(store.load(path), "BUG-001")
    assert bug["status"] == "open" and bug["occurrences"] == ["2026-10-27", "2026-11-02"]
    assert bug["history"][-1]["note"].startswith("[eod-cron-2026-11-02]")


def test_actions_are_read_from_the_last_block_of_a_report():
    text = 'review\n```bug-tracker\n[]\n```\nmore\n```bug-tracker\n[{"action": "note"}]\n```\n'
    assert store.actions_from_report(text) == [{"action": "note"}]
    assert store.actions_from_report("no block") is None
    with pytest.raises(store.TrackerError):
        store.actions_from_report("```bug-tracker\n{}\n```")


def test_a_re_run_of_a_day_before_the_fix_does_not_reopen(path):
    _add(path)
    bug = store.update("BUG-001", status="fixed", fix="sweep sets the id", path=path)
    fixed_day = bug["history"][-1]["at"][:10]
    bug = store.seen("BUG-001", "2026-10-27", path=path)  # a re-run of the session that found it
    assert bug["status"] == "fixed" and "stays fixed" in bug["history"][-1]["note"]
    later = "2099-01-02"
    assert later > fixed_day
    assert store.seen("BUG-001", later, path=path)["status"] == "open"
