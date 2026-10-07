"""Source-independent signal freshness.

A signal older than risk.stale_signal_seconds must never become a trade, whatever delayed it:
a Telegram outage whose backlog arrives after reconnect, a slow API call, a lock. The origin
time (when the SOURCE issued the signal, e.g. the Telegram message date) travels with the
signal through a context variable, so the check lives at the pipeline entry rather than in any
one transport. Nothing here queues or stores a signal.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_origin: ContextVar[datetime | None] = ContextVar("signal_origin_at", default=None)


def stale_age(origin: datetime | None, limit_seconds: float, now: datetime | None = None) -> float | None:
    """Age in seconds if `origin` is older than the limit, else None.

    An unknown origin (None) is not judged: sources with no origin time, such as the Python
    runner, apply their own freshness rule. A naive datetime is read as UTC.
    """
    if origin is None:
        return None
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - origin).total_seconds()
    return age if age > limit_seconds else None


def current_origin() -> datetime | None:
    return _origin.get()


@contextmanager
def origin_scope(origin: datetime | None) -> Iterator[None]:
    """Make `origin` visible to everything awaited inside the block, then restore."""
    token = _origin.set(origin)
    try:
        yield
    finally:
        _origin.reset(token)
