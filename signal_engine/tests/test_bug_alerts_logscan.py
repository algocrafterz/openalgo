"""Phase 3: the conservative log detector and the automation safety rule (R3.4).

The bug alerts that once sat here were removed with outgoing Telegram notifications."""

import ast
import json
import pathlib
from collections import Counter

import pytest

from signal_engine.bugs import store
from signal_engine.ops import autotrack, logscan
from signal_engine.ops.model import FAIL, Check, write_phase

pytestmark = pytest.mark.filterwarnings("ignore")
DAY = "2026-11-16"


def _bug(path, title, severity="HIGH", seen="2026-11-16"):
    return store.add(
        title=title, kind="BUG", severity=severity, area="x", seen=seen, impact="i", path=path
    )


# R3.1 ---------------------------------------------------------------------------------------


def test_autotrack_never_downgrades_or_closes(tmp_path):
    reports, path = str(tmp_path / "r"), str(tmp_path / "b.json")
    chk = [Check("first runner cycle", FAIL, "no 'pipeline cycle' line")]
    write_phase("liveness", "2026-11-11", chk, "s", folder=reports)
    autotrack.record_failures("2026-11-11", folder=reports, path=path)
    store.update(
        "BUG-001", severity="HIGH", note="raised for test", path=path
    )  # severity change needs a note
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
    store.update(
        "BUG-001", severity="HIGH", note="raised for test", path=path
    )  # severity change needs a note
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


def test_automation_cannot_verify_close_or_change_status(tmp_path):
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
    for name in ("autotrack.py", "logscan.py"):
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
