#!/usr/bin/env python
"""
Strategy Book Broker Column Migration Script for OpenAlgo

Brokerage differs per broker (Flattrade charges none; others charge per order), so the
Performance page prices each closed trade with the fee schedule of the broker that placed it.
That needs the broker on the record. This adds a nullable `broker` column to
`strategy_order_tags` (stamped when the order is placed) and `strategy_closed_trades` (copied
from the closing order's tag), then backfills existing rows from evidence, never from a guess.

Backfill, in order of strength:
  1. an order tag whose order id is in `db/latency.db`'s `order_latency` log takes that row's
     broker (the log records the broker of every order the API handled);
  2. otherwise the broker that handled most orders on the same calendar day in that log;
  3. otherwise 'unknown', which the Performance page prices on an assumed default schedule and
     labels as such.
Closed trades carry no order id, so they take rule 2 (a trading day has one broker in practice).

Only rows whose broker is still NULL are touched, so a value set by the running app, or by an
earlier run, is never overwritten. Idempotent and safe to re-run.

Usage:
    cd upgrade
    uv run migrate_strategy_book_broker.py                       # Apply migration
    uv run migrate_strategy_book_broker.py --status              # What would change
    uv run migrate_strategy_book_broker.py --db-path /path/to.db --latency-db-path /path/l.db

Migration: 013
Created: 2026-10-01
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

MIGRATION_NAME = "strategy_book_broker"
MIGRATION_VERSION = "013"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

TABLES = ("strategy_order_tags", "strategy_closed_trades")


def _resolve(explicit: str | None, env_name: str, default: str) -> str:
    if explicit:
        return explicit
    url = os.getenv(env_name, f"sqlite:///{default}")
    if not url.startswith("sqlite:///"):
        raise ValueError(f"This migration only supports SQLite; {env_name} is {url!r}")
    path = url.replace("sqlite:///", "")
    return path if os.path.isabs(path) else os.path.join(parent_dir, path)


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _load_latency(latency_path: str) -> tuple[dict[str, str], dict[str, str]]:
    """(order_id -> broker, 'YYYY-MM-DD' -> the broker that handled most orders that day)."""
    if not os.path.exists(latency_path):
        return {}, {}
    con = sqlite3.connect(latency_path)
    try:
        if not _table_exists(con, "order_latency"):
            return {}, {}
        by_order = {
            str(oid): broker.strip().lower()
            for oid, broker in con.execute(
                "SELECT order_id, broker FROM order_latency "
                "WHERE broker IS NOT NULL AND broker != '' AND order_id IS NOT NULL "
                "AND order_id NOT IN ('', 'unknown')"
            )
        }
        counts: dict[str, dict[str, int]] = {}
        for day, broker, n in con.execute(
            "SELECT substr(timestamp, 1, 10), broker, COUNT(*) FROM order_latency "
            "WHERE broker IS NOT NULL AND broker != '' GROUP BY 1, 2"
        ):
            counts.setdefault(day, {})[broker.strip().lower()] = n
        by_day = {day: max(c, key=c.get) for day, c in counts.items()}
        return by_order, by_day
    finally:
        con.close()


def _add_columns(conn) -> list[str]:
    added = []
    for table in TABLES:
        if _table_exists(conn, table) and "broker" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN broker VARCHAR(40)")
            added.append(table)
    return added


def _backfill(conn, latency_path: str) -> dict[str, int]:
    by_order, by_day = _load_latency(latency_path)
    result = {
        "tags_by_order": 0,
        "tags_by_day": 0,
        "tags_unknown": 0,
        "trades_by_day": 0,
        "trades_unknown": 0,
    }
    if _table_exists(conn, "strategy_order_tags"):
        rows = conn.execute(
            "SELECT id, orderid, substr(COALESCE(created_at, ''), 1, 10) "
            "FROM strategy_order_tags WHERE broker IS NULL"
        ).fetchall()
        for rid, orderid, day in rows:
            if orderid in by_order:
                broker, key = by_order[orderid], "tags_by_order"
            elif day in by_day:
                broker, key = by_day[day], "tags_by_day"
            else:
                broker, key = "unknown", "tags_unknown"
            result[key] += 1
            conn.execute("UPDATE strategy_order_tags SET broker=? WHERE id=?", (broker, rid))
    if _table_exists(conn, "strategy_closed_trades"):
        rows = conn.execute(
            "SELECT id, trade_date FROM strategy_closed_trades WHERE broker IS NULL"
        ).fetchall()
        for rid, day in rows:
            broker = by_day.get(day)
            result["trades_by_day" if broker else "trades_unknown"] += 1
            conn.execute(
                "UPDATE strategy_closed_trades SET broker=? WHERE id=?", (broker or "unknown", rid)
            )
    return result


def _status(conn, latency_path: str) -> str:
    missing = [t for t in TABLES if _table_exists(conn, t) and "broker" not in _columns(conn, t)]
    pending = {}
    for table in TABLES:
        if not _table_exists(conn, table):
            continue
        has = "broker" in _columns(conn, table)
        sql = f"SELECT COUNT(*) FROM {table}" + (" WHERE broker IS NULL" if has else "")
        pending[table] = conn.execute(sql).fetchone()[0]
    by_order, by_day = _load_latency(latency_path)
    return (
        f"{MIGRATION_NAME}: columns to add: {missing or 'none'}; rows to backfill: {pending}; "
        f"latency log knows {len(by_order)} order(s) and {len(by_day)} day(s)"
    )


def run(db_path: str, latency_path: str, status_only: bool = False) -> int:
    con = sqlite3.connect(db_path, timeout=30)
    try:
        if status_only:
            print(_status(con, latency_path))
            return 0
        added = _add_columns(con)
        counts = _backfill(con, latency_path)
        con.commit()
        logger.info(f"{MIGRATION_NAME}: added to {added or 'none (present)'}; backfilled {counts}")
        print(f"{MIGRATION_NAME}: done. added={added or 'none'} backfilled={counts}")
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Add and backfill the broker column on the strategy book")
    ap.add_argument("--status", action="store_true", help="report what would change, change nothing")
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--latency-db-path", default=None)
    args = ap.parse_args(argv)
    db_path = _resolve(args.db_path, "DATABASE_URL", "db/openalgo.db")
    latency = _resolve(args.latency_db_path, "LATENCY_DATABASE_URL", "db/latency.db")
    logger.info(f"Strategy book broker migration target: {db_path} (latency log: {latency})")
    return run(db_path, latency, status_only=args.status)


if __name__ == "__main__":
    sys.exit(main())
