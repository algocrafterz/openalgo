"""Report a Daily Workflow finding as a bug.

Backs the "Report as bug" action on /logs/workflow. It writes only through
signal_engine.bugs.store, so validation, the exclusive file lock, the atomic save and the history
are the CLI's own. This module adds three things the store does not have: it looks the finding up
in the day's stored reports (the browser never supplies the evidence), it gives each finding a
stable key so reporting it twice finds the same bug, and it keeps the original context in the bug.

Session-authed and UI-only. CSRF is the app's global CSRFProtect; this route is not exempt.
"""

import os
import re

from flask import Blueprint, jsonify, request

from blueprints.bug_tracker import TRACKER_PATH
from blueprints.ops_workflow import (
    _DAY,
    PHASES,
    REPORTS_DIR,
    _checked,
    _steps,
)
from limiter import limiter
from signal_engine.bugs import store
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

ops_findings_bp = Blueprint("ops_findings_bp", __name__, url_prefix="/")

OPS_FINDING_READ_LIMIT = os.getenv("OPS_FINDING_READ_LIMIT", "60 per minute")
OPS_FINDING_WRITE_LIMIT = os.getenv("OPS_FINDING_WRITE_LIMIT", "20 per minute")

MARKER = "workflow-finding:"

#: Which check results each checkpoint writes. `step` and `execution` are not check results.
PHASE_GROUPS = {
    "open-day": ("open-day", "step"),
    "liveness": ("liveness", "step"),
    "close-day": ("logs", "value-areas", "jobs", "step", "execution"),
}
CHECK_GROUPS = ("open-day", "liveness", "logs", "value-areas", "jobs")
#: Phases whose `check` step exits 1 by design when its own verdict is RED.
RED_EXIT_STEPS = {
    ("open-day", "check"): "open-day",
    ("liveness", "check"): "liveness",
    ("close-day", "logs"): "logs",
    ("close-day", "jobs"): "jobs",
    ("close-day", "value_areas"): "value-areas",
}
#: Report shown as the source of a finding, by group.
GROUP_REPORT = {
    "open-day": "open-day",
    "liveness": "liveness",
    "logs": "logs",
    "value-areas": "value-areas",
    "jobs": "jobs",
    "execution": "execution-health",
}
AREAS = ("signal_engine", "openalgo-core", "sandbox", "broker", "data", "strategy", "ops")
MAX_IMPACT = 600
MAX_PLAIN = 400
PHASE_LABEL = {"open-day": "Open", "liveness": "After the open", "close-day": "Close"}

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FIRST = re.compile(r"^first \d{2}:\d{2}:\d{2};\s*")
_ROW = re.compile(r"^\|\s*Metric\s*\|", re.I)


class FindingError(ValueError):
    """A request the endpoint refuses. The message is written for the trader."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _metric_rows(day: str, folder: str) -> list[dict]:
    """Rows of the execution-health table that the report itself marks WATCH or REGRESSION."""
    path = os.path.join(folder, f"execution-health-{day}.md")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    head = next((i for i, ln in enumerate(lines) if _ROW.match(ln)), None)
    if head is None:
        return []
    cols = [c.strip().lower() for c in lines[head].strip().strip("|").split("|")]
    try:
        i_metric, i_today, i_status = (
            cols.index("metric"),
            cols.index("today"),
            cols.index("status"),
        )
    except ValueError:
        return []
    out = []
    for line in lines[head + 1 :]:
        if not line.strip().startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) <= max(i_metric, i_today, i_status) or set(cells[0]) <= {"-", " ", ":"}:
            continue
        if cells[i_status] in ("WATCH", "REGRESSION"):
            out.append(
                {"name": cells[i_metric], "status": cells[i_status], "today": cells[i_today]}
            )
    return out


def _step_failed(day_checks: dict, phase: str, step: dict) -> bool:
    """A non-zero step is a failure unless it is a check command that reported its own RED result."""
    if step["exit"] == 0:
        return False
    key = RED_EXIT_STEPS.get((phase, step["step"]))
    red = step["exit"] == 1 and key and (day_checks.get(key) or {}).get("verdict") == "RED"
    return not red


def enumerate_findings(day: str, folder: str | None = None) -> list[dict]:
    """Every finding the page can show for a day, from the stored results only."""
    folder = folder or REPORTS_DIR
    found: list[dict] = []
    checks = {g: _checked(g, day, folder) for g in CHECK_GROUPS}
    for phase in PHASES:
        steps = _steps(phase, day, folder) or []
        checked_at = steps[0]["started"] if steps else ""
        for group in PHASE_GROUPS[phase]:
            if group in CHECK_GROUPS:
                for c in (checks.get(group) or {}).get("checks", []):
                    if c.get("status") == "OK":
                        continue
                    found.append(
                        {
                            "phase": phase,
                            "group": group,
                            "name": c["name"],
                            "status": c["status"],
                            "evidence": str(c.get("evidence", "")),
                            "plain": str(c.get("plain", "")),
                            "checked_at": checked_at,
                        }
                    )
        for s in steps:
            if _step_failed({k: v for k, v in checks.items() if v}, phase, s):
                found.append(
                    {
                        "phase": phase,
                        "group": "step",
                        "name": s["step"],
                        "status": "FAIL",
                        "evidence": f"step {s['step']} exited with {s['exit']} at {s['started']}",
                        "plain": f"The {s['step']} step did not finish cleanly.",
                        "checked_at": s["started"],
                    }
                )
    for row in _metric_rows(day, folder):
        found.append(
            {
                "phase": "close-day",
                "group": "execution",
                "name": row["name"],
                "status": row["status"],
                "evidence": f"{row['name']}: today {row['today']}, report status {row['status']}",
                "plain": f"{row['name']} was {row['today']} ({row['status'].lower()}).",
                "checked_at": (_steps("close-day", day, folder) or [{}])[0].get("started", ""),
            }
        )
    return found


def finding_key(f: dict) -> str:
    """Stable across days: dates are masked, and a log group is named by its error, not its count."""
    text = _FIRST.sub("", f["evidence"]) if f["group"] == "logs" else f["name"]
    text = _DATE.sub("<date>", text.lower())
    return f"{f['phase']}:{f['group']}:{' '.join(text.split())[:160]}"


def finding_title(f: dict) -> str:
    detail = _FIRST.sub("", f["evidence"])[:100] if f["group"] == "logs" else f["name"]
    return f"{f['plain'].strip() or f['name']} ({detail})"[:240]


def _find(day: str, phase: str, group: str, name: str, folder: str | None) -> dict:
    if not _DAY.match(day or ""):
        raise FindingError("Pick a valid day.")
    if phase not in PHASES or group not in PHASE_GROUPS.get(phase, ()):
        raise FindingError("That finding does not belong to this checkpoint.")
    for f in enumerate_findings(day, folder):
        if f["phase"] == phase and f["group"] == group and f["name"] == name:
            return f
    raise FindingError(
        "That finding is not in this day's recorded results, so it was not reported."
    )


def _clean(payload: dict) -> dict:
    """Validate everything the browser sends. Unknown keys are ignored, never stored."""

    def text(name: str, limit: int, required: bool = False) -> str:
        value = payload.get(name, "")
        if not isinstance(value, str):
            raise FindingError("The form has an invalid value.")
        value = " ".join(value.split()) if name != "impact" else value.strip()
        if required and not value:
            raise FindingError("Describe the trading impact before reporting.")
        if len(value) > limit:
            raise FindingError(f"Keep {name} under {limit} characters.")
        return value

    clean = {
        "day": text("day", 10, True),
        "phase": text("phase", 20, True),
        "group": text("group", 20, True),
        "name": text("name", 200, True),
        "impact": text("impact", MAX_IMPACT, True),
        "plain": text("plain", MAX_PLAIN),
        "severity": payload.get("severity"),
        "kind": payload.get("kind"),
        "area": payload.get("area"),
    }
    if clean["severity"] not in store.SEVERITIES:
        raise FindingError("Choose a risk level.")
    if payload.get("severity_confirmed") is not True:
        raise FindingError(
            "Confirm the risk level before reporting. It is not taken from the finding."
        )
    if clean["kind"] not in store.KINDS:
        raise FindingError("Choose what kind of problem this is.")
    if clean["area"] not in AREAS:
        raise FindingError("Choose the component this belongs to.")
    return clean


def _tracking(bugs: list[dict], key: str) -> dict | None:
    """The bug that already tracks a finding, by its source key. Titles are not used for links."""
    marker = MARKER + key
    return next((b for b in bugs if marker in b.get("sources", [])), None)


def report_finding(payload: dict, *, path: str | None = None, folder: str | None = None) -> dict:
    """Create the bug, or record the day on the bug that already tracks this finding.

    Returns {"created": bool, "recorded": bool, "bug": {...}}. `recorded` is False when the day was
    already on the bug, so a repeated click changes nothing. The decision and the write happen in
    store.report() under one lock, so concurrent requests and CLI writers cannot both act on a
    stale view. Raises store.AmbiguousMatch (nothing written) and store.TrackerBusy (try again).
    """
    path = path or TRACKER_PATH
    folder = folder or REPORTS_DIR
    clean = _clean(payload)
    finding = _find(clean["day"], clean["phase"], clean["group"], clean["name"], folder)
    key = finding_key(finding)
    day = clean["day"]

    origin = (
        f"Reported from Daily Workflow: {PHASE_LABEL[finding['phase']]} checkpoint on {day}"
        f"{' at ' + finding['checked_at'] if finding['checked_at'] else ''}; "
        f"recorded status {finding['status']}. {finding['name']}: {finding['evidence']}"
    )
    report = GROUP_REPORT.get(finding["group"])
    sources = [MARKER + key]
    if report and os.path.exists(os.path.join(folder, f"{report}-{day}.md")):
        sources.append(f"signal_engine/reports/{report}-{day}.md")

    return store.report(
        source_key=MARKER + key,
        title=finding_title(finding),
        kind=clean["kind"],
        severity=clean["severity"],
        area=clean["area"],
        day=day,
        impact=clean["impact"],
        evidence=[origin],
        sources=sources,
        plain=clean["plain"] or finding["plain"],
        path=path,
    )


def links_for_day(day: str, *, path: str | None = None, folder: str | None = None) -> list[dict]:
    """Findings of the day that already have a bug, matched by the same key the report uses."""
    if not _DAY.match(day or ""):
        raise FindingError("Pick a valid day.")
    bugs = store.load(path or TRACKER_PATH)["bugs"]
    out = []
    for f in enumerate_findings(day, folder):
        bug = _tracking(bugs, finding_key(f))
        if bug:
            out.append(
                {
                    "phase": f["phase"],
                    "group": f["group"],
                    "name": f["name"],
                    "bug_id": bug["id"],
                    "status": bug["status"],
                }
            )
    return out


def _error(exc: Exception, status: int):
    return jsonify({"status": "error", "message": str(exc)}), status


@ops_findings_bp.route("/api/ops/report-finding", methods=["POST"])
@check_session_validity
@limiter.limit(OPS_FINDING_WRITE_LIMIT)
def report_finding_route():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error(FindingError("The request was not understood."), 400)
    try:
        result = report_finding(payload)
        return jsonify({"status": "success", **result}), 200 if not result["created"] else 201
    except FindingError as exc:
        return _error(exc, exc.status)
    except store.AmbiguousMatch as exc:
        ids = ", ".join(exc.candidates)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": (
                        f"{ids} may already cover this finding, but it is not clearly the same "
                        "problem, so nothing was recorded. Open the Bug Tracker to check, or "
                        "choose the same type and component as that bug."
                    ),
                    "candidates": exc.candidates,
                }
            ),
            409,
        )
    except store.TrackerBusy:
        logger.exception("The bug tracker was locked by another writer")
        return _error(FindingError("The bug tracker is busy. Try again in a moment."), 503)
    except store.TrackerError:
        logger.exception("The bug tracker refused a Daily Workflow finding")
        return _error(
            FindingError("The bug could not be recorded. Check the fields and try again."), 400
        )
    except Exception:
        logger.exception("Could not record a Daily Workflow finding as a bug")
        return _error(FindingError("The bug could not be recorded. Try again in a moment."), 500)


@ops_findings_bp.route("/api/ops/finding-links", methods=["GET"])
@check_session_validity
@limiter.limit(OPS_FINDING_READ_LIMIT)
def finding_links_route():
    try:
        return jsonify(
            {"status": "success", "data": links_for_day(request.args.get("day", ""))}
        ), 200
    except FindingError as exc:
        return _error(exc, exc.status)
    except Exception:
        logger.exception("Could not read the bug links for a workflow day")
        return jsonify({"status": "success", "data": []}), 200
