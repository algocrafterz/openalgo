"""Persistent risk counter storage backed by SQLite.

Stores daily risk counters keyed by (mode, date) so that exposure limits
survive application restarts and are isolated between live/sandbox modes.
"""

import sqlite3
from datetime import date, timedelta


class RiskStore:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS risk_counters (
                mode        TEXT NOT NULL,
                trade_date  TEXT NOT NULL,
                trades_today    INTEGER NOT NULL DEFAULT 0,
                daily_loss      REAL NOT NULL DEFAULT 0.0,
                open_positions  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (mode, trade_date)
            )
        """)
        self._conn.commit()

    def save(self, mode: str, trade_date: date, *,
             trades_today: int, daily_loss: float, open_positions: int) -> None:
        self._conn.execute("""
            INSERT INTO risk_counters (mode, trade_date, trades_today, daily_loss, open_positions)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mode, trade_date) DO UPDATE SET
                trades_today = excluded.trades_today,
                daily_loss = excluded.daily_loss,
                open_positions = excluded.open_positions
        """, (mode, trade_date.isoformat(), trades_today, daily_loss, open_positions))
        self._conn.commit()

    def load(self, mode: str, trade_date: date) -> dict:
        cur = self._conn.execute(
            "SELECT trades_today, daily_loss, open_positions FROM risk_counters "
            "WHERE mode = ? AND trade_date = ?",
            (mode, trade_date.isoformat()),
        )
        row = cur.fetchone()
        if row is None:
            return {"trades_today": 0, "daily_loss": 0.0, "open_positions": 0}
        return {"trades_today": row[0], "daily_loss": row[1], "open_positions": row[2]}

    def weekly_loss(self, mode: str, ref_date: date) -> float:
        week_start = ref_date - timedelta(days=ref_date.weekday())
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(daily_loss), 0.0) FROM risk_counters "
            "WHERE mode = ? AND trade_date >= ? AND trade_date <= ?",
            (mode, week_start.isoformat(), ref_date.isoformat()),
        )
        return cur.fetchone()[0]

    def monthly_loss(self, mode: str, ref_date: date) -> float:
        month_start = ref_date.replace(day=1)
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(daily_loss), 0.0) FROM risk_counters "
            "WHERE mode = ? AND trade_date >= ? AND trade_date <= ?",
            (mode, month_start.isoformat(), ref_date.isoformat()),
        )
        return cur.fetchone()[0]
