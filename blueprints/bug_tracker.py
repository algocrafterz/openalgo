"""Bug Tracker Blueprint.

Serves the signal_engine bug tracker (signal_engine/bugs/bugs.json) to the page at
/logs/bugs. Read-only: entries are written by the tracker CLI
(`uv run python -m signal_engine.bugs`), so validation and history live in one place.
Session-authed, UI-only, same pattern as blueprints/strategy_pnl.py.
"""

import json
import os

from flask import Blueprint, jsonify

from limiter import limiter
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

bug_tracker_bp = Blueprint("bug_tracker_bp", __name__, url_prefix="/")

BUG_TRACKER_READ_LIMIT = os.getenv("BUG_TRACKER_READ_LIMIT", "30 per minute")

TRACKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "signal_engine",
    "bugs",
    "bugs.json",
)


def read_tracker(path: str = TRACKER_PATH) -> list:
    """Every tracked bug. A missing file means nothing has been recorded yet."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    bugs = data.get("bugs") if isinstance(data, dict) else None
    if not isinstance(bugs, list):
        raise ValueError(f"{path} is not a bug tracker file")
    return bugs


@bug_tracker_bp.route("/api/bugs", methods=["GET"])
@check_session_validity
@limiter.limit(BUG_TRACKER_READ_LIMIT)
def list_bugs():
    try:
        return jsonify({"status": "success", "data": read_tracker()}), 200
    except Exception:
        logger.exception("Could not read the bug tracker")
        return jsonify(
            {
                "status": "error",
                "message": "The bug list could not be read. It is being rewritten or is damaged; "
                "refresh in a moment, and if it persists ask Claude to check signal_engine/bugs/bugs.json.",
            }
        ), 500
