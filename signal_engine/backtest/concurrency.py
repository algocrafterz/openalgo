"""How many positions a trade list needs at once, and what is left with only N slots.

A per-trade edge means nothing to an account that cannot afford every trade. EMA pullback
looked like +131 bps a trade until it turned out to want a median of 54 and a peak of 138
simultaneous positions; at 10 slots it was negative. Two tools expose that:

  concurrency_profile  positions open at the moment each trade opened (itself included).
  apply_position_cap   the trades a fixed number of slots would actually have taken.

Rules, fixed here so runs stay comparable:

  FIRST COME, FIRST SERVED.  Signals are taken in entry-time order. A signal is skipped
  when every slot is full, and is not queued for later.

  A SLOT FREES AT THE EXIT.  A trade that exits at or before the next entry has already
  released its slot. A trade with no exit time is treated as open forever.

  NO ALPHABETICAL BIAS.  Signals at the same instant (a daily strategy fires them all at
  the open) are ordered by a hash of symbol and time, so which names win a full book is
  deterministic but unrelated to ticker order, and independent of input order.
"""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Sequence
from statistics import median
from typing import Any

import pandas as pd

_OPEN_FOREVER = pd.Timestamp.max


def _entry(trade: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(trade.entry_time) if trade.entry_time is not None else pd.NaT
    if pd.isna(stamp):
        raise ValueError(f"trade on {trade.symbol} has no entry_time; cannot place it in time")
    return stamp


def _exit(trade: Any) -> pd.Timestamp:
    if trade.exit_time is None:
        return _OPEN_FOREVER
    stamp = pd.Timestamp(trade.exit_time)
    return _OPEN_FOREVER if pd.isna(stamp) else stamp


def _tie_break(trade: Any) -> str:
    return hashlib.sha1(f"{trade.symbol}|{trade.entry_time}".encode()).hexdigest()


def _in_entry_order(trades: Sequence[Any]) -> list[Any]:
    return sorted(trades, key=lambda t: (_entry(t), _tie_break(t)))


def concurrency_profile(trades: Sequence[Any]) -> dict[str, float]:
    """Peak and median of the positions open when each trade opened, itself included."""
    open_exits: list[pd.Timestamp] = []
    counts: list[int] = []
    for trade in _in_entry_order(trades):
        entry = _entry(trade)
        while open_exits and open_exits[0] <= entry:
            heapq.heappop(open_exits)
        heapq.heappush(open_exits, _exit(trade))
        counts.append(len(open_exits))
    if not counts:
        return {"peak": 0, "median": 0.0}
    return {"peak": max(counts), "median": float(median(counts))}


def apply_position_cap(trades: Sequence[Any], max_positions: int) -> list[Any]:
    """The trades taken with `max_positions` slots, in entry order. Inputs are not changed."""
    if isinstance(max_positions, bool) or max_positions < 1:
        raise ValueError(f"max_positions must be >= 1, got {max_positions!r}")
    open_exits: list[pd.Timestamp] = []
    taken: list[Any] = []
    for trade in _in_entry_order(trades):
        entry = _entry(trade)
        while open_exits and open_exits[0] <= entry:
            heapq.heappop(open_exits)
        if len(open_exits) < max_positions:
            heapq.heappush(open_exits, _exit(trade))
            taken.append(trade)
    return taken
