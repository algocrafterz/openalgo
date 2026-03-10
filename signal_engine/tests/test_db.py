"""Tests for trade persistence — RED phase first."""

import os
import sqlite3

import pytest

from signal_engine.db import save, _get_connection
from signal_engine.models import (
    Action,
    Direction,
    Order,
    OrderStatus,
    Signal,
    TradeResult,
)
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
        "strategy_tag": "ORB",
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
