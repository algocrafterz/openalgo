"""The 16:01 afternoon retry and the 08:36 morning recovery (signal_engine/ops/recovery.py).

Everything runs on tmp_path folders with a simulated command runner and a fake clock. No real
phase, backfill, eod, review, broker, OpenAlgo call or Telegram message is made: the runner never
starts a process, and alerts.deliver is replaced by a list.
"""

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from signal_engine.ops import actions, alerts, automation, jobs, model, recovery, verdict
from signal_engine.ops.model import IST, OK, Check, write_phase

DAY = "2026-11-12"  # Thursday
RECOVER_TODAY = "2026-11-13"  # Friday: the previous trading day is DAY
ALL_STEPS = tuple(recovery.STEPS)
pytestmark = pytest.mark.filterwarnings("ignore")


def at(day, hhmm, sec=0):
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST) + timedelta(
        seconds=sec
    )


class Clock:
    def __init__(self, now):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


REVIEW_OK = (
    'Review.\n```close-day-verdict\n{"verdict": "AMBER", "line": "Net 1; watch."}\n```\n'
    "```bug-tracker\n[]\n```\n"
)


class World:
    def __init__(self, tmp_path):
        self.folder = str(tmp_path / "reports")
        self.logs = str(tmp_path / "logs")
        self.snaps = tmp_path / "snaps"
        self.state = str(tmp_path / "state")
        os.makedirs(self.folder)
        self.snaps.mkdir()

    # -- seeding the 15:36 run ------------------------------------------------------------
    def seed(self, rows=None, *, state="failed", day=DAY, review=True, snapshot=True):
        """The close-day run of `day`: a status file, a finished manifest, the reports."""
        rows = dict.fromkeys(ALL_STEPS, 0) | (rows or {})
        status = "step\tstarted\tended\texit\n"
        model.manifest_start("close-day", day, pid=1, folder=self.folder)
        for step, code in rows.items():
            if code is None:
                continue
            status += f"{step}\t15:36:00\t15:36:09\t{code}\n"
            model.manifest_step("close-day", day, step, "15:36:00", "15:36:09", code, self.folder)
        Path(self.folder, f"close-day-{day}-status.tsv").write_text(status)
        model.manifest_finish("close-day", day, state, folder=self.folder)
        for name in ("logs", "value-areas", "jobs"):
            write_phase(name, day, [Check("a", OK, "x")], "s", folder=self.folder)
        if review:
            Path(self.folder, f"eod-{day}-claude-review.md").write_text(REVIEW_OK)
        if snapshot:
            (self.snaps / f"tradebook_{day}.json").write_text("[]")

    def status_text(self, day=DAY):
        return Path(self.folder, f"close-day-{day}-status.tsv").read_text()

    def manifest(self, phase, day=DAY):
        return json.loads(Path(self.folder, f"{phase}-{day}-run.json").read_text())


@pytest.fixture
def world(tmp_path, monkeypatch):
    # A readable calendar with no holiday: every weekday is a trading day. An unreadable calendar
    # makes the recovery skip itself (see the holiday-table tests below).
    monkeypatch.setattr(automation, "DB", _calendar(tmp_path, []))
    monkeypatch.delenv(recovery.NOW_ENV, raising=False)
    return World(tmp_path)


class Sim:
    """A command runner that writes the output a real step would write. Nothing is started."""

    def __init__(self, world, clock):
        self.world, self.clock = world, clock
        self.calls: list[tuple[str, list[str]]] = []
        self.mode: dict[str, str] = {}
        self.takes: dict[str, float] = {}

    @staticmethod
    def step_of(argv):
        text = " ".join(argv)
        for key, step in (
            ("backfill_cron.sh", "backfill"),
            ("eod.sh", "eod"),
            ("execution_health", "execution_health"),
            ("ops logs", "logs"),
            ("ops value-areas", "value_areas"),
            ("ops jobs", "jobs"),
            ("ops track", "track"),
            ("eod_claude_review.sh", "review"),
            ("ops notify", "notify"),
        ):
            if key in text.replace("signal_engine.", ""):
                return step
        raise AssertionError(f"an unexpected command: {argv}")

    def __call__(self, argv, env, out_path, budget, cutoff, stdout_path=None):
        step = self.step_of(argv)
        self.calls.append((step, list(argv)))
        self.clock.advance(self.takes.get(step, 5))
        mode = self.mode.get(step, "ok")
        folder, day = self.world.folder, self.current_day
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        log = Path(out_path)
        if mode == "fail":
            log.write_text("boom\n")
            return 1, False
        if mode == "cut":
            return 124, True
        if step == "backfill":
            log.write_text("done\n" if mode == "no_verify" else "--- verify ---\nall current\n")
        elif step == "eod":
            if mode != "no_output":
                Path(folder, f"eod-{day}.md").write_text("# EOD review\n")
                (self.world.snaps / f"tradebook_{day}.json").write_text("[]")
            else:
                Path(folder, f"eod-{day}.md").unlink(missing_ok=True)
        elif step == "execution_health":
            if mode != "empty":
                Path(stdout_path).write_text("# health\n")
            else:
                Path(stdout_path).write_text("")
        elif step in ("logs", "value_areas", "jobs"):
            name = {"value_areas": "value-areas"}.get(step, step)
            if mode != "no_output":
                write_phase(name, day, [Check("a", OK, "x")], "s", folder=folder)
            else:  # a leftover of the 15:36 run must not pass for this run's output
                Path(folder, f"{name}-{day}.json").unlink(missing_ok=True)
        elif step == "track":
            log.write_text(f"track {day}: 0 failed check(s)\n")
        elif step == "review":
            if mode == "ok":
                Path(folder, f"eod-{day}-claude-review.md").write_text(REVIEW_OK)
        return 0, False

    current_day = DAY


def run_retry(world, now, rows=None, *, sim=None, seed=True, **seed_kwargs):
    clock = Clock(now)
    sim = sim or Sim(world, clock)
    sim.clock = clock
    if seed:
        world.seed(rows, **seed_kwargs)
    lines = []
    rc = recovery.retry_main(
        DAY,
        clock=clock,
        sleep=clock.sleep,
        run_command=sim,
        folder=world.folder,
        logs_dir=world.logs,
        state_dir=world.state,
        say=lines.append,
    )
    return rc, sim, clock, lines


def run_recover(
    world, now, rows=None, *, sim=None, login=None, seed=True, today=RECOVER_TODAY, **kw
):
    clock = Clock(now)
    sim = sim or Sim(world, clock)
    sim.clock = clock
    if seed:
        world.seed(rows, **kw)
    sent = []
    lines = []
    rc = recovery.recover_main(
        today,
        clock=clock,
        sleep=clock.sleep,
        run_command=sim,
        login_ready=login or (lambda: True),
        folder=world.folder,
        logs_dir=world.logs,
        snapshot_dir=str(world.snaps),
        deliver=lambda text, **k: sent.append((text, k)) or True,
        say=lines.append,
    )
    return rc, sim, clock, sent, lines


def steps_run(sim):
    return [s for s, _ in sim.calls]


RETRY_START = at(DAY, "16:01")


# ---- (A) afternoon retry: selection, order, rules ---------------------------------------------


def test_retry_runs_only_open_steps_in_dependency_order_and_leaves_good_ones_alone(world):
    rows = {"backfill": 1, "value_areas": 1, "review": 1, "logs": 1, "notify": 0}
    world.seed(rows, review=False)  # logs and value_areas exit 1 with a RED-less report: failures
    before = world.status_text()
    rc, sim, _, _ = run_retry(world, RETRY_START, seed=False)
    # eod, execution_health, jobs, track and notify were good; logs and value_areas wrote a
    # non-RED report with exit 1 (a failure), the review has no verdict.
    assert steps_run(sim) == ["backfill", "logs", "value_areas", "review", "notify"]
    assert rc == 0
    after = world.status_text()
    assert after.startswith(before)  # the first attempt is never rewritten
    appended = after[len(before) :].splitlines()
    assert [r.split("\t")[0] for r in appended] == [
        "backfill",
        "logs",
        "value_areas",
        "review",
        "notify",
    ]
    assert all(r.endswith("\t0") for r in appended)


def test_a_successful_day_retries_nothing_and_writes_nothing(world):
    rc, sim, _, lines = run_retry(world, RETRY_START)
    assert rc == 0 and sim.calls == []
    assert any("nothing to retry" in ln for ln in lines)
    assert not Path(world.folder, f"close-day-retry-{DAY}-run.json").exists()


def test_a_report_step_that_exited_1_with_a_red_report_is_a_result_not_a_failure(world):
    world.seed({"logs": 1})
    write_phase("logs", DAY, [Check("x", "FAIL", "e")], "s", folder=world.folder)
    now = datetime.now().timestamp() + 10
    os.utime(Path(world.folder, f"logs-{DAY}.json"), (now, now))
    assert recovery.outstanding(DAY, world.folder)["steps"] == {}


def test_outstanding_names_the_reason_for_each_step(world):
    world.seed({"backfill": 124, "eod": 2, "track": None, "jobs": recovery.EXIT_NO_OUTPUT})
    got = recovery.outstanding(DAY, world.folder)
    assert got["steps"] == {
        "backfill": "timed_out",
        "eod": "failed",
        "jobs": "no_output",
        "track": "never_started",
    }


def test_a_run_that_is_still_going_is_not_retried(world, monkeypatch):
    world.seed({"backfill": 1})
    model.manifest_start("close-day", DAY, pid=os.getpid(), folder=world.folder)  # running, alive
    assert recovery.outstanding(DAY, world.folder)["state"] == "running"
    rc, sim, _, lines = run_retry(world, RETRY_START, seed=False)
    assert sim.calls == [] and any("still going" in ln for ln in lines)


def test_a_day_with_no_record_is_left_to_the_deadman(world):
    rc, sim, _, lines = run_retry(world, RETRY_START, seed=False)
    assert sim.calls == [] and any("no close-day record" in ln for ln in lines)


def test_steps_with_a_failed_prerequisite_are_recorded_as_blocked_and_never_run(world):
    world.seed({"backfill": 1, "value_areas": 1, "eod": 2, "review": 1}, review=False)
    sim = Sim(world, Clock(RETRY_START))
    sim.mode = {"backfill": "fail", "eod": "fail"}
    rc, sim, _, _ = run_retry(world, RETRY_START, sim=sim, seed=False)
    ran = steps_run(sim)
    assert "value_areas" not in ran and "review" not in ran  # blocked by backfill and eod
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    assert rows["value_areas"]["result"] == "blocked" and rows["review"]["result"] == "blocked"
    assert rows["backfill"]["result"] == "failed" and rows["backfill"]["exit"] == 1
    assert world.manifest("close-day-retry")["state"] == "failed"
    assert rc == 1
    # a blocked step did not run, so it has no new status row
    assert "value_areas" not in world.status_text().split("15:36:09\t1\n")[-1]


def test_the_retry_manifest_and_the_first_manifest_get_new_rows_with_result_and_attempt(world):
    world.seed({"track": 1})
    first = world.manifest("close-day")
    rc, sim, _, _ = run_retry(world, RETRY_START, seed=False)
    after = world.manifest("close-day")
    assert after["steps"][: len(first["steps"])] == first["steps"]
    new = after["steps"][len(first["steps"]) :]
    assert [r["step"] for r in new] == ["track", "notify"]
    assert all(
        r["result"] == "recovered" and r["attempt"].startswith("close-day-retry-") for r in new
    )
    mine = world.manifest("close-day-retry")
    assert mine["state"] == "completed" and mine["ended_at"]


# ---- the deadline, the wall clock and output checks -------------------------------------------


def test_the_deadline_is_1625_and_never_extended():
    assert recovery.RETRY_DEADLINE == "16:25"
    assert recovery.RECOVER_HARD_STOP == "08:55"
    assert recovery.RETRY_REVIEW_LATEST_START == "16:20"


def test_a_retry_started_after_the_deadline_does_not_run(world):
    rc, sim, _, lines = run_retry(world, at(DAY, "16:26"), {"backfill": 1})
    assert sim.calls == [] and any("16:25" in ln for ln in lines)


def test_unresolved_steps_at_the_deadline_are_recorded_in_manifest_and_status(world):
    world.seed({"backfill": 1, "track": 1, "review": 1}, review=False)
    # 16:15:30 is the latest start of a download: it needs 450 s before the 16:23 step cutoff.
    clock = Clock(at(DAY, "16:15", 30))
    sim = Sim(world, clock)
    sim.takes = {"backfill": 600}  # the clock passes the deadline (16:25) inside the backfill
    rc, sim, clock, _ = run_retry(world, at(DAY, "16:15", 30), sim=sim, seed=False)
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    assert rows["backfill"]["result"] == "timed_out" and rows["backfill"]["exit"] == 124
    assert rows["track"]["result"] == "incomplete" and rows["review"]["result"] == "incomplete"
    assert rows["notify"]["result"] == "incomplete"
    final = world.manifest("close-day-retry")
    assert final["state"] == "timed_out" and "unresolved" in final["note"]
    latest = verdict.latest_steps("close-day", DAY, world.folder)
    assert latest["backfill"] == latest["track"] == latest["review"] == 124
    assert rc == 1
    assert steps_run(sim) == ["backfill"]  # nothing started after the deadline
    assert clock() == at(DAY, "16:25", 30)  # the fake clock only moved inside the backfill


def test_the_last_notification_may_use_the_two_minutes_before_the_deadline(world):
    world.seed({"backfill": 1, "track": 1}, state="failed")
    sim = Sim(world, Clock(at(DAY, "16:15", 30)))
    sim.takes = {"backfill": 470}  # ends 16:23:20: past the step cutoff, before the deadline
    run_retry(world, at(DAY, "16:15", 30), sim=sim, seed=False)
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    assert rows["backfill"]["result"] == "timed_out" and rows["track"]["result"] == "incomplete"
    assert rows["notify"]["result"] == "recovered"  # the summary still goes out


def test_a_host_sleep_cannot_extend_the_run_the_real_runner_checks_the_wall_clock(tmp_path):
    clock = Clock(at(DAY, "16:10"))
    ctx = recovery.Ctx(
        mode="retry", day=DAY, phase="x", deadline=at(DAY, "16:25"),
        step_cutoff=at(DAY, "16:23"), review_latest_start=at(DAY, "16:20"),
        folder=str(tmp_path), logs_dir=str(tmp_path), clock=clock, sleep=lambda s: None,
    )  # fmt: skip
    ticks = iter([0, 0, 1])

    def jumpy_clock():
        if next(ticks, 1):
            clock.now = at(DAY, "16:40")  # the machine slept for 30 minutes
        return clock.now

    ctx.clock = jumpy_clock
    run = recovery.make_runner(ctx)
    started = datetime.now()
    rc, cut = run(["sleep", "60"], dict(os.environ), str(tmp_path / "o.log"), 600, at(DAY, "16:23"))
    assert (rc, cut) == (124, True)
    assert (datetime.now() - started).total_seconds() < 20  # the 600 s budget did not matter
    assert ctx.children and ctx.children[0].poll() is not None  # the child is gone


def test_the_real_runner_stops_a_step_at_its_budget_and_kills_only_its_own_group(tmp_path):
    clock = Clock(at(DAY, "16:10"))
    ctx = recovery.Ctx(
        mode="retry", day=DAY, phase="x", deadline=at(DAY, "16:25"),
        step_cutoff=at(DAY, "16:23"), review_latest_start=at(DAY, "16:20"),
        folder=str(tmp_path), logs_dir=str(tmp_path), clock=lambda: clock.now, sleep=lambda s: __import__("time").sleep(0.05),
    )  # fmt: skip
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        rc, cut = recovery.make_runner(ctx)(
            ["sleep", "60"], dict(os.environ), str(tmp_path / "o.log"), 1, at(DAY, "16:23")
        )
        assert (rc, cut) == (124, True)
        assert bystander.poll() is None  # a process this run did not start is untouched
    finally:
        bystander.kill()
        bystander.wait()


def test_a_zero_exit_with_missing_output_is_not_recovered(world):
    world.seed({"backfill": 1, "execution_health": 1, "jobs": 1, "eod": 1})
    sim = Sim(world, Clock(RETRY_START))
    sim.mode = {
        "backfill": "no_verify",
        "execution_health": "empty",
        "jobs": "no_output",
        "eod": "no_output",
    }
    rc, sim, _, _ = run_retry(world, RETRY_START, sim=sim, seed=False)
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    for step in ("backfill", "execution_health", "jobs", "eod"):
        assert rows[step]["result"] == "no_output", step
        assert rows[step]["exit"] == recovery.EXIT_NO_OUTPUT, step
    latest = verdict.latest_steps("close-day", DAY, world.folder)
    assert latest["backfill"] == recovery.EXIT_NO_OUTPUT  # the day stays failed, not healthy
    assert rc == 1
    assert not Path(world.folder, f"execution-health-{DAY}.md").exists()  # no empty report kept


def test_a_review_without_a_verdict_is_not_recovered_and_one_with_a_verdict_is(world):
    world.seed({"review": 1}, review=False)
    sim = Sim(world, Clock(RETRY_START))
    sim.mode = {"review": "no_verdict"}
    run_retry(world, RETRY_START, sim=sim, seed=False)
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    assert rows["review"]["result"] == "no_output"
    assert verdict.review_outcome(DAY, world.folder)["outcome"] != "verdict"


def test_the_review_records_its_outcome_in_the_close_day_manifest(world):
    world.seed({"review": 1}, review=False)
    run_retry(world, RETRY_START, seed=False)
    assert world.manifest("close-day")["review"]["status"] == "verdict"


def test_a_usage_limit_that_ends_after_1620_leaves_the_review_incomplete(world):
    world.seed({"review": 1}, review=False)
    attempts = Path(world.folder, "review-attempts")
    attempts.mkdir()
    (attempts / f"{DAY}-153700.out").write_text("You've hit your 5-hour limit · resets 6:30pm")
    (attempts / f"{DAY}-153700.exit").write_text("1\n")
    (attempts / f"{DAY}-latest.txt").write_text(f"{DAY}-153700\n")
    rc, sim, _, _ = run_retry(world, RETRY_START, seed=False)
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    assert rows["review"]["result"] == "incomplete"
    assert "review" not in steps_run(sim)
    assert recovery.review_reset_time(DAY, world.folder) == at(DAY, "18:31")


def test_a_usage_limit_that_ends_before_1620_makes_the_review_wait(world):
    world.seed({"review": 1}, review=False)
    attempts = Path(world.folder, "review-attempts")
    attempts.mkdir()
    (attempts / f"{DAY}-153700.out").write_text("You've hit your 5-hour limit · resets 4:10pm")
    (attempts / f"{DAY}-latest.txt").write_text(f"{DAY}-153700\n")
    rc, sim, clock, _ = run_retry(world, RETRY_START, seed=False)
    assert "review" in steps_run(sim)
    assert any(s > 0 for s in clock.sleeps)  # it waited for the reset, in bounded slices
    assert rc == 0


def test_retry_sends_the_summary_through_the_notify_command_not_its_own_telegram_call(world):
    world.seed({"track": 1})
    rc, sim, _, _ = run_retry(world, RETRY_START, seed=False)
    notify = [argv for step, argv in sim.calls if step == "notify"]
    assert notify and notify[0][-4:] == ["notify", "close-day", "--day", DAY]


# ---- (B) the final dead-man judges the day after the retry ------------------------------------


def _healthy_close_day(world):
    world.seed({})
    return automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:28"))


def test_final_deadman_a_recovered_step_counts_as_healthy(world):
    world.seed({"track": 1}, state="failed")
    now = at(DAY, "16:28")
    assert automation.judge_phase("close-day", DAY, world.folder, now=now)["state"] == "failed"
    run_retry(world, RETRY_START, seed=False)
    judged = automation.judge_phase("close-day", DAY, world.folder, now=now)
    assert judged["state"] == "healthy", judged


def test_final_deadman_an_unresolved_step_stays_failed(world):
    world.seed({"backfill": 1}, state="failed")
    sim = Sim(world, Clock(RETRY_START))
    sim.mode = {"backfill": "fail"}
    run_retry(world, RETRY_START, sim=sim, seed=False)
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:28"))
    assert judged["state"] == "failed"
    assert any("backfill" in r for r in judged["reasons"])
    assert any("16:01 retry ended as failed" in r for r in judged["reasons"])


def test_final_deadman_a_step_cut_at_the_deadline_is_failed_not_healthy(world):
    world.seed({"backfill": 1}, state="failed")
    sim = Sim(world, Clock(at(DAY, "16:15", 30)))
    sim.takes = {"backfill": 670}
    run_retry(world, at(DAY, "16:15", 30), sim=sim, seed=False)
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:28"))
    assert judged["state"] == "failed"
    assert any("timed out" in r for r in judged["reasons"])


def test_final_deadman_a_retry_still_running_makes_the_day_incomplete(world):
    world.seed({"track": 1}, state="failed")
    model.manifest_start("close-day-retry", DAY, pid=os.getpid(), folder=world.folder)
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:20"))
    assert any("retry is still running" in r for r in judged["reasons"])
    assert judged["state"] in ("incomplete", "failed")
    # on a day that was otherwise healthy, a running retry alone is incomplete
    world.seed({}, state="completed")
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:20"))
    assert judged["state"] == "incomplete"


def test_a_retry_that_was_interrupted_is_incomplete(world):
    world.seed({}, state="completed")
    model.manifest_start("close-day-retry", DAY, pid=1_999_999_999, folder=world.folder)  # dead
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:28"))
    assert judged["state"] == "incomplete"
    assert any("interrupted" in r for r in judged["reasons"])


def test_the_morning_recovery_is_the_later_attempt_and_can_supersede_a_failed_retry(world):
    world.seed({"track": 1}, state="failed")
    model.manifest_start("close-day-retry", DAY, pid=1, folder=world.folder)
    model.manifest_finish("close-day-retry", DAY, "failed", "unresolved: track", world.folder)
    model.manifest_start("close-day-recover", DAY, pid=1, folder=world.folder)
    model.manifest_finish("close-day-recover", DAY, "completed", "", world.folder)
    Path(world.folder, f"close-day-{DAY}-status.tsv").write_text(
        world.status_text() + "track\t08:40:00\t08:40:01\t0\n"
    )
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(RECOVER_TODAY, "09:40"))
    assert judged["state"] == "healthy", judged


def test_the_deadman_close_time_constant_is_1628():
    assert automation.FINAL_DEADMAN_CLOSE == "16:28"


# ---- (C) next-trading-day recovery ------------------------------------------------------------


def _calendar(tmp_path, holidays, name="cal.db"):
    db = tmp_path / name
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE market_holidays (id INTEGER PRIMARY KEY, holiday_date TEXT, description TEXT);"
        "CREATE TABLE market_holiday_exchanges (holiday_id INTEGER, exchange_code TEXT);"
    )
    for i, day in enumerate(holidays, 1):
        con.execute("INSERT INTO market_holidays VALUES (?, ?, 'h')", (i, day))
        con.execute("INSERT INTO market_holiday_exchanges VALUES (?, 'NSE')", (i,))
    con.commit()
    con.close()
    return str(db)


def test_the_previous_trading_day_skips_weekends_and_holidays(tmp_path):
    db = _calendar(tmp_path, ["2026-11-10", "2026-11-24"])
    prev = recovery.previous_trading_day
    assert prev("2026-11-16", db_path=db) == "2026-11-13"  # Monday: Friday
    assert prev("2026-11-11", db_path=db) == "2026-11-09"  # after the Tuesday holiday: Monday
    assert prev("2026-11-12", db_path=db) == "2026-11-11"
    assert prev("2026-11-25", db_path=db) == "2026-11-23"  # holiday 11-24 skipped
    db2 = _calendar(tmp_path, ["2026-11-13", "2026-11-12"], "cal2.db")  # Friday and Thursday closed
    assert prev("2026-11-16", db_path=db2) == "2026-11-11"


def test_recover_retries_only_the_open_steps_of_the_previous_trading_day(world):
    rc, sim, _, sent, _ = run_recover(world, at(RECOVER_TODAY, "08:36"), {"track": 1, "logs": 1})
    # logs wrote a report that is not RED, so exit 1 is a failure and it is retried
    assert steps_run(sim) == ["logs", "track"]
    assert world.manifest("close-day-recover")["state"] == "completed"
    assert rc == 0 and len(sent) == 1


def test_recover_on_a_clean_previous_day_sends_nothing_and_writes_nothing(world):
    rc, sim, _, sent, lines = run_recover(world, at(RECOVER_TODAY, "08:36"))
    assert rc == 0 and sim.calls == [] and sent == []
    assert any("nothing outstanding" in ln for ln in lines)
    assert not Path(world.folder, f"close-day-recover-{DAY}-run.json").exists()


def test_recover_does_not_start_the_whole_workflow_when_there_is_no_record(world):
    rc, sim, _, sent, _ = run_recover(world, at(RECOVER_TODAY, "08:36"), seed=False)
    assert sim.calls == [] and sent == []


def test_recover_does_nothing_on_a_weekend_and_after_0855(world):
    rc, sim, *_ = run_recover(world, at("2026-11-14", "08:36"), {"track": 1}, today="2026-11-14")
    assert sim.calls == []
    rc, sim, *_ = run_recover(world, at(RECOVER_TODAY, "08:56"), {"track": 1})
    assert sim.calls == []


def test_recover_runs_login_free_steps_first_then_waits_for_the_login_for_the_download(world):
    polls = []

    def login():
        polls.append(1)
        return len(polls) >= 4

    rows = {"backfill": 1, "eod": 1, "logs": 2, "value_areas": 1}
    rc, sim, clock, sent, _ = run_recover(world, at(RECOVER_TODAY, "08:36"), rows, login=login)
    ran = steps_run(sim)
    assert ran[:3] == [
        "eod",
        "logs",
        "backfill",
    ]  # local steps at once, the download after the wait
    assert ran[3] == "value_areas" and len(polls) == 4
    assert sum(1 for s in clock.sleeps if s == recovery.LOGIN_POLL_SECONDS) == 3


def test_the_token_wait_stops_at_the_hard_stop_0855(world):
    rows = {"backfill": 1, "value_areas": 1, "track": 1}
    clock_start = at(RECOVER_TODAY, "08:36")
    rc, sim, clock, sent, _ = run_recover(world, clock_start, rows, login=lambda: False)
    assert "backfill" not in steps_run(sim)  # never ran: no fresh login
    assert clock() <= at(RECOVER_TODAY, "08:50")  # the wait ends early enough for the short steps
    assert clock() < at(RECOVER_TODAY, "08:55")  # and never goes past the hard stop
    manifest = world.manifest("close-day-recover")
    results = {r["step"]: r["result"] for r in manifest["steps"]}
    assert results["backfill"] == "incomplete"
    assert results["value_areas"] == "blocked"
    assert results["track"] == "recovered"  # the local steps were not held back
    assert manifest["state"] == "timed_out"
    assert rc == 1
    assert len(sent) == 1 and "partly recovered" in sent[0][0]
    assert "08:55" not in sent[0][0]  # a trader reads words, not clock internals


def test_recover_skips_the_funds_reconciliation_and_says_so_in_the_manifest(world):
    rc, sim, _, _, _ = run_recover(world, at(RECOVER_TODAY, "08:36"), {"eod": 2})
    rows = {r["step"]: r for r in world.manifest("close-day-recover")["steps"]}
    assert rows["eod_reconciliation"]["result"] == "skipped_not_applicable"
    eod_call = [argv for step, argv in sim.calls if step == "eod"][0]
    assert eod_call[-1] == DAY and eod_call[-2].endswith("eod.sh")  # the past day, not today


def test_eod_for_a_past_day_with_a_snapshot_calls_no_broker_and_without_one_is_not_recoverable(
    world,
):
    rc, sim, _, sent, _ = run_recover(
        world, at(RECOVER_TODAY, "08:36"), {"eod": 2, "review": 1}, snapshot=False, review=False
    )
    assert "eod" not in steps_run(sim)  # the eod command (the only one that reads a tradebook)
    rows = {r["step"]: r for r in world.manifest("close-day-recover")["steps"]}
    assert rows["eod"]["result"] == "not_recoverable"
    assert rows["review"]["result"] == "blocked"
    assert "was not saved" in world.manifest("close-day-recover")["note"]
    assert "cannot be rebuilt" in sent[0][0] and "trade report" in sent[0][0]
    assert not list(world.snaps.glob(f"tradebook_{RECOVER_TODAY}*"))  # nothing fetched for today


def test_the_recovery_notice_has_three_outcomes_and_one_message(world):
    _, _, _, sent, _ = run_recover(world, at(RECOVER_TODAY, "08:36"), {"track": 1})
    assert [k["condition"] for _, k in sent] == ["recovered"] and "Nothing is outstanding" in sent[
        0
    ][0]
    w2 = World(Path(world.folder).parent / "w2")
    _, _, _, sent, _ = run_recover(
        w2, at(RECOVER_TODAY, "08:36"), {"track": 1, "backfill": 1}, sim=None, login=lambda: False
    )
    assert [k["condition"] for _, k in sent] == ["partly"]
    w3 = World(Path(world.folder).parent / "w3")
    sim = Sim(w3, Clock(at(RECOVER_TODAY, "08:36")))
    sim.mode = {"track": "fail"}
    _, _, _, sent, _ = run_recover(w3, at(RECOVER_TODAY, "08:36"), {"track": 1}, sim=sim)
    assert [k["condition"] for _, k in sent] == ["unresolved"]
    assert all(k["severity"] == "INFO" and k["kind"] == "recovery" for _, k in sent)


def test_a_notify_step_left_open_is_folded_into_the_recovery_notice(world):
    rc, sim, _, sent, _ = run_recover(world, at(RECOVER_TODAY, "08:36"), {"notify": 1})
    assert sim.calls == [] and len(sent) == 1  # the old day's Telegram line is not sent again
    assert verdict.latest_steps("close-day", DAY, world.folder)["notify"] == 0


def test_recovery_preserves_every_attempt_and_the_original_failure(world):
    world.seed({"track": 1})
    before = world.status_text()
    first = world.manifest("close-day")
    sim = Sim(world, Clock(at(RECOVER_TODAY, "08:36")))
    sim.mode = {"track": "fail"}
    run_recover(world, at(RECOVER_TODAY, "08:36"), sim=sim, seed=False)
    run_recover(world, at(RECOVER_TODAY, "08:40"), seed=False)  # a second attempt succeeds
    text = world.status_text()
    assert text.startswith(before)
    track_rows = [r.split("\t")[3] for r in text.splitlines() if r.startswith("track\t")]
    assert track_rows == ["1", "1", "0"]  # original, the failed retry, the recovery
    assert world.manifest("close-day")["steps"][: len(first["steps"])] == first["steps"]


# ---- (D) the morning dead-man's previous-day line ---------------------------------------------


def test_the_morning_line_reports_nothing_outstanding_recovered_partly_and_unresolved(world):
    now = at(RECOVER_TODAY, "09:40")
    line = recovery.previous_day_line(RECOVER_TODAY, folder=world.folder, now=now)
    assert line["level"] == "WARN" and line["outcome"] == "unresolved"  # no close-day record
    world.seed({})
    line = recovery.previous_day_line(RECOVER_TODAY, folder=world.folder, now=now)
    assert (line["level"], line["outcome"]) == ("OK", "nothing_outstanding")
    world.seed({"track": 1})
    line = recovery.previous_day_line(RECOVER_TODAY, folder=world.folder, now=now)
    assert (line["level"], line["outcome"]) == ("WARN", "not_run")
    assert "the bug tracker update" in line["text"] and "not affected" not in line["text"]
    run_recover(world, at(RECOVER_TODAY, "08:36"), seed=False)
    line = recovery.previous_day_line(RECOVER_TODAY, folder=world.folder, now=now)
    assert (line["level"], line["outcome"]) == ("OK", "recovered")
    world.seed({"track": 1, "backfill": 1})
    sim = Sim(world, Clock(at(RECOVER_TODAY, "08:36")))
    run_recover(world, at(RECOVER_TODAY, "08:36"), sim=sim, seed=False, login=lambda: False)
    line = recovery.previous_day_line(RECOVER_TODAY, folder=world.folder, now=now)
    assert (line["level"], line["outcome"]) == ("WARN", "partly")
    world.seed({"track": 1})
    sim = Sim(world, Clock(at(RECOVER_TODAY, "08:36")))
    sim.mode = {"track": "fail"}
    run_recover(world, at(RECOVER_TODAY, "08:36"), sim=sim, seed=False)
    line = recovery.previous_day_line(RECOVER_TODAY, folder=world.folder, now=now)
    assert (line["level"], line["outcome"]) == ("WARN", "unresolved")


def test_the_morning_deadman_prints_the_line_and_sends_it_at_warn_level_not_red(
    world, monkeypatch, capsys
):
    from signal_engine.ops import __main__ as cli

    world.seed({"track": 1})
    for phase in ("open-day", "liveness"):  # the morning checkpoints themselves are healthy
        Path(world.folder, f"{phase}-{RECOVER_TODAY}-status.tsv").write_text(
            "step\tstarted\tended\texit\ncheck\t09:12:00\t09:12:05\t0\nnotify\t09:12:06\t09:12:07\t0\ntrack\t09:12:07\t09:12:08\t0\n"
        )
        write_phase(phase, RECOVER_TODAY, [Check("a", OK, "x")], "s", folder=world.folder)
        model.manifest_start(phase, RECOVER_TODAY, pid=1, folder=world.folder)
        for step in ("check", "notify"):
            model.manifest_step(phase, RECOVER_TODAY, step, "09:12:00", "09:12:01", 0, world.folder)
        model.manifest_finish(phase, RECOVER_TODAY, "completed", folder=world.folder)
    for mod in (automation, model, verdict, actions):
        monkeypatch.setattr(mod, "REPORTS", world.folder)
    monkeypatch.setattr(actions, "now_ist", lambda: at(RECOVER_TODAY, "09:41"))
    sent = []
    monkeypatch.setattr(alerts, "deliver", lambda text, **kw: sent.append((text, kw)) or True)
    monkeypatch.setattr(alerts, "recover", lambda **kw: True)
    rc = cli.main(["deadman", "morning", "--day", RECOVER_TODAY])
    out = capsys.readouterr().out
    assert rc == 0  # the previous-day line never changes the dead-man's own result
    assert "previous day [WARN] not_run" in out
    assert len(sent) == 1 and sent[0][1]["severity"] == "WARN"
    assert sent[0][1]["phase"] == "morning-previous-day"
    assert sent[0][1]["severity"] != "RED"


# ---- safety: nothing here places an order -----------------------------------------------------

ORDER_WORDS = (
    "placeorder",
    "placesmartorder",
    "basketorder",
    "splitorder",
    "modifyorder",
    "cancelorder",
    "cancelallorder",
    "closeposition",
    "optionsorder",
    "/api/v1",
)
ALLOWED = (
    "backfill_cron.sh",
    "eod.sh",
    "signal_engine.analysis.execution_health",
    "signal_engine.ops",
    "eod_claude_review.sh",
)


def test_retry_and_recover_only_ever_run_the_allowed_read_and_report_commands(world):
    world.seed(dict.fromkeys(ALL_STEPS, 1), review=False)
    _, sim, *_ = run_retry(world, RETRY_START, seed=False)
    w2 = World(Path(world.folder).parent / "w2")
    _, sim2, *_ = run_recover(
        w2, at(RECOVER_TODAY, "08:36"), dict.fromkeys(ALL_STEPS, 1), review=False
    )
    calls = sim.calls + sim2.calls
    assert {s for s, _ in calls} >= {"backfill", "eod", "review", "notify"}
    for _step, argv in calls:
        text = " ".join(argv)
        assert any(a in text for a in ALLOWED), text
        assert not any(w in text.lower() for w in ORDER_WORDS), text


def test_the_module_never_names_an_order_endpoint_or_a_live_trading_database():
    source = Path(recovery.__file__).read_text()
    source = source[source.index("from __future__") :]  # past the module docstring
    code = "\n".join(ln for ln in source.splitlines() if not ln.lstrip().startswith("#")).lower()
    for word in ORDER_WORDS:
        assert word not in code, word
    for db in ("trades.db", "risk.db", "sandbox.db"):
        assert db not in code, db
    assert "requests" not in code and "httpx" not in code and "urllib" not in code


# ---- the login check --------------------------------------------------------------------------


def _auth_db(tmp_path, revoked, name):
    db = tmp_path / name
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE auth (id INTEGER PRIMARY KEY, auth TEXT, is_revoked BOOLEAN)")
    con.execute("INSERT INTO auth VALUES (1, 'x', ?)", (revoked,))
    con.commit()
    con.close()
    return str(db)


def test_a_fresh_login_needs_an_active_token_a_login_line_of_today_and_a_warm_engine(tmp_path):
    day = RECOVER_TODAY
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    active, revoked_db = _auth_db(tmp_path, 0, "a.db"), _auth_db(tmp_path, 1, "r.db")
    ready = lambda **k: recovery.broker_login_ready(  # noqa: E731
        day, db_path=revoked_db if k.get("revoked") else active, log_dir=str(log_dir),
        engine_log=str(tmp_path / "engine.log"), supervisor_log=str(tmp_path / "none.log"),
    )  # fmt: skip
    assert not ready()  # no log line yet
    engine = tmp_path / "engine.log"
    (log_dir / f"openalgo_{day}.log").write_text(
        f"[{day} 08:31:02,100] INFO in openalgoscheduler: Auth token stored for user: u\n"
    )
    assert ready()  # no engine log: no engine, nothing to wait for
    engine.write_text(f"{day} 08:32:00 | INFO | service | engine is starting\n")
    assert not ready()  # the engine is still warming up and reads the market data store
    engine.write_text(
        f"{day} 08:33:00 | INFO | runner | runner warm-up finished: 40 ok, 0 failed\n"
    )
    assert ready()
    assert not ready(revoked=1)  # a revoked token is yesterday's session
    (log_dir / f"openalgo_{day}.log").write_text(
        "[2026-11-12 08:31:02,100] INFO in openalgoscheduler: Auth token stored for user: u\n"
    )
    assert not ready()  # a login line of yesterday does not count
    (log_dir / f"openalgo_{day}.log").write_text(
        f"[{day} 08:31:02,100] INFO in openalgoscheduler: Existing session valid - skipping TOTP\n"
    )
    assert ready()


def test_the_login_check_never_calls_the_network():
    source = Path(recovery.__file__).read_text()
    body = source[source.index("def broker_login_ready") : source.index("def review_reset_time")]
    assert "urlopen" not in body and "fetch_" not in body and "decrypt" not in body.split('"""')[2]


# ---- the clock override -----------------------------------------------------------------------


def test_the_clock_override_applies_only_when_the_variable_is_set(monkeypatch):
    monkeypatch.delenv(recovery.NOW_ENV, raising=False)
    real = recovery._clock()
    assert abs((real - model.now_ist()).total_seconds()) < 5
    monkeypatch.setenv(recovery.NOW_ENV, "2026-11-12T16:24:00")
    assert recovery._clock() == at(DAY, "16:24")
    monkeypatch.setenv(recovery.NOW_ENV, "2026-11-12T16:24:00+05:30")
    assert recovery._clock() == at(DAY, "16:24")


def test_nothing_in_the_scripts_sets_the_clock_override():
    root = Path(recovery.__file__).resolve().parents[1]
    for rel in ("ops/run_upkeep.sh", "ops/run_phase.sh", "analysis/close_day.sh"):
        assert recovery.NOW_ENV not in (root / rel).read_text()


# ---- the unified case of the same test file for the old retry-failed command -------------------


def test_the_installed_retry_failed_command_still_works_unchanged(world, monkeypatch):
    world.seed({"backfill": 1})
    calls = []
    monkeypatch.setattr(actions, "REPORTS", world.folder)
    monkeypatch.setattr(actions, "STATE_DIR", world.state)
    monkeypatch.setattr(automation, "REPORTS", world.folder)
    monkeypatch.setattr(
        actions, "run", lambda action, day, run_id, **kw: calls.append((action, day)) or 0
    )
    lines = automation.retry_failed(now=at(RECOVER_TODAY, "08:56"), folder=world.folder)
    assert calls == [("backfill", DAY)] and "retried" in lines[0]


def test_python_reachable_entry_points_exist():
    from signal_engine.ops import __main__ as cli

    assert callable(recovery.retry_main) and callable(recovery.recover_main)
    assert jobs.REQUIRE_NEW_SCHEDULE is False


# ---- R1: the login evidence ---------------------------------------------------------------------


def _login_world(tmp_path, revoked=0):
    log_dir = tmp_path / "log"
    log_dir.mkdir(exist_ok=True)
    return {
        "db": _auth_db(tmp_path, revoked, f"auth{revoked}.db"),
        "log_dir": log_dir,
        "ctl": tmp_path / "openalgoctl.log",
    }


def _evidence(w, day=RECOVER_TODAY):
    return recovery.broker_login_evidence(
        day, db_path=w["db"], log_dir=str(w["log_dir"]), supervisor_log=str(w["ctl"])
    )


def _line(stamp, marker="Auth token stored for user: u"):
    return f"[{stamp},123] INFO in openalgoscheduler: {marker}\n"


def test_each_login_line_is_judged_by_its_own_date_not_by_the_name_of_its_file(tmp_path):
    w = _login_world(tmp_path)
    # the file named for the day holds only lines of the NEXT day (real: openalgo_2026-11-13.log)
    (w["log_dir"] / f"openalgo_{RECOVER_TODAY}.log").write_text(_line("2026-11-14 08:45:38"))
    assert _evidence(w) == ""
    # a rotated file named for another day holds the line of the day (real: .log.2026-11-13)
    (w["log_dir"] / "openalgo_2026-11-12.log.2026-11-12").write_text(
        _line("2026-11-12 23:50:00") + _line(f"{RECOVER_TODAY} 08:50:17")
    )
    assert _evidence(w) == "08:50"
    assert recovery.login_lines(
        RECOVER_TODAY, [str(w["log_dir"] / "openalgo_2026-11-12.log.2026-11-12")]
    ) == ["08:50"]


def test_a_login_line_before_0305_never_counts(tmp_path):
    w = _login_world(tmp_path)
    log = w["log_dir"] / f"openalgo_{RECOVER_TODAY}.log"
    log.write_text(
        _line(f"{RECOVER_TODAY} 00:05:09", "Existing session valid - skipping TOTP")
        + _line(f"{RECOVER_TODAY} 03:04:59")
    )
    assert _evidence(w) == ""  # before the 03:00 rollover the token can still be yesterday's
    log.write_text(log.read_text() + _line(f"{RECOVER_TODAY} 03:05:00"))
    assert _evidence(w) == "03:05"
    assert recovery.LOGIN_FLOOR == "03:05"


def test_yesterdays_token_that_was_not_revoked_is_not_fresh(tmp_path):
    w = _login_world(tmp_path)  # an active auth row: the 16:31 stop did not revoke it
    (w["log_dir"] / "openalgo_2026-11-12.log").write_text(
        _line("2026-11-12 08:50:15") + _line("2026-11-12 18:41:20", "Existing session valid")
    )
    w["ctl"].write_text(_line("2026-11-12 20:46:00"))
    assert not recovery.broker_login_ready(
        RECOVER_TODAY, db_path=w["db"], log_dir=str(w["log_dir"]), supervisor_log=str(w["ctl"]),
        engine_log=str(tmp_path / "none"),
    )  # fmt: skip


def test_the_supervisor_log_counts_as_evidence_and_a_revoked_token_does_not(tmp_path):
    w = _login_world(tmp_path)
    w["ctl"].write_text(_line(f"{RECOVER_TODAY} 08:31:00", "Existing session valid"))
    assert _evidence(w) == "08:31"
    revoked = _login_world(tmp_path, revoked=1)
    revoked["ctl"] = w["ctl"]
    assert _evidence(revoked) == ""


def test_unclear_evidence_is_not_ready(tmp_path):
    w = _login_world(tmp_path)
    good = _line(f"{RECOVER_TODAY} 08:31:00")
    (w["log_dir"] / f"openalgo_{RECOVER_TODAY}.log").write_text(good)
    assert _evidence(w) == "08:31"
    broken = dict(w, db=str(tmp_path / "missing.db"))
    assert _evidence(broken) == ""  # unreadable auth table
    no_table = tmp_path / "empty.db"
    sqlite3.connect(no_table).close()
    assert _evidence(dict(w, db=str(no_table))) == ""
    assert _evidence(dict(w, log_dir=tmp_path / "no-such-folder", ctl=tmp_path / "x")) == ""
    # a garbled line (no timestamp) and a stale file (older than the floor) do not count
    (w["log_dir"] / f"openalgo_{RECOVER_TODAY}.log").write_text("Auth token stored for user: u\n")
    assert _evidence(w) == ""
    old = w["log_dir"] / "openalgo_old.log"
    old.write_text(good)
    os.utime(old, (1_700_000_000, 1_700_000_000))
    (w["log_dir"] / f"openalgo_{RECOVER_TODAY}.log").unlink()
    assert _evidence(w) == ""


def test_the_login_evidence_reads_files_only(tmp_path):
    source = Path(recovery.__file__).read_text()
    body = source[source.index("def login_lines") : source.index("def review_reset_time")]
    for word in ("urlopen", "httpx", "requests", "fetch_", "Popen", "asyncio"):
        assert word not in body, word
    assert "mode=ro" in body and "decrypt(" not in body


# ---- R2/R5: the words a trader reads ----------------------------------------------------------

RAW_TOKENS = (
    "exited with code", "code 1", "code 124", "limit_or_auth", "no_output", "blocked",
    "timed_out", "not_recoverable", "no_verdict", "usage_limit", "exit 70", "exit 124", " 70", "124",
)  # fmt: skip


def _results_of_every_kind():
    kinds = (
        (recovery.RECOVERED, 0, "the coverage check passed"),
        (recovery.FAILED, 1, "x"),
        (recovery.TIMED_OUT, 124, "the step ran out of time and was stopped"),
        (recovery.INCOMPLETE, 124, "not enough time left"),
        (recovery.BLOCKED, 125, "it needs the market data download first"),
        (recovery.NO_OUTPUT, 70, "the trade report was not written"),
        (recovery.NOT_RECOVERABLE, 125, "the trades were not saved"),
    )
    return {
        kind: [recovery.StepResult(step, kind, code, text) for step in recovery.STEPS]
        for kind, code, text in kinds
    }


def test_the_morning_notice_has_no_status_code_and_no_internal_token(world):
    sets = _results_of_every_kind()
    for kind, results in sets.items():
        outcome, text = recovery._notice(DAY, results)
        for token in RAW_TOKENS:
            assert token not in text, (kind, token, text)
    # a mix: some recovered, some open
    mixed = sets[recovery.RECOVERED][:3] + sets[recovery.FAILED][3:]
    outcome, text = recovery._notice(DAY, mixed)
    assert outcome == "partly" and "Next:" in text and "Daily Workflow page" in text


def test_every_failure_text_of_verify_is_plain_words(world):
    ctx = recovery.Ctx(
        mode="retry", day=DAY, phase="x", deadline=at(DAY, "16:25"),
        step_cutoff=at(DAY, "16:23"), review_latest_start=at(DAY, "16:20"),
        folder=world.folder, logs_dir=world.logs, snapshot_dir=str(world.snaps),
    )  # fmt: skip
    log = Path(world.logs, "x.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("boom\n")
    for step in recovery.STEPS:
        for rc in (1, 2, 70, 124):
            ok, why = recovery.verify(step, rc, ctx, 0.0, str(log))
            for token in RAW_TOKENS:
                assert token not in why, (step, rc, token, why)
            assert ok or why


def test_a_review_that_gave_no_verdict_is_named_by_cause_in_plain_words(world, monkeypatch):
    for cause, words in recovery.REVIEW_CAUSE_WORDS.items():
        monkeypatch.setattr(
            verdict, "review_outcome",
            lambda day, folder=None, _c=cause: {"outcome": "limit_or_auth", "cause": _c},
        )  # fmt: skip
        ctx = recovery.Ctx(
            mode="retry", day=DAY, phase="x", deadline=at(DAY, "16:25"),
            step_cutoff=at(DAY, "16:23"), review_latest_start=at(DAY, "16:20"),
            folder=world.folder, logs_dir=world.logs,
        )  # fmt: skip
        ok, why = recovery.verify("review", 1, ctx, 0.0, "x")
        assert not ok and why == words and "limit_or_auth" not in why


def test_the_morning_notice_says_stale_price_history_matters_and_makes_no_trading_claim():
    steps = [
        recovery.StepResult(
            "backfill", recovery.FAILED, 1, "the market data download stopped with an error"
        )
    ]
    _outcome, text = recovery._notice(DAY, steps)
    assert "Old price history can change what the strategies see" in text
    assert "not affected" not in text and "does not block" not in text
    reports = [
        recovery.StepResult(
            "track", recovery.FAILED, 1, "the bug tracker update stopped with an error"
        )
    ]
    _outcome, text = recovery._notice(DAY, reports)
    assert "price history" not in text and "not affected" not in text


def test_the_previous_day_line_has_no_false_claim_and_names_the_data_risk(world):
    now = at(RECOVER_TODAY, "09:40")
    world.seed({"backfill": 1})
    line = recovery.previous_day_line(RECOVER_TODAY, folder=world.folder, now=now)
    assert line["level"] == "WARN"
    assert "not affected" not in line["text"] and "does not block" not in line["text"]
    assert "old price history" in line["text"] and "Daily Workflow page" in line["text"]
    for token in RAW_TOKENS:
        assert token not in line["text"], token


def test_the_final_manifest_note_is_plain_words(world):
    world.seed({"backfill": 1, "track": 1})
    sim = Sim(world, Clock(at(DAY, "16:15", 30)))
    sim.mode = {"backfill": "fail"}
    run_retry(world, at(DAY, "16:15", 30), sim=sim, seed=False)
    note = world.manifest("close-day-retry")["note"]
    assert "the market data download" in note
    for token in RAW_TOKENS:
        assert token not in note, token


# ---- N-a: a step is not started with less time than it needs ----------------------------------


def test_the_least_useful_time_per_step_is_the_stated_table():
    assert recovery.MIN_USEFUL_SECONDS == {"backfill": 450, "review": 60, "eod": 60}
    assert recovery.DEFAULT_MIN_USEFUL_SECONDS == 20
    assert recovery.LOGIN_WAIT_TAIL_SECONDS == 450


@pytest.mark.parametrize(
    ("step", "minimum"),
    [("backfill", 450), ("review", 60), ("eod", 60), ("track", 20), ("logs", 20)],
)
def test_a_step_with_less_than_its_minimum_is_recorded_incomplete_and_never_started(
    world, step, minimum
):
    cutoff = at(DAY, "16:23")
    world.seed({step: 1}, review=step != "review")
    late = Sim(world, Clock(cutoff - timedelta(seconds=minimum - 1)))
    rc, sim, _, _ = run_retry(world, cutoff - timedelta(seconds=minimum - 1), sim=late, seed=False)
    rows = {r["step"]: r for r in world.manifest("close-day-retry")["steps"]}
    assert rows[step]["result"] == recovery.INCOMPLETE and rows[step]["exit"] == 124, step
    assert step not in steps_run(sim)  # never started
    final = world.manifest("close-day-retry")["note"]
    assert "not enough time left" in final


@pytest.mark.parametrize("step", ["backfill", "review", "eod", "track"])
def test_a_step_with_exactly_its_minimum_starts(world, step):
    minimum = recovery.MIN_USEFUL_SECONDS.get(step, recovery.DEFAULT_MIN_USEFUL_SECONDS)
    world.seed({step: 1}, review=step != "review")
    start = at(DAY, "16:23") - timedelta(seconds=minimum)
    rc, sim, _, _ = run_retry(world, start, sim=Sim(world, Clock(start)), seed=False)
    assert step in steps_run(sim)


def test_the_login_wait_ends_when_a_download_could_no_longer_start(world):
    clock = Clock(at(RECOVER_TODAY, "08:36"))
    sim = Sim(world, clock)
    run_recover(world, at(RECOVER_TODAY, "08:36"), {"backfill": 1}, sim=sim, login=lambda: False)
    rows = {r["step"]: r for r in world.manifest("close-day-recover")["steps"]}
    assert rows["backfill"]["result"] == recovery.INCOMPLETE
    assert "backfill" not in steps_run(sim)
    # the wait stopped 450 s before the 08:53 step cutoff, not later
    assert clock() <= at(RECOVER_TODAY, "08:45", 45)


# ---- N-d: an unreadable holiday table --------------------------------------------------------


def test_recovery_skips_itself_and_says_so_when_the_holiday_table_is_unreadable(world, tmp_path):
    world.seed({"track": 1})
    lines = []
    sent = []
    rc = recovery.recover_main(
        RECOVER_TODAY, clock=Clock(at(RECOVER_TODAY, "08:36")), db_path=str(tmp_path / "missing.db"),
        folder=world.folder, logs_dir=world.logs, run_command=Sim(world, Clock(at(RECOVER_TODAY, "08:36"))),
        deliver=lambda text, **k: sent.append(text) or True, say=lines.append,
    )  # fmt: skip
    assert rc == 1 and sent == []
    assert lines and lines[0].startswith("recovery skipped: holiday table unreadable")


def test_the_morning_dead_man_line_is_a_warn_when_the_holiday_table_is_unreadable(world, tmp_path):
    line = recovery.previous_day_line(
        RECOVER_TODAY, folder=world.folder, now=at(RECOVER_TODAY, "09:40"),
        db_path=str(tmp_path / "missing.db"),
    )  # fmt: skip
    assert line["level"] == "WARN" and line["outcome"] == "calendar_unreadable"
    assert line["text"].startswith("Recovery skipped: the holiday table is unreadable")
    assert "Next:" in line["text"] and "Daily Workflow page" in line["text"]
    ok = recovery.previous_day_line(
        RECOVER_TODAY, folder=world.folder, now=at(RECOVER_TODAY, "09:40")
    )
    assert ok["outcome"] != "calendar_unreadable"  # a readable calendar (the world's) is silent


def test_a_weekend_never_needs_the_holiday_table(world, tmp_path):
    lines = []
    rc = recovery.recover_main(
        "2026-11-14", clock=Clock(at("2026-11-14", "08:36")), db_path=str(tmp_path / "missing.db"),
        folder=world.folder, say=lines.append,
    )  # fmt: skip
    assert rc == 0 and "not a trading day" in lines[0]


# ---- R3: the risk database row ---------------------------------------------------------------


def test_the_eod_smoke_test_row_is_one_idempotent_zero_row_the_engine_never_reads(
    tmp_path, monkeypatch
):
    from signal_engine import smoke_test
    from signal_engine.risk_store import RiskStore
    from signal_engine.timeutils import ist_today

    db = str(tmp_path / "risk.db")
    monkeypatch.setattr(smoke_test, "RISK_DB_PATH", db)
    smoke_test.check_db()
    smoke_test.check_db()  # the 16:01 retry and the 08:36 recovery run it again
    store = RiskStore(db)
    rows = store._conn.execute(
        "SELECT strategy, mode, trades_today, daily_loss FROM risk_counters"
    ).fetchall()
    assert rows == [("SMOKE_TEST", "smoke_test", 0, 0.0)]  # one row, no counters
    today = ist_today()
    for mode in ("live", "analyze"):  # the modes the engine restores its strategies from
        assert store.strategies_for(mode, today) == []
