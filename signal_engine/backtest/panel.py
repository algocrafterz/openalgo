"""Cross-sectional helpers for ranked strategies: weekly dates, a session window, a top-N schedule.

`harness.Backtest` simulates one symbol at a time, so a strategy that ranks the whole universe
needs that ranking worked out first and handed to each symbol as columns. This module does the
three generic parts of that:

  WEEKLY DATES      the last session of each ISO week in the run's calendar. The final session
                    of the calendar never rebalances: there is no next session to fill on.
  SESSION WINDOW    a trailing window counted in CALENDAR sessions, not in rows. Eligibility and
                    data cleaning delete rows from a symbol's frame, so a 252-row rolling max
                    would silently reach further back than 252 sessions. Here a missing row is
                    skipped inside the window and the window still ends 252 calendar sessions back.
  TOP-N SCHEDULE    on each rebalance date, the N best-scoring names that clear a threshold. Ties
                    are broken by a hash of symbol and date, never alphabetically, so which names
                    win a tied book does not depend on ticker order.

The result is exact for a rule whose selection depends only on prices on the rebalance date (a full
re-rank). A rule that depends on what is already held cannot be run symbol by symbol.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from datetime import date
from itertools import groupby

import numpy as np
import pandas as pd


def weekly_rebalance_sessions(sessions: Sequence[date]) -> list[date]:
    """The last session of each ISO week, leaving out the final session of the calendar."""
    ordered = sorted(set(sessions))
    if not ordered:
        return []
    last_of_week = [max(group) for _, group in groupby(ordered, key=lambda d: d.isocalendar()[:2])]
    return [d for d in last_of_week if d != ordered[-1]]


def close_panel(frames: Mapping[str, pd.DataFrame], sessions: Sequence[date]) -> pd.DataFrame:
    """Closes as a (calendar session x symbol) panel; a session with no bar is NaN."""
    index = pd.DatetimeIndex(sorted(set(sessions)))
    columns = {}
    for symbol in sorted(frames):
        df = frames[symbol]
        own = pd.Series(df["Close"].to_numpy(dtype=float), index=pd.DatetimeIndex(df.index.date))
        columns[symbol] = own[~own.index.duplicated(keep="last")].reindex(index)
    return pd.DataFrame(columns, index=index)


def trailing_high(close: pd.DataFrame, window: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The highest close over the trailing `window` calendar sessions, and whether history suffices.

    `history_ok` is True once the symbol has had at least `window` calendar sessions since its
    first bar, so the first valid signal is the `window`-th session of its history.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    high = close.rolling(window, min_periods=1).max()
    present = close.notna().to_numpy()
    first = present.argmax(axis=0)
    seen = present.any(axis=0)
    position = np.arange(len(close))[:, None]
    ok = (position - first[None, :] + 1 >= window) & seen[None, :]
    return high, pd.DataFrame(ok, index=close.index, columns=close.columns)


def _tie_break(symbol: str, day: date) -> str:
    return hashlib.sha1(f"{symbol}|{day.isoformat()}".encode()).hexdigest()


def top_n_schedule(
    score: pd.DataFrame, rebalance_dates: Sequence[date], n: int, min_score: float
) -> dict[date, list[str]]:
    """For each rebalance date, the names to hold: the `n` highest scores at or above `min_score`.

    A NaN score is not rankable. Fewer than `n` qualifiers means fewer positions: the empty slots
    are not filled with names below the threshold.
    """
    out: dict[date, list[str]] = {}
    for day in rebalance_dates:
        row = score.loc[pd.Timestamp(day)].dropna()
        row = row[row >= min_score]
        ranked = sorted(row.items(), key=lambda kv: (-kv[1], _tie_break(kv[0], day)))
        out[day] = [symbol for symbol, _ in ranked[:n]]
    return out


def attach_schedule(
    frames: Mapping[str, pd.DataFrame],
    rebalance_dates: Sequence[date],
    schedule: Mapping[date, Sequence[str]],
    sessions: Sequence[date],
) -> dict[str, pd.DataFrame]:
    """Copies of `frames` with the rebalance decision as three boolean columns.

    rebalance_prev  the previous CALENDAR session was a rebalance date, so this bar is the
                    execution session. A symbol with no bar on that session never gets it.
    target_prev     rebalance_prev, and the symbol is in that rebalance's held set.
    target_latest   the symbol is in the held set of the latest rebalance strictly before this bar.
                    A decision is never visible on the rebalance session itself.
    """
    calendar = sorted(set(sessions))
    position = {d: i for i, d in enumerate(calendar)}
    rebalances = sorted(set(rebalance_dates))
    rebalance_set = set(rebalances)
    held = {d: set(schedule.get(d, ())) for d in rebalances}
    out: dict[str, pd.DataFrame] = {}
    for symbol, df in frames.items():
        dates = list(df.index.date)
        prev_rebalance, in_prev, in_latest = [], [], []
        for d in dates:
            k = position.get(d, 0)
            prior = calendar[k - 1] if k > 0 else None
            is_exec = prior is not None and prior in rebalance_set
            prev_rebalance.append(is_exec)
            in_prev.append(bool(is_exec and symbol in held[prior]))
            j = bisect_left(rebalances, d) - 1
            in_latest.append(bool(j >= 0 and symbol in held[rebalances[j]]))
        copy = df.copy()
        copy["rebalance_prev"] = np.array(prev_rebalance, dtype=bool)
        copy["target_prev"] = np.array(in_prev, dtype=bool)
        copy["target_latest"] = np.array(in_latest, dtype=bool)
        out[symbol] = copy
    return out
