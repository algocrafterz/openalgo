"""upgrade/migrate_strategy_opened_at.py against a database forced back to the old schema.

Run with: uv run pytest test/test_migrate_strategy_opened_at.py -v
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "upgrade" / "migrate_strategy_opened_at.py"


@pytest.fixture(scope="module")
def mig():
    sys.path.insert(0, str(SCRIPT.parent))  # for the sibling _pragmas module
    spec = importlib.util.spec_from_file_location("migrate_strategy_opened_at", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make(tmp_path):
    main, sandbox = tmp_path / "main.db", tmp_path / "sandbox.db"
    con = sqlite3.connect(main)
    con.executescript(
        """
        CREATE TABLE strategy_closed_trades (
            id INTEGER PRIMARY KEY, strategy TEXT, symbol TEXT, exchange TEXT, product TEXT,
            mode TEXT, direction TEXT, entry_price FLOAT, closed_at DATETIME);
        CREATE TABLE strategy_positions (
            id INTEGER PRIMARY KEY, strategy TEXT, symbol TEXT, exchange TEXT, product TEXT,
            mode TEXT, quantity FLOAT, average_price FLOAT);
        """
    )
    closed = [
        # id 1: LONG matches the later of two BUY fills in tolerance
        (1, "S", "SBIN", "NSE", "MIS", "analyze", "LONG", 100.0, "2026-10-30 10:00:00"),
        # id 2: SHORT matches a SELL
        (2, "S", "SBIN", "NSE", "MIS", "analyze", "SHORT", 200.0, "2026-10-30 10:00:00"),
        # id 3: only fill is a price miss
        (3, "S", "ITC", "NSE", "MIS", "analyze", "LONG", 100.0, "2026-10-30 10:00:00"),
        # id 4: live rows are left alone
        (4, "S", "SBIN", "NSE", "MIS", "live", "LONG", 100.0, "2026-10-30 10:00:00"),
        # id 5: fill is older than the 10 day window
        (5, "S", "TCS", "NSE", "MIS", "analyze", "LONG", 100.0, "2026-10-30 10:00:00"),
    ]
    con.executemany("INSERT INTO strategy_closed_trades VALUES (?,?,?,?,?,?,?,?,?)", closed)
    con.executemany(
        "INSERT INTO strategy_positions VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, "S", "SBIN", "NSE", "MIS", "analyze", 10, 100.2),
            (2, "S", "SBIN", "NSE", "MIS", "analyze", 0, 100.0),  # flat: ignored
            (3, "S", "SBIN", "NSE", "MIS", "live", 10, 100.0),  # live: ignored
        ],
    )
    con.commit()
    con.close()
    sb = sqlite3.connect(sandbox)
    sb.execute(
        "CREATE TABLE sandbox_trades (id INTEGER PRIMARY KEY, user_id TEXT, strategy TEXT, "
        "symbol TEXT, exchange TEXT, action TEXT, price FLOAT, product TEXT, trade_timestamp DATETIME)"
    )
    fills = [
        ("S", "SBIN", "NSE", "BUY", 100.1, "MIS", "2026-10-30 09:20:00.5"),
        ("S", "SBIN", "NSE", "BUY", 100.3, "MIS", "2026-10-30 09:40:00"),
        ("S", "SBIN", "NSE", "BUY", 100.2, "MIS", "2026-10-30 10:30:00"),  # after closed_at
        ("S", "SBIN", "NSE", "SELL", 200.5, "MIS", "2026-10-30 09:30:00"),
        ("S", "ITC", "NSE", "BUY", 110.0, "MIS", "2026-10-30 09:30:00"),
        ("S", "TCS", "NSE", "BUY", 100.0, "MIS", "2026-10-15 09:30:00"),
    ]
    sb.executemany(
        "INSERT INTO sandbox_trades (user_id,strategy,symbol,exchange,action,price,product,"
        "trade_timestamp) VALUES ('',?,?,?,?,?,?,?)",
        fills,
    )
    sb.commit()
    sb.close()
    return str(main), str(sandbox)


def _values(path, table):
    con = sqlite3.connect(path)
    try:
        return dict(con.execute(f"SELECT id, opened_at FROM {table}").fetchall())
    finally:
        con.close()


def test_status_on_old_schema_changes_nothing(mig, tmp_path, capsys):
    main, sandbox = _make(tmp_path)
    assert mig.run(main, sandbox, status_only=True) == 0
    out = capsys.readouterr().out
    assert "'strategy_closed_trades': False" in out
    con = sqlite3.connect(main)
    assert "opened_at" not in {
        r[1] for r in con.execute("PRAGMA table_info(strategy_closed_trades)")
    }
    con.close()


def test_migration_adds_columns_backfills_and_is_idempotent(mig, tmp_path):
    main, sandbox = _make(tmp_path)
    assert mig.run(main, sandbox) == 0

    closed = _values(main, "strategy_closed_trades")
    assert closed[1] == "2026-10-30 09:40:00"  # latest in-tolerance fill before closed_at
    assert closed[2] == "2026-10-30 09:30:00"
    assert closed[3] is None and closed[4] is None and closed[5] is None
    positions = _values(main, "strategy_positions")
    assert positions[1] == "2026-10-30 10:30:00"  # open leg: latest fill, no upper bound
    assert positions[2] is None and positions[3] is None

    assert mig.run(main, sandbox) == 0
    assert _values(main, "strategy_closed_trades") == closed
    assert _values(main, "strategy_positions") == positions


def test_never_overwrites_existing_value(mig, tmp_path):
    main, sandbox = _make(tmp_path)
    mig.run(main, sandbox)
    con = sqlite3.connect(main)
    con.execute("UPDATE strategy_closed_trades SET opened_at='2026-01-01 00:00:00' WHERE id=1")
    con.commit()
    con.close()
    mig.run(main, sandbox)
    assert _values(main, "strategy_closed_trades")[1] == "2026-01-01 00:00:00"


def test_status_after_run_reports_only_unmatched(mig, tmp_path, capsys):
    main, sandbox = _make(tmp_path)
    mig.run(main, sandbox)
    capsys.readouterr()
    mig.run(main, sandbox, status_only=True)
    out = capsys.readouterr().out
    assert "{'strategy_positions': 0, 'strategy_closed_trades': 0}" in out.split("unmatched")[0]
    assert "'strategy_closed_trades': 2" in out  # ITC price miss, TCS outside window


def test_missing_sandbox_db_still_adds_columns(mig, tmp_path):
    main, _ = _make(tmp_path)
    assert mig.run(main, str(tmp_path / "absent.db")) == 0
    assert set(_values(main, "strategy_closed_trades").values()) == {None}
