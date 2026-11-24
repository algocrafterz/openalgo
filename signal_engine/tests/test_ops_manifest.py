"""Run manifest: lifecycle, atomic write, readers, and the shell scripts on a stub environment.

The shell tests copy the real scripts into a temporary tree and put stub commands first on PATH.
Nothing here runs a real phase, the real eod, the real review or Telegram.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from signal_engine.ops import model, verdict

ROOT = Path(__file__).resolve().parents[2]
DAY = "2026-11-12"  # a Thursday, in the past: close_day.sh then uses its 20-minute budget
NOW = datetime(2026, 11, 12, 15, 50, tzinfo=model.IST)


# ---- manifest lifecycle ------------------------------------------------------------------------


def _start(tmp_path, phase="close-day", pid=None, when=None):
    data = model.manifest_start(phase, DAY, pid=pid or os.getpid(), folder=str(tmp_path))
    if when:
        data["started_at"] = when.isoformat(timespec="seconds")
        model.atomic_write_json(model.manifest_path(phase, DAY, str(tmp_path)), data)
    return data


def test_a_started_run_is_running_with_id_pid_and_ist_start_time(tmp_path):
    data = _start(tmp_path)
    assert data["state"] == "running"
    assert data["pid"] == os.getpid()
    assert data["run_id"].startswith(f"close-day-{DAY}-")
    assert data["started_at"].endswith("+05:30")
    got = model.read_run("close-day", DAY, str(tmp_path))
    assert got["state"] == "running"  # this pid is alive and the run is young


def test_a_run_that_finishes_cleanly_is_completed(tmp_path):
    _start(tmp_path)
    for name in model.CRITICAL_STEPS["close-day"]:
        model.manifest_step("close-day", DAY, name, "15:36:00", "15:36:09", 0, str(tmp_path))
    model.manifest_finish("close-day", DAY, folder=str(tmp_path))
    got = model.read_run("close-day", DAY, str(tmp_path))
    assert got["state"] == "completed"
    assert got["ended_at"]
    assert [s["step"] for s in got["steps"]] == list(model.CRITICAL_STEPS["close-day"])


def test_a_failed_critical_step_makes_the_run_failed(tmp_path):
    _start(tmp_path)
    model.manifest_step("close-day", DAY, "backfill", "15:36:00", "15:40:00", 1, str(tmp_path))
    model.manifest_finish("close-day", DAY, folder=str(tmp_path))
    assert model.read_run("close-day", DAY, str(tmp_path))["state"] == "failed"


def test_a_reporting_step_exiting_one_is_not_a_failed_run(tmp_path):
    _start(tmp_path, "open-day")
    model.manifest_step("open-day", DAY, "check", "09:12:00", "09:12:30", 1, str(tmp_path))
    model.manifest_step("open-day", DAY, "notify", "09:12:30", "09:12:31", 0, str(tmp_path))
    model.manifest_finish("open-day", DAY, folder=str(tmp_path))
    assert model.read_run("open-day", DAY, str(tmp_path))["state"] == "completed"


def test_a_step_at_the_deadline_makes_the_run_timed_out(tmp_path):
    _start(tmp_path)
    model.manifest_step("close-day", DAY, "review", "15:43:00", "15:58:00", 124, str(tmp_path))
    model.manifest_finish("close-day", DAY, folder=str(tmp_path))
    assert model.read_run("close-day", DAY, str(tmp_path))["state"] == "timed_out"


def test_an_explicit_interrupted_state_is_kept_and_a_second_finish_does_not_rewrite_it(tmp_path):
    _start(tmp_path)
    model.manifest_finish("close-day", DAY, "interrupted", "stopped by signal TERM", str(tmp_path))
    model.manifest_finish("close-day", DAY, folder=str(tmp_path))  # the exit trap, later
    got = model.read_run("close-day", DAY, str(tmp_path))
    assert got["state"] == "interrupted"
    assert "signal" in got["note"]


def test_running_with_a_dead_pid_reads_as_interrupted(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _start(tmp_path, pid=proc.pid)
    got = model.read_run("close-day", DAY, str(tmp_path))
    assert got["state"] == "interrupted"
    assert got["stored_state"] == "running"
    assert "gone" in got["note"]


def test_stale_running_reads_as_interrupted_even_if_the_pid_is_alive(tmp_path):
    old = datetime.now(model.IST) - timedelta(seconds=model.MAX_RUN_SECONDS + 60)
    _start(tmp_path, when=old)  # pid is this test process: alive
    got = model.read_run("close-day", DAY, str(tmp_path))
    assert got["state"] == "interrupted"
    assert "longer than the maximum" in got["note"]


def test_a_running_manifest_is_never_read_as_success(tmp_path):
    _start(tmp_path)
    assert model.read_run("close-day", DAY, str(tmp_path))["state"] not in ("completed",)


def test_missing_and_legacy_and_unreadable(tmp_path):
    assert model.read_run("close-day", DAY, str(tmp_path))["state"] == "missing"
    (tmp_path / f"close-day-{DAY}-status.tsv").write_text("step\tstarted\tended\texit\n")
    assert model.read_run("close-day", DAY, str(tmp_path))["state"] == "legacy"
    (tmp_path / f"close-day-{DAY}-run.json").write_text("{ not json")
    assert model.read_run("close-day", DAY, str(tmp_path))["state"] == "unreadable"
    (tmp_path / f"close-day-{DAY}-run.json").write_text('{"state": "wat"}')
    assert model.read_run("close-day", DAY, str(tmp_path))["state"] == "unreadable"


def test_updates_to_a_damaged_manifest_do_nothing_and_do_not_raise(tmp_path):
    (tmp_path / f"close-day-{DAY}-run.json").write_text("{ not json")
    assert model.manifest_step("close-day", DAY, "eod", "a", "b", 0, str(tmp_path)) is None
    assert (tmp_path / f"close-day-{DAY}-run.json").read_text() == "{ not json"


def test_the_review_attempt_is_recorded_after_the_run_ended(tmp_path):
    _start(tmp_path)
    model.manifest_finish("close-day", DAY, folder=str(tmp_path))
    model.manifest_review("close-day", DAY, "verdict", f"{DAY}-183500", 0, str(tmp_path))
    got = model.read_run("close-day", DAY, str(tmp_path))
    assert got["state"] == "completed"
    assert got["review"]["status"] == "verdict"


# ---- atomic write ------------------------------------------------------------------------------


def test_atomic_write_leaves_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    model.atomic_write_json(str(path), {"a": 1})

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(model.os, "replace", boom)
    with pytest.raises(OSError):
        model.atomic_write_json(str(path), {"a": 2})
    assert json.loads(path.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # no temp file left behind


def test_atomic_write_calls_fsync_before_replace(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(model.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        model.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1]
    )
    model.atomic_write_json(str(tmp_path / "x.json"), {"a": 1})
    assert order.index("fsync") < order.index("replace")


def test_concurrent_step_writers_lose_no_row(tmp_path):
    _start(tmp_path)
    errors = []

    def write(i):
        try:
            model.manifest_step("close-day", DAY, f"s{i}", "t", "t", 0, str(tmp_path))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    rows = json.loads((tmp_path / f"close-day-{DAY}-run.json").read_text())["steps"]
    assert sorted(r["step"] for r in rows) == sorted(f"s{i}" for i in range(20))


def test_concurrent_processes_lose_no_row(tmp_path):
    _start(tmp_path)
    code = (
        "import sys; from signal_engine.ops import model; "
        "model.manifest_step('close-day', sys.argv[2], sys.argv[3], 'a', 'b', 0, sys.argv[1])"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(tmp_path), DAY, f"p{i}"], env=env)
        for i in range(8)
    ]
    assert [p.wait(timeout=60) for p in procs] == [0] * 8
    rows = json.loads((tmp_path / f"close-day-{DAY}-run.json").read_text())["steps"]
    assert len(rows) == 8


def test_the_lock_times_out_instead_of_waiting_forever(tmp_path):
    with model.file_lock(str(tmp_path)):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with model.file_lock(str(tmp_path), timeout=0.3):
                pass
        assert time.monotonic() - started < 3


def test_the_cli_never_raises_into_a_phase_when_a_write_fails(tmp_path, capsys):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a folder")
    rc = model._cli(
        ["manifest", "start", "--phase", "close-day", "--day", DAY, "--folder", str(blocker)]
    )
    assert rc == 1
    assert "failed" in capsys.readouterr().err


# ---- shell behaviour on a stub environment ------------------------------------------------------

bash = shutil.which("bash")
pytestmark_shell = pytest.mark.skipif(
    bash is None or sys.platform == "win32", reason="needs bash on Linux"
)

STUB_UV = """#!/usr/bin/env bash
# Stub for uv. Logs the call. STUB_FAIL=<word>: a call containing that word exits 3.
# STUB_SLEEP=<word>: a call containing that word sleeps 30 s first.
echo "$*" >> "$STUB_LOG"
case " $* " in *" $STUB_SLEEP "*) [ -n "${STUB_SLEEP:-}" ] && exec sleep 30 ;; esac
case " $* " in *" $STUB_FAIL "*) [ -n "${STUB_FAIL:-}" ] && exit 3 ;; esac
case "$*" in
  *"signal_engine.ops open-day"*|*"signal_engine.ops liveness"*)
    echo "stub check" ;;
esac
exit 0
"""
STUB_CLAUDE = """#!/usr/bin/env bash
# Stub for the claude CLI. CLAUDE_MODE: ok (verdict, exit 0), okexit1 (verdict, exit 1),
# limit (usage-limit text, exit 1)
echo "stderr warning" >&2
case "${CLAUDE_MODE:-ok}" in
  ok|okexit1)
    printf 'Review text\\n```close-day-verdict\\n{"verdict": "AMBER", "line": "Net 100; watch one thing."}\\n```\\n```bug-tracker\\n[]\\n```\\n'
    [ "$CLAUDE_MODE" = okexit1 ] && exit 1
    exit 0 ;;
  limit)
    echo "You've hit your 5-hour limit"; exit 1 ;;
  hang)
    echo "partial text"; exec sleep 30 ;;
esac
"""
STUB_OK = "#!/usr/bin/env bash\nexit 0\n"


@pytest.fixture
def tree(tmp_path):
    """A copy of the real scripts in a temporary repo, with stubs for everything they call."""
    repo = tmp_path / "repo"
    for rel in (
        "signal_engine/ops/run_phase.sh",
        "signal_engine/ops/model.py",
        "signal_engine/ops/verdict.py",
        "signal_engine/analysis/close_day.sh",
        "signal_engine/analysis/eod_claude_review.sh",
        "signal_engine/analysis/review_catchup.sh",
    ):
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
        dest.chmod(0o755)
    (repo / "signal_engine/__init__.py").write_text("")
    (repo / "signal_engine/ops/__init__.py").write_text("")
    for rel in ("signal_engine/analysis/eod.sh", "signal_engine/analysis/weekly.sh"):
        (repo / rel).write_text(STUB_OK)
        (repo / rel).chmod(0o755)
    (repo / "signal_engine/backtest").mkdir(parents=True)
    (repo / "signal_engine/backtest/backfill_cron.sh").write_text(STUB_OK)
    (repo / "signal_engine/backtest/backfill_cron.sh").chmod(0o755)
    (repo / "signal_engine/logs").mkdir()
    (repo / "signal_engine/reports").mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("uv", STUB_UV), ("claude", STUB_CLAUDE)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "HOME": str(home),
        "STUB_LOG": str(tmp_path / "uv.log"),
        "CLAUDE_TOKEN_FILE": str(tmp_path / "no-token"),
    }
    return repo, env


def _manifest(repo, phase):
    return json.loads((repo / f"signal_engine/reports/{phase}-{DAY}-run.json").read_text())


def _run(repo, env, script, *args, **extra):
    return subprocess.run(
        [str(repo / script), *args],
        env={**env, **extra},
        capture_output=True,
        text=True,
        timeout=120,
        cwd=repo,
    )


def _wait_for_step(path, env):
    """Wait until the manifest exists and the stubbed step has started (it then sleeps)."""
    deadline = time.time() + 20
    while time.time() < deadline and not (path.exists() and Path(env["STUB_LOG"]).exists()):
        time.sleep(0.1)
    assert path.exists()


@pytestmark_shell
def test_run_phase_writes_a_completed_manifest(tree):
    repo, env = tree
    res = _run(repo, env, "signal_engine/ops/run_phase.sh", "open-day", DAY)
    assert res.returncode == 0, res.stderr
    m = _manifest(repo, "open-day")
    assert m["state"] == "completed"
    assert m["pid"] > 0 and m["started_at"].endswith("+05:30")
    assert [s["step"] for s in m["steps"]] == ["check", "track"]
    assert (repo / f"signal_engine/reports/open-day-{DAY}-status.tsv").exists()  # TSV kept


@pytestmark_shell
def test_run_phase_records_a_failed_critical_step(tree):
    repo, env = tree
    res = _run(repo, env, "signal_engine/ops/run_phase.sh", "liveness", DAY, STUB_FAIL="liveness")
    assert res.returncode == 0
    m = _manifest(repo, "liveness")
    assert m["state"] == "failed"
    assert {s["step"]: s["exit"] for s in m["steps"]}["check"] == 3


@pytestmark_shell
def test_run_phase_skips_a_weekend_without_a_manifest(tree):
    repo, env = tree
    res = _run(repo, env, "signal_engine/ops/run_phase.sh", "open-day", "2026-11-14")
    assert "weekend" in res.stdout
    assert not list((repo / "signal_engine/reports").glob("*-run.json"))


@pytestmark_shell
def test_run_phase_trap_on_sigterm_leaves_an_interrupted_manifest(tree):
    repo, env = tree
    proc = subprocess.Popen(
        [str(repo / "signal_engine/ops/run_phase.sh"), "open-day", DAY],
        env={**env, "STUB_SLEEP": "open-day"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=repo,
    )
    path = repo / f"signal_engine/reports/open-day-{DAY}-run.json"
    _wait_for_step(path, env)
    assert json.loads(path.read_text())["state"] == "running"
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    m = json.loads(path.read_text())
    assert m["state"] == "interrupted"
    assert "signal TERM" in m["note"]
    assert model.read_run("open-day", DAY, str(path.parent))["state"] == "interrupted"


@pytestmark_shell
def test_run_phase_killed_hard_reads_as_interrupted(tree):
    repo, env = tree
    proc = subprocess.Popen(
        [str(repo / "signal_engine/ops/run_phase.sh"), "open-day", DAY],
        env={**env, "STUB_SLEEP": "open-day"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=repo,
        start_new_session=True,
    )
    path = repo / f"signal_engine/reports/open-day-{DAY}-run.json"
    _wait_for_step(path, env)
    os.killpg(proc.pid, signal.SIGKILL)  # no trap can run
    proc.wait(timeout=30)
    assert json.loads(path.read_text())["state"] == "running"  # nothing could update it
    assert model.read_run("open-day", DAY, str(path.parent))["state"] == "interrupted"


@pytestmark_shell
def test_close_day_writes_manifest_attempt_files_and_review_outcome(tree):
    repo, env = tree
    res = _run(repo, env, "signal_engine/analysis/close_day.sh", DAY, CLAUDE_MODE="okexit1")
    assert res.returncode == 0, res.stderr + res.stdout
    m = _manifest(repo, "close-day")
    assert m["state"] == "completed"
    names = [s["step"] for s in m["steps"]]
    for step in model.CRITICAL_STEPS["close-day"] + ("review",):
        assert step in names
    # the review exited 1 after a complete verdict: recorded as a verdict, exit kept as evidence
    assert m["review"]["status"] == "verdict"
    assert m["review"]["exit"] == 1
    attempts = repo / "signal_engine/reports/review-attempts"
    outs = sorted(attempts.glob(f"{DAY}-*.out"))
    assert len(outs) == 1
    base = outs[0].with_suffix("")
    assert (base.with_suffix(".exit")).read_text().strip() == "1"
    assert "stderr warning" in base.with_suffix(".err").read_text()
    assert "close-day-verdict" in outs[0].read_text()
    assert (attempts / f"{DAY}-latest.txt").read_text().strip() == base.name
    report = (repo / f"signal_engine/reports/eod-{DAY}-claude-review.md").read_text()
    assert "stderr warning" in report and "close-day-verdict" in report  # report shape kept


@pytestmark_shell
def test_a_second_review_attempt_never_overwrites_the_first(tree):
    repo, env = tree
    review = "signal_engine/analysis/eod_claude_review.sh"
    first = _run(repo, env, review, DAY, CLAUDE_MODE="limit")
    assert first.returncode == 1
    second = _run(repo, env, review, DAY, CLAUDE_MODE="ok")
    assert second.returncode == 0
    attempts = repo / "signal_engine/reports/review-attempts"
    outs = sorted(attempts.glob(f"{DAY}-*.out"), key=lambda p: verdict._attempt_order(p.stem))
    assert len(outs) == 2
    assert "5-hour limit" in outs[0].read_text()
    assert "close-day-verdict" in outs[1].read_text()
    assert outs[0].with_suffix(".exit").read_text().strip() == "1"
    assert outs[1].with_suffix(".exit").read_text().strip() == "0"
    assert list(attempts.glob("*.previous-report.md"))  # the first report was kept
    assert (attempts / f"{DAY}-latest.txt").read_text().strip() == outs[1].stem


@pytestmark_shell
def test_a_review_stopped_by_the_deadline_keeps_its_partial_output_and_exit_143(tree):
    repo, env = tree
    proc = subprocess.Popen(
        [str(repo / "signal_engine/analysis/eod_claude_review.sh"), DAY],
        env={**env, "CLAUDE_MODE": "hang"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=repo,
    )
    attempts = repo / "signal_engine/reports/review-attempts"
    deadline = time.time() + 20
    while time.time() < deadline and not (
        list(attempts.glob("*.out"))
        and "partial text" in next(iter(attempts.glob("*.out"))).read_text()
    ):
        time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    out = next(iter(attempts.glob("*.out")))
    assert out.with_suffix(".exit").read_text().strip() == "143"
    partial = repo / f"signal_engine/reports/eod-{DAY}-claude-review.md.partial"
    assert "partial text" in partial.read_text()  # close_day.sh turns this into the report


@pytestmark_shell
def test_close_day_with_a_usage_limit_review_records_limit_or_auth(tree):
    repo, env = tree
    _run(repo, env, "signal_engine/analysis/close_day.sh", DAY, CLAUDE_MODE="limit")
    m = _manifest(repo, "close-day")
    assert m["review"]["status"] == "limit_or_auth"
    assert m["state"] == "completed"  # the review is not a critical step


@pytestmark_shell
def test_close_day_trap_on_sigterm_leaves_an_interrupted_manifest(tree):
    repo, env = tree
    proc = subprocess.Popen(
        [str(repo / "signal_engine/analysis/close_day.sh"), DAY],
        env={**env, "STUB_SLEEP": "--format", "CLAUDE_MODE": "ok"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=repo,
    )
    path = repo / f"signal_engine/reports/close-day-{DAY}-run.json"
    _wait_for_step(path, env)
    time.sleep(1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    assert json.loads(path.read_text())["state"] == "interrupted"


@pytestmark_shell
def test_catchup_appends_a_review_row_so_the_stale_failure_is_replaced(tree):
    repo, env = tree
    reports = repo / "signal_engine/reports"
    (reports / f"close-day-{DAY}-status.tsv").write_text(
        "step\tstarted\tended\texit\nreview\t15:45:00\t15:45:21\t1\n"
    )
    res = _run(repo, env, "signal_engine/analysis/review_catchup.sh", DAY, CLAUDE_MODE="ok")
    assert res.returncode == 0, res.stderr + res.stdout
    rows = (reports / f"close-day-{DAY}-status.tsv").read_text().splitlines()
    review_rows = [r.split("\t") for r in rows if r.startswith("review")]
    assert review_rows[0][3] == "1" and review_rows[-1][3] == "0"  # old row kept, new row last
    assert not any(r.startswith("notify") for r in rows)  # no Telegram step any more
    assert verdict.latest_steps("close-day", DAY, str(reports))["review"] == 0
