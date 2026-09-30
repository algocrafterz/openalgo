"""Idempotency for the runner: last processed bar per symbol, and seen signal ids.

A restart mid-day must neither re-fire a bar nor skip one. `last_bar` records the newest bar
already evaluated for (mode, day, strategy, symbol); `seen_sig_id` answers "was this trade
key ever produced", from this store or from trades.db (DECLINED rows included).
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from signal_engine import db
from signal_engine.timeutils import IST

STATE_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "runner_state.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS last_bar (
    mode TEXT NOT NULL, day TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
    bar_epoch INTEGER NOT NULL,
    PRIMARY KEY (mode, day, strategy, symbol)
);
CREATE TABLE IF NOT EXISTS sig_ids (sig_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS shadow_events (
    day TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL, bar_epoch INTEGER NOT NULL,
    kind TEXT NOT NULL, direction TEXT NOT NULL DEFAULT '', sig_id TEXT NOT NULL DEFAULT '',
    entry REAL, sl REAL, tp REAL, reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (day, strategy, symbol, bar_epoch, kind)
);
"""


@dataclass(frozen=True)
class ShadowEvent:
    """One thing the shadow runner did on a bar: would have sent a signal, or skipped and why."""

    day: dt.date
    strategy: str
    symbol: str
    bar_time: pd.Timestamp
    kind: str  # "signal" | "skip"
    direction: str = ""
    sig_id: str = ""
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    reason: str = ""


class RunnerStateStore:
    """One long-lived connection, closed by the owner (context manager or close())."""

    def __init__(
        self, path: str = STATE_DB_PATH, trades_lookup: Callable[[str], bool] | None = None
    ) -> None:
        self._conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._trades_lookup = trades_lookup or db.sig_id_exists

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        self._conn = None

    def __enter__(self) -> RunnerStateStore:
        return self

    def __exit__(self, *_exc) -> bool:
        self.close()
        return False

    def last_bar(self, mode: str, day: dt.date, strategy: str, symbol: str) -> pd.Timestamp | None:
        row = self._conn.execute(
            "SELECT bar_epoch FROM last_bar WHERE mode=? AND day=? AND strategy=? AND symbol=?",
            (mode, day.isoformat(), strategy, symbol),
        ).fetchone()
        if row is None:
            return None
        return pd.Timestamp(row[0], unit="s", tz="UTC").tz_convert(IST)

    def mark(
        self, mode: str, day: dt.date, strategy: str, symbol: str, bar_time: pd.Timestamp
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO last_bar (mode, day, strategy, symbol, bar_epoch) VALUES (?,?,?,?,?) "
                "ON CONFLICT(mode, day, strategy, symbol) DO UPDATE SET "
                "bar_epoch = MAX(bar_epoch, excluded.bar_epoch)",
                (mode, day.isoformat(), strategy, symbol, int(bar_time.timestamp())),
            )

    def seen_sig_id(self, sig_id: str, include_trades: bool = True) -> bool:
        """True when this key was produced before. `include_trades=False` ignores trades.db.

        Shadow mode must not consult trades.db: a sig_id Pine already sent would then look
        "seen" and the runner's matching signal would never be recorded for the comparison.
        """
        row = self._conn.execute("SELECT 1 FROM sig_ids WHERE sig_id=?", (sig_id,)).fetchone()
        if row is not None:
            return True
        return include_trades and self._trades_lookup(sig_id)

    def record_sig_id(self, sig_id: str) -> None:
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO sig_ids (sig_id) VALUES (?)", (sig_id,))

    def last_bars(self, day: dt.date, strategy: str) -> dict[str, pd.Timestamp]:
        """Newest evaluated bar per symbol for the day, across trade modes."""
        rows = self._conn.execute(
            "SELECT symbol, MAX(bar_epoch) FROM last_bar WHERE day=? AND strategy=? GROUP BY symbol",
            (day.isoformat(), strategy),
        ).fetchall()
        return {sym: pd.Timestamp(ep, unit="s", tz="UTC").tz_convert(IST) for sym, ep in rows}

    def record_shadow(
        self,
        day: dt.date,
        strategy: str,
        symbol: str,
        bar_time: pd.Timestamp,
        kind: str,
        *,
        direction: str = "",
        sig_id: str = "",
        entry: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        reason: str = "",
    ) -> None:
        """Durable record of a shadow signal or skip. Idempotent per (day, symbol, bar, kind)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO shadow_events "
                "(day, strategy, symbol, bar_epoch, kind, direction, sig_id, entry, sl, tp, reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    day.isoformat(), strategy, symbol, int(bar_time.timestamp()), kind,
                    direction, sig_id, entry, sl, tp, reason,
                ),
            )

    def shadow_events(self, day: dt.date, strategy: str) -> list[ShadowEvent]:
        rows = self._conn.execute(
            "SELECT symbol, bar_epoch, kind, direction, sig_id, entry, sl, tp, reason "
            "FROM shadow_events WHERE day=? AND strategy=? ORDER BY bar_epoch, rowid",
            (day.isoformat(), strategy),
        ).fetchall()
        return [
            ShadowEvent(
                day, strategy, sym, pd.Timestamp(ep, unit="s", tz="UTC").tz_convert(IST),
                kind, direction, sid, entry, sl, tp, reason,
            )
            for sym, ep, kind, direction, sid, entry, sl, tp, reason in rows
        ]
