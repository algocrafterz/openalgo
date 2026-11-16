"""INVALID_IP alert for the OpenAlgo scheduler auto-login (BUG-029).

Called from the broker-login failure branch only. Acts when the failure text contains
INVALID_IP (case-insensitive); every other failure is ignored. Never raises, never blocks the
login: the send runs on a background thread through the existing ops alert channel
(signal_engine.ops.alerts.deliver, which has its own retry budget and persistent dedupe).

Sanitised: the failure text is only inspected, never logged or sent. The log line is a fixed
message plus the exception class. No egress IP is fetched (that would be a new network call in
the login path), so none is logged or sent.

Dedupe: one alert per IST day per process (per distinct failure marker), plus a 23 h
cross-process cooldown in alerts.deliver. A genuine second INVALID_IP after an allow-list fix
inside that window sends no new alert (the existing startup-failure notice still goes out).

Exit delay: the alert thread is non-daemon, so after a failed login the process can wait for it
before exiting: about 30 s with Telegram unreachable, up to about 75 s worst case (bounded by the
lock and HTTP timeouts).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

MARKER = "INVALID_IP"
MESSAGE = (
    "Broker rejected the login: INVALID_IP. Update the Flattrade IP allow-list for this network."
)
_IST = timezone(timedelta(hours=5, minutes=30))
_sent: set[tuple[str, str]] = set()
_lock = threading.Lock()


def _warn(text: str, *args) -> None:
    try:
        from utils.logging import get_logger

        get_logger(__name__).warning(text, *args)
    except Exception:  # noqa: BLE001
        pass


def _deliver(text: str) -> bool:
    from signal_engine.ops import alerts

    return alerts.deliver(
        text, kind="invalid-ip", phase="login", condition=MARKER, severity="RED"
    )


def _spawn(fn: Callable[[], object]) -> None:
    threading.Thread(target=fn, name="invalid-ip-alert", daemon=False).start()


def notify_login_failure(
    error,
    *,
    send: Callable[[str], object] | None = None,
    spawn: Callable[[Callable[[], object]], None] | None = None,
    now: datetime | None = None,
) -> bool:
    """True when an alert was started for this failure; never raises, never blocks."""
    try:
        if MARKER.lower() not in str(error or "").lower():
            return False
        day = (now or datetime.now(_IST)).strftime("%Y-%m-%d")
        key = (day, MARKER)
        with _lock:
            if key in _sent:
                return False
            _sent.add(key)

        sender = send or _deliver

        def _run() -> None:
            try:
                sender(MESSAGE)
            except Exception as exc:  # noqa: BLE001
                _warn("INVALID_IP alert send failed: %s", type(exc).__name__)

        (spawn or _spawn)(_run)
        return True
    except Exception as exc:  # noqa: BLE001
        _warn("INVALID_IP alert hook failed: %s", type(exc).__name__)
        return False
