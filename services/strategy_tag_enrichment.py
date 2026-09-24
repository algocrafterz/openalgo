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
