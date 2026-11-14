"""F1: the bug-alert state is read, sent and saved under one exclusive lock."""

import json
import threading
import time

import pytest

from signal_engine.bugs import store
from signal_engine.ops import alerts, bugalerts

pytestmark = pytest.mark.filterwarnings("ignore")
DAY = "2026-11-16"


def _bug(path, title):
    return store.add(
        title=title, kind="BUG", severity="HIGH", area="x", seen=DAY, impact="i", path=path
    )


def test_two_simultaneous_runs_send_each_alert_once(tmp_path, monkeypatch):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    calls, guard = [], threading.Lock()

    def slow_deliver(text, **kw):
        time.sleep(0.4)  # long enough for the other run to read the same (empty) state
        with guard:
            calls.append(kw["condition"])
        return True

    monkeypatch.setattr(alerts, "deliver", slow_deliver)
    gate = threading.Barrier(2)
    results = []

    def go():
        gate.wait()
        results.append(bugalerts.run(DAY, dry_run=False, path=path, folder=folder))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == ["BUG-001"]
    assert json.loads((tmp_path / "bug-alert-state.json").read_text())["sent"] == [
        "bug-new|BUG-001"
    ]


def test_a_stuck_holder_makes_the_run_skip_not_crash_or_send(tmp_path, monkeypatch):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    sent = []
    monkeypatch.setattr(alerts, "deliver", lambda *a, **k: sent.append(1) or True)
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.1)
    with store._locked(str(tmp_path / "bug-alert-state.json")):
        out = bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    assert sent == [] and out and out[0].startswith("skipped")


def test_old_state_shape_still_loads_and_is_kept(tmp_path, monkeypatch):
    path, folder = str(tmp_path / "b.json"), str(tmp_path)
    _bug(path, "Orders stuck")
    (tmp_path / "bug-alert-state.json").write_text('{"sent": ["bug-new|BUG-001"]}')
    sent = []
    monkeypatch.setattr(alerts, "deliver", lambda *a, **k: sent.append(1) or True)
    bugalerts.run(DAY, dry_run=False, path=path, folder=folder)
    assert sent == []
    assert set(json.loads((tmp_path / "bug-alert-state.json").read_text())) == {"sent"}
