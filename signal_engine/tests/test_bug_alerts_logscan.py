"""Phase 3: bug alerts, the conservative log detector, and the automation safety rule (R3.4)."""

import ast
import json
import pathlib
from collections import Counter

import pytest

from signal_engine.bugs import store
from signal_engine.ops import alerts, autotrack, bugalerts, logscan
from signal_engine.ops.model import FAIL, Check, write_phase

pytestmark = pytest.mark.filterwarnings("ignore")
DAY = "2026-11-16"


def _bug(path, title, severity="HIGH", seen="2026-11-16"):
    return store.add(
        title=title, kind="BUG", severity=severity, area="x", seen=seen, impact="i", path=path
    )


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(text, **kw):
        calls.append((text, kw))
        return True

    monkeypatch.setattr(alerts, "deliver", fake)
    return calls


def test_new_high_alerts_once_and_not_medium(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    _bug(path, "Minor thing", severity="MEDIUM")
    bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    assert len(sent) == 1
    assert sent[0][1]["condition"] == "BUG-001" and sent[0][1]["phase"] == "bugs"
    assert sent[0][1]["severity"] == "RED"
    bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    assert len(sent) == 1  # state remembers it


def test_dry_run_sends_nothing_and_writes_no_state(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    lines = bugalerts.run(DAY, dry_run=True, path=path, folder=folder)
    assert sent == [] and lines[0].startswith("[dry-run] would send")
    assert not (tmp_path / "bug-alert-state.json").exists()


def _restamp(path, bug_id, stamps):
    data = store.load(path)
    bug = next(b for b in data["bugs"] if b["id"] == bug_id)
    for h in bug["history"]:
        if h["status"] == "fixed":
            h["at"] = f"{stamps['fixed']}T10:00:00+05:30"
    store._save(data, path)


def _restamp_last(path, bug_id, day):
    data = store.load(path)
    bug = next(b for b in data["bugs"] if b["id"] == bug_id)
    bug["history"][-1]["at"] = f"{day}T10:00:00+05:30"
    store._save(data, path)


def test_overdue_and_unverified_are_weekly_and_reopen_counts(tmp_path, sent):
    path = str(tmp_path / "b.json")
    _bug(path, "Old high", seen="2026-11-01")
    fixed = _bug(path, "Fixed long ago", seen="2026-11-01")["id"]
    store.update(fixed, status="in_progress", path=path)
    store.update(fixed, status="fixed", fix="x", path=path)
    # history stamps are "now", so place the fix on D=2026-11-02, then daily review notes after it
    _restamp(path, fixed, {"fixed": "2026-11-02"})
    for stamp in ("2026-11-05", "2026-11-08"):
        store.note(fixed, "review: not yet verified", path=path)
        _restamp_last(path, fixed, stamp)
    kinds = {e[0]: e[1] for e in bugalerts.events(DAY, path)}
    assert kinds["bug-overdue"] == "BUG-001:2026-W47"
    assert kinds["bug-unverified"] == "BUG-002:2026-W47"
    # D+7 fires, D+6 does not, despite notes on D+3 and D+6
    unver = lambda day: [e for e in bugalerts.events(day, path) if e[0] == "bug-unverified"]  # noqa: E731
    assert unver("2026-11-09") and not unver("2026-11-08")
    # a later week is a new bucket
    assert {e[1] for e in bugalerts.events("2026-11-23", path) if e[0] == "bug-overdue"} == {
        "BUG-001:2026-W48"
    }


def test_reopened_high_condition_has_count(tmp_path, sent):
    path = str(tmp_path / "b.json")
    bug = _bug(path, "Flaky", seen="2026-11-10")
    store.update(bug["id"], status="in_progress", path=path)
    store.update(bug["id"], status="fixed", fix="x", path=path)
    data = store.load(path)
    data["bugs"][0]["history"][-1]["at"] = "2026-11-11T10:00:00+05:30"
    store._save(data, path)
    store.seen(bug["id"], DAY, path=path)
    data = store.load(path)
    data["bugs"][0]["history"][-1]["at"] = f"{DAY}T09:00:00+05:30"
    store._save(data, path)
    cond = [e[1] for e in bugalerts.events(DAY, path) if e[0] == "bug-reopened"]
    assert cond == ["BUG-001:reopen1"]


def test_cap_of_five_with_overflow_line(tmp_path, sent):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    for i in range(8):
        _bug(path, f"High bug number {i}")
    lines = bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    assert len(sent) == 5 and "and 3 more" in sent[-1][0] and lines[-1] == "and 3 more"
    bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    assert len(sent) == 8  # the rest go out next run, never repeated


def test_failed_send_is_retried_next_run(tmp_path, monkeypatch):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    monkeypatch.setattr(alerts, "deliver", lambda *a, **k: False)
    bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    assert len(bugalerts.events(DAY, path)) == 1
    assert not (tmp_path / "bug-alert-state.json").exists()


# R3.1 ---------------------------------------------------------------------------------------


def test_autotrack_never_downgrades_or_closes(tmp_path):
    reports, path = str(tmp_path / "r"), str(tmp_path / "b.json")
    chk = [Check("first runner cycle", FAIL, "no 'pipeline cycle' line")]
    write_phase("liveness", "2026-11-11", chk, "s", folder=reports)
    autotrack.record_failures("2026-11-11", folder=reports, path=path)
    store.update("BUG-001", severity="HIGH", path=path)
    write_phase("liveness", "2026-11-12", chk, "s", folder=reports)
    autotrack.record_failures("2026-11-12", folder=reports, path=path)
    bug = store.load(path)["bugs"][0]
    assert len(store.load(path)["bugs"]) == 1
    assert bug["severity"] == "HIGH" and bug["status"] == "open"
    assert bug["occurrences"] == ["2026-11-11", "2026-11-12"] and bug["last_seen"] == "2026-11-12"


# R3.2 ---------------------------------------------------------------------------------------

NOISE = [
    "ws: Connection to remote host was lost",
    "ws: ping/pong timed out",
    "feed: no market data for X",
    "yf: Failed download of Y",
    "http: 404 not found",
    "NIFTYFPI.NS: delisted",
]


def _counter(today, past=None):
    def fn(d):
        return Counter(today if d == DAY else (past or {}))

    return fn


def test_detector_rule_and_denylist():
    fn = _counter({"a: boom": 5, "b: once": 1, "c: twice": 2, **dict.fromkeys(NOISE, 50)})
    found = {s for s, _, _ in logscan.candidates(DAY, fn)}
    assert found == {"a: boom"}
    fn = _counter({"b: once": 1}, {"b: once": 1})
    assert [s for s, _, _ in logscan.candidates(DAY, fn)] == ["b: once"]  # on 2+ days


def test_detector_dry_run_then_write_then_update(tmp_path):
    path = str(tmp_path / "b.json")
    fn = _counter({"a: boom": 6})
    out = logscan.detect(DAY, counter=fn, path=path)
    assert out[0].startswith("[dry-run] would create") and store.load(path)["bugs"] == []
    logscan.detect(DAY, dry_run=False, counter=fn, path=path)
    bug = store.load(path)["bugs"][0]
    assert bug["severity"] == "MEDIUM" and bug["status"] == "open"
    assert bug["sources"][0].startswith("log-signature:") and len(bug["sources"][0]) == 24
    assert logscan.detect(DAY, counter=fn, path=path)[0].startswith(
        "[dry-run] would update BUG-001"
    )
    store.update("BUG-001", severity="HIGH", path=path)
    logscan.detect(
        "2026-11-17", dry_run=False, counter=lambda d: Counter({"a: boom": 7}), path=path
    )
    bug = store.load(path)["bugs"][0]
    assert bug["severity"] == "HIGH" and bug["occurrences"] == [DAY, "2026-11-17"]


def test_evidence_is_one_masked_short_line():
    sig = "x: bad token=abcdef1234567890ABCDEFGH12 and AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\nsecond"
    ev = logscan.mask_evidence(sig, 5, 1)
    assert "\n" not in ev and len(ev) <= 200
    assert "abcdef1234567890" not in ev and "AAAAAAAAAAAA" not in ev


# R3.4 ---------------------------------------------------------------------------------------


def test_automation_cannot_verify_close_or_change_status(tmp_path, sent):
    """Run every automation path on a fixed HIGH bug: only open/seen/note may result."""
    path, folder = str(tmp_path / "b.json"), str(tmp_path / "r")
    bug = _bug(path, "High one", seen="2026-11-01")
    store.update(bug["id"], status="in_progress", path=path)
    store.update(bug["id"], status="fixed", fix="x", path=path)
    done = _bug(path, "High two", seen="2026-11-01")
    store.update(done["id"], status="in_progress", path=path)
    store.update(done["id"], status="fixed", fix="x", path=path)
    store.update(done["id"], status="verified", verification="clean session", path=path)
    before = {b["id"]: b for b in store.load(path)["bugs"]}
    bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    logscan.detect(DAY, dry_run=False, counter=_counter({"a: boom": 9}), path=path)
    chk = [Check("first runner cycle", FAIL, "e")]
    write_phase("liveness", DAY, chk, "s", folder=folder)
    autotrack.record_failures(DAY, folder=folder, path=path)
    after = {b["id"]: b for b in store.load(path)["bugs"]}
    assert after["BUG-001"]["status"] == before["BUG-001"]["status"] == "fixed"
    assert after["BUG-002"]["status"] == before["BUG-002"]["status"] == "verified"
    assert after["BUG-001"]["severity"] == after["BUG-002"]["severity"] == "HIGH"
    assert all(b["status"] == "open" for k, b in after.items() if k not in ("BUG-001", "BUG-002"))


def test_automation_modules_call_no_status_changing_store_function():
    root = pathlib.Path(__file__).resolve().parents[1] / "ops"
    banned = {"update", "apply_actions"}
    for name in ("autotrack.py", "logscan.py", "bugalerts.py"):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert not (node.value.id == "store" and node.attr in banned), name


def _sym_counter(symbols, extra=None):
    def counter(day):
        c = Counter(
            {
                f"signal_engine.pipeline.cycle: pipeline {s} bar #: evaluate failed": 6
                for s in symbols
            }
        )
        c.update(extra or {})
        return c

    return counter


def test_per_symbol_variants_collapse_to_one_key(tmp_path):
    path = str(tmp_path / "b.json")
    one = logscan.detect(DAY, counter=_sym_counter(["SUNPHARMA", "M&M", "BAJFINANCE"]), path=path)
    assert len(one) == 1 and "<SYM>" in one[0] and "x18" in one[0]


def test_normalized_key_is_stable_and_ignores_ids():
    a = logscan.normalize(
        "x.y: order 1a2b3c4d5e6f7a8b rejected for RELIANCE id # tok abcdefghijklmnopqrstuvwxyz1"
    )
    b = logscan.normalize(
        "x.y: order ffeeddccbbaa9988 rejected for TCS id # tok zyxwvutsrqponmlkjihgfedcba2"
    )
    assert a == b and logscan.source_key(a) == logscan.source_key(b)
    assert (
        logscan.normalize("a: HTTP ERROR from NSE for INFY") == "a: HTTP ERROR from NSE for <SYM>"
    )


def test_short_known_symbol_masked_only_with_universe():
    sig = "p: pipeline LT bar #: evaluate failed"
    assert logscan.normalize(sig, {"LT"}) == "p: pipeline <SYM> bar #: evaluate failed"
    assert logscan.normalize(sig) == sig


def test_benign_still_excluded_after_normalizing(tmp_path):
    benign = {"ws: Connection to remote host was lost. - goodbye": 20}
    out = logscan.detect(DAY, counter=_sym_counter(["TCS"], benign), path=str(tmp_path / "b.json"))
    assert len(out) == 1 and "remote host" not in out[0]


def test_short_symbols_inferred_from_sibling_signatures(tmp_path):
    out = logscan.detect(
        DAY, counter=_sym_counter(["LT", "HAL", "BEL", "TCS"]), path=str(tmp_path / "b.json")
    )
    assert len(out) == 1 and "pipeline <SYM> bar" in out[0]


def test_title_and_dry_run_are_masked(tmp_path):
    path = str(tmp_path / "b.json")
    sig = "auth: failed token=abcdef1234567890ABCDEFGH12 for user"
    fn = _counter({sig: 6})
    line = logscan.detect(DAY, counter=fn, path=path)[0]
    assert "abcdef1234567890" not in line
    logscan.detect(DAY, dry_run=False, counter=fn, path=path)
    bug = store.load(path)["bugs"][0]
    assert "abcdef1234567890" not in bug["title"] and "<masked>" in bug["title"]


def test_damaged_tracker_does_not_abort_detect(tmp_path):
    path = tmp_path / "b.json"
    path.write_text("{not json")
    out = logscan.detect(DAY, dry_run=False, counter=_counter({"a: boom": 6}), path=str(path))
    assert out and out[0].startswith("skipped")


def test_old_reopen_wording_counts(tmp_path, sent):
    path = str(tmp_path / "b.json")
    bug = _bug(path, "Flaky", seen="2026-11-10")
    data = store.load(path)
    data["bugs"][0]["history"].append(
        {
            "at": f"{DAY}T09:00:00+05:30",
            "status": "open",
            "note": "seen: recurred after being fixed, reopened",
        }
    )
    store._save(data, path)
    assert [e[1] for e in bugalerts.events(DAY, path) if e[0] == "bug-reopened"] == [
        f"{bug['id']}:reopen1"
    ]
