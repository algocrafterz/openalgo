"""Shared request pacing for every Flattrade HTTP entry point.

Flattrade documents two separate ceilings
(broker-api-docs/flattrade-api-docs/14-rate-limits.md):

    order endpoints      10 req/sec   and   40 req/min
    non-order endpoints  40 req/sec   and  200 req/min

Before this module the dual sliding-window limiter lived inside
broker/flattrade/api/data.py and was reachable only from that file's
``get_api_response``. Everything else — order_api.py (which has its own
``get_api_response``, plus place/modify/cancel), funds.py and margin_api.py —
issued unpaced requests, so the orderbook/positions/funds polling that the UI
does continuously was never counted against either window, and
``close_all_positions`` / ``cancel_all_orders_api`` fired their per-order calls
in a bare ``for`` loop that can breach the 10/sec order limit outright. Both
limiters now live here and every Flattrade caller shares them, which is the only
way a shared window can actually be shared.

Two limiters, not one: the order ceiling is four times tighter, so pacing order
placement against the data window would let a burst of orders through, while
pacing quote fetches against the order window would throttle them pointlessly.

**Adaptive clamping.** The documented numbers are the ceiling for a
fully-provisioned account, not a promise about yours. Flattrade provisions some
accounts lower and reports it only in the rejection text:

    "Invalid Input : Order Recieved 11 in a current second exceeds Limit 10 for user"

``note_rate_limit_rejection`` parses that ``Limit N`` and clamps the offending
window for the rest of the process, so the retry that follows is not just a
slower repeat of a request the account was never allowed to make. The clamp only
ever ratchets downward, and is reported once at WARNING when it moves.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections import deque

from utils import broker_priority
from utils.broker_backpressure import BrokerBusyError, check_queue_wait
from utils.logging import get_logger

logger = get_logger(__name__)


def _env_int(name: str, default: int, ceiling: int) -> int:
    """Read a positive int from the environment, bounded by `ceiling`.

    `ceiling` is the highest figure Flattrade publishes for that window
    (14-rate-limits.md). An override exists so a higher-tier account is not
    pinned to the conservative default, but nothing legitimate sits above the
    published table, and an override that does is a typo (a stray zero on
    "380") that would silently disable the pacing this module exists to
    provide. Clamp rather than trust, and say so.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(f"{name}={raw!r} is not an integer; using {default}")
        return default
    if value < 1:
        logger.warning(f"{name}={value} must be >= 1; using {default}")
        return default
    if value > ceiling:
        logger.warning(
            f"{name}={value} is above Flattrade's published ceiling of {ceiling}; "
            f"using {ceiling}. Nothing is provisioned above the published table, "
            "so this is almost certainly a typo."
        )
        return ceiling
    return value


def _env_reserve(name: str, ceiling: int) -> int:
    """Read an optional lane size: 0 or unset means no lane, anything else is bounded."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return 0
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(f"{name}={raw!r} is not an integer; no reserved lane")
        return 0
    return max(0, min(value, ceiling))


def _env_seconds(name: str) -> float:
    """Read an optional wait bound in seconds: 0 or unset means no bound."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return 0.0
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning(f"{name}={raw!r} is not a number; no bound")
        return 0.0


# Flattrade's own rejection text, e.g.
#   "Order Recieved 11 in a current second exceeds Limit 10 for user"
# (the broker's spelling of "Received" is reproduced, not corrected — it is
# matched case-insensitively and only "exceeds Limit N" is actually captured).
_LIMIT_IN_MESSAGE = re.compile(r"exceeds\s+limit\s+(\d+)", re.IGNORECASE)
_PER_SECOND_HINT = re.compile(r"current\s+second", re.IGNORECASE)
_PER_MINUTE_HINT = re.compile(r"current\s+minute", re.IGNORECASE)


#: Seconds between the one-line limiter summaries written to the log.
SUMMARY_INTERVAL_SECONDS = 60.0

#: A queue wait of at least this long counts as a delayed call in the metrics.
DELAYED_WAIT_SECONDS = 1.0


class _ClassStats:
    """Queue-wait figures for one caller class over the current summary interval."""

    __slots__ = ("calls", "wait_sum", "wait_max", "delayed", "refused")

    def __init__(self) -> None:
        self.calls = 0
        self.wait_sum = 0.0
        self.wait_max = 0.0
        self.delayed = 0
        self.refused = 0

    def served(self, wait: float) -> None:
        self.calls += 1
        self.wait_sum += wait
        self.wait_max = max(self.wait_max, wait)
        if wait >= DELAYED_WAIT_SECONDS:
            self.delayed += 1

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "avg_wait": round(self.wait_sum / self.calls, 3) if self.calls else 0.0,
            "max_wait": round(self.wait_max, 3),
            "delayed": self.delayed,
            "refused": self.refused,
        }


class SlidingWindowLimiter:
    """Dual rolling-window limiter: bursts to the per-second cap, hard-capped
    on both windows.

    Reservation happens inside the lock; sleeping is the caller's job, outside
    the lock, so concurrent threads queue without serializing on the lock.

    The deque holds reserved call timestamps in non-decreasing order (each new
    reservation is >= the previous by construction: a full window pushes the new
    slot past entries the previous reservation also sat behind). The earliest
    permissible slot is:

      - now, if neither window is full at that instant;
      - 1s after the Nth-most-recent reservation when the per-second window is
        full (that entry must age out of the rolling second first);
      - 60s after the Mth-most-recent reservation when the per-minute window is
        full;

    whichever is latest. Entries older than 60s can no longer constrain any
    future slot and are purged.

    **Priority lanes (opt-in).** The broker budget can be split into up to three
    lanes, each keeping the rule above on its own deque and caps:

      - a *reserved* lane that only ``CRITICAL`` callers (orders, see
        ``utils.broker_priority``) may enter;
      - a *background* lane that is the only place ``BACKGROUND`` callers
        (position valuation, page refreshes) may book;
      - a *general* lane, the rest of the budget, for everyone except
        ``BACKGROUND``.

    A caller takes the lane that offers the earliest slot among those it may
    use: ``CRITICAL`` may use all three, ``NORMAL`` the general and background
    lanes (idle background capacity is not wasted), ``BACKGROUND`` only its own.
    Because every lane obeys its own caps and the lane caps sum to the broker's
    totals, the broker's per-second and per-minute limits are never exceeded,
    no volume of background work can take capacity from strategy calls or
    orders, and no volume of strategy calls can take the orders' lane. If no
    lane is configured the limiter behaves exactly as before. When the broker
    quota itself is spent a critical call still waits for its slot: the lane
    guarantees it is not queued behind other callers, not that the broker has
    room.

    **Bounded background wait (opt-in).** With ``background_max_wait`` > 0, a
    ``BACKGROUND`` caller whose slot is further away than that is refused with
    ``BrokerBusyError`` before anything is booked, so it neither waits long nor
    leaves a reservation behind.
    """

    def __init__(
        self,
        name: str,
        max_per_second: int,
        max_per_minute: int,
        kind: str = "data",
        reserve_per_second: int = 0,
        reserve_per_minute: int = 0,
        background_max_wait: float = 0.0,
        background_per_second: int = 0,
        background_per_minute: int = 0,
    ):
        self.name = name
        self.max_per_second = max_per_second
        self.max_per_minute = max_per_minute
        #: Which wait bound applies under the gthread worker: "data" or "order"
        #: (utils.broker_backpressure).
        self.kind = kind
        if reserve_per_second < 1 or reserve_per_minute < 1:
            # A lane needs both windows. Half a lane would either starve it or
            # silently disable the second window, so neither is created.
            reserve_per_second = reserve_per_minute = 0
        if background_per_second < 1 or background_per_minute < 1:
            background_per_second = background_per_minute = 0
        self.reserve_per_second = reserve_per_second
        self.reserve_per_minute = reserve_per_minute
        self.background_per_second = background_per_second
        self.background_per_minute = background_per_minute
        self.background_max_wait = max(0.0, float(background_max_wait))
        self._lock = threading.Lock()
        #: The general lane. With no other lane configured it is the whole budget.
        self._reserved: deque[float] = deque()
        self._reserved_lane: deque[float] = deque()
        self._background_lane: deque[float] = deque()
        self._classes = {
            broker_priority.CRITICAL: _ClassStats(),
            broker_priority.NORMAL: _ClassStats(),
            broker_priority.BACKGROUND: _ClassStats(),
        }
        self._rejections = 0
        self._timeouts = 0
        self._latency_calls = 0
        self._latency_sum = 0.0
        self._latency_max = 0.0
        self._interval_started = time.time()

    def _partition(self) -> dict[str, tuple[int, int]]:
        """Per-second and per-minute caps for each lane in use, from the current totals.

        Each configured lane is held to a third of the current total, so an adaptive
        clamp that lowers the broker cap never leaves the general lane under a third.
        """
        total_s, total_m = self.max_per_second, self.max_per_minute
        lanes: dict[str, tuple[int, int]] = {}
        taken_s = taken_m = 0
        for name, want_s, want_m in (
            (broker_priority.CRITICAL, self.reserve_per_second, self.reserve_per_minute),
            (broker_priority.BACKGROUND, self.background_per_second, self.background_per_minute),
        ):
            lane_s = min(want_s, total_s // 3)
            lane_m = min(want_m, total_m // 3)
            if lane_s >= 1 and lane_m >= 1:
                lanes[name] = (lane_s, lane_m)
                taken_s += lane_s
                taken_m += lane_m
        lanes[broker_priority.NORMAL] = (total_s - taken_s, total_m - taken_m)
        return lanes

    def _deque_for(self, lane: str) -> deque:
        return {
            broker_priority.CRITICAL: self._reserved_lane,
            broker_priority.BACKGROUND: self._background_lane,
        }.get(lane, self._reserved)

    @staticmethod
    def _slot(booked: deque, now: float, per_second: int, per_minute: int) -> float:
        slot = now
        if len(booked) >= per_second:
            slot = max(slot, booked[-per_second] + 1.0)
        if len(booked) >= per_minute:
            slot = max(slot, booked[-per_minute] + 60.0)
        return slot

    def reserve(self, priority: str | None = None) -> float:
        """Reserve the earliest slot satisfying both windows.

        Returns the seconds the caller must sleep before issuing its request.

        Past the per-minute cap every extra caller is booked about one slot
        after the previous one, without limit. Under the gthread worker each
        of those sleeps holds a request thread, so a caller whose slot is
        further away than ``max_queue_wait(self.kind)`` is refused instead,
        before anything is booked, and delays nobody behind it. Under eventlet
        and the dev server there is no bound and every caller is booked as
        before, unless ``background_max_wait`` is set.

        Args:
            priority: ``broker_priority`` class of the caller. Defaults to the
                class set by the surrounding ``broker_priority`` context.

        Raises:
            BrokerBusyError: Under gthread, when the slot is too far away, or
                for a background caller beyond ``background_max_wait``.
        """
        if priority is None:
            priority = broker_priority.current()
        summary = None
        with self._lock:
            now = time.time()
            for booked in (self._reserved, self._reserved_lane, self._background_lane):
                while booked and booked[0] <= now - 60.0:
                    booked.popleft()

            lanes = self._partition()
            if priority == broker_priority.CRITICAL:
                usable = (broker_priority.CRITICAL, broker_priority.NORMAL, broker_priority.BACKGROUND)
            elif priority == broker_priority.BACKGROUND and broker_priority.BACKGROUND in lanes:
                usable = (broker_priority.BACKGROUND,)
            elif priority == broker_priority.BACKGROUND:
                usable = (broker_priority.NORMAL,)
            else:
                usable = (broker_priority.NORMAL, broker_priority.BACKGROUND)

            target, slot = None, None
            for lane in usable:  # own lane first: a tie keeps borrowed capacity borrowed
                if lane not in lanes:
                    continue
                booked = self._deque_for(lane)
                lane_slot = self._slot(booked, now, *lanes[lane])
                if slot is None or lane_slot < slot:
                    target, slot = booked, lane_slot

            wait = slot - now
            stats = self._classes.get(priority) or self._classes[broker_priority.NORMAL]
            if (
                priority == broker_priority.BACKGROUND
                and self.background_max_wait > 0
                and wait > self.background_max_wait
            ):
                stats.refused += 1
                raise BrokerBusyError(retry_after=wait)
            check_queue_wait(wait, self.kind)
            target.append(slot)
            stats.served(wait)
            if now - self._interval_started >= SUMMARY_INTERVAL_SECONDS:
                summary = self._summary_line_locked(now)
        if summary:
            logger.info(summary)
        return wait

    def acquire(self) -> None:
        """Block until this caller's reserved slot arrives."""
        sleep_time = self.reserve()
        if sleep_time > 0:
            logger.debug(
                f"Rate limiting ({self.name}): sleeping {sleep_time:.2f}s "
                "before Flattrade API call"
            )
            time.sleep(sleep_time)

    async def acquire_async(self) -> None:
        """Same reservation, awaiting asyncio.sleep so the loop is not blocked."""
        sleep_time = self.reserve()
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

    def clamp_per_second(self, limit: int) -> bool:
        """Lower the per-second cap to `limit`. Ratchets down only.

        Returns True when the cap actually moved.
        """
        with self._lock:
            if limit < 1 or limit >= self.max_per_second:
                return False
            previous = self.max_per_second
            self.max_per_second = limit
        logger.warning(
            f"Flattrade reported a per-second limit of {limit} for the {self.name} "
            f"endpoints; lowering the local cap from {previous} to {limit} for the "
            "rest of this process. Set FLATTRADE_MAX_PER_SECOND to pin this "
            "explicitly and avoid the first rejection."
        )
        return True

    def clamp_per_minute(self, limit: int) -> bool:
        """Lower the per-minute cap to `limit`. Ratchets down only."""
        with self._lock:
            if limit < 1 or limit >= self.max_per_minute:
                return False
            previous = self.max_per_minute
            self.max_per_minute = limit
        logger.warning(
            f"Flattrade reported a per-minute limit of {limit} for the {self.name} "
            f"endpoints; lowering the local cap from {previous} to {limit} for the "
            "rest of this process."
        )
        return True

    # -- Metrics -------------------------------------------------------------

    def record_rejection(self) -> None:
        """Count a rate-limit rejection the broker sent back."""
        with self._lock:
            self._rejections += 1

    def record_timeout(self) -> None:
        """Count a broker request that timed out."""
        with self._lock:
            self._timeouts += 1

    def record_latency(self, seconds: float) -> None:
        """Record how long one broker request took, after its slot arrived."""
        with self._lock:
            self._latency_calls += 1
            self._latency_sum += seconds
            self._latency_max = max(self._latency_max, seconds)

    def snapshot(self) -> dict:
        """Figures for the current summary interval, by caller class."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        snap = {name: stats.as_dict() for name, stats in self._classes.items()}
        snap["rejections"] = self._rejections
        snap["timeouts"] = self._timeouts
        snap["latency"] = {
            "calls": self._latency_calls,
            "avg": round(self._latency_sum / self._latency_calls, 3)
            if self._latency_calls
            else 0.0,
            "max": round(self._latency_max, 3),
        }
        snap["cap"] = {"per_second": self.max_per_second, "per_minute": self.max_per_minute}
        return snap

    def _summary_line_locked(self, now: float) -> str:
        snap = self._snapshot_locked()
        parts = []
        for name in (broker_priority.CRITICAL, broker_priority.NORMAL, broker_priority.BACKGROUND):
            c = snap[name]
            parts.append(
                f"{name} n={c['calls']} avg_wait={c['avg_wait']}s max_wait={c['max_wait']}s "
                f"delayed={c['delayed']} refused={c['refused']}"
            )
        line = (
            f"Flattrade {self.name} limiter, last {now - self._interval_started:.0f}s: "
            + "; ".join(parts)
            + f"; broker_rejections={snap['rejections']} timeouts={snap['timeouts']} "
            f"latency_avg={snap['latency']['avg']}s latency_max={snap['latency']['max']}s "
            f"cap={self.max_per_second}/s,{self.max_per_minute}/min"
        )
        for stats in self._classes.values():
            stats.__init__()
        self._rejections = self._timeouts = self._latency_calls = 0
        self._latency_sum = self._latency_max = 0.0
        self._interval_started = now
        return line


# Non-order defaults are set from OBSERVED enforcement, not from the published
# table, because the two disagree.
#
# 14-rate-limits.md publishes 40/sec and 200/min. A live account on 2026-09-01
# was rejected at both a much lower per-second and per-minute ceiling, and the
# rejection text named them:
#
#   "Invalid Input :  Order Recieved 11 in a current second exceeds Limit 10 for user"
#
# giving 10/sec and 120/min. Those are exactly the figures TradeSmart — the same
# Noren backend — publishes for its general budget
# (broker/tradesmart/api/rate_limiter.py: 10/sec, 120/min), which is strong
# evidence that 10/120 is the Noren platform's native shape and that the 40/200
# in Flattrade's docs describes a higher provisioning tier rather than the
# default one.
#
# Defaulting to the published 38/190 meant the adaptive clamp below had to
# discover the truth by being rejected, and since the clamp is process-local
# every restart paid that rejection again (observed twice in one morning:
# "lowering the local cap from 38 to 10" at 11:13 and again at 11:22 after a
# restart). Starting at the real ceiling costs nothing on a correctly
# provisioned account beyond throughput that account was never going to get,
# and costs a burst of failed quotes on every other one.
#
# 9/110 rather than 10/120: the same margin TradeSmart uses, for clock skew
# between our rolling windows and however Noren measures theirs.
#
# Accounts on the higher tier (see 01-introduction.md, "More Than 10 Orders Per
# Second") should raise these with FLATTRADE_MAX_PER_SECOND /
# FLATTRADE_MAX_PER_MINUTE — the clamp only ever ratchets down, so a raised cap
# is still protected by it.
DATA_LIMITER = SlidingWindowLimiter(
    "data",
    max_per_second=_env_int("FLATTRADE_MAX_PER_SECOND", 9, ceiling=40),
    max_per_minute=_env_int("FLATTRADE_MAX_PER_MINUTE", 110, ceiling=200),
    # Opt-in, off unless set: a lane of this size per second and per minute that only
    # order-critical callers (utils.broker_priority) may use, a capped lane for background
    # callers, and a wait bound after which background callers are refused.
    # Suggested: reserve 2 and 10, background 1 and 20, bound 3.
    reserve_per_second=_env_reserve("FLATTRADE_DATA_RESERVE_PER_SECOND", ceiling=20),
    reserve_per_minute=_env_reserve("FLATTRADE_DATA_RESERVE_PER_MINUTE", ceiling=100),
    background_max_wait=_env_seconds("FLATTRADE_BACKGROUND_MAX_WAIT_SECONDS"),
    background_per_second=_env_reserve("FLATTRADE_DATA_BACKGROUND_PER_SECOND", ceiling=20),
    background_per_minute=_env_reserve("FLATTRADE_DATA_BACKGROUND_PER_MINUTE", ceiling=100),
)

# Documented order ceiling is 10/sec and 40/min, which matches the observed
# per-second figure above, so the published numbers are kept here; 9/38 for the
# same margin.
ORDER_LIMITER = SlidingWindowLimiter(
    "order",
    max_per_second=_env_int("FLATTRADE_ORDER_MAX_PER_SECOND", 9, ceiling=10),
    max_per_minute=_env_int("FLATTRADE_ORDER_MAX_PER_MINUTE", 38, ceiling=40),
    kind="order",
)


def is_rate_limit_error(response) -> bool:
    """Return True when a parsed Flattrade response is a rate-limit rejection."""
    if not isinstance(response, dict):
        return False
    if response.get("stat") != "Not_Ok":
        return False
    emsg = str(response.get("emsg", ""))
    return "exceeds limit" in emsg.lower()


MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BASE_DELAY = 2.0  # seconds, doubled per attempt: 2, 4, 8


def rate_limit_retry_delay(
    response,
    limiter: SlidingWindowLimiter,
    retry_count: int,
    context: str = "",
) -> float | None:
    """Clamp from a rejection and return how long to wait before retrying.

    Returns None when the response was not a rate-limit rejection, or when the
    retry budget is spent — in both cases the caller returns the response.

    The recursive re-issue stays at the call site because each one has a
    different signature and one of them is async; everything that was actually
    duplicated (detection, clamping, the attempt count, the backoff curve and
    the warning) lives here. Retry policy is now changed in one place instead
    of the five copies this replaced.

    Order endpoints deliberately do not use this: see clamp_from_response.
    """
    if not clamp_from_response(response, limiter):
        return None
    if retry_count >= MAX_RATE_LIMIT_RETRIES:
        logger.warning(
            f"Flattrade rate limit still hit after {MAX_RATE_LIMIT_RETRIES} "
            f"retries{f' on {context}' if context else ''}; giving up. "
            f"The {limiter.name} cap is now {limiter.max_per_second}/sec, "
            f"{limiter.max_per_minute}/min."
        )
        return None

    delay = RATE_LIMIT_BASE_DELAY * (2**retry_count)
    emsg = response.get("emsg") if isinstance(response, dict) else None
    logger.warning(
        f"Flattrade rate limit hit{f' on {context}' if context else ''} ({emsg}). "
        f"Retrying in {delay}s (attempt {retry_count + 1}/{MAX_RATE_LIMIT_RETRIES})"
    )
    return delay


def clamp_from_response(response, limiter: SlidingWindowLimiter) -> bool:
    """Learn from `response` if it is a rate-limit rejection.

    Returns True when it was one, so the caller can decide whether to retry.
    Combines the detect-then-clamp pair every Flattrade entry point needs, so a
    call site cannot pace its requests but forget to adapt (which is exactly
    what order_api.py, funds.py and margin_api.py did).

    Note for order endpoints: clamp, but do NOT auto-retry. A PlaceOrder that
    was rejected for rate is safe to retry, but this code cannot distinguish
    that from a response lost after the broker accepted it, and a duplicate
    order is far worse than a failed one. Read-only endpoints retry; order
    endpoints surface the error.
    """
    if not is_rate_limit_error(response):
        return False
    limiter.record_rejection()
    note_rate_limit_rejection(response, limiter)
    return True


def note_rate_limit_rejection(response, limiter: SlidingWindowLimiter) -> None:
    """Teach `limiter` the ceiling Flattrade just named in its rejection.

    A no-op unless the message actually carries a "Limit N", so an unparseable
    rejection simply leaves the configured caps alone.
    """
    if not isinstance(response, dict):
        return
    emsg = str(response.get("emsg", ""))
    match = _LIMIT_IN_MESSAGE.search(emsg)
    if not match:
        return
    try:
        limit = int(match.group(1))
    except (TypeError, ValueError):
        return

    # The message says which window it tripped. When it says neither, assume
    # per-second: that is the window a burst trips first, and clamping it also
    # holds the per-minute rate down.
    if _PER_MINUTE_HINT.search(emsg) and not _PER_SECOND_HINT.search(emsg):
        limiter.clamp_per_minute(limit)
    else:
        limiter.clamp_per_second(limit)
