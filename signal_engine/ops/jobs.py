"""Close-day step `jobs`: did every scheduled job run today, and succeed?

Owns the schedule audit. Sources: Windows Task Scheduler (process lifecycle), the crontab (all nine OpenAlgo
lines) and each phase's step status file.
"""

from __future__ import annotations

import csv
import datetime
import io
import os
import shutil
import subprocess

from signal_engine.ops import verdict
from signal_engine.ops.model import FAIL, OK, REPORTS, WARN, Check, read_phase, write_phase

#: Windows task -> scheduled HH:MM. Their own scripts live in signal_engine/scripts/*.ps1.
WINDOWS_TASKS = {
    "openAlgoAutoStart": "08:50",
    "openAlgoWatchdog": None,
    "openAlgoHeartbeatCheck": None,
    "breakingTradeAutoStart": "09:10",
    "breakingTradeWatchdog": None,
    "openAlgoSquareOff": "14:55",
    "breakingTradeAutoStop": "15:35",
    "openAlgoAutoStop": None,  # 16:00, after this step: shows the previous run
}
#: Every OpenAlgo cron line: (what the line runs, label, accepted "HH:MM" times). The audit checks
#: the script, its arguments, the time and the weekday field (1-5) of each line, so a line that
#: moved or was deleted is found, not only a line that is missing.
#: The close-day dead-man accepts three times. 16:20 is the line installed today. 21:35 is the line
#: proposed in the Phase 1 cron proposal: it must run after the 21:05 catch-up. Both pass until the
#: maintainer switches the cron line, so deploying this code before the cron change never turns the
#: audit RED. When the cron line is on 21:35, delete "16:20" here in the same change.
CRON_LINES = (
    ("signal_engine/ops/run_upkeep.sh retry-failed", "retry-failed", ("08:56",)),
    ("signal_engine/ops/run_phase.sh open-day", "open-day", ("09:12",)),
    ("signal_engine/ops/run_phase.sh liveness", "liveness", ("09:27",)),
    ("signal_engine/ops/run_upkeep.sh deadman morning", "deadman morning", ("09:40",)),
    ("signal_engine/analysis/close_day.sh", "close-day", ("15:36",)),
    ("signal_engine/ops/run_upkeep.sh deadman close", "deadman close", ("16:20", "21:30", "21:35")),
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
        failed = [s for s in steps if s[3] != "0"]
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


def audit_cron(text: str) -> tuple[list[str], list[str]]:
    """(problems, notes) for a crontab text against CRON_LINES. A problem is a missing line, a
    wrong time or a wrong weekday field. A note is a line on an accepted but older time."""
    lines = [p for p in (parse_cron_line(ln) for ln in text.splitlines()) if p]
    problems: list[str] = []
    notes: list[str] = []
    used: set[int] = set()
    for needle, label, times in CRON_LINES:
        match = [
            i
            for i, (_, _, cmd) in enumerate(lines)
            if needle in cmd and i not in used and not cmd.startswith("#")
        ]
        exact = [i for i in match if lines[i][0] in times]
        if not exact:
            seen = ", ".join(lines[i][0] for i in match)
            problems.append(
                f"{label}: expected {' or '.join(times)}, "
                f"{'found ' + seen if seen else 'no line found'}"
            )
            continue
        used.add(exact[0])
        if lines[exact[0]][1] != "1-5":
            problems.append(f"{label}: weekday field is {lines[exact[0]][1]}, expected 1-5")
        if lines[exact[0]][0] != times[-1] and len(times) > 1:
            notes.append(f"{label} still at {lines[exact[0]][0]} (move to {times[-1]})")
    return problems, notes


def _cron_check() -> Check:
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return Check("crontab", WARN, "crontab unreadable")
    problems, notes = audit_cron(out)
    ok = f"{len(CRON_LINES)} cron lines scheduled" + (f" ({'; '.join(notes)})" if notes else "")
    return Check("crontab", FAIL if problems else OK, "; ".join(problems) if problems else ok)


def run(day: str) -> str:
    tasks = _windows_tasks()
    checks = []
    if tasks is None:
        checks.append(Check("Windows Task Scheduler", "NOT VERIFIED", "schtasks.exe unavailable"))
    else:
        checks += [task_check(name, at, tasks.get(name), day) for name, at in WINDOWS_TASKS.items()]
    checks.append(_cron_check())
    checks += _phase_checks(day)
    summary = "Did every scheduled job run today?"
    return write_phase("jobs", day, checks, summary)
