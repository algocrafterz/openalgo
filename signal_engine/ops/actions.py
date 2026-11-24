"""Run-again actions for the Daily Workflow page.

A fixed list of commands the page may start. Each one re-runs a close-day step that failed or
went stale; none changes trading behaviour, orders, positions, config or code, and none restarts
anything. The page sends only an action key and a day: the command lines live here and nothing
from the browser reaches a shell.

    uv run python -m signal_engine.ops run-action ACTION --day D [--id RUN]

The step's new result is appended to the day's status file, so the page shows the latest run of
each step (and that it was run again). The run's own state is a small JSON file per run in
signal_engine/logs/ops_actions/, which is how the page sees progress without a worker thread.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from signal_engine.timeutils import IST

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SE = os.path.join(REPO, "signal_engine")
REPORTS = os.path.join(SE, "reports")
STATE_DIR = os.path.join(SE, "logs", "ops_actions")

DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_ID = re.compile(r"^\d{10}-[a-z_]+$")
#: Close-day steps write their results from this time (the BreakingTrade poller stops at 15:35;
#: the signal engine stops together with OpenAlgo, at the AutoStop task: 16:00, 16:31 after the
#: Windows task change).
CLOSE_FROM = "15:36"
#: A page may re-run a step only for a recent day.
MAX_AGE_DAYS = 7
#: How long after a run's last sign of life a missing end marker means the run died.
STATE_KEEP_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Action:
    key: str
    #: The step name in the close-day status file.
    step: str
    label: str
    detail: str
    timeout: int


ACTIONS: dict[str, Action] = {
    a.key: a
    for a in (
        Action(
            "backfill",
            "backfill",
            "Download the missing market data",
            "Runs the daily data download again. About 7 minutes.",
            1800,
        ),
        Action(
            "eod",
            "eod",
            "Rebuild the trade report",
            "Reads the broker trades again and rebuilds the day's report. About 1 minute.",
            600,
        ),
        Action(
            "execution_health",
            "execution_health",
            "Rebuild the order health report",
            "Re-reads the day's logs for order and broker problems. Seconds.",
            300,
        ),
        Action(
            "logs",
            "logs",
            "Re-scan the error logs",
            "Groups the day's errors again. Seconds.",
            300,
        ),
        Action(
            "value_areas",
            "value_areas",
            "Rebuild the value areas",
            "Needs the day's market data to be complete. Seconds.",
            300,
        ),
        Action(
            "jobs",
            "jobs",
            "Re-check the scheduled jobs",
            "Looks at every scheduled task again. Seconds.",
            300,
        ),
        Action(
            "review",
            "review",
            "Run the AI review again",
            "Needs your Claude sign-in. Up to 25 minutes.",
            1600,
        ),
    )
}


def _python_module(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", module, *args]


def command(action: str, day: str) -> list[str]:
    """The exact command line for an action. Raises KeyError for anything not on the list."""
    if not DAY.match(day):
        raise ValueError("bad day")
    return {
        "backfill": ["bash", os.path.join(SE, "backtest", "backfill_cron.sh")],
        "eod": ["bash", os.path.join(SE, "analysis", "eod.sh"), day],
        "execution_health": _python_module(
            "signal_engine.analysis.execution_health", "--day", day, "--format", "md"
        ),
        "logs": _python_module("signal_engine.ops", "logs", "--day", day),
        "value_areas": _python_module("signal_engine.ops", "value-areas", "--day", day),
        "jobs": _python_module("signal_engine.ops", "jobs", "--day", day),
        "review": ["bash", os.path.join(SE, "analysis", "review_catchup.sh"), day],
    }[action]


def now_ist() -> datetime:
    return datetime.now(IST)


def refusal(action: str, day: str, *, now: datetime | None = None, folder: str | None = None):
    """Why an action cannot start for this day right now, in words for a trader, or None."""
    now = now or now_ist()
    folder = folder or REPORTS
    if action not in ACTIONS:
        return "That action does not exist."
    if not DAY.match(day or ""):
        return "Pick a valid day."
    if not os.path.exists(os.path.join(folder, f"close-day-{day}-status.tsv")):
        return "The close-day checks did not start on this day, so there is nothing to run again."
    try:
        age = (now.date() - datetime.strptime(day, "%Y-%m-%d").date()).days
    except ValueError:
        return "Pick a valid day."
    if age < 0:
        return "That day has not happened yet."
    if age > MAX_AGE_DAYS:
        return f"Only the last {MAX_AGE_DAYS} days can be run again."
    if age == 0 and now.strftime("%H:%M") < CLOSE_FROM:
        return f"Available after {CLOSE_FROM}, once the engine has stopped for the day."
    if action == "review" and _has_verdict(day, folder):
        return "The review already has a verdict for this day."
    return None


def _has_verdict(day: str, folder: str) -> bool:
    path = os.path.join(folder, f"eod-{day}-claude-review.md")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "```close-day-verdict" in handle.read()
    except OSError:
        return False


# --- run state ------------------------------------------------------------------------------


def _state_path(run_id: str, state_dir: str | None = None) -> str:
    return os.path.join(state_dir or STATE_DIR, f"{run_id}.json")


def write_state(run_id: str, data: dict, state_dir: str | None = None) -> None:
    folder = state_dir or STATE_DIR
    os.makedirs(folder, exist_ok=True)
    tmp = _state_path(run_id, folder) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, _state_path(run_id, folder))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state(run_id: str, state_dir: str | None = None) -> dict | None:
    """One run's state. A run with no end marker whose process is gone is reported as failed."""
    if not RUN_ID.match(run_id or ""):
        return None
    try:
        with open(_state_path(run_id, state_dir), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if "ended" not in data and not _alive(int(data.get("pid", 0))):
        data = {**data, "ended": data.get("started"), "outcome": "failed", "exit": -1}
        data["message"] = "The run stopped before it finished. Run it again."
    return data


def latest_run(state_dir: str | None = None) -> dict | None:
    """The newest run on record, if it is recent."""
    folder = state_dir or STATE_DIR
    if not os.path.isdir(folder):
        return None
    ids = sorted(
        (n[:-5] for n in os.listdir(folder) if n.endswith(".json") and RUN_ID.match(n[:-5])),
        reverse=True,
    )
    for run_id in ids[:1]:
        state = read_state(run_id, folder)
        if state and time.time() - int(run_id.split("-")[0]) < STATE_KEEP_SECONDS:
            return state
    return None


def prune(state_dir: str | None = None, *, keep_days: int = 7) -> int:
    """Delete run state and log files older than `keep_days`. Returns how many were removed."""
    folder = state_dir or STATE_DIR
    if not os.path.isdir(folder):
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


def running(state_dir: str | None = None) -> dict | None:
    """The run that is still going, if any."""
    state = latest_run(state_dir)
    return state if state and state.get("outcome") is None else None


# --- the run itself ----------------------------------------------------------------------------


def append_step(day: str, step: str, started: str, ended: str, rc: int, folder: str | None = None):
    """Record the new result of a close-day step. The page shows the last row of each step."""
    path = os.path.join(folder or REPORTS, f"close-day-{day}-status.tsv")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{step}\t{started}\t{ended}\t{rc}\n")


def _tail(path: str, limit: int = 1500) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


#: After a step succeeds, these are re-run so the page does not keep a finding the fix cleared.
#: Each is a seconds-long read-only rebuild. The backfill feeds the value areas, and the
#: scheduled-jobs audit reads every step's status, so it comes last.
FOLLOW_UPS: dict[str, tuple[str, ...]] = {
    "backfill": ("value_areas", "jobs"),
    "eod": ("jobs",),
    "execution_health": ("jobs",),
    "logs": ("jobs",),
    "value_areas": ("jobs",),
    "review": ("jobs",),
    "jobs": (),
}


def _run_one(action: str, day: str, log: str) -> tuple[int, bool, str, str]:
    """Run one action's command once. Returns (exit code, timed out, started, ended) as HH:MM:SS."""
    spec = ACTIONS[action]
    started = now_ist().strftime("%H:%M:%S")
    env = {**os.environ, "PYTHONPATH": REPO}
    report = os.path.join(REPORTS, f"execution-health-{day}.md")
    out_path = report + ".part" if action == "execution_health" else None
    rc, timed_out = 1, False
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "ab") as log_handle:
            stdout = open(out_path, "wb") if out_path else log_handle  # noqa: SIM115
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv from command(), never a shell
                    command(action, day),
                    cwd=REPO,
                    env=env,
                    stdout=stdout,
                    stderr=log_handle,
                    stdin=subprocess.DEVNULL,
                    timeout=spec.timeout,
                    check=False,
                )
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc, timed_out = 124, True
            finally:
                if out_path:
                    stdout.close()
        if out_path:
            if rc == 0:
                os.replace(out_path, report)  # a partial report never replaces a good one
            elif os.path.exists(out_path):
                os.remove(out_path)
    except OSError:
        rc = 1
    ended = now_ist().strftime("%H:%M:%S")
    try:
        append_step(day, spec.step, started, ended, rc)
    except OSError:
        pass
    return rc, timed_out, started, ended


def _succeeded(action: str, rc: int) -> bool:
    # An ops check exits 1 by design when its own verdict is RED: it ran. Anything else non-zero
    # is a failure, the same reading the page applies to the cron run.
    return rc == 0 or (action in ("logs", "value_areas", "jobs") and rc == 1)


def run(
    action: str,
    day: str,
    run_id: str,
    *,
    state_dir: str | None = None,
    auto: bool = False,
) -> int:
    """Run one action to the end, then its follow-up re-checks. Returns 0 when the action worked.

    Progress is in the run's state file. `auto` marks a run the system started on its own.
    """
    spec = ACTIONS[action]
    command(action, day)  # refuse an unknown action or a bad day before anything is recorded
    folder = state_dir or STATE_DIR
    log = os.path.join(folder, f"{run_id}.log")
    started = now_ist()
    base = {
        "id": run_id,
        "action": action,
        "day": day,
        "label": spec.label,
        "pid": os.getpid(),
        "started": started.isoformat(timespec="seconds"),
        "auto": auto,
    }
    write_state(run_id, base, folder)

    rc, timed_out, _, _ = _run_one(action, day, log)
    ok = _succeeded(action, rc)
    followed: list[str] = []
    if ok:
        for follow in FOLLOW_UPS.get(action, ()):
            follow_rc, _, _, _ = _run_one(follow, day, log)
            followed.append(ACTIONS[follow].label if _succeeded(follow, follow_rc) else "")
    followed = [name for name in followed if name]

    message = (
        "Done. The page now shows the new result."
        if ok
        else "Stopped at the time limit."
        if timed_out
        else "It did not finish cleanly."
    )
    if ok and followed:
        message += f" Also done: {', '.join(n[0].lower() + n[1:] for n in followed)}."
    write_state(
        run_id,
        {
            **base,
            "ended": now_ist().isoformat(timespec="seconds"),
            "exit": rc,
            "outcome": "ok" if ok else "failed",
            "message": message,
            "tail": _tail(log),
        },
        folder,
    )
    return 0 if ok else 1
