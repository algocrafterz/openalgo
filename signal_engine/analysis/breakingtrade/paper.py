"""Paper-trade the BTST list: record hypothetical buys, settle them next session, score them.

WHY THIS RATHER THAN TRADING IT SMALL

The BTST list has no established edge - 96 measured trades put it between -0.10% and +0.07%
against the market depending on the benchmark, none of it statistically distinguishable from
zero. Live-testing that costs real capital to learn something a ledger gives for free, and a
live size small enough to be safe is still too small to be statistically informative. So the
list is paper-traded until the ledger says otherwise.

WHAT IT SIMULATES, EXACTLY

    buy  every name on the BTST list, at the price in the snapshot that produced the list
    sell all of it in the next session, at that session's closing price

Deliberately the same close-to-close hold the backtest measured, so the paper record and the
backtest answer the same question and can be pooled. No stop and no target, because the backtest
modelled none - adding either here would make the two incomparable.

WHAT IT DOES NOT MODEL: slippage, brokerage, or whether a name was actually tradeable at the
price shown. Real costs on this book run about 0.19% per round trip, so the report prints the
result both gross and net of that.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pandas as pd

from signal_engine.analysis.breakingtrade import store

# Round-trip cost assumption for the net column: brokerage plus slippage, from PRD.md.
ROUND_TRIP_COST_PCT = 0.19

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    strategy     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    entry_at     TEXT NOT NULL,
    entry_price  REAL NOT NULL,
    exit_at      TEXT,
    exit_price   REAL,
    return_pct   REAL,
    status       TEXT NOT NULL DEFAULT 'open',
    note         TEXT,
    PRIMARY KEY (strategy, symbol, entry_at)
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades (status, entry_at);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(store._DB_PATH, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def record_entries(watchlist: pd.DataFrame, captured_at: datetime, strategy: str = "BTST") -> int:
    """Open a hypothetical position in every name on the list, at its snapshot price."""
    if watchlist is None or watchlist.empty:
        return 0
    stamp = captured_at.replace(second=0, microsecond=0).isoformat(sep=" ")
    rows = [
        (strategy, row.symbol, stamp, float(row.price), "open")
        for row in watchlist.itertuples()
        if row.price and row.price > 0
    ]
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO paper_trades "
            "(strategy, symbol, entry_at, entry_price, status) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def settle_open_trades(strategy: str = "BTST") -> int:
    """Close every open paper trade against the LATEST stored session on the first day AFTER
    its entry.

    The exit price comes from the same snapshot table the entry did, so both sides of the trade
    share one source - a few paise off the exchange's official close, but internally consistent,
    which is what matters for measuring the strategy rather than the broker.

    Originally required a snapshot stamped exactly '%15:30:00' - which only `--backfill`
    produces (the vendor's own single end-of-day read). The live `--watch` poller writes many
    snapshots a day (09:20 through 14:50/15:05) and none at 15:30, so a position opened while
    `--watch` was the only data source could never settle - it just sat 'open' forever, which is
    what happened to the whole 2026-09-04 BTST list. Using the latest snapshot of the next
    trading day instead works with either source: it's exactly right for `--backfill`'s single
    15:30 row, and for `--watch` it picks the read closest to the close (14:50, minutes before
    continuous F&O trading actually ends at 15:15) rather than the day's opening read.
    """
    with _connect() as conn:
        open_rows = conn.execute(
            "SELECT symbol, entry_at, entry_price FROM paper_trades "
            "WHERE strategy = ? AND status = 'open'",
            (strategy,),
        ).fetchall()
        if not open_rows:
            return 0

        sessions = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT captured_at FROM snapshots WHERE kind = 'market_profile' "
                "ORDER BY captured_at"
            ).fetchall()
        ]
        # Ascending order means the last write for a given calendar day is that day's latest -
        # exactly the "closest to the close" read wanted for settlement.
        latest_by_day = {s[:10]: s for s in sessions}

        settled = 0
        for symbol, entry_at, entry_price in open_rows:
            later_days = sorted(day for day in latest_by_day if day > entry_at[:10])
            if not later_days:
                continue  # the next session has not been captured yet
            exit_session = latest_by_day[later_days[0]]
            row = conn.execute(
                "SELECT price FROM snapshots WHERE kind = 'market_profile' "
                "AND captured_at = ? AND symbol = ?",
                (exit_session, symbol),
            ).fetchone()
            if not row or not row[0]:
                conn.execute(
                    "UPDATE paper_trades SET status = 'void', note = ? "
                    "WHERE strategy = ? AND symbol = ? AND entry_at = ?",
                    ("no exit price in the next session", strategy, symbol, entry_at),
                )
                continue
            exit_price = float(row[0])
            conn.execute(
                "UPDATE paper_trades SET exit_at = ?, exit_price = ?, return_pct = ?, "
                "status = 'closed' WHERE strategy = ? AND symbol = ? AND entry_at = ?",
                (
                    exit_session,
                    exit_price,
                    (exit_price / entry_price - 1) * 100,
                    strategy,
                    symbol,
                    entry_at,
                ),
            )
            settled += 1
    return settled


def ledger(strategy: str = "BTST") -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query(
            "SELECT * FROM paper_trades WHERE strategy = ? ORDER BY entry_at, symbol",
            conn,
            params=(strategy,),
        )


def report(strategy: str = "BTST") -> pd.DataFrame:
    """Settle what can be settled, then print the running paper record."""
    settled = settle_open_trades(strategy)
    trades = ledger(strategy)
    closed = trades[trades["status"] == "closed"]

    print(f"PAPER LEDGER - {strategy}")
    print(f"  settled this run : {settled}")
    print(f"  closed trades    : {len(closed)}")
    print(f"  still open       : {int((trades['status'] == 'open').sum())}")
    if closed.empty:
        print("  (nothing closed yet - a trade settles once the NEXT session is captured)")
        return trades

    gross = closed["return_pct"].mean()
    print(f"  mean gross return: {gross:+.3f}%")
    print(
        f"  mean NET return  : {gross - ROUND_TRIP_COST_PCT:+.3f}%   "
        f"(after ~{ROUND_TRIP_COST_PCT}% round-trip cost)"
    )
    print(f"  win rate (gross) : {(closed['return_pct'] > 0).mean():.0%}")
    print(
        f"  best / worst     : {closed['return_pct'].max():+.2f}% / "
        f"{closed['return_pct'].min():+.2f}%"
    )

    by_day = closed.groupby(closed["entry_at"].str[:10])["return_pct"].agg(["count", "mean"])
    print()
    print("  by entry date:")
    print(by_day.round(3).to_string())
    print()
    print("  Cost is the thing to watch: anything averaging under ~0.19% gross is losing money")
    print("  in practice, however good the win rate looks.")
    return trades
