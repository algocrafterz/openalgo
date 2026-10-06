"""Counts of what the data loaders dropped, reported without changing what they return.

`data.py` passes an optional `DataCollector` through `_clean_session`, `from_historify`
and `from_historify_daily`. The loaders fill it as a side channel; the returned frames
are identical with or without one. Kept free of duckdb so it can be imported anywhere.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

CLEAN_KEYS = ("duplicate_ts", "outside_hours", "impossible")


@dataclass
class DataCollector:
    clean_dropped: dict[str, dict[str, int]] = field(default_factory=dict)
    split_days_dropped: dict[str, list[dt.date]] = field(default_factory=dict)
    symbols_dropped_min_sessions: dict[str, int] = field(default_factory=dict)
    rows_seen: dict[str, int] = field(default_factory=dict)
    symbols_loaded: int = 0
    interval: str = ""
    #: Sessions in the loaded range on which no requested symbol was usable, with the reason
    #: (intraday_access.empty_session_reasons). Reported by the run calendar check.
    empty_sessions: dict[dt.date, str] = field(default_factory=dict)

    def record_clean(
        self, symbol: str, duplicate_ts: int = 0, outside_hours: int = 0, impossible: int = 0
    ) -> None:
        """Add dropped-row counts for a symbol; all-zero calls record nothing."""
        add = {
            "duplicate_ts": int(duplicate_ts),
            "outside_hours": int(outside_hours),
            "impossible": int(impossible),
        }
        if not any(add.values()):
            return
        cur = self.clean_dropped.setdefault(symbol, dict.fromkeys(CLEAN_KEYS, 0))
        for k, v in add.items():
            cur[k] += v

    def record_seen(self, symbol: str, rows: int) -> None:
        """Add rows read for a symbol before any cleaning (the denominator of drop shares)."""
        self.rows_seen[symbol] = self.rows_seen.get(symbol, 0) + int(rows)

    def record_split_days(self, symbol: str, days) -> None:
        days = sorted(set(days))
        if days:
            merged = set(self.split_days_dropped.get(symbol, [])) | set(days)
            self.split_days_dropped[symbol] = sorted(merged)

    def record_min_sessions(self, symbol: str, sessions: int) -> None:
        self.symbols_dropped_min_sessions[symbol] = int(sessions)

    def to_dict(self) -> dict:
        """JSON-safe, key-sorted, deterministic."""
        return {
            "clean_dropped": {
                s: {k: int(self.clean_dropped[s][k]) for k in CLEAN_KEYS}
                for s in sorted(self.clean_dropped)
            },
            "interval": self.interval,
            "rows_seen": {s: int(self.rows_seen[s]) for s in sorted(self.rows_seen)},
            "split_days_dropped": {
                s: [d.isoformat() for d in sorted(self.split_days_dropped[s])]
                for s in sorted(self.split_days_dropped)
            },
            "symbols_dropped_min_sessions": {
                s: int(self.symbols_dropped_min_sessions[s])
                for s in sorted(self.symbols_dropped_min_sessions)
            },
            "symbols_loaded": int(self.symbols_loaded),
        }

    def merge(self, other: DataCollector) -> DataCollector:
        """A new collector with both sets of counts summed; neither input changes."""
        out = DataCollector(
            interval=self.interval or other.interval,
            symbols_loaded=self.symbols_loaded + other.symbols_loaded,
        )
        for src in (self, other):
            for s, c in src.clean_dropped.items():
                out.record_clean(s, **c)
            for s, days in src.split_days_dropped.items():
                out.record_split_days(s, days)
            out.symbols_dropped_min_sessions.update(src.symbols_dropped_min_sessions)
            out.empty_sessions.update(src.empty_sessions)
            for s, n in src.rows_seen.items():
                out.record_seen(s, n)
        return out
