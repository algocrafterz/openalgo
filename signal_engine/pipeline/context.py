"""Rolling per-symbol window: warm-up history plus today's closed bars.

The strategy sees the same kind of frame in replay, paper and live: N prior sessions of
5-minute bars and the last 2 sessions of continuous-trading 1-minute bars (value area),
followed by today's closed bars. Warm-up is loaded once per symbol; a symbol that cannot be
warmed is named and excluded, never silently dropped.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

import pandas as pd
from loguru import logger

from signal_engine.features.session import continuous_session
from signal_engine.pipeline.strategy import Fire, Strategy

#: prior sessions of 1-minute bars the value area needs (previous session only, plus one spare)
WARMUP_SESSIONS_1M = 2


class WarmupLoader(Protocol):
    def load(
        self, symbols: list[str], day: dt.date, sessions5: int, sessions1: int
    ) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], list[str]]:
        """(5m history, 1m history, failed symbols); history is strictly before `day`."""
        ...


class HistorifyLoader:
    """Warm-up bars from the read-only Historify store (opened and closed per call)."""

    def __init__(self, db_path=None) -> None:
        self._db_path = db_path

    def load(self, symbols, day, sessions5, sessions1):
        from signal_engine.backtest import data

        start = (pd.Timestamp(day) - pd.Timedelta(days=int(sessions5 * 1.7) + 7)).date().isoformat()
        end = (pd.Timestamp(day) - pd.Timedelta(days=1)).date().isoformat()
        common = {
            "symbols": list(symbols),
            "start": start,
            "end": end,
            "min_sessions": 1,
            "db_path": self._db_path,
        }
        f5 = data.from_historify(interval="5m", **common)
        f1 = data.from_historify(interval="1m", **common)
        h5, h1, failed = {}, {}, []
        for s in symbols:
            if s not in f5 or s not in f1:
                failed.append(s)
                continue
            h5[s] = _last_sessions(f5[s], sessions5)
            h1[s] = _last_sessions(f1[s], sessions1)
        return h5, h1, failed


def _last_sessions(df: pd.DataFrame, n: int) -> pd.DataFrame:
    days = sorted(set(df.index.date))[-n:]
    return df[pd.Index(df.index.date).isin(days)]


class RollingContext:
    def __init__(self, strategy: Strategy, loader: WarmupLoader) -> None:
        self._strategy = strategy
        self._loader = loader
        self._h5: dict[str, pd.DataFrame] = {}
        self._h1: dict[str, pd.DataFrame] = {}
        self.failed: set[str] = set()

    @property
    def warmed(self) -> set[str]:
        return set(self._h5)

    def warm(self, symbols: list[str], day: dt.date) -> list[str]:
        """Load history for symbols not yet warmed. Returns the symbols that failed."""
        todo = [s for s in symbols if s not in self._h5 and s not in self.failed]
        if not todo:
            return []
        h5, h1, failed = self._loader.load(
            todo, day, self._strategy.warmup_sessions, WARMUP_SESSIONS_1M
        )
        self._h5.update(h5)
        self._h1.update(h1)
        for s in failed:
            self.failed.add(s)
            logger.warning(f"warm-up failed for {s}: excluded for the day")
        return failed

    def window(
        self, symbol: str, today5: pd.DataFrame, today1: pd.DataFrame | None
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        if symbol not in self._h5:
            raise KeyError(f"{symbol} is not warmed")
        return (
            _join(self._h5[symbol], today5),
            continuous_session(_join(self._h1[symbol], today1)),
        )

    def evaluate(
        self, symbol: str, today5: pd.DataFrame, today1: pd.DataFrame | None
    ) -> list[Fire]:
        d5, d1 = self.window(symbol, today5, today1)
        return self._strategy.evaluate(symbol, d5, d1)


def _join(prior: pd.DataFrame, today: pd.DataFrame | None) -> pd.DataFrame:
    if today is None or today.empty:
        return prior
    # Live bars carry a fixed-offset IST, warm-up bars carry the Asia/Kolkata zone. Same instants,
    # but concatenating the two gives an object Index the features cannot read. Align first.
    if prior.index.tz is not None and today.index.tz != prior.index.tz:
        today = today.tz_convert(prior.index.tz)
    start = today.index[0].normalize()
    both = pd.concat([prior[prior.index < start], today])
    return both[~both.index.duplicated(keep="last")].sort_index()
