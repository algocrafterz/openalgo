"""Close-day step `jobs`: did every scheduled job run today, and succeed?

Owns the schedule audit. Sources: Windows Task Scheduler (process lifecycle), the crontab (every OpenAlgo
line) and each phase's step status file.
"""

from __future__ import annotations

import csv
import datetime
import io
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from signal_engine.ops import verdict
from signal_engine.ops.model import FAIL, OK, REPORTS, WARN, Check, read_phase, write_phase

#: THE MAINTAINER FLAG. False while the crontab and the Windows tasks still hold the schedule that
#: ran before the 16:01 retry and the 08:36 recovery. Every changed job then accepts its old and its
#: new time, a job that is new reports "not installed yet" as a WARN, and a job that is going away
#: reports a note. After the maintainer installs the new crontab and the two Windows task changes
#: (the commands are in the Phase 1b cron proposal), set this to True in the same change: the new
#: lines become mandatory, an old time is a problem, and a line that should be gone is a problem.
REQUIRE_NEW_SCHEDULE = True
#: ROLLOUT ORDER of the Phase 1b schedule. The reason for each step is in the proposal
#: (cron_diff.txt) and in the signal_engine/ops/recovery.py docstring:
#:   1. merge the code (the commits);
#:   2. install the two Windows task changes (AutoStart 08:30 with a 10-hour limit, AutoStop
#:      16:31), on a weekend: the 16:01 retry needs OpenAlgo up until 16:25 and the 08:36
#:      recovery needs the 08:30 login;
#:   3. then install the cron lines;
#:   4. then set this flag to True and commit.

#: Windows task -> a time its own run is checked against (the run must have happened today).
#: Their own scripts live in signal_engine/scripts/*.ps1. openAlgoAutoStart is 08:50 until the
#: maintainer moves it to 08:30; the start time itself is checked by WINDOWS_START_TIMES.
WINDOWS_TASKS = {
    "openAlgoAutoStart": "08:30" if REQUIRE_NEW_SCHEDULE else "08:50",
    "openAlgoWatchdog": None,
    "openAlgoHeartbeatCheck": None,
    "breakingTradeAutoStart": "09:10",
    "breakingTradeWatchdog": None,
    "openAlgoSquareOff": "14:55",
    "breakingTradeAutoStop": "15:35",
    "openAlgoAutoStop": None,  # 16:00 until moved to 16:31, after this step: shows the previous run
}
#: Windows task -> (old start time, new start time) read from Task Scheduler's "Start Time" column.
#: The old time passes while REQUIRE_NEW_SCHEDULE is False, with a pending-install note. The 08:30
#: start gives OpenAlgo a broker login before the 08:36 recovery; the 16:31 stop leaves the broker
#: session alive for the 16:01 retry. Both are repo-side changes of signal_engine/scripts/
#: createTaskOpenAlgoScheduler.ps1; the live task is changed by the maintainer.
WINDOWS_START_TIMES = {
    "openAlgoAutoStart": ("08:50", "08:30"),
    "openAlgoAutoStop": ("16:00", "16:31"),
}


@dataclass(frozen=True)
class CronLine:
    """One OpenAlgo cron line the audit looks for.

    `times` holds the accepted "HH:MM" times, the proposed time last. `exclude` keeps a line with
    that text out of the match (the plain close-day line must not take the --retry line). `new`
    marks a job that the proposal adds: while it is absent and REQUIRE_NEW_SCHEDULE is False the
    audit gives a WARN, not a problem.
    """

    needle: str
    label: str
    times: tuple[str, ...]
    exclude: str = ""
    new: bool = False


#: Every OpenAlgo cron line of the proposed schedule. The audit checks the script, its arguments,
#: the time and the weekday field (1-5) of each line, so a line that moved or was deleted is found,
#: not only a line that is missing. The last time of `times` is the proposed one.
CRON_LINES = (
    CronLine("signal_engine/ops/run_upkeep.sh recover", "recover", ("08:36",), new=True),
    CronLine("signal_engine/ops/run_phase.sh open-day", "open-day", ("09:12",)),
    CronLine("signal_engine/ops/run_phase.sh liveness", "liveness", ("09:27",)),
    CronLine("signal_engine/ops/run_upkeep.sh deadman morning", "deadman morning", ("09:40",)),
    CronLine("signal_engine/analysis/close_day.sh", "close-day", ("15:36",), exclude="--retry"),
    CronLine(
        "signal_engine/analysis/close_day.sh --retry", "close-day retry", ("16:01",), new=True
    ),
    CronLine("signal_engine/ops/run_upkeep.sh deadman close", "deadman close", ("16:20", "16:28")),
)
#: Lines that run today and leave the schedule: (what the line runs, label, their times). They
#: raise a note while they are installed, and a problem once REQUIRE_NEW_SCHEDULE is True.
#: autofix.sh stays in the repository; only its 16:30 cron line goes. The two review catch-ups
#: are replaced by the 16:01 retry and the 08:36 recovery. retry-failed becomes recover.
CRON_TO_REMOVE = (
    ("signal_engine/ops/run_upkeep.sh retry-failed", "retry-failed", ("08:56",)),
    ("signal_engine/analysis/autofix.sh", "autofix", ("16:30",)),
    ("signal_engine/analysis/review_catchup.sh", "review catch-up 1", ("18:35",)),
    ("signal_engine/analysis/review_catchup.sh", "review catch-up 2", ("21:05",)),
)
PHASE_FILES = ("open-day", "liveness", "close-day")

#: cron and agent shells have no Windows directories on PATH, so a bare name is not found.
WINDOWS_SCHTASKS = "/mnt/c/Windows/System32/schtasks.exe"
#: (phase, step) -> report that step writes. These steps exit 1 when their verdict is RED: the
#: step ran and reported a finding, which is not the same as a job that failed or never ran.
REPORTING_STEPS = {
    ("open-day", "check"): "open-day",
    ("liveness", "check"): "liveness",
    ("close-day", "jobs"): "jobs",
    ("close-day", "logs"): "logs",
    ("close-day", "value_areas"): "value-areas",
}


def schtasks_exe() -> str:
    """Path of schtasks.exe: PATH first, then the standard WSL mount, else the bare name."""
    found = shutil.which("schtasks.exe")
    if found:
        return found
    return WINDOWS_SCHTASKS if os.path.exists(WINDOWS_SCHTASKS) else "schtasks.exe"


def reported_red(
    phase: str, step: str, started: str, exit_code: str, day: str, folder: str = REPORTS
) -> bool:
    """True when this failing step exited 1 because its own report says RED.

    Anything else stays a failure: another exit code, no report, or a report written before this
    run started (a leftover from an earlier run that this run did not replace).
    """
    report = REPORTING_STEPS.get((phase, step))
    if report is None or exit_code != "1":
        return False
    data = read_phase(report, day, folder=folder)
    if not data or data.get("verdict") != "RED":
        return False
    try:
        began = datetime.datetime.fromisoformat(f"{day}T{started}").timestamp()
        written = os.path.getmtime(os.path.join(folder, f"{report}-{day}.json"))
    except (ValueError, OSError):
        return False
    return written >= began - 1


def parse_schtasks(text: str) -> dict[str, dict]:
    """`schtasks /query /fo csv /v` -> {task name: row}. Columns are by header, not position."""
    rows = {}
    for row in csv.DictReader(io.StringIO(text.replace("\r", ""))):
        name = (row.get("TaskName") or "").lstrip("\\")
        if name and name not in rows:
            rows[name] = row
    return rows


def _windows_tasks() -> dict[str, dict] | None:
    try:
        out = subprocess.run(
            [schtasks_exe(), "/query", "/fo", "csv", "/v"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_schtasks(out.stdout) if out.returncode == 0 else None


def task_check(name: str, at: str | None, row: dict | None, day: str) -> Check:
    if row is None:
        return Check(f"task {name}", FAIL, "not registered in Task Scheduler")
    last, result = row.get("Last Run Time", ""), (row.get("Last Result") or "").strip()
    day_dmy = f"{day[8:10]}-{day[5:7]}-{day[0:4]}"
    ran_today = last.startswith(day_dmy)
    if at and not ran_today:
        return Check(f"task {name}", FAIL, f"expected {at}, last ran {last or 'never'}")
    status = (
        OK if result in ("0", "267009", "267011") else WARN
    )  # 267009 running, 267011 not yet run
    return Check(f"task {name}", status, f"last {last}, result {result}")


def _phase_checks(day: str, folder: str = REPORTS) -> list[Check]:
    checks = []
    for phase in PHASE_FILES:
        path = os.path.join(folder, f"{phase}-{day}-status.tsv")
        if not os.path.exists(path):
            status = WARN if phase == "close-day" else FAIL
            checks.append(Check(f"phase {phase}", status, "no status file today"))
            continue
        with open(path, encoding="utf-8") as handle:
            steps = [line.rstrip("\n").split("\t") for line in handle][1:]
        steps = [s for s in steps if len(s) == 4]
        # A step run again (the retry, the recovery, the page) appends a row: its newest row counts.
        failed = [s for s in {s[0]: s for s in steps}.values() if s[3] != "0"]
        reported = [s for s in failed if reported_red(phase, s[0], s[1], s[3], day, folder)]
        # The review is judged by what it produced, not by its exit code (verdict.review_outcome).
        review = [s for s in failed if phase == "close-day" and s[0] == "review"]
        if review:
            outcome = verdict.review_outcome(day, folder)
            evidence_note = f"; review: {outcome['outcome']}"
            if outcome["outcome"] != "failed":
                reported = reported + review
        else:
            evidence_note = ""
        quiet_review = ""
        if phase == "close-day" and not review:
            outcome = verdict.review_outcome(day, folder)
            if any(s[0] == "review" for s in steps) and outcome["outcome"] != "verdict":
                quiet_review = f"; review exited 0 but gave no verdict ({outcome['outcome']})"
        bad = [s for s in failed if s not in reported]
        evidence = ", ".join(f"{s[0]} {s[1]}-{s[2]} exit {s[3]}" for s in steps)
        if bad:
            status = FAIL
        elif reported:
            status = WARN
            if any(s not in review for s in reported):
                evidence += "; it ran and reported RED (see that day's report)"
            evidence += evidence_note
        elif quiet_review:
            status = WARN
            evidence += quiet_review
        else:
            status = OK
        checks.append(Check(f"phase {phase}", status, evidence or "no steps recorded"))
    return checks


def parse_cron_line(line: str) -> tuple[str, str, str] | None:
    """(HH:MM, weekday field, command) of a crontab line, or None for a comment or other line."""
    text = line.strip()
    if not text or text.startswith("#") or "=" in text.split()[0]:
        return None
    fields = text.split(None, 5)
    if len(fields) < 6 or not (fields[0].isdigit() and fields[1].isdigit()):
        return None
    return f"{int(fields[1]):02d}:{int(fields[0]):02d}", fields[4], fields[5]


def audit_cron_full(
    text: str, *, require_new: bool | None = None
) -> tuple[list[str], list[str], list[str]]:
    """(problems, warnings, notes) of a crontab text against CRON_LINES and CRON_TO_REMOVE.

    A problem is a missing line, a wrong time or a wrong weekday field. A warning is a new line
    that is not installed yet. A note is a line on an accepted older time, or a line that is going
    away. With `require_new` True (REQUIRE_NEW_SCHEDULE) only the proposed times pass, every new
    line is mandatory and a line that should be gone is a problem.
    """
    strict = REQUIRE_NEW_SCHEDULE if require_new is None else require_new
    lines = [p for p in (parse_cron_line(ln) for ln in text.splitlines()) if p]
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    used: set[int] = set()
    for spec in CRON_LINES:
        times = (spec.times[-1],) if strict else spec.times
        match = [
            i
            for i, (_, _, cmd) in enumerate(lines)
            if spec.needle in cmd and i not in used and not (spec.exclude and spec.exclude in cmd)
        ]
        exact = [i for i in match if lines[i][0] in times]
        if not exact:
            if spec.new and not match and not strict:
                warnings.append(
                    f"{spec.label}: not installed yet (pending install: add the {times[-1]} line)"
                )
                continue
            seen = ", ".join(lines[i][0] for i in match)
            problems.append(
                f"{spec.label}: expected {' or '.join(times)}, "
                f"{'found ' + seen if seen else 'no line found'}"
            )
            continue
        used.add(exact[0])
        if lines[exact[0]][1] != "1-5":
            problems.append(f"{spec.label}: weekday field is {lines[exact[0]][1]}, expected 1-5")
        if lines[exact[0]][0] != spec.times[-1]:
            notes.append(
                f"{spec.label} still at {lines[exact[0]][0]} "
                f"(pending install: move to {spec.times[-1]})"
            )
    for needle, label, old_times in CRON_TO_REMOVE:
        found = [
            i
            for i, (when, _, cmd) in enumerate(lines)
            if needle in cmd and when in old_times and i not in used
        ]
        for i in found[:1]:
            used.add(i)
            if strict:
                problems.append(f"{label}: the {lines[i][0]} line must be removed")
            else:
                notes.append(f"{label} at {lines[i][0]} is to be removed (pending install)")
    return problems, warnings, notes


def audit_cron(text: str, *, require_new: bool | None = None) -> tuple[list[str], list[str]]:
    """(problems, notes) for a crontab text. The notes include the "not installed yet" warnings."""
    problems, warnings, notes = audit_cron_full(text, require_new=require_new)
    return problems, warnings + notes


def _cron_check() -> Check:
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return Check("crontab", WARN, "crontab unreadable")
    problems, warnings, notes = audit_cron_full(out)
    if problems:
        return Check("crontab", FAIL, "; ".join(problems))
    extra = warnings + notes
    ok = f"{len(CRON_LINES)} cron lines scheduled" + (f" ({'; '.join(extra)})" if extra else "")
    return Check("crontab", WARN if warnings else OK, ok)


_TASK_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp])?\.?[Mm]?\.?$")


def task_time_24h(text: str) -> str | None:
    """A Task Scheduler "Start Time" as HH:MM on the 24-hour clock, or None when it cannot be read.

    The column follows the Windows locale: "08:50:00", "8:50:00 AM", "4:31:00 PM". A 12-hour
    value without AM or PM is read as written.
    """
    match = _TASK_TIME.match(text.strip())
    if not match:
        return None
    hour, minute, half = int(match.group(1)), int(match.group(2)), match.group(3)
    if minute > 59 or hour > 23 or (half and not 1 <= hour <= 12):
        return None
    if half:
        hour = hour % 12 + (12 if half.lower() == "p" else 0)
    return f"{hour:02d}:{minute:02d}"


def start_time_check(name: str, row: dict | None) -> Check | None:
    """Compare a Windows task's start time with its accepted old and new time, or None.

    None when the task has no expected start time, is not registered (task_check reports that) or
    Task Scheduler shows no start time at all (empty or "N/A"). A value that is present but cannot
    be read (an unknown locale format) is a WARN, never a silent skip. The old time is accepted
    with a pending-install note until REQUIRE_NEW_SCHEDULE is True.
    """
    accepted = WINDOWS_START_TIMES.get(name)
    if accepted is None or row is None:
        return None
    raw = (row.get("Start Time") or "").strip()
    if not raw or raw.upper() == "N/A":
        return None
    label = f"task {name} start time"
    seen = task_time_24h(raw)
    if seen is None:
        return Check(
            label,
            WARN,
            "could not read the task time: Task Scheduler shows it in a format this check does "
            "not know. Next: open Task Scheduler and read the start time of the task by hand.",
        )
    old, new = accepted
    if seen == new:
        return Check(label, OK, f"starts at {seen}")
    if seen == old and not REQUIRE_NEW_SCHEDULE:
        return Check(label, OK, f"starts at {seen} (pending install: move to {new})")
    expected = new if REQUIRE_NEW_SCHEDULE else f"{old} or {new}"
    return Check(label, FAIL, f"starts at {seen}, expected {expected}")


def run(day: str) -> str:
    tasks = _windows_tasks()
    checks = []
    if tasks is None:
        checks.append(Check("Windows Task Scheduler", "NOT VERIFIED", "schtasks.exe unavailable"))
    else:
        checks += [task_check(name, at, tasks.get(name), day) for name, at in WINDOWS_TASKS.items()]
        checks += [c for c in (start_time_check(n, tasks.get(n)) for n in WINDOWS_START_TIMES) if c]
    checks.append(_cron_check())
    checks += _phase_checks(day)
    summary = "Did every scheduled job run today?"
    return write_phase("jobs", day, checks, summary)
