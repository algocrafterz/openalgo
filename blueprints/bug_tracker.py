"""Bug Tracker Blueprint.

Serves the signal_engine bug tracker (signal_engine/bugs/bugs.json) to the page at /logs/bugs, and
takes the page's seven write actions (assign, start, note, fix, verify, reopen, wontfix).

Every write goes through signal_engine.bugs.store, so the transition table, the required reasons,
the exclusive file lock, the atomic save and the history are the CLI's own. The browser sends only
free text (an owner, a note, a fix, evidence, a reason) and never a status or a computed field:
the route picks the status from the action, and `verified` is reachable only through `verify`
with non-empty evidence. Session-authed and UI-only; CSRF is the app's global CSRFProtect and
these routes are not exempt.
"""

import json
import os
import re
from datetime import date, datetime

from flask import Blueprint, jsonify, request

from limiter import limiter
from signal_engine.bugs import store
from signal_engine.timeutils import IST
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

bug_tracker_bp = Blueprint("bug_tracker_bp", __name__, url_prefix="/")

BUG_TRACKER_READ_LIMIT = os.getenv("BUG_TRACKER_READ_LIMIT", "30 per minute")
BUG_TRACKER_WRITE_LIMIT = os.getenv("BUG_TRACKER_WRITE_LIMIT", "30 per minute")

TRACKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "signal_engine",
    "bugs",
    "bugs.json",
)


def read_tracker(path: str | None = None) -> list:
    """Every tracked bug. A missing file means nothing has been recorded yet."""
    path = path or TRACKER_PATH
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    bugs = data.get("bugs") if isinstance(data, dict) else None
    if not isinstance(bugs, list):
        raise ValueError(f"{path} is not a bug tracker file")
    return bugs


#: What a person should do next, by status. Computed here so the page never invents it.
NEXT_ACTION = {
    "open": "Assign and start",
    "in_progress": "Finish fix",
    "fixed": "Verify with evidence",
    "verified": "None",
    "wontfix": "None",
}


def enrich(bug: dict, today: date | str | None = None) -> dict:
    """The stored bug plus owner, age_days, overdue, awaiting_verification and next_action.

    Computed on every read from the stored fields and never written back.
    """
    today = today or datetime.now(IST).date()
    out = dict(bug)
    out["owner"] = str(bug.get("owner") or "")
    try:
        out["age_days"] = store.age_days(bug, today)
        out["overdue"] = store.is_overdue(bug, today)
    except (KeyError, ValueError, TypeError):
        out["age_days"] = None
        out["overdue"] = False
    out["awaiting_verification"] = store.awaiting_verification(bug)
    out["next_action"] = NEXT_ACTION.get(bug.get("status") or "open", "None")
    return out


@bug_tracker_bp.route("/api/bugs", methods=["GET"])
@check_session_validity
@limiter.limit(BUG_TRACKER_READ_LIMIT)
def list_bugs():
    try:
        today = datetime.now(IST).date()
        return jsonify(
            {"status": "success", "data": [enrich(b, today) for b in read_tracker()]}
        ), 200
    except Exception:
        logger.exception("Could not read the bug tracker")
        return jsonify(
            {
                "status": "error",
                "message": "The bug list could not be read. It is being rewritten or is damaged; "
                "refresh in a moment, and if it persists ask Claude to check signal_engine/bugs/bugs.json.",
            }
        ), 500


# ---- write actions -------------------------------------------------------------------------

_ID = re.compile(r"^BUG-\d{1,6}$")
_COMMIT = re.compile(r"^[0-9A-Za-z._/-]{4,80}$")
MAX_SHORT = 80
MAX_TEXT = 4000


class ActionError(ValueError):
    """A request the endpoint refuses. The message is written for the trader."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _error(exc: Exception, status: int):
    return jsonify({"status": "error", "message": str(exc)}), status


def _text(payload: dict, key: str, label: str, *, required: bool, limit: int = MAX_TEXT) -> str:
    value = payload.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ActionError(f"{label} must be text.")
    value = value.strip()
    if required and not value:
        raise ActionError(f"{label} is required.")
    if len(value) > limit:
        raise ActionError(f"{label} is too long (at most {limit} characters).")
    return value


def _current(bug_id: str, path: str) -> dict:
    for bug in read_tracker(path):
        if bug.get("id") == bug_id:
            return bug
    raise ActionError(f"There is no bug {bug_id}.", 404)


def _require_move(bug: dict, target: str, action_label: str) -> None:
    """Refuse early, in plain words, a status change the store's table does not allow."""
    current = bug.get("status")
    if not current:
        raise ActionError(f"{bug['id']} has no status, so it cannot be {action_label} now.", 409)
    if target not in store.TRANSITIONS.get(current, ()):
        raise ActionError(
            f"{bug['id']} is {current.replace('_', ' ')}, so it cannot be {action_label} now. "
            "Refresh the page; it may have changed.",
            409,
        )


def apply_action(bug_id: str, action: str, payload: dict, path: str | None = None) -> dict:
    """Run one write action through the store and return the updated, enriched bug.

    The target status comes from the action alone. The preflight read only gives a plain refusal;
    the store re-checks the transition and the required text under its own lock.
    """
    path = path or TRACKER_PATH
    if not _ID.match(bug_id):
        raise ActionError(f"There is no bug {bug_id}.", 404)
    if action not in ACTIONS:
        raise ActionError("Unknown action.", 404)
    bug = _current(bug_id, path)
    if not bug.get("status"):
        raise ActionError(f"{bug_id} has no status recorded, so it cannot be changed here.", 409)

    if action == "assign":
        owner = _text(payload, "owner", "The owner", required=True, limit=MAX_SHORT)
        if owner == str(bug.get("owner") or "").strip():
            return enrich(bug)  # already assigned: no history line
        updated = store.update(bug_id, owner=owner, note=f"assigned to {owner}", path=path)
    elif action == "note":
        text = _text(payload, "text", "The note", required=True)
        updated = store.note(bug_id, text, path=path)
    elif action == "start":
        _require_move(bug, "in_progress", "started")
        owner = _text(payload, "owner", "The owner", required=False, limit=MAX_SHORT)
        text = _text(payload, "text", "The note", required=False)
        fields = {"owner": owner} if owner else {}
        updated = store.update(bug_id, status="in_progress", note=text, path=path, **fields)
    elif action == "fix":
        _require_move(bug, "fixed", "marked fixed")
        fix = _text(payload, "fix", "The fix description", required=True)
        commit = _text(payload, "commit", "The commit", required=False, limit=80)
        if commit and not _COMMIT.match(commit):
            raise ActionError("The commit must be a commit hash or reference without spaces.")
        explanation = _text(payload, "fix_explanation", "The explanation", required=False)
        fields = {"fix": fix}
        if commit:
            fields["fix_commit"] = commit
        if explanation:
            fields["fix_explanation"] = explanation
        updated = store.update(bug_id, status="fixed", path=path, **fields)
    elif action == "verify":
        _require_move(bug, "verified", "verified")
        evidence = _text(payload, "evidence", "The verification evidence", required=True)
        updated = store.update(bug_id, status="verified", verification=evidence, path=path)
    elif action == "reopen":
        _require_move(bug, "open", "reopened")
        reason = _text(payload, "reason", "The reason", required=True)
        updated = store.update(bug_id, status="open", note=reason, path=path)
    else:  # wontfix
        _require_move(bug, "wontfix", "closed as not fixing")
        reason = _text(payload, "reason", "The reason", required=True)
        updated = store.update(bug_id, status="wontfix", note=reason, path=path)
    return enrich(updated)


ACTIONS = ("assign", "start", "note", "fix", "verify", "reopen", "wontfix")


@bug_tracker_bp.route("/api/bugs/<bug_id>/<action>", methods=["POST"])
@check_session_validity
@limiter.limit(BUG_TRACKER_WRITE_LIMIT)
def bug_action_route(bug_id: str, action: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error(ActionError("The request was not understood."), 400)
    try:
        bug = apply_action(bug_id, action, payload)
        return jsonify({"status": "success", "data": bug}), 200
    except ActionError as exc:
        return _error(exc, exc.status)
    except store.TrackerBusy:
        logger.exception("The bug tracker was locked by another writer")
        return _error(ActionError("The bug tracker is busy. Try again in a moment."), 423)
    except store.TrackerError:
        logger.exception("The bug tracker refused a change")
        return _error(
            ActionError(
                "The tracker did not accept that change. Refresh the page and check the bug.", 409
            ),
            409,
        )
    except Exception:
        logger.exception("Could not update the bug tracker")
        return _error(ActionError("The bug could not be updated. Try again in a moment."), 500)
