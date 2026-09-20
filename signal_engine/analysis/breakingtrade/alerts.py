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

Setup (one time): create a bot with @BotFather, create one Telegram channel per (strategy,
phase) pair - ANALYZE and LIVE are always separate channels, never shared - add the bot to
each as an administrator, then:

  - Put ONLY the bot token in signal_engine/.env (a real secret):

        BREAKINGTRADE_BOT_TOKEN=123456:ABC...

  - Put every channel id in signal_engine/config.yaml (not a secret, and this is the ONE place
    all of this project's Telegram channel ids live - see config.yaml's `telegram:` block):

        telegram.channels: entries named "intraday-breakingtrade-analyze" / "-live" and
        "intraday-breakingtrade-watchlist-analyze" / "-live" (these are ALSO what the engine's
        own listener subscribes to - the id here and the id the engine watches must be the
        SAME channel, since this bot posts and the engine's Telethon listener reads back from
        it) - see chat_id_for() below.

        telegram.breakingtrade_btst_channels.analyze / .live: BTST has no engine counterpart
        (it is a manual daily decision, never auto-traded - see alert_btst()'s docstring), so
        it gets its own small config.yaml mapping instead of a telegram.channels entry.

Which one a message goes to is decided HERE, automatically, from OpenAlgo's live analyze/live
state (see `_current_phase()`) - never configured per call. Leave a strategy's `-live` channel
out of config.yaml (or its `breakingtrade_btst_channels.live` key unset) until that strategy is
actually promoted: alerts still record to the database, just aren't delivered (see `send()`),
exactly like an unconfigured channel always has.

Without a configured channel, alerts are still recorded, just not delivered - and the module
says so once rather than failing repeatedly.

A FUTURE STANDALONE STRATEGY (its own scheduled single-file script, not run through main.py -
e.g. strategies/examples/momentum_rank_strategy.py, which shares this same bot token for its
own channel) cannot import this module - it needs to stay a portable single file. Copy that
file's `_clean_bot_token()`/`_send_telegram()` pair as the reference pattern for the same Bot
HTTP API contract implemented here, rather than reinventing the token-cleaning edge cases from
scratch.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime

import httpx
from dotenv import dotenv_values

from signal_engine.analysis.breakingtrade import review, store

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

# Columns added after the table shipped. SQLite has no "ADD COLUMN IF NOT EXISTS", so the
# existing set is read once per connection and only the gaps are filled - same idiom as
# signal_engine/db.py's _add_missing_columns, which this was copied from.
_ADDED_COLUMNS = (
    # Telegram's own id for the message this row came from. Lets a later alert (e.g. a
    # structure-flip warning) link straight back to the exact message a symbol was first
    # called on, instead of making the reader search the channel by eye.
    ("message_id", "INTEGER"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    for name, coltype in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {coltype}")

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

# One plain-English line per scan: WHY that side, in terms a trader can check against the
# chart themselves - not the vendor's column names (TPO Pos, Open Drive, Day Type...), which
# mean nothing without having read the full guide. Written from each scan's own documented
# condition in scans.py's SCANS list - see that file for the exact structural test.
_SCAN_REASON = {
    "The Runaway": "Gapped up and opened with immediate one-way buying, still above value - momentum hasn't paused yet",
    "The Breakdown": "Gapped down and opened with immediate one-way selling, still below value - momentum hasn't paused yet",
    "Breakaway Above PDH": "Broke and held above yesterday's high, dip-buyers defended it on the way - trend day forming",
    "Breakaway Below PDL": "Broke and held below yesterday's low, rally-sellers defended it on the way - trend day forming",
    "Value Migration Up": "Trading range has shifted decisively higher - a new, higher value area forming, not just a spike",
    "Value Migration Down": "Trading range has shifted decisively lower - a new, lower value area forming, not just a spike",
    "The Gap-Up Trap": "Gapped up but got rejected, now trading back below the open - gap-up buyers are trapped and selling",
    "Gap-Down Rescue": "Gapped down but buyers stepped in and bought it back - early sellers got rescued, real demand at the lows",
    "Neutral Day Resolution Up": "A choppy, directionless day is finally breaking upward late in the session - often carries to tomorrow's open",
    "Neutral Day Resolution Down": "A choppy, directionless day is finally breaking downward late in the session - often carries to tomorrow's open",
    "Live Print in Formation Up": "Price is actively stacking new highs right now, several ticks running - a breakout forming live, not confirmed yet",
    "Live Print in Formation Down": "Price is actively stacking new lows right now, several ticks running - a breakdown forming live, not confirmed yet",
}


def scan_reason(scan_name: str) -> str:
    """The one-line plain-English reason for a scan name, or a safe fallback for an unknown
    one (a vendor scan added to scans.py without a matching entry here shouldn't crash the
    poller - just read less helpfully until _SCAN_REASON is updated)."""
    return _SCAN_REASON.get(scan_name, "setup matched (reason not documented yet)")

#: (group, phase) pairs already warned about — one warning per DESTINATION, not one for the
#: whole module. See send().
_warned_missing_config: set = set()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(store._DB_PATH, timeout=10)
    conn.executescript(_SCHEMA)
    _add_missing_columns(conn)
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
#
# Each of these is further split ANALYZE/LIVE by _current_phase() below - a strategy's
# paper-phase chatter must never sit in the same channel as its live fills, or a performance
# review can no longer tell which trades were real. See the module docstring.
#
# "intraday"/"watchlist" name config.yaml's telegram.channels entries (same physical channel
# the engine's own listener subscribes to); "btst" names config.yaml's
# telegram.breakingtrade_btst_channels mapping instead, since BTST has no engine counterpart.
_CHANNEL_GROUP_BY_KIND = {
    "btst": "btst",
    "btst_empty": "btst",
    "intraday_transition": "intraday",
    "trade_signal": "intraday",
    "structure_flip": "intraday",
    "health": "intraday",
    # WATCHLIST outcome (trigger.plan_trade_watchlist - entry at the scan-hit price, no
    # confirming-close wait) gets its OWN channel, deliberately separate from the CONFIRMED
    # "trade_signal" above - see config.yaml's intraday-breakingtrade-watchlist channel and
    # __main__.py's _emit_trade_signals() for why the two are kept apart end to end.
    "trade_signal_watchlist": "watchlist",
}
_DEFAULT_CHANNEL_GROUP = "intraday"

# config.yaml telegram.channels NAME for each (group, phase) pair - not the BTST group, which
# has no engine-subscribed channel and is looked up via settings.breakingtrade_btst_channels
# instead (see chat_id_for()).
_CHANNEL_NAME_BY_GROUP = {
    "intraday": "intraday-breakingtrade",
    "watchlist": "intraday-breakingtrade-watchlist",
}

# How long a checked OpenAlgo mode is trusted before re-checking. Long enough that a burst of
# alerts (several signals on one poll) doesn't hammer OpenAlgo's API once per message; short
# enough that flipping OpenAlgo's mode mid-day - exactly what happens when a strategy is
# promoted to live - is picked up without needing to restart the poller.
_MODE_CACHE_TTL_SECONDS = 60
_mode_cache = {"is_analyze": True, "checked_at": 0.0}

#: Prepended to every outgoing message in send() below - see notifier.py's identical
#: _PHASE_TAG for the full rationale. Kept as its own copy rather than imported: this module
#: is a peer of notifier.py, not a dependent of it, and the two-entry dict is cheaper to
#: duplicate than to couple.
_PHASE_TAG = {"live": "[LIVE] ", "analyze": "[PAPER] "}


def _current_phase() -> str:
    """"analyze" or "live", from OpenAlgo's live analyze/live state, cached briefly.

    Defaults to "analyze" - the lower-stakes destination - whenever OpenAlgo can't be reached,
    so a network hiccup routes a message to the paper channel rather than the live one. This
    only decides which Telegram channel a message is POSTED to for human review; it carries no
    trading authority of its own - whether the engine ever acts on the underlying signal is
    still decided entirely by that channel's `enabled` flag in config.yaml (see
    alert_trade_signal()'s docstring).
    """
    now = time.monotonic()
    if now - _mode_cache["checked_at"] > _MODE_CACHE_TTL_SECONDS:
        mode, is_analyze = review.trading_mode()
        _mode_cache["is_analyze"] = True if mode == "unknown" else is_analyze
        _mode_cache["checked_at"] = now
    return "analyze" if _mode_cache["is_analyze"] else "live"


def prime_mode_cache(mode: str, is_analyze: bool) -> None:
    """Seed the mode cache from a trading_mode() call the CALLER already made.

    __main__.py's _watch() fetches mode once at startup for its own log line and the
    alert_started() banner - without this, alert_started()'s first send would immediately
    trigger a SECOND, redundant OpenAlgo API call from chat_id_for()'s cold cache to answer
    the exact same question. Priming also avoids a (rare) inconsistency where the banner text
    and the channel the banner is actually delivered to could disagree, if OpenAlgo's mode
    flipped in the gap between two independent checks.
    """
    _mode_cache["is_analyze"] = True if mode == "unknown" else is_analyze
    _mode_cache["checked_at"] = time.monotonic()


def _env() -> dict:
    """The bot token only - every channel id lives in config.yaml, not here. See the module
    docstring's Setup section for why."""
    env = dict(dotenv_values(_ENV_PATH)) if os.path.exists(_ENV_PATH) else {}
    env.setdefault("BREAKINGTRADE_BOT_TOKEN", os.getenv("BREAKINGTRADE_BOT_TOKEN"))
    return env


#: SQL fragment for "this alert is at or after `since`", parsing BOTH sides.
#:
#: A raw string comparison is WRONG here and was wrong in three separate places. trades.db
#: writes timestamps with datetime.isoformat() ("2026-09-11T11:46:43.327851"); this database
#: writes them with a space ("2026-09-11 14:52:16"). At index 10 ' ' (0x20) sorts BELOW 'T'
#: (0x54), so a LATER alert always compared as SMALLER and every such filter matched nothing.
#:
#: The cost was three "have I already done this?" checks that always answered no:
#:   tp_watch._last_level_hit   - re-sent TP1 every poll (5x AXISBANK on 2026-09-11)
#:   flip_watch._already_warned - re-warned every poll (4x ADANIENSOL, 4x ADANIENT)
#:   entry_watch                - same shape
#: Use this constant rather than writing the comparison again.
SINCE_CLAUSE = "datetime(created_at) >= datetime(?)"


def chat_id_for(kind: str) -> str | None:
    """Which channel a given alert kind belongs in, for the CURRENT OpenAlgo mode.

    There is deliberately NO fallback to a generic chat id. An earlier single-channel setup sent
    these into the channel breakout.pine already uses, mixing two unrelated strategies' signals.
    Silently reverting to that on a missing key would repeat the mistake, so an unconfigured
    channel means "record it, do not deliver it" - which is also what happens here for as long
    as a strategy's `-live` channel is left out of config.yaml during its paper phase.
    """
    from signal_engine.config import settings

    group = _CHANNEL_GROUP_BY_KIND.get(kind, _DEFAULT_CHANNEL_GROUP)
    phase = _current_phase()

    if group == "btst":
        ch = settings.breakingtrade_btst_channels.get(phase)
        return str(ch.id) if ch else None

    name = f"{_CHANNEL_NAME_BY_GROUP[group]}-{phase}"
    return next((str(ch.id) for ch in settings.telegram_channels if ch.name == name), None)


def _credentials(kind: str = None) -> tuple:
    env = _env()
    token = _clean_token(env.get("BREAKINGTRADE_BOT_TOKEN"))
    chat = chat_id_for(kind)
    return token, (chat.strip() if chat else chat)


def send(text: str, kind: str = None, monospace: bool = False) -> tuple[bool, int | None]:
    """Deliver one message to the channel that `kind` belongs to.

    Returns (delivered, message_id). Never raises - a failed alert must not take the collector
    down - so both are (False, None) when unconfigured, refused, or unreachable. message_id is
    Telegram's own id for the sent message, kept so a later alert can link straight back to it
    (see telegram_link()).

    monospace=True wraps the whole message in a Markdown code block. Every column-aligned
    message here (padded with f"{x:<10}" etc.) was being sent as plain text with no parse_mode -
    Telegram renders that in a PROPORTIONAL font, so the padding spaces do nothing and every
    "aligned" table was actually ragged on the phone. Safe to always escape-free here: inside a
    Markdown code block only a literal backtick or backslash needs escaping, and none of this
    module's generated text (numbers, symbols, arrows) ever contains either.
    """
    token, chat_id = _credentials(kind)
    phase = _current_phase()
    # Same convention as notifier.py's _PHASE_TAG: a message must never depend on which
    # physical channel it landed in to say whether it describes real money - see that
    # module's docstring for the incident this defends against.
    text = f"{_PHASE_TAG.get(phase, '')}{text}"
    if not token or not chat_id:
        group = _CHANNEL_GROUP_BY_KIND.get(kind, _DEFAULT_CHANNEL_GROUP)
        # Keyed by (group, phase), not one flag for everything: a missing BTST channel used
        # to suppress the warning for a later missing WATCHLIST one, so the second gap was
        # invisible and its alerts silently went undelivered with nothing said about it.
        warn_key = (group, phase)
        if warn_key not in _warned_missing_config:
            where = (
                f"telegram.breakingtrade_btst_channels.{phase}"
                if group == "btst"
                else f"telegram.channels ({_CHANNEL_NAME_BY_GROUP[group]}-{phase})"
            )
            print(
                f"  [alerts] BREAKINGTRADE_BOT_TOKEN not set, or no {where} channel in "
                "config.yaml - alerts are being recorded but not delivered"
            )
            _warned_missing_config.add(warn_key)
        return False, None
    payload = {"chat_id": chat_id, "disable_web_page_preview": True}
    if monospace:
        payload["text"] = f"```\n{text}\n```"
        payload["parse_mode"] = "Markdown"
    else:
        payload["text"] = text
    try:
        response = httpx.post(
            _TELEGRAM_API.format(token=token),
            json=payload,
            timeout=15,
        )
        if response.status_code == 200:
            try:
                message_id = response.json().get("result", {}).get("message_id")
            except Exception:  # noqa: BLE001 - a link is a bonus, not worth failing delivery over
                message_id = None
            return True, message_id
        # Surface Telegram's own reason. Returning a bare False here once cost a diagnosis
        # session: a 404 "Not Found" means a bad token, 400 "chat not found" means the bot was
        # never added to the channel, 403 means it lacks permission to post. Very different
        # fixes, indistinguishable without the description.
        try:
            reason = response.json().get("description", response.text[:120])
        except Exception:  # noqa: BLE001
            reason = response.text[:120]
        print(f"  [alerts] Telegram refused ({response.status_code}): {reason}")
        return False, None
    except Exception as exc:  # noqa: BLE001 - alerting must never break collection
        print(f"  [alerts] delivery failed: {type(exc).__name__}: {exc}")
        return False, None


def record(
    kind: str,
    message: str,
    symbol: str = None,
    direction: str = None,
    scan: str = None,
    deliver: bool = True,
    delivered: bool = False,
    message_id: int | None = None,
    when: datetime | None = None,
) -> bool:
    """Persist an alert row, optionally delivering it.

    Delivery is separable from recording because ONE message often covers MANY symbols. The
    message is sent once; a row is written per symbol so the alert can be scored per name
    later. Recording per symbol AND delivering per symbol would send the same text once per
    row - the BTST list did exactly that and fired six identical messages.

    message_id is Telegram's id for the message this row belongs to - passed straight through
    when the caller already sent the message itself (the multi-symbol callers below), or filled
    in here when `deliver=True` does the sending.

    when: the LOGICAL time this alert is for (defaults to wall-clock now). Callers whose
    dedup check keys off a caller-supplied timestamp - alert_btst()'s captured_at, the scan
    time rather than the moment record() happens to run - must pass it here too, or the
    written created_at silently disagrees with the date the dedup query filters on. 2026-09-17:
    _btst_names_sent_today() filtered by date(captured_at), but every row was always stamped
    with real datetime.now(), so on any day those two disagreed the "one message per day" gate
    never found the earlier row and sent a duplicate every run.
    """
    if deliver:
        delivered, message_id = send(message, kind)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO alerts (created_at, kind, symbol, direction, scan, message, "
            "delivered, message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (when or datetime.now()).replace(microsecond=0).isoformat(sep=" "),
                kind,
                symbol,
                direction,
                scan,
                message,
                int(delivered),
                message_id,
            ),
        )
    return delivered


def telegram_link(kind: str, message_id: int | None) -> str | None:
    """A t.me deep link straight to one delivered message, or None when there is nothing to
    link (never delivered, or the channel isn't a supergroup/channel with a -100... id).

    Telegram's own link shape for a private supergroup/channel is
    https://t.me/c/<internal_id>/<message_id>, where <internal_id> is the chat id with its
    leading "-100" stripped. Opens directly in any Telegram client for members of the channel -
    no need for the channel to be public.
    """
    if not message_id:
        return None
    chat_id = chat_id_for(kind)
    if not chat_id:
        return None
    chat_id = chat_id.strip()
    if not chat_id.startswith("-100"):
        return None
    return f"https://t.me/c/{chat_id[4:]}/{message_id}"


def find_last_alert(symbol: str, kind: str, before: datetime = None) -> dict | None:
    """Most recent alert of `kind` for `symbol`, at or before `before` (default: now).

    Used to find the trade_signal (or watchlist) message that first called a symbol, so a later
    alert about it - a structure flip, say - can link straight back rather than making the
    reader search the channel by eye.
    """
    query = "SELECT created_at, scan, message, message_id FROM alerts WHERE symbol = ? AND kind = ?"
    params = [symbol, kind]
    if before is not None:
        query += " AND created_at <= ?"
        params.append(before.replace(microsecond=0).isoformat(sep=" "))
    query += " ORDER BY created_at DESC LIMIT 1"
    with _connect() as conn:
        row = conn.execute(query, params).fetchone()
    if not row:
        return None
    created_at, scan, message, message_id = row
    return {
        "created_at": created_at,
        "scan": scan,
        "message": message,
        "message_id": message_id,
        "link": telegram_link(kind, message_id),
    }


def alert_transitions(new_by_scan: dict, captured_at: datetime, snapshot=None) -> int:
    """Alert only names that ENTERED a scan on this poll.

    Alerting the full match list every poll would repeat the same names for hours and train the
    reader to ignore the channel. The transition is the event.

    This is a WATCHLIST notice, not a trade signal - it just says a symbol newly matched a scan
    condition. `alert_trade_signal` is the only kind of message the engine will ever act on, and
    its format ("STRATEGY LONG"/"STRATEGY SHORT" as the exact first line, parsed by parser.py)
    can't share a first-line shape with this one without becoming parseable as a real signal. So
    the two are kept visually apart instead: this one is headed "BREAKINGTRADE WATCHLIST" (never
    abbreviated - see alert_trade_signal()'s own docstring for why short forms are avoided
    throughout) and says "no action" up front, and its per-row side tag is lowercase
    ("long"/"short") rather than the upper-case LONG/SHORT that only ever appears in an actual
    entry signal. Note this is a DIFFERENT message from the "BREAKINGTRADE-WATCHLIST LONG/SHORT"
    trade signal alert_trade_signal() sends on the intraday-breakingtrade-watchlist channel -
    that one IS a real trade the engine acts on; this one never is.
    """
    if not new_by_scan:
        return 0

    prices = {}
    if snapshot is not None and "price" in getattr(snapshot, "columns", []):
        prices = dict(zip(snapshot["symbol"], snapshot["price"], strict=False))

    # One line per name: side, symbol, price, then the crisp WHY (see _SCAN_REASON) and the
    # short scan tag in parens for reference. Longer than the old bare "side symbol price tag"
    # row on purpose - a trader who can't recall what "GapDnRescue" means from the tag alone
    # has to look it up before trusting or dismissing the call, which is worse than a longer
    # line they can act on immediately.
    rows, count = [], 0
    for scan_name, symbols in sorted(new_by_scan.items()):
        side = (
            "short"
            if scan_name.rstrip().endswith(("Down", "Dn", "Trap", "Breakdown", "PDL"))
            else "long"
        )
        short = _SHORT_SCAN.get(scan_name, scan_name)
        reason = scan_reason(scan_name)
        for symbol in symbols:
            price = prices.get(symbol)
            rows.append((side, symbol, f"{price:,.1f}" if price else "-", reason, short))
            count += 1

    width = max((len(r[1]) for r in rows), default=8)
    # Header shape matches alert_btst()'s below: "LABEL timestamp | N noun | context" - same
    # three pipe-delimited sections in the same order in both channels, so a trader scanning
    # either one always finds "how many" in the same place before reading further.
    lines = [
        f"BREAKINGTRADE WATCHLIST {captured_at:%H:%M} | {count} new | "
        "no action, not a trade signal"
    ]
    lines += [
        f"{side:<5} {sym:<{width}} {px:>9} - {reason} ({tag})"
        for side, sym, px, reason, tag in rows
    ]
    message = "\n".join(lines)

    # one message covering every new name
    delivered, message_id = send(message, "intraday_transition", monospace=True)
    for scan_name, symbols in new_by_scan.items():
        for symbol in symbols:
            record(
                "intraday_transition",
                message,
                symbol=symbol,
                scan=scan_name,
                deliver=False,
                delivered=delivered,
                message_id=message_id,
            )
    return count


def _btst_names_sent_today(day: str) -> set | None:
    """The symbol set of the last BTST watchlist sent today, or None if none was."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT symbol FROM alerts WHERE kind = 'btst' AND date(created_at) = ? "
            "AND created_at = (SELECT MAX(created_at) FROM alerts WHERE kind = 'btst' "
            "AND date(created_at) = ?)",
            (day, day),
        ).fetchall()
    return {r[0] for r in rows} if rows else None


def alert_btst(watchlist, captured_at: datetime) -> int:
    """The closing-hour carry list, with its execution deadline stated in the message.

    ONE MESSAGE PER DAY unless the selection actually changes. The closing scan runs at 14:50
    and again at 15:10, and each run used to send its own copy - so the channel showed the
    same list twice, with most names repeated. The paper book now keeps only the FIRST
    recommendation of the day (one position per stock, enforced by paper.py's unique index),
    so the second copy is noise. A later run whose SELECTION genuinely differs IS worth
    seeing, and is marked REVISED so it is not mistaken for the original call.
    """
    if watchlist is None or watchlist.empty:
        record("btst_empty", f"BTST {captured_at:%d-%b} | no candidates today", when=captured_at)
        return 0

    names = {str(r.symbol) for r in watchlist.itertuples()}
    already = _btst_names_sent_today(f"{captured_at:%Y-%m-%d}")
    if already is not None and already == names:
        return 0
    revised = " (REVISED)" if already is not None else ""

    width = max(len(str(r.symbol)) for r in watchlist.itertuples())
    # Header shape matches alert_transitions() above: "LABEL timestamp | N noun | context".
    lines = [
        f"BTST {captured_at:%d-%b}{revised} | {len(watchlist)} names | BUY CNC before 15:15"
    ]
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

    # one message listing the whole watchlist
    delivered, message_id = send(message, "btst", monospace=True)
    for row in watchlist.itertuples():
        record(
            "btst",
            message,
            symbol=row.symbol,
            direction="up",
            scan="BTST",
            deliver=False,
            delivered=delivered,
            message_id=message_id,
            when=captured_at,
        )
    return len(watchlist)


def alert_trade_signal(
    plan, strategy: str = "BREAKINGTRADE", scan_name: str = None, kind: str = "trade_signal"
) -> bool:
    """Emit ONE trade in the exact shape signal_engine's parser accepts, so the engine can take
    it end to end - sizing, entry, stop placement, staged exits and the time exit.

    The format is deliberately identical to a PineScript alert:

        BREAKINGTRADE LONG
        Symbol: VOLTAS
        Entry: 1186.5
        SL: 1178.2
        TP: 1203.0
        Reason: Gapped down but buyers stepped in and bought it back...

    `strategy` is written verbatim as the first token of the first line - parser.py splits on
    whitespace, so it must never contain a space, and it must never be shortened. Two full-name
    values are used across this codebase: "BREAKINGTRADE" for the CONFIRMED outcome (waits for
    entry_trigger's closing confirmation) and "BREAKINGTRADE-WATCHLIST" for the WATCHLIST
    outcome (entered at the scan-hit price - see trigger.plan_trade_watchlist()). Never "BT" or
    any other abbreviation - a trader scanning two channels side by side needs the full name to
    tell them apart at a glance, and a shortened tag would also silently diverge from the
    strategy_profiles/blacklist config keys in config.yaml, which are keyed on the full name.

    `Reason:` is not one of parser.py's mandatory fields - it is kept in Signal.context rather
    than dropped, so it survives into the trade log without the engine needing to know what it
    means.

    `kind` selects which Telegram channel this goes to (see _CHANNEL_GROUP_BY_KIND) - "trade_signal"
    for the intraday-breakingtrade channel (CONFIRMED), "trade_signal_watchlist" for
    intraday-breakingtrade-watchlist (WATCHLIST). Whether either actually TRADES is decided by
    that channel's `enabled` flag in config.yaml, not by anything here - which is what lets the
    same message stream be recorded, read and scored long before it is allowed to touch money.
    """
    message = build_trade_signal_message(plan, strategy, scan_name)
    return record(kind, message, symbol=plan.symbol, direction=plan.direction, scan=strategy)


def build_trade_signal_message(plan, strategy: str = "BREAKINGTRADE", scan_name: str = None) -> str:
    """The alert text for one trade, as a pure function of the plan.

    Split out of alert_trade_signal() so the message can be asserted on directly - the
    2026-09-11 R:R bug (every alert overstating by 2.00x) was invisible to tests because the
    only way to reach this text was through a function that also writes to the database and
    talks to Telegram.
    """
    side = "LONG" if plan.direction == "up" else "SHORT"
    lines = [
        f"{strategy} {side}",
        f"Symbol: {plan.symbol}",
        f"Entry: {plan.entry}",
        f"SL: {plan.stop}",
        f"TP: {plan.targets[0]}",
        # Same "R:R 1:N" quick-glance field ORB/BREAKOUT's PineScript alerts already carry -
        # not one of parser.py's consumed fields, purely for a trader scanning the channel.
        # getattr, not plan.reward_risk: some callers pass a lightweight stand-in without the
        # full TradePlan property set (e.g. this module's own tests), and a missing R:R is a
        # cosmetic omission, not a reason to fail the whole signal.
        # R:R measures the TP on the line above - the one the engine actually trades. It
        # used to measure to the ladder's FINAL target while TP carried the first, which
        # overstated every alert by exactly 2.00x (see TradePlan.reward_risk).
        f"R:R: 1:{getattr(plan, 'reward_risk', None)}" if getattr(plan, "reward_risk", None) else "",
        # The ladder, named rather than folded into R:R. It is what the plan WOULD be worth
        # if the staged exits were wired through; today they are not, so it is context for a
        # reader, never a number to size or judge the trade on.
        (
            f"Runner target (not traded yet): {plan.targets[-1]} = 1:"
            f"{getattr(plan, 'reward_risk_runner', None)}"
        ) if getattr(plan, "reward_risk_runner", None) else "",
        f"Time: {plan.triggered_at:%H:%M}" if plan.triggered_at else "",
    ]
    if scan_name:
        lines.append(f"Reason: {scan_reason(scan_name)}")
    return "\n".join(line for line in lines if line).strip()


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
