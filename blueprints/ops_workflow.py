"""Daily Workflow Blueprint.

Shows the signal_engine ops phases at /logs/workflow: open-day (09:12), liveness (09:27) and
close-day (15:36), each step's start, end and exit code, the check verdicts, and the reports they
wrote. Read-only over signal_engine/reports/; the phases themselves run from cron
(signal_engine/ops/run_phase.sh, signal_engine/analysis/close_day.sh). Session-authed, UI-only.
"""

import json
import os
import re
from datetime import timedelta

from flask import Blueprint, jsonify, request

from limiter import limiter
from signal_engine.ops import model, workflow_status
from signal_engine.ops.model import read_run
from signal_engine.ops.verdict import review_outcome
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

ops_workflow_bp = Blueprint("ops_workflow_bp", __name__, url_prefix="/")

OPS_WORKFLOW_READ_LIMIT = os.getenv("OPS_WORKFLOW_READ_LIMIT", "60 per minute")

REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "signal_engine", "reports"
)

PHASES = ("open-day", "liveness", "close-day")
#: Phase or step reports with a machine-readable verdict next to them.
CHECKED = ("open-day", "liveness", "logs", "value-areas", "jobs")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: A report the page may open: a plain file name in REPORTS_DIR, nothing else.
_REPORT = re.compile(
    r"^[a-z][a-z0-9-]*-\d{4}-\d{2}-\d{2}(-to-\d{4}-\d{2}-\d{2})?(-claude-review)?\.md$"
)
_VERDICT = re.compile(r"```close-day-verdict\s*\n(.*?)```", re.S)
_STATUS = re.compile(r"^(open-day|liveness|close-day)-(\d{4}-\d{2}-\d{2})-status\.tsv$")


def _folder(folder: str | None) -> str:
    """The reports folder, read when called so a changed REPORTS_DIR is honoured."""
    return folder or REPORTS_DIR


def _schedule() -> dict:
    """{"schedule": ...} for the payload, or {} (the key absent) when it cannot be read."""
    try:
        return {"schedule": workflow_status.schedule()}
    except Exception:
        logger.exception("Could not read the workflow schedule")
        return {}


#: Calendar days back from today that list_days considers for trading days without any file.
MISSED_WINDOW_DAYS = 14


def list_days(folder: str | None = None, limit: int = 30, now=None) -> list[str]:
    """Days to show, newest first: every day with a phase status file, plus trading days with none.

    A trading day with no status file at all must stay selectable, or a missed run is invisible
    (its run_status reads missed / not_due). Added days: today when it is a trading day, and past
    trading days in the last MISSED_WINDOW_DAYS calendar days that are not older than the oldest
    day with a file (so a fresh install does not list days before the workflow existed). The
    trading calendar is workflow_status.is_trading_day (NSE holidays, Mon-Fri fallback); `now`
    is injectable for tests. Never raises on a calendar or clock problem.
    """
    folder = _folder(folder)
    days: set[str] = set()
    if os.path.isdir(folder):
        days = {m.group(2) for name in os.listdir(folder) if (m := _STATUS.match(name))}
    try:
        today = (now or model.now_ist()).date()
        oldest = min(days) if days else None
        for back in range(MISSED_WINDOW_DAYS + 1):
            day = (today - timedelta(days=back)).isoformat()
            if day in days or (back > 0 and (oldest is None or day < oldest)):
                continue
            if workflow_status.is_trading_day(day):
                days.add(day)
    except Exception:
        logger.exception("Could not add trading days without status files")
    return sorted(days, reverse=True)[:limit]


def _steps(phase: str, day: str, folder: str) -> list[dict] | None:
    path = os.path.join(folder, f"{phase}-{day}-status.tsv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        rows = [line.rstrip("\n").split("\t") for line in handle][1:]
    # A step run again from the page appends a new row: show the latest result of each step, in
    # the order the steps first ran, and say it was run again.
    latest: dict[str, dict] = {}
    for r in rows:
        if len(r) == 4 and r[3].lstrip("-").isdigit():
            row = {"step": r[0], "started": r[1], "ended": r[2], "exit": int(r[3])}
            if r[0] in latest:
                row["reran"] = True
            latest[r[0]] = row
    return list(latest.values())


def _checked(name: str, day: str, folder: str) -> dict | None:
    path = os.path.join(folder, f"{name}-{day}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, UnicodeDecodeError):  # a damaged report reads as not checked
        logger.warning("Ignoring unreadable check report %s", os.path.basename(path))
        return None


def close_day_verdict(day: str, folder: str | None = None) -> dict | None:
    """The close-day review's verdict block ({"verdict", "line"}), or None when it has none."""
    folder = _folder(folder)
    path = os.path.join(folder, f"eod-{day}-claude-review.md")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as handle:
        blocks = _VERDICT.findall(handle.read())
    try:
        data = json.loads(blocks[-1])
    except (IndexError, ValueError):
        return None
    if data.get("verdict") not in ("GREEN", "AMBER", "RED"):
        return None
    return {"verdict": data["verdict"], "line": str(data.get("line", ""))[:400]}


def _run_summary(phase: str, day: str, folder: str) -> dict:
    """The run manifest state of a phase (legacy or missing for an old day or a missed run)."""
    run = read_run(phase, day, folder)
    return {
        "state": run["state"],
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        "note": run.get("note", ""),
    }


def day_view(day: str, folder: str | None = None, now=None) -> dict:
    folder = _folder(folder)
    phases = [{"phase": p, "steps": _steps(p, day, folder)} for p in PHASES]
    checks = {name: data for name in CHECKED if (data := _checked(name, day, folder))}
    reports = (
        sorted(n for n in os.listdir(folder) if day in n and _REPORT.match(n))
        if os.path.isdir(folder)
        else []
    )
    review = review_outcome(day, folder)
    # The review's own reading wins over the raw block: a GREEN verdict on an incomplete report
    # shows as AMBER, as the Telegram line does.
    verdict = (
        {"verdict": review["verdict"], "line": review["line"]}
        if review["outcome"] == "verdict"
        else None
    )
    return {
        "day": day,
        "phases": phases,
        "checks": checks,
        "reports": reports,
        "verdict": verdict,
        "review": {
            "outcome": review["outcome"],
            "cause": review["cause"],
            "text": review["text"],
            "exit": review["exit"],
            "note": review["note"],
        },
        "runs": {p: _run_summary(p, day, folder) for p in PHASES},
        # Added fields (R4.1): expected-versus-completed runs, recurring failures, last alert.
        "run_status": workflow_status.run_status(day, folder, now),
        "recurrence": workflow_status.recurrence(day, folder),
        "notifications": workflow_status.notifications(folder),
        **_schedule(),
    }


def read_report(name: str, folder: str | None = None) -> str | None:
    """A report's text, only for a whitelisted plain file name inside the reports folder."""
    folder = _folder(folder)
    if not _REPORT.match(name or ""):
        return None
    path = os.path.join(folder, name)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(folder) or not os.path.isfile(
        path
    ):
        return None
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


@ops_workflow_bp.route("/api/ops/workflow", methods=["GET"])
@check_session_validity
@limiter.limit(OPS_WORKFLOW_READ_LIMIT)
def workflow():
    try:
        days = list_days()
        day = request.args.get("day") or (days[0] if days else "")
        if day and not _DAY.match(day):
            return jsonify({"status": "error", "message": "Pick a day from the list."}), 400
        data = (
            day_view(day)
            if day
            else {
                "day": "",
                "phases": [],
                "checks": {},
                "reports": [],
                "verdict": None,
                "notifications": workflow_status.notifications(REPORTS_DIR),
                **_schedule(),
            }
        )
        return jsonify({"status": "success", "days": days, "data": data}), 200
    except Exception:
        logger.exception("Could not read the ops workflow reports")
        return jsonify(
            {
                "status": "error",
                "message": "The workflow status could not be read. Refresh in a moment.",
            }
        ), 500


@ops_workflow_bp.route("/api/ops/report", methods=["GET"])
@check_session_validity
@limiter.limit(OPS_WORKFLOW_READ_LIMIT)
def report():
    text = read_report(request.args.get("name", ""))
    if text is None:
        return jsonify({"status": "error", "message": "That report does not exist."}), 404
    return jsonify({"status": "success", "data": text}), 200
