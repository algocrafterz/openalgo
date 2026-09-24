"""
Per-strategy performance for the UI-only Strategy P&L page.

`database/strategy_book_db.py` already tracks per-strategy legs (open
quantity, average price, realized P&L) fed from the event bus for both live
and sandbox orders, but it is otherwise read only by Flow's internal
StrategyPnlNode. This module adapts the same read path
(`services/strategy_pnl_service.pnl_from_book`) for a general, session-authed
page instead of Flow's SDK-style client.

Never reports a P&L of zero on a read failure - an unreadable strategy book
or a broker position-book error is returned as an explicit error, since a
silent zero is indistinguishable from a flat, healthy strategy.
"""

from typing import Any

from database.auth_db import get_api_key_for_tradingview
from database.settings_db import get_analyze_mode
from database.strategy_book_db import StrategyBookUnavailable, get_strategy_legs
from services.positionbook_service import get_positionbook
from services.strategy_pnl_service import pnl_from_book
from utils.logging import get_logger

logger = get_logger(__name__)


def get_strategy_performance(
    login_username: str, auth_token: str, broker: str
) -> tuple[bool, dict[str, Any], int]:
    """Realized / unrealized / total P&L for every tracked strategy.

    Mirrors the live/analyze branching `blueprints/orders.py`'s positions
    route uses, since positions differ by mode but the strategy book does not
    (both live and sandbox order.placed events feed the same book).
    """
    current_mode = "analyze" if get_analyze_mode() else "live"

    try:
        # No user_id filter: orders placed via /api/v1/placeorder (e.g. by an
        # external script) carry no user_id in their request body, so the
        # book's StrategyOrderTag.user_id is empty for them. Since a
        # deployment is single-user (see CLAUDE.md), reading every leg is
        # correct rather than a leak.
        #
        # Filtered to the current mode: a paper and a live position of the
        # same (strategy, symbol, exchange, product) are separate rows (see
        # upgrade/migrate_strategy_book_mode.py) - mixing them back together
        # here would reintroduce the exact ambiguity that migration removed.
        # Pre-migration rows (mode='unknown') are deliberately excluded, not
        # just kept separate - they predate several known accounting bugs in
        # this data (see the sandbox P&L undercount and EOD partial-exit
        # fixes), so they are untrusted rather than merely unattributed.
        legs = get_strategy_legs(mode=current_mode)
    except StrategyBookUnavailable as exc:
        logger.error(f"Strategy P&L unavailable: {exc}")
        return False, {"status": "error", "message": f"Strategy book unavailable: {exc}"}, 503

    if get_analyze_mode():
        api_key = get_api_key_for_tradingview(login_username)
        if not api_key:
            return (
                False,
                {"status": "error", "message": "API key required for analyze mode"},
                400,
            )
        success, response, status_code = get_positionbook(api_key=api_key)
    else:
        success, response, status_code = get_positionbook(auth_token=auth_token, broker=broker)

    if not success:
        message = response.get("message", "Position book unavailable")
        logger.error(f"Strategy P&L: could not fetch positions: {message}")
        return (
            False,
            {
                "status": "error",
                "message": f"Position book unavailable, cannot value open legs: {message}",
            },
            status_code if status_code >= 400 else 502,
        )

    positions = response.get("data") or []
    if not isinstance(positions, list):
        positions = []

    grouped = pnl_from_book(legs, positions)
    strategies = list(grouped.values())

    return (
        True,
        {
            "status": "success",
            "mode": current_mode,
            "strategies": strategies,
            "count": len(strategies),
        },
        200,
    )
