"""End-of-day review of the intraday paper phase.

WHAT THIS ANSWERS, DAILY

    Was OpenAlgo actually in ANALYZE mode?   - if not, the week traded real money
    What did the scanner signal?             - from the alerts table
    What did signal_engine do with it?       - from trades.db
    How did those trades end?                - fill prices and status

THE MODE CHECK IS THE POINT OF THE FIRST LINE

The whole paper phase rests on one assumption: OpenAlgo is in analyze mode, so orders reach the
sandbox rather than the broker. That assumption is invisible - a live order and a sandbox order
look identical in the logs until the money is gone. So the mode is checked and printed first,
every time, and a live-mode reading during the paper phase is reported as an alarm rather than
a footnote.

WHY IT READS TWO DATABASES

breakingtrade.db holds what the scanner CLAIMED; trades.db holds what the engine DID. They
diverge whenever a signal is rejected for risk, margin, a blacklist or a duplicate - and that
gap is usually the most informative line in the report. A review reading only one would quietly
present intentions as outcomes.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import httpx
import pandas as pd

from signal_engine.analysis.breakingtrade import store

STRATEGY = "BREAKINGTRADE"
_TRADES_DB = store._DB_PATH.replace("breakingtrade.db", "trades.db")


def trading_mode() -> tuple:
    """(mode string, is_analyze). ("unknown", False) if OpenAlgo cannot be reached."""
    try:
        from signal_engine.config import settings

        response = httpx.post(
            f"{settings.openalgo_base_url}/api/v1/analyzer",
            json={"apikey": settings.openalgo_api_key},
            timeout=15,
        )
        body = response.json()
        if body.get("status") != "success":
            return ("unknown", False)
        data = body.get("data", {})
        return (data.get("mode", "unknown"), bool(data.get("analyze_mode", False)))
    except Exception:  # noqa: BLE001 - a review must never fail on an unreachable API
        return ("unknown", False)


def signals_on(day: date) -> pd.DataFrame:
    """What the scanner emitted - its claims, not its results."""
    with sqlite3.connect(store._DB_PATH) as conn:
        return pd.read_sql_query(
            "SELECT created_at, symbol, direction, scan, delivered FROM alerts "
            "WHERE kind = 'trade_signal' AND created_at LIKE ? ORDER BY created_at",
            conn,
            params=(f"{day:%Y-%m-%d}%",),
        )


def trades_on(day: date) -> pd.DataFrame:
    """What signal_engine actually did, from its own ledger."""
    try:
        with sqlite3.connect(_TRADES_DB) as conn:
            return pd.read_sql_query(
                "SELECT received_at, symbol, direction, entry, sl, tp, quantity, status, "
                "fill_price, message FROM trades WHERE strategy = ? AND received_at LIKE ? "
                "ORDER BY received_at",
                conn,
                params=(STRATEGY, f"{day:%Y-%m-%d}%"),
            )
    except Exception:  # noqa: BLE001 - trades.db may not exist before the first trade
        return pd.DataFrame()


def report(day: date = None) -> None:
    day = day or date.today()
    mode, is_analyze = trading_mode()

    print(f"INTRADAY PAPER REVIEW - {day:%Y-%m-%d} ({day:%A})")
    print()
    if is_analyze:
        print(f"  OpenAlgo mode : {mode}  (ANALYZE - orders are sandboxed, no real money)")
    elif mode == "unknown":
        print("  OpenAlgo mode : UNREACHABLE - cannot confirm orders were sandboxed.")
        print("                  Treat today's trades as unverified until this is checked.")
    else:
        print(f"  OpenAlgo mode : {mode}")
        print("  *** WARNING: NOT in analyze mode. If the channel was enabled, these were")
        print("  *** REAL orders against real money, not paper trades. Verify immediately.")
    print()

    signals = signals_on(day)
    trades = trades_on(day)

    print(f"  signals emitted by the scanner : {len(signals)}")
    print(f"  trades taken by signal_engine  : {len(trades)}")
    if len(signals) and len(trades) < len(signals):
        print(
            f"  -> {len(signals) - len(trades)} signal(s) did not become a trade "
            "(risk limits, margin, duplicate or blacklist - check the engine log)"
        )
    print()

    if not signals.empty:
        print("  SIGNALS")
        print(signals.to_string(index=False))
        print()

    if not trades.empty:
        print("  TRADES")
        print(trades.to_string(index=False))
        filled = trades[trades["fill_price"].notna()]
        print()
        print(f"  filled: {len(filled)} of {len(trades)}")
    else:
        print("  (no trades recorded - either nothing fired, or the channel is still disabled)")

    print()
    print("  Reminder: one day is not evidence. What this week answers is whether the plumbing")
    print("  works end to end, NOT whether the strategy makes money.")
