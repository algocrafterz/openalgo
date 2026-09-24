"""Strategy P&L Blueprint.

Surfaces `database/strategy_book_db.py`'s per-strategy position/P&L book,
otherwise read only by Flow's internal StrategyPnlNode, as a general page.
Single-user, session-authed (no /api/v1 exposure — UI-only), following the
same pattern as `blueprints/strategy_portfolio.py`.
"""

import os

from flask import Blueprint, jsonify, session, url_for
from werkzeug.utils import redirect

from database.auth_db import get_auth_token
from limiter import limiter
from services.strategy_performance_service import get_strategy_performance
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

strategy_pnl_bp = Blueprint("strategy_pnl_bp", __name__, url_prefix="/")

# Reasonable read rate limit — it's a UI endpoint, not a hot path.
STRATEGY_PNL_READ_LIMIT = os.getenv("STRATEGY_PNL_READ_LIMIT", "30 per minute")


@strategy_pnl_bp.route("/api/strategy-pnl", methods=["GET"])
@check_session_validity
@limiter.limit(STRATEGY_PNL_READ_LIMIT)
def strategy_pnl():
    login_username = session["user"]
    auth_token = get_auth_token(login_username)

    if auth_token is None:
        logger.warning(f"No auth token found for user {login_username}")
        return redirect(url_for("auth.logout"))

    broker = session.get("broker")
    if not broker:
        logger.error("Broker not set in session")
        return jsonify({"status": "error", "message": "Broker not set in session"}), 400

    _success, response, status_code = get_strategy_performance(login_username, auth_token, broker)
    return jsonify(response), status_code
