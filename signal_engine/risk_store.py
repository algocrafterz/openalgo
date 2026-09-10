"""Persistent risk counter storage backed by SQLite.

Stores daily risk counters keyed by (strategy, mode, date) so that exposure limits
survive application restarts and are isolated between strategies AND between
live/sandbox modes. Each strategy gets its own capital/position/trade/loss counters —
see RiskEngine for why (paper-trading capital isolation, 2026-09-10).
"""

import os
import sqlite3
from datetime import date, timedelta

# Canonical path — all signal_engine data files live under signal_engine/data/
RISK_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "risk.db")

# Sentinel strategy key used only for rows written before the per-strategy split
# (2026-09-10). Those counters were a single shared total across every strategy —
# migrated forward under this name for audit history, never read by new code.
LEGACY_STRATEGY = "_LEGACY"


class RiskStore:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(risk_counters)")
        columns = {row[1] for row in cur.fetchall()}

        if not columns:
            # Fresh DB — create directly with the current (strategy, mode, date) schema.
            self._conn.execute("""
                CREATE TABLE risk_counters (
                    strategy    TEXT NOT NULL,
                    mode        TEXT NOT NULL,
                    trade_date  TEXT NOT NULL,
                    trades_today    INTEGER NOT NULL DEFAULT 0,
                    daily_loss      REAL NOT NULL DEFAULT 0.0,
                    daily_net_pnl   REAL NOT NULL DEFAULT 0.0,
                    open_positions  INTEGER NOT NULL DEFAULT 0,
                    day_start_capital REAL NOT NULL DEFAULT 0.0,
                    PRIMARY KEY (strategy, mode, trade_date)
                )
            """)
            self._conn.commit()
            return

        if "strategy" in columns:
            self._add_daily_net_pnl(columns)
            return

        # Pre-2026-09-10 DB: (mode, trade_date) primary key, no strategy column. SQLite
        # can't ALTER a PRIMARY KEY in place — rebuild the table, carrying old rows
        # forward under LEGACY_STRATEGY rather than discarding the history.
        self._conn.execute("ALTER TABLE risk_counters RENAME TO risk_counters_pre_strategy_split")
        self._conn.execute("""
            CREATE TABLE risk_counters (
                strategy    TEXT NOT NULL,
                mode        TEXT NOT NULL,
                trade_date  TEXT NOT NULL,
                trades_today    INTEGER NOT NULL DEFAULT 0,
                daily_loss      REAL NOT NULL DEFAULT 0.0,
                daily_net_pnl   REAL NOT NULL DEFAULT 0.0,
                open_positions  INTEGER NOT NULL DEFAULT 0,
                day_start_capital REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (strategy, mode, trade_date)
            )
        """)
        self._conn.execute(f"""
            INSERT INTO risk_counters
                (strategy, mode, trade_date, trades_today, daily_loss, daily_net_pnl,
                 open_positions, day_start_capital)
            SELECT '{LEGACY_STRATEGY}', mode, trade_date, trades_today, daily_loss, -daily_loss,
                   open_positions, day_start_capital
            FROM risk_counters_pre_strategy_split
        """)
        self._conn.commit()

    def _add_daily_net_pnl(self, columns: set) -> None:
        """Add the NET P&L column to a pre-2026-09-11 table, backfilled from the row itself.

        Idempotent and safe to re-run. Backfill is -daily_loss, not 0.0: the row's own gross
        loss is the only evidence available for a day whose wins were never recorded, and
        assuming no wins is the conservative reading — it can only make a limit fire earlier,
        never later. Rows written from here on carry the real net figure.
        """
        if "daily_net_pnl" in columns:
            return
        self._conn.execute(
            "ALTER TABLE risk_counters ADD COLUMN daily_net_pnl REAL NOT NULL DEFAULT 0.0"
        )
        self._conn.execute("UPDATE risk_counters SET daily_net_pnl = -daily_loss")
        self._conn.commit()

    def save(self, strategy: str, mode: str, trade_date: date, *,
             trades_today: int, daily_loss: float, open_positions: int,
             day_start_capital: float = 0.0, daily_net_pnl: float = 0.0) -> None:
        self._conn.execute("""
            INSERT INTO risk_counters
                (strategy, mode, trade_date, trades_today, daily_loss, daily_net_pnl,
                 open_positions, day_start_capital)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy, mode, trade_date) DO UPDATE SET
                trades_today = excluded.trades_today,
                daily_loss = excluded.daily_loss,
                daily_net_pnl = excluded.daily_net_pnl,
                open_positions = excluded.open_positions,
                day_start_capital = excluded.day_start_capital
        """, (strategy, mode, trade_date.isoformat(), trades_today, daily_loss, daily_net_pnl,
              open_positions, day_start_capital))
        self._conn.commit()

    def load(self, strategy: str, mode: str, trade_date: date) -> dict:
        cur = self._conn.execute(
            "SELECT trades_today, daily_loss, open_positions, day_start_capital, daily_net_pnl "
            "FROM risk_counters WHERE strategy = ? AND mode = ? AND trade_date = ?",
            (strategy, mode, trade_date.isoformat()),
        )
        row = cur.fetchone()
        if row is None:
            return {"trades_today": 0, "daily_loss": 0.0, "open_positions": 0,
                    "day_start_capital": 0.0, "daily_net_pnl": 0.0}
        return {"trades_today": row[0], "daily_loss": row[1], "open_positions": row[2],
                "day_start_capital": row[3], "daily_net_pnl": row[4]}

    def strategies_for(self, mode: str, trade_date: date) -> list:
        """Strategies with a persisted row for this mode/date — used to restore
        per-strategy state after a restart without needing to know the strategy set
        up front (strategy names come from free-text Telegram signal headers)."""
        cur = self._conn.execute(
            "SELECT DISTINCT strategy FROM risk_counters WHERE mode = ? AND trade_date = ? "
            "AND strategy != ?",
            (mode, trade_date.isoformat(), LEGACY_STRATEGY),
        )
        return [row[0] for row in cur.fetchall()]

    def weekly_loss(self, strategy: str, mode: str, ref_date: date) -> float:
        week_start = ref_date - timedelta(days=ref_date.weekday())
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(daily_loss), 0.0) FROM risk_counters "
            "WHERE strategy = ? AND mode = ? AND trade_date >= ? AND trade_date <= ?",
            (strategy, mode, week_start.isoformat(), ref_date.isoformat()),
        )
        return cur.fetchone()[0]

    def monthly_loss(self, strategy: str, mode: str, ref_date: date) -> float:
        month_start = ref_date.replace(day=1)
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(daily_loss), 0.0) FROM risk_counters "
            "WHERE strategy = ? AND mode = ? AND trade_date >= ? AND trade_date <= ?",
            (strategy, mode, month_start.isoformat(), ref_date.isoformat()),
        )
        return cur.fetchone()[0]

    def weekly_net_pnl(self, strategy: str, mode: str, ref_date: date) -> float:
        """Signed net realised P&L this ISO week. Negative = drawdown; the weekly limit
        measures this rather than the gross daily_loss column — see RiskEngine."""
        week_start = ref_date - timedelta(days=ref_date.weekday())
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(daily_net_pnl), 0.0) FROM risk_counters "
            "WHERE strategy = ? AND mode = ? AND trade_date >= ? AND trade_date <= ?",
            (strategy, mode, week_start.isoformat(), ref_date.isoformat()),
        )
        return cur.fetchone()[0]

    def monthly_net_pnl(self, strategy: str, mode: str, ref_date: date) -> float:
        """Signed net realised P&L this calendar month — see weekly_net_pnl()."""
        month_start = ref_date.replace(day=1)
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(daily_net_pnl), 0.0) FROM risk_counters "
            "WHERE strategy = ? AND mode = ? AND trade_date >= ? AND trade_date <= ?",
            (strategy, mode, month_start.isoformat(), ref_date.isoformat()),
        )
        return cur.fetchone()[0]
