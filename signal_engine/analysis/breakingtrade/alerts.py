"""Telegram alerts for the scanner, persisted so they can be scored later.

TWO DESIGN DECISIONS WORTH KNOWING

**Every alert is written to the database whether or not sending succeeds.** The alert log is not
a delivery receipt, it is the record of what the strategy claimed and when - the only way to
answer "was the alert right?" afterwards. If Telegram is down, or was never configured, the
analysis must still be possible. A missed send is an inconvenience; a missing record is a lost
experiment.

**Delivery uses the Telegram Bot HTTP API, not signal_engine's Telethon client.** The notifier in
signal_engine/notifier.py needs a live TelegramClient owned by the trading engine's event loop,
and this poller is a separate long-running process. Two processes sharing one Telethon session
file is a good way to corrupt it. A bot token is independent, stateless, and safe to call from
anywhere.

Setup (one time): create a bot with @BotFather, then add to signal_engine/.env

    BREAKINGTRADE_BOT_TOKEN=123456:ABC...
    BREAKINGTRADE_CHAT_ID=-1001234567890

Without those, alerts are still recorded, just not delivered - and the module says so once
rather than failing repeatedly.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import httpx
from dotenv import dotenv_values

from signal_engine.analysis.breakingtrade import store

_SIGNAL_ENGINE_DIR = os.path.dirname(os.path.dirname(store._DB_PATH))
_ENV_PATH = os.path.join(_SIGNAL_ENGINE_DIR, ".env")
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    symbol      TEXT,
    direction   TEXT,
    scan        TEXT,
    message     TEXT NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts (symbol, created_at);
"""

_warned_missing_config = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(store._DB_PATH, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def _credentials() -> tuple:
    env = dotenv_values(_ENV_PATH) if os.path.exists(_ENV_PATH) else {}
    return (
        env.get("BREAKINGTRADE_BOT_TOKEN") or os.getenv("BREAKINGTRADE_BOT_TOKEN"),
        env.get("BREAKINGTRADE_CHAT_ID") or os.getenv("BREAKINGTRADE_CHAT_ID"),
    )


def send(text: str) -> bool:
    """Deliver one message. Returns False (never raises) when unconfigured or unreachable -
    a failed alert must not take the collector down with it."""
    global _warned_missing_config
    token, chat_id = _credentials()
    if not token or not chat_id:
        if not _warned_missing_config:
            print(
                "  [alerts] BREAKINGTRADE_BOT_TOKEN / BREAKINGTRADE_CHAT_ID not set - "
                "alerts are being recorded but not delivered"
            )
            _warned_missing_config = True
        return False
    try:
        response = httpx.post(
            _TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        return response.status_code == 200
    except Exception as exc:  # noqa: BLE001 - alerting must never break collection
        print(f"  [alerts] delivery failed: {type(exc).__name__}: {exc}")
        return False


def record(
    kind: str, message: str, symbol: str = None, direction: str = None, scan: str = None
) -> bool:
    """Persist an alert, then attempt delivery. Returns whether delivery succeeded."""
    delivered = send(message)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO alerts (created_at, kind, symbol, direction, scan, message, delivered) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().replace(microsecond=0).isoformat(sep=" "),
                kind,
                symbol,
                direction,
                scan,
                message,
                int(delivered),
            ),
        )
    return delivered


def alert_transitions(new_by_scan: dict, captured_at: datetime, snapshot=None) -> int:
    """Alert only names that ENTERED a scan on this poll.

    Alerting the full match list every poll would repeat the same names for hours and train the
    reader to ignore the channel. The transition is the event.
    """
    if not new_by_scan:
        return 0

    prices = {}
    if snapshot is not None and "price" in getattr(snapshot, "columns", []):
        prices = dict(zip(snapshot["symbol"], snapshot["price"], strict=False))

    lines = [f"BreakingTrade intraday  {captured_at:%H:%M}"]
    count = 0
    for scan_name, symbols in sorted(new_by_scan.items()):
        for symbol in symbols:
            price = prices.get(symbol)
            suffix = f" @ {price:g}" if price else ""
            lines.append(f"  NEW  {symbol}{suffix}  [{scan_name}]")
            count += 1
    lines += ["", "Selection only - not a trade instruction. No edge established yet."]
    message = "\n".join(lines)

    for scan_name, symbols in new_by_scan.items():
        for symbol in symbols:
            record("intraday_transition", message, symbol=symbol, scan=scan_name)
    return count


def alert_btst(watchlist, captured_at: datetime, deadline: str = "15:15") -> int:
    """The closing-hour carry list, with its execution deadline stated in the message."""
    if watchlist is None or watchlist.empty:
        record("btst_empty", f"BreakingTrade BTST {captured_at:%d-%b %H:%M}: no candidates today.")
        return 0

    lines = [
        f"BreakingTrade BTST  {captured_at:%d-%b %H:%M}",
        f"Place CNC orders before {deadline} - continuous trading in F&O stocks ends then.",
        "",
    ]
    for row in watchlist.itertuples():
        delivery = f"{row.delivery_pct * 100:.0f}%" if row.delivery_pct == row.delivery_pct else "-"
        lines.append(
            f"  {row.symbol:<12} {row.price:>9,.2f}  {row.change_pct:+.2f}%  "
            f"del {delivery}  {row.day_type}"
        )
    lines += ["", "Watchlist, not a signal. Long only. Size for an overnight gap, not a stop."]
    message = "\n".join(lines)

    for row in watchlist.itertuples():
        record("btst", message, symbol=row.symbol, direction="up", scan="BTST")
    return len(watchlist)


def alert_health(text: str) -> None:
    record("health", f"BreakingTrade poller: {text}")


def history(since: datetime = None):
    """Stored alerts, for scoring what was claimed against what happened."""
    import pandas as pd

    query = "SELECT created_at, kind, symbol, direction, scan, delivered FROM alerts"
    params = []
    if since:
        query += " WHERE created_at >= ?"
        params.append(since.isoformat(sep=" "))
    query += " ORDER BY created_at"
    with _connect() as conn:
        return pd.read_sql_query(query, conn, params=params)
