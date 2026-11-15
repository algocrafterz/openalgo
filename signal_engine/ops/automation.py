"""Unattended upkeep around the daily workflow: an overnight retry and a dead-man check.

Both use only what already exists. The retry runs the same fixed actions as the page's Run again
button (signal_engine/ops/actions.py). The dead-man check reads the run manifest, the status file
and the reports of each phase. It sends one Telegram line when a checkpoint is missing,
incomplete or failed. Neither touches orders, positions, config or code.

    uv run python -m signal_engine.ops retry-failed          08:56 weekdays (installed until the
                                                             08:36 recover line replaces it): redo
                                                             yesterday's failed backfill only
    uv run python -m signal_engine.ops recover               08:36 weekdays: signal_engine/ops/recovery.py
    uv run python -m signal_engine.ops close-day-retry        16:01 weekdays, called by close_day.sh --retry
    uv run python -m signal_engine.ops deadman PHASE         PHASE is morning (09:40) or close
                                                             (16:28, after the 16:01 retry; the
                                                             line installed today runs at 16:20)

The close dead-man judges the day after the retry: a step the retry or the morning recovery
recovered counts as healthy, a step that stays open stays failed or incomplete, and a retry that is
still running makes the day incomplete.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

from signal_engine.ops import actions, jobs, model, verdict
from signal_engine.ops.model import REPORTS
from signal_engine.ops.workflow_status import DUE, MARGIN_MINUTES

DB = os.path.join(actions.REPO, "db", "openalgo.db")
#: Clock-time constants. Cron times live in jobs.CRON_LINES and phase due times in
#: workflow_status.DUE; the two below are derived from or checked against them by
#: test_ops_schedule_constants.py, so no copy can drift alone.
#: The retry must end before the market is near: backfill reads the broker. It is a cut-off, not a
#: cron time: after the 08:36 recover line and before the 09:12 open-day check.
RETRY_BEFORE = "09:10"


def _proposed_time(label: str) -> str:
    """The proposed (last) time of the CRON_LINES entry with this label."""
    return next(spec.times[-1] for spec in jobs.CRON_LINES if spec.label == label)


def is_trading_day(day: str, *, db_path: str | None = None) -> bool:
    """A weekday that is not an NSE holiday in OpenAlgo's own calendar."""
    if datetime.strptime(day, "%Y-%m-%d").weekday() > 4:
        return False
    try:
        con = sqlite3.connect(f"file:{db_path or DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT 1 FROM market_holidays h JOIN market_holiday_exchanges e "
                "ON e.holiday_id = h.id WHERE h.holiday_date = ? AND e.exchange_code = 'NSE' "
                "LIMIT 1",
                (day,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return True  # an unreadable calendar must not silence the check
    return row is None


def _latest_exit(day: str, step: str, folder: str) -> int | None:
    """The newest recorded exit code of a close-day step, or None when it never ran."""
    path = os.path.join(folder, f"close-day-{day}-status.tsv")
    try:
        with open(path, encoding="utf-8") as handle:
            rows = [line.rstrip("\n").split("\t") for line in handle][1:]
    except OSError:
        return None
    codes = [int(r[3]) for r in rows if len(r) == 4 and r[0] == step and r[3].lstrip("-").isdigit()]
    return codes[-1] if codes else None


def _previous_day(today: str, folder: str) -> str | None:
    """The newest earlier day that has a close-day status file, within the retry window."""
    limit = datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=actions.MAX_AGE_DAYS)
    days = sorted(
        (
            name[len("close-day-") : -len("-status.tsv")]
            for name in os.listdir(folder)
            if name.startswith("close-day-") and name.endswith("-status.tsv")
        ),
        reverse=True,
    )
    for day in days:
        if day < today and datetime.strptime(day, "%Y-%m-%d").date() >= limit:
            return day
    return None


def _already_tried(day: str, state_dir: str | None) -> bool:
    folder = state_dir or actions.STATE_DIR
    if not os.path.isdir(folder):
        return False
    for name in os.listdir(folder):
        if name.endswith(".json"):
            state = actions.read_state(name[:-5], folder)
            if state and state.get("auto") and state.get("action") == "backfill":
                if state.get("day") == day:
                    return True
    return False


def retry_failed(
    *, now: datetime | None = None, folder: str | None = None, state_dir: str | None = None
) -> list[str]:
    """Re-run the last session's data download if it failed. At most once per day, and only before
    the market is near. Returns one line per decision for the cron log."""
    now = now or actions.now_ist()
    folder = folder or REPORTS
    today = now.strftime("%Y-%m-%d")
    if not is_trading_day(today):
        return [f"{today} is not a trading day - nothing to retry."]
    if now.strftime("%H:%M") >= RETRY_BEFORE:
        return [f"It is {now.strftime('%H:%M')}, too close to the open - not retrying."]
    day = _previous_day(today, folder) if os.path.isdir(folder) else None
    if day is None:
        return ["No recent close-day record - nothing to retry."]
    code = _latest_exit(day, "backfill", folder)
    if code is None or code == 0:
        return [f"{day}: the data download {'did not run' if code is None else 'finished'}."]
    if _already_tried(day, state_dir):
        return [f"{day}: already retried once - leaving it for you."]
    if actions.running(state_dir):
        return ["Another run is in progress - not retrying now."]
    reason = actions.refusal("backfill", day, now=now, folder=folder)
    if reason:
        return [f"{day}: {reason}"]
    run_id = f"{int(time.time())}-backfill"
    rc = actions.run("backfill", day, run_id, state_dir=state_dir, auto=True)
    return [f"{day}: retried the data download, {'done' if rc == 0 else 'failed again'}."]


#: What each dead-man phase expects: (phase, label as a trader reads it, due HH:MM). A phase is
#: only judged once its due time plus DUE_MARGIN_MINUTES has passed.
EXPECTED = {
    "morning": (
        ("open-day", f"The {DUE['open-day']} readiness check", DUE["open-day"]),
        ("liveness", f"The {DUE['liveness']} after-the-open check", DUE["liveness"]),
    ),
    "close": (("close-day", f"The {DUE['close-day']} end-of-day checks", DUE["close-day"]),),
}
DUE_MARGIN_MINUTES = MARGIN_MINUTES  # one source: workflow_status
#: The close dead-man runs at the proposed time of its cron line (16:28, jobs.CRON_LINES): the
#: afternoon retry ends by 16:25. A retry still running at that time is reported as incomplete.
FINAL_DEADMAN_CLOSE = _proposed_time("deadman close")
#: Reports a phase must leave behind, readable as JSON, besides its status file. close-day also
#: needs the review report (read through verdict.review_outcome).
PHASE_REPORTS = {
    "open-day": ("open-day",),
    "liveness": ("liveness",),
    "close-day": ("logs", "value-areas", "jobs"),
}
HEALTHY, MISSING, INCOMPLETE, FAILED = "healthy", "missing", "incomplete", "failed"
_NEXT = {
    MISSING: "The scheduler may have stopped or the PC started late. Check the machine, then run it by hand.",
    INCOMPLETE: "Open the workflow page, then run the missing step again.",
    FAILED: "Open the workflow page and read the failed step.",
}


def _read_rows(path: str) -> list[list[str]] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return [line.rstrip("\n").split("\t") for line in handle][1:]
    except OSError:
        return None


def _json_ok(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            return isinstance(json.load(handle), dict)
    except (OSError, ValueError):
        return False


def _only_review_timed_out(steps: dict[str, int], day: str, folder: str) -> bool:
    """True when the review is the only step that exited 124 and review_outcome holds a verdict."""
    timed_out = {name for name, code in steps.items() if code == 124}
    return timed_out == {"review"} and verdict.review_outcome(day, folder)["outcome"] == "verdict"


def _later_attempts(
    day: str, folder: str, now: datetime | None
) -> tuple[bool, list[str], list[str]]:
    """What the morning recovery and the 16:01 retry of a close-day run say about it.

    Returns (superseded, failed, incomplete). `superseded` is True when the newest attempt ended
    completed: every step that was open is recovered. The recovery is the later attempt, so it is
    read first. A manifest that does not exist (no retry was needed) says nothing.
    """
    for phase, label in (
        ("close-day-recover", "the morning recovery"),
        ("close-day-retry", "the 16:01 retry"),
    ):
        run = model.read_run(phase, day, folder, now=now)
        state = run["state"]
        if state == model.COMPLETED:
            return True, [], []
        if state == model.RUNNING:
            return False, [], [f"{label} is still running"]
        if state == model.INTERRUPTED:
            return False, [], [f"{label} was interrupted before it finished"]
        if state in (model.FAILED, model.TIMED_OUT):
            note = (run.get("note") or "").strip()
            return (
                False,
                [f"{label} ended as {state.replace('_', ' ')}" + (f" ({note})" if note else "")],
                [],
            )
        if state == model.UNREADABLE:
            return False, [], [f"the record of {label} is unreadable"]
    return False, [], []


def judge_phase(phase: str, day: str, folder: str, now: datetime | None = None) -> dict:
    """Judge one phase's recorded run. Returns {phase, state, reasons}; state is one of
    healthy, missing, incomplete or failed. Reads files only.

    Checks, in order: the run exists (manifest or status file); the manifest is terminal (a run
    whose process is gone or that is older than the maximum run time reads as interrupted); the
    status rows hold every critical step (model.CRITICAL_STEPS) with a good exit; the reports
    exist and parse; the verdict is not RED; for close-day the review gave a usable verdict.
    """
    run = model.read_run(phase, day, folder, now=now)
    rows = _read_rows(os.path.join(folder, f"{phase}-{day}-status.tsv"))
    if run["state"] == model.MISSING and rows is None:
        return {"phase": phase, "state": MISSING, "reasons": ["no record of this run"]}
    failed: list[str] = []
    incomplete: list[str] = []
    state = run["state"]
    steps = verdict.latest_steps(phase, day, folder)
    superseded = False
    if phase == "close-day":
        superseded, later_failed, later_incomplete = _later_attempts(day, folder, now)
        failed += later_failed
        incomplete += later_incomplete
    if superseded and state in (model.FAILED, model.TIMED_OUT, model.INTERRUPTED, model.RUNNING):
        # The retry or the morning recovery finished with every open step recovered. The steps'
        # newest rows and reports are judged below, so the first run's end state is history.
        pass
    elif (
        state == model.TIMED_OUT
        and phase == "close-day"
        and _only_review_timed_out(steps, day, folder)
    ):
        # The review hit its deadline, then a later attempt (the catch-up) wrote a valid verdict.
        # The review is judged below by review_outcome, so the old timeout is not a failure.
        pass
    elif state in (model.FAILED, model.TIMED_OUT):
        failed.append(f"the run ended as {state}")
    elif state in (model.INTERRUPTED, model.RUNNING):
        incomplete.append("the run was interrupted before it finished")
    elif state == model.UNREADABLE:
        incomplete.append("the run record is unreadable")
    if rows is None or not rows:
        incomplete.append("no step was recorded")
    for name in model.CRITICAL_STEPS.get(phase, ()):
        if name not in steps:
            incomplete.append(f"step {name} has no record")
    for name in verdict.failed_critical_steps(phase, day, folder):
        failed.append(f"step {name} failed")
    for report in PHASE_REPORTS.get(phase, ()):
        if not os.path.exists(os.path.join(folder, f"{report}-{day}.json")):
            incomplete.append(f"the {report} report is missing")
        elif not _json_ok(os.path.join(folder, f"{report}-{day}.json")):
            incomplete.append(f"the {report} report cannot be read")
        elif (
            phase in ("open-day", "liveness")
            and model.read_phase(report, day, folder=folder)["verdict"] == "RED"
        ):
            failed.append(f"the {report} check reported RED")
    if phase == "close-day":
        review = verdict.review_outcome(day, folder)
        if review["outcome"] != "verdict":
            incomplete.append(review["text"].split(" Next:")[0].rstrip("."))
        elif review["verdict"] == "RED":
            failed.append("the close-day review verdict is RED")
    if failed:
        return {"phase": phase, "state": FAILED, "reasons": failed + incomplete}
    if incomplete:
        return {"phase": phase, "state": INCOMPLETE, "reasons": incomplete}
    return {"phase": phase, "state": HEALTHY, "reasons": []}


def _due(day: str, hhmm: str, now: datetime) -> bool:
    due = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
    return now >= due + timedelta(minutes=DUE_MARGIN_MINUTES)


def deadman_report(
    kind: str, day: str, *, folder: str | None = None, now: datetime | None = None
) -> list[dict]:
    """One judgement per expected phase that is due, each with a `label`. Empty on a non-trading day."""
    folder = folder or REPORTS
    now = now or actions.now_ist()
    if kind not in EXPECTED or not is_trading_day(day):
        return []
    out = []
    for phase, label, hhmm in EXPECTED[kind]:
        if not _due(day, hhmm, now):
            continue
        out.append({**judge_phase(phase, day, folder, now), "label": label})
    return out


_WORST = (FAILED, MISSING, INCOMPLETE)


def worst_state(report: list[dict]) -> str:
    """failed, missing, incomplete or healthy."""
    states = {r["state"] for r in report}
    return next((s for s in _WORST if s in states), HEALTHY)


def deadman(
    kind: str, day: str, *, folder: str | None = None, now: datetime | None = None
) -> str | None:
    """The alert text when a due checkpoint is missing, incomplete or failed, else None."""
    report = [r for r in deadman_report(kind, day, folder=folder, now=now) if r["state"] != HEALTHY]
    if not report:
        return None
    parts = []
    for r in report:
        if r["state"] == MISSING:
            parts.append(f"{r['label']} did not run today (missing).")
        elif r["state"] == INCOMPLETE:
            parts.append(f"{r['label']} did not finish (incomplete): {'; '.join(r['reasons'])}.")
        else:
            parts.append(f"{r['label']} failed: {'; '.join(r['reasons'])}.")
    nxt = " ".join(dict.fromkeys(_NEXT[r["state"]] for r in report))
    return f"{' '.join(parts)} {nxt} Details: /logs/workflow"
