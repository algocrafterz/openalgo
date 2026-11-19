"""
Discretionary ledger subscriber: a live fill seen on the order.update topic triggers ONE
discretionary ledger sync a short debounce window later. Trigger only.

It writes nothing: the sync it asks for is the existing scheduler path
(services.discretionary_ledger_scheduler.trigger_sync with reason "order_event" ->
sync_all -> sync_once), which reads the broker TradeBook and appends ledger rows exactly as a
periodic sync does. The event payload itself never reaches the ledger; attribution, the grace
period, the coverage gap and classify are unchanged.

Filters: event.mode == "live" and event.filled_quantity > 0 (paper/analyze events and
unfilled updates are ignored), DISCRETIONARY_LEDGER_ENABLED true (read per event, parsed like
app.py) and the scheduler started (so nothing runs when the ledger did not start).

Coalescing (thread safe, one lock):
  - the first relevant event arms one threading.Timer (DISCRETIONARY_LEDGER_EVENT_DEBOUNCE_SEC,
    default 8); events while it is armed are coalesced into it (the sync starts after them);
  - when the timer fires, the sync is queued with an on_done callback; events while that sync
    is queued or running set a dirty flag; when it finishes, a dirty flag arms ONE more timer
    (the trailing re-check), so no event is lost and syncs never run back to back;
  - if the scheduler is busy (another sync in flight, the trigger is dropped), the timer is
    re-armed for one more window, at most MAX_BUSY_RETRIES times in a row (the periodic sync
    covers the rest);
  - outside the periodic window (weekdays 09:15 to 15:30, never 05:00 to 06:00; see
    scheduler.event_sync_allowed) a fired timer queues nothing: the end-of-day sync covers it.
At most one event sync per debounce window (plus sync time), plus the trailing one.

Why a Timer per window and not a long-lived thread: a Timer thread lives only for one window
(8 s) and exists only after a live fill; the scheduler ticker ticks every 60 s, too coarse for
an 8 s debounce. No new service, no second broker connection.

Why the default event-bus lane: the callback only does in-memory bookkeeping and the sync it
triggers is idempotent; if the bus sheds it under overload (counted by the bus), the periodic
and end-of-day syncs still collect the fill. The critical lane stays reserved for the strategy
book, whose fills must not be lost.

Independent kill switch (the event sync only; periodic, boot and end-of-day syncs are never
affected). The event sync is ON by default whenever the ledger runs. It is OFF when either
  - DISCRETIONARY_LEDGER_EVENT_SYNC_ENABLED is set to anything but a truthy value
    (1/true/yes/on, same parsing as DISCRETIONARY_LEDGER_ENABLED); absent or empty means ON; or
  - the kill file exists (DISCRETIONARY_LEDGER_EVENT_SYNC_KILL_FILE, default
    <repo>/tmp/DISCRETIONARY_EVENT_SYNC_OFF; tmp/ is gitignored).
Both are read on EVERY event, nothing is cached or bound at registration. The kill file takes
effect on the very next event without a restart (create it = off, delete it = on; it must be on
the same host or container as the app). An edit of .env is only read at process start, so the
env variable needs an application restart; only a process that changes os.environ itself
(tests) sees it live. While off, a relevant event is counted (events_ignored, ignored_disabled),
an armed debounce timer is cancelled, and a timer that still fires queues nothing. A sync that
is already queued or running is not interrupted. The ledger flag still wins: with
DISCRETIONARY_LEDGER_ENABLED false nothing runs at all.

Failures are logged (no unicode) and never raise into the event bus or the order path.
"""

import os
import threading
from pathlib import Path

from utils.env_config import env_float
from utils.logging import get_logger

logger = get_logger(__name__)

DEBOUNCE_DEFAULT_SEC = 8.0
DEBOUNCE_MIN_SEC = 1.0
MAX_BUSY_RETRIES = 15
_TRUE = ("1", "true", "yes", "on")
KILL_FILE_DEFAULT = str(
    Path(__file__).resolve().parent.parent / "tmp" / "DISCRETIONARY_EVENT_SYNC_OFF"
)

_lock = threading.Lock()
_timer: threading.Timer | None = None
_running = False  # an event sync is queued or running
_dirty = False  # an event arrived that the queued/running sync may not have seen
_busy_retries = 0
_stats = {
    "events_seen": 0,  # relevant events (live, filled, flag on, scheduler running)
    "events_ignored": 0,  # paper/analyze, unfilled, flag off or scheduler not running
    "ignored_disabled": 0,  # relevant events dropped by the event-sync kill switch
    "syncs_triggered": 0,  # event syncs queued
    "coalesced": 0,  # events folded into an armed timer or a running sync
    "trailing": 0,  # trailing re-checks armed after a sync
    "busy_retries": 0,  # timer fired while another sync ran; re-armed
    "outside_window": 0,  # timer fired outside the periodic window; nothing queued
    "failed": 0,  # event syncs that did not end ok/partial for every account
    "errors": 0,  # exceptions caught in this module
}

# Injection points for tests.
_timer_factory = threading.Timer


def _debounce_seconds() -> float:
    return env_float(
        "DISCRETIONARY_LEDGER_EVENT_DEBOUNCE_SEC", DEBOUNCE_DEFAULT_SEC, minimum=DEBOUNCE_MIN_SEC
    )


def _ledger_enabled() -> bool:
    return os.getenv("DISCRETIONARY_LEDGER_ENABLED", "false").strip().lower() in _TRUE


def _kill_file() -> str:
    return os.getenv("DISCRETIONARY_LEDGER_EVENT_SYNC_KILL_FILE", "").strip() or KILL_FILE_DEFAULT


def _event_sync_off_reason() -> str | None:
    """None when the event sync is on, else "env" or "kill_file". Read on every call."""
    raw = os.getenv("DISCRETIONARY_LEDGER_EVENT_SYNC_ENABLED", "").strip().lower()
    if raw and raw not in _TRUE:
        return "env"
    try:
        if os.path.exists(_kill_file()):
            return "kill_file"
    except Exception:
        pass  # an unreadable path never turns the sync off by accident or raises
    return None


def _scheduler():
    # Imported lazily: importing this module (app start with the ledger off) must not import
    # the ledger service or its database module.
    from services import discretionary_ledger_scheduler

    return discretionary_ledger_scheduler


def get_stats() -> dict:
    """In-memory counters plus the current state (no scheduler status endpoint exists)."""
    with _lock:
        out: dict = dict(_stats)
        out.update(timer_armed=_timer is not None, sync_running=_running, dirty=_dirty)
    out["debounce_sec"] = _debounce_seconds()
    reason = _event_sync_off_reason()
    out.update(event_sync_enabled=reason is None, event_sync_off_reason=reason)
    out["event_sync_kill_file"] = _kill_file()
    return out


def _reset_state_for_tests() -> None:
    global _timer, _running, _dirty, _busy_retries, _timer_factory
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = None
        _running = False
        _dirty = False
        _busy_retries = 0
        for k in _stats:
            _stats[k] = 0
        _timer_factory = threading.Timer


def _relevant(event) -> bool:
    if getattr(event, "mode", "") != "live":
        return False
    try:
        if int(getattr(event, "filled_quantity", 0) or 0) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return _ledger_enabled()


def on_order_update(event) -> None:
    """order.update handler (default lane). Never raises."""
    try:
        if not _relevant(event) or not _scheduler().is_running():
            with _lock:
                _stats["events_ignored"] += 1
            return
        if _event_sync_off_reason() is not None:
            _disabled_event()
            return
        _note_event()
    except Exception as e:
        _count_error("event", e)


def _disabled_event() -> None:
    """Kill switch is on: count the event, cancel an armed timer, queue nothing."""
    global _timer, _dirty, _busy_retries
    with _lock:
        _stats["events_ignored"] += 1
        _stats["ignored_disabled"] += 1
        if _timer is not None:
            _timer.cancel()
            _timer = None
        _dirty = False
        _busy_retries = 0


def _note_event() -> None:
    global _dirty
    with _lock:
        _stats["events_seen"] += 1
        if _timer is not None:
            _stats["coalesced"] += 1  # the armed sync starts after this event
            return
        if _running:
            _dirty = True  # the queued/running sync may have read the TradeBook already
            _stats["coalesced"] += 1
            return
        _arm_locked()


def _arm_locked() -> None:
    """Arm one debounce timer. Caller holds _lock and checked that none is armed."""
    global _timer
    t = _timer_factory(_debounce_seconds(), _fire)
    t.daemon = True
    t.name = "discretionary-ledger-debounce"
    _timer = t
    t.start()


def _fire() -> None:
    """Debounce window over: queue one event sync (scheduler single-flight applies)."""
    global _timer, _running, _dirty, _busy_retries
    try:
        sched = _scheduler()
        with _lock:
            _timer = None
            if _event_sync_off_reason() is not None:
                _stats["ignored_disabled"] += 1
                _dirty = False
                _busy_retries = 0
                return
            if not sched.event_sync_allowed(sched.now()):
                _stats["outside_window"] += 1
                _dirty = False
                _busy_retries = 0
                return
            _dirty = False  # the sync queued now starts after every event so far
            _running = True
        queued = sched.trigger_sync(sched.EVENT_REASON, on_done=_on_done)
        with _lock:
            if queued:
                _stats["syncs_triggered"] += 1
                _busy_retries = 0
                return
            _running = False
            if _busy_retries < MAX_BUSY_RETRIES and _timer is None:
                _busy_retries += 1
                _stats["busy_retries"] += 1
                _arm_locked()  # another sync is in flight: try once more a window later
            else:
                _busy_retries = 0
                logger.warning("Discretionary ledger event sync dropped: scheduler busy")
    except Exception as e:
        with _lock:
            _running = False
        _count_error("fire", e)


def _on_done(ok: bool) -> None:
    """Called by the scheduler worker after an event sync finished."""
    global _running, _dirty
    try:
        with _lock:
            _running = False
            if not ok:
                _stats["failed"] += 1
            if _dirty and _timer is None:
                _dirty = False
                _stats["trailing"] += 1
                _arm_locked()  # exactly one trailing re-check, a window later
        if not ok:
            logger.warning("Discretionary ledger event sync did not complete for every account")
    except Exception as e:
        _count_error("done", e)


def _count_error(where: str, e: Exception) -> None:
    try:
        with _lock:
            _stats["errors"] += 1
        logger.error("Discretionary ledger event trigger (%s) failed: %s", where, type(e).__name__)
    except Exception:
        pass
