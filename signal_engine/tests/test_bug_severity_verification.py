"""Severity change with a reason, and the optional structured verification reference."""

import json

import pytest

from signal_engine.bugs import store
from signal_engine.bugs.__main__ import main


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "bugs.json")


def _add(path, **kw):
    fields = {
        "title": "T",
        "kind": "BUG",
        "severity": "MEDIUM",
        "area": "ops",
        "seen": "2026-11-01",
        "impact": "i",
        "path": path,
    }
    return store.add(**{**fields, **kw})


def _fixed(path):
    _add(path)
    store.update("BUG-001", status="in_progress", path=path)
    store.update("BUG-001", status="fixed", fix="f", path=path)


# ---- severity ---------------------------------------------------------------------------------


def test_severity_change_records_reason_and_keeps_status(path):
    _fixed(path)
    bug = store.set_severity("BUG-001", "HIGH", "  stops were skipped  ", path=path)
    assert bug["severity"] == "HIGH" and bug["status"] == "fixed"
    last = bug["history"][-1]
    assert last["note"] == "severity MEDIUM -> HIGH: stops were skipped"
    assert last["status"] == "fixed"
    assert store.load(path)["bugs"][0]["severity"] == "HIGH"


def test_severity_change_to_high_makes_an_old_open_bug_overdue(path):
    _add(path, seen="2026-11-01")
    bug = store.set_severity("BUG-001", "HIGH", "wrong order risk", path=path)
    assert store.is_overdue(bug, "2026-11-10") is True


@pytest.mark.parametrize(
    ("value", "reason", "match"),
    [
        ("URGENT", "why", "severity must be one of"),
        ("", "why", "severity must be one of"),
        ("HIGH", "", "needs a reason"),
        ("HIGH", "   ", "needs a reason"),
        ("MEDIUM", "same", "already MEDIUM"),
    ],
)
def test_severity_refusals_write_nothing(path, value, reason, match):
    _add(path)
    before = open(path, "rb").read()
    with pytest.raises(store.TrackerError, match=match):
        store.set_severity("BUG-001", value, reason, path=path)
    assert open(path, "rb").read() == before


def test_severity_unknown_bug_and_lock(path, monkeypatch):
    _add(path)
    with pytest.raises(store.TrackerError, match="no bug BUG-009"):
        store.set_severity("BUG-009", "LOW", "x", path=path)
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.2)
    with store._locked(path), pytest.raises(store.TrackerBusy):
        store.set_severity("BUG-001", "LOW", "x", path=path)
    assert store.load(path)["bugs"][0]["severity"] == "MEDIUM"


def test_cli_update_severity_needs_a_note_and_logs_it(path, monkeypatch, capsys):
    _add(path)
    monkeypatch.setattr(store, "TRACKER_PATH", path)
    assert main(["update", "BUG-001", "--severity", "HIGH"]) == 2
    assert store.load(path)["bugs"][0]["severity"] == "MEDIUM"
    assert main(["update", "BUG-001", "--severity", "HIGH", "--note", "stop risk"]) == 0
    bug = store.load(path)["bugs"][0]
    assert bug["severity"] == "HIGH"
    assert bug["history"][-1]["note"] == "severity MEDIUM -> HIGH: stop risk"
    capsys.readouterr()


def test_cli_update_severity_and_status_together_is_all_or_nothing(path, monkeypatch, capsys):
    _add(path)
    store.update("BUG-001", status="in_progress", path=path)
    monkeypatch.setattr(store, "TRACKER_PATH", path)
    # status wontfix needs a note: no note -> nothing written, severity unchanged
    assert main(["update", "BUG-001", "--severity", "HIGH", "--status", "wontfix"]) == 2
    bug = store.load(path)["bugs"][0]
    assert bug["severity"] == "MEDIUM" and bug["status"] == "in_progress"
    # an illegal transition with a note: still nothing written
    assert (
        main(["update", "BUG-001", "--severity", "HIGH", "--status", "verified", "--note", "n"])
        == 2
    )
    bug = store.load(path)["bugs"][0]
    assert bug["severity"] == "MEDIUM" and bug["status"] == "in_progress"
    # with a note both apply
    assert (
        main(["update", "BUG-001", "--severity", "HIGH", "--status", "wontfix", "--note", "dup"])
        == 0
    )
    bug = store.load(path)["bugs"][0]
    assert bug["severity"] == "HIGH" and bug["status"] == "wontfix"
    assert (
        "dup" in bug["history"][-1]["note"]
        and "severity MEDIUM -> HIGH" in bug["history"][-1]["note"]
    )
    capsys.readouterr()


def test_store_update_refuses_a_severity_change_without_a_reason(path):
    _add(path)
    with pytest.raises(store.TrackerError, match="needs a reason"):
        store.update("BUG-001", severity="HIGH", path=path)
    with pytest.raises(store.TrackerError, match="needs a reason"):
        store.update("BUG-001", severity="HIGH", note="  ", path=path)
    assert store.load(path)["bugs"][0]["severity"] == "MEDIUM"
    bug = store.update("BUG-001", severity="HIGH", note="why", path=path)
    assert bug["severity"] == "HIGH"
    store.update("BUG-001", severity="HIGH", path=path)  # unchanged value needs no reason


# ---- verification reference -------------------------------------------------------------------


def test_verification_ref_is_stored_additively_and_logged(path):
    _fixed(path)
    assert "verification_ref" not in store.load(path)["bugs"][0]
    bug = store.update(
        "BUG-001",
        status="verified",
        verification="held for 5 sessions",
        verification_ref={"kind": "test", "ref": "  signal_engine/tests/test_x.py::t  "},
        path=path,
    )
    assert bug["verification_ref"] == {"kind": "test", "ref": "signal_engine/tests/test_x.py::t"}
    assert "verification ref test: signal_engine/tests/test_x.py::t" in bug["history"][-1]["note"]
    assert store.load(path)["bugs"][0]["verification_ref"]["kind"] == "test"


def test_verify_without_ref_leaves_the_key_absent_not_null(path):
    _fixed(path)
    bug = store.update("BUG-001", status="verified", verification="v", path=path)
    assert "verification_ref" not in bug
    assert "verification_ref" not in store.load(path)["bugs"][0]


@pytest.mark.parametrize(
    "ref",
    [
        {"kind": "magic", "ref": "x"},
        {"kind": "test"},
        {"kind": "test", "ref": "  "},
        {"ref": "x"},
        {"kind": "test", "ref": "x" * 301},
        {"kind": "test", "ref": 5},
        "test:x",
    ],
)
def test_bad_verification_refs_are_refused_and_write_nothing(path, ref):
    _fixed(path)
    before = open(path, "rb").read()
    with pytest.raises(store.TrackerError):
        store.update(
            "BUG-001", status="verified", verification="v", verification_ref=ref, path=path
        )
    assert open(path, "rb").read() == before


def test_a_ref_is_accepted_only_on_the_move_to_verified(path):
    _fixed(path)
    with pytest.raises(store.TrackerError, match="move to verified"):
        store.update("BUG-001", verification_ref={"kind": "test", "ref": "x"}, path=path)
    store.update("BUG-001", status="verified", verification="v", path=path)
    with pytest.raises(store.TrackerError, match="move to verified"):
        store.update(
            "BUG-001", status="verified", verification_ref={"kind": "test", "ref": "x"}, path=path
        )


def test_a_ref_of_exactly_300_characters_is_accepted(path):
    _fixed(path)
    bug = store.update(
        "BUG-001",
        status="verified",
        verification="v",
        verification_ref={"kind": "command", "ref": "x" * 300},
        path=path,
    )
    assert len(bug["verification_ref"]["ref"]) == 300


def test_a_new_proof_replaces_an_old_reference(path):
    _fixed(path)
    ref = {"kind": "commit", "ref": "abc1234"}
    store.update("BUG-001", status="verified", verification="v", verification_ref=ref, path=path)
    store.update("BUG-001", status="open", note="came back", path=path)
    store.update("BUG-001", status="in_progress", path=path)
    store.update("BUG-001", status="fixed", fix="f2", path=path)
    bug = store.update("BUG-001", status="verified", verification="v2", path=path)
    assert "verification_ref" not in bug


def test_a_legacy_record_without_the_field_loads_lists_and_verifies(path):
    legacy = {
        "id": "BUG-001",
        "title": "Old",
        "plain": "",
        "kind": "BUG",
        "severity": "LOW",
        "status": "fixed",
        "owner": "",
        "area": "ops",
        "strategies": [],
        "first_seen": "2026-10-01",
        "last_seen": "2026-10-01",
        "occurrences": ["2026-10-01"],
        "impact": "i",
        "evidence": [],
        "root_cause": "",
        "fix": "f",
        "fix_commit": "",
        "fix_explanation": "",
        "verification": "",
        "sources": [],
        "history": [{"at": "2026-10-02T10:00:00+05:30", "status": "fixed", "note": "fixed"}],
        "created_at": "2026-10-01T10:00:00+05:30",
        "updated_at": "2026-10-02T10:00:00+05:30",
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "bugs": [legacy]}, handle)
    assert store.load(path)["bugs"][0] == legacy
    assert store.awaiting_verification(legacy) is True
    bug = store.update("BUG-001", status="verified", verification="session held", path=path)
    assert bug["status"] == "verified" and "verification_ref" not in bug
    # a legacy record also takes a severity change
    assert store.set_severity("BUG-001", "HIGH", "reassessed", path=path)["severity"] == "HIGH"
