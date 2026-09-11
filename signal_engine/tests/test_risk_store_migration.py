"""The risk.db migrations, run against databases forced back to each older schema.

CLAUDE.md's rule: a migration that works on an empty database and fails on a populated one is
the common failure, so each test below starts from a REAL old-schema table with rows in it.

Two migrations have accumulated:
  pre-2026-09-10  (mode, trade_date) primary key, no `strategy` column. SQLite cannot ALTER a
                  primary key, so the table is rebuilt and old rows carried forward under
                  LEGACY_STRATEGY rather than discarded.
  pre-2026-09-11  no `daily_net_pnl` column. Added in place and backfilled from the row's own
                  -daily_loss, which is the conservative reading: it can only make a limit
                  fire earlier, never later.
"""

import sqlite3
from datetime import date, timedelta

import pytest

from signal_engine.risk_store import LEGACY_STRATEGY, RiskStore

MODE = "live"
TODAY = date(2026, 9, 11)


def _old_no_strategy(path):
    """The pre-2026-09-10 schema, with rows."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE risk_counters (
            mode TEXT NOT NULL, trade_date TEXT NOT NULL,
            trades_today INTEGER NOT NULL DEFAULT 0,
            daily_loss REAL NOT NULL DEFAULT 0.0,
            open_positions INTEGER NOT NULL DEFAULT 0,
            day_start_capital REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (mode, trade_date)
        )
    """)
    conn.execute("INSERT INTO risk_counters VALUES (?, ?, 3, 1200.0, 1, 35000.0)",
                 (MODE, TODAY.isoformat()))
    conn.commit()
    conn.close()


def _old_no_net_pnl(path):
    """The pre-2026-09-11 schema: split by strategy, but no daily_net_pnl."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE risk_counters (
            strategy TEXT NOT NULL, mode TEXT NOT NULL, trade_date TEXT NOT NULL,
            trades_today INTEGER NOT NULL DEFAULT 0,
            daily_loss REAL NOT NULL DEFAULT 0.0,
            open_positions INTEGER NOT NULL DEFAULT 0,
            day_start_capital REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (strategy, mode, trade_date)
        )
    """)
    conn.execute("INSERT INTO risk_counters VALUES ('ORB', ?, ?, 4, 1400.0, 2, 35000.0)",
                 (MODE, TODAY.isoformat()))
    conn.execute("INSERT INTO risk_counters VALUES ('BREAKOUT', ?, ?, 2, 0.0, 0, 35000.0)",
                 (MODE, TODAY.isoformat()))
    conn.commit()
    conn.close()


class TestStrategySplitMigration:
    def test_old_rows_survive_under_the_legacy_tag(self, tmp_path):
        path = str(tmp_path / "risk.db")
        _old_no_strategy(path)
        with RiskStore(path) as store:
            row = store.load(LEGACY_STRATEGY, MODE, TODAY)
        assert row["trades_today"] == 3
        assert row["daily_loss"] == 1200.0
        assert row["day_start_capital"] == 35000.0

    def test_legacy_rows_are_hidden_from_restore(self, tmp_path):
        """_LEGACY is audit history, never a bucket new code reads."""
        path = str(tmp_path / "risk.db")
        _old_no_strategy(path)
        with RiskStore(path) as store:
            assert store.strategies_for(MODE, TODAY) == []

    def test_the_backfilled_net_is_the_conservative_reading(self, tmp_path):
        path = str(tmp_path / "risk.db")
        _old_no_strategy(path)
        with RiskStore(path) as store:
            assert store.load(LEGACY_STRATEGY, MODE, TODAY)["daily_net_pnl"] == -1200.0

    def test_the_original_table_is_kept_not_dropped(self, tmp_path):
        path = str(tmp_path / "risk.db")
        _old_no_strategy(path)
        RiskStore(path).close()
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "risk_counters_pre_strategy_split" in tables


class TestNetPnlMigration:
    def test_the_column_is_added_and_backfilled_per_row(self, tmp_path):
        path = str(tmp_path / "risk.db")
        _old_no_net_pnl(path)
        with RiskStore(path) as store:
            assert store.load("ORB", MODE, TODAY)["daily_net_pnl"] == -1400.0
            assert store.load("BREAKOUT", MODE, TODAY)["daily_net_pnl"] == 0.0

    def test_existing_columns_are_untouched(self, tmp_path):
        path = str(tmp_path / "risk.db")
        _old_no_net_pnl(path)
        with RiskStore(path) as store:
            row = store.load("ORB", MODE, TODAY)
        assert (row["trades_today"], row["daily_loss"], row["open_positions"]) == (4, 1400.0, 2)

    def test_re_running_is_a_no_op(self, tmp_path):
        """Every startup re-opens the store; the migration must be safe to re-run."""
        path = str(tmp_path / "risk.db")
        _old_no_net_pnl(path)
        RiskStore(path).close()
        with RiskStore(path) as store:
            store.save("ORB", MODE, TODAY, trades_today=5, daily_loss=1400.0,
                       open_positions=1, daily_net_pnl=250.0)
        with RiskStore(path) as store:
            assert store.load("ORB", MODE, TODAY)["daily_net_pnl"] == 250.0

    def test_a_fresh_database_already_has_the_column(self, tmp_path):
        with RiskStore(str(tmp_path / "new.db")) as store:
            assert store.load("ORB", MODE, TODAY)["daily_net_pnl"] == 0.0


class TestNetAggregates:
    def test_weekly_net_sums_both_signs(self, tmp_path):
        with RiskStore(str(tmp_path / "risk.db")) as store:
            monday = TODAY - timedelta(days=TODAY.weekday())
            store.save("ORB", MODE, monday, trades_today=1, daily_loss=500.0,
                       open_positions=0, daily_net_pnl=-500.0)
            store.save("ORB", MODE, TODAY, trades_today=1, daily_loss=0.0,
                       open_positions=0, daily_net_pnl=900.0)
            assert store.weekly_net_pnl("ORB", MODE, TODAY) == 400.0

    def test_monthly_net_sums_both_signs(self, tmp_path):
        with RiskStore(str(tmp_path / "risk.db")) as store:
            store.save("ORB", MODE, TODAY.replace(day=1), trades_today=1, daily_loss=800.0,
                       open_positions=0, daily_net_pnl=-800.0)
            store.save("ORB", MODE, TODAY, trades_today=1, daily_loss=0.0,
                       open_positions=0, daily_net_pnl=300.0)
            assert store.monthly_net_pnl("ORB", MODE, TODAY) == -500.0

    def test_another_mode_is_never_mixed_in(self, tmp_path):
        with RiskStore(str(tmp_path / "risk.db")) as store:
            store.save("ORB", "analyze", TODAY, trades_today=1, daily_loss=0.0,
                       open_positions=0, daily_net_pnl=-9999.0)
            assert store.weekly_net_pnl("ORB", "live", TODAY) == 0.0


class TestLifecycle:
    def test_close_is_idempotent(self, tmp_path):
        store = RiskStore(str(tmp_path / "risk.db"))
        store.close()
        store.close()

    def test_context_manager_closes_on_exit(self, tmp_path):
        with RiskStore(str(tmp_path / "risk.db")) as store:
            pass
        assert store._conn is None

    def test_an_exception_inside_the_block_still_closes(self, tmp_path):
        store = RiskStore(str(tmp_path / "risk.db"))
        with pytest.raises(ValueError), store:
            raise ValueError("boom")
        assert store._conn is None
