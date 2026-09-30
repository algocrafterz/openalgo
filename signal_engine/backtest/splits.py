"""Purge and embargo at the in-sample / out-of-sample cut (acceptance criteria G3.5).

The cut is by trade entry day, so a multi-day swing trade entered just before it can be
scored in-sample while its outcome lands in the out-of-sample window: the in-sample set then
carries information from the period it is meant to be blind to. Two corrections:

  PURGE    in-sample trades whose exit falls on or after the cut are dropped.
  EMBARGO  out-of-sample trades entered in the first `embargo_sessions` sessions after the
           cut are dropped, so serial correlation across the cut cannot flatter the result.

Sessions are counted as business days, not exchange sessions, so a holiday inside the
embargo makes it one session shorter than asked.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd


def _iso(day: Any) -> str:
    return pd.Timestamp(day).date().isoformat()


def _exit_iso(trade: Any) -> str | None:
    return None if trade.exit_time is None else _iso(trade.exit_time)


def count_straddlers(trades: Sequence, oos_start: str) -> int:
    """Trades entered before the cut that exit on or after it."""
    return sum(
        1
        for t in trades
        if _iso(t.day) < oos_start and (_exit_iso(t) is not None and _exit_iso(t) >= oos_start)
    )


def purge_and_embargo(
    trades: Sequence, oos_start: str, embargo_sessions: int
) -> tuple[list, list, dict[str, int]]:
    """Split `trades` at `oos_start` with purge and embargo. Inputs are not modified.

    Returns (in_sample, out_of_sample, info). `info` records the embargo and how many trades
    each correction removed, so the record shows what was done rather than only the result.
    """
    if embargo_sessions < 0:
        raise ValueError(f"embargo_sessions must be >= 0, got {embargo_sessions}")
    embargo_end = (
        (pd.Timestamp(oos_start) + pd.offsets.BDay(embargo_sessions)).date().isoformat()
        if embargo_sessions
        else oos_start
    )
    in_sample, out_of_sample = [], []
    purged = embargoed = 0
    for t in trades:
        day = _iso(t.day)
        if day < oos_start:
            exit_day = _exit_iso(t)
            if exit_day is not None and exit_day >= oos_start:
                purged += 1
            else:
                in_sample.append(t)
        elif day < embargo_end:
            embargoed += 1
        else:
            out_of_sample.append(t)
    info = {
        "embargo_sessions": embargo_sessions,
        "purged_in_sample": purged,
        "embargoed_oos": embargoed,
    }
    return in_sample, out_of_sample, info
