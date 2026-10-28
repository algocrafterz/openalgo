"""Who is asking for a broker data slot: an order, a strategy, or a background reader.

A limiter that treats every caller alike lets a burst of background reads (position
valuation, page refreshes, reconciliation) queue ahead of the quote an order is waiting
for. This module carries the caller's class to the limiter without changing any
signature on the way: a context variable set around the call.

* ``CRITICAL``: pricing and executing an order. May use the lane the limiter reserves.
* ``NORMAL``: everything untagged, including strategy bar history.
* ``BACKGROUND``: work that can wait or fail without hurting a trade. The limiter may
  refuse it when its turn is too far away.

A context variable follows the call in the same thread or greenlet and into asyncio
tasks. It does not follow a hop into a thread pool: use :func:`run_in_context` to submit.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

CRITICAL = "critical"
NORMAL = "normal"
BACKGROUND = "background"

_priority: contextvars.ContextVar[str] = contextvars.ContextVar(
    "broker_request_priority", default=NORMAL
)


def current() -> str:
    """The priority of the code running now."""
    return _priority.get()


@contextmanager
def _tagged(value: str) -> Iterator[None]:
    token = _priority.set(value)
    try:
        yield
    finally:
        _priority.reset(token)


def critical():
    """Tag broker data calls made inside the block as order-critical."""
    return _tagged(CRITICAL)


def background():
    """Tag broker data calls made inside the block as background work."""
    return _tagged(BACKGROUND)


def run_in_context(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[[], Any]:
    """Bind ``fn`` to a copy of the caller's context, for submission to a thread pool."""
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn, *args, **kwargs)
