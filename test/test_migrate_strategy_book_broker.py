"""upgrade/migrate_strategy_book_broker.py: add the broker column and backfill it from evidence.

Covers the cases that matter on an existing install: a populated old-schema database, re-runs
(idempotent), a value already set by the running app (never overwritten), and no latency log.

Run with: uv run pytest test/test_migrate_strategy_book_broker.py -v
"""

import importlib
import os
import sqlite3
import sys

import pytest

UPGRADE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "upgrade")


@pytest.fixture()
def mig():
    if UPGRADE not in sys.path:
        sys.path.insert(0, UPGRADE)
    sys.modules.pop("migrate_strategy_book_broker", None)
    return importlib.import_module("migrate_strategy_book_broker")


def _book(path, populated=True):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE strategy_order_tags (id INTEGER PRIMARY KEY AUTOINCREMENT, orderid TEXT, "
        "created_at TEXT, mode TEXT)"
    )
    con.execute(
        "CREATE TABLE strategy_closed_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "trade_date TEXT, realized_pnl REAL)"
    )
    if populated:
        con.executemany(
            "INSERT INTO strategy_order_tags (orderid, created_at, mode) VALUES (?,?,?)",
            [
                ("O-KNOWN", "2026-09-30 10:00:00", "analyze"),  # in the latency log
                ("O-SAMEDAY", "2026-09-30 11:00:00", "analyze"),  # not logged, day is known
                ("O-NOEVIDENCE", "2026-01-02 11:00:00", "analyze"),  # no data for that day
            ],
        )
        con.executemany(
            "INSERT INTO strategy_closed_trades (trade_date, realized_pnl) VALUES (?,?)",
            [("2026-09-30", 10.0), ("2026-01-02", 5.0)],
        )
    con.commit()
    con.close()


def _latency(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE order_latency (id INTEGER PRIMARY KEY, timestamp TEXT, order_id TEXT, "
        "broker TEXT)"
    )
    con.executemany(
        "INSERT INTO order_latency (timestamp, order_id, broker) VALUES (?,?,?)",
        [
            ("2026-09-30 10:00:01", "O-KNOWN", "Flattrade"),
            ("2026-09-30 10:05:00", "unknown", "flattrade"),
            ("2026-09-30 10:06:00", "unknown", "flattrade"),
            ("2026-09-30 10:07:00", "unknown", "mstock"),
        ],
    )
    con.commit()
    con.close()


def _rows(path, table):
    con = sqlite3.connect(path)
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    finally:
        con.close()


def test_adds_the_column_and_backfills_by_order_then_by_day_then_unknown(mig, tmp_path):
    db, lat = str(tmp_path / "book.db"), str(tmp_path / "lat.db")
    _book(db)
    _latency(lat)
    assert mig.run(db, lat) == 0
    tags = {r[1]: r[-1] for r in _rows(db, "strategy_order_tags")}
    assert tags == {"O-KNOWN": "flattrade", "O-SAMEDAY": "flattrade", "O-NOEVIDENCE": "unknown"}
    trades = {r[1]: r[-1] for r in _rows(db, "strategy_closed_trades")}
    assert trades == {"2026-09-30": "flattrade", "2026-01-02": "unknown"}


def test_idempotent_and_never_overwrites_a_broker_already_set(mig, tmp_path):
    db, lat = str(tmp_path / "book.db"), str(tmp_path / "lat.db")
    _book(db)
    _latency(lat)
    mig.run(db, lat)
    con = sqlite3.connect(db)
    con.execute("UPDATE strategy_order_tags SET broker='zerodha' WHERE orderid='O-KNOWN'")
    con.commit()
    con.close()
    first = _rows(db, "strategy_order_tags")
    assert mig.run(db, lat) == 0
    assert _rows(db, "strategy_order_tags") == first
    assert {r[1]: r[-1] for r in first}["O-KNOWN"] == "zerodha"


def test_status_changes_nothing(mig, tmp_path, capsys):
    db, lat = str(tmp_path / "book.db"), str(tmp_path / "lat.db")
    _book(db)
    _latency(lat)
    before = _rows(db, "strategy_order_tags")
    assert mig.run(db, lat, status_only=True) == 0
    out = capsys.readouterr().out
    assert "columns to add" in out and "strategy_order_tags" in out
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(strategy_order_tags)")}
    con.close()
    assert "broker" not in cols and _rows(db, "strategy_order_tags") == before


def test_missing_latency_log_leaves_everything_unknown_not_guessed(mig, tmp_path):
    db = str(tmp_path / "book.db")
    _book(db)
    assert mig.run(db, str(tmp_path / "absent.db")) == 0
    assert {r[-1] for r in _rows(db, "strategy_order_tags")} == {"unknown"}
    assert {r[-1] for r in _rows(db, "strategy_closed_trades")} == {"unknown"}


def test_empty_tables_and_missing_tables_are_fine(mig, tmp_path):
    db = str(tmp_path / "book.db")
    _book(db, populated=False)
    assert mig.run(db, str(tmp_path / "absent.db")) == 0
    empty = str(tmp_path / "empty.db")
    sqlite3.connect(empty).close()
    assert mig.run(empty, str(tmp_path / "absent.db")) == 0
