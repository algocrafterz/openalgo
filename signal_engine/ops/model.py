"""What every ops check returns, and how a phase report is written.

A phase (open-day, liveness, a close-day step) is a list of checks. Each check has one status and
the evidence for it; the phase verdict is the worst status. The report is markdown for people and
a JSON file for the notify step and the /logs workflow page, so nothing parses prose.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # Linux
    msvcrt = None

REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")

OK, WARN, FAIL, NOT_VERIFIED = "OK", "WARN", "FAIL", "NOT VERIFIED"
_RANK = {OK: 0, NOT_VERIFIED: 1, WARN: 1, FAIL: 2}
VERDICT = {0: "GREEN", 1: "AMBER", 2: "RED"}
MEANING = {"GREEN": "all good", "AMBER": "needs a look", "RED": "action needed"}
TITLES = {
    "open-day": "Before the open",
    "liveness": "After the open",
    "logs": "Errors today",
    "jobs": "Scheduled jobs",
    "value-areas": "Value areas",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    evidence: str
    #: One sentence for a trader (signal_engine/ops/plain.py); filled in by write_phase.
    plain: str = ""


def verdict(checks: list[Check]) -> str:
    """GREEN when every check is OK, RED when any FAILed, AMBER otherwise."""
    return VERDICT[max((_RANK[c.status] for c in checks), default=0)]


def write_phase(
    phase: str, day: str, checks: list[Check], summary: str, *, folder: str = REPORTS
) -> str:
    """Write <phase>-<day>.md and .json; return the markdown path."""
    from signal_engine.ops.plain import plain_for

    os.makedirs(folder, exist_ok=True)
    checks = [c if c.plain else replace(c, plain=plain_for(c.name, c.status)) for c in checks]
    result = verdict(checks)
    attention = [c for c in checks if c.status != OK]
    headline = (
        f"All {len(checks)} checks passed."
        if not attention
        else f"{len(attention)} of {len(checks)} checks need a look:"
    )
    plain_lines = "\n".join(f"- **{c.status}**: {c.plain or c.name}" for c in attention)
    rows = "\n".join(
        f"| {c.plain or c.name} | {c.status} | {c.name}: {c.evidence.replace('|', '/')} |"
        for c in checks
    )
    text = (
        f"# {TITLES.get(phase, phase)}, {day}: {MEANING[result]} ({result})\n\n"
        f"{summary}\n\n{headline}\n{plain_lines}\n\n"
        f"## Details\n\n| What it means | Status | Technical detail |\n| --- | --- | --- |\n{rows}\n"
    )
    md = os.path.join(folder, f"{phase}-{day}.md")
    with open(md, "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(os.path.join(folder, f"{phase}-{day}.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "phase": phase,
                "day": day,
                "verdict": result,
                "summary": summary,
                "checks": [asdict(c) for c in checks],
            },
            handle,
            indent=2,
        )
    return md


def read_phase(phase: str, day: str, *, folder: str = REPORTS) -> dict | None:
    path = os.path.join(folder, f"{phase}-{day}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------------------------
# Run manifest: one small JSON file per phase run (reports/<phase>-<day>-run.json).
#
# The status.tsv file records a step only when it ends, so it cannot tell "still running" from
# "killed" or "never started". The manifest is written when the run starts, updated at each step
# end and finalised with a terminal state. Shell scripts call this file directly with the system
# python3 (stdlib only, no uv, about 30 ms) so a step costs almost nothing:
#
#     python3 signal_engine/ops/model.py manifest start|step|finish|review ...
#
# "missed" is never written. A reader derives it: an expected phase on a trading day with no
# manifest after its due time plus a margin (signal_engine/ops/automation.py).
# --------------------------------------------------------------------------------------------

MANIFEST_VERSION = 1
RUNNING, COMPLETED, FAILED, INTERRUPTED, TIMED_OUT = (
    "running",
    "completed",
    "failed",
    "interrupted",
    "timed_out",
)
TERMINAL_STATES = (COMPLETED, FAILED, INTERRUPTED, TIMED_OUT)
#: Reader-only states. `legacy`: no manifest but a status file (a day before manifests existed).
#: `missing`: neither. `unreadable`: a manifest that is not valid JSON.
LEGACY, MISSING, UNREADABLE = "legacy", "missing", "unreadable"
#: A manifest that says `running` for longer than this is read as interrupted. The longest real run
#: is close-day (15:36 to the 15:58 deadline, about 22 minutes); a hand re-run of an earlier day
#: gets 20 minutes plus the review. One hour covers both with room to spare.
MAX_RUN_SECONDS = 3600
IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------------------------
# The afternoon retry window (close_day.sh --retry). ONE definition: recovery.py imports these
# names and close_day.sh asks `model.py retry-limits`, so the shell and the python cannot drift.
#   16:25:00  RETRY_DEADLINE: the soft end. No step starts after 16:23, the last notification
#             may run to 16:25.
#   16:26:40  RETRY_OUTER_TERM: the outer `timeout` sends TERM here, 15 s before its KILL.
#   16:26:55  TERM + RETRY_KILL_AFTER_SECONDS: the last moment the retry process can exist.
#   16:27:00  RETRY_HARD_END: nothing of the retry runs past this. Its 5 s margin covers the
#             exit of the killed process. The 16:28 dead-man and the 16:30 limit stay as before.
# --------------------------------------------------------------------------------------------
RETRY_DEADLINE = "16:25"
RETRY_HARD_END = "16:27"
RETRY_OUTER_TERM = "16:26:40"
RETRY_KILL_AFTER_SECONDS = 15
#: A retry that starts with less time than this before RETRY_DEADLINE does not start.
RETRY_MIN_START_SECONDS = 30
#: Test hook: an ISO date-time that replaces the clock. Unset in production.
NOW_ENV = "OPS_RECOVERY_NOW"

#: Steps that must finish for a phase to count as complete. The review has its own rule
#: (signal_engine/ops/verdict.py review_outcome) and is not listed.
#:  - open-day, liveness: `check` writes the report and `notify` is the only alert path for RED.
#:  - close-day (signal_engine/analysis/close_day.sh): `eod` (tradebook snapshot and ledger,
#:    everything else reads it), `backfill` (tomorrow's data), `execution_health` and `logs`
#:    (the review's inputs), `value_areas` (needs the backfill), `jobs` (schedule audit), `track`
#:    (records failed checks in the bug tracker even when the review cannot run) and `notify`
#:    (the day's one Telegram line). `weekly` runs on Fridays only and is not critical.
CRITICAL_STEPS = {
    "open-day": ("check", "notify"),
    "liveness": ("check", "notify"),
    "close-day": (
        "eod",
        "backfill",
        "execution_health",
        "logs",
        "value_areas",
        "jobs",
        "track",
        "notify",
    ),
}
#: These steps exit 1 on purpose when their own report says RED: it is a finding, not a crash.
RED_EXIT_OK_STEPS = ("check", "logs", "value_areas", "jobs")


def now_ist() -> datetime:
    return datetime.now(IST)


def clock_now() -> datetime:
    """Now in IST. OPS_RECOVERY_NOW replaces it for tests only (unset in production)."""
    fake = os.environ.get(NOW_ENV)
    if fake:
        stamp = datetime.fromisoformat(fake)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=IST)
    return now_ist()


def at_ist(day: str, clock: str) -> datetime:
    """`day` (YYYY-MM-DD) at `clock` (HH:MM or HH:MM:SS), IST."""
    fmt = "%Y-%m-%d %H:%M:%S" if clock.count(":") == 2 else "%Y-%m-%d %H:%M"
    return datetime.strptime(f"{day} {clock}", fmt).replace(tzinfo=IST)


def retry_outer_limits(now: datetime, day: str) -> tuple[int, int] | None:
    """(timeout seconds, kill-after seconds) of the outer `timeout` around the afternoon retry.

    The timeout sends TERM at RETRY_OUTER_TERM and KILL RETRY_KILL_AFTER_SECONDS later, so the
    process is gone by 16:26:55 and always before RETRY_HARD_END. The cases:
      - another day than today: python exits at once; a short fixed limit;
      - a start before 16:26:35: TERM at RETRY_OUTER_TERM;
      - a late start: python only writes the "started too late" records, so it gets the time
        that is left minus two seconds (at most 8) and a one second kill-after;
      - None when less than 3 seconds remain before RETRY_HARD_END: nothing can be recorded.
    """
    today = now.astimezone(IST).strftime("%Y-%m-%d")
    if day != today:
        return 120, RETRY_KILL_AFTER_SECONDS
    room = int((at_ist(today, RETRY_HARD_END) - now).total_seconds())
    if room < 3:
        return None
    wait = int((at_ist(today, RETRY_OUTER_TERM) - now).total_seconds())
    if wait >= 5:
        return wait, RETRY_KILL_AFTER_SECONDS
    return max(1, min(8, room - 2)), 1


def atomic_write_json(path: str, data: dict) -> None:
    """Write `data` so a reader sees the old file or the new file, never half of one."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError, AttributeError):  # directory fsync: best effort
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


_BUSY = (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK)


@contextlib.contextmanager
def file_lock(folder: str, timeout: float = 10.0):
    """Exclusive cross-process lock for a read-modify-write of a small state file.

    Same pattern as signal_engine/bugs/store.py: flock on Linux, a byte lock on Windows, a
    non-blocking try in a sleep loop, and the lock dies with its holder. These scripts run as plain
    processes (cron, shell), never inside the OpenAlgo worker, so real threads and time.sleep are
    correct here. Raises TimeoutError when the lock stays busy.
    """
    os.makedirs(folder, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(os.path.join(folder, ".ops-state.lock"), "a") as handle:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if exc.errno not in _BUSY:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("the ops state lock stayed busy") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def manifest_path(phase: str, day: str, folder: str = REPORTS) -> str:
    return os.path.join(folder, f"{phase}-{day}-run.json")


def _load_manifest(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"state": UNREADABLE}
    return data if isinstance(data, dict) else {"state": UNREADABLE}


def manifest_start(phase: str, day: str, *, pid: int, folder: str = REPORTS) -> dict:
    """Begin a run: state `running`, a new run id, the pid and the start time in IST."""
    path = manifest_path(phase, day, folder)
    with file_lock(folder):
        previous = _load_manifest(path) or {}
        now = now_ist()
        data = {
            "version": MANIFEST_VERSION,
            "phase": phase,
            "day": day,
            "run_id": f"{phase}-{day}-{int(now.timestamp())}-{pid}",
            "previous_run_id": previous.get("run_id"),
            "pid": pid,
            "state": RUNNING,
            "started_at": now.isoformat(timespec="seconds"),
            "ended_at": None,
            "expected_steps": list(CRITICAL_STEPS.get(phase, ())),
            "steps": [],
            "note": "",
        }
        atomic_write_json(path, data)
    return data


def _update(phase: str, day: str, folder: str, change) -> dict | None:
    path = manifest_path(phase, day, folder)
    with file_lock(folder):
        data = _load_manifest(path)
        if not data or data.get("state") == UNREADABLE:
            return None
        change(data)
        atomic_write_json(path, data)
    return data


def manifest_step(
    phase: str,
    day: str,
    step: str,
    started: str,
    ended: str,
    exit_code: int,
    folder: str = REPORTS,
    *,
    result: str = "",
    attempt: str = "",
) -> dict | None:
    """Record one finished step. A step run again replaces nothing: it appends a row.

    `result` and `attempt` are set only by a retry or a recovery run (signal_engine/ops/recovery.py):
    the outcome in words (recovered, failed, timed_out, blocked, ...) and the id of that run.
    """

    def change(data: dict) -> None:
        row = {"step": step, "started": started, "ended": ended, "exit": int(exit_code)}
        if result:
            row["result"] = result
        if attempt:
            row["attempt"] = attempt
        data.setdefault("steps", []).append(row)

    return _update(phase, day, folder, change)


def derive_final_state(phase: str, steps: list[dict]) -> str:
    """completed, failed or timed_out, from the recorded steps. Never `interrupted`."""
    latest: dict[str, int] = {}
    for row in steps:
        latest[row["step"]] = int(row["exit"])
    if any(code == 124 for code in latest.values()):
        return TIMED_OUT  # any step cut off at the deadline, the review included
    for name in CRITICAL_STEPS.get(phase, ()):
        code = latest.get(name)
        if code is None:
            continue  # a step that never ran is the dead-man's finding, not a failure here
        if code != 0 and not (code == 1 and name in RED_EXIT_OK_STEPS):
            return FAILED
    return COMPLETED


def manifest_finish(
    phase: str, day: str, state: str = "auto", note: str = "", folder: str = REPORTS
) -> dict | None:
    """Finalise a run. `auto` derives completed, failed or timed_out from the steps."""

    def change(data: dict) -> None:
        final = derive_final_state(phase, data.get("steps", [])) if state == "auto" else state
        if data.get("state") in TERMINAL_STATES and data.get("state") != final:
            # A second finish (the exit trap after a normal finish) never rewrites the outcome.
            return
        data["state"] = final
        data["ended_at"] = now_ist().isoformat(timespec="seconds")
        if note:
            data["note"] = note

    return _update(phase, day, folder, change)


def manifest_review(
    phase: str, day: str, status: str, attempt: str, exit_code: int, folder: str = REPORTS
) -> dict | None:
    """Record the newest review attempt (a catch-up adds one after the run has ended)."""

    def change(data: dict) -> None:
        data["review"] = {
            "status": status,
            "attempt": attempt,
            "exit": int(exit_code),
            "at": now_ist().isoformat(timespec="seconds"),
        }

    return _update(phase, day, folder, change)


def _pid_alive(pid: object) -> bool | None:
    """True or False for a pid on this machine; None when it cannot be told (not Linux)."""
    if not isinstance(pid, int) or pid <= 0 or sys.platform == "win32":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def read_run(phase: str, day: str, folder: str = REPORTS, *, now: datetime | None = None) -> dict:
    """The run manifest of a phase and day with its effective state.

    `state` is what to believe: a stored `running` whose pid is gone, or that is older than
    MAX_RUN_SECONDS, reads as `interrupted` (never as running or successful). With no manifest the
    state is `legacy` when a status file exists (old day, fall back to it) and `missing` otherwise.
    """
    data = _load_manifest(manifest_path(phase, day, folder))
    if data is None:
        legacy = os.path.exists(os.path.join(folder, f"{phase}-{day}-status.tsv"))
        return {"phase": phase, "day": day, "state": LEGACY if legacy else MISSING, "steps": []}
    if data.get("state") == UNREADABLE:
        return {"phase": phase, "day": day, "state": UNREADABLE, "steps": []}
    data["stored_state"] = data.get("state")
    if data.get("state") == RUNNING:
        now = now or now_ist()
        reason = ""
        try:
            age = (now - datetime.fromisoformat(data["started_at"])).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = None
        if _pid_alive(data.get("pid")) is False:
            reason = "The run's process is gone."
        elif age is None or age > MAX_RUN_SECONDS:
            reason = "The run has been open longer than the maximum run time."
        if reason:
            data["state"] = INTERRUPTED
            data["note"] = (data.get("note") or reason).strip()
    elif data.get("state") not in TERMINAL_STATES:
        data["state"] = UNREADABLE
    return data


def _retry_limits_cli(argv: list[str]) -> int:
    """`model.py retry-limits --day D`: print "<timeout> <kill-after>" or "refuse" for the shell."""
    parser = argparse.ArgumentParser(prog="model.py retry-limits")
    parser.add_argument("--day", required=True)
    args = parser.parse_args(argv)
    limits = retry_outer_limits(clock_now(), args.day)
    sys.stdout.write("refuse\n" if limits is None else f"{limits[0]} {limits[1]}\n")
    return 0


def _cli(argv: list[str] | None = None) -> int:
    if (argv if argv is not None else sys.argv[1:])[:1] == ["retry-limits"]:
        return _retry_limits_cli((argv if argv is not None else sys.argv[1:])[1:])
    parser = argparse.ArgumentParser(prog="model.py")
    parser.add_argument("area", choices=("manifest",))
    parser.add_argument("action", choices=("start", "step", "finish", "review"))
    parser.add_argument("--phase", required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--folder", default=REPORTS)
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--name", default="")
    parser.add_argument("--started", default="")
    parser.add_argument("--ended", default="")
    parser.add_argument("--exit", type=int, default=0, dest="exit_code")
    parser.add_argument("--state", default="auto")
    parser.add_argument("--note", default="")
    parser.add_argument("--status", default="")
    parser.add_argument("--attempt", default="")
    parser.add_argument("--result", default="")
    args = parser.parse_args(argv)
    try:
        if args.action == "start":
            manifest_start(args.phase, args.day, pid=args.pid, folder=args.folder)
        elif args.action == "step":
            manifest_step(
                args.phase,
                args.day,
                args.name,
                args.started,
                args.ended,
                args.exit_code,
                args.folder,
                result=args.result,
                attempt=args.attempt,
            )
        elif args.action == "finish":
            manifest_finish(args.phase, args.day, args.state, args.note, args.folder)
        else:
            manifest_review(
                args.phase, args.day, args.status, args.attempt, args.exit_code, args.folder
            )
    except (OSError, TimeoutError, ValueError) as exc:
        # The manifest is evidence, never a dependency: a write that fails must not stop a phase.
        sys.stderr.write(f"manifest {args.action} failed: {type(exc).__name__}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
