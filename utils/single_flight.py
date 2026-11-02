"""Let overlapping identical requests share one in-flight call.

The first caller for a key (the leader) runs the fetch. Callers that arrive with the same key
while it runs (joiners) book no fetch of their own and receive a copy of the leader's result,
or the leader's exception. Nothing is kept once the flight ends, so the next request asks
again. A flight older than ``join_window`` is not joined, so a request never takes an answer
from a call that began long before it was made.

The waiters use real-thread primitives, and the registry lock guards dict work only, so the
class is correct under eventlet, gthread and the dev server (see utils/real_threading).
"""

import copy
import time

from utils import real_threading
from utils.logging import get_logger

logger = get_logger(__name__)


class _Flight:
    __slots__ = ("event", "result", "error", "started", "keep_until")

    def __init__(self):
        self.event = real_threading.Event()
        self.result = None
        self.error = None
        self.started = time.monotonic()
        self.keep_until = None  # set when a successful flight is kept for a ttl


class SingleFlight:
    """Collapse overlapping calls that share a hashable key into one."""

    def __init__(self, join_window: float, wait_timeout: float):
        self._join_window = join_window
        self._wait_timeout = wait_timeout
        self._flights: dict = {}
        self._lock = real_threading.Lock()
        self._led = 0
        self._joined = 0

    def clear(self) -> None:
        with self._lock:
            self._flights.clear()
            self._led = self._joined = 0

    def stats(self) -> dict:
        """Requests that ran the fetch (led) and requests that shared one (joined)."""
        with self._lock:
            return {"led": self._led, "joined": self._joined}

    def do(self, key, fetch, ttl: float = 0.0):
        """Return ``fetch()``, sharing its result with callers that overlap on ``key``.

        With ``ttl`` > 0 a successful result is also handed to callers for that many seconds after
        it was fetched, so reads that come back to back cost one fetch. Failures are never kept.
        """
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            flight = self._flights.get(key)
            cached = (
                flight is not None and flight.keep_until is not None and now <= flight.keep_until
            )
            leader = not cached and (
                flight is None
                or flight.keep_until is not None
                or now - flight.started > self._join_window
            )
            if leader:
                flight = _Flight()
                self._flights[key] = flight
                self._led += 1
            else:
                self._joined += 1

        if not leader and flight.event.is_set():
            return copy.deepcopy(flight.result)

        if not leader:
            if not real_threading.wait_for(flight.event, self._wait_timeout):
                logger.warning("Single-flight wait timed out; fetching directly")
                return fetch()
            if flight.error is not None:
                raise flight.error
            return copy.deepcopy(flight.result)

        try:
            result = fetch()
            flight.result = copy.deepcopy(result)
            if ttl > 0:
                flight.keep_until = time.monotonic() + ttl
            return result
        except BaseException as exc:  # noqa: BLE001 - handed to the joiners unchanged
            flight.error = exc
            raise
        finally:
            flight.event.set()
            with self._lock:
                if flight.keep_until is None and self._flights.get(key) is flight:
                    del self._flights[key]  # a replacement flight is not ours to remove

    def _prune_locked(self, now: float) -> None:
        """Drop kept results whose ttl has run out, so the registry does not grow with the keys."""
        expired = [
            k for k, f in self._flights.items() if f.keep_until is not None and f.keep_until < now
        ]
        for k in expired:
            del self._flights[k]
