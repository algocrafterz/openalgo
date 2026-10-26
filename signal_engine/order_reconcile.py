"""Reconcile an order whose submission timed out or could not be confirmed.

A client-side TIMEOUT only means the HTTP answer never arrived; the broker may have taken the
order. 2026-10-13 ~13:56: the broker connection degraded, every exit for LAURUSLABS (74) and
SUNPHARMA (236) timed out, the order book could not be read either, and the retry loop read
"book unreadable" as "not filled" and resent. The broker had filled all three copies of each:
the first closed the long, the other two opened shorts of 148 and 472 that nothing tracked.

Rule enforced here: an order is only resent when the broker's order book was READ and shows no
live or completed copy of it. If the book cannot be read, or the order is still working, the
outcome is UNCERTAIN: nothing more is sent for that position until a later reconcile settles it.

Pure logic plus a small registry; the caller owns logging and alerts. The one exception is an
order-book timestamp this module cannot read, which only it can see.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

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
    #: Copies whose stamp could not prove they are or are not this order (see classify_book_detail).
    ambiguous_ids: tuple[str, ...] = ()
    #: The completed ones among them: the only ones a broker-position proof may adopt.
    ambiguous_complete_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class UncertainOrder:
    """An order the broker may or may not hold, awaiting reconciliation."""

    strategy: str
    symbol: str
    action: str
    quantity: int
    since: datetime
    kind: str  # "EXIT" | "ENTRY"
    #: For an ENTRY: the signal and order, so a fill found later can be adopted and given its stop.
    signal: object | None = field(default=None, compare=False)
    order: object | None = field(default=None, compare=False)
    #: For an ENTRY whose last order-book read was CLEAN (nothing on the book) right after a timeout:
    #: until this moment the order may still land, so the entry stays watched and new entries for
    #: the strategy and symbol are held. None = no bounded window (the book was unreadable, or the
    #: order was still working), so only a readable answer releases it.
    watch_until: datetime | None = field(default=None, compare=False)


# (strategy, symbol) -> the unresolved order. Bounded by strategies x symbols; an entry is
# removed when a reconcile settles it.
_uncertain: dict[tuple[str, str], UncertainOrder] = {}


# (strategy, symbol) -> when the sweeper last looked, and when an alert last went out.
_last_checked: dict[tuple[str, str], datetime] = {}
_last_alerted: dict[tuple[str, str], datetime] = {}


def mark_uncertain(order: UncertainOrder) -> None:
    _uncertain[(order.strategy, order.symbol)] = order


def watch_open(order: UncertainOrder, now: datetime) -> bool:
    """True while a clean-absent ENTRY is still inside its late-fill watch window."""
    return order.watch_until is not None and now < order.watch_until


def get_uncertain(strategy: str, symbol: str) -> UncertainOrder | None:
    return _uncertain.get((strategy, symbol))


def all_uncertain() -> list[UncertainOrder]:
    return list(_uncertain.values())


def clear_uncertain(strategy: str, symbol: str) -> None:
    key = (strategy, symbol)
    _uncertain.pop(key, None)
    _last_checked.pop(key, None)
    _last_alerted.pop(key, None)


@dataclass(frozen=True)
class LateDuplicateWatch:
    """An exit resent after a clean late-fill watch: the original may still land and duplicate it.

    Kept apart from the held-order registry on purpose: a held order blocks the tracker's
    missing-stop retry and new entries, and a partial exit's remaining shares must keep their stop.
    The position fields are a snapshot taken before the exit, because the position may be gone.
    """

    strategy: str
    symbol: str
    exchange: str
    product: str
    action: str
    quantity: int
    since: datetime  # when the timed-out original was sent
    until: datetime  # stop watching after this
    known_ids: frozenset  # the resent order(s): not duplicates
    direction: object
    pre_exit_quantity: int


# (strategy, symbol) -> the watch. Bounded by strategies x symbols; dropped when it expires.
_late_duplicates: dict[tuple[str, str], LateDuplicateWatch] = {}


def watch_late_duplicate(watch: LateDuplicateWatch) -> None:
    key = (watch.strategy, watch.symbol)
    _late_duplicates[key] = watch
    _last_checked.pop(("late", *key), None)  # a new watch is looked at on the next sweep
    _last_alerted.pop(("late", *key), None)


def get_late_duplicate_watch(strategy: str, symbol: str) -> LateDuplicateWatch | None:
    return _late_duplicates.get((strategy, symbol))


def all_late_duplicate_watches() -> list[LateDuplicateWatch]:
    return list(_late_duplicates.values())


def _throttle(table: dict, key: tuple, now: datetime, interval_seconds: float) -> bool:
    last = table.get(key)
    if last is not None and (now - last).total_seconds() < interval_seconds:
        return False
    table[key] = now
    return True


def late_due(watch: LateDuplicateWatch, now: datetime, interval_seconds: float) -> bool:
    """True when the sweeper has not looked at this watch within `interval_seconds`; records the look."""
    return _throttle(_last_checked, ("late", watch.strategy, watch.symbol), now, interval_seconds)


def late_alert_due(watch: LateDuplicateWatch, now: datetime, interval_seconds: float) -> bool:
    return _throttle(_last_alerted, ("late", watch.strategy, watch.symbol), now, interval_seconds)


def clear_late_duplicate_watch(strategy: str, symbol: str) -> None:
    key = (strategy, symbol)
    _late_duplicates.pop(key, None)
    _last_checked.pop(("late", *key), None)
    _last_alerted.pop(("late", *key), None)


def due(order: UncertainOrder, now: datetime, interval_seconds: float) -> bool:
    """True when the sweeper has not looked at this order within `interval_seconds`; records the look."""
    key = (order.strategy, order.symbol)
    last = _last_checked.get(key)
    if last is not None and (now - last).total_seconds() < interval_seconds:
        return False
    _last_checked[key] = now
    return True


def alert_due(order: UncertainOrder, now: datetime, interval_seconds: float) -> bool:
    """Rate limit for repeat alerts about an order that stays unresolved; records the alert."""
    key = (order.strategy, order.symbol)
    last = _last_alerted.get(key)
    if last is not None and (now - last).total_seconds() < interval_seconds:
        return False
    _last_alerted[key] = now
    return True


#: Order-book stamps as brokers send them, all IST: the sandbox's ISO form, and Flattrade (Noren)
#: day-first forms - "norentm" is time-first ("13:56:46 13-10-2026"), its history is date-first.
_BROKER_STAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%H:%M:%S %d-%m-%Y", "%d-%m-%Y %H:%M:%S")


def parse_broker_stamp(raw) -> datetime | None:
    """An order-book timestamp as an IST datetime, or None when it is missing or unrecognised.

    ISO stamps may carry a "T" and an offset ("2026-10-13T08:26:46Z"); those are converted to IST.
    A stamp without an offset is IST, as every broker this engine trades sends it.
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in _BROKER_STAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    if "T" in text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(IST) if parsed.tzinfo else parsed.replace(tzinfo=IST)
    return None


#: Seconds before `since` in which a stamp may still be this order: the broker's clock and ours
#: are not the same clock (Flattrade's request_time ran up to ~1.3 s off ours 2026-10-21..23), so
#: an order stamped a moment before the send is neither proof it is ours nor proof it is not.
_SKEW_SECONDS = 5.0


@dataclass(frozen=True)
class BookMatch:
    """Same symbol, side and size rows of the book, split by how sure the match is."""

    complete: list  # completed, stamped at or after `since`: this order (or a copy of it)
    working: int  # still in flight, stamped at or after `since`
    ambiguous: list  # live or completed, stamp unreadable or within the skew before `since`
    ambiguous_complete: list = field(default_factory=list)  # the completed ones among `ambiguous`


def classify_book_detail(
    book: list,
    symbol: str,
    action: str,
    quantity: int,
    since: datetime,
    strategy: str = "",
    exclude: frozenset[str] = frozenset(),
) -> BookMatch:
    """Classify the book's copies of an order as DEFINITE (complete/working), AMBIGUOUS or not ours.

    An unreadable stamp is never proof the order was placed after the send (it could be an earlier
    same-size fill of the day) and never proof it was not (it could be this order): it is AMBIGUOUS,
    which blocks a resend without adopting anything. With `strategy`, a row tagged for a DIFFERENT
    strategy is not a copy (BREAKINGTRADE and -WATCHLIST shared 49 positions 2026-09-30 to 10-23);
    untagged rows (a live broker's book) still match. `exclude` ids are already booked here.
    """
    complete: list[str] = []
    ambiguous: list[str] = []
    ambiguous_complete: list[str] = []
    working = 0
    for row in book:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")).upper() != symbol.upper():
            continue
        tag = str(row.get("strategy") or "").strip()
        if strategy and tag and tag.upper() != strategy.upper():
            continue
        if str(row.get("action", "")).upper() != action.upper():
            continue
        try:
            if int(row.get("quantity", 0)) != quantity:
                continue
        except (TypeError, ValueError):
            continue
        status = str(row.get("order_status") or row.get("status") or "").lower()
        if status in _DEAD:
            continue
        order_id = str(row.get("orderid") or row.get("order_id") or "")
        if order_id and order_id in exclude:
            continue
        raw = row.get("timestamp") or row.get("order_timestamp")
        stamp = parse_broker_stamp(raw)
        if stamp is None or stamp < since:
            if stamp is not None and (since - stamp).total_seconds() > _SKEW_SECONDS:
                continue  # clearly earlier than the send: not this order
            if stamp is None:
                logger.warning(
                    f"order book timestamp {raw!r} for order {order_id or '?'} ({symbol} {action} "
                    f"{quantity}) is in an unknown format; it cannot be matched or ruled out"
                )
            ambiguous.append(order_id or "?")
            if status in _COMPLETE and order_id:
                ambiguous_complete.append(order_id)
            continue
        if status in _COMPLETE:
            if order_id:
                complete.append(order_id)
            continue
        working += 1  # open, pending, trigger pending, partially filled: still in flight
    return BookMatch(complete, working, ambiguous, ambiguous_complete)


def classify_book(
    book: list, symbol: str, action: str, quantity: int, since: datetime, strategy: str = ""
) -> tuple[list[str], int]:
    """(definite completed ids, copies still in flight) - ambiguous copies count as in flight."""
    match = classify_book_detail(book, symbol, action, quantity, since, strategy)
    return match.complete, match.working + len(match.ambiguous)


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
    strategy: str = "",
) -> Reconciliation:
    """Poll the order book until the order is seen, or the answer is as settled as it can be.

    FILLED    a completed copy exists (any further completed copies are reported as duplicates).
    ABSENT    the final read succeeded and showed no completed or working copy: safe to resend.
    UNCERTAIN the book was unreadable on the final read, or a copy is still working.

    `exclude` holds ids already booked by this engine, so an earlier same-size fill is not adopted.
    """
    detail = "order book never read"
    last_readable_clean = False
    ambiguous: tuple[str, ...] = ()
    adoptable: tuple[str, ...] = ()
    for attempt in range(1, attempts + 1):
        await asyncio.sleep(delay)
        book = await fetch_orderbook()
        if book is None:
            last_readable_clean = False
            detail = f"order book unreadable on attempt {attempt}/{attempts}"
            continue
        match = classify_book_detail(book, symbol, action, quantity, since, strategy, exclude)
        ambiguous = tuple(match.ambiguous)
        # Only a completed ambiguous copy with nothing definite still in flight may later be
        # adopted, and only on a broker-position proof (the caller's job).
        adoptable = tuple(match.ambiguous_complete) if not match.working else ()
        if match.complete:
            return Reconciliation(
                FILLED,
                match.complete[0],
                tuple(match.complete[1:]),
                "completed on broker",
                ambiguous_ids=ambiguous,
            )
        if match.working or ambiguous:
            last_readable_clean = False
            detail = (
                f"{match.working} matching order(s) still working, ambiguous {list(ambiguous)} "
                f"(stamp unreadable or at the send), on attempt {attempt}/{attempts}"
            )
            continue
        last_readable_clean = True
        detail = "no matching order on the broker"
    if last_readable_clean:
        return Reconciliation(ABSENT, detail=detail)
    return Reconciliation(
        UNCERTAIN, detail=detail, ambiguous_ids=ambiguous, ambiguous_complete_ids=adoptable
    )
