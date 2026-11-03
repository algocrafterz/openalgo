"""Strategy P&L Blueprint.

Surfaces `database/strategy_book_db.py`'s per-strategy position/P&L book,
otherwise read only by Flow's internal StrategyPnlNode, as a general page.
Single-user, session-authed (no /api/v1 exposure — UI-only), following the
same pattern as `blueprints/strategy_portfolio.py`.
"""

import csv
import hashlib
import io
import os

from flask import Blueprint, Response, jsonify, request, session, url_for
from werkzeug.utils import redirect

from database.auth_db import get_api_key_for_tradingview, get_auth_token
from database.settings_db import get_analyze_mode
from limiter import limiter
from services.strategy_book_reconciler import get_data_health, schedule_reconcile
from services.strategy_daily_performance_service import (
    get_daily_performance,
    get_strategy_comparison,
    get_trades_for_export,
)
from services.strategy_performance_service import get_strategy_performance
from utils.logging import get_logger
from utils.session import check_session_validity
from utils.single_flight import SingleFlight

logger = get_logger(__name__)

strategy_pnl_bp = Blueprint("strategy_pnl_bp", __name__, url_prefix="/")

# Reasonable read rate limit — it's a UI endpoint, not a hot path.
STRATEGY_PNL_READ_LIMIT = os.getenv("STRATEGY_PNL_READ_LIMIT", "30 per minute")


def _with_data_health(response: dict, status_code: int) -> dict:
    """Verify the strategy book against the broker in the background and attach
    the last verdict, so the page can warn when its figures may be stale.

    Best-effort: a failure here must never break the page it decorates.
    """
    if status_code >= 400 or not isinstance(response, dict) or response.get("status") != "success":
        return response
    try:
        username = session["user"]
        mode = "analyze" if get_analyze_mode() else "live"
        schedule_reconcile(
            mode,
            {
                "auth_token": get_auth_token(username),
                "broker": session.get("broker"),
                "api_key": get_api_key_for_tradingview(username),
            },
        )
        return {**response, "data_health": get_data_health(mode)}
    except Exception:
        logger.exception("Could not attach strategy book data health")
        return response


# The P&L page refreshes every 30 s, and every tab that has it open refreshes on its own schedule.
# Each read costs a broker position-book call, so reads that overlap (or come back within the
# window) share one result. Keyed by user and broker; the broker token enters the key as a hash.
_PNL_READ_TTL_S = 3.0
_pnl_flights = SingleFlight(join_window=5.0, wait_timeout=60.0)


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

    token_tag = hashlib.sha256(str(auth_token).encode()).hexdigest()[:12]
    key = ("performance", login_username, broker, token_tag)
    _success, response, status_code = _pnl_flights.do(
        key,
        lambda: get_strategy_performance(login_username, auth_token, broker),
        ttl=_PNL_READ_TTL_S,
    )
    return jsonify(_with_data_health(response, status_code)), status_code


_MAX_FILTER_NAMES = 100
_MAX_NAME_LENGTH = 64


def _names(key: str) -> list[str]:
    """Repeated query values: whitespace stripped, empties dropped, duplicates
    removed with the first order kept."""
    seen: dict[str, None] = {}
    for raw in request.args.getlist(key):
        name = raw.strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def _strategy_filter():
    """Read the strategy filter from the query string.

    Returns (strategy, include, exclude, None) or (None, None, None, error
    response). The first included name goes in `strategy` and the rest in
    `include`, so a plain `?strategy=ORB` reaches the service as before.
    """
    included = _names("strategy")
    excluded = _names("exclude")
    if len(included) + len(excluded) > _MAX_FILTER_NAMES:
        message = (
            f"Too many strategies selected. Choose at most {_MAX_FILTER_NAMES} "
            "strategies to include or exclude."
        )
    elif any(len(name) > _MAX_NAME_LENGTH for name in included + excluded):
        message = (
            f"A strategy name is too long. Names can have at most {_MAX_NAME_LENGTH} characters."
        )
    else:
        strategy = included[0] if included else None
        return strategy, included[1:], excluded, None
    return None, None, None, (jsonify({"status": "error", "message": message}), 400)


@strategy_pnl_bp.route("/api/strategy-pnl/daily", methods=["GET"])
@check_session_validity
@limiter.limit(STRATEGY_PNL_READ_LIMIT)
def strategy_pnl_daily():
    """Day-by-day metrics, daily P&L series and equity curve for the chosen
    strategies (repeat `strategy` to include several, `exclude` to leave some
    out), or the pooled portfolio if neither is given.
    `period=custom` takes `start` and `end` as YYYY-MM-DD."""
    strategy, include, exclude, error = _strategy_filter()
    if error:
        return error
    period = request.args.get("period", "30d")
    start = request.args.get("start")
    end = request.args.get("end")
    key = (
        "daily",
        session["user"],
        tuple(sorted(([strategy] if strategy else []) + include)),
        tuple(sorted(exclude)),
        period,
        start,
        end,
    )
    _success, response, status_code = _pnl_flights.do(
        key,
        lambda: get_daily_performance(
            strategy, period, start=start, end=end, include=include, exclude=exclude
        ),
        ttl=_PNL_READ_TTL_S,
    )
    return jsonify(_with_data_health(response, status_code)), status_code


@strategy_pnl_bp.route("/api/strategy-pnl/compare", methods=["GET"])
@check_session_validity
@limiter.limit(STRATEGY_PNL_READ_LIMIT)
def strategy_pnl_compare():
    """Condensed metrics for the chosen strategies in the current mode (all of
    them when no filter is given), side by side - the bird's-eye-view
    comparison table."""
    strategy, include, exclude, error = _strategy_filter()
    if error:
        return error
    period = request.args.get("period", "30d")
    names = ([strategy] if strategy else []) + include
    _success, response, status_code = get_strategy_comparison(
        period,
        start=request.args.get("start"),
        end=request.args.get("end"),
        include=names,
        exclude=exclude,
    )
    return jsonify(_with_data_health(response, status_code)), status_code


EXPORT_COLUMNS = (
    "trade_date",
    "opened_at",
    "closed_at",
    "hold_min",
    "entry_hour",
    "weekday",
    "mode",
    "strategy",
    "broker",
    "symbol",
    "exchange",
    "product",
    "direction",
    "closed_quantity",
    "entry_price",
    "exit_price",
    "sl_price",
    "realized_pnl",
    "est_cost",
    "net_pnl",
    "return_pct",
    "r_multiple",
    "entry_slippage",
    "mfe",
    "mae",
)
_EXPORT_SOURCE = {"est_cost": "cost"}
_NUMERIC_COLUMNS = {
    "hold_min",
    "entry_hour",
    "closed_quantity",
    "entry_price",
    "exit_price",
    "sl_price",
    "realized_pnl",
    "est_cost",
    "net_pnl",
    "return_pct",
    "r_multiple",
    "entry_slippage",
    "mfe",
    "mae",
}


def _csv_cell(column: str, value):
    """None becomes empty; text starting with = + - @ gets a leading quote so a
    spreadsheet cannot run it as a formula. Numbers are left untouched."""
    if value is None:
        return ""
    if column in _NUMERIC_COLUMNS:
        return value
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@strategy_pnl_bp.route("/api/strategy-pnl/trades/export", methods=["GET"])
@check_session_validity
@limiter.limit(STRATEGY_PNL_READ_LIMIT)
def strategy_pnl_trades_export():
    """Enriched closed trades for the chosen range as a CSV download, one row per
    closed trade, oldest close first.

    Columns (money in INR, durations in minutes; empty means unknown):
      trade_date       IST session date, YYYY-MM-DD
      opened_at        entry time, ISO 8601 with +05:30 offset
      closed_at        exit time, ISO 8601 with +05:30 offset
      hold_min         minutes between opened_at and closed_at
      entry_hour       IST hour of the entry (0-23)
      weekday          Mon..Sun of trade_date
      mode             live or analyze
      strategy, broker, symbol, exchange, product
      direction        LONG or SHORT
      closed_quantity  shares/units closed
      entry_price, exit_price   average prices per unit, INR
      sl_price         the matched signal stop price used for R
      realized_pnl     gross P&L, INR
      est_cost         estimated charges, INR
      net_pnl          realized_pnl minus est_cost
      return_pct       realized_pnl / (entry_price * closed_quantity) * 100
      r_multiple       realized_pnl / (signal stop distance * quantity)
      entry_slippage   fill vs signal entry, INR (positive = worse than the alert)
      mfe, mae         best and worst open excursion in INR, an upper bound
                       from 1-minute bars; blank when a range exceeds 2000 trades
    """
    strategy, include, exclude, error = _strategy_filter()
    if error:
        return error
    period = request.args.get("period", "30d")
    success, body, status_code = get_trades_for_export(
        strategy,
        period,
        start=request.args.get("start"),
        end=request.args.get("end"),
        include=include,
        exclude=exclude,
    )
    if not success:
        return jsonify(body), status_code
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for trade in body["trades"]:
        writer.writerow([_csv_cell(c, trade.get(_EXPORT_SOURCE.get(c, c))) for c in EXPORT_COLUMNS])
    filename = f"strategy-trades-{body['start_date']}_{body['end_date']}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
