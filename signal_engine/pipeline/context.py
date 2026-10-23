"""Rolling per-symbol window: warm-up history plus today's closed bars.

The strategy sees the same kind of frame in replay, paper and live: N prior sessions of
5-minute bars and the last 2 sessions of continuous-trading 1-minute bars (value area),
followed by today's closed bars. Warm-up is loaded once per symbol; a symbol that cannot be
warmed is named and excluded, never silently dropped.
"""

from __future__ import annotations

import datetime as dt
import threading
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
    """Warm-up bars from the read-only Historify store (opened and closed per call).

    History is checked against the NSE calendar before it is used, because the strategies read
    "the previous session" as whatever session comes last in the warm-up. On 2026-10-14 the store
    lacked 10-13 and BREAKOUT-PY traded PFC on 10-12's value area. A symbol is rejected (excluded
    for the day, named in `rejected`, reported once a day through `on_rejected`) when:

      * its newest session is not the calendar's previous session (stale store), or
      * that session is incomplete: fewer than MIN_CONTINUOUS_BARS continuous 1m bars, or none at
        or after LAST_CONTINUOUS_BAR (a backfill cut off early).

    Only regular sessions count: bars on a day that is not a session (a stray weekend print) or
    on a special (Muhurat) session are dropped first, so the previous session is the last
    regular one (features.session.special_sessions has the policy). If the calendar
    cannot answer, every symbol is rejected: no signal is better than one on unknown history.
    """

    def __init__(self, db_path=None, *, calendar=None, on_rejected=None) -> None:
        self._db_path = db_path
        self._calendar = calendar
        self._on_rejected = on_rejected
        self._reported: set[dt.date] = set()
        self._lock = threading.Lock()
        self.rejected: dict[str, str] = {}

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
        sessions, calendar_problem = self._sessions(pd.Timestamp(start).date(), day)
        h5, h1, failed, rejected = {}, {}, [], {}
        for s in symbols:
            if s not in f5 or s not in f1:
                # Reported like a stale store: an unreadable or empty store would otherwise
                # leave every symbol out with no alert at all.
                rejected[s] = "no stored bars before this session"
                continue
            if calendar_problem:
                rejected[s] = calendar_problem
                continue
            five, one = _on_sessions(f5[s], sessions), _on_sessions(f1[s], sessions)
            problem = self._problem(one, five, sessions[-1])
            if problem:
                rejected[s] = problem
                continue
            h5[s] = _last_sessions(five, sessions5)
            h1[s] = _last_sessions(one, sessions1)
        failed += list(rejected)
        self._report(day, rejected)
        return h5, h1, failed

    def _sessions(self, start: dt.date, day: dt.date) -> tuple[tuple[dt.date, ...], str]:
        """Calendar sessions in [start, day) and, when the calendar cannot answer, why."""
        from signal_engine.backtest.data_readiness.calendar import CalendarError, load_calendar

        try:
            cal = self._calendar or load_calendar()
            specials = set(getattr(cal, "specials", {}) or {})
            # Regular sessions only: a special (Muhurat) session is not trading history
            # (features.session.special_sessions), so "previous" skips it.
            sessions = tuple(
                d for d in cal.expected_sessions(start, day - dt.timedelta(days=1)) if d not in specials
            )
        except CalendarError as e:
            return (), f"calendar cannot confirm the previous session ({e})"
        if not sessions:
            return (), f"calendar has no session in {start}..{day - dt.timedelta(days=1)}"
        return sessions, ""

    def _problem(self, one: pd.DataFrame, five: pd.DataFrame, previous: dt.date) -> str:
        newest = max(set(one.index.date) & set(five.index.date), default=None)
        if newest != previous:
            return f"stale: newest stored session {newest}, previous session {previous}"
        cont = continuous_session(one[pd.Index(one.index.date) == previous])
        if len(cont) < MIN_CONTINUOUS_BARS or cont.index[-1].time() < LAST_CONTINUOUS_BAR:
            last = cont.index[-1].strftime("%H:%M") if len(cont) else "none"
            return f"incomplete: {previous} has {len(cont)} continuous 1m bars, last {last}"
        return ""

    def _report(self, day: dt.date, rejected: dict[str, str]) -> None:
        if not rejected:
            return
        self.rejected.update(rejected)
        sample = "; ".join(f"{s}: {why}" for s, why in list(rejected.items())[:3])
        logger.critical(
            f"warm-up {day}: {len(rejected)} symbol(s) excluded for the day, history not usable ({sample})"
        )
        with self._lock:
            if day in self._reported or self._on_rejected is None:
                return
            self._reported.add(day)
        try:
            self._on_rejected(day, dict(rejected))
        except Exception:  # noqa: BLE001 - a failed alert must not fail the warm-up
            logger.exception("warm-up: could not report the excluded symbols")


#: Continuous-market 1m bars a complete regular session has: 09:15-15:14 is 360. Every one of
#: 16,685 real symbol-sessions since 2026-07-01 had 360; 324 (90%) only allows a few gaps.
MIN_CONTINUOUS_BARS = 324
#: A session whose continuous bars stop before this was cut off (a backfill run mid-session).
LAST_CONTINUOUS_BAR = dt.time(15, 10)


def _on_sessions(df: pd.DataFrame, sessions) -> pd.DataFrame:
    """Only bars on calendar sessions: a stray print on a holiday or weekend is not a session."""
    return df[pd.Index(df.index.date).isin(set(sessions))]


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
