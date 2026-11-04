"""Close-day step `jobs`: did every scheduled job run today, and succeed?

Owns the schedule audit. Sources: Windows Task Scheduler (process lifecycle), the crontab (the
three ops phases) and each phase's step status file.
"""

from __future__ import annotations

import csv
import datetime
import io
import os
import shutil
import subprocess

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
CRON_PHASES = (
    "signal_engine/ops/run_phase.sh open-day",
    "signal_engine/ops/run_phase.sh liveness",
    "signal_engine/analysis/close_day.sh",
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
        bad = [s for s in failed if s not in reported]
        evidence = ", ".join(f"{s[0]} {s[1]}-{s[2]} exit {s[3]}" for s in steps)
        if bad:
            status = FAIL
        elif reported:
            status = WARN
            evidence += "; it ran and reported RED (see that day's report)"
        else:
            status = OK
        checks.append(Check(f"phase {phase}", status, evidence or "no steps recorded"))
    return checks


def _cron_check() -> Check:
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return Check("crontab", WARN, "crontab unreadable")
    missing = [p for p in CRON_PHASES if p not in out]
    return Check(
        "crontab",
        FAIL if missing else OK,
        f"missing: {missing}" if missing else "3 ops phases scheduled",
    )


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
