#!/usr/bin/env python
"""
Strategy Book Mode Separation Migration Script for OpenAlgo

`database/strategy_book_db.py` keeps a per-strategy position book fed from
the event bus, covering both live and sandbox (analyze) orders. Before this
migration, `strategy_positions` was keyed on
`(user_id, strategy, symbol, exchange, product)` with no mode - so a paper
BUY and a live BUY of the same symbol under the same strategy name booked
into the SAME row. A strategy promoted from paper to live silently inherited
its paper P&L, and a strategy traded in both modes reported one blended
number.

This adds `mode` ("live"/"analyze"/"unknown") to both `strategy_order_tags`
(simple ADD COLUMN - not part of any constraint there) and
`strategy_positions` (part of the unique constraint - SQLite cannot alter a
UNIQUE constraint in place, so this rebuilds the table via the standard
create-new / copy-data / drop-old / rename procedure, preserving every
existing row and index, matching migrate_sandbox_trigger_pending.py's
pattern).

Existing rows are backfilled to mode='unknown', never guessed as live or
analyze: `strategy_order_tags` rows are pruned after
STRATEGY_TAG_RETENTION_DAYS (30 by default) so a `strategy_positions` leg may
already combine fills whose original tags are long gone, and no evidence
distinguishes which mode produced it. The app and the Strategy P&L page
surface 'unknown' legs as a clearly separate, labeled group rather than
silently dropping or blending them into current-mode totals.

MUST run before starting the app on code that includes this migration's
target schema - `_apply_fill_locked` always sets `mode` when creating a
`StrategyPosition` row, so an un-migrated `strategy_positions` table would
fail every fill-booking write (caught and logged, not a crash, but P&L
tracking silently stops until this migration runs).

Usage:
    cd upgrade
    uv run migrate_strategy_book_mode.py                       # Apply migration
    uv run migrate_strategy_book_mode.py --status               # Check status
    uv run migrate_strategy_book_mode.py --db-path /path/to.db  # Target a
        specific SQLite file instead of resolving DATABASE_URL - used to test
        this script against a copy of a database before running it for real.

Migration: 012
Created: 2026-09-24
"""

import argparse
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Register the app's SQLite pragmas on this process's engines, so a migration
# waits the same 15s for a write lock the running app does instead of the
# sqlite3 default of 5s (GitHub issue #1726).
import _pragmas  # noqa: F401,E402
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from utils.logging import get_logger

logger = get_logger(__name__)

MIGRATION_NAME = "strategy_book_mode_separation"
MIGRATION_VERSION = "012"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

# Every index create_all() (via database/strategy_book_db.py's SQLAlchemy
# model) creates on strategy_positions - recreated after the rebuild so
# nothing is lost. The unique constraint's own index is recreated implicitly
# by declaring it inline in the CREATE TABLE below, matching how SQLite names
# it (sqlite_autoindex_strategy_positions_N) regardless of the constraint's
# declared name - no separate statement needed for that one.
STRATEGY_POSITIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_strategy_positions_user_id ON strategy_positions(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_strategy_positions_strategy ON strategy_positions(strategy)",
    "CREATE INDEX IF NOT EXISTS ix_strategy_positions_symbol ON strategy_positions(symbol)",
]


def _resolve_db_path(explicit_path: str | None) -> str:
    if explicit_path:
        return explicit_path
    database_url = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")
    if not database_url.startswith("sqlite:///"):
        raise ValueError(
            f"This migration only supports SQLite; DATABASE_URL is {database_url!r}"
        )
    db_path = database_url.replace("sqlite:///", "")
    if not os.path.isabs(db_path):
        db_path = os.path.join(parent_dir, db_path)
    return db_path


def get_target_engine(explicit_path: str | None = None):
    """Engine bound to the database this migration should act on.

    `--db-path` (or a direct `explicit_path` call) overrides DATABASE_URL
    entirely - required for testing this script against a copy of a real
    database rather than always resolving the live path.
    """
    db_path = _resolve_db_path(explicit_path)
    logger.info(f"Strategy book mode migration target: {db_path}")
    return create_engine(f"sqlite:///{db_path}")


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table_name}
    )
    return result.fetchone() is not None


def _column_names(conn, table_name: str) -> set[str]:
    result = conn.execute(text(f"PRAGMA table_info({table_name})"))
    return {row[1] for row in result.fetchall()}


def _order_tags_needs_mode_column(conn) -> bool:
    if not _table_exists(conn, "strategy_order_tags"):
        return False
    return "mode" not in _column_names(conn, "strategy_order_tags")


def _positions_needs_rebuild(conn) -> bool:
    if not _table_exists(conn, "strategy_positions"):
        return False
    return "mode" not in _column_names(conn, "strategy_positions")


def add_order_tags_mode_column(conn):
    """Simple ADD COLUMN - `mode` is not part of any constraint on this table."""
    if not _table_exists(conn, "strategy_order_tags"):
        logger.info("strategy_order_tags table does not exist yet, nothing to migrate")
        return
    if not _order_tags_needs_mode_column(conn):
        logger.info("strategy_order_tags.mode already present, skipping")
        return

    logger.info("Adding strategy_order_tags.mode column...")
    conn.execute(text("ALTER TABLE strategy_order_tags ADD COLUMN mode VARCHAR(20)"))
    updated = conn.execute(
        text("UPDATE strategy_order_tags SET mode = 'unknown' WHERE mode IS NULL")
    ).rowcount
    conn.commit()
    logger.info(f"strategy_order_tags.mode added; backfilled {updated} row(s) to 'unknown'")


def rebuild_strategy_positions(conn):
    """Rebuild strategy_positions with `mode` in the unique constraint.

    SQLite cannot alter a UNIQUE constraint in place. Every existing row is
    backfilled to mode='unknown' - never guessed as live or analyze, since no
    evidence in this table distinguishes which mode produced it.
    """
    if not _table_exists(conn, "strategy_positions"):
        logger.info("strategy_positions table does not exist yet, nothing to migrate")
        return
    if not _positions_needs_rebuild(conn):
        logger.info("strategy_positions.mode already present, skipping")
        return

    logger.info("Rebuilding strategy_positions to add mode to the unique constraint...")

    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text("ALTER TABLE strategy_positions RENAME TO strategy_positions_old"))

    conn.execute(
        text("""
        CREATE TABLE strategy_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id VARCHAR(64) NOT NULL,
            strategy VARCHAR(120) NOT NULL,
            symbol VARCHAR(64) NOT NULL,
            exchange VARCHAR(20) NOT NULL,
            product VARCHAR(20) NOT NULL,
            mode VARCHAR(20) NOT NULL DEFAULT 'unknown',
            quantity FLOAT NOT NULL DEFAULT 0.0,
            average_price FLOAT NOT NULL DEFAULT 0.0,
            realized_pnl FLOAT NOT NULL DEFAULT 0.0,
            today_realized_pnl FLOAT NOT NULL DEFAULT 0.0,
            trade_date VARCHAR(10),
            updated_at DATETIME NOT NULL,
            CONSTRAINT uq_strategy_leg UNIQUE (user_id, strategy, symbol, exchange, product, mode)
        )
    """)
    )

    conn.execute(
        text("""
        INSERT INTO strategy_positions (
            id, user_id, strategy, symbol, exchange, product, mode,
            quantity, average_price, realized_pnl, today_realized_pnl,
            trade_date, updated_at
        )
        SELECT
            id, user_id, strategy, symbol, exchange, product, 'unknown',
            quantity, average_price, realized_pnl, today_realized_pnl,
            trade_date, updated_at
        FROM strategy_positions_old
    """)
    )

    old_count = conn.execute(text("SELECT COUNT(*) FROM strategy_positions_old")).scalar()
    new_count = conn.execute(text("SELECT COUNT(*) FROM strategy_positions")).scalar()
    if old_count != new_count:
        raise RuntimeError(
            f"Row count mismatch after rebuild: strategy_positions_old had {old_count}, "
            f"new strategy_positions has {new_count} - aborting, not dropping old table"
        )
    logger.info(f"Copied {new_count} existing strategy position row(s) to the rebuilt table")

    conn.execute(text("DROP TABLE strategy_positions_old"))

    for stmt in STRATEGY_POSITIONS_INDEXES:
        conn.execute(text(stmt))

    conn.execute(text("PRAGMA foreign_keys=ON"))
    conn.commit()
    logger.info("strategy_positions rebuilt with mode in the unique constraint")


def upgrade(db_path: str | None = None) -> bool:
    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_target_engine(db_path)
        try:
            with engine.connect() as conn:
                add_order_tags_mode_column(conn)
                rebuild_strategy_positions(conn)
        finally:
            # A one-shot CLI process exits right after this anyway, but this
            # module is also imported and called directly by
            # test/test_migrate_strategy_book_mode.py, which invokes
            # upgrade()/status() several times in one process - each creates
            # a fresh engine (QueuePool by default, not the app's NullPool;
            # migration scripts run standalone, never inside the long-lived
            # Gunicorn worker), so dispose it explicitly rather than relying
            # on GC to eventually close pooled connections.
            engine.dispose()
        logger.info(f"Migration {MIGRATION_NAME} completed successfully")
        return True
    except Exception:
        logger.exception(f"Migration {MIGRATION_NAME} failed")
        return False


def status(db_path: str | None = None) -> bool:
    try:
        logger.info(f"Checking status of migration: {MIGRATION_NAME}")
        engine = get_target_engine(db_path)
        try:
            with engine.connect() as conn:
                tags_ok = not _order_tags_needs_mode_column(conn)
                positions_ok = not _positions_needs_rebuild(conn)
        finally:
            engine.dispose()
        logger.info(
            f"strategy_order_tags.mode present: {tags_ok}; "
            f"strategy_positions rebuilt with mode: {positions_ok}"
        )
        if tags_ok and positions_ok:
            logger.info("Migration already applied")
            return True
        logger.info("Migration needed")
        return False
    except Exception:
        logger.exception("Status check failed")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"Migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--status", action="store_true", help="Check migration status")
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to a specific SQLite file, overriding DATABASE_URL (for testing against a copy)",
    )

    args = parser.parse_args()

    success = status(args.db_path) if args.status else upgrade(args.db_path)

    sys.exit(0 if success else 1)
