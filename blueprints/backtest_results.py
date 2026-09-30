"""Backtest Results Blueprint.

Publishes the backtest run records kept under `signal_engine/research` as a read-only
page: leaderboard, per-idea run history and run-to-run comparison. Single-user,
session-authed (no /api/v1 exposure - UI only), following the same pattern as
`blueprints/strategy_pnl.py`.
"""

import os

from flask import Blueprint, jsonify, request

from limiter import limiter
from services.backtest_results_service import compare_runs, get_idea_results, list_results
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
