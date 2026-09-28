"""
Attaches the `strategy` tag to live orderbook/tradebook rows for display.

Sandbox rows already carry `strategy` from `database/sandbox_db.py`. Live
rows never do - the broker has no concept of OpenAlgo's strategy tag - so it
is looked up here from `database/strategy_book_db.py`'s orderid -> strategy
mapping, which is populated for live orders too (`order.placed` fires in both
modes). Display-only: a missing or unreadable tag never breaks the row.
"""

from typing import Any

from database.strategy_book_db import get_strategies_for_orderids


def attach_strategy(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return new row dicts with `strategy` filled in from the tag lookup.

    Rows that already carry a `strategy` (sandbox) are left untouched. The
    input list and its dicts are never mutated.
    """
    if not rows:
        return []

    orderids = [row.get("orderid") for row in rows if "strategy" not in row and row.get("orderid")]
    tags = get_strategies_for_orderids(orderids)

    enriched = []
    for row in rows:
        if "strategy" in row:
            enriched.append(dict(row))
            continue
        new_row = dict(row)
        new_row["strategy"] = tags.get(row.get("orderid"), "")
        enriched.append(new_row)
    return enriched


def attach_strategy_to_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return new position row dicts with `strategy` filled in.

    A position has no `orderid` to look up - it is the broker's NET quantity
    per (symbol, exchange, product), and `database/sandbox_db.py`'s
    SandboxPositions carries no strategy column at all, by design: more than
    one strategy can share the same net position (confirmed in practice -
    e.g. JIOFIN, HDFCBANK, TCS were each traded by two strategies
    concurrently, see docs/strategy-pnl-fork-modification.md). So this
    matches against every currently-open leg from
    `database/strategy_book_db.py` (the same source of truth the Strategy
    P&L page uses, for the currently running mode) by (symbol, exchange,
    product), and joins every strategy with a nonzero quantity there - one
    name for the common case, several comma-separated when genuinely shared,
    empty when no strategy tag ever reached this position.
    """
    if not rows:
        return []

    from database.settings_db import get_analyze_mode
    from database.strategy_book_db import StrategyBookUnavailable, get_strategy_legs

    mode = "analyze" if get_analyze_mode() else "live"
    try:
        legs = get_strategy_legs(mode=mode)
    except StrategyBookUnavailable:
        # Display-only: an unreadable strategy book should not break the
        # positions page, just leave the column blank.
        legs = []

    strategies_by_key: dict[tuple[Any, Any, Any], list[str]] = {}
    for leg in legs:
        if abs(float(leg.get("quantity") or 0)) <= 1e-9:
            continue
        key = (leg.get("symbol"), leg.get("exchange"), leg.get("product"))
        names = strategies_by_key.setdefault(key, [])
        name = leg.get("strategy")
        if name and name not in names:
            names.append(name)

    enriched = []
    for row in rows:
        key = (row.get("symbol"), row.get("exchange"), row.get("product"))
        new_row = dict(row)
        new_row["strategy"] = ", ".join(strategies_by_key.get(key, []))
        enriched.append(new_row)
    return enriched
