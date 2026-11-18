"""Discretionary P&L Blueprint (Stage 3a, read-only JSON API for the Performance page).

A separate blueprint, so the Live / Paper routes in ``blueprints/strategy_pnl.py`` and their
responses are untouched. Session-authed, GET only (no CSRF exposure), rate limited and
single-flighted like strategy_pnl. Every route except /status answers 404 with code
``feature_disabled`` unless BOTH DISCRETIONARY_PERFORMANCE_UI_ENABLED and
DISCRETIONARY_LEDGER_ENABLED are truthy (1/true/yes/on), read on every request.

Nothing here writes ledger_fills, reconciles or calls a broker. The only broker call is the
quote fetch of /summary?quotes=1.
"""

import os
from datetime import date

from flask import Blueprint, jsonify, request, session

from limiter import limiter
from services import discretionary_pnl_service as disc
from utils.logging import get_logger
from utils.session import check_session_validity
from utils.single_flight import SingleFlight

logger = get_logger(__name__)

discretionary_pnl_bp = Blueprint("discretionary_pnl_bp", __name__, url_prefix="/")

DISCRETIONARY_PNL_READ_LIMIT = os.getenv("DISCRETIONARY_PNL_READ_LIMIT", "30 per minute")

_TRUTHY = ("1", "true", "yes", "on")
VALID_PERIODS = ("1d", "7d", "15d", "30d", "90d", "ytd", "all", "custom")
_MAX_FILTER_LENGTH = 80
_MAX_ECHO_LENGTH = 24
_MAX_TRADE_ID = 2**63 - 1  # larger ids cannot exist (and overflow the database driver)

# Same shape as strategy_pnl: overlapping and back-to-back identical reads share one result.
# The key carries the user and every filter, so users and filters never mix.
_READ_TTL_S = 3.0
_disc_flights = SingleFlight(join_window=5.0, wait_timeout=60.0)


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in _TRUTHY


def discretionary_enabled() -> bool:
    """True only when the Performance UI flag AND the ledger flag are both on."""
    return _flag("DISCRETIONARY_PERFORMANCE_UI_ENABLED") and _flag("DISCRETIONARY_LEDGER_ENABLED")


def _disabled():
    return (
        jsonify(
            {
                "status": "error",
                "code": "feature_disabled",
                "message": "Discretionary performance is turned off.",
            }
        ),
        404,
    )


@discretionary_pnl_bp.after_request
def _no_store(response):
    """Every discretionary JSON answer is per-user and live: never cached by a browser or proxy."""
    response.headers["Cache-Control"] = "no-store"
    return response


def _echo(value: str) -> str:
    """A user-supplied value shortened for an error message."""
    if len(value) > _MAX_ECHO_LENGTH:
        return value[:_MAX_ECHO_LENGTH] + "..."
    return value


def _error(message: str, status_code: int = 400):
    return jsonify({"status": "error", "message": message}), status_code


def _text(name: str):
    """A stripped query value, None when absent or blank. Returns (value, error response)."""
    raw = request.args.get(name)
    if raw is None or not raw.strip():
        return None, None
    value = raw.strip()
    if len(value) > _MAX_FILTER_LENGTH:
        return None, _error(f"{name} is too long. Use at most {_MAX_FILTER_LENGTH} characters.")
    return value, None


def _filters():
    """(broker, symbol, account, error response)."""
    values = []
    for name in ("broker", "symbol", "account"):
        value, error = _text(name)
        if error:
            return None, None, None, error
        values.append(value)
    return values[0], values[1], values[2], None


def _iso_date(name: str):
    value, error = _text(name)
    if error or value is None:
        return None, error
    try:
        date.fromisoformat(value)
    except ValueError:
        return None, _error(f"{name} must be a date as YYYY-MM-DD, for example 2026-11-17.")
    return value, None


def _quotes_flag():
    raw = request.args.get("quotes")
    if raw is None or not raw.strip():
        return False, None
    if raw.strip() in ("0", "1"):
        return raw.strip() == "1", None
    return None, _error("quotes must be 0 or 1.")


@discretionary_pnl_bp.route("/api/discretionary-pnl/status", methods=["GET"])
@check_session_validity
@limiter.limit(DISCRETIONARY_PNL_READ_LIMIT)
def discretionary_status():
    return jsonify({"status": "success", "enabled": discretionary_enabled()})


@discretionary_pnl_bp.route("/api/discretionary-pnl/summary", methods=["GET"])
@check_session_validity
@limiter.limit(DISCRETIONARY_PNL_READ_LIMIT)
def discretionary_summary():
    """Headline metrics, breakdowns, trade and position tables for the chosen period and
    filters (broker, symbol, account). ``quotes=1`` fetches live quotes for the unrealized
    P&L of open positions; the default makes no broker call."""
    if not discretionary_enabled():
        return _disabled()
    period = request.args.get("period", "30d").strip()
    if period not in VALID_PERIODS:
        return _error(f"Invalid period: {_echo(period)}. Use one of {', '.join(VALID_PERIODS)}.")
    start, error = _iso_date("start")
    if error:
        return error
    end, error = _iso_date("end")
    if error:
        return error
    broker, symbol, account, error = _filters()
    if error:
        return error
    quotes, error = _quotes_flag()
    if error:
        return error
    key = ("summary", session["user"], period, start, end, broker, symbol, account, quotes)

    def fetch():
        try:
            body = disc.get_performance(period, start, end, broker, symbol, account, quotes)
        except ValueError as exc:
            return False, {"status": "error", "message": str(exc)}, 400
        except Exception:
            logger.exception("Discretionary summary failed")
            return (
                False,
                {"status": "error", "message": "Could not build the discretionary summary."},
                500,
            )
        return True, {"status": "success", **body}, 200

    _ok, response, status_code = _disc_flights.do(key, fetch, ttl=_READ_TTL_S)
    return jsonify(response), status_code


@discretionary_pnl_bp.route("/api/discretionary-pnl/trades/<int:trade_id>/fills", methods=["GET"])
@check_session_validity
@limiter.limit(DISCRETIONARY_PNL_READ_LIMIT)
def discretionary_trade_fills(trade_id):
    """The ledger fills (ENTRY / EXIT, of every leg) behind one completed trade (a position
    from flat to flat; its id is the position id) of the build named by the
    ``build_id`` query parameter. Trade ids are only unique within a build (a rebuild
    reassigns them), so a build_id that is not the current one answers 409 build_changed
    instead of returning whatever trade now holds that id."""
    if not discretionary_enabled():
        return _disabled()
    build_id, error = _text("build_id")
    if error:
        return error
    if build_id is None:
        return (
            jsonify(
                {
                    "status": "error",
                    "code": "build_id_required",
                    "message": "build_id is required: use the build_id of the summary you are viewing.",
                }
            ),
            400,
        )
    if trade_id > _MAX_TRADE_ID:
        return (
            jsonify({"status": "error", "code": "trade_not_found", "message": "Trade not found."}),
            404,
        )
    key = ("fills", session["user"], trade_id, build_id)

    def fetch():
        try:
            body = disc.get_trade_fills(trade_id, build_id=build_id)
        except disc.BuildChanged as exc:
            return (
                False,
                {
                    "status": "error",
                    "code": "build_changed",
                    "message": "The data changed since this page loaded. Refresh and try again.",
                    "current_build_id": exc.current_build_id,
                },
                409,
            )
        except Exception:
            logger.exception("Discretionary trade fills failed")
            return False, {"status": "error", "message": "Could not read the trade fills."}, 500
        if body is None:
            return (
                False,
                {"status": "error", "code": "trade_not_found", "message": "Trade not found."},
                404,
            )
        return True, {"status": "success", **body}, 200

    _ok, response, status_code = _disc_flights.do(key, fetch, ttl=_READ_TTL_S)
    return jsonify(response), status_code


@discretionary_pnl_bp.route("/api/discretionary-pnl/unresolved", methods=["GET"])
@check_session_validity
@limiter.limit(DISCRETIONARY_PNL_READ_LIMIT)
def discretionary_unresolved():
    """Fills still unattributed (excluded from P&L) and fills the engine could not place."""
    if not discretionary_enabled():
        return _disabled()
    broker, symbol, account, error = _filters()
    if error:
        return error
    key = ("unresolved", session["user"], broker, symbol, account)

    def fetch():
        try:
            unresolved = disc.get_unresolved(broker=broker, account=account, symbol=symbol)
            orphans = disc.get_orphans(broker=broker, account=account, symbol=symbol)
        except Exception:
            logger.exception("Discretionary unresolved read failed")
            return False, {"status": "error", "message": "Could not read the fills."}, 500
        return (
            True,
            {
                "status": "success",
                "unresolved": unresolved,
                "unresolved_count": len(unresolved),
                "orphans": orphans,
                "orphans_count": len(orphans),
            },
            200,
        )

    _ok, response, status_code = _disc_flights.do(key, fetch, ttl=_READ_TTL_S)
    return jsonify(response), status_code
