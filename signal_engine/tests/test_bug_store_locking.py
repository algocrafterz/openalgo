"""The bug tracker's lock and its atomic save, and the single-lock `report()` decision.

The cross-process behaviour (web route and CLI as separate processes, under the real gunicorn
workers) is in test/test_ops_findings_processes.py and test/test_gthread_ops_findings_workers.py.
Here: the lock mechanics, the Windows code path (simulated, because CI has no Windows), file
descriptors, and what a failed save leaves behind.
"""

import errno
import json
import os
import threading
import time

import pytest

from signal_engine.bugs import store

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "bugs.json")


def _add(path, n=1, **over):
    fields = {
        "title": f"Problem number {n}",
        "kind": "BUG",
        "severity": "LOW",
        "area": "ops",
        "seen": "2026-11-01",
        "impact": "x",
        "path": path,
    }
    return store.add(**{**fields, **over})


def _report(path, key="workflow-finding:a", day="2026-11-03", **over):
    fields = {
        "source_key": key,
        "title": "A finding",
        "kind": "EXECUTION",
        "severity": "MEDIUM",
        "area": "ops",
        "day": day,
        "impact": "Not assessed",
        "evidence": [f"seen {day}"],
        "sources": [key],
        "path": path,
    }
    return store.report(**{**fields, **over})


# --- the lock ---------------------------------------------------------------------------------


@pytest.mark.skipif(store.fcntl is None, reason="POSIX flock")
def test_a_second_holder_waits_by_polling_and_times_out_with_a_plain_error(path, monkeypatch):
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.3)
    with store._locked(path):
        started = time.monotonic()
        with pytest.raises(store.TrackerBusy) as info:
            with store._locked(path):
                pass
        assert 0.25 <= time.monotonic() - started < 2
    assert "Try again in a moment" in str(info.value)
    assert issubclass(store.TrackerBusy, store.TrackerError)  # the CLI prints it as a refusal
    with store._locked(path):  # and the lock is free again
        pass


@pytest.mark.skipif(store.fcntl is None, reason="POSIX flock")
def test_the_wait_goes_through_time_sleep_so_eventlet_can_yield(path, monkeypatch):
    """A C-level wait cannot be yielded; a time.sleep poll can (eventlet patches time.sleep)."""
    sleeps = []
    real = time.sleep
    monkeypatch.setattr(store.time, "sleep", lambda s: (sleeps.append(s), real(0.01)))
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.2)
    with store._locked(path):
        with pytest.raises(store.TrackerBusy):
            with store._locked(path):
                pass
    assert sleeps and all(0 < s <= 0.1 for s in sleeps)


def test_the_windows_lock_path_is_non_blocking_and_unlocks(path, monkeypatch):
    """CI has no Windows: drive the msvcrt branch with a stand-in that refuses while held."""

    class FakeMsvcrt:
        LK_NBLCK, LK_UNLCK = 2, 0
        held = False
        calls: list = []

        @classmethod
        def locking(cls, fd, mode, nbytes):
            cls.calls.append((mode, nbytes))
            if mode == cls.LK_NBLCK:
                if cls.held:
                    raise PermissionError(errno.EACCES, "locked")
                cls.held = True
            else:
                cls.held = False

    monkeypatch.setattr(store, "fcntl", None)
    monkeypatch.setattr(store, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.2)
    with store._locked(path):
        assert FakeMsvcrt.held
        with pytest.raises(store.TrackerBusy):
            with store._locked(path):
                pass
    assert FakeMsvcrt.held is False
    assert (FakeMsvcrt.LK_UNLCK, 1) in FakeMsvcrt.calls
    with store._locked(path):
        pass


def test_a_lock_error_that_is_not_contention_is_not_swallowed(path, monkeypatch):
    class Broken:
        LK_NBLCK, LK_UNLCK = 2, 0

        @staticmethod
        def locking(fd, mode, nbytes):
            raise OSError(errno.EBADF, "bad descriptor")

    monkeypatch.setattr(store, "fcntl", None)
    monkeypatch.setattr(store, "msvcrt", Broken)
    with pytest.raises(OSError):
        with store._locked(path):
            pass


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_no_descriptor_is_left_open_by_waits_timeouts_or_failures(path, monkeypatch):
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.05)
    _add(path)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(60):
        with store._locked(path):
            with pytest.raises(store.TrackerBusy):
                with store._locked(path):
                    pass
        with pytest.raises(RuntimeError):
            with store._locked(path):
                raise RuntimeError("body failed")
        store.load(path)
    assert len(os.listdir("/proc/self/fd")) <= before + 1


# --- the save ---------------------------------------------------------------------------------


def test_a_failed_save_leaves_the_old_tracker_untouched_and_no_temp_file(path, monkeypatch):
    _add(path)
    before = open(path).read()

    def half_written(data, handle, **kwargs):
        handle.write('{"version": 1, "bugs": [')  # cut off mid-document
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(store.json, "dump", half_written)
    with pytest.raises(OSError):
        _add(path, n=2)
    monkeypatch.undo()
    assert open(path).read() == before
    assert json.loads(before)["bugs"][0]["id"] == "BUG-001"
    assert [f for f in os.listdir(os.path.dirname(path)) if f.startswith(".bugs-")] == []


def test_the_new_file_is_flushed_to_disk_before_it_replaces_the_tracker(path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(store.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(
        store.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))
    )
    _add(path)
    assert order == ["fsync", "replace"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_a_save_keeps_the_trackers_permissions(path):
    _add(path)
    os.chmod(path, 0o640)
    _add(path, n=2)
    assert oct(os.stat(path).st_mode & 0o777) == "0o640"


def test_on_windows_a_swap_refused_by_a_reader_is_retried(path, monkeypatch):
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(errno.EACCES, "file in use")
        os.rename(src, dst)

    with monkeypatch.context() as m:
        m.setattr(store.os, "name", "nt")
        m.setattr(store.os, "replace", flaky)
        m.setattr(store.time, "sleep", lambda s: None)
        open(path, "w").write("{}")
        src = path + ".new"
        open(src, "w").write('{"version": 1, "bugs": []}')
        store._replace(src, path)
    assert calls["n"] == 3 and json.load(open(path))["bugs"] == []


def test_on_linux_a_refused_swap_is_not_retried_or_hidden(path, monkeypatch):
    monkeypatch.setattr(store.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        store._replace(path + ".x", path)


# --- threads in one process (flock is per open file, so this holds in gthread) ----------------


def test_many_threads_adding_at_once_lose_nothing(path):
    barrier = threading.Barrier(12)

    def go(n):
        barrier.wait()
        _add(path, n=n)

    threads = [threading.Thread(target=go, args=(n,)) for n in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ids = [b["id"] for b in store.load(path)["bugs"]]
    assert sorted(ids) == [f"BUG-{i:03d}" for i in range(1, 13)]


def test_many_threads_reporting_one_finding_make_one_bug_and_record_each_day_once(path):
    days = ["2026-11-03", "2026-11-04", "2026-11-05"]
    barrier = threading.Barrier(12)
    results = []

    def go(day):
        barrier.wait()
        results.append(_report(path, day=day))

    threads = [threading.Thread(target=go, args=(days[i % 3],)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    bugs = store.load(path)["bugs"]
    assert len(bugs) == 1 and bugs[0]["occurrences"] == days
    assert sum(r["created"] for r in results) == 1
    assert len([h for h in bugs[0]["history"] if h["note"].startswith("seen on")]) == 2


# --- report(): one decision under one lock ----------------------------------------------------


def test_report_creates_then_is_idempotent_for_the_same_day(path):
    first = _report(path)
    before = open(path).read()
    again = _report(path)
    assert first["created"] and not again["created"] and not again["recorded"]
    assert open(path).read() == before


def test_report_adds_a_new_day_with_the_seen_rule_and_reopens_only_after_the_fix(path):
    _report(path)
    store.update("BUG-001", status="fixed", fix="changed", note="fixed", path=path)
    fixed_day = time.strftime("%Y-%m-%d")
    same = _report(path, day=fixed_day)
    assert same["bug"]["status"] == "fixed"  # on or before the fix: the old code
    later = _report(path, day="2099-01-01")
    assert later["bug"]["status"] == "open"


def test_report_needs_the_key_among_the_sources_and_a_valid_form(path):
    with pytest.raises(store.TrackerError):
        _report(path, sources=["something else"])
    with pytest.raises(store.TrackerError):
        _report(path, severity="HUGE")
    with pytest.raises(store.TrackerError):
        _report(path, day="03/11/2026")
    with pytest.raises(store.TrackerError):
        _report(path, impact="  ")
    assert store.load(path)["bugs"] == []


def test_report_links_one_unambiguous_same_title_bug_of_the_same_kind_and_component(path):
    _add(path, title="A finding", kind="EXECUTION", area="ops")
    result = _report(path)
    assert not result["created"] and result["recorded"]
    assert "workflow-finding:a" in result["bug"]["sources"]
    assert len(store.load(path)["bugs"]) == 1


@pytest.mark.parametrize("over", [{"kind": "DATA"}, {"area": "broker"}])
def test_report_will_not_guess_when_kind_or_component_differ(path, over):
    _add(path, title="A finding", kind="EXECUTION", area="ops")
    before = open(path).read()
    with pytest.raises(store.AmbiguousMatch) as info:
        _report(path, **over)
    assert info.value.candidates == ["BUG-001"]
    assert open(path).read() == before


def test_report_will_not_guess_between_several_candidates(path):
    _add(path, title="A finding", kind="EXECUTION", area="ops")
    data = store.load(path)
    data["bugs"].append({**data["bugs"][0], "id": "BUG-002"})
    store._save(data, path)
    with pytest.raises(store.AmbiguousMatch) as info:
        _report(path)
    assert info.value.candidates == ["BUG-001", "BUG-002"]


def test_report_prefers_the_finding_key_to_any_title_and_ignores_closed_bugs(path):
    _report(path)
    store.update("BUG-001", title="Renamed by hand", path=path)
    assert _report(path, day="2026-11-04")["bug"]["id"] == "BUG-001"
    other = _add(path, title="Other thing", kind="BUG")
    store.update(other["id"], status="wontfix", note="n", path=path)
    assert _report(path, key="workflow-finding:b", title="Other thing", kind="BUG")["created"]


def test_the_cli_and_report_share_one_title_rule(path):
    """add() refuses a title an open bug has, and report() with the key still finds its bug."""
    _report(path)
    with pytest.raises(store.TrackerError):
        _add(path, title="A finding")
