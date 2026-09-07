"""Alert-only warning when a currently-open BREAKINGTRADE position's own setup reverses.

WHAT THIS ANSWERS

A scan match is a read of the tape at one instant (gap_down/rejection/tail columns from the
market-profile snapshot). The trade it produces runs on a fixed SL/TP after that - nothing
re-checks whether the read it was based on is still true. Most of the time the SL is what
catches a reversal anyway, since trigger.stop_level() places the stop just beyond the same tail
the entry was built on. This module is the gap between "the SL hasn't been hit yet" and "the
setup that justified the position no longer holds" - visible to a human before it would
otherwise only show up as an instinct that something felt wrong.

WHY ALERT-ONLY, NOT AN AUTO-EXIT

This is Day 1 of the paper week. There is no measurement yet of whether "exit on flip" beats
riding the existing SL, and closing early would just as easily cut a position that recovers (a
name can drift against its tail without the tail itself flipping - see PERSISTENT on 2026-09-07,
down since entry with buy_tail/up still intact). Inventing a second exit rule on no evidence is
exactly what trigger.py already refuses to do for entries; the same restraint applies here. The
flip is logged as its own alert kind so it can be scored later: did positions that got a manual
override after a flip warning outperform the ones left to run their SL?

WHAT COUNTS AS A FLIP

Only open_type_dir reversing (the poll that produced the entry read "up", a later poll reads
"down", or vice versa) - not a blank reading, which just means neither open type held this poll,
and not the tail alone, since tail can go blank between reappearances without the auction
direction itself having reversed. tail is carried in the alert body as corroborating detail.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pandas as pd

from signal_engine.analysis.breakingtrade import alerts, store

STRATEGY = "BREAKINGTRADE"
_TRADES_DB = store._DB_PATH.replace("breakingtrade.db", "trades.db")

_EXPECTED_DIR = {"LONG": "up", "SHORT": "down"}
_EXPECTED_TAIL = {"LONG": "buy_tail", "SHORT": "sell_tail"}


def _open_positions(today: str) -> dict:
    """{symbol: {direction, entry, sl, tp, executed_at}} for BREAKINGTRADE symbols still open
    today - the latest SUCCESS row per symbol is LONG/SHORT (an EXIT row, if present, is always
    the true latest and marks the symbol closed)."""
    try:
        conn = sqlite3.connect(_TRADES_DB, timeout=10)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT symbol, direction, entry, sl, tp, executed_at
            FROM trades
            WHERE upper(strategy) = ? AND status = 'SUCCESS' AND date(executed_at) = ?
            ORDER BY id
            """,
            (STRATEGY, today),
        ).fetchall()
    except sqlite3.OperationalError:
        # trades.db not created yet (nothing has ever traded) - nothing open.
        return {}
    finally:
        conn.close()

    latest = {}
    for symbol, direction, entry, sl, tp, executed_at in rows:
        latest[symbol] = {
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "executed_at": executed_at,
        }
    return {sym: pos for sym, pos in latest.items() if pos["direction"] in ("LONG", "SHORT")}


def _already_warned(symbol: str, since: str) -> bool:
    """True if a structure_flip alert already went out for this symbol's current position - one
    warning per open position, not one per poll."""
    with alerts._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE kind = 'structure_flip' AND symbol = ? "
            "AND created_at >= ? LIMIT 1",
            (symbol, since),
        ).fetchone()
    return row is not None


def _alert_flip(symbol: str, pos: dict, open_type_dir: str, tail: str, captured_at: datetime) -> None:
    entry_ref = alerts.find_last_alert(symbol, "trade_signal", before=captured_at)
    was_dir = _EXPECTED_DIR[pos["direction"]]
    was_tail = _EXPECTED_TAIL[pos["direction"]]

    lines = [
        f"BT FLIP WARNING {captured_at:%H:%M} | {symbol}",
        f"Position: {pos['direction']} @ {pos['entry']:,.2f} "
        f"(entered {pos['executed_at'][11:16] if pos['executed_at'] else '?'})",
        f"Was: {was_dir}/{was_tail}  ->  Now: {open_type_dir or '-'}/{tail or '-'}",
        "SL/TP unchanged, nothing closed automatically - review manually.",
    ]
    if entry_ref:
        ref_line = f"Original signal: {entry_ref['created_at'][11:16]} ({entry_ref['scan']})"
        if entry_ref["link"]:
            ref_line += f" {entry_ref['link']}"
        lines.append(ref_line)
    message = "\n".join(lines)

    delivered, message_id = alerts.send(message, "structure_flip")
    alerts.record(
        "structure_flip",
        message,
        symbol=symbol,
        direction=pos["direction"],
        scan="FLIP",
        deliver=False,
        delivered=delivered,
        message_id=message_id,
    )


def check(snapshot: pd.DataFrame, captured_at: datetime) -> int:
    """Compare each open BREAKINGTRADE position's current-poll structure against the direction
    it was entered on. Sends (and records) one alert the first time a position flips against it;
    silent otherwise. Returns how many new flip alerts were sent this poll."""
    if snapshot is None or snapshot.empty or "symbol" not in snapshot.columns:
        return 0

    positions = _open_positions(captured_at.strftime("%Y-%m-%d"))
    if not positions:
        return 0

    sent = 0
    for symbol, pos in positions.items():
        match = snapshot.loc[snapshot["symbol"] == symbol]
        if match.empty:
            continue
        row = match.iloc[0]
        open_type_dir = row.get("open_type_dir") or None
        tail = row.get("tail") or None

        if open_type_dir not in ("up", "down"):
            continue  # no directional read this poll - nothing to compare
        if open_type_dir == _EXPECTED_DIR[pos["direction"]]:
            continue  # still agrees with the position

        if _already_warned(symbol, pos["executed_at"]):
            continue

        _alert_flip(symbol, pos, open_type_dir, tail, captured_at)
        sent += 1
    return sent
