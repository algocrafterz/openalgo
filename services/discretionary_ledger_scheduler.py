# services/discretionary_ledger_scheduler.py
"""
Single-flight scheduler for the discretionary ledger sync.

Thread model: one named single-worker executor ("discretionary-ledger") runs every sync,
and one daemon ticker thread decides when a sync is due. At most one sync is in flight;
a trigger that arrives while one runs is dropped. Nothing here runs on a request thread
and a failure only logs - it never reaches Live/Paper or the order path.

Triggers: boot catch-up (once, at start), periodic during market hours, one end-of-day
sync, a manual trigger function (no HTTP route in Stage 1), and order events: the
discretionary ledger subscriber (subscribers/discretionary_ledger_subscriber.py) calls
trigger_sync("order_event") a debounce window after a live fill is seen. A successful
order-event sync (every account ok or partial) moves the periodic timer: the next periodic
sync is a full interval after it. It records ledger_sync_runs rows exactly like a periodic
sync (same sync_all path), so it is a same-day heartbeat too. Order-event syncs run only in
the periodic window (see event_sync_allowed).

UNSUPPORTED FOR NOW (user decision): MCX evening collection, and any collection outside
NSE/BSE equity hours. Nothing here syncs for the MCX evening session (17:00 to 23:55) and
no periodic sync runs after 15:30. Fills made outside the sync windows can be LOST, because
the broker TradeBook in use today is a current-day book whose reset time is NOT verified.

Sync windows: periodic 09:15 to 15:30 (DISCRETIONARY_LEDGER_INTERVAL_MIN, default 15), one
end-of-day sync at DISCRETIONARY_LEDGER_EOD_TIME (HH:MM IST, default 16:15, after the NSE
closing session 15:40/15:50 to 16:00; the exchange trade-modification cut-off is 16:15), and a
boot catch-up. The end-of-day sync runs once per weekday at the first tick at or after that
time (also after a late restart). The 05:00 to 06:00 sync skip window is applied by the sync.

End-of-day retry: the day is marked done only after a sync cycle in which every account
ended with status ok or partial (sync_all returns the per-account results). A failed cycle
(no_auth, broker_error, error, skipped_window, no_fill_data, or an exception) is retried at a
later tick, not more often than every DISCRETIONARY_LEDGER_RETRY_MIN minutes (default 15),
until one succeeds or the day rolls over; never inside the 05:00 to 06:00 skip window. Retries
are single-flight like every sync. A sync function that returns no list (a test stub) counts as
success; an empty account list counts as success (nothing to retry). Periodic syncs are
unchanged and never mark the end-of-day sync done.

Post-close recovery. VERIFIED: a live read at 22:51 IST returned the same-day fills from
12:11 to 13:05 and no previous-day row; the end-of-day sync at 16:15 is after the NSE closing
session ends. NOT VERIFIED: when the TradeBook resets (the broker documents only that access
tokens are cleared between 05:00 and 06:00), whether a boot before 05:00 next morning still
sees yesterday's fills, and whether trade corrections after 16:15 change fills already stored.
"""

import atexit
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from services import discretionary_ledger_service as svc
from utils.env_config import env_int
from utils.logging import get_logger

logger = get_logger(__name__)

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
EOD_TIME = (16, 15)  # default; DISCRETIONARY_LEDGER_EOD_TIME overrides
TICK_SECONDS = 60
SKIP_WINDOW = ((5, 0), (6, 0))  # naive IST, start inclusive, end exclusive; mirrors the broker
OK_STATUSES = frozenset({"ok", "partial"})
# trigger_sync reason used by subscribers/discretionary_ledger_subscriber
EVENT_REASON = "order_event"

_lock = threading.Lock()
_inflight = False
_executor: ThreadPoolExecutor | None = None
_ticker: threading.Thread | None = None
_stop = threading.Event()
_future: Future | None = None
_atexit_registered = False
_last_periodic: datetime | None = None
_last_eod_date = None  # set only after a fully successful end-of-day cycle
_last_eod_attempt: datetime | None = None
_eod_attempt_date = None
_sync_fn: Callable[[], object] = svc.sync_all
_now_fn: Callable[[], datetime] = svc.now_ist


def _interval_minutes() -> int:
    return env_int("DISCRETIONARY_LEDGER_INTERVAL_MIN", 15, minimum=1)


def _eod_time() -> tuple[int, int]:
    """End-of-day sync time (hour, minute) IST from DISCRETIONARY_LEDGER_EOD_TIME (HH:MM).

    A missing or malformed value falls back to the default; it never raises.
    """
    raw = os.getenv("DISCRETIONARY_LEDGER_EOD_TIME", "").strip()
    if not raw:
        return EOD_TIME
    try:
        hh, mm = raw.split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    except ValueError:
        pass
    logger.warning("Ignoring invalid DISCRETIONARY_LEDGER_EOD_TIME; using the default")
    return EOD_TIME


def _retry_minutes() -> int:
    return env_int("DISCRETIONARY_LEDGER_RETRY_MIN", 15, minimum=1)


def cycle_succeeded(result) -> bool:
    """True when every account in a sync cycle ended ok or partial."""
    if not isinstance(result, list):
        return True  # no per-account result available (stub): treat as success
    return all(isinstance(r, dict) and r.get("status") in OK_STATUSES for r in result)


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="discretionary-ledger")
        return _executor


def _reset_state_for_tests() -> None:
    global _inflight, _last_periodic, _last_eod_date, _sync_fn, _now_fn
    global _last_eod_attempt, _eod_attempt_date
    with _lock:
        _inflight = False
        _last_periodic = None
        _last_eod_date = None
        _last_eod_attempt = None
        _eod_attempt_date = None
        _sync_fn = svc.sync_all
        _now_fn = svc.now_ist


def _worker(reason: str, on_done: Callable[[bool], None] | None = None) -> None:
    global _inflight, _last_eod_date, _last_periodic
    ok = False
    try:
        started = _now_fn()
        ok = cycle_succeeded(_sync_fn())
        if reason == "eod":
            if ok:
                with _lock:
                    _last_eod_date = _eod_attempt_date
            else:
                logger.warning("Discretionary ledger end-of-day sync incomplete; will retry")
        elif reason == EVENT_REASON and ok:
            # An event-triggered sync runs the same sync_all -> sync_once path as a periodic
            # one, so it records the same ledger_sync_runs rows (started_at, status) and is a
            # same-day heartbeat too: the next periodic sync waits a full interval after it.
            # A failed event sync leaves the periodic timing as it was.
            with _lock:
                if _last_periodic is None or started > _last_periodic:
                    _last_periodic = started
    except Exception as e:  # never propagate
        ok = False
        logger.error("Discretionary ledger sync (%s) failed: %s", reason, type(e).__name__)
    finally:
        try:
            from utils.db_sessions import remove_all_scoped_sessions

            remove_all_scoped_sessions()
        except Exception:
            pass
        with _lock:
            _inflight = False
        if on_done is not None:
            try:
                on_done(ok)
            except Exception as e:  # a callback failure never reaches the worker
                logger.error("Discretionary ledger sync callback failed: %s", type(e).__name__)


def trigger_sync(reason: str = "manual", on_done: Callable[[bool], None] | None = None) -> bool:
    """Queue one sync unless one is already in flight. Returns True if queued.

    ``on_done(ok)`` (optional) is called on the worker thread after the sync finished and the
    in-flight flag is released; ``ok`` is True when every account ended ok or partial. It is
    not called for a dropped trigger (False returned).
    """
    global _inflight, _future
    with _lock:
        if _inflight:
            return False
        _inflight = True
    try:
        _future = _get_executor().submit(_worker, reason, on_done)
    except Exception as e:  # executor shut down etc.
        with _lock:
            _inflight = False
        logger.error("Discretionary ledger could not queue sync: %s", type(e).__name__)
        return False
    return True


def is_running() -> bool:
    """True while the ticker thread is alive (start_scheduler ran and stop_scheduler did not)."""
    with _lock:
        return _ticker is not None and _ticker.is_alive()


def event_sync_allowed(now: datetime) -> bool:
    """Whether an order-event sync may run at ``now``: the periodic window only.

    Weekdays, MARKET_OPEN to MARKET_CLOSE inclusive (same test as the periodic sync), never
    inside the 05:00 to 06:00 skip window. Fills after 15:30 are left to the end-of-day sync,
    as for the periodic sync, so an event sync is never less conservative than a periodic one.
    sync_once still applies the broker's own skip window on top.
    """
    if now.weekday() >= 5 or svc.in_skip_window(now, SKIP_WINDOW):
        return False
    return MARKET_OPEN <= (now.hour, now.minute) <= MARKET_CLOSE


def now() -> datetime:
    """The scheduler's clock (naive IST; replaced in tests)."""
    return _now_fn()


def due_reason(
    now: datetime,
    last_periodic: datetime | None,
    last_eod_date,
    last_eod_attempt: datetime | None = None,
) -> str | None:
    """Pure decision: which scheduled sync, if any, is due at ``now``.

    The end-of-day sync is due until ``last_eod_date`` is today; after a failed attempt it
    waits DISCRETIONARY_LEDGER_RETRY_MIN minutes before the next one. Never inside the skip window.
    """
    if now.weekday() >= 5:
        return None
    hm = (now.hour, now.minute)
    if (
        hm >= _eod_time()
        and last_eod_date != now.date()
        and not svc.in_skip_window(now, SKIP_WINDOW)
    ):
        if (
            last_eod_attempt is None
            or last_eod_attempt.date() != now.date()
            or (now - last_eod_attempt).total_seconds() >= _retry_minutes() * 60
        ):
            return "eod"
    if MARKET_OPEN <= hm <= MARKET_CLOSE:
        if (
            last_periodic is None
            or (now - last_periodic).total_seconds() >= _interval_minutes() * 60
        ):
            return "periodic"
    return None


def _tick() -> None:
    global _last_periodic, _last_eod_attempt, _eod_attempt_date
    now = _now_fn()
    reason = due_reason(now, _last_periodic, _last_eod_date, _last_eod_attempt)
    if reason is None:
        return
    if reason == "eod":
        previous = (_last_eod_attempt, _eod_attempt_date)
        _last_eod_attempt, _eod_attempt_date = now, now.date()
        if not trigger_sync(reason):  # dropped (one in flight): not an attempt
            _last_eod_attempt, _eod_attempt_date = previous
    elif trigger_sync(reason):
        _last_periodic = now


def _run_ticker() -> None:
    while not _stop.wait(TICK_SECONDS):
        try:
            _tick()
        except Exception as e:
            logger.error("Discretionary ledger tick failed: %s", type(e).__name__)


def start_scheduler() -> None:
    """Start the ticker (idempotent) and queue the boot catch-up sync."""
    global _ticker, _atexit_registered
    with _lock:
        if _ticker is not None and _ticker.is_alive():
            return
        if not _atexit_registered:
            atexit.register(stop_scheduler)
            _atexit_registered = True
        _stop.clear()
        _ticker = threading.Thread(
            target=_run_ticker, name="discretionary-ledger-tick", daemon=True
        )
        _ticker.start()
    trigger_sync("boot")
    logger.info("Discretionary ledger scheduler started")


def stop_scheduler() -> None:
    """Stop the ticker and the worker. Registered with atexit at start; also used by tests.

    Never blocks on a running sync: queued work is cancelled, a running sync is left to
    finish on its own thread (it only reads the broker and appends ledger rows).
    """
    global _ticker, _executor, _inflight, _future
    _stop.set()
    with _lock:
        t, ex, fut = _ticker, _executor, _future
        _ticker = None
        _executor = None
        _future = None
    if t is not None and t is not threading.current_thread():
        t.join(timeout=5)
    if ex is not None:
        ex.shutdown(wait=False, cancel_futures=True)
        if fut is not None and fut.cancelled():
            with _lock:
                _inflight = False  # the cancelled job's finally block will never run
