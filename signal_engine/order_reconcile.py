"""Reconcile an order whose submission timed out or could not be confirmed.

A client-side TIMEOUT only means the HTTP answer never arrived; the broker may have taken the
order. 2026-10-13 ~13:56: the broker connection degraded, every exit for LAURUSLABS (74) and
SUNPHARMA (236) timed out, the order book could not be read either, and the retry loop read
"book unreadable" as "not filled" and resent. The broker had filled all three copies of each:
the first closed the long, the other two opened shorts of 148 and 472 that nothing tracked.

Rule enforced here: an order is only resent when the broker's order book was READ and shows no
live or completed copy of it. If the book cannot be read, or the order is still working, the
outcome is UNCERTAIN: nothing more is sent for that position until a later reconcile settles it.

Pure logic plus a small registry; the caller owns logging and alerts.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from signal_engine.timeutils import IST

FILLED = "filled"
ABSENT = "absent"
UNCERTAIN = "uncertain"

_COMPLETE = {"complete", "completed", "filled"}
_DEAD = {"rejected", "cancelled", "canceled", "expired"}


@dataclass(frozen=True)
class Reconciliation:
    outcome: str  # FILLED | ABSENT | UNCERTAIN
    order_id: str = ""  # the order to adopt when FILLED
    duplicate_ids: tuple[str, ...] = ()  # further completed copies of the same order
    detail: str = ""


@dataclass(frozen=True)
class UncertainOrder:
    """An order the broker may or may not hold, awaiting reconciliation."""

    strategy: str
    symbol: str
    action: str
    quantity: int
    since: datetime
    kind: str  # "EXIT" | "ENTRY"


# (strategy, symbol) -> the unresolved order. Bounded by strategies x symbols; an entry is
# removed when a reconcile settles it.
_uncertain: dict[tuple[str, str], UncertainOrder] = {}


def mark_uncertain(order: UncertainOrder) -> None:
    _uncertain[(order.strategy, order.symbol)] = order


def get_uncertain(strategy: str, symbol: str) -> UncertainOrder | None:
    return _uncertain.get((strategy, symbol))


def clear_uncertain(strategy: str, symbol: str) -> None:
    _uncertain.pop((strategy, symbol), None)


def _placed_at_or_after(row: dict, since: datetime) -> bool:
    raw = row.get("timestamp") or row.get("order_timestamp")
    if not raw:
        return True
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST) >= since
    except ValueError:
        return True  # an unparseable stamp must not discard a real match


def classify_book(
    book: list, symbol: str, action: str, quantity: int, since: datetime
) -> tuple[list[str], int]:
    """(completed order ids, count of still-working copies) for this symbol, side and size."""
    complete: list[str] = []
    working = 0
    for row in book:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")).upper() != symbol.upper():
            continue
        if str(row.get("action", "")).upper() != action.upper():
            continue
        try:
            if int(row.get("quantity", 0)) != quantity:
                continue
        except (TypeError, ValueError):
            continue
        if not _placed_at_or_after(row, since):
            continue
        status = str(row.get("order_status") or row.get("status") or "").lower()
        if status in _DEAD:
            continue
        if status in _COMPLETE:
            order_id = str(row.get("orderid") or row.get("order_id") or "")
            if order_id:
                complete.append(order_id)
            continue
        working += 1  # open, pending, trigger pending, partially filled: still in flight
    return complete, working


async def reconcile_timed_out_order(
    *,
    symbol: str,
    action: str,
    quantity: int,
    since: datetime,
    fetch_orderbook: Callable[[], Awaitable[list | None]],
    attempts: int,
    delay: float,
    exclude: frozenset[str] = frozenset(),
) -> Reconciliation:
    """Poll the order book until the order is seen, or the answer is as settled as it can be.

    FILLED    a completed copy exists (any further completed copies are reported as duplicates).
    ABSENT    the final read succeeded and showed no completed or working copy: safe to resend.
    UNCERTAIN the book was unreadable on the final read, or a copy is still working.

    `exclude` holds ids already booked by this engine, so an earlier same-size fill is not adopted.
    """
    detail = "order book never read"
    last_readable_clean = False
    for attempt in range(1, attempts + 1):
        await asyncio.sleep(delay)
        book = await fetch_orderbook()
        if book is None:
            last_readable_clean = False
            detail = f"order book unreadable on attempt {attempt}/{attempts}"
            continue
        complete, working = classify_book(book, symbol, action, quantity, since)
        complete = [oid for oid in complete if oid not in exclude]
        if complete:
            return Reconciliation(FILLED, complete[0], tuple(complete[1:]), "completed on broker")
        if working:
            last_readable_clean = False
            detail = f"{working} matching order(s) still working on attempt {attempt}/{attempts}"
            continue
        last_readable_clean = True
        detail = "no matching order on the broker"
    if last_readable_clean:
        return Reconciliation(ABSENT, detail=detail)
    return Reconciliation(UNCERTAIN, detail=detail)
