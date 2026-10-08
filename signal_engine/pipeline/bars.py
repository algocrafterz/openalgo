"""Bar sources: the only place bars enter the pipeline, and where "closed" is decided.

Every adapter returns frames with columns Open/High/Low/Close/Volume on a tz-aware IST
index, holding only bars that have CLOSED by `now`. The closed-bar rule lives in one
function (`closed_only`) so replay, paper and live cannot disagree about it. OpenAlgo's
history endpoint returns UNIX epoch seconds (a UTC instant); the conversion to IST happens
once, here, at the boundary.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Protocol

import httpx
import pandas as pd
from loguru import logger

from signal_engine.timeutils import IST

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


class MissingBarsError(Exception):
    """Some symbols returned no usable bars. `frames` holds the ones that did."""

    def __init__(self, missing: list[str], frames: dict[str, pd.DataFrame]) -> None:
        self.missing = list(missing)
        self.frames = frames
        super().__init__(f"no bars for {len(self.missing)} symbol(s): {', '.join(self.missing)}")


class BarSource(Protocol):
    async def fetch(
        self, symbols: list[str], interval_min: int, day: dt.date, now: pd.Timestamp
    ) -> dict[str, pd.DataFrame]: ...


def closed_only(df: pd.DataFrame, now: pd.Timestamp, interval_min: int) -> pd.DataFrame:
    """Bars whose open time plus the interval is not after `now`. The forming bar is dropped."""
    if df.empty:
        return df
    return df[df.index + pd.Timedelta(minutes=interval_min) <= now]


def _finish(
    symbols: list[str], frames: dict[str, pd.DataFrame], missing: list[str]
) -> dict[str, pd.DataFrame]:
    if missing:
        raise MissingBarsError(missing, frames)
    return frames


class ReplaySource:
    """Serves preloaded frames (Historify) as if they arrived live, truncated to `now`."""

    def __init__(
        self,
        frames5: dict[str, pd.DataFrame],
        frames1: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self._frames = {5: frames5, 1: frames1 or {}}

    async def fetch(
        self, symbols: list[str], interval_min: int, day: dt.date, now: pd.Timestamp
    ) -> dict[str, pd.DataFrame]:
        pool = self._frames.get(interval_min, {})
        out: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for s in symbols:
            df = pool.get(s)
            if df is None or df.empty:
                missing.append(s)
                continue
            today = df[df.index.date == day]
            today = closed_only(today, now, interval_min)
            if today.empty:
                missing.append(s)
            else:
                out[s] = today
        return _finish(symbols, out, missing)


def parse_history(rows: list[dict]) -> pd.DataFrame:
    """OpenAlgo history rows (epoch seconds) to an IST-indexed OHLCV frame."""
    df = pd.DataFrame(rows)
    need = {"timestamp", "open", "high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        raise ValueError(f"history rows lack columns {sorted(need - set(df.columns))}")
    idx = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    out = df[["open", "high", "low", "close", "volume"]].astype("float64")
    out.columns = OHLCV
    out.index = pd.DatetimeIndex(idx)
    return out[~out.index.duplicated(keep="last")].sort_index()


class OpenAlgoHistorySource:
    """Live bars from OpenAlgo's POST /api/v1/history, through the shared HTTP client."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float,
        max_concurrent: int,
        exchange: str = "NSE",
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout_s
        self._max_concurrent = max_concurrent
        self._exchange = exchange

    def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        from signal_engine import api_client

        return api_client._get_client()

    def _endpoint(self) -> tuple[str, str]:
        if self._base_url is not None and self._api_key is not None:
            return f"{self._base_url}/api/v1/history", self._api_key
        from signal_engine.config import settings

        return f"{settings.openalgo_base_url}/api/v1/history", settings.openalgo_api_key

    async def _one(self, sem, symbol, interval_min, day, now):
        url, key = self._endpoint()
        payload = {
            "apikey": key,
            "symbol": symbol,
            "exchange": self._exchange,
            "interval": f"{interval_min}m",
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        }
        async with sem:
            try:
                resp = await self._http().post(url, json=payload, timeout=self._timeout)
                resp.raise_for_status()
                rows = resp.json().get("data") or []
                df = parse_history(rows) if rows else pd.DataFrame()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(f"history {symbol}: {type(exc).__name__}: {exc}")
                return symbol, None
        df = closed_only(df, now, interval_min)
        return symbol, (df if not df.empty else None)

    async def fetch(
        self, symbols: list[str], interval_min: int, day: dt.date, now: pd.Timestamp
    ) -> dict[str, pd.DataFrame]:
        sem = asyncio.Semaphore(self._max_concurrent)
        results = await asyncio.gather(
            *(self._one(sem, s, interval_min, day, now) for s in symbols)
        )
        out = {s: df for s, df in results if df is not None}
        missing = [s for s, df in results if df is None]
        return _finish(symbols, out, missing)


async def fetch_current(
    source: BarSource,
    symbols: list[str],
    interval_min: int,
    day: dt.date,
    clock,
    bar_close: pd.Timestamp,
    deadline_s: float,
    backoff_s: float,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Fetch until every symbol carries the bar that closed at `bar_close`, or the deadline.

    Returns (frames, late): `late` names every symbol still without that bar. Only the
    still-pending symbols are re-requested on each retry.
    """
    deadline = clock.now() + pd.Timedelta(seconds=deadline_s)
    last_open = bar_close - pd.Timedelta(minutes=interval_min)
    pending = list(symbols)
    got: dict[str, pd.DataFrame] = {}
    while True:
        try:
            frames = await source.fetch(pending, interval_min, day, clock.now())
        except MissingBarsError as exc:
            frames = exc.frames
        for s, df in frames.items():
            if not df.empty and df.index[-1] >= last_open:
                got[s] = df
        pending = [s for s in pending if s not in got]
        if not pending:
            break
        nxt = clock.now() + pd.Timedelta(seconds=backoff_s)
        if nxt > deadline:
            break
        await clock.sleep_until(nxt)
    return got, pending


class SharedBarSource:
    """One fetch per symbol and bar for every runner session that shares this source.

    Each strategy session runs its own cycle at the same bar close. Without sharing, two
    strategies on the same universe double the history calls to a broker endpoint that is already
    near its limit. The first session to ask for a symbol fetches it; a session asking again
    while the cached frame already holds the latest bar that can have closed by `now` gets the
    cached frame, truncated to `now`. A lock serialises fetch rounds so two sessions never request
    the same symbol at once. One frame per (symbol, interval) is kept, for the current day only.
    """

    def __init__(self, inner: BarSource) -> None:
        self._inner = inner
        self._lock = asyncio.Lock()
        self._day: dt.date | None = None
        self._frames: dict[tuple[str, int], pd.DataFrame] = {}

    @staticmethod
    def _last_closed_open(now: pd.Timestamp, interval_min: int) -> pd.Timestamp:
        step = pd.Timedelta(minutes=interval_min)
        return now.floor(f"{interval_min}min") - step

    def _fresh(self, symbol: str, interval_min: int, now: pd.Timestamp) -> pd.DataFrame | None:
        df = self._frames.get((symbol, interval_min))
        if df is None or df.empty:
            return None
        if df.index[-1] < self._last_closed_open(now, interval_min):
            return None
        return closed_only(df, now, interval_min)

    async def fetch(
        self, symbols: list[str], interval_min: int, day: dt.date, now: pd.Timestamp
    ) -> dict[str, pd.DataFrame]:
        async with self._lock:
            if self._day != day:
                self._day, self._frames = day, {}
            out: dict[str, pd.DataFrame] = {}
            todo = []
            for s in symbols:
                cached = self._fresh(s, interval_min, now)
                if cached is None:
                    todo.append(s)
                else:
                    out[s] = cached
            missing: list[str] = []
            if todo:
                try:
                    got = await self._inner.fetch(todo, interval_min, day, now)
                except MissingBarsError as exc:
                    got, missing = exc.frames, exc.missing
                for s, df in got.items():
                    self._frames[(s, interval_min)] = df
                    out[s] = df
            return _finish(symbols, out, missing)
