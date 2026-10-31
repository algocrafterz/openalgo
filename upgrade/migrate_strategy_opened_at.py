#!/usr/bin/env python
"""
Strategy Book Entry Time Migration Script for OpenAlgo

Adds a nullable `opened_at` column to `strategy_positions` (when the open leg was
entered) and to `strategy_closed_trades` (the leg's entry time as it was before the
closing fill). The Strategy Performance page uses them for holding-time and
entry-hour analytics.

Backfill, analyze (sandbox) rows only. Live rows are left NULL because OpenAlgo keeps
no local record of live fills.
  - A closed trade takes the latest opening-side sandbox fill (LONG -> BUY, SHORT ->
    SELL) with the same strategy, symbol, exchange and product, filled at or before
    `closed_at` and at most 10 days before it, at a price within 0.5% of `entry_price`.
  - An open leg (quantity != 0) takes the latest opening-side fill of the same key
    whose price is within 0.5% of `average_price`.
A non-NULL `opened_at` is never overwritten. Idempotent and safe to re-run.

Usage:
    cd upgrade
    uv run migrate_strategy_opened_at.py                 # Apply migration
    uv run migrate_strategy_opened_at.py --status        # What would change
    uv run migrate_strategy_opened_at.py --db-path /path/to.db --sandbox-db-path /path/s.db

Migration: 014
Created: 2026-10-31
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _pragmas  # noqa: F401,E402
from dotenv import load_dotenv

from utils.logging import get_logger

logger = get_logger(__name__)

MIGRATION_NAME = "strategy_opened_at"
MIGRATION_VERSION = "014"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

TABLES = ("strategy_positions", "strategy_closed_trades")
PRICE_TOLERANCE = 0.005
LOOKBACK = timedelta(days=10)


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


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("T", " "))
    except (ValueError, TypeError):
        return None


def _load_fills(sandbox_path: str) -> dict[tuple, list[tuple[datetime, float]]]:
    """(strategy, symbol, exchange, product, action) -> [(time, price)] ascending."""
    fills: dict[tuple, list[tuple[datetime, float]]] = {}
    if not sandbox_path or not os.path.exists(sandbox_path):
        return fills
    con = sqlite3.connect(sandbox_path, timeout=30)
    try:
        if not _table_exists(con, "sandbox_trades"):
            return fills
        rows = con.execute(
            "SELECT strategy, symbol, exchange, product, action, price, trade_timestamp "
            "FROM sandbox_trades WHERE strategy IS NOT NULL AND strategy != ''"
        ).fetchall()
    finally:
        con.close()
    for strategy, symbol, exchange, product, action, price, ts in rows:
        when = _parse(ts)
        if when is None:
            continue
        key = (strategy, symbol, exchange, product, str(action).upper())
        fills.setdefault(key, []).append((when, float(price)))
    for series in fills.values():
        series.sort(key=lambda f: f[0])
    return fills


def _latest_fill(series, price: float, not_after: datetime | None, not_before: datetime | None):
    """Latest fill in the window whose price is within tolerance of `price`."""
    if price <= 0:
        return None
    for when, fill_price in reversed(series):
        if not_after is not None and when > not_after:
            continue
        if not_before is not None and when < not_before:
            return None  # ascending series: everything earlier is older still
        if abs(fill_price - price) / price <= PRICE_TOLERANCE:
            return when
    return None


def _pending_rows(conn, table: str) -> list[tuple]:
    """Analyze rows still lacking an entry time, without needing the column to exist."""
    has = "opened_at" in _columns(conn, table)
    null_clause = " AND opened_at IS NULL" if has else ""
    if table == "strategy_closed_trades":
        sql = (
            "SELECT id, strategy, symbol, exchange, product, direction, entry_price, closed_at "
            f"FROM strategy_closed_trades WHERE mode='analyze'{null_clause}"
        )
    else:
        sql = (
            "SELECT id, strategy, symbol, exchange, product, quantity, average_price, NULL "
            f"FROM strategy_positions WHERE mode='analyze' AND quantity != 0{null_clause}"
        )
    return conn.execute(sql).fetchall()


def _plan(conn, sandbox_path: str) -> dict:
    """Matches for the rows to backfill: {table: [(id, datetime)]} plus unmatched counts."""
    fills = _load_fills(sandbox_path)
    plan: dict = {"matches": {t: [] for t in TABLES}, "unmatched": dict.fromkeys(TABLES, 0)}
    for table in TABLES:
        if not _table_exists(conn, table):
            continue
        for rid, strategy, symbol, exchange, product, side, price, closed_at in _pending_rows(
            conn, table
        ):
            if table == "strategy_closed_trades":
                action = "BUY" if side == "LONG" else "SELL"
                end = _parse(closed_at)
                start = end - LOOKBACK if end is not None else None
            else:
                action = "BUY" if side > 0 else "SELL"
                end = start = None
            series = fills.get((strategy, symbol, exchange, product, action), [])
            found = None
            if table == "strategy_positions" or end is not None:
                found = _latest_fill(series, float(price or 0), end, start)
            if found is None:
                plan["unmatched"][table] += 1
            else:
                plan["matches"][table].append((rid, found))
    return plan


def _add_columns(conn) -> list[str]:
    added = []
    for table in TABLES:
        if _table_exists(conn, table) and "opened_at" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN opened_at DATETIME")
            added.append(table)
    return added


def _summary(plan: dict) -> str:
    matched = {t: len(m) for t, m in plan["matches"].items()}
    return f"rows to backfill: {matched}; unmatched: {plan['unmatched']}"


def _status(conn, sandbox_path: str) -> str:
    present = {t: "opened_at" in _columns(conn, t) for t in TABLES if _table_exists(conn, t)}
    return f"{MIGRATION_NAME}: columns present: {present}; " + _summary(_plan(conn, sandbox_path))


def run(db_path: str, sandbox_path: str, status_only: bool = False) -> int:
    con = sqlite3.connect(db_path, timeout=30)
    try:
        if status_only:
            print(_status(con, sandbox_path))
            return 0
        added = _add_columns(con)
        plan = _plan(con, sandbox_path)
        for table, matches in plan["matches"].items():
            for rid, when in matches:
                # Guarded on NULL so a value set by the running app is never overwritten.
                con.execute(
                    f"UPDATE {table} SET opened_at=? WHERE id=? AND opened_at IS NULL",
                    (when.isoformat(sep=" "), rid),
                )
        con.commit()
        logger.info(f"{MIGRATION_NAME}: added to {added or 'none (present)'}; {_summary(plan)}")
        print(f"{MIGRATION_NAME}: done. added={added or 'none'} {_summary(plan)}")
        return 0
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Add and backfill opened_at on the strategy book")
    ap.add_argument(
        "--status", action="store_true", help="report what would change, change nothing"
    )
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--sandbox-db-path", default=None)
    args = ap.parse_args(argv)
    db_path = _resolve(args.db_path, "DATABASE_URL", "db/openalgo.db")
    sandbox = _resolve(args.sandbox_db_path, "SANDBOX_DATABASE_URL", "db/sandbox.db")
    logger.info(f"Strategy opened_at migration target: {db_path} (sandbox fills: {sandbox})")
    return run(db_path, sandbox, status_only=args.status)


if __name__ == "__main__":
    sys.exit(main())
