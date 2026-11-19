#!/usr/bin/env python
"""
Discretionary Ledger Migration Script for OpenAlgo

Creates the Stage 1 tables of the Discretionary Trading Performance module:
`ledger_fills`, `ledger_sync_runs`, `ledger_attribution_audit`,
`ledger_account_identity` (plus indexes).

Create-only: uses CREATE TABLE / INDEX IF NOT EXISTS, plus a plain ADD COLUMN for each
later nullable column a table lacks (ledger_fills.source_evidence,
ledger_attribution_audit.evidence; existing rows stay NULL). It never drops or rewrites rows,
so running it twice changes nothing.

Usage:
    cd upgrade
    uv run migrate_discretionary_ledger.py
    uv run migrate_discretionary_ledger.py --status
    uv run migrate_discretionary_ledger.py --db-path /path/to.db
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

MIGRATION_NAME = "discretionary_ledger"
MIGRATION_VERSION = "015"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

TABLES = (
    "ledger_fills",
    "ledger_sync_runs",
    "ledger_attribution_audit",
    "ledger_account_identity",
)

DDL = (
    """
    CREATE TABLE IF NOT EXISTS ledger_fills (
        id INTEGER NOT NULL PRIMARY KEY,
        broker VARCHAR(40) NOT NULL,
        account VARCHAR(64) NOT NULL,
        order_id VARCHAR(64) NOT NULL,
        fill_id VARCHAR(64),
        symbol VARCHAR(80) NOT NULL,
        exchange VARCHAR(20) NOT NULL,
        product VARCHAR(20) NOT NULL,
        side VARCHAR(4) NOT NULL,
        qty FLOAT NOT NULL,
        price FLOAT NOT NULL,
        fill_time DATETIME NOT NULL,
        occurrence_idx INTEGER NOT NULL,
        dedup_key VARCHAR(400) NOT NULL,
        attribution_state VARCHAR(20) NOT NULL,
        strategy VARCHAR(120),
        synced_at DATETIME NOT NULL,
        raw_hash VARCHAR(64),
        source_evidence TEXT,
        UNIQUE (dedup_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ledger_fills_broker_account_time "
    "ON ledger_fills (broker, account, fill_time)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_fills_attribution_state "
    "ON ledger_fills (attribution_state)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_fills_order_id ON ledger_fills (order_id)",
    """
    CREATE TABLE IF NOT EXISTS ledger_sync_runs (
        id INTEGER NOT NULL PRIMARY KEY,
        broker VARCHAR(40) NOT NULL,
        account VARCHAR(64) NOT NULL,
        started_at DATETIME NOT NULL,
        finished_at DATETIME,
        status VARCHAR(20) NOT NULL,
        fetched INTEGER NOT NULL,
        inserted INTEGER NOT NULL,
        error TEXT,
        tag_min_created_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_attribution_audit (
        id INTEGER NOT NULL PRIMARY KEY,
        fill_ref INTEGER NOT NULL,
        dedup_key VARCHAR(400) NOT NULL,
        from_state VARCHAR(20),
        to_state VARCHAR(20) NOT NULL,
        reason VARCHAR(200) NOT NULL,
        source VARCHAR(20) NOT NULL,
        at DATETIME NOT NULL,
        evidence VARCHAR(200)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ledger_attribution_audit_fill_ref "
    "ON ledger_attribution_audit (fill_ref)",
    """
    CREATE TABLE IF NOT EXISTS ledger_account_identity (
        id INTEGER NOT NULL PRIMARY KEY,
        broker VARCHAR(40) NOT NULL,
        auth_name VARCHAR(255) NOT NULL,
        account VARCHAR(64) NOT NULL,
        first_seen DATETIME NOT NULL,
        CONSTRAINT uq_ledger_account_identity_broker_auth UNIQUE (broker, auth_name)
    )
    """,
)


# Nullable columns added after the first release (same list as
# database/discretionary_ledger_db.ADDED_COLUMNS; kept here so the script needs no app import).
ADDED_COLUMNS = (
    ("ledger_fills", "source_evidence", "TEXT"),
    ("ledger_attribution_audit", "evidence", "VARCHAR(200)"),
)


def _missing_columns(conn) -> list[tuple[str, str, str]]:
    tables = _existing(conn)
    out = []
    for table, column, sql_type in ADDED_COLUMNS:
        if table not in tables:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            out.append((table, column, sql_type))
    return out


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
            cols = [f"{t}.{c}" for t, c, _ in _missing_columns(con)]
            print(
                f"{MIGRATION_NAME}: present={sorted(before)} missing={missing} "
                f"missing_columns={cols}"
            )
            return 0
        for stmt in DDL:
            con.execute(stmt)
        added = []
        for table, column, sql_type in _missing_columns(con):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
            added.append(f"{table}.{column}")
        con.commit()
        created = [t for t in TABLES if t not in before]
        logger.info(
            f"{MIGRATION_NAME}: created {created or 'none (present)'}; "
            f"columns added {added or 'none'}"
        )
        print(
            f"{MIGRATION_NAME}: done. created={created or 'none'} columns_added={added or 'none'}"
        )
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create the discretionary ledger tables")
    ap.add_argument("--status", action="store_true", help="report only, change nothing")
    ap.add_argument("--db-path", default=None)
    args = ap.parse_args(argv)
    db_path = _resolve(args.db_path, "DATABASE_URL", "db/openalgo.db")
    logger.info(f"Discretionary ledger migration target: {db_path}")
    return run(db_path, status_only=args.status)


if __name__ == "__main__":
    sys.exit(main())
