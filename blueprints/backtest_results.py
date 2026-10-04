"""Backtest Results Blueprint.

Publishes the backtest run records kept under `signal_engine/research` as a read-only
page: leaderboard, per-idea run history, run-to-run comparison and run export. Single-user,
session-authed (no /api/v1 exposure - UI only), following the same pattern as
`blueprints/strategy_pnl.py`.
"""

import os
import re

from flask import Blueprint, Response, jsonify, request, send_file

from limiter import limiter
from services.backtest_results_service import (
    compare_runs,
    export_run,
    get_idea_results,
    get_run_detail,
    get_run_trades,
    get_trade_lineage,
    list_results,
)
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

backtest_results_bp = Blueprint("backtest_results_bp", __name__, url_prefix="/")

# It is a UI read of small files, not a hot path.
BACKTEST_RESULTS_READ_LIMIT = os.getenv("BACKTEST_RESULTS_READ_LIMIT", "30 per minute")


@backtest_results_bp.route("/api/backtest-results", methods=["GET"])
@check_session_validity
@limiter.limit(BACKTEST_RESULTS_READ_LIMIT)
def backtest_results_list():
    _success, response, status_code = list_results()
    return jsonify(response), status_code


@backtest_results_bp.route("/api/backtest-results/<idea_id>", methods=["GET"])
@check_session_validity
@limiter.limit(BACKTEST_RESULTS_READ_LIMIT)
def backtest_results_idea(idea_id):
    _success, response, status_code = get_idea_results(idea_id)
    return jsonify(response), status_code


@backtest_results_bp.route("/api/backtest-results/<idea_id>/compare", methods=["GET"])
@check_session_validity
@limiter.limit(BACKTEST_RESULTS_READ_LIMIT)
def backtest_results_compare(idea_id):
    """Two runs of one idea: `?a=<run_id>&b=<run_id>`."""
    run_a, run_b = request.args.get("a"), request.args.get("b")
    if not run_a or not run_b:
        return jsonify({"status": "error", "message": "Both run ids (a and b) are required"}), 400
    _success, response, status_code = compare_runs(idea_id, run_a, run_b)
    return jsonify(response), status_code


@backtest_results_bp.route("/api/backtest-results/<idea_id>/runs/<run_id>", methods=["GET"])
@check_session_validity
@limiter.limit(BACKTEST_RESULTS_READ_LIMIT)
def backtest_results_run_detail(idea_id, run_id):
    """One run's folder contents: verdict, scope, reproducibility, assumptions."""
    _success, response, status_code = get_run_detail(idea_id, run_id)
    return jsonify(response), status_code


@backtest_results_bp.route("/api/backtest-results/<idea_id>/runs/<run_id>/export", methods=["GET"])
@check_session_validity
@limiter.limit(BACKTEST_RESULTS_READ_LIMIT)
def backtest_results_export(idea_id, run_id):
    """Download one run: `?format=json|metrics|series|trades` (default json)."""
    fmt = request.args.get("format", "json")
    success, result, status_code = export_run(idea_id, run_id, fmt)
    if not success:
        return jsonify(result), status_code
    if result.path is not None:
        return send_file(
            result.path,
            mimetype=result.mimetype,
            as_attachment=True,
            download_name=result.filename,
        )
    response = Response(result.body, mimetype=result.mimetype)
    response.headers["Content-Disposition"] = f'attachment; filename="{result.filename}"'
    return response


_BOOLEANS = {"1": True, "true": True, "0": False, "false": False}


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"-?\d{1,9}", raw.strip()):
        raise ValueError(name)
    return int(raw)


@backtest_results_bp.route("/api/backtest-results/<idea_id>/runs/<run_id>/trades", methods=["GET"])
@check_session_validity
@limiter.limit(BACKTEST_RESULTS_READ_LIMIT)
def backtest_results_trades(idea_id, run_id):
    """One page of a run's trades: `?subset&symbol&reason&sort&desc&limit&offset`."""
    try:
        limit = _int_arg("limit", 100)
        offset = _int_arg("offset", 0)
        desc = _BOOLEANS[request.args.get("desc", "false").strip().lower()]
    except (ValueError, KeyError):
        return jsonify({"status": "error", "message": "Invalid query parameter"}), 400
    try:
        _success, response, status_code = get_run_trades(
            idea_id,
            run_id,
            subset=request.args.get("subset", "all"),
            symbol=request.args.get("symbol") or None,
            reason=request.args.get("reason") or None,
            sort=request.args.get("sort", "entry_time"),
            desc=desc,
            limit=limit,
            offset=offset,
        )
    except Exception:
        logger.exception("Backtest trades request failed")
        return jsonify({"status": "error", "message": "Could not read the trades"}), 500
    return jsonify(response), status_code


@backtest_results_bp.route(
    "/api/backtest-results/<idea_id>/runs/<run_id>/trades/<trade_id>/lineage", methods=["GET"]
)
@check_session_validity
@limiter.limit(BACKTEST_RESULTS_READ_LIMIT)
def backtest_results_trade_lineage(idea_id, run_id, trade_id):
    """The signal, order and fills behind one trade."""
    try:
        _success, response, status_code = get_trade_lineage(idea_id, run_id, trade_id)
    except Exception:
        logger.exception("Backtest lineage request failed")
        return jsonify({"status": "error", "message": "Could not read the lineage"}), 500
    return jsonify(response), status_code
