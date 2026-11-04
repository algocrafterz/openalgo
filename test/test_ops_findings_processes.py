"""Bug tracker writers in separate processes: the web route and the CLI at the same time.

Threads in one process cannot show whether the lock works across processes, and the CLI, the
cron `apply` and the web server are separate processes. So every writer here is its own OS
process, released at one shared instant to make them collide.

Everything runs on a temporary tracker; the real signal_engine/bugs/bugs.json is never opened.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_findings_fixtures import DAYS, REPO, child_env, write_day  # noqa: E402

CHILD = REPO / "test" / "ops_findings_child.py"
DAY = "2026-11-03"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses POSIX signals to kill a lock holder")


@pytest.fixture
def env(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    for day in DAYS:
        write_day(reports, day)
    tracker = tmp_path / "bugs.json"
    tracker.write_text(json.dumps({"version": 1, "bugs": []}))
    return reports, tracker


def _spawn(tmp_path, *args) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(CHILD), *map(str, args)],
        cwd=str(REPO),
        env=child_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(procs, timeout=120):
    """Wait for every child. Returns the JSON line each printed, failing loudly on a crash."""
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=timeout)
        assert p.returncode == 0, f"child failed ({p.returncode}): {stderr[-1500:]}"
        lines = [ln for ln in stdout.splitlines() if ln.startswith("{")]
        out.append(json.loads(lines[-1]) if lines else {})
    return out


def _release_at(seconds_from_now: float = 6.0) -> float:
    """Children spend a few seconds importing; they all start working at this one instant."""
    return time.time() + seconds_from_now


def _bugs(tracker: Path) -> list:
    return json.loads(tracker.read_text())["bugs"]


def _payload(day=DAY, **over):
    base = {
        "day": day,
        "phase": "open-day",
        "group": "open-day",
        "name": f"close-day {day}",
        "severity": "MEDIUM",
        "kind": "EXECUTION",
        "area": "ops",
        "impact": "Not assessed",
        "severity_confirmed": True,
    }
    return {**base, **over}


def _logs_payload(day=DAY):
    return _payload(day, phase="close-day", group="logs", name="NEW x4")


def _exec_payload(day=DAY):
    return _payload(day, phase="close-day", group="execution", name="Limiter refused calls (total)")


def test_six_processes_reporting_the_same_finding_make_exactly_one_bug(env, tmp_path):
    reports, tracker = env
    start = _release_at()
    procs = [
        _spawn(tmp_path, "http", reports, tracker, start, json.dumps(_payload())) for _ in range(6)
    ]
    results = _finish(procs)
    assert [r["http"] for r in results].count(201) == 1
    assert sorted({r["http"] for r in results}) == [200, 201]
    assert {r["bug_id"] for r in results} == {"BUG-001"}
    bugs = _bugs(tracker)
    assert len(bugs) == 1 and bugs[0]["occurrences"] == [DAY] and len(bugs[0]["evidence"]) == 1


def test_web_and_cli_writers_at_once_lose_nothing_and_keep_ids_unique(env, tmp_path):
    reports, tracker = env
    start = _release_at()
    http = [
        _spawn(tmp_path, "http", reports, tracker, start, json.dumps(p))
        for p in (_payload(), _logs_payload(), _exec_payload())
    ]
    cli = [_spawn(tmp_path, "cli", tracker, start, f"Writer {n}", 15) for n in range(3)]
    results = _finish(http + cli)
    assert all(r["http"] == 201 for r in results[:3])
    assert all(r["cli_exit_codes"] == [0] * 15 for r in results[3:])
    bugs = _bugs(tracker)
    ids = [b["id"] for b in bugs]
    assert len(bugs) == 3 + 45, "a write was lost"
    assert len(set(ids)) == len(ids), "two writers were given the same id"
    assert sorted(ids) == [f"BUG-{i:03d}" for i in range(1, 49)]
    assert sum(1 for b in bugs if any(s.startswith("workflow-finding:") for s in b["sources"])) == 3


def test_the_same_finding_on_five_days_from_five_processes_is_one_bug_with_five_days(env, tmp_path):
    reports, tracker = env
    start = _release_at()
    procs = [
        _spawn(tmp_path, "http", reports, tracker, start, json.dumps(_payload(day))) for day in DAYS
    ]
    results = _finish(procs)
    assert [r["http"] for r in results].count(201) == 1
    bugs = _bugs(tracker)
    assert len(bugs) == 1
    assert bugs[0]["occurrences"] == sorted(DAYS)
    assert len(bugs[0]["evidence"]) == 5
    notes = [h["note"] for h in bugs[0]["history"]]
    assert notes[0] == "recorded" and len([n for n in notes if n.startswith("seen on")]) == 4


def test_a_writer_killed_while_holding_the_lock_does_not_block_the_next_one(env, tmp_path):
    reports, tracker = env
    ready = tmp_path / "ready"
    holder = _spawn(tmp_path, "hold", tracker, 120, ready)
    deadline = time.time() + 60
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "the holder never took the lock"
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=10)

    started = time.time()
    result = _finish(
        [_spawn(tmp_path, "http", reports, tracker, time.time(), json.dumps(_payload()))]
    )[0]
    assert result["http"] == 201
    assert time.time() - started < 30
    assert len(_bugs(tracker)) == 1


def test_a_crash_between_writing_and_swapping_leaves_the_old_tracker_whole(env, tmp_path):
    reports, tracker = env
    good = _spawn(tmp_path, "http", reports, tracker, time.time(), json.dumps(_payload()))
    _finish([good])
    before = tracker.read_text()

    crashed = _spawn(tmp_path, "crash", tracker)
    crashed.communicate(timeout=60)
    assert crashed.returncode == 7

    assert tracker.read_text() == before, "the tracker changed although the swap never happened"
    assert len(_bugs(tracker)) == 1
    # the next writer is not blocked by the dead one and its write is complete
    after = _finish(
        [_spawn(tmp_path, "http", reports, tracker, time.time(), json.dumps(_logs_payload()))]
    )[0]
    assert after["http"] == 201
    assert [b["id"] for b in _bugs(tracker)] == ["BUG-001", "BUG-002"]


def test_a_writer_that_waits_too_long_is_told_the_tracker_is_busy_then_recovers(env, tmp_path):
    reports, tracker = env
    ready = tmp_path / "ready"
    holder = _spawn(tmp_path, "hold", tracker, 40, ready)
    deadline = time.time() + 60
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    try:
        # LOCK_TIMEOUT is 10s in the child, shorter than the 40s hold
        busy = _finish(
            [_spawn(tmp_path, "http", reports, tracker, time.time(), json.dumps(_payload()))]
        )[0]
        assert busy["http"] == 503
        assert busy["message"] == "The bug tracker is busy. Try again in a moment."
        assert _bugs(tracker) == []
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
    ok = _finish([_spawn(tmp_path, "http", reports, tracker, time.time(), json.dumps(_payload()))])[
        0
    ]
    assert ok["http"] == 201
