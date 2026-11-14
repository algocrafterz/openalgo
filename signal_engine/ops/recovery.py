"""The 16:01 afternoon retry and the 08:36 next-trading-day recovery of the close-day steps.

Two entry points share one engine. Both read what the 15:36 close-day run left on disk, pick the
steps whose newest result is not a success, run only those in dependency order, check the real
output of each one, and record every attempt without touching the first one.

    close_day.sh --retry                    16:01, today only, ends by 16:25 IST (wall clock);
                                            the whole process, with the termination of its
                                            children and its last writes, ends by 16:27:00
                                            (model.RETRY_* hold every time of that window)
    run_upkeep.sh recover                   08:36, the previous trading day, ends by 08:55 IST

What a run never does: place or change an order, call an OpenAlgo order endpoint, rerun a step
that succeeded, extend its deadline, or start a step it cannot finish (see MIN_USEFUL_SECONDS).

PRECONDITIONS (a run that lacks one leaves the step open and says so; it never guesses)
  - The Windows task openAlgoAutoStart starts OpenAlgo and the broker login at 08:30, and
    openAlgoAutoStop stops it at 16:31 (signal_engine/scripts/createTaskOpenAlgoScheduler.ps1).
    The 08:36 recovery needs the 08:30 login: with the old 08:50 start no login exists before
    08:50 and every login-gated step ends "incomplete". The 16:01 retry needs OpenAlgo up until
    16:25: with the old 16:00 stop it finds OpenAlgo gone.
  - ROLLOUT ORDER (the reason for each step is in cron_diff.txt of the Phase 1b proposal):
      1. merge the code (the commits);
      2. install the two Windows task changes, on a weekend;
      3. then install the cron lines;
      4. then set jobs.REQUIRE_NEW_SCHEDULE = True and commit.
    Cron before the Windows tasks: the 16:01 retry finds OpenAlgo stopped and appends a failed
    row every day, and the 08:36 recovery finds no login. Flag before cron: the audit reports
    problems for lines that are not installed yet.

HOW "THE BROKER LOGIN OF TODAY IS FRESH" IS DECIDED (broker_login_ready; read-only evidence)
  The 09:12 open-day check asks the OpenAlgo funds API (readiness._broker_and_mode). That call
  goes through OpenAlgo to the broker, and in sandbox mode it answers from the sandbox and proves
  nothing about the broker token, so recovery does not reuse it. It uses only files:
    1. db/openalgo.db table auth, opened with mode=ro, no token column read: a row with
       is_revoked = 0 must exist. The table has no timestamp column, so this alone cannot show
       that the token is today's: yesterday's non-revoked token is still in the table until the
       broker rolls it over at about 03:00.
    2. A login line with its OWN date equal to the day and its own time at or after 03:05, in
       the OpenAlgo log files (log/openalgo_*.log and the rotated log/openalgo_*.log.<date>
       files; the file name does not tell which days a file holds) or in the supervisor log
       signal_engine/logs/openalgoctl.log. The lines are "Auth token stored for user" (a new
       broker login) and "Existing session valid" (openalgoscheduler.py logs it only after
       verify_broker_auth accepted the stored token at that moment). Both prove a token that
       the broker accepted after the 03:00 rollover. A line before 03:05 never counts.
    3. The engine log of the day, when it exists, holds "runner warm-up finished": the engine
       reads the Historify store while it warms up and the download writes it.
  Any doubt (database unreadable, no revoked flag, no qualifying line, files unreadable) gives
  False: the login-gated steps stay open and nothing is fetched with a stale token.

WHAT THE RETRY AND THE RECOVERY WRITE (the true list; nothing else)
  Reports and run records, signal_engine/reports/:
    close-day-<day>-status.tsv (append), close-day-<day>-run.json, close-day-retry-<day>-run.json,
    close-day-recover-<day>-run.json, .ops-state.lock, .close-day-retry|recover-<day>.lock,
    telegram-state.json, eod-<day>.md, execution-health-<day>.md(.part), logs|value-areas|
    jobs-<day>.json, eod-<day>-claude-review.md(.partial), review-attempts/*
  Logs: signal_engine/logs/recovery/*.log, the backfill and eod logs and their locks.
  Data: signal_engine/data/tradebook/tradebook_<today>.json (merge, never shrinks; no fetch for
    a past day), db/historify.duckdb (backfill), signal_engine/data/value_areas.db (INSERT OR
    REPLACE), signal_engine/bugs/bugs.json (track and review apply, under the tracker lock),
    and signal_engine/data/risk.db (one row, see below).
  Never written: db/openalgo.db (opened read-only), sandbox.db, trades.db, any strategy table.
  risk.db: the eod step runs smoke_test.check_db, which upserts ONE row keyed (strategy
    SMOKE_TEST, mode smoke_test, date = the calendar day of the run) with zero counters; a
    repeat changes nothing (ON CONFLICT DO UPDATE with the value it read), so 16:01 and 08:36
    add no row beyond the first of the day. The engine reads only rows of its own trade mode
    (RiskStore.strategies_for(mode, date), risk.py), the 09:12 check ignores rows with zero
    counters, the engine startup runs the same check_db at about 08:5x, and the store opens the
    file in WAL mode with a 10 s wait, so a write at 08:36 does not block the engine. It is
    harmless, so the run does not skip it.

STEP BUDGETS AND THE LEAST TIME A STEP NEEDS TO START (MIN_USEFUL_SECONDS)
  A step that has less time left than its minimum is not started. It is recorded incomplete
  ("not enough time left", status row 124) so a partial result is never written.
    backfill 450 s (normal run 433-529 s), review 60 s, eod 60 s, every other step 20 s.

REPEAT SAFETY OF EACH STEP
  backfill, eod, execution_health, logs, value_areas, jobs, track: safe to repeat (merge, replace
  or already recorded). notify: a second summary is sent on purpose. review: NOT strictly
  idempotent. `bugs apply` appends a history note to a bug on every `note` action, so a review
  that applies its notes and is then retried can add the same note twice. The store has no
  duplicate check and this change does not alter the bug store. A review that was cut off by
  the time limit exits before it applies anything, which is the usual retry case.

Records (all append-only, the 15:36 evidence stays):
  - signal_engine/reports/close-day-<day>-status.tsv   one new row per step that ran or was cut off
  - signal_engine/reports/close-day-<day>-run.json     the same rows, with `result` and `attempt`
  - signal_engine/reports/close-day-retry-<day>-run.json     this run's own manifest (afternoon)
  - signal_engine/reports/close-day-recover-<day>-run.json   this run's own manifest (morning)
The manifests and logs keep the codes (exit codes, result tokens). A message for the trader names
the cause and the next action in plain words and carries no code.

Test hook: the environment variable OPS_RECOVERY_NOW (an ISO date-time) replaces the clock. It is
read only here, only through _clock(), and is unset in production (cron and the scripts never set
it). The functions also take a clock argument, which the tests use.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from signal_engine.ops import actions, alerts, automation, jobs, logs, model, verdict

try:
    import fcntl
except ImportError:  # Windows: the scripts run under WSL, but the module must import anywhere
    fcntl = None

REPO = actions.REPO
REPORTS = model.REPORTS
LOGS = logs.SE_LOGS
SNAPSHOTS = os.path.join(REPO, "signal_engine", "data", "tradebook")
OPENALGO_DB = os.path.join(REPO, "db", "openalgo.db")
OPENALGO_LOG_DIR = os.path.join(REPO, "log")
#: The supervisor log: openalgoscheduler (the AutoStart login) writes its lines here as well.
SUPERVISOR_LOG = os.path.join(LOGS, "openalgoctl.log")

NOW_ENV = model.NOW_ENV
RETRY_PHASE, RECOVER_PHASE = "close-day-retry", "close-day-recover"

#: The afternoon retry ends here. 16:25 and not 16:30: the final dead-man runs at 16:28 and must
#: judge a finished retry, and the 16:30 hard limit then has a margin. The deadline is never
#: extended. The value lives in model.py because close_day.sh reads it from there too.
RETRY_DEADLINE = model.RETRY_DEADLINE
#: Everything of the retry, the termination of its children and its last writes included, ends
#: by this time (16:27:00). The outer timeout of close_day.sh is worked out from model.py too.
RETRY_HARD_END = model.RETRY_HARD_END
#: A retry that starts with less time than this before RETRY_DEADLINE does not start.
RETRY_MIN_START_SECONDS = model.RETRY_MIN_START_SECONDS
STARTED_TOO_LATE = "started too late"
#: No step except the last notification starts or runs past this time (deadline minus two minutes).
RETRY_NOTIFY_RESERVE_SECONDS = 120
#: The review starts only when it can still run: not after this time.
RETRY_REVIEW_LATEST_START = "16:20"
#: The morning recovery stops here. The 09:00 to 09:15 window stays free for trading readiness.
RECOVER_HARD_STOP = "08:55"
RECOVER_NOTICE_RESERVE_SECONDS = 60
RECOVER_REVIEW_RESERVE_SECONDS = 150
#: How often the broker-login wait looks again.
LOGIN_POLL_SECONDS = 15
#: A login line counts only at or after this time (IST): the broker token rolls over at about
#: 03:00, so an earlier line can describe the token of the day before.
LOGIN_FLOOR = "03:05"
LOGIN_MARKERS = ("Auth token stored for user", "Existing session valid")
#: The least time a step needs to start. A step with less time left than this is recorded
#: incomplete ("not enough time left") and is not started, because it could only be cut off.
#: backfill: the normal run takes 433-529 s. Every other step is measured at 2-121 s.
MIN_USEFUL_SECONDS: dict[str, int] = {"backfill": 450, "review": 60, "eod": 60}
DEFAULT_MIN_USEFUL_SECONDS = 20
#: The login wait ends this long before the last moment a step may run: a download that starts
#: later than this is not started (MIN_USEFUL_SECONDS), so waiting longer would only waste time.
LOGIN_WAIT_TAIL_SECONDS = MIN_USEFUL_SECONDS["backfill"]
#: Wait after a TERM before the KILL. Only a child this run started is ever signalled.
TERM_WAIT_SECONDS = 10
#: The same waits when the retry itself receives a TERM (the outer timeout at 16:26:40): 5 s for
#: the TERM and 2 s for the KILL, 7 s in all, inside the 15 s that the outer KILL allows.
SIGNAL_TERM_WAIT_SECONDS = 5
SIGNAL_KILL_WAIT_SECONDS = 2

#: Status exit codes written by this module (besides the real exit code of a step).
EXIT_TIMED_OUT = (
    124  # cut off by the deadline or the step budget (same code the timeout command uses)
)
EXIT_NO_OUTPUT = 70  # the command exited 0 but its output is missing or unreadable (log only)
EXIT_BLOCKED = 125  # a prerequisite step stayed unresolved: recorded in the manifest only

RECOVERED, FAILED, TIMED_OUT, INCOMPLETE = "recovered", "failed", "timed_out", "incomplete"
BLOCKED, NO_OUTPUT = "blocked", "no_output"
NOT_RECOVERABLE, SKIPPED_NA = "not_recoverable", "skipped_not_applicable"

#: Step names in the words a trader uses.
WORDS = {
    "backfill": "the market data download",
    "eod": "the trade report",
    "execution_health": "the order health report",
    "logs": "the error log scan",
    "value_areas": "the value-area levels",
    "jobs": "the scheduled-jobs check",
    "track": "the bug tracker update",
    "review": "the AI review",
    "notify": "the Telegram summary",
}

#: Steps that write the price history and the value-area levels the engine reads at its warm-up.
#: A stale store once made PFC trade on old levels (header of backtest/backfill_cron.sh).
DATA_STEPS = ("backfill", "value_areas")
DATA_WARNING = "Old price history can change what the strategies see."


def data_open(steps) -> bool:
    return any(step in DATA_STEPS for step in steps)


@dataclass(frozen=True)
class StepSpec:
    """One close-day step the retry may run again.

    prereqs: steps of the same run that must be recovered or already good first.
    login: needs a fresh broker session or today-only API state (the morning waits for it).
    retry_budget, recover_budget: seconds, from the measured durations in the Phase 1 schedule
    study (signal_engine/reports/close-day-*-status.tsv, 8 days). Why each number is safe is
    in the comments of STEPS.
    """

    name: str
    retry_budget: int
    recover_budget: int
    prereqs: tuple[str, ...] = ()
    login: bool = False


#: Dependency order of the afternoon retry. Budgets against the measured maximum of 8 days:
#:  backfill 600 s (normal 433-529 s, one 8405 s day was a host sleep), eod 120 s (max 18 s),
#:  execution_health and logs 60 s (max 2 s), value_areas 120 s (max 31 s), jobs 60 s (max 18 s),
#:  track 30 s (max 2 s), review 600 s (max 121 s; recovery gives it 420 s), notify 120 s (max 2 s).
STEPS: dict[str, StepSpec] = {
    s.name: s
    for s in (
        StepSpec("backfill", 600, 600, login=True),
        StepSpec("eod", 120, 120),
        StepSpec("execution_health", 60, 60),
        StepSpec("logs", 60, 60),
        StepSpec("value_areas", 120, 120, prereqs=("backfill",)),
        StepSpec("jobs", 60, 60),
        StepSpec("track", 30, 30),
        StepSpec("review", 600, 420, prereqs=("eod", "execution_health", "logs")),
        StepSpec("notify", 120, 60),
    )
}
RETRY_ORDER = (
    "backfill",
    "eod",
    "execution_health",
    "logs",
    "value_areas",
    "jobs",
    "track",
    "review",
    "notify",
)
#: In the morning the steps that need no login run first, at once; the download waits for the login.
RECOVER_ORDER = (
    "eod",
    "execution_health",
    "logs",
    "backfill",
    "value_areas",
    "jobs",
    "track",
    "review",
    "notify",
)
#: `weekly` is not here: it is Friday-only, not a critical step, and not worth a retry.

_RESET = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)


# ---- clock and helpers ----------------------------------------------------------------------


def _clock() -> datetime:
    """Now in IST. OPS_RECOVERY_NOW replaces it for tests only (unset in production)."""
    return model.clock_now()


def _at(day: str, hhmm: str) -> datetime:
    return model.at_ist(day, hhmm)


def previous_trading_day(today: str, *, db_path: str | None = None) -> str:
    """The latest earlier day that is a weekday and not an NSE holiday (OpenAlgo's own calendar)."""
    current = datetime.strptime(today, "%Y-%m-%d").date()
    for _ in range(21):  # no real gap is longer than a week; the bound stops a broken calendar
        current -= timedelta(days=1)
        if automation.is_trading_day(current.isoformat(), db_path=db_path):
            return current.isoformat()
    return current.isoformat()


def holiday_table_readable(*, db_path: str | None = None) -> bool:
    """True when OpenAlgo's NSE holiday table can be read. Without it the previous trading day
    cannot be named (automation.is_trading_day then answers True for every weekday), so the
    recovery must not guess a day."""
    try:
        con = sqlite3.connect(f"file:{db_path or automation.DB}?mode=ro", uri=True, timeout=5)
        try:
            con.execute("SELECT 1 FROM market_holidays LIMIT 1").fetchall()
            con.execute("SELECT 1 FROM market_holiday_exchanges LIMIT 1").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return True


HOLIDAY_TABLE_TEXT = (
    "Recovery skipped: the holiday table is unreadable, so the previous trading day cannot be "
    "named. Next: check that the OpenAlgo database file db/openalgo.db opens, then open the "
    "Daily Workflow page and run the open steps of the last trading day by hand."
)


# ---- what is outstanding ---------------------------------------------------------------------


def _latest_rows(day: str, folder: str) -> dict[str, tuple[str, str, int]]:
    """step -> (started, ended, exit) of its newest row in the close-day status file."""
    rows = automation._read_rows(os.path.join(folder, f"close-day-{day}-status.tsv")) or []
    out: dict[str, tuple[str, str, int]] = {}
    for cols in rows:
        if len(cols) == 4 and cols[3].lstrip("-").isdigit():
            out[cols[0]] = (cols[1], cols[2], int(cols[3]))
    return out


def _step_good(step: str, day: str, folder: str, row: tuple[str, str, int] | None) -> bool:
    """True when the newest row of a step is a success the retry must leave alone."""
    if row is None:
        return False
    started, _ended, code = row
    if step == "review":  # judged by what it produced, not by its exit code
        return verdict.review_outcome(day, folder)["outcome"] == "verdict"
    if code == 0:
        return True
    if code == 1 and step in model.RED_EXIT_OK_STEPS:
        return jobs.reported_red("close-day", step, started, str(code), day, folder)
    return False


def outstanding(day: str, folder: str | None = None, *, now: datetime | None = None) -> dict:
    """The steps of a close-day run whose newest result is not a success.

    Returns {"state": <why or "ok">, "steps": {step: reason}}. `state` is "running" when the main
    run is still going (nothing may be retried), "no_record" when the day has neither a manifest
    nor a status file, else "ok". A reason is never_started, timed_out, no_output or failed.
    """
    folder = folder or REPORTS
    run = model.read_run("close-day", day, folder, now=now)
    if run["state"] == model.RUNNING:
        return {"state": "running", "steps": {}}
    rows = _latest_rows(day, folder)
    if run["state"] == model.MISSING or (run["state"] == model.LEGACY and not rows):
        return {"state": "no_record", "steps": {}}
    steps: dict[str, str] = {}
    for name in STEPS:
        row = rows.get(name)
        if _step_good(name, day, folder, row):
            continue
        if row is None:
            # --no-review runs end without a review and a notify; that is not an outstanding step.
            if name in ("review", "notify") and run["state"] == model.COMPLETED:
                continue
            steps[name] = "never_started"
        elif row[2] == EXIT_TIMED_OUT:
            steps[name] = "timed_out"
        elif row[2] == EXIT_NO_OUTPUT:
            steps[name] = "no_output"
        else:
            steps[name] = "failed"
    return {"state": "ok", "steps": steps}


# ---- how a step runs -------------------------------------------------------------------------


@dataclass
class Ctx:
    """Everything one run needs. Tests replace the clock, the sleep, the command runner and the
    login check; nothing else about a run is a hook."""

    mode: str  # "retry" or "recover"
    day: str  # the close-day day being worked on
    phase: str
    deadline: datetime  # the run ends here; never extended
    step_cutoff: datetime  # no step except notify may run past this
    review_latest_start: datetime
    folder: str = REPORTS
    logs_dir: str = LOGS
    clock: Callable[[], datetime] = _clock
    sleep: Callable[[float], None] = time.sleep
    #: (argv, env, out_path, budget_seconds, cutoff) -> (exit code, cut off by time)
    run_command: Callable | None = None
    #: () -> bool: the broker session of today is fresh (morning only)
    login_ready: Callable[[], bool] | None = None
    snapshot_dir: str = SNAPSHOTS
    run_id: str = ""
    children: list = field(default_factory=list)


@dataclass
class StepResult:
    step: str
    result: str  # recovered, failed, timed_out, incomplete, blocked, no_output, not_recoverable
    exit: int
    detail: str = ""
    ran: bool = False


def _kill_group(
    proc: subprocess.Popen,
    term_wait: float = TERM_WAIT_SECONDS,
    kill_wait: float = TERM_WAIT_SECONDS,
) -> None:
    """TERM the process group this run created for `proc`, wait a bounded time, then KILL it."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + term_wait
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=kill_wait)


def make_runner(ctx: Ctx) -> Callable:
    """The real command runner: its own process group, a budget, and a wall-clock cutoff.

    The wall clock is read every second. After a host sleep the monotonic budget has not moved, but
    the wall clock has, so a step is stopped when the clock passes the cutoff. Only the group this
    function created is signalled.
    """

    def run(argv, env, out_path, budget, cutoff, stdout_path=None):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        started = time.monotonic()
        with open(out_path, "ab") as log_handle:
            out = open(stdout_path, "wb") if stdout_path else log_handle  # noqa: SIM115
            try:
                proc = subprocess.Popen(  # noqa: S603 - fixed argv built in _command, no shell
                    argv,
                    cwd=REPO,
                    env=env,
                    stdout=out,
                    stderr=log_handle,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                ctx.children.append(proc)
                cut = False
                while proc.poll() is None:
                    if time.monotonic() - started >= budget or ctx.clock() >= cutoff:
                        cut = True
                        _kill_group(proc)
                        break
                    ctx.sleep(1.0)
                code = proc.wait()
            finally:
                if stdout_path:
                    out.close()
        return (EXIT_TIMED_OUT if cut else code), cut

    return run


def _command(step: str, ctx: Ctx, budget: int) -> tuple[list[str], dict, str | None]:
    """(argv, extra env, stdout file or None) of a step. Every command is read-only for orders:
    reports, the market data store and the bug tracker are all they write."""
    day = ctx.day
    env = {"PYTHONPATH": REPO}
    if step == "eod":
        env["EOD_TELEGRAM"] = "0"  # the digest goes into the report, not to Telegram
    if step == "execution_health":
        part = os.path.join(ctx.folder, f"execution-health-{day}.md.part")
        return actions.command(step, day), env, part
    if step == "track":
        return [sys.executable, "-m", "signal_engine.ops", "track", "--day", day], env, None
    if step == "review":
        env["CLOSE_DAY_BUDGET_SECONDS"] = str(max(DEFAULT_MIN_USEFUL_SECONDS, budget - 30))
        script = os.path.join(actions.SE, "analysis", "eod_claude_review.sh")
        return ["bash", script, day], env, None
    if step == "notify":
        argv = [sys.executable, "-m", "signal_engine.ops", "notify", "close-day", "--day", day]
        return argv, env, None
    return actions.command(step, day), env, None


# ---- output checks ---------------------------------------------------------------------------


def _fresh(path: str, since: float) -> bool:
    try:
        return os.path.getmtime(path) >= since - 1
    except OSError:
        return False


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _snapshot_ok(ctx: Ctx) -> bool:
    try:
        with open(
            os.path.join(ctx.snapshot_dir, f"tradebook_{ctx.day}.json"), encoding="utf-8"
        ) as handle:
            return isinstance(json.load(handle), list)
    except (OSError, ValueError):
        return False


#: step -> the report file (name without the day) an ops check writes.
_REPORT_OF = {"logs": "logs", "value_areas": "value-areas", "jobs": "jobs"}


REVIEW_CAUSE_WORDS = {
    "usage_limit": "the Claude usage limit was reached",
    "auth": "the Claude sign-in has expired (run claude auth login on the server)",
    "deadline": "the review ran out of time",
    "exit": "the review stopped with an error",
}


def verify(step: str, rc: int, ctx: Ctx, since: float, log_path: str) -> tuple[bool, str]:
    """Is the output of a step that just ran real? A zero exit with missing output is not.

    Returns (ok, why). `since` is the real start time of the step: a file older than that is a
    leftover from an earlier run and does not count. `why` goes to the trader, so it holds no exit
    code and no internal token; the exit code is kept in the status rows and the step log.
    """
    day, folder = ctx.day, ctx.folder
    if step == "backfill":
        text = _read_text(log_path)
        if rc != 0:
            return False, "the market data download stopped with an error"
        if "--- verify ---" not in text:
            return False, "the market data download stopped before its final check"
        if "extend failed" in text or "store is behind the last expected session" in text:
            return False, "some symbols still have old price history after the download"
        return True, "the coverage check passed"
    if step == "eod":
        report = os.path.join(folder, f"eod-{day}.md")
        if rc != 0:
            return False, "the trade report could not be built"
        if not _fresh(report, since):
            return False, "the trade report was not written"
        if "snapshot FAILED" in _read_text(report):
            return False, "the trade report was built without the trades saved at 15:36"
        if not _snapshot_ok(ctx):
            return False, "the trades saved at 15:36 are missing or unreadable"
        return True, "the trade report and the saved tradebook exist"
    if step == "execution_health":
        report = os.path.join(folder, f"execution-health-{day}.md")
        if rc != 0:
            return False, "the order health report could not be built"
        if not _fresh(report, since) or os.path.getsize(report) == 0:
            return False, "the order health report was not written"
        return True, "the order health report exists"
    if step in _REPORT_OF:
        path = os.path.join(folder, f"{_REPORT_OF[step]}-{day}.json")
        if rc not in (0, 1):
            return False, "the check stopped with an error"
        if not _fresh(path, since) or not automation._json_ok(path):
            return False, "the check wrote no readable report"
        report_verdict = (model.read_phase(_REPORT_OF[step], day, folder=folder) or {}).get(
            "verdict"
        )
        if rc == 1 and report_verdict != "RED":
            return False, "the check stopped before it wrote a result"
        return True, f"the report is readable ({report_verdict})"
    if step == "track":
        if rc != 0:
            return False, "the bug tracker update stopped with an error"
        if f"track {day}:" not in _read_text(log_path):
            return False, "the bug tracker update printed no result"
        return True, "the tracker command reported"
    if step == "review":
        found = verdict.review_outcome(day, folder)
        if found["outcome"] != "verdict":
            return False, REVIEW_CAUSE_WORDS.get(found["cause"], "the review produced no verdict")
        return True, f"the review verdict is {found['verdict']}"
    if step == "notify":
        return (
            rc == 0,
            "the summary was delivered" if rc == 0 else "the summary was not delivered",
        )
    return rc == 0, ""


# ---- broker login and the review gate --------------------------------------------------------

_LOG_STAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2}")


def login_lines(day: str, paths: list[str]) -> list[str]:
    """The HH:MM of every login line of `day` at or after LOGIN_FLOOR, from the given files.

    Each line is judged by its own date and time. A file is read only when it was written after
    the floor of the day (an older file cannot hold such a line). A file that cannot be read is
    skipped: missing evidence is never a login.
    """
    floor = _at(day, LOGIN_FLOOR)
    found: list[str] = []
    for path in paths:
        try:
            if os.path.getmtime(path) < floor.timestamp():
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not any(marker in line for marker in LOGIN_MARKERS):
                        continue
                    stamp = _LOG_STAMP.match(line)
                    if stamp and stamp.group(1) == day and stamp.group(2) >= LOGIN_FLOOR:
                        found.append(stamp.group(2))
        except OSError:
            continue
    return sorted(found)


def _login_files(log_dir: str, supervisor_log: str) -> list[str]:
    try:
        names = sorted(os.listdir(log_dir))
    except OSError:
        names = []
    files = [os.path.join(log_dir, n) for n in names if n.startswith("openalgo_") and ".log" in n]
    return [*files, supervisor_log]


def broker_login_evidence(
    day: str,
    *,
    db_path: str = OPENALGO_DB,
    log_dir: str = OPENALGO_LOG_DIR,
    supervisor_log: str = SUPERVISOR_LOG,
) -> str:
    """The HH:MM of the first login line of `day` (at or after 03:05), or "" when there is none.

    Evidence 1 and 2 of the module docstring: an auth row that is not revoked, and a login line
    dated `day` and timed at or after 03:05. Read-only, no broker call, no token decrypted.
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            active = con.execute("SELECT 1 FROM auth WHERE is_revoked = 0 LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    if not active:
        return ""
    times = login_lines(day, _login_files(log_dir, supervisor_log))
    return times[0] if times else ""


def broker_login_ready(
    day: str,
    *,
    db_path: str = OPENALGO_DB,
    log_dir: str = OPENALGO_LOG_DIR,
    engine_log: str | None = None,
    supervisor_log: str = SUPERVISOR_LOG,
) -> bool:
    """True when the broker login of `day` is proven and the engine finished its warm-up.

    The login evidence is broker_login_evidence (see the module docstring for why the funds API
    is not used). When the engine log of the day exists, it must hold "runner warm-up finished":
    the engine reads the Historify store while it warms up, and the download writes it. Anything
    unclear is False.
    """
    if not broker_login_evidence(
        day, db_path=db_path, log_dir=log_dir, supervisor_log=supervisor_log
    ):
        return False
    engine_path = engine_log or logs.engine_log(day)
    return not (
        os.path.exists(engine_path) and "runner warm-up finished" not in _read_text(engine_path)
    )


def review_reset_time(day: str, folder: str) -> datetime | None:
    """When the Claude usage limit ends, from the newest review attempt, plus one minute. Or None."""
    attempt = verdict.latest_attempt(day, folder)
    if not attempt:
        return None
    text = attempt["out"] + "\n" + attempt["err"]
    if not verdict._USAGE_LIMIT.search(text):
        return None
    match = _RESET.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
    stamp = attempt["id"].split("-")[3:4]  # <day>-<HHMMSS>[-n]
    base = _at(day, "00:00")
    if stamp and len(stamp[0]) == 6 and stamp[0].isdigit():
        base = base.replace(hour=int(stamp[0][:2]), minute=int(stamp[0][2:4]))
    reset = base.replace(hour=hour, minute=int(match.group(2) or 0), second=0)
    if reset < base:
        reset += timedelta(days=1)
    return reset + timedelta(minutes=1)


# ---- the engine ------------------------------------------------------------------------------


def _hms(moment: datetime) -> str:
    return moment.strftime("%H:%M:%S")


def _record(ctx: Ctx, res: StepResult, started: str, ended: str, *, to_status: bool) -> None:
    """One row, appended in the run's own manifest and (when the step ran or was cut) in the
    close-day status file and manifest. Nothing earlier is changed."""
    model.manifest_step(
        ctx.phase, ctx.day, res.step, started, ended, res.exit, ctx.folder,
        result=res.result, attempt=ctx.run_id,
    )  # fmt: skip
    if not to_status:
        return
    with contextlib.suppress(OSError):
        actions.append_step(ctx.day, res.step, started, ended, res.exit, ctx.folder)
    model.manifest_step(
        "close-day", ctx.day, res.step, started, ended, res.exit, ctx.folder,
        result=res.result, attempt=ctx.run_id,
    )  # fmt: skip


def _wait_until(ctx: Ctx, ready: Callable[[], bool], limit: datetime) -> bool:
    """Poll `ready` every LOGIN_POLL_SECONDS until it is true or the wall clock reaches `limit`."""
    while True:
        if ready():
            return True
        now = ctx.clock()
        if now >= limit:
            return False
        ctx.sleep(min(LOGIN_POLL_SECONDS, max(1.0, (limit - now).total_seconds())))


def _finish_review(ctx: Ctx) -> None:
    """What close_day.sh does after the review: finish a cut-off report, store the outcome."""
    report = os.path.join(ctx.folder, f"eod-{ctx.day}-claude-review.md")
    partial = report + ".partial"
    if os.path.exists(partial):
        note = "> Review stopped at the time limit: incomplete, no tracker changes applied.\n"
        body = _read_text(partial)
        with open(report, "w", encoding="utf-8") as handle:
            handle.write(f"{body}\n{note}")
        with contextlib.suppress(OSError):
            os.remove(partial)
    found = verdict.review_outcome(ctx.day, ctx.folder)
    with contextlib.suppress(OSError, TimeoutError, ValueError):
        model.manifest_review(
            "close-day",
            ctx.day,
            found["outcome"],
            found["attempt"] or "",
            found["exit"] or 0,
            ctx.folder,
        )


def _run_step(ctx: Ctx, step: str, cutoff: datetime) -> StepResult:
    spec = STEPS[step]
    budget_cap = spec.retry_budget if ctx.mode == "retry" else spec.recover_budget
    left = (cutoff - ctx.clock()).total_seconds()
    budget = int(min(budget_cap, left))
    if budget < MIN_USEFUL_SECONDS.get(step, DEFAULT_MIN_USEFUL_SECONDS):
        return StepResult(step, INCOMPLETE, EXIT_TIMED_OUT, "not enough time left")
    argv, extra_env, stdout_path = _command(step, ctx, budget)
    log_path = os.path.join(
        ctx.logs_dir, "recovery", f"{ctx.phase}-{ctx.day}-{step}-{ctx.run_id}.log"
    )
    env = {**os.environ, **extra_env}
    run_command = ctx.run_command or make_runner(ctx)
    since = time.time()
    if stdout_path:
        rc, cut = run_command(argv, env, log_path, budget, cutoff, stdout_path=stdout_path)
        with contextlib.suppress(OSError):
            if rc == 0 and not cut and os.path.getsize(stdout_path) > 0:
                os.replace(stdout_path, os.path.join(ctx.folder, f"execution-health-{ctx.day}.md"))
            else:
                os.remove(stdout_path)  # a partial report never replaces a good one
    else:
        rc, cut = run_command(argv, env, log_path, budget, cutoff)
    if step == "review":
        _finish_review(ctx)
    if cut or ctx.clock() > cutoff:
        return StepResult(
            step, TIMED_OUT, EXIT_TIMED_OUT, "the step ran out of time and was stopped", ran=True
        )
    ok, why = verify(step, rc, ctx, since, log_path)
    if ok:
        return StepResult(step, RECOVERED, 0, why, ran=True)
    if rc in (0, 1) and step in _REPORT_OF or rc == 0:
        return StepResult(step, NO_OUTPUT, EXIT_NO_OUTPUT, why, ran=True)
    return StepResult(step, FAILED, rc, why, ran=True)


def _review_gate(ctx: Ctx, cutoff: datetime) -> StepResult | None:
    """None when the review may start now. Otherwise the result that says why it did not.

    A usage limit names its reset time in the failed attempt. The review waits for that time plus
    one minute when it falls before the latest start, else it is left for the next recovery.
    """
    reset = review_reset_time(ctx.day, ctx.folder)
    if reset is None or reset <= ctx.clock():
        return None
    if reset > ctx.review_latest_start:
        return StepResult(
            "review",
            INCOMPLETE,
            EXIT_TIMED_OUT,
            f"the Claude usage limit ends at {reset.strftime('%H:%M')}, too late for this run",
        )
    if not _wait_until(ctx, lambda: ctx.clock() >= reset, ctx.review_latest_start):
        return StepResult(
            "review", INCOMPLETE, EXIT_TIMED_OUT, "the Claude usage limit did not end in time"
        )
    return None


def _past_deadline(ctx: Ctx, step: str) -> bool:
    cutoff = ctx.deadline if step == "notify" else ctx.step_cutoff
    return ctx.clock() >= cutoff


def execute(ctx: Ctx, todo: list[str]) -> list[StepResult]:
    """Run the steps in `todo` (already in order). Returns one result per step.

    Rules: a step whose prerequisite stayed unresolved is recorded as blocked and does not run; a
    step that cannot finish before the deadline is recorded as incomplete (status row 124); a step
    that is cut off is recorded as timed_out (status row 124); a step is recovered only when its
    output check passes.
    """
    results: dict[str, StepResult] = {}
    for step in todo:
        spec = STEPS[step]
        at = ctx.clock()
        cutoff = ctx.deadline if step == "notify" else ctx.step_cutoff
        started = _hms(at)
        unresolved = [p for p in spec.prereqs if p in results and results[p].result != RECOVERED]
        if unresolved:
            res = StepResult(
                step,
                BLOCKED,
                EXIT_BLOCKED,
                f"it needs {', '.join(WORDS[p] for p in unresolved)} first",
            )
            _record(ctx, res, started, started, to_status=False)
            results[step] = res
            continue
        if _past_deadline(ctx, step):
            res = StepResult(step, INCOMPLETE, EXIT_TIMED_OUT, "the time for this run was over")
            _record(ctx, res, started, started, to_status=True)
            results[step] = res
            continue
        if ctx.mode == "recover" and step == "eod" and not _snapshot_ok(ctx):
            res = StepResult(
                step,
                NOT_RECOVERABLE,
                EXIT_BLOCKED,
                f"the broker tradebook of {ctx.day} was not saved at 15:36 and the broker "
                "returns only today's fills, so the trade report of that day cannot be rebuilt",
            )
            _record(ctx, res, started, started, to_status=False)
            results[step] = res
            continue
        if ctx.mode == "recover" and spec.login and ctx.login_ready is not None:
            wait_limit = ctx.step_cutoff - timedelta(seconds=LOGIN_WAIT_TAIL_SECONDS)
            if not _wait_until(ctx, ctx.login_ready, wait_limit):
                res = StepResult(
                    step,
                    INCOMPLETE,
                    EXIT_TIMED_OUT,
                    "the broker login of today was not found in time",
                )
                _record(ctx, res, started, _hms(ctx.clock()), to_status=True)
                results[step] = res
                continue
            started = _hms(ctx.clock())
        if step == "review":
            gate = _review_gate(ctx, cutoff)
            if gate is not None:
                _record(ctx, gate, started, _hms(ctx.clock()), to_status=True)
                results[step] = gate
                continue
        res = _run_step(ctx, step, cutoff)
        _record(ctx, res, started, _hms(ctx.clock()), to_status=True)
        results[step] = res
    return [results[s] for s in todo]


# ---- locks and manifests ---------------------------------------------------------------------


@contextlib.contextmanager
def _run_lock(folder: str, name: str):
    """One run of this kind at a time. Yields False when another holds the lock."""
    os.makedirs(folder, exist_ok=True)
    handle = open(os.path.join(folder, f".{name}.lock"), "a")  # noqa: SIM115
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
        yield True
    finally:
        handle.close()


def _final_state(results: list[StepResult], timed_out: bool) -> tuple[str, str]:
    open_steps = [r for r in results if r.result != RECOVERED]
    if not open_steps:
        return model.COMPLETED, "every outstanding step was recovered"
    note = "; ".join(f"{WORDS[r.step]}: {r.detail}" for r in open_steps)
    cut = any(r.result in (TIMED_OUT, INCOMPLETE) for r in open_steps)
    return (model.TIMED_OUT if (timed_out or cut) else model.FAILED), f"unresolved: {note}"


def _install_signal_handlers(ctx: Ctx, state: dict) -> None:
    def handler(signum, _frame):
        # The retry has a hard end (RETRY_HARD_END): its outer timeout KILLs 15 s after the TERM.
        # So its children get a short, bounded wait. The morning recovery keeps the long one.
        waits = (
            (SIGNAL_TERM_WAIT_SECONDS, SIGNAL_KILL_WAIT_SECONDS)
            if ctx.mode == "retry"
            else (TERM_WAIT_SECONDS, TERM_WAIT_SECONDS)
        )
        for proc in list(ctx.children):
            if proc.poll() is None:
                _kill_group(proc, *waits)
        if not state.get("finished"):
            model.manifest_finish(
                ctx.phase, ctx.day, model.INTERRUPTED, f"stopped by signal {signum}", ctx.folder
            )
            state["finished"] = True
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _begin(ctx: Ctx) -> None:
    run = model.manifest_start(ctx.phase, ctx.day, pid=os.getpid(), folder=ctx.folder)
    ctx.run_id = run["run_id"]


def run_plan(ctx: Ctx, todo: list[str], *, say: Callable[[str], None] = print) -> list[StepResult]:
    """Start the run manifest, execute, finish the manifest in a terminal state."""
    _begin(ctx)
    state: dict = {}
    if ctx.run_command is None and threading_main():
        _install_signal_handlers(ctx, state)
    try:
        results = execute(ctx, todo)
    except BaseException:
        if not state.get("finished"):
            model.manifest_finish(
                ctx.phase, ctx.day, model.INTERRUPTED, "the run ended early", ctx.folder
            )
            state["finished"] = True
        raise
    timed_out = ctx.clock() >= ctx.deadline
    final, note = _final_state(results, timed_out)
    model.manifest_finish(ctx.phase, ctx.day, final, note, ctx.folder)
    state["finished"] = True
    for r in results:
        say(f"  {r.step}: {r.result} ({r.detail})")
    return results


def _not_started(
    ctx: Ctx, todo: list[str], now: datetime, say: Callable[[str], None]
) -> list[StepResult]:
    """A retry that begins with less than RETRY_MIN_START_SECONDS before RETRY_DEADLINE (or after
    it) runs no step. It records every open step as not done, in its own manifest and in the
    close-day status file, so the 16:28 dead-man reads a clear answer and not a missing record."""
    say(
        f"It is {now.strftime('%H:%M:%S')}, less than {RETRY_MIN_START_SECONDS} seconds before "
        f"{RETRY_DEADLINE} - {STARTED_TOO_LATE}, no step was run. {ACTION}"
    )
    _begin(ctx)
    stamp = _hms(now)
    results = []
    for step in todo:
        res = StepResult(step, INCOMPLETE, EXIT_TIMED_OUT, STARTED_TOO_LATE)
        _record(ctx, res, stamp, stamp, to_status=True)
        results.append(res)
    note = (
        f"{STARTED_TOO_LATE}: the retry began at {now.strftime('%H:%M:%S')}, with less than "
        f"{RETRY_MIN_START_SECONDS} seconds before {RETRY_DEADLINE}. No step was run. {ACTION}"
    )
    model.manifest_finish(ctx.phase, ctx.day, model.TIMED_OUT, note, ctx.folder)
    return results


def threading_main() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()


# ---- afternoon retry -------------------------------------------------------------------------


def retry_main(
    day: str | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    run_command: Callable | None = None,
    folder: str | None = None,
    logs_dir: str | None = None,
    state_dir: str | None = None,
    say: Callable[[str], None] = print,
) -> int:
    """close_day.sh --retry. Exit 0 when nothing was outstanding or everything was recovered, 1
    when a step stays open. Runs for today only."""
    clock = clock or _clock
    folder = folder or REPORTS
    now = clock()
    today = now.strftime("%Y-%m-%d")
    day = day or today
    if day != today:
        say(f"The retry works on today only. For {day} use: run_upkeep.sh recover.")
        return 0
    if not automation.is_trading_day(day):
        say(f"{day} is not a trading day - nothing to retry.")
        return 0
    deadline = _at(day, RETRY_DEADLINE)
    found = outstanding(day, folder, now=now)
    if found["state"] == "running":
        say(f"{day}: the close-day run is still going - not retrying.")
        return 0
    if found["state"] == "no_record":
        say(f"{day}: no close-day record - nothing to retry (the dead-man reports it).")
        return 0
    todo = [s for s in RETRY_ORDER if s in found["steps"]]
    if not todo:
        say(f"{day}: every close-day step succeeded - nothing to retry.")
        return 0
    if actions.running(state_dir):
        say("Another run is in progress - not retrying now.")
        return 0
    if "notify" not in todo:
        todo.append("notify")  # the corrected summary goes out after any retry
    ctx = Ctx(
        mode="retry",
        day=day,
        phase=RETRY_PHASE,
        deadline=deadline,
        step_cutoff=deadline - timedelta(seconds=RETRY_NOTIFY_RESERVE_SECONDS),
        review_latest_start=_at(day, RETRY_REVIEW_LATEST_START),
        folder=folder,
        logs_dir=logs_dir or LOGS,
        clock=clock,
        sleep=sleep,
        run_command=run_command,
    )
    say(f"=== close-day retry {day} {now.strftime('%H:%M:%S')}: {', '.join(todo)} ===")
    with _run_lock(folder, f"{RETRY_PHASE}-{day}") as free:
        if not free:
            say("Another retry holds the lock - not starting.")
            return 0
        if (deadline - now).total_seconds() < RETRY_MIN_START_SECONDS:
            results = _not_started(ctx, todo, now, say)
        else:
            results = run_plan(ctx, todo, say=say)
    wanted = set(found["steps"])
    return 0 if all(r.result == RECOVERED for r in results if r.step in wanted) else 1


# ---- morning recovery ------------------------------------------------------------------------


ACTION = "Next: open the Daily Workflow page and run the open steps by hand."


def _notice(day: str, results: list[StepResult]) -> tuple[str, str]:
    """(outcome, text) of the one morning notice. Words for a trader, no step codes."""
    good = [WORDS[r.step] for r in results if r.result == RECOVERED]
    left = [r for r in results if r.result != RECOVERED]
    if not left:
        return "recovered", (
            f"Close-day {day}: recovered this morning: {', '.join(good)}. Nothing is outstanding."
        )
    why = "; ".join(f"{WORDS[r.step]} ({r.detail})" for r in left)
    warning = f" {DATA_WARNING}" if data_open(r.step for r in left) else ""
    if good:
        return "partly", (
            f"Close-day {day}: partly recovered. Done: {', '.join(good)}. Still open: {why}."
            f"{warning} {ACTION}"
        )
    return "unresolved", f"Close-day {day}: could not recover: {why}.{warning} {ACTION}"


def recover_main(
    today: str | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    run_command: Callable | None = None,
    login_ready: Callable[[], bool] | None = None,
    folder: str | None = None,
    logs_dir: str | None = None,
    snapshot_dir: str | None = None,
    db_path: str | None = None,
    deliver: Callable | None = None,
    say: Callable[[str], None] = print,
) -> int:
    """run_upkeep.sh recover (08:36). Retries only the outstanding steps of the previous trading
    day. Sends one notice, and only when something was outstanding. Exit 0 when nothing was open
    or all was recovered, 1 otherwise."""
    clock = clock or _clock
    folder = folder or REPORTS
    now = clock()
    today = today or now.strftime("%Y-%m-%d")
    if datetime.strptime(today, "%Y-%m-%d").weekday() <= 4 and not holiday_table_readable(
        db_path=db_path
    ):
        say("recovery skipped: holiday table unreadable. " + HOLIDAY_TABLE_TEXT)
        return 1
    if not automation.is_trading_day(today, db_path=db_path):
        say(f"{today} is not a trading day - nothing to recover.")
        return 0
    stop = _at(today, RECOVER_HARD_STOP)
    if now >= stop:
        say(f"It is {now.strftime('%H:%M')}, after {RECOVER_HARD_STOP} - not recovering.")
        return 0
    day = previous_trading_day(today, db_path=db_path)
    found = outstanding(day, folder, now=now)
    if found["state"] != "ok":
        say(f"{day}: no usable close-day record ({found['state']}) - nothing to recover.")
        return 0
    todo = [s for s in RECOVER_ORDER if s in found["steps"]]
    if not todo:
        say(f"{day}: nothing outstanding.")
        return 0
    if login_ready is None:
        login_ready = lambda: broker_login_ready(today)  # noqa: E731
    ctx = Ctx(
        mode="recover",
        day=day,
        phase=RECOVER_PHASE,
        deadline=stop,
        step_cutoff=stop - timedelta(seconds=RECOVER_NOTICE_RESERVE_SECONDS),
        review_latest_start=stop - timedelta(seconds=RECOVER_REVIEW_RESERVE_SECONDS),
        folder=folder,
        logs_dir=logs_dir or LOGS,
        clock=clock,
        sleep=sleep,
        run_command=run_command,
        login_ready=login_ready,
        snapshot_dir=snapshot_dir or SNAPSHOTS,
    )
    # The summary of the old day is replaced by the one notice below, so notify is not run.
    notify_open = "notify" in todo
    todo = [s for s in todo if s != "notify"]
    say(
        f"=== close-day recover {day} (today {today}) {now.strftime('%H:%M:%S')}: {', '.join(todo)} ==="
    )
    with _run_lock(folder, f"{RECOVER_PHASE}-{day}") as free:
        if not free:
            say("Another recovery holds the lock - not starting.")
            return 0
        results = run_plan(ctx, todo, say=say)
        # The reconciliation inside the trade report answers for today's funds only.
        if any(r.step == "eod" and r.ran for r in results):
            at = _hms(clock())
            model.manifest_step(
                RECOVER_PHASE, day, "eod_reconciliation", at, at, 0, folder,
                result=SKIPPED_NA, attempt=ctx.run_id,
            )  # fmt: skip
        outcome, text = _notice(day, results)
        send = deliver or alerts.deliver
        sent = False
        if clock() < stop:
            sent = bool(
                send(text, kind="recovery", phase="close-day", condition=outcome, severity="INFO")
            )
        say(f"recovery {outcome}; notice {'sent' if sent else 'NOT sent'}: {text}")
        if notify_open and sent:
            at = _hms(clock())
            _record(
                ctx,
                StepResult("notify", RECOVERED, 0, "the recovery notice was delivered", ran=True),
                at,
                at,
                to_status=True,
            )
    return 0 if outcome == "recovered" else 1


# ---- the morning dead-man's previous-day line ------------------------------------------------


def previous_day_line(
    today: str,
    *,
    folder: str | None = None,
    now: datetime | None = None,
    db_path: str | None = None,
) -> dict:
    """The WARN-level line the 09:40 dead-man adds: what the previous trading day left open and
    what the 08:36 recovery did. {level: "OK" or "WARN", outcome, text}. Reads files only.

    outcome is one of: nothing_outstanding, recovered, partly, unresolved, not_run, running,
    calendar_unreadable.
    """
    folder = folder or REPORTS
    if not holiday_table_readable(db_path=db_path):
        return {"level": "WARN", "outcome": "calendar_unreadable", "text": HOLIDAY_TABLE_TEXT}
    day = previous_trading_day(today, db_path=db_path)
    found = outstanding(day, folder, now=now)
    if found["state"] == "no_record":
        return {
            "level": "WARN",
            "outcome": "unresolved",
            "text": f"Close-day {day} left no record. Next: check the machine and run /close-day by hand.",
        }
    if found["state"] == "running":
        return {"level": "WARN", "outcome": "running", "text": f"Close-day {day} is still running."}
    run = model.read_run(RECOVER_PHASE, day, folder, now=now)
    rows = [r for r in run.get("steps", []) if r.get("result") == RECOVERED]
    left = found["steps"]
    if not left:
        if run["state"] in (model.MISSING, model.LEGACY):
            return {
                "level": "OK",
                "outcome": "nothing_outstanding",
                "text": f"Close-day {day}: nothing outstanding.",
            }
        return {
            "level": "OK",
            "outcome": "recovered",
            "text": f"Close-day {day}: recovered by the morning recovery.",
        }
    names = ", ".join(WORDS[s] for s in left)
    if run["state"] == model.RUNNING:
        outcome, word = "running", "the recovery is still running"
    elif run["state"] in (model.MISSING, model.LEGACY):
        outcome, word = "not_run", "the morning recovery did not run"
    elif rows:
        outcome, word = "partly", "the morning recovery fixed part of it"
    else:
        outcome, word = "unresolved", "the morning recovery could not fix it"
    return {
        "level": "WARN",
        "outcome": outcome,
        "text": (
            f"Close-day {day} still has open work: {names}; {word}."
            f"{' The strategies may have started with old price history.' if data_open(left) else ''}"
            f" {ACTION}"
        ),
    }
