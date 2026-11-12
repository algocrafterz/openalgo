"""Run-again buttons for the Daily Workflow page.

Starts one of a fixed list of close-day steps (signal_engine/ops/actions.py) for a recent day and
reports its progress. The browser sends an action key and a day, never a command. One run at a
time. Session-authed and UI-only; CSRF is the app's global CSRFProtect and this route is not exempt.

The child process is detached and writes its own state file, so a run survives an app restart and
no worker thread waits on it. Children are reaped here the next time a request looks at them.
"""

import os
import subprocess
import sys
import time

from flask import Blueprint, jsonify, request

from limiter import limiter
from signal_engine.ops import actions
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

ops_actions_bp = Blueprint("ops_actions_bp", __name__, url_prefix="/")

OPS_ACTION_READ_LIMIT = os.getenv("OPS_ACTION_READ_LIMIT", "120 per minute")
OPS_ACTION_WRITE_LIMIT = os.getenv("OPS_ACTION_WRITE_LIMIT", "6 per minute")

#: Popen handles of runs this process started, kept only so they can be reaped.
_CHILDREN: dict[str, subprocess.Popen] = {}


def _reap() -> None:
    for run_id, proc in list(_CHILDREN.items()):
        if proc.poll() is not None:
            _CHILDREN.pop(run_id, None)


def catalogue(day: str, *, now=None, folder: str | None = None) -> list[dict]:
    """Every action with whether it can start for this day, and why not when it cannot."""
    out = []
    for spec in actions.ACTIONS.values():
        reason = actions.refusal(spec.key, day, now=now, folder=folder)
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "detail": spec.detail,
                "available": reason is None,
                "reason": reason or "",
            }
        )
    return out


def start(action: str, day: str, *, now=None, folder=None, state_dir=None) -> dict:
    """Start an action. Returns {"run": state} or {"error": message, "status": http code}."""
    _reap()
    reason = actions.refusal(action, day, now=now, folder=folder)
    if reason:
        return {"error": reason, "status": 400}
    busy = actions.running(state_dir)
    if busy:
        return {
            "error": f"'{busy.get('label', 'Another run')}' is still running. Wait for it to finish.",
            "status": 409,
        }
    run_id = f"{int(time.time())}-{action}"
    state_folder = state_dir or actions.STATE_DIR
    os.makedirs(state_folder, exist_ok=True)
    actions.prune(state_folder)
    # Written before the child starts so a second click in the same second sees a run.
    state = {
        "id": run_id,
        "action": action,
        "day": day,
        "label": actions.ACTIONS[action].label,
        "pid": os.getpid(),
        "started": actions.now_ist().isoformat(timespec="seconds"),
    }
    actions.write_state(run_id, state, state_folder)
    log = os.path.join(state_folder, f"{run_id}.log")
    env = {**os.environ, "PYTHONPATH": actions.REPO}
    with open(log, "ab") as log_handle:  # the child gets its own copy of the descriptor
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "signal_engine.ops",
                "run-action",
                action,
                "--day",
                day,
                "--id",
                run_id,
            ],
            cwd=actions.REPO,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    _CHILDREN[run_id] = proc
    state = {**state, "pid": proc.pid}
    actions.write_state(run_id, state, state_folder)
    return {"run": state, "status": 202}


@ops_actions_bp.route("/api/ops/actions", methods=["GET"])
@check_session_validity
@limiter.limit(OPS_ACTION_READ_LIMIT)
def list_actions():
    day = request.args.get("day", "")
    try:
        _reap()
        data = {
            "actions": catalogue(day) if actions.DAY.match(day) else [],
            "run": actions.latest_run(),
        }
        return jsonify({"status": "success", "data": data}), 200
    except Exception:
        logger.exception("Could not read the run-again actions")
        return jsonify({"status": "success", "data": {"actions": [], "run": None}}), 200


@ops_actions_bp.route("/api/ops/run-action", methods=["POST"])
@check_session_validity
@limiter.limit(OPS_ACTION_WRITE_LIMIT)
def run_action():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "The request was not understood."}), 400
    action, day = payload.get("action"), payload.get("day")
    if not isinstance(action, str) or not isinstance(day, str):
        return jsonify({"status": "error", "message": "The request was not understood."}), 400
    try:
        result = start(action, day)
    except Exception:
        logger.exception("Could not start the run-again action")
        return (
            jsonify({"status": "error", "message": "It could not be started. Try again."}),
            500,
        )
    if "error" in result:
        return jsonify({"status": "error", "message": result["error"]}), result["status"]
    return jsonify({"status": "success", "run": result["run"]}), 202
