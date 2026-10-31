"""Daily Workflow Blueprint.

Shows the signal_engine ops phases at /logs/workflow: open-day (09:12), liveness (09:27) and
close-day (15:36), each step's start, end and exit code, the check verdicts, and the reports they
wrote. Read-only over signal_engine/reports/; the phases themselves run from cron
(signal_engine/ops/run_phase.sh, signal_engine/analysis/close_day.sh). Session-authed, UI-only.
"""

import json
import os
import re

from flask import Blueprint, jsonify, request

from limiter import limiter
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


def list_days(folder: str = REPORTS_DIR, limit: int = 30) -> list[str]:
    """Days that have at least one phase status file, newest first."""
    if not os.path.isdir(folder):
        return []
    days = {m.group(2) for name in os.listdir(folder) if (m := _STATUS.match(name))}
    return sorted(days, reverse=True)[:limit]


def _steps(phase: str, day: str, folder: str) -> list[dict] | None:
    path = os.path.join(folder, f"{phase}-{day}-status.tsv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        rows = [line.rstrip("\n").split("\t") for line in handle][1:]
    return [
        {"step": r[0], "started": r[1], "ended": r[2], "exit": int(r[3])}
        for r in rows
        if len(r) == 4 and r[3].lstrip("-").isdigit()
    ]


def _checked(name: str, day: str, folder: str) -> dict | None:
    path = os.path.join(folder, f"{name}-{day}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def close_day_verdict(day: str, folder: str = REPORTS_DIR) -> dict | None:
    """The close-day review's verdict block ({"verdict", "line"}), or None when it has none."""
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


def day_view(day: str, folder: str = REPORTS_DIR) -> dict:
    phases = [{"phase": p, "steps": _steps(p, day, folder)} for p in PHASES]
    checks = {name: data for name in CHECKED if (data := _checked(name, day, folder))}
    reports = (
        sorted(n for n in os.listdir(folder) if day in n and _REPORT.match(n))
        if os.path.isdir(folder)
        else []
    )
    return {
        "day": day,
        "phases": phases,
        "checks": checks,
        "reports": reports,
        "verdict": close_day_verdict(day, folder),
    }


def read_report(name: str, folder: str = REPORTS_DIR) -> str | None:
    """A report's text, only for a whitelisted plain file name inside the reports folder."""
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
            else {"day": "", "phases": [], "checks": {}, "reports": [], "verdict": None}
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
