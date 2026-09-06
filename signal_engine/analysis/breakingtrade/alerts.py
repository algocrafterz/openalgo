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

Setup (one time): create a bot with @BotFather, create TWO channels, add the bot to each as an
administrator, then put in signal_engine/.env

    BREAKINGTRADE_BOT_TOKEN=123456:ABC...
    BREAKINGTRADE_CHAT_ID_INTRADAY=-1001234567890   # intraday-breakingtrade
    BREAKINGTRADE_CHAT_ID_BTST=-1009876543210       # btst-breakingtrade

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

# Long vendor scan names are unreadable in a phone notification. A trader needs to know which
# setup fired, not its full title.
_SHORT_SCAN = {
    "The Runaway": "Runaway",
    "The Breakdown": "Breakdown",
    "Breakaway Above PDH": "BreakPDH",
    "Breakaway Below PDL": "BreakPDL",
    "Value Migration Up": "ValueMigUp",
    "Value Migration Down": "ValueMigDn",
    "The Gap-Up Trap": "GapUpTrap",
    "Gap-Down Rescue": "GapDnRescue",
    "Neutral Day Resolution Up": "NeutResUp",
    "Neutral Day Resolution Down": "NeutResDn",
    "Live Print in Formation Up": "LivePrintUp",
    "Live Print in Formation Down": "LivePrintDn",
}

_warned_missing_config = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(store._DB_PATH, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def _clean_token(token: str) -> str:
    """Normalize a pasted bot token.

    The API path is /bot<TOKEN>/method, so BotFather's token is often copied WITH the "bot"
    prefix already attached - which produces /botbot123.../ and a bare 404 that looks exactly
    like an invalid token. Also strips quotes and any stray CR from a CRLF .env file.
    """
    if not token:
        return token
    token = token.strip().strip("\r").strip("\"'")
    if token.lower().startswith("bot") and ":" in token[3:]:
        token = token[3:]
    return token


# Each strategy gets its OWN channel. Not for tidiness - for attention. The BTST message is a
# single actionable alert per day with a hard 15:15 deadline; the intraday stream is exploratory
# research with no established edge. Mixed together, the one message that needs acting on within
# 25 minutes competes with a scroll of "here is something interesting", which is precisely how a
# deadline gets missed. Analysis separation is already handled by the `kind` column, so channels
# exist purely to keep the urgent thing visible.
_CHANNEL_BY_KIND = {
    "btst": "BREAKINGTRADE_CHAT_ID_BTST",
    "btst_empty": "BREAKINGTRADE_CHAT_ID_BTST",
    "intraday_transition": "BREAKINGTRADE_CHAT_ID_INTRADAY",
    "trade_signal": "BREAKINGTRADE_CHAT_ID_INTRADAY",
    "health": "BREAKINGTRADE_CHAT_ID_INTRADAY",
}
_DEFAULT_CHANNEL_KEY = "BREAKINGTRADE_CHAT_ID_INTRADAY"


def _env() -> dict:
    env = dict(dotenv_values(_ENV_PATH)) if os.path.exists(_ENV_PATH) else {}
    for key in (
        "BREAKINGTRADE_BOT_TOKEN",
        "BREAKINGTRADE_CHAT_ID_BTST",
        "BREAKINGTRADE_CHAT_ID_INTRADAY",
    ):
        env.setdefault(key, os.getenv(key))
    return env


def chat_id_for(kind: str) -> str | None:
    """Which channel a given alert kind belongs in.

    There is deliberately NO fallback to a generic chat id. An earlier single-channel setup sent
    these into the channel breakout.pine already uses, mixing two unrelated strategies' signals.
    Silently reverting to that on a missing key would repeat the mistake, so an unconfigured
    channel means "record it, do not deliver it".
    """
    return _env().get(_CHANNEL_BY_KIND.get(kind, _DEFAULT_CHANNEL_KEY))


def _credentials(kind: str = None) -> tuple:
    env = _env()
    token = _clean_token(env.get("BREAKINGTRADE_BOT_TOKEN"))
    chat = chat_id_for(kind) if kind else env.get(_DEFAULT_CHANNEL_KEY)
    return token, (chat.strip() if chat else chat)


def send(text: str, kind: str = None) -> bool:
    """Deliver one message to the channel that `kind` belongs to. Returns False (never raises)
    when unconfigured or unreachable - a failed alert must not take the collector down."""
    global _warned_missing_config
    token, chat_id = _credentials(kind)
    if not token or not chat_id:
        if not _warned_missing_config:
            key = _CHANNEL_BY_KIND.get(kind, _DEFAULT_CHANNEL_KEY)
            print(
                f"  [alerts] BREAKINGTRADE_BOT_TOKEN / {key} not set - "
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
        if response.status_code == 200:
            return True
        # Surface Telegram's own reason. Returning a bare False here once cost a diagnosis
        # session: a 404 "Not Found" means a bad token, 400 "chat not found" means the bot was
        # never added to the channel, 403 means it lacks permission to post. Very different
        # fixes, indistinguishable without the description.
        try:
            reason = response.json().get("description", response.text[:120])
        except Exception:  # noqa: BLE001
            reason = response.text[:120]
        print(f"  [alerts] Telegram refused ({response.status_code}): {reason}")
        return False
    except Exception as exc:  # noqa: BLE001 - alerting must never break collection
        print(f"  [alerts] delivery failed: {type(exc).__name__}: {exc}")
        return False


def record(
    kind: str,
    message: str,
    symbol: str = None,
    direction: str = None,
    scan: str = None,
    deliver: bool = True,
    delivered: bool = False,
) -> bool:
    """Persist an alert row, optionally delivering it.

    Delivery is separable from recording because ONE message often covers MANY symbols. The
    message is sent once; a row is written per symbol so the alert can be scored per name
    later. Recording per symbol AND delivering per symbol would send the same text once per
    row - the BTST list did exactly that and fired six identical messages.
    """
    if deliver:
        delivered = send(message, kind)
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

    # Format is deliberately terse: side, symbol, price, setup. One line per name, aligned so
    # the eye can scan a column. A phone notification that needs reading twice gets ignored.
    rows, count = [], 0
    for scan_name, symbols in sorted(new_by_scan.items()):
        side = (
            "SHORT"
            if scan_name.rstrip().endswith(("Down", "Dn", "Trap", "Breakdown", "PDL"))
            else "LONG"
        )
        short = _SHORT_SCAN.get(scan_name, scan_name)
        for symbol in symbols:
            price = prices.get(symbol)
            rows.append((side, symbol, f"{price:,.1f}" if price else "-", short))
            count += 1

    width = max((len(r[1]) for r in rows), default=8)
    lines = [f"BT {captured_at:%H:%M} | {count} new"]
    lines += [f"{side:<5} {sym:<{width}} {px:>9} {tag}" for side, sym, px, tag in rows]
    message = "\n".join(lines)

    delivered = send(message, "intraday_transition")  # one message covering every new name
    for scan_name, symbols in new_by_scan.items():
        for symbol in symbols:
            record(
                "intraday_transition",
                message,
                symbol=symbol,
                scan=scan_name,
                deliver=False,
                delivered=delivered,
            )
    return count


def alert_btst(watchlist, captured_at: datetime) -> int:
    """The closing-hour carry list, with its execution deadline stated in the message."""
    if watchlist is None or watchlist.empty:
        record("btst_empty", f"BTST {captured_at:%d-%b} | no candidates today")
        return 0

    width = max(len(str(r.symbol)) for r in watchlist.itertuples())
    lines = [f"BTST {captured_at:%d-%b} | BUY CNC before 15:15 | {len(watchlist)} names"]
    for row in watchlist.itertuples():
        delivery = (
            f"del{row.delivery_pct * 100:.0f}" if row.delivery_pct == row.delivery_pct else "del-"
        )
        trend = "  TREND" if row.day_type == "Trend" else ""
        lines.append(
            f"{row.symbol:<{width}} {row.price:>9,.1f} {row.change_pct:>+6.1f}% {delivery}{trend}"
        )
    # State the EXIT RULE rather than an SL/TP. The measured result was a close-to-close hold
    # with no stop; printing a stop and target here would imply a precision the test never had,
    # and would quietly change the distribution the numbers came from. The disaster stop is
    # risk control against an overnight gap, not a strategy parameter - deliberately wide
    # enough that it should almost never trigger.
    lines.append("Exit: sell next session (~15:10). Disaster GTT stop -4%. Long only.")
    lines.append("Size for a gap, not a stop. No edge established - paper first.")
    message = "\n".join(lines)

    delivered = send(message, "btst")  # one message listing the whole watchlist
    for row in watchlist.itertuples():
        record(
            "btst",
            message,
            symbol=row.symbol,
            direction="up",
            scan="BTST",
            deliver=False,
            delivered=delivered,
        )
    return len(watchlist)


def alert_trade_signal(plan, strategy: str = "BREAKINGTRADE") -> bool:
    """Emit ONE trade in the exact shape signal_engine's parser accepts, so the engine can take
    it end to end - sizing, entry, stop placement, staged exits and the time exit.

    The format is deliberately identical to a PineScript alert:

        BREAKINGTRADE LONG
        Symbol: VOLTAS
        Entry: 1186.5
        SL: 1178.2
        TP: 1203.0

    Sent to the intraday channel. Whether it actually TRADES is decided by that channel's
    `enabled` flag in config.yaml, not by anything here - which is what lets the same message
    stream be recorded, read and scored long before it is allowed to touch money.
    """
    side = "LONG" if plan.direction == "up" else "SHORT"
    message = "\n".join(
        [
            f"{strategy} {side}",
            f"Symbol: {plan.symbol}",
            f"Entry: {plan.entry}",
            f"SL: {plan.stop}",
            f"TP: {plan.targets[0]}",
            f"Time: {plan.triggered_at:%H:%M}" if plan.triggered_at else "",
        ]
    ).strip()
    return record(
        "trade_signal", message, symbol=plan.symbol, direction=plan.direction, scan=strategy
    )


def alert_health(text: str) -> None:
    record("health", f"BT poller: {text}")


def alert_started(mode: str, is_analyze: bool, windows: str, already: int) -> None:
    """One message on start, led by the trading mode.

    Silence is ambiguous - a poller that never speaks looks identical to one that died, which
    is exactly what happened on 2026-09-04. So it says hello, and the FIRST thing it says is
    whether orders are paper or real, because that is the fact that decides whether an enabled
    channel is safe.
    """
    # THREE states, not two. "unknown" means OpenAlgo was unreachable - which is normal on a
    # weekend start - and reporting that as LIVE fires the alarm on every ordinary restart. An
    # alarm that cries wolf is one that gets ignored, so unknown says exactly that.
    if is_analyze:
        banner = "PAPER (analyze mode)"
    elif mode in ("unknown", None, ""):
        banner = "mode UNKNOWN - OpenAlgo unreachable, verify before the open"
    else:
        banner = f"*** LIVE ({mode}) - ORDERS WOULD BE REAL ***"
    lines = [f"BT poller STARTED | {banner}", windows]
    if already:
        lines.append(f"resuming - {already} polls already stored today")
    record("health", "\n".join(lines))


def alert_stopped(polls: int, signals: int, reason: str = "stopped") -> None:
    """One message on exit, with the day's tally so an empty day is distinguishable from a
    dead poller."""
    record("health", f"BT poller {reason} | {polls} polls stored today, {signals} signals sent")


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
