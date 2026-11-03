"""Strategy P&L Blueprint.

Surfaces `database/strategy_book_db.py`'s per-strategy position/P&L book,
otherwise read only by Flow's internal StrategyPnlNode, as a general page.
Single-user, session-authed (no /api/v1 exposure — UI-only), following the
same pattern as `blueprints/strategy_portfolio.py`.
"""

import csv
import hashlib
import io
import json
import math
import os
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import redirect

from database.auth_db import get_api_key_for_tradingview, get_auth_token
from database.settings_db import get_analyze_mode
from limiter import limiter
from services.performance_eval.ai_response import INVALID_MESSAGE, validate_response
from services.performance_eval.dataset import (
    build_evaluation_dataset,
    dataset_to_text,
    package_hash,
)
from services.performance_eval.prompt import (
    PROMPT_PASTE_MAX_CHARS,
    build_prompt,
    data_filename,
)
from services.performance_eval.rules import build_report
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


def _clean_names(raw_values) -> list[str]:
    """Whitespace stripped, empties dropped, duplicates removed with the first
    order kept."""
    seen: dict[str, None] = {}
    for raw in raw_values:
        name = raw.strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def _names(key: str) -> list[str]:
    """Repeated query values, cleaned by `_clean_names`."""
    return _clean_names(request.args.getlist(key))


def _filter_error_message(included: list[str], excluded: list[str]) -> str | None:
    """The trader-facing reason a strategy filter is not acceptable, or None."""
    if len(included) + len(excluded) > _MAX_FILTER_NAMES:
        return (
            f"Too many strategies selected. Choose at most {_MAX_FILTER_NAMES} "
            "strategies to include or exclude."
        )
    if any(len(name) > _MAX_NAME_LENGTH for name in included + excluded):
        return f"A strategy name is too long. Names can have at most {_MAX_NAME_LENGTH} characters."
    return None


def _strategy_filter():
    """Read the strategy filter from the query string.

    Returns (strategy, include, exclude, None) or (None, None, None, error
    response). The first included name goes in `strategy` and the rest in
    `include`, so a plain `?strategy=ORB` reaches the service as before.
    """
    included = _names("strategy")
    excluded = _names("exclude")
    message = _filter_error_message(included, excluded)
    if message is None:
        strategy = included[0] if included else None
        return strategy, included[1:], excluded, None
    return None, None, None, (jsonify({"status": "error", "message": message}), 400)


MAX_CAPITAL = 1_000_000_000_000  # 1 lakh crore rupees


def _capital_from_text(raw: str | None) -> tuple[float | None, str | None]:
    """Validate a capital amount (rupees) given as text.

    Returns (capital, None) with None for a blank or absent value, or
    (None, trader-facing message) when the value is not a usable amount.
    """
    if raw is None or not raw.strip():
        return None, None
    try:
        value = float(raw.strip())
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value <= 0:
        return None, "Enter a capital amount greater than zero, for example 1000000."
    if value > MAX_CAPITAL:
        return None, "That capital is larger than this page supports. Enter a smaller amount."
    return value, None


def _parse_capital():
    """Read the optional `capital` query value (rupees).

    Returns (capital, None) with None for a blank or absent value, or
    (None, error response) when the value is not a usable amount.
    """
    value, message = _capital_from_text(request.args.get("capital"))
    if message:
        return None, (jsonify({"status": "error", "message": message}), 400)
    return value, None


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
    capital, capital_error = _parse_capital()
    if capital_error:
        return capital_error
    period = request.args.get("period", "30d")
    start = request.args.get("start")
    end = request.args.get("end")
    extra = {} if capital is None else {"capital": capital}
    key = (
        "daily",
        session["user"],
        tuple(sorted(([strategy] if strategy else []) + include)),
        tuple(sorted(exclude)),
        period,
        start,
        end,
        capital,
    )
    _success, response, status_code = _pnl_flights.do(
        key,
        lambda: get_daily_performance(
            strategy, period, start=start, end=end, include=include, exclude=exclude, **extra
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


# ---------------------------------------------------------------------------
# Evaluation package (read only; no model is ever called)
# ---------------------------------------------------------------------------

#: The largest request body the evaluation routes read, in bytes. The pasted
#: analysis is at most 60,000 characters, which can take up to 3 bytes each when
#: JSON-escaped; this leaves room for the selection fields.
MAX_EVALUATE_BODY_BYTES = 130_000
_TOO_LARGE_MESSAGE = "That request is too large. Paste a shorter analysis and try again."
_UNREADABLE_MESSAGE = "The request could not be read. Reload the page and try again."
_EVALUATION_FAILED_MESSAGE = "The evaluation could not be prepared. Try again in a moment."


def _json_names(body: dict, key: str) -> tuple[list[str] | None, str | None]:
    """A list of strategy names (or one name) from a JSON body, cleaned like the query string."""
    raw = body.get(key)
    if raw is None:
        return [], None
    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return None, "Strategy names must be sent as text."
    return _clean_names(values), None


def _json_capital(body: dict) -> tuple[float | None, str | None]:
    """The optional `capital` from a JSON body, validated like the query value."""
    raw = body.get("capital")
    if raw is None:
        return None, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return _capital_from_text("nan")
    return _capital_from_text(raw if isinstance(raw, str) else repr(raw))


def _evaluate_error(message: str, status: int = 400, **extra):
    return jsonify({"status": "error", "message": message, **extra}), status


def _read_json_body():
    """The JSON object of the request, or (None, error response)."""
    request.max_content_length = MAX_EVALUATE_BODY_BYTES
    if (request.content_length or 0) > MAX_EVALUATE_BODY_BYTES:
        return None, _evaluate_error(_TOO_LARGE_MESSAGE, 413)
    # The Content-Length check above does the real work. The per-request limit
    # is a backstop for a body that declares no length.
    try:
        body = request.get_json(silent=True)
    except RequestEntityTooLarge:
        return None, _evaluate_error(_TOO_LARGE_MESSAGE, 413)
    except (RecursionError, ValueError):
        # Crafted nesting can exhaust the parser's stack; treat it as unreadable.
        return None, _evaluate_error(_UNREADABLE_MESSAGE)
    if not isinstance(body, dict):
        return None, _evaluate_error(_UNREADABLE_MESSAGE)
    return body, None


def _read_selection(body: dict):
    """Validate the selection fields of a body. Returns (selection, error response).

    Same limits as the GET routes. The mode is never read from the body.
    """
    included, message = _json_names(body, "strategy")
    excluded, exclude_message = _json_names(body, "exclude")
    message = (
        message
        or exclude_message
        or (
            _filter_error_message(included, excluded)
            if included is not None and excluded is not None
            else None
        )
    )
    if message:
        return None, _evaluate_error(message)
    capital, message = _json_capital(body)
    if message:
        return None, _evaluate_error(message)
    period = body.get("period", "30d")
    start, end = body.get("start"), body.get("end")
    if not isinstance(period, str) or any(
        v is not None and not isinstance(v, str) for v in (start, end)
    ):
        return None, _evaluate_error("The period and dates must be sent as text.")
    return (included, excluded, period, start, end, capital), None


def _build_dataset(selection):
    included, excluded, period, start, end, capital = selection
    return build_evaluation_dataset(
        included[1:], excluded, included[0] if included else None, period, start, end, capital
    )


@strategy_pnl_bp.route("/api/strategy-pnl/evaluation/prepare", methods=["POST"])
@check_session_validity
@limiter.limit(STRATEGY_PNL_READ_LIMIT)
def strategy_pnl_evaluation_prepare():
    """Build the evaluation package: dataset, rule-based report, prompt and export.

    Body (JSON): strategy, exclude, period, start, end, capital. Nothing is
    stored and no model is called. Mode is never read from the body.
    """
    body, error = _read_json_body()
    if error:
        return error
    selection, error = _read_selection(body)
    if error:
        return error
    try:
        ok, dataset, status = _build_dataset(selection)
        if not ok:
            return jsonify(dataset), status
        meta = dataset["meta"]
        digest = package_hash(dataset)
        with_data = build_prompt(dataset, include_data=True)
        package = {
            "meta": {
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "mode_display": meta["mode_display"],
                "period": meta["period"],
                "start_date": meta["start_date"],
                "end_date": meta["end_date"],
                "capital": meta["capital"],
                "strategies_included": meta["strategies_included"],
                "package_hash": digest,
                "truncated": meta["truncated"],
            },
            "dataset": json.loads(dataset_to_text(dataset)),
            "report": build_report(dataset),
            "prompt": {
                "text": build_prompt(dataset, include_data=False),
                "text_with_data": with_data if len(with_data) <= PROMPT_PASTE_MAX_CHARS else None,
            },
            "export": {
                "data_filename": data_filename(dataset),
                "data_json": dataset_to_text(dataset),
                "notice": (
                    "This file holds the figures computed from your closed trades in the "
                    f"{meta['mode_display']} mode for the selected strategies, including "
                    "strategy names and symbols. It holds no account details or keys."
                ),
            },
        }
    except Exception:
        logger.exception("Could not prepare the evaluation package")
        return _evaluate_error(_EVALUATION_FAILED_MESSAGE, 500)
    return jsonify({"status": "success", "schema_version": 1, "package": package}), 200


@strategy_pnl_bp.route("/api/strategy-pnl/evaluation/validate", methods=["POST"])
@check_session_validity
@limiter.limit(STRATEGY_PNL_READ_LIMIT)
def strategy_pnl_evaluation_validate():
    """Check an analysis pasted back from claude.ai against the current selection.

    Body (JSON): the selection fields plus `response` (text or an object).
    Nothing is stored and no model is called.
    """
    body, error = _read_json_body()
    if error:
        return error
    selection, error = _read_selection(body)
    if error:
        return error
    pasted = body.get("response")
    if not isinstance(pasted, (str, dict)) or (isinstance(pasted, str) and not pasted.strip()):
        return _evaluate_error("Paste the analysis from Claude before checking it.")
    try:
        ok, dataset, status = _build_dataset(selection)
        if not ok:
            return jsonify(dataset), status
        labels = {b["strategy"]: b["evidence_label"] for b in dataset["strategies"]}
        counts = {b["strategy"]: b["trades_count"] for b in dataset["strategies"]}
        valid, evaluation, checks, problems = validate_response(
            pasted,
            package_hash=package_hash(dataset),
            deterministic_labels=labels,
            pooled_label=dataset["pooled"]["evidence_label"],
            trade_counts=counts,
        )
    except Exception:
        logger.exception("Could not check the pasted evaluation")
        return _evaluate_error(_EVALUATION_FAILED_MESSAGE, 500)
    if not valid:
        message = (
            problems[0]["message"]
            if len(problems) == 1 and problems[0]["path"] == "response"
            else INVALID_MESSAGE
        )
        return _evaluate_error(message, 400, problems=problems)
    return jsonify({"status": "success", "evaluation": evaluation, "checks": checks}), 200
