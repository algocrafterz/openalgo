"""Close-day step `jobs`: did every scheduled job run today, and succeed?

Owns the schedule audit. Sources: Windows Task Scheduler (process lifecycle), the crontab (the
three ops phases) and each phase's step status file.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess

from signal_engine.ops.model import FAIL, OK, REPORTS, WARN, Check, write_phase

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
            ["schtasks.exe", "/query", "/fo", "csv", "/v"],
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


def _phase_checks(day: str) -> list[Check]:
    checks = []
    for phase in PHASE_FILES:
        path = os.path.join(REPORTS, f"{phase}-{day}-status.tsv")
        if not os.path.exists(path):
            status = WARN if phase == "close-day" else FAIL
            checks.append(Check(f"phase {phase}", status, "no status file today"))
            continue
        with open(path, encoding="utf-8") as handle:
            steps = [line.rstrip("\n").split("\t") for line in handle][1:]
        bad = [s for s in steps if len(s) == 4 and s[3] != "0"]
        evidence = ", ".join(f"{s[0]} {s[1]}-{s[2]} exit {s[3]}" for s in steps if len(s) == 4)
        checks.append(Check(f"phase {phase}", FAIL if bad else OK, evidence or "no steps recorded"))
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
