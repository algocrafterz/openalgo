"""Order construction and OpenAlgo API integration."""

import asyncio
import math
import time
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger

from signal_engine.config import settings
from signal_engine.features.instruments import safe_tick
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.timeutils import IST

# Float noise allowance when dividing a price by its tick (2079.7 / 0.1 is 20796.999999999996).
_TICK_EPS = 1e-9

#: Order statuses that can still execute. Matches tracker's working-order set.
_LIVE_ORDER_STATUSES = frozenset({"trigger pending", "open", "pending", "partially filled"})
_STOP_PRICE_TYPES = frozenset({"SL-M", "SL"})

#: (strategy, symbol, action) of stop submissions that timed out and were never resolved against
#: the order book. The next place_sl_order for the key reconciles before it sends anything.
class _UnconfirmedRegistry:
    """Set-like registry whose entries expire, so a stop that never landed cannot block the
    same strategy/symbol/side forever (including a fresh entry on a later day)."""

    def __init__(self, ttl_s: float = 1800.0) -> None:
        self._ttl = ttl_s
        self._at: dict[tuple[str, str, str], float] = {}

    def _expire(self) -> None:
        now = time.monotonic()
        for k in [k for k, t in self._at.items() if now - t > self._ttl]:
            del self._at[k]

    def add(self, key) -> None:
        self._at[key] = time.monotonic()

    def discard(self, key) -> None:
        self._at.pop(key, None)

    def clear(self) -> None:
        self._at.clear()

    def __contains__(self, key) -> bool:
        self._expire()
        return key in self._at

    def __bool__(self) -> bool:
        self._expire()
        return bool(self._at)


_UNCONFIRMED_SL = _UnconfirmedRegistry()

#: Order states the broker reports for an order that will never execute.
_DEAD_ORDER_STATUSES = frozenset({"cancelled", "canceled", "rejected", "expired"})


@dataclass
class _PendingDuplicate:
    """A strategy/symbol/side whose duplicate protective stops are not yet proved gone."""

    keep_id: str  # the stop to keep; "" = none chosen yet, the resolver picks one live stop
    last_try: float = 0.0
    since: float = 0.0  # time.monotonic() when first recorded
    failures: int = 0  # consecutive resolver passes that could not prove the key clean
    alerted: bool = False  # an operator alert for this key has been handed out


class _PendingDuplicateRegistry:
    """Stops that may be duplicated and were not proved removed by a fresh order-book read.

    A cancel request that fails or times out says nothing about the order, so a duplicate that
    cannot be proved gone stays here and resolve_pending_duplicate_stops() retries it. Entries
    only leave when a book read shows no live duplicate. Bounded by strategies x symbols x 2.
    """

    def __init__(self) -> None:
        self._items: dict[tuple, _PendingDuplicate] = {}

    def add(self, key, keep_id: str = "") -> None:
        entry = self._items.get(key)
        if entry is None:
            self._items[key] = _PendingDuplicate(keep_id=keep_id, since=time.monotonic())
        elif keep_id:
            entry.keep_id = keep_id

    def get(self, key) -> "_PendingDuplicate | None":
        return self._items.get(key)

    def discard(self, key) -> None:
        self._items.pop(key, None)

    def keys(self) -> list:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()

    def __contains__(self, key) -> bool:
        return key in self._items

    def __bool__(self) -> bool:
        return bool(self._items)


_PENDING_DUPLICATE_STOPS = _PendingDuplicateRegistry()

#: Minimum gap between cleanup retries for one key (the order book endpoint is rate limited).
_DUPLICATE_RETRY_INTERVAL_S = 15.0

#: Resolver passes that fail in a row before the key is escalated to the operator.
_ESCALATE_AFTER_FAILURES = 3

#: How long a key may stay pending only because its kept stop has not shown up on the book. After
#: that the key is dropped with an error log; the tracker's periodic audit re-flags real duplicates.
_KEEP_LAG_TTL_S = 600.0

#: place_sl_order result message for "an earlier submission is unresolved, nothing was sent".
HOLD_MESSAGE = "earlier SL submission unresolved; held"

#: (strategy, symbol, action) -> time.monotonic() of the last stop submission whose outcome is
#: UNKNOWN (timed out, nothing visible on the book). It may still land, so a new cycle waits
#: _RESEND_GRACE_S before sending again, and one cycle sends at most _MAX_UNKNOWN_SENDS such
#: submissions. A CONFIRMED failure (rejection, HTTP error) is not counted: retrying it cannot
#: duplicate anything. INFY 2026-10-15: five unknown sends per call plus the tracker's retries
#: left nine live stops on a 48-share position. Both values are policy candidates, tested by
#: patching them, not production-tuned numbers.
_LAST_UNKNOWN_SEND: dict[tuple, float] = {}
_RESEND_GRACE_S = 25.0
_MAX_UNKNOWN_SENDS = 2

#: (strategy, symbol, action) -> when the first unresolved submission of the current episode began.
#: A stop that landed AND fired after that moment is adopted instead of replaced.
_EPISODE_START: dict[tuple, datetime] = {}

#: loop -> {(strategy, symbol, action): Lock}. Locks bind to the loop that first waits on them,
#: so they are kept per loop; one stop submission per key runs at a time.
_STOP_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _stop_lock(key) -> asyncio.Lock:
    locks = _STOP_LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock


def _end_episode(key) -> None:
    _EPISODE_START.pop(key, None)
    _LAST_UNKNOWN_SEND.pop(key, None)


def round_to_tick(price: float, direction: str = "nearest") -> float:
    """Round price to a valid NSE tick.

    NSE ticks are tiered by price (0.01 / 0.05 / 0.10 / 0.50 / 1.00 / 5.00, NSE/CMTR/67133) and
    the tier comes from the PREVIOUS month's last close, which is not known here. The price is
    rounded to the coarsest tick that could apply (features.instruments.safe_tick), which is also
    valid under every finer tier. A fixed 0.05 was wrong above Rs 1,000: TCS 2079.75 is not a
    multiple of 0.10.

    direction: 'nearest', 'down', or 'up'
    """
    tick = safe_tick(price)
    steps = price / tick
    if direction == "down":
        n = math.floor(steps + _TICK_EPS)
    elif direction == "up":
        n = math.ceil(steps - _TICK_EPS)
    else:
        n = math.floor(steps + 0.5 + _TICK_EPS)
    return round(n * tick, 2)


def _trigger_matches(order_trigger: float, wanted: float) -> bool:
    """True when a resting order's trigger is within one tick of the wanted trigger price."""
    return abs(order_trigger - wanted) <= safe_tick(wanted) + _TICK_EPS


def build_order(
    signal: Signal,
    quantity: int,
    order_type: str = "",
    exchange: str = "",
    product: str = "",
) -> Order:
    """Convert a Signal + quantity into an Order ready for OpenAlgo."""
    action = Action.BUY if signal.direction == Direction.LONG else Action.SELL
    otype = order_type or settings.order_type
    price = 0.0 if otype == "MARKET" else signal.entry

    return Order(
        symbol=signal.symbol,
        exchange=exchange or signal.exchange or settings.exchange,
        action=action,
        quantity=quantity,
        price=price,
        order_type=otype,
        product=product or signal.product or settings.product,
        strategy_tag=signal.strategy,
    )


async def send_order(order: Order) -> TradeResult:
    """Send order to OpenAlgo REST API asynchronously."""
    url = f"{settings.openalgo_base_url}/api/v1/placeorder"
    payload = _placeorder_payload(order)

    try:
        async with httpx.AsyncClient(timeout=settings.order_timeout) as client:
            response = await client.post(url, json=payload)
            data = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            return _interpret_placeorder(order, response, data)
    except httpx.TimeoutException:
        logger.error(f"Timeout sending order for {order.symbol}")
        return TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")
    except Exception as e:
        logger.error(f"Error sending order for {order.symbol}: {e}")
        return TradeResult(status=OrderStatus.ERROR, message=str(e))


def _placeorder_payload(order: Order) -> dict:
    """Map an Order onto the OpenAlgo placeorder request body."""
    payload = {
        "apikey": settings.openalgo_api_key,
        "strategy": order.strategy_tag,
        "symbol": order.symbol,
        "action": order.action.value,
        "exchange": order.exchange,
        "pricetype": order.order_type,
        "product": order.product,
        "quantity": order.quantity,
        "price": order.price,
    }
    if order.trigger_price > 0:
        payload["trigger_price"] = order.trigger_price
    return payload


def _interpret_placeorder(order: Order, response, data: dict) -> TradeResult:
    """Turn a placeorder response into a TradeResult.

    Deliberately avoids raise_for_status so the JSON error message survives, and
    treats a success status with a null orderid as a rejection — some brokers answer
    that way when the order was refused downstream.
    """
    if response.status_code >= 400:
        reason = data.get("message", response.text)
        mode = data.get("mode", "")
        mode_tag = f" [{mode}]" if mode else ""
        logger.warning(f"Order rejected for {order.symbol}{mode_tag}: {reason}")
        return TradeResult(status=OrderStatus.REJECTED, message=reason)

    if data.get("status") == "error":
        reason = data.get("message", "Unknown error")
        logger.warning(f"Order failed for {order.symbol}: {reason}")
        return TradeResult(status=OrderStatus.REJECTED, message=reason)

    raw_order_id = data.get("orderid")
    if raw_order_id is None:
        reason = data.get("message", "Order returned success but no order ID")
        logger.warning(f"Order rejected for {order.symbol}: {reason}")
        return TradeResult(status=OrderStatus.REJECTED, message=reason)

    return TradeResult(
        order_id=str(raw_order_id),
        status=OrderStatus.SUCCESS,
        message=str(data.get("status", "")),
    )


def build_exit_order(
    symbol: str,
    exchange: str,
    quantity: int,
    product: str,
    strategy_tag: str,
    direction: "Direction" = None,
) -> Order:
    """Build a MARKET order to close an existing position.

    For LONG positions: SELL to close.
    For SHORT positions: BUY to cover.
    """
    from signal_engine.models import Direction as _Direction

    if direction is None:
        direction = _Direction.LONG
    action = Action.SELL if direction == _Direction.LONG else Action.BUY
    return Order(
        symbol=symbol,
        exchange=exchange,
        action=action,
        quantity=quantity,
        price=0.0,
        order_type="MARKET",
        product=product,
        strategy_tag=strategy_tag,
    )


async def place_sl_order(
    symbol: str,
    exchange: str,
    direction: "Direction",
    quantity: int,
    sl_price: float,
    product: str,
    strategy_tag: str,
) -> TradeResult:
    """Place a SL-M order with retries. Reusable for initial bracket and re-placement after partial exit.

    For LONG positions: SELL SL-M with trigger at sl_price (rounded up — fires earlier).
    For SHORT positions: BUY SL-M with trigger at sl_price (rounded down — fires earlier).

    Returns TradeResult — caller must check .status == OrderStatus.SUCCESS.
    """
    from signal_engine.models import Direction as _Direction

    action = Action.SELL if direction == _Direction.LONG else Action.BUY
    # Conservative rounding: trigger SL before the stated price to minimise loss
    # LONG SL is below entry — round UP so trigger fires before reaching stated price
    # SHORT SL is above entry — round DOWN so trigger fires before reaching stated price
    sl_direction = "up" if direction == _Direction.LONG else "down"
    trigger_price = round_to_tick(sl_price, sl_direction)

    sl_order = Order(
        symbol=symbol,
        exchange=exchange,
        action=action,
        quantity=quantity,
        price=0.0,
        order_type=settings.bracket_sl_order_type,
        product=product,
        strategy_tag=strategy_tag,
        trigger_price=trigger_price,
    )

    key = (strategy_tag.upper(), symbol.upper(), action.value)
    call_started = datetime.now(IST).replace(microsecond=0)  # broker order stamps are whole seconds
    # One submission per strategy/symbol/side at a time: a second caller (the tracker's missing-stop
    # retry) must see what the first one left on the book, not race it.
    async with _stop_lock(key):
        return await _place_sl_order_locked(
            sl_order, trigger_price, symbol, strategy_tag, action, key, call_started
        )


async def _place_sl_order_locked(
    sl_order: Order,
    trigger_price: float,
    symbol: str,
    strategy_tag: str,
    action: Action,
    key,
    call_started: datetime,
) -> TradeResult:
    if key in _UNCONFIRMED_SL:
        # An earlier submission for this stop timed out and was never resolved. It may have
        # landed (or even fired) since: adopt it, and send nothing without proof.
        found, readable = await _scan_resting_sl_order(
            sl_order, trigger_price, since=_EPISODE_START.get(key, call_started)
        )
        if found is not None:
            logger.info(
                f"SL for {symbol} adopted from an earlier unconfirmed submission: id={found.order_id}"
            )
            # Several submissions can land together (LICI 2026-10-15: five): keep one, remove the
            # rest. The unconfirmed flag stays until a book read proves the cleanup (see
            # _reconcile_pending_key), not merely because a cancel request was sent.
            await _remove_duplicate_stops(symbol, strategy_tag, action.value, found.order_id)
            _end_episode(key)
            return found
        if readable and time.monotonic() - _LAST_UNKNOWN_SEND.get(key, float("-inf")) < _RESEND_GRACE_S:
            # Reconciled just now: nothing is on the book, but the last submission's outcome is
            # still unknown and it may yet land. Sending again is the duplicate-stop path.
            logger.warning(
                f"SL for {symbol}: an earlier submission timed out under {_RESEND_GRACE_S:.0f}s ago and "
                "is not on the book yet - holding, the position stays under watch"
            )
            return TradeResult(status=OrderStatus.TIMEOUT, message=HOLD_MESSAGE)
        if not readable:
            logger.error(
                f"SL for {symbol}: earlier submission unconfirmed and the order book is "
                "unreadable - not re-sending"
            )
            return TradeResult(
                status=OrderStatus.TIMEOUT,
                message="earlier SL submission unconfirmed; order book unreadable",
            )

    ambiguous = key in _UNCONFIRMED_SL
    result: TradeResult = TradeResult(status=OrderStatus.ERROR, message="Not attempted")
    attempts_made = 0
    unknown_sends = 0
    for attempt in range(1, settings.bracket_max_sl_retries + 1):
        attempts_made = attempt
        result = await send_order(sl_order)
        if result.status == OrderStatus.SUCCESS:
            logger.info(
                f"SL placed for {symbol}: id={result.order_id} trigger={trigger_price} (attempt {attempt})"
            )
            break
        logger.warning(
            f"SL attempt {attempt}/{settings.bracket_max_sl_retries} failed for {symbol}: {result.message}"
        )
        if result.status == OrderStatus.TIMEOUT:
            # A client-side TIMEOUT does not mean the broker never got the order — only
            # that the response never arrived in time. Blindly retrying stacks duplicate
            # LIVE SL-M orders on top of one that actually went through. Confirmed
            # 2026-09-24: 5 consecutive "failed" SL retries each for NAUKRI and SOLARINDS
            # had ALL actually succeeded on the broker — every attempt reported TIMEOUT,
            # yet the orderbook showed 5 resting SL-M orders per symbol. Check before
            # retrying (or giving up) so a genuine broker fill is adopted, not duplicated.
            ambiguous = True
            _UNCONFIRMED_SL.add(key)
            _EPISODE_START.setdefault(key, call_started)
            recovered, readable = await _await_resting_sl_order(
                sl_order, trigger_price, since=_EPISODE_START[key]
            )
            if recovered is not None:
                result = recovered
                logger.info(
                    f"SL for {symbol} confirmed on the broker despite client timeout: "
                    f"id={recovered.order_id} (attempt {attempt})"
                )
                break
            if not readable:
                # No proof either way, and a timeout is not a rejection: a retry could stack
                # a second live stop. The key stays flagged, so the next call reconciles first.
                logger.error(
                    f"SL attempt {attempt} for {symbol} unconfirmed and the order book is "
                    "unreadable - not retrying"
                )
                break
            _LAST_UNKNOWN_SEND[key] = time.monotonic()
            unknown_sends += 1
            if unknown_sends >= _MAX_UNKNOWN_SENDS:
                logger.error(
                    f"SL for {symbol}: {unknown_sends} submissions have an unknown outcome and none is on "
                    "the book - no further send this cycle; the next cycle reconciles first"
                )
                break
        if attempt < settings.bracket_max_sl_retries:
            await asyncio.sleep(settings.bracket_retry_delay)

    if result.status != OrderStatus.SUCCESS:
        logger.error(f"SL failed after {attempts_made} attempts for {symbol} — no SL protection")
        if ambiguous:
            # Timed-out requests can still land: INFY 2026-10-15 ended with nine live stops after
            # "no SL protection" was logged. Keep watching this key until the book is settled.
            _PENDING_DUPLICATE_STOPS.add(key, "")
    else:
        if ambiguous:
            # An earlier timed-out request can land after the retry that replaced it (HINDALCO
            # 2026-10-14, 6 ms apart). Keep the confirmed order, cancel any twin. The unconfirmed
            # flag is cleared only when a book read proves no twin is left.
            await _remove_duplicate_stops(symbol, strategy_tag, action.value, result.order_id)
        _end_episode(key)

    return result


async def adopt_resting_stop(
    symbol: str,
    exchange: str,
    direction: "Direction",
    quantity: int,
    sl_price: float,
    product: str,
    strategy_tag: str,
    fetch_book=None,
) -> TradeResult | None:
    """Reconcile only: adopt a matching stop that landed late, send nothing.

    For a position whose submissions all ended unknown. Returns the stop (twins removed) or
    None when none is on the book or the book cannot be read.
    """
    from signal_engine.models import Direction as _Direction

    action = Action.SELL if direction == _Direction.LONG else Action.BUY
    trigger_price = round_to_tick(sl_price, "up" if direction == _Direction.LONG else "down")
    order = Order(
        symbol=symbol, exchange=exchange, action=action, quantity=quantity, price=0.0,
        order_type=settings.bracket_sl_order_type, product=product, strategy_tag=strategy_tag,
        trigger_price=trigger_price,
    )
    key = (strategy_tag.upper(), symbol.upper(), action.value)
    async with _stop_lock(key):
        found, _readable = await _scan_resting_sl_order(
            order, trigger_price, fetch_book, since=_EPISODE_START.get(key)
        )
        if found is None:
            return None
        logger.info(f"SL for {symbol} adopted by reconcile-only recovery: id={found.order_id}")
        await _remove_duplicate_stops(symbol, strategy_tag, action.value, found.order_id)
        _end_episode(key)
        return found


def flag_duplicate_stop_groups(book: list, keep_ids: dict | None = None) -> list[tuple]:
    """Put every exact-twin stop group on the book on the pending list. Idempotent.

    `keep_ids` maps a key to the stop the caller tracks, which is preferred when it is one of the
    twins. Returns the keys flagged.
    """
    keep_ids = keep_ids or {}
    flagged = []
    for key, ids in find_duplicate_stop_groups(book).items():
        wanted = keep_ids.get(key, "")
        _PENDING_DUPLICATE_STOPS.add(key, wanted if wanted in ids else "")
        flagged.append(key)
    return flagged


async def _remove_duplicate_stops(symbol: str, strategy: str, action: str, keep_id: str) -> None:
    """Remove every live protective order except `keep_id`, and say only what the book proves.

    A cancel request result is not an order state (INFY 2026-10-15: all five cancel requests timed
    out). Success is claimed only for orders a fresh book read shows cancelled. Anything else is
    logged as unresolved and stays on the pending list for resolve_pending_duplicate_stops().
    """
    key = (strategy.upper(), symbol.upper(), action.upper())
    await asyncio.sleep(float(getattr(settings, "sl_timeout_settle_seconds", 3.0)))
    _PENDING_DUPLICATE_STOPS.add(key, keep_id)
    await resolve_pending_duplicate_stops(force=True, only=key)


def find_duplicate_stop_groups(book: list) -> dict[tuple, list[str]]:
    """Live protective orders that are exact twins: same strategy, symbol, side, quantity, trigger.

    Stateless, so it works after a restart. A key is reported only when ALL of its live stops are
    twins of one another, so a strategy's deliberately different stops are never touched.
    Returns {(strategy, symbol, action): [order ids]} with at least two ids each.
    """
    by_key: dict[tuple, list[dict]] = {}
    for o in book or []:
        if not isinstance(o, dict) or not _order_id(o):
            continue
        if str(o.get("pricetype") or o.get("price_type") or "").upper() not in _STOP_PRICE_TYPES:
            continue
        if _order_status(o) not in _LIVE_ORDER_STATUSES:
            continue
        key = (str(o.get("strategy", "")).upper(), str(o.get("symbol", "")).upper(),
               str(o.get("action", "")).upper())
        if not key[0]:
            continue  # an untagged row cannot be attributed to a strategy
        by_key.setdefault(key, []).append(o)
    groups: dict[tuple, list[str]] = {}
    for key, rows in by_key.items():
        if len(rows) < 2:
            continue
        sigs = {(str(r.get("quantity")), str(r.get("trigger_price"))) for r in rows}
        if len(sigs) == 1:
            groups[key] = [_order_id(r) for r in rows]
    return groups


def escalations() -> list[tuple]:
    """Pending keys that failed to settle repeatedly and have not been handed to an operator yet.

    Marks them alerted; the caller sends the alert. A key stays pending (and keeps being retried)
    after it is escalated.
    """
    out = []
    for key in _PENDING_DUPLICATE_STOPS.keys():
        entry = _PENDING_DUPLICATE_STOPS.get(key)
        if entry is not None and entry.failures >= _ESCALATE_AFTER_FAILURES and not entry.alerted:
            entry.alerted = True
            out.append(key)
    return out


async def resolve_pending_duplicate_stops(force: bool = False, only: tuple | None = None) -> list:
    """Retry duplicate-stop cleanup that an earlier call could not prove. Returns the keys settled.

    Safe to call every poll: an empty registry costs nothing, and each key is retried at most once
    per _DUPLICATE_RETRY_INTERVAL_S unless `force`. Fails closed: an unreadable book cancels
    nothing. It never cancels the last live stop: if the stop it meant to keep is not live, one
    live stop is promoted to be kept. A key whose submission is still in flight is skipped.
    """
    if not _PENDING_DUPLICATE_STOPS:
        return []
    from signal_engine import api_client

    settled: list = []
    for key in _PENDING_DUPLICATE_STOPS.keys():
        if only is not None and key != only:
            continue
        entry = _PENDING_DUPLICATE_STOPS.get(key)
        if entry is None:
            continue
        now = time.monotonic()
        if not force and entry.last_try and now - entry.last_try < _DUPLICATE_RETRY_INTERVAL_S:
            continue
        if only is None and _stop_lock(key).locked():
            continue  # a submission is in flight; an explicit `only` caller already owns the key
        entry.last_try = now
        book = await api_client.fetch_orderbook()
        if book is None:
            entry.failures += 1
            continue
        if await _reconcile_pending_key(key, entry, book, api_client):
            _PENDING_DUPLICATE_STOPS.discard(key)
            settled.append(key)
        else:
            entry.failures += 1
    return settled


async def _reconcile_pending_key(key: tuple, entry, book: list, api_client) -> bool:
    """One reconcile pass for one pending key against an already-read book. True when settled."""
    strategy, symbol, action = key
    live = [_order_id(o) for o in protective_orders(book, symbol, strategy, action)]
    status_by_id = {_order_id(o): _order_status(o) for o in book if isinstance(o, dict)}
    if len(live) <= 1:
        keep = entry.keep_id
        if not live or not keep or live[0] == keep:
            _UNCONFIRMED_SL.discard(key)
            return True
        if keep not in status_by_id and time.monotonic() - entry.since < _KEEP_LAG_TTL_S:
            return False  # the kept stop may still be landing; the lone live stop may be a twin
        if keep not in status_by_id:
            logger.error(
                f"Duplicate SL watch for {symbol} [{strategy}] dropped: kept stop {keep} never appeared "
                f"on the book in {_KEEP_LAG_TTL_S:.0f}s (live: {live})"
            )
        _UNCONFIRMED_SL.discard(key)
        return True
    keep = entry.keep_id if entry.keep_id in live else live[0]
    entry.keep_id = keep
    first_read = [book]  # reuse the read: the order book endpoint is rate limited

    async def fetch_book():
        return first_read.pop() if first_read else await api_client.fetch_orderbook()

    sweep = await cancel_live_protective_orders(
        symbol, strategy, action, keep_id=keep, fetch_book=fetch_book, cancel=api_client.cancel_order
    )
    if sweep.confirmed_cancelled:
        logger.warning(
            f"Duplicate SL for {symbol} confirmed cancelled by the order book: "
            f"{list(sweep.confirmed_cancelled)} (kept {keep})"
        )
    if sweep.executed:
        logger.error(
            f"Duplicate SL for {symbol} FILLED instead of cancelling: {list(sweep.executed)} - "
            "the position changed, verify it against the broker"
        )
    if sweep.clear:
        return True
    logger.error(
        f"Duplicate SL check for {symbol} could not be completed: unresolved={list(sweep.unresolved)} "
        f"(kept {keep}, cancel requested {list(sweep.cancelled)}, book readable={sweep.readable}); "
        "a duplicate stop may still be live and will be retried"
    )
    return False


async def _await_resting_sl_order(
    order: Order, trigger_price: float, since: datetime | None = None
) -> tuple[TradeResult | None, bool]:
    """Poll for the resting SL after a TIMEOUT, giving a backed-up server time to book it.

    A timeout can mean the OpenAlgo server is stalled, not that the order was dropped: on
    2026-10-01 the server sat ~48s behind on /api/strategy-pnl, so an immediate orderbook
    read found nothing, the SL was re-sent four times, and all five reached the sandbox
    once it caught up (net long 684 on a 171 short). Wait `sl_timeout_settle_seconds`
    before each of `sl_timeout_verify_attempts` reads so a late order is adopted, not duplicated.

    Returns (order or None, readable). `readable` is False when no read succeeded, so the
    caller cannot treat "not found" as proof that the order never landed.
    """
    attempts = int(getattr(settings, "sl_timeout_verify_attempts", 3))
    settle = float(getattr(settings, "sl_timeout_settle_seconds", 3.0))
    readable = False
    for _ in range(max(1, attempts)):
        await asyncio.sleep(settle)
        found, ok = await _scan_resting_sl_order(order, trigger_price, since=since)
        readable = readable or ok
        if found is not None:
            return found, True
    return None, readable


async def _find_resting_sl_order(order: Order, trigger_price: float) -> TradeResult | None:
    """Look for a resting SL-M order on the broker matching `order`, to recover from an
    ambiguous client-side TIMEOUT before place_sl_order retries and creates a duplicate.
    """
    return (await _scan_resting_sl_order(order, trigger_price))[0]


def _filled_since(row: dict, since: datetime) -> bool:
    """True when `row` carries a parseable broker timestamp at or after `since`.

    Stricter than api_client.find_recent_complete_order_id: an unparseable stamp does NOT match
    here, because adopting an unrelated old fill would leave the position without a stop.
    """
    # The shared parser reads the sandbox stamp and Flattrade's day-first stamps (and ISO with an
    # offset, converted to IST); a sandbox-only parse rejected every live Flattrade row.
    from signal_engine.order_reconcile import parse_broker_stamp

    stamp = parse_broker_stamp(row.get("timestamp") or row.get("order_timestamp"))
    return stamp is not None and stamp >= since


async def _scan_resting_sl_order(
    order: Order, trigger_price: float, fetch_book=None, since: datetime | None = None
) -> tuple[TradeResult | None, bool]:
    """One orderbook read. Returns (match or None, readable).

    Matches on symbol + action + quantity + trigger_price (within a tick) among orders
    still working (not yet filled/cancelled/rejected). `readable` is False when the book
    could not be read at all (an empty book is readable).
    """
    if fetch_book is None:
        from signal_engine.api_client import fetch_orderbook as fetch_book

    book = await fetch_book()
    if book is None:
        return None, False
    for o in book:
        if not isinstance(o, dict):
            continue
        if str(o.get("symbol", "")).upper() != order.symbol.upper():
            continue
        if str(o.get("action", "")).upper() != order.action.value:
            continue
        # Another strategy's stop on the same netted symbol is its own protection: never adopt it.
        # A row with no strategy tag is still matched (legacy rows, as before).
        row_strategy = str(o.get("strategy", "")).upper()
        if row_strategy and row_strategy != order.strategy_tag.upper():
            continue
        row_type = str(o.get("pricetype") or o.get("price_type") or "").upper()
        if row_type and row_type not in _STOP_PRICE_TYPES:
            continue
        try:
            if int(o.get("quantity", 0)) != order.quantity:
                continue
        except (TypeError, ValueError):
            continue
        try:
            if not _trigger_matches(float(o.get("trigger_price", 0) or 0), trigger_price):
                continue
        except (TypeError, ValueError):
            continue
        status = _order_status(o)
        fired = False
        if status not in _LIVE_ORDER_STATUSES:
            # A stop that landed and already triggered shows "complete": adopt it only when it
            # was filled after this episode began, so the tracker closes the position through its
            # normal stop-filled path instead of a new stop going onto a flat position.
            if not (status == "complete" and since is not None and _filled_since(o, since)):
                continue
            fired = True
        order_id = str(o.get("orderid") or o.get("order_id") or "")
        if order_id:
            return (
                TradeResult(
                    order_id=order_id,
                    status=OrderStatus.SUCCESS,
                    message="recovered after timeout (already triggered)" if fired else "recovered after timeout",
                ),
                True,
            )
    return None, True


def _order_status(row: dict) -> str:
    return str(row.get("order_status") or row.get("status") or "").lower().replace("_", " ")


def _order_id(row: dict) -> str:
    return str(row.get("orderid") or row.get("order_id") or "")


def protective_orders(book: list, symbol: str, strategy: str, action: str) -> list[dict]:
    """Live stop orders of one strategy on one symbol and side.

    Strategy is matched exactly: another strategy's stop on the same netted symbol is its own
    protection and must never be cancelled. A row with no strategy tag is not selected.
    """
    return [
        o
        for o in book
        if isinstance(o, dict)
        and str(o.get("symbol", "")).upper() == symbol.upper()
        and str(o.get("strategy", "")).upper() == strategy.upper()
        and str(o.get("action", "")).upper() == action.upper()
        and str(o.get("pricetype") or o.get("price_type") or "").upper() in _STOP_PRICE_TYPES
        and _order_status(o) in _LIVE_ORDER_STATUSES
        and _order_id(o)
    ]


@dataclass(frozen=True)
class StopSweep:
    """Outcome of cancel_live_protective_orders.

    clear: a fresh order book read shows no live protective order left (besides keep_id).
    readable: False when an order book read failed, so clear=False means "unknown", not "live".
    cancelled: ids a cancel REQUEST was sent for. This is not proof of anything: a request can
        fail yet the order is cancelled, or succeed yet the order stays live.
    confirmed_cancelled: ids the fresh verification read shows cancelled, rejected or expired.
    unresolved: ids still live in the verification read, or all targets when it was unreadable.
    executed: ids that were filled, fully or partly, by the time of the verification read -
        the position has changed and the caller must not trust its own quantity.
    """

    clear: bool
    cancelled: tuple[str, ...] = ()
    executed: tuple[str, ...] = ()
    readable: bool = True
    confirmed_cancelled: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


async def cancel_live_protective_orders(
    symbol: str,
    strategy: str,
    action: str,
    keep_id: str = "",
    *,
    fetch_book=None,
    cancel=None,
    settle_s: float = 0.5,
) -> StopSweep:
    """Cancel every live protective order of `strategy` on `symbol`, then verify with a fresh read.

    This does not trust a tracked order id: a timed-out stop request can land late and leave
    a second live stop the tracker never heard of. Fails closed: an unreadable book, or an
    order still live after the cancel, returns clear=False.
    """
    if fetch_book is None or cancel is None:
        from signal_engine import api_client

        fetch_book = fetch_book or api_client.fetch_orderbook
        cancel = cancel or api_client.cancel_order
    key = (strategy.upper(), symbol.upper(), action.upper())

    book = await fetch_book()
    if book is None:
        return StopSweep(clear=False, readable=False)
    targets = [_order_id(o) for o in protective_orders(book, symbol, strategy, action)]
    targets = [t for t in targets if t != keep_id]
    if not targets:
        _UNCONFIRMED_SL.discard(key)
        return StopSweep(clear=True)

    for order_id in targets:
        await cancel(order_id, strategy)

    still_live: list[str] = []
    for read in range(2):
        if read:
            await asyncio.sleep(settle_s)  # a lagging book can show a just-cancelled stop as live
        book = await fetch_book()
        if book is None:
            return StopSweep(
                clear=False, cancelled=tuple(targets), readable=False, unresolved=tuple(targets)
            )
        still_live = [
            _order_id(o) for o in protective_orders(book, symbol, strategy, action)
            if _order_id(o) != keep_id
        ]
        if not still_live:
            break
    filled = tuple(
        _order_id(o)
        for o in book
        if isinstance(o, dict)
        and _order_id(o) in targets
        and _order_status(o) in ("complete", "partially filled")
    )
    if not still_live:
        _UNCONFIRMED_SL.discard(key)
    status_by_id = {_order_id(o): _order_status(o) for o in book if isinstance(o, dict)}
    confirmed = tuple(t for t in targets if status_by_id.get(t) in _DEAD_ORDER_STATUSES)
    return StopSweep(
        clear=not still_live,
        cancelled=tuple(targets),
        executed=filled,
        confirmed_cancelled=confirmed,
        unresolved=tuple(t for t in targets if t in still_live),
    )


async def send_bracket_legs(
    signal: Signal,
    quantity: int,
    entry_order_id: str,
) -> tuple[TradeResult, TradeResult | None]:
    """Place SL leg only for a bracket order.

    Indian brokers treat the first SELL as an exit from the long, and any second
    SELL (even a TP LIMIT) as a new short position requiring full margin. Placing
    both SL and TP simultaneously therefore always results in one of them being
    rejected with FUND LIMIT INSUFFICIENT.

    Strategy: Place only the SL-M order here (safety-critical). TP exit is driven
    by TradingView TP HIT signal -> _handle_exit pipeline.

    Returns (sl_result, None) always — tp_result slot reserved for future use.
    """
    sl_result = await place_sl_order(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        direction=signal.direction,
        quantity=quantity,
        sl_price=signal.sl,
        product=signal.product or settings.product,
        strategy_tag=signal.strategy,
    )
    # TP exit via TradingView TP HIT signal -> _handle_exit (not a broker order)
    return sl_result, None
