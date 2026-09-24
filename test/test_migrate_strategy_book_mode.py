"""
Tests for upgrade/migrate_strategy_book_mode.py.

Covers --status reporting, idempotency, and - the case that matters most for
an existing installation - running the migration against a copy of a real,
populated database rather than only a freshly created one. A migration that
only works on an empty database and fails on a populated one is the failure
mode this suite exists to catch.

Run with: uv run pytest test/test_migrate_strategy_book_mode.py -v
"""

import importlib
import os
import shutil
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MIGRATE_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "upgrade"
)


@pytest.fixture()
def migrate_mod():
    """Import upgrade/migrate_strategy_book_mode.py as a standalone module,
    matching how it is actually invoked (`cd upgrade && uv run ...py`)."""
    if MIGRATE_MODULE_PATH not in sys.path:
        sys.path.insert(0, MIGRATE_MODULE_PATH)
    if "migrate_strategy_book_mode" in sys.modules:
        del sys.modules["migrate_strategy_book_mode"]
    return importlib.import_module("migrate_strategy_book_mode")


def _make_old_schema_db(path: str, populated: bool = False) -> None:
    """Build a SQLite file shaped like the pre-migration schema (no `mode`
    anywhere), optionally with rows in it - simulating an existing
    installation, not a fresh one."""
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE strategy_order_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orderid VARCHAR(64) UNIQUE NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            strategy VARCHAR(120) NOT NULL,
            symbol VARCHAR(64) NOT NULL,
            exchange VARCHAR(20) NOT NULL,
            product VARCHAR(20) NOT NULL,
            applied_quantity FLOAT NOT NULL DEFAULT 0.0,
            applied_notional FLOAT NOT NULL DEFAULT 0.0,
            created_at DATETIME NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE strategy_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id VARCHAR(64) NOT NULL,
            strategy VARCHAR(120) NOT NULL,
            symbol VARCHAR(64) NOT NULL,
            exchange VARCHAR(20) NOT NULL,
            product VARCHAR(20) NOT NULL,
            quantity FLOAT NOT NULL DEFAULT 0.0,
            average_price FLOAT NOT NULL DEFAULT 0.0,
            realized_pnl FLOAT NOT NULL DEFAULT 0.0,
            today_realized_pnl FLOAT NOT NULL DEFAULT 0.0,
            trade_date VARCHAR(10),
            updated_at DATETIME NOT NULL,
            CONSTRAINT uq_strategy_leg UNIQUE (user_id, strategy, symbol, exchange, product)
        )
    """)
    if populated:
        con.execute(
            "INSERT INTO strategy_order_tags "
            "(orderid, user_id, strategy, symbol, exchange, product, created_at) "
            "VALUES ('o1', '', 'ORB', 'SBIN', 'NSE', 'MIS', '2026-01-01 00:00:00')"
        )
        con.execute(
            "INSERT INTO strategy_positions "
            "(user_id, strategy, symbol, exchange, product, quantity, average_price, "
            "realized_pnl, today_realized_pnl, updated_at) "
            "VALUES ('', 'ORB', 'SBIN', 'NSE', 'MIS', 10.0, 100.0, 50.0, 50.0, '2026-01-01 00:00:00')"
        )
    con.commit()
    con.close()


def test_status_reports_migration_needed_on_old_schema(tmp_path, migrate_mod):
    db_path = str(tmp_path / "old.db")
    _make_old_schema_db(db_path)

    assert migrate_mod.status(db_path) is False


def test_status_reports_already_applied_after_upgrade(tmp_path, migrate_mod):
    db_path = str(tmp_path / "old.db")
    _make_old_schema_db(db_path)
    assert migrate_mod.upgrade(db_path) is True

    assert migrate_mod.status(db_path) is True


def test_upgrade_is_idempotent(tmp_path, migrate_mod):
    db_path = str(tmp_path / "old.db")
    _make_old_schema_db(db_path, populated=True)

    assert migrate_mod.upgrade(db_path) is True
    first_count = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM strategy_positions"
    ).fetchone()[0]

    assert migrate_mod.upgrade(db_path) is True  # second run, should no-op
    second_count = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM strategy_positions"
    ).fetchone()[0]

    assert first_count == second_count == 1


def test_upgrade_backfills_existing_rows_to_unknown_mode(tmp_path, migrate_mod):
    db_path = str(tmp_path / "old.db")
    _make_old_schema_db(db_path, populated=True)

    assert migrate_mod.upgrade(db_path) is True

    con = sqlite3.connect(db_path)
    tag_mode = con.execute("SELECT mode FROM strategy_order_tags WHERE orderid='o1'").fetchone()[0]
    position_mode = con.execute("SELECT mode FROM strategy_positions").fetchone()[0]
    con.close()

    assert tag_mode == "unknown"
    assert position_mode == "unknown"


def test_upgrade_preserves_every_row_on_a_populated_copy_of_the_real_database(tmp_path, migrate_mod):
    """The mandatory check: a migration that only works on an empty database
    and fails on a populated one is the common real-world failure. This runs
    against a COPY of the actual, currently-populated live openalgo.db, not
    a synthetic fixture - proving the rebuild survives real historical data
    (real strategy names, real P&L figures, real row counts) rather than a
    toy shape this script's author happened to imagine."""
    live_db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "openalgo.db"
    )
    if not os.path.exists(live_db_path):
        pytest.skip("No live db/openalgo.db present in this environment to copy from")

    copy_path = str(tmp_path / "openalgo-live-copy.db")
    # An online, WAL-consistent copy via sqlite3's own backup API - never
    # opens the original for writing, and captures data still in the WAL
    # file that a plain file copy could miss.
    src = sqlite3.connect(live_db_path)
    dst = sqlite3.connect(copy_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    con = sqlite3.connect(copy_path)
    tables = {
        row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "strategy_order_tags" not in tables or "strategy_positions" not in tables:
        con.close()
        pytest.skip("Live database copy has no strategy book tables to migrate")

    tags_before = con.execute("SELECT COUNT(*) FROM strategy_order_tags").fetchone()[0]
    positions_before = con.execute("SELECT COUNT(*) FROM strategy_positions").fetchone()[0]
    sample_before = con.execute(
        "SELECT strategy, symbol, quantity, realized_pnl FROM strategy_positions ORDER BY id"
    ).fetchall()
    con.close()

    assert migrate_mod.upgrade(copy_path) is True

    con = sqlite3.connect(copy_path)
    tags_after = con.execute("SELECT COUNT(*) FROM strategy_order_tags").fetchone()[0]
    positions_after = con.execute("SELECT COUNT(*) FROM strategy_positions").fetchone()[0]
    sample_after = con.execute(
        "SELECT strategy, symbol, quantity, realized_pnl FROM strategy_positions ORDER BY id"
    ).fetchall()
    unknown_tags = con.execute(
        "SELECT COUNT(*) FROM strategy_order_tags WHERE mode != 'unknown' OR mode IS NULL"
    ).fetchone()[0]
    unknown_positions = con.execute(
        "SELECT COUNT(*) FROM strategy_positions WHERE mode != 'unknown'"
    ).fetchone()[0]
    con.close()

    # No data loss: identical row counts, and every pre-existing figure
    # (strategy, symbol, quantity, realized P&L) survives byte-for-byte.
    assert tags_after == tags_before
    assert positions_after == positions_before
    assert sample_after == sample_before
    # Never guessed: every pre-migration row is 'unknown', not defaulted to
    # 'live' or 'analyze'.
    assert unknown_tags == 0
    assert unknown_positions == 0

    shutil.rmtree(tmp_path, ignore_errors=True)
