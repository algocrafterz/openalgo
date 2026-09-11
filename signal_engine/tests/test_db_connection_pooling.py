"""L1/L2: one connection per process, schema checked once, writes off the event loop.

Every db.py call used to open a connection, re-run CREATE TABLE, PRAGMA table_info and
possibly ALTER TABLE, then close — all synchronously, on the asyncio event loop. The
BreakingTrade poller is a separate process that also writes trades.db (flip_watch.py), so a
write-lock collision could block the loop for up to `timeout=10` — stalling the position
poll and SL placement for ten seconds during market hours.
"""

import sqlite3
import threading

import pytest

from signal_engine import db
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
    db.reset_connection()
    yield
    db.reset_connection()


def _signal(symbol="SBIN"):
    return Signal(strategy="ORB", direction=Direction.LONG, symbol=symbol,
                  entry=800.0, sl=796.0, tp=810.0, raw_message="x")


def _order(symbol="SBIN"):
    return Order(symbol=symbol, exchange="NSE", action=Action.BUY, quantity=10,
                 price=0.0, order_type="MARKET", product="MIS", strategy_tag="ORB")


class TestConnectionIsReused:
    def test_the_same_connection_object_comes_back(self):
        assert db._get_connection() is db._get_connection()

    def test_reset_hands_out_a_new_one(self):
        first = db._get_connection()
        db.reset_connection()
        assert db._get_connection() is not first

    def test_reset_closes_the_old_connection(self):
        conn = db._get_connection()
        db.reset_connection()
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_each_thread_gets_its_own_connection(self):
        """Sharing ONE handle across threads corrupts the Python statement cache — the same
        hazard the root CLAUDE.md records for StaticPool. See _get_connection()."""
        seen = []
        main = db._get_connection()

        def _grab():
            seen.append(db._get_connection())

        t = threading.Thread(target=_grab)
        t.start()
        t.join()
        assert seen[0] is not main


class TestSchemaIsCheckedOnce:
    def test_migration_does_not_re_run_on_every_call(self, monkeypatch):
        db._get_connection()  # first call builds the schema
        calls = []
        real = db._add_missing_columns
        monkeypatch.setattr(db, "_add_missing_columns", lambda c: calls.append(1) or real(c))
        for _ in range(5):
            db._get_connection()
        assert calls == []


class TestConcurrentUseIsSafe:
    def test_a_write_from_another_thread_does_not_raise(self):
        """check_same_thread=False plus a lock — the tracker's poll and an exit handler can
        touch this from different threads once writes move off the loop."""
        errors = []

        def _write(n):
            try:
                db.save(_signal(f"SYM{n}"), _order(f"SYM{n}"),
                        TradeResult(status=OrderStatus.SUCCESS, order_id=f"O{n}"))
            except Exception as e:  # noqa: BLE001 - the assertion is that there are none
                errors.append(e)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # The row COUNT is the real assertion: save() catches and logs its own exceptions
        # (deliberately - a DB failure must not break signal handling), so a lost write
        # shows up as a missing row and an empty error list, not as a raise.
        conn = db._get_connection()
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 8


class TestWritesStillPersist:
    def test_a_saved_trade_is_readable_back(self):
        db.save(_signal(), _order("SBIN"),
                TradeResult(status=OrderStatus.SUCCESS, order_id="O1"))
        rows = db._get_connection().execute(
            "SELECT symbol, order_id FROM trades").fetchall()
        assert rows == [("SBIN", "O1")]
