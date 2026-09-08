"""Tests for trade persistence — RED phase first."""


import json
import sqlite3

import pytest

from signal_engine.db import _get_connection, fetch_all_open_positions, save, save_reconciled_exit
from signal_engine.models import (
    Action,
    Order,
    OrderStatus,
    TradeResult,
)
from signal_engine.strategies import ORB
from signal_engine.tests.conftest import make_signal as _make_signal


def _make_order(**overrides) -> Order:
    defaults = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": Action.BUY,
        "quantity": 10,
        "price": 0,
        "order_type": "MARKET",
        "product": "MIS",
        "strategy_tag": ORB,
    }
    defaults.update(overrides)
    return Order(**defaults)


def _make_result(**overrides) -> TradeResult:
    defaults = {
        "order_id": "12345",
        "status": OrderStatus.SUCCESS,
        "message": "Order placed",
    }
    defaults.update(overrides)
    return TradeResult(**defaults)


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_trades.db")
    monkeypatch.setattr("signal_engine.db._DB_PATH", db_path)


class TestDatabaseOperations:
    def test_table_created_on_connection(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "fresh.db")
        monkeypatch.setattr("signal_engine.db._DB_PATH", db_path)
        conn = _get_connection()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_save_single_trade(self):
        signal = _make_signal()
        order = _make_order()
        result = _make_result()
        save(signal, order, result)

        conn = _get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM trades")
        assert cursor.fetchone()[0] == 1
        conn.close()

    def test_save_multiple_trades(self):
        for i in range(3):
            save(_make_signal(), _make_order(), _make_result(order_id=str(i)))

        conn = _get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM trades")
        assert cursor.fetchone()[0] == 3
        conn.close()

    def test_column_values_stored_correctly(self):
        signal = _make_signal(strategy="VWAP", symbol="TCS")
        order = _make_order(symbol="TCS", quantity=25, strategy_tag="VWAP")
        result = _make_result(order_id="ABC123", message="filled")
        save(signal, order, result)

        conn = _get_connection()
        row = conn.execute("SELECT * FROM trades").fetchone()
        conn.close()

        # row[0] is id
        assert row[1] == "VWAP"       # strategy
        assert row[2] == "LONG"       # direction
        assert row[3] == "TCS"        # symbol
        assert row[4] == 2500.0       # entry
        assert row[5] == 2485.0       # sl
        assert row[6] == 2540.0       # tp
        assert row[7] == 25           # quantity
        assert row[8] == "ABC123"     # order_id
        assert row[9] == "SUCCESS"    # status
        assert row[10] == "filled"    # message

    def test_save_error_does_not_raise(self, monkeypatch):
        monkeypatch.setattr("signal_engine.db._DB_PATH", "/invalid/path/db.db")
        # Should log error but not raise
        save(_make_signal(), _make_order(), _make_result())


class TestContextPersistence:
    """The trade log is the only durable record of why a signal fired. Entry criteria must
    survive to it or a 30-day attribution study has nothing to group by."""

    def test_raw_message_and_context_persisted(self):
        save(
            _make_signal(
                raw_message="BREAKOUT LONG\nSymbol: TATASTEEL\nScore: 9",
                context={"score": "9", "rvol": "1.5", "trigger": "VAH-RT"},
            ),
            _make_order(),
            _make_result(),
        )
        conn = _get_connection()
        raw, ctx = conn.execute("SELECT raw_message, context FROM trades").fetchone()
        conn.close()
        assert "Score: 9" in raw
        assert json.loads(ctx) == {"score": "9", "rvol": "1.5", "trigger": "VAH-RT"}

    def test_empty_context_stored_as_empty_object(self):
        save(_make_signal(), _make_order(), _make_result())
        conn = _get_connection()
        (ctx,) = conn.execute("SELECT context FROM trades").fetchone()
        conn.close()
        assert json.loads(ctx) == {}

    def test_legacy_table_gains_new_columns(self, tmp_path, monkeypatch):
        """An existing trades.db predates these columns and must not need a manual migration."""
        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT, "
            "direction TEXT, symbol TEXT, entry REAL, sl REAL, tp REAL, quantity INTEGER, "
            "order_id TEXT, status TEXT, message TEXT, signal_time TEXT, received_at TEXT, "
            "executed_at TEXT)"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("signal_engine.db._DB_PATH", path)
        save(_make_signal(context={"score": "7"}), _make_order(), _make_result())

        conn = sqlite3.connect(path)
        (ctx,) = conn.execute("SELECT context FROM trades").fetchone()
        conn.close()
        assert json.loads(ctx) == {"score": "7"}


class TestOpenPositionReconciliation:
    """fetch_all_open_positions() / save_reconciled_exit() - startup.reconcile_open_positions()
    uses these to find and backfill a position the broker closed while the engine was down."""

    def test_open_entry_with_no_exit_is_returned(self):
        save(_make_signal(symbol="HINDALCO"), _make_order(symbol="HINDALCO"), _make_result())
        open_positions = fetch_all_open_positions()
        assert len(open_positions) == 1
        assert open_positions[0]["symbol"] == "HINDALCO"
        assert open_positions[0]["direction"] == "LONG"

    def test_entry_followed_by_exit_is_not_open(self):
        from signal_engine.models import Direction

        save(_make_signal(symbol="HINDALCO"), _make_order(symbol="HINDALCO"), _make_result())
        save(
            _make_signal(symbol="HINDALCO", direction=Direction.EXIT),
            _make_order(symbol="HINDALCO"),
            _make_result(),
        )
        assert fetch_all_open_positions() == []

    def test_only_todays_entries_are_considered(self, monkeypatch):
        """A position genuinely opened on an earlier day (should never happen for MIS, but the
        query must not silently reach back regardless) is out of scope for today's reconciliation."""
        conn = _get_connection()
        conn.execute(
            "INSERT INTO trades (strategy, direction, symbol, status, executed_at) "
            "VALUES ('ORB', 'LONG', 'OLDSYM', 'SUCCESS', '2020-01-01T09:20:00+00:00')"
        )
        conn.commit()
        conn.close()
        assert fetch_all_open_positions() == []

    def test_save_reconciled_exit_writes_a_success_exit_row(self):
        save_reconciled_exit(
            "BREAKOUT", "HINDALCO", 1022.4, 1019.43, 1026.85, 107, 1019.4, -363.8,
            "Reconciled at startup",
        )
        conn = _get_connection()
        row = conn.execute(
            "SELECT strategy, direction, symbol, status, fill_price FROM trades"
        ).fetchone()
        conn.close()
        assert row == ("BREAKOUT", "EXIT", "HINDALCO", "SUCCESS", 1019.4)

    def test_save_reconciled_exit_removes_the_position_from_open_positions(self):
        # Same strategy tag (ORB, _make_signal's default) as the reconciled exit below - the
        # two rows must be recognised as the SAME (strategy, symbol) position for this to prove
        # anything.
        save(_make_signal(symbol="HINDALCO"), _make_order(symbol="HINDALCO"), _make_result())
        assert len(fetch_all_open_positions()) == 1

        save_reconciled_exit(
            "ORB", "HINDALCO", 1022.4, 1019.43, 1026.85, 107, 1019.4, -363.8,
            "Reconciled at startup",
        )
        assert fetch_all_open_positions() == []
