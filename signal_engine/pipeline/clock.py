"""Clock adapters and the pure bar-boundary arithmetic every mode shares."""

from __future__ import annotations

import asyncio
from typing import Protocol

import pandas as pd

from signal_engine.timeutils import IST

_SESSION_OPEN = pd.Timedelta(hours=9, minutes=15)


def next_cycle(
    now: pd.Timestamp, interval_min: int, delay_s: float
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(bar_close, run_at): the first bar boundary whose run time is still in the future.

    Boundaries sit on the session-open grid (09:15 IST). run_at is the boundary plus the
    delay that lets the broker publish the closed bar.
    """
    now = now.tz_convert(IST)
    anchor = now.normalize() + _SESSION_OPEN
    step = pd.Timedelta(minutes=interval_min)
    k = ((now - pd.Timedelta(seconds=delay_s) - anchor) // step) + 1
    bar_close = anchor + k * step
    return bar_close, bar_close + pd.Timedelta(seconds=delay_s)


class Clock(Protocol):
    def now(self) -> pd.Timestamp: ...

    async def sleep_until(self, t: pd.Timestamp) -> None: ...


class WallClock:
    def now(self) -> pd.Timestamp:
        return pd.Timestamp.now(tz=IST)

    async def sleep_until(self, t: pd.Timestamp) -> None:
        wait = (t - self.now()).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)


class SimClock:
    """Replay clock: sleeping advances time instantly and never backwards."""

    def __init__(self, start: pd.Timestamp) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock start must be tz-aware")
        self._now = start.tz_convert(IST)

    def now(self) -> pd.Timestamp:
        return self._now

    async def sleep_until(self, t: pd.Timestamp) -> None:
        self._now = max(self._now, t.tz_convert(IST))
