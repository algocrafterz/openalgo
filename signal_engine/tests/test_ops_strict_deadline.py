"""The strict end of the 16:01 retry: everything ends by 16:27:00 IST, the 16:28 dead-man keeps
its clear window and the 16:30 limit is never touched.

The clock is OPS_RECOVERY_NOW (shell) or a fake clock (python). `timeout` is a stub that records
its limit. Nothing real is started, sent or written outside tmp_path.
"""

import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from signal_engine.ops import automation, model, recovery
from signal_engine.tests import test_ops_recovery as rec
from signal_engine.tests import test_ops_recovery_shell as shl

pytestmark = pytest.mark.filterwarnings("ignore")


DAY, Clock, Sim, at = rec.DAY, rec.Clock, rec.Sim, rec.at
run_retry, steps_run = rec.run_retry, rec.steps_run
FAKE_WEEKDAY, ROOT, run = shl.FAKE_WEEKDAY, shl.ROOT, shl.run
world = rec.world  # the fixtures of the two test files, reused as they are
tree = shl.tree


def stamp(clock: str) -> datetime:
    return model.at_ist(DAY, clock)


# ---- the constants and the arithmetic -----------------------------------------------------------


def test_the_times_of_the_window_are_the_stated_ones_and_defined_once():
    assert model.RETRY_DEADLINE == "16:25" == recovery.RETRY_DEADLINE
    assert model.RETRY_HARD_END == "16:27" == recovery.RETRY_HARD_END
    assert model.RETRY_OUTER_TERM == "16:26:40"
    assert model.RETRY_KILL_AFTER_SECONDS == 15
    assert model.RETRY_MIN_START_SECONDS == 30 == recovery.RETRY_MIN_START_SECONDS
    assert recovery.NOW_ENV == model.NOW_ENV == "OPS_RECOVERY_NOW"


def test_the_outer_timeout_arithmetic_ends_before_the_hard_end_and_the_deadman():
    term = stamp(model.RETRY_OUTER_TERM)
    killed = term + timedelta(seconds=model.RETRY_KILL_AFTER_SECONDS)
    assert killed == stamp("16:26:55")  # 16:26:40 + 15 s
    assert killed <= stamp(model.RETRY_HARD_END)
    assert (stamp(model.RETRY_HARD_END) - killed).total_seconds() == 5  # the margin
    assert stamp(model.RETRY_HARD_END) < stamp(automation.FINAL_DEADMAN_CLOSE) == stamp("16:28")
    assert stamp(model.RETRY_HARD_END) < stamp("16:30")  # the 16:30 limit is never reached
    assert stamp(model.RETRY_DEADLINE) < term  # the soft deadline comes first


def test_the_sigterm_handling_of_the_retry_fits_inside_the_kill_after():
    worst = recovery.SIGNAL_TERM_WAIT_SECONDS + recovery.SIGNAL_KILL_WAIT_SECONDS
    assert worst == 7 < model.RETRY_KILL_AFTER_SECONDS


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("16:01:00", (1540, 15)),  # TERM at 16:26:40, 1540 s later
        ("16:24:00", (160, 15)),
        ("16:26:35", (5, 15)),  # the last start with a normal window
        ("16:26:36", (8, 1)),  # a late start: only the records are written
        ("16:26:50", (8, 1)),
        ("16:26:56", (2, 1)),
        ("16:26:57", (1, 1)),
        ("16:26:58", None),  # less than 3 s remain: nothing can be recorded
        ("16:27:30", None),
    ],
)
def test_the_outer_limits_for_a_start_time(clock, expected):
    assert model.retry_outer_limits(stamp(clock), DAY) == expected


def test_every_start_second_ends_by_the_hard_end():
    now = stamp("16:00:00")
    while now < stamp("16:27:00"):
        limits = model.retry_outer_limits(now, DAY)
        if limits is not None:
            ends = now + timedelta(seconds=sum(limits))
            assert ends <= stamp("16:27:00"), (now, limits)
        now += timedelta(seconds=1)


def test_another_day_than_today_gets_a_short_fixed_limit():
    assert model.retry_outer_limits(stamp("16:01:00"), "2026-11-11") == (120, 15)


def test_the_command_line_helper_prints_the_limits_and_refuses_late(monkeypatch, capsys):
    monkeypatch.setenv(model.NOW_ENV, f"{DAY}T16:01:00")
    assert model._cli(["retry-limits", "--day", DAY]) == 0
    assert capsys.readouterr().out == "1540 15\n"
    monkeypatch.setenv(model.NOW_ENV, f"{DAY}T16:27:01")
    assert model._cli(["retry-limits", "--day", DAY]) == 0
    assert capsys.readouterr().out == "refuse\n"


# ---- the shell: limits from the wall clock, no copy of the times ----------------------------------

bash = shutil.which("bash")
shell = pytest.mark.skipif(bash is None, reason="needs bash on Linux")


def retry_at(tree, clock):
    repo, env, log = tree
    env = {**env, model.NOW_ENV: f"{FAKE_WEEKDAY}T{clock}"}
    res = run(repo, env, "signal_engine/analysis/close_day.sh", "--retry", FAKE_WEEKDAY)
    timeouts = Path(env["STUB_TIMEOUT_LOG"])
    seen = timeouts.read_text().splitlines() if timeouts.exists() else []
    return res, seen, log


@shell
@pytest.mark.parametrize(
    ("clock", "limits"),
    [("16:01:00", "--kill-after=15 1540"), ("16:24:00", "--kill-after=15 160")],
)
def test_the_shell_passes_the_computed_limits_to_timeout(tree, clock, limits):
    res, seen, log = retry_at(tree, clock)
    assert res.returncode == 0, res.stderr + res.stdout
    assert len(seen) == 1 and seen[0].startswith(limits + " "), seen
    assert "close-day-retry" in log.read_text()


@shell
def test_the_shell_gives_a_late_start_a_short_limit(tree):
    res, seen, log = retry_at(tree, "16:26:50")
    assert res.returncode == 0, res.stderr + res.stdout
    assert seen[0].startswith("--kill-after=1 8 ")  # 16:26:50 + 8 + 1 = 16:26:59


@shell
def test_the_shell_does_not_start_python_after_the_window(tree):
    res, seen, log = retry_at(tree, "16:27:10")
    assert res.returncode == 0 and "not retrying" in res.stdout
    assert seen == [] and not log.exists()


@shell
def test_the_shell_stops_when_the_limits_cannot_be_worked_out(tree):
    repo, env, log = tree
    (repo / "signal_engine/ops/model.py").unlink()
    res = run(repo, env, "signal_engine/analysis/close_day.sh", "--retry", FAKE_WEEKDAY)
    assert res.returncode == 1 and "could not be worked out" in res.stdout
    assert not log.exists()
    assert not Path(env["STUB_TIMEOUT_LOG"]).exists()


def test_the_shell_holds_no_copy_of_the_retry_times():
    code = [
        ln
        for ln in (ROOT / "signal_engine/analysis/close_day.sh").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    for ln in code:
        assert "16:2" not in ln and "RETRY_END" not in ln, ln
    assert "retry-limits" in "\n".join(code)


# ---- the python retry: start times ----------------------------------------------------------------


def test_a_retry_at_1601_runs_its_open_steps_and_ends_before_the_soft_deadline(world):
    rc, sim, clock, _ = run_retry(world, at(DAY, "16:01"), {"track": 1})
    assert steps_run(sim) == ["track"] and rc == 0
    assert clock() < at(DAY, "16:25")


def test_a_retry_at_1624_starts_no_step_because_none_may_start_after_1623(world):
    rc, sim, clock, _ = run_retry(world, at(DAY, "16:24"), {"track": 1})
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    assert steps_run(sim) == []  # no step may start after 16:23
    assert rows["track"]["result"] == "incomplete" and "notify" not in rows
    assert clock() <= at(DAY, "16:25") and rc == 1


def test_a_retry_with_less_than_30_seconds_left_does_not_start_and_says_so(world):
    rc, sim, _, lines = run_retry(world, at(DAY, "16:24", 40), {"backfill": 1, "track": 1})
    manifest = world.manifest("close-day-retry")
    assert sim.calls == []
    assert manifest["state"] == "timed_out" and "started too late" in manifest["note"]
    assert {r["step"]: r["result"] for r in manifest["steps"]} == {
        "backfill": "incomplete",
        "track": "incomplete",
    }
    status = world.status_text()
    assert status.count("\t124\n") == 2  # backfill and track, in the close-day status file
    assert any("started too late" in ln and "16:25" in ln for ln in lines)
    assert rc == 1


def test_a_retry_with_exactly_30_seconds_left_starts(world):
    rc, sim, _, _ = run_retry(world, at(DAY, "16:24", 30), {"track": 1})
    manifest = world.manifest("close-day-retry")
    assert "started too late" not in manifest["note"]  # it started; the 16:23 cutoff held the step
    assert steps_run(sim) == []


def test_a_retry_at_162650_is_not_started_and_the_record_is_complete(world):
    rc, sim, _, lines = run_retry(world, at(DAY, "16:26", 50), {"backfill": 1})
    manifest = world.manifest("close-day-retry")
    assert sim.calls == [] and rc == 1
    assert manifest["state"] == "timed_out" and "started too late" in manifest["note"]
    assert "Daily Workflow" in manifest["note"]  # the next action, in plain words
    assert "16:26:50" in manifest["note"]
    assert any("started too late" in ln for ln in lines)


def test_a_late_start_with_nothing_open_writes_nothing(world):
    rc, sim, _, lines = run_retry(world, at(DAY, "16:26", 50))  # every step succeeded
    assert rc == 0 and sim.calls == [] and any("nothing to retry" in ln for ln in lines)
    assert not Path(world.folder, f"close-day-retry-{DAY}-run.json").exists()


def test_a_host_sleep_over_the_deadline_stops_all_later_steps(world):
    world.seed({"eod": 1, "logs": 1, "track": 1})
    sim = Sim(world, Clock(at(DAY, "16:10")))
    sim.takes = {"eod": 30 * 60}  # the host sleeps 30 minutes inside the first step
    rc, sim, clock, _ = run_retry(world, at(DAY, "16:10"), sim=sim, seed=False)
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    assert steps_run(sim) == ["eod"]  # nothing starts after the jump to 16:40
    assert rows["eod"]["result"] == "timed_out"
    assert rows["logs"]["result"] == rows["track"]["result"] == "incomplete"
    assert "notify" not in rows
    assert world.manifest("close-day-retry")["state"] == "timed_out" and rc == 1


def test_the_real_runner_cuts_a_step_after_a_clock_jump_and_ends_before_the_hard_end(tmp_path):
    clock = Clock(at(DAY, "16:10"))
    ctx = recovery.Ctx(
        mode="retry", day=DAY, phase="x", deadline=at(DAY, "16:25"),
        step_cutoff=at(DAY, "16:23"), review_latest_start=at(DAY, "16:20"),
        folder=str(tmp_path), logs_dir=str(tmp_path), clock=clock, sleep=lambda s: None,
    )  # fmt: skip
    ticks = iter([0, 0, 1])

    def jumpy():
        if next(ticks, 1):
            clock.now = at(DAY, "16:40")
        return clock.now

    ctx.clock = jumpy
    started = time.monotonic()
    rc, cut = recovery.make_runner(ctx)(
        ["sleep", "60"], dict(os.environ), str(tmp_path / "o.log"), 600, at(DAY, "16:23")
    )
    assert (rc, cut) == (124, True)
    # The longest real wait of a cut is TERM_WAIT + TERM_WAIT: 20 s. From the 16:25 deadline that
    # is 16:25:20, which is before the hard end (16:27:00) by a wide margin.
    assert time.monotonic() - started < 20
    assert at(DAY, "16:25") + timedelta(seconds=2 * recovery.TERM_WAIT_SECONDS) < stamp("16:27")


# ---- TERM handling --------------------------------------------------------------------------------


def test_term_handling_of_the_retry_finishes_inside_the_kill_after_and_before_the_hard_end(
    tmp_path,
):
    """The outer timeout sends TERM at 16:26:40. A child that ignores TERM is killed after the
    short wait, the manifest says interrupted and the process exits, all before 16:26:55."""
    ctx = recovery.Ctx(
        mode="retry", day=DAY, phase="close-day-retry", deadline=at(DAY, "16:25"),
        step_cutoff=at(DAY, "16:23"), review_latest_start=at(DAY, "16:20"),
        folder=str(tmp_path), logs_dir=str(tmp_path),
    )  # fmt: skip
    recovery._begin(ctx)
    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [bash or "bash", "-c", f"trap '' TERM; touch {ready}; sleep 30"], start_new_session=True
    )
    for _ in range(100):  # the TERM must arrive after the trap is set
        if ready.exists():
            break
        time.sleep(0.05)
    assert ready.exists()
    ctx.children.append(child)
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        recovery._install_signal_handlers(ctx, {})
        started = time.monotonic()
        with pytest.raises(SystemExit) as stopped:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        elapsed = time.monotonic() - started
    finally:
        for number, handler in old.items():
            signal.signal(number, handler)
        child.kill()
        child.wait()
    assert stopped.value.code == 128 + signal.SIGTERM
    assert child.poll() is not None
    assert elapsed >= recovery.SIGNAL_TERM_WAIT_SECONDS  # it ignored TERM: the KILL was needed
    assert elapsed < recovery.SIGNAL_TERM_WAIT_SECONDS + recovery.SIGNAL_KILL_WAIT_SECONDS + 2
    finished = stamp(model.RETRY_OUTER_TERM) + timedelta(seconds=elapsed)
    assert finished < stamp("16:26:55") < stamp(model.RETRY_HARD_END)
    assert model.read_run("close-day-retry", DAY, str(tmp_path))["state"] == "interrupted"


def test_the_morning_recovery_keeps_its_longer_wait_when_it_gets_a_term(tmp_path, monkeypatch):
    waits = {}
    monkeypatch.setattr(recovery, "_kill_group", lambda proc, *w: waits.setdefault(proc.mode, w))

    class Child:
        def __init__(self, mode):
            self.mode = mode

        def poll(self):
            return None

    old = signal.getsignal(signal.SIGTERM)
    try:
        for mode in ("retry", "recover"):
            ctx = recovery.Ctx(
                mode=mode, day=DAY, phase=f"close-day-{mode}", deadline=at(DAY, "16:25"),
                step_cutoff=at(DAY, "16:23"), review_latest_start=at(DAY, "16:20"),
                folder=str(tmp_path), logs_dir=str(tmp_path),
            )  # fmt: skip
            recovery._begin(ctx)
            ctx.children.append(Child(mode))
            recovery._install_signal_handlers(ctx, {})
            with pytest.raises(SystemExit):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, old)
    assert waits["retry"] == (5, 2)
    assert waits["recover"] == (recovery.TERM_WAIT_SECONDS, recovery.TERM_WAIT_SECONDS) == (10, 10)


def test_the_morning_recovery_job_is_unchanged():
    assert recovery.RECOVER_HARD_STOP == "08:55"
    text = (ROOT / "signal_engine/ops/run_upkeep.sh").read_text()
    assert "LIMIT=1140" in text and "retry-limits" not in text
    assert automation.FINAL_DEADMAN_CLOSE == "16:28"
