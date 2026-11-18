#!/usr/bin/env python
"""
Discretionary Book Migration Script for OpenAlgo

Creates the Stage 2 derived tables of the Discretionary Trading Performance module:
`disc_positions`, `disc_trades`, `disc_trade_fills`, `disc_orphans`, `disc_build_meta`
(plus indexes). They are derived from `ledger_fills` and rebuilt by the service; this only
creates the empty tables.

Create-only: uses CREATE TABLE / INDEX IF NOT EXISTS, never alters, drops or rewrites
rows, so running it twice changes nothing.

Usage:
    cd upgrade
    uv run migrate_discretionary_book.py
    uv run migrate_discretionary_book.py --status
    uv run migrate_discretionary_book.py --db-path /path/to.db
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _pragmas  # noqa: F401,E402
from dotenv import load_dotenv

from utils.logging import get_logger

logger = get_logger(__name__)

MIGRATION_NAME = "discretionary_book"
MIGRATION_VERSION = "016"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

TABLES = ("disc_positions", "disc_trades", "disc_trade_fills", "disc_orphans", "disc_build_meta")

DDL = (
    """
    CREATE TABLE IF NOT EXISTS disc_positions (
        id INTEGER NOT NULL PRIMARY KEY,
        build_id VARCHAR(40) NOT NULL,
        broker VARCHAR(40) NOT NULL,
        account VARCHAR(64) NOT NULL,
        exchange VARCHAR(20) NOT NULL,
        symbol VARCHAR(80) NOT NULL,
        product VARCHAR(20) NOT NULL,
        position_seq INTEGER NOT NULL,
        direction VARCHAR(5) NOT NULL,
        open_qty FLOAT NOT NULL,
        avg_entry FLOAT NOT NULL,
        opened_at DATETIME NOT NULL,
        opened_session VARCHAR(10) NOT NULL,
        realized_gross FLOAT NOT NULL,
        entry_charges_carried FLOAT,
        opening_unknown BOOLEAN NOT NULL,
        status VARCHAR(5) NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_disc_positions_identity "
    "ON disc_positions (broker, account, exchange, symbol, product)",
    "CREATE INDEX IF NOT EXISTS ix_disc_positions_status ON disc_positions (status)",
    """
    CREATE TABLE IF NOT EXISTS disc_trades (
        id INTEGER NOT NULL PRIMARY KEY,
        build_id VARCHAR(40) NOT NULL,
        broker VARCHAR(40) NOT NULL,
        account VARCHAR(64) NOT NULL,
        exchange VARCHAR(20) NOT NULL,
        symbol VARCHAR(80) NOT NULL,
        product VARCHAR(20) NOT NULL,
        position_seq INTEGER NOT NULL,
        direction VARCHAR(5) NOT NULL,
        closed_quantity FLOAT NOT NULL,
        entry_price FLOAT NOT NULL,
        exit_price FLOAT NOT NULL,
        opened_at DATETIME NOT NULL,
        closed_at DATETIME NOT NULL,
        trade_date VARCHAR(10) NOT NULL,
        overnight BOOLEAN NOT NULL,
        hold_minutes FLOAT,
        realized_pnl FLOAT NOT NULL,
        charges FLOAT,
        charges_status VARCHAR(12) NOT NULL,
        net_pnl FLOAT,
        charges_breakdown_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_disc_trades_identity "
    "ON disc_trades (broker, account, exchange, symbol, product)",
    "CREATE INDEX IF NOT EXISTS ix_disc_trades_trade_date ON disc_trades (trade_date)",
    """
    CREATE TABLE IF NOT EXISTS disc_trade_fills (
        id INTEGER NOT NULL PRIMARY KEY,
        build_id VARCHAR(40) NOT NULL,
        trade_id INTEGER,
        position_id INTEGER,
        fill_ref INTEGER NOT NULL,
        role VARCHAR(5) NOT NULL,
        qty_used FLOAT NOT NULL,
        charge_allocated FLOAT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_disc_trade_fills_trade ON disc_trade_fills (trade_id)",
    "CREATE INDEX IF NOT EXISTS ix_disc_trade_fills_position ON disc_trade_fills (position_id)",
    "CREATE INDEX IF NOT EXISTS ix_disc_trade_fills_fill ON disc_trade_fills (fill_ref)",
    """
    CREATE TABLE IF NOT EXISTS disc_orphans (
        id INTEGER NOT NULL PRIMARY KEY,
        build_id VARCHAR(40) NOT NULL,
        fill_ref INTEGER,
        reason VARCHAR(60) NOT NULL,
        qty FLOAT NOT NULL,
        broker VARCHAR(40),
        account VARCHAR(64),
        exchange VARCHAR(20),
        symbol VARCHAR(80),
        product VARCHAR(20),
        side VARCHAR(4),
        price FLOAT,
        fill_time DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS disc_build_meta (
        build_id VARCHAR(40) NOT NULL PRIMARY KEY,
        built_at DATETIME NOT NULL,
        fingerprint VARCHAR(64) NOT NULL,
        fill_count INTEGER NOT NULL,
        engine_version VARCHAR(20) NOT NULL
    )
    """,
)


def _resolve(explicit: str | None, env_name: str, default: str) -> str:
    if explicit:
        return explicit
    url = os.getenv(env_name, f"sqlite:///{default}")
    if not url.startswith("sqlite:///"):
        raise ValueError(f"This migration only supports SQLite; {env_name} is {url!r}")
    path = url.replace("sqlite:///", "")
    return path if os.path.isabs(path) else os.path.join(parent_dir, path)


def _existing(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows} & set(TABLES)


def run(db_path: str, status_only: bool = False) -> int:
    con = sqlite3.connect(db_path, timeout=30)
    try:
        before = _existing(con)
        if status_only:
            missing = [t for t in TABLES if t not in before]
            print(f"{MIGRATION_NAME}: present={sorted(before)} missing={missing}")
            return 0
        for stmt in DDL:
            con.execute(stmt)
        con.commit()
        created = [t for t in TABLES if t not in before]
        logger.info(f"{MIGRATION_NAME}: created {created or 'none (present)'}")
        print(f"{MIGRATION_NAME}: done. created={created or 'none'}")
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create the discretionary book (derived) tables")
    ap.add_argument("--status", action="store_true", help="report only, change nothing")
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args(argv)
    db_path = _resolve(args.db_path, "DATABASE_URL", "db/openalgo.db")
    logger.info(f"Discretionary book migration target: {db_path}")
    return run(db_path, status_only=args.status)


if __name__ == "__main__":
    sys.exit(main())
