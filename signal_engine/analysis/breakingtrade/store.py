"""Time-series storage of scanner snapshots, and the scan transitions between them.

WHY STORE EVERY SNAPSHOT RATHER THAN OVERWRITE ONE FILE

Two reasons, both load-bearing:

  1. THE EVENT IS THE TRANSITION, NOT THE STATE. "ZYDUSLIFE matches Breakaway Below PDL" is
     true for hours once it becomes true; alerting on it every poll is noise. "ZYDUSLIFE
     ENTERED Breakaway Below PDL at 12:31" happens once and is the thing worth acting on.
     That can only be computed by comparing a snapshot against the previous one.

  2. IT IS THE ONLY WAY TO FIND OUT WHETHER THESE SCANS ARE WORTH TRADING. The vendor
     publishes no win rate, and this repo's own assessment of it is blunt - see
     ../../pinescripts/intraday/breaking-trade/README.md: "our constraint has not been a
     shortage of signals - every intraday breakout variant tested so far has come out at or
     below break-even after costs. Buying a third-party scanner adds signal volume, not edge."
     A stored history of "scan X fired on symbol Y at time T" can be joined against subsequent
     price to measure whether it predicted anything, BEFORE any of it is wired to capital.

Plain sqlite3 rather than an ORM, matching signal_engine/db.py, which is the convention inside
this subsystem.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

import pandas as pd

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "breakingtrade.db",
)

# Columns promoted to real SQL columns because scans and later analysis filter on them; every
# other field of the row is kept verbatim in `payload` so nothing captured is ever lost, and a
# vendor adding a column does not require a migration before it can be recorded.
_INDEXED_FIELDS = [
    "sector",
    "price",
    "change_pct",
    "ib_pct",
    "opening",
    "open_type",
    "open_type_dir",
    "tail",
    "single_print",
    "poor_hl",
    "day_type",
    "day_type_dir",
    "tpo_pos",
    "tpo_pos_count",
    "tpo_pos_prev",
    "surge_x",
    "delivery_pct",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS snapshots (
    captured_at TEXT NOT NULL,
    kind        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    {" ".join(f"{name} TEXT," for name in _INDEXED_FIELDS)}
    payload     TEXT NOT NULL,
    PRIMARY KEY (captured_at, kind, symbol)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol ON snapshots (symbol, captured_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_daytype ON snapshots (day_type, captured_at);

CREATE TABLE IF NOT EXISTS scan_hits (
    captured_at TEXT NOT NULL,
    scan        TEXT NOT NULL,
    direction   TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    is_new      INTEGER NOT NULL,
    PRIMARY KEY (captured_at, scan, symbol)
);
CREATE INDEX IF NOT EXISTS idx_scan_hits_new ON scan_hits (is_new, captured_at);

-- One row per (day, mode, symbol) BTST qualifier - mode is "live" (candidates(), what was
-- actually actionable that day) or "retrospective" (retrospective_candidates(), the K+L+M
-- read taken after full close - never actionable, exists to compare against "live" once
-- enough day-pairs accumulate). See btst.retrospective_candidates()'s docstring.
CREATE TABLE IF NOT EXISTS btst_candidates (
    trade_day   TEXT NOT NULL,
    mode        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    delivery_pct REAL,
    change_pct  REAL,
    rank        INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (trade_day, mode, symbol)
);
CREATE INDEX IF NOT EXISTS idx_btst_candidates_day ON btst_candidates (trade_day, mode);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def _iso(moment: datetime) -> str:
    return moment.replace(second=0, microsecond=0).isoformat(sep=" ")


def save_snapshot(snapshot) -> int:
    """Append one Snapshot. Re-saving the same (captured_at, kind, symbol) replaces it, so a
    re-run of the same poll is harmless rather than duplicating rows."""
    if snapshot.captured_at is None:
        raise ValueError("snapshot has no captured_at - refusing to store an undated snapshot")

    stamp = _iso(snapshot.captured_at)
    rows = []
    for record in snapshot.frame.to_dict(orient="records"):
        values = [
            None if pd.isna(record.get(field)) else str(record.get(field))
            for field in _INDEXED_FIELDS
        ]
        payload = json.dumps(
            {k: (None if pd.isna(v) else v) for k, v in record.items()}, default=str
        )
        rows.append([stamp, snapshot.kind, record.get("symbol"), *values, payload])

    placeholders = ", ".join("?" * (3 + len(_INDEXED_FIELDS) + 1))
    columns = ", ".join(["captured_at", "kind", "symbol", *_INDEXED_FIELDS, "payload"])
    with _connect() as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO snapshots ({columns}) VALUES ({placeholders})", rows
        )
    return len(rows)


def is_duplicate_of_last(snapshot) -> bool:
    """Is this snapshot byte-identical to the most recent stored one of its kind?

    Guards against storing a session that never happened. On an exchange holiday the scanner
    still renders the previous session's table, so the clock says "trading day" while the data
    says nothing has moved. Comparing the payloads is the only reliable tell.
    """
    if snapshot.captured_at is None:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT captured_at FROM snapshots WHERE kind = ? AND captured_at < ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (snapshot.kind, _iso(snapshot.captured_at)),
        ).fetchone()
        if not row:
            return False
        previous = conn.execute(
            "SELECT symbol, payload FROM snapshots WHERE kind = ? AND captured_at = ?",
            (snapshot.kind, row[0]),
        ).fetchall()

    if len(previous) != len(snapshot.frame):
        return False
    stored = dict(previous)
    for record in snapshot.frame.to_dict(orient="records"):
        payload = json.dumps(
            {k: (None if pd.isna(v) else v) for k, v in record.items()}, default=str
        )
        if stored.get(record.get("symbol")) != payload:
            return False
    return True


def previous_hits(before: datetime) -> dict:
    """{scan_name: {symbols}} from the most recent stored poll before `before`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(captured_at) FROM scan_hits WHERE captured_at < ?", (_iso(before),)
        ).fetchone()
        if not row or row[0] is None:
            return {}
        hits = conn.execute(
            "SELECT scan, symbol FROM scan_hits WHERE captured_at = ?", (row[0],)
        ).fetchall()

    previous: dict = {}
    for scan, symbol in hits:
        previous.setdefault(scan, set()).add(symbol)
    return previous


def record_hits(results, captured_at: datetime) -> dict:
    """Store this poll's scan matches and return {scan_name: [newly appeared symbols]}.

    "New" means the symbol was not matching that same scan in the previous stored poll. The
    first ever poll reports nothing as new - with no baseline, everything would look like an
    event, which would fire a burst of false alerts on startup.
    """
    previous = previous_hits(captured_at)
    is_first_poll = not previous
    stamp = _iso(captured_at)

    new_by_scan: dict = {}
    rows = []
    for result in results:
        name = result.scan.name
        if result.matches.empty:
            continue
        seen_before = previous.get(name, set())
        for symbol in result.matches["symbol"]:
            fresh = (not is_first_poll) and symbol not in seen_before
            rows.append([stamp, name, result.scan.direction, symbol, int(fresh)])
            if fresh:
                new_by_scan.setdefault(name, []).append(symbol)

    if rows:
        with _connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO scan_hits "
                "(captured_at, scan, direction, symbol, is_new) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
    return new_by_scan


def save_btst_candidates(trade_day: str, mode: str, candidates: pd.DataFrame) -> int:
    """Persist one day's BTST qualifier list under `mode` ("live" or "retrospective") so a
    later comparison can read both without re-running the scan. Replaces any existing rows for
    this (day, mode) - idempotent if called twice for the same day.

    Returns the number of rows written.
    """
    with _connect() as conn:
        conn.execute(
            "DELETE FROM btst_candidates WHERE trade_day = ? AND mode = ?", (trade_day, mode)
        )
        if candidates is None or candidates.empty:
            return 0
        recorded_at = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        rows = [
            (
                trade_day,
                mode,
                str(row["symbol"]),
                float(row["delivery_pct"]) if pd.notna(row.get("delivery_pct")) else None,
                float(row["change_pct"]) if pd.notna(row.get("change_pct")) else None,
                rank,
                recorded_at,
            )
            for rank, (_, row) in enumerate(candidates.iterrows())
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO btst_candidates "
            "(trade_day, mode, symbol, delivery_pct, change_pct, rank, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def btst_candidates_for(trade_day: str, mode: str) -> pd.DataFrame:
    """Symbols saved for one (day, mode) - the read side of save_btst_candidates()."""
    with _connect() as conn:
        return pd.read_sql_query(
            "SELECT symbol, delivery_pct, change_pct, rank FROM btst_candidates "
            "WHERE trade_day = ? AND mode = ? ORDER BY rank",
            conn,
            params=(trade_day, mode),
        )


def history(symbol: str = None, since: datetime = None) -> pd.DataFrame:
    """Stored snapshots as a DataFrame - the input for forward-testing whether a scan
    firing at time T actually preceded a favourable move."""
    query = "SELECT captured_at, kind, symbol, " + ", ".join(_INDEXED_FIELDS) + " FROM snapshots"
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if since:
        clauses.append("captured_at >= ?")
        params.append(_iso(since))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY captured_at, symbol"

    with _connect() as conn:
        return pd.read_sql_query(query, conn, params=params)
