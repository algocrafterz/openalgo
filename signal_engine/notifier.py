"""Telegram notification sender for order state and session events.

Sends messages to notify_channel (signal-engine) so you can monitor
order placement, position lifecycle, risk events, and daily summary.

Uses the same TelegramClient as the listener (set via set_client once connected).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from signal_engine import api_client
from signal_engine.config import settings
from signal_engine.timeutils import IST

if TYPE_CHECKING:
    from telethon import TelegramClient

_client: TelegramClient | None = None


def set_client(client: TelegramClient) -> None:
    """Called by listener once the Telegram client is connected."""
    global _client
    _client = client


# How long a checked OpenAlgo mode is trusted before re-checking - same reasoning and same TTL
# as signal_engine/analysis/breakingtrade/alerts.py's identical mechanism: long enough that a
# burst of notifications doesn't hammer OpenAlgo's API, short enough that a mid-day mode flip
# reaches the right channel without an engine restart.
_MODE_CACHE_TTL_SECONDS = 60
_mode_cache = {"is_analyze": True, "checked_at": 0.0}


async def _current_phase() -> str:
    """"analyze" or "live", from OpenAlgo's live analyze/live state, cached briefly. Defaults
    to "analyze" - the lower-stakes destination - when OpenAlgo can't be reached."""
    now = time.monotonic()
    if now - _mode_cache["checked_at"] > _MODE_CACHE_TTL_SECONDS:
        mode, is_analyze = await api_client.fetch_trading_mode()
        _mode_cache["is_analyze"] = True if mode == "unknown" else is_analyze
        _mode_cache["checked_at"] = now
    return "analyze" if _mode_cache["is_analyze"] else "live"


def _channel_for_phase(phase: str):
    """settings.notify_channel[phase], falling back to whichever phase IS configured.

    Unlike a per-strategy channel, an admin alert can be safety-critical (SL failed, risk
    halted, startup failure) - silently dropping it because only one phase's channel has been
    set up would be worse than delivering it to the "wrong" (but real) channel."""
    if not settings.notify_channel:
        return None
    return settings.notify_channel.get(phase) or next(iter(settings.notify_channel.values()), None)


#: Strategy tag (as it appears on the Telegram alert / TradeRecord.strategy) -> config.yaml
#: telegram.channels base name, for the per-strategy EOD summary (see
#: _send_per_strategy_day_summaries()). Deliberately explicit rather than derived from the
#: strategy string, since the two live in different namespaces (a free-text alert header vs a
#: channel name) and a guessed transform would silently misroute the day a new strategy is
#: added. A strategy tag with no entry here (or none of this session's four are trading yet)
#: simply gets no per-strategy send - it still appears in the consolidated comparison.
_STRATEGY_CHANNEL_BASE = {
    "ORB": "intraday-orb",
    "BREAKOUT": "intraday-breakout",
    "BREAKINGTRADE": "intraday-breakingtrade",
    "BREAKINGTRADE-WATCHLIST": "intraday-breakingtrade-watchlist",
}


def _channel_for_strategy(strategy: str, phase: str):
    """The strategy's own -analyze/-live channel from settings.telegram_channels, by name - or
    None if the strategy isn't in _STRATEGY_CHANNEL_BASE or that phase's channel doesn't exist
    yet. Mirrors alerts.py's identical per-strategy lookup for the BreakingTrade family."""
    base = _STRATEGY_CHANNEL_BASE.get((strategy or "").upper())
    if base is None:
        return None
    name = f"{base}-{phase}"
    return next((ch for ch in settings.telegram_channels if ch.name == name), None)


#: Lowest notify_level at which each event is delivered. Events absent from this map are
#: ALWAYS delivered: a notify_* added later must show up until someone deliberately
#: classifies it, rather than disappearing because nobody remembered this table.
#:
#: Nothing that reports a FAILURE appears here at all. Trimming routine traffic is only
#: worth doing if it makes the exceptional traffic easier to see, and a channel that can
#: also hide a failed SL would be worse than a noisy one.
EVENT_LEVELS = {
    # Acknowledgements: report that something was received, not what happened.
    "exit_signal_received": "verbose",
    # Intermediate steps of an entry that ends in its own message anyway.
    "order_placed": "normal",
    "sl_placed": "normal",
    "be_stop_applied": "normal",
    "exit_no_position": "normal",
    # Outcomes — kept even in quiet.
    "entry_filled": "quiet",
    # A partial exit BOOKS money — it is an outcome, not a step. With extended runner tiers
    # (30/35/35) most trades end as a sequence of these and never send position_closed at all,
    # so suppressing them would make a quiet channel silent about the actual results.
    "partial_exit": "quiet",
    "position_closed": "quiet",
    "time_exit": "quiet",
    "no_progress_exit": "quiet",
    "day_summary": "quiet",
}

_LEVEL_RANK = {"quiet": 0, "normal": 1, "verbose": 2}


def should_notify(event: str, level: str) -> bool:
    """Whether `event` is delivered at the configured `level`.

    Unknown event or unknown level both resolve to "deliver". Silence should always be a
    decision someone made, never a gap in a lookup table.
    """
    required = EVENT_LEVELS.get(event)
    if required is None:
        return True
    configured = _LEVEL_RANK.get((level or "").lower())
    if configured is None:
        return True
    return configured >= _LEVEL_RANK[required]


async def notify_event(event: str, text: str) -> bool:
    """notify() with the event name first, so call sites read as `notify_event(\"x\", msg)`.

    Returns what notify() returns - see its docstring for why callers that mark something as
    "done" (e.g. tracker.py's day-summary marker) need to check this rather than assume a call
    that didn't raise means a message actually went out.
    """
    return await notify(text, event=event)


async def notify(text: str, event: str = "") -> bool:
    """Send a message to the notify_channel. No-op if not configured or client not ready.

    Returns True only when a send was actually attempted against Telegram - False for every
    silent no-op (no channel configured, notify_level filtered it, client not ready yet).

    2026-09-09: tracker.py's send_day_summary() used to mark itself "sent" the moment this
    coroutine returned, regardless of which branch it took. On 2026-09-09 the Telegram client
    was not ready when the 14:45 time-exit fired send_day_summary() - this returned via the
    `_client is None` branch, logged only at DEBUG (invisible at the file's INFO level), and the
    day summary was silently never delivered - but the "already sent today" marker was written
    anyway, permanently blocking any retry for the rest of the day. `_client is None` is now
    logged at WARNING (this is not a routine, expected state the way "no channel configured" or
    "filtered by notify_level" are) and the caller can check this return value to skip marking
    itself done.
    """
    if not settings.notify_channel:
        return False
    if event and not should_notify(event, getattr(settings, "notify_level", "normal")):
        return False
    if _client is None:
        logger.warning("Notifier: client not ready, skipping (message dropped, not queued)")
        return False
    channel = _channel_for_phase(await _current_phase())
    if channel is None:
        return False
    try:
        await _client.send_message(channel.id, text)
        return True
    except asyncio.CancelledError:
        logger.debug("Notifier: send cancelled (event loop shutting down)")
        return False
    except Exception as e:
        logger.warning(f"Notifier: failed to send message: {e}")
        return False


# ── Format helpers ─────────────────────────────────────────────────────────────

def _dir(direction: str) -> str:
    return "LONG ▲" if direction.upper() == "LONG" else "SHORT ▼"


def _pnl(amount: float) -> str:
    return f"+₹{amount:,.0f}" if amount >= 0 else f"-₹{abs(amount):,.0f}"


def _r(r: float | None) -> str:
    return f" ({r:+.1f}R)" if r is not None else ""


def _dur(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _now_ist() -> str:
    return datetime.now(IST).strftime("%H:%M IST")


def _tag(strategy: str) -> str:
    return f" | {strategy}" if strategy else ""


def format_day_context(
    day_trades: int,
    day_wins: int,
    day_losses: int,
    day_pnl: float,
    max_trades: int | None = None,
) -> str:
    """Compact per-day running context line shared across entry/exit messages."""
    trades_str = f"{day_trades}/{max_trades}" if max_trades else str(day_trades)
    return f"Day: {trades_str} trades (W:{day_wins} L:{day_losses}) | P&L: {_pnl(day_pnl)}"


def format_slot_context(open_positions: int, max_positions: int) -> str:
    """Compact slot usage line: 'Slot 2/3 used'. 0 = unlimited (no cap configured)."""
    cap = max_positions if max_positions > 0 else "unlimited"
    return f"Slot {open_positions}/{cap} used"


# ── Order placement ────────────────────────────────────────────────────────────

async def notify_order_placed(
    symbol: str,
    direction: str,
    strategy: str = "",
    signal_price: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    rr: float | None = None,
    slot_context: str = "",
) -> None:
    """Brief confirmation that the entry order reached the broker."""
    rr_str = f" | R:R 1:{rr:.1f}" if rr is not None else ""
    sl_str = f" | SL: {sl:.2f}" if sl is not None else ""
    tp_str = f" | TP: {tp:.2f}" if tp is not None else ""
    price_str = f"Signal: {signal_price:.2f}" if signal_price is not None else "Signal: —"
    slot_line = f"\n{slot_context}" if slot_context else ""
    await notify_event("order_placed",
        f"ENTRY SENT | {symbol} {_dir(direction)}{_tag(strategy)} | {_now_ist()}\n"
        f"{price_str}{sl_str}{tp_str}{rr_str}{slot_line}"
    )


async def notify_entry_filled(
    symbol: str,
    direction: str,
    fill_price: float,
    qty: int,
    signal_price: float,
    strategy: str = "",
    sl: float | None = None,
    tp: float | None = None,
) -> None:
    slip = fill_price - signal_price if fill_price > 0 else 0.0
    logger.info(f"LIVE | {symbol} [{strategy}] fill={fill_price:.2f} slip={slip:+.2f} qty={qty} sl={sl}")
    sl_str = f" | SL: {sl:.2f}" if sl is not None else ""
    tp_str = f" | TP: {tp:.2f}" if tp is not None else ""
    await notify_event("entry_filled",
        # "FILLED", not "LIVE" - since the ANALYZE/LIVE channel split, "LIVE" reads as a phase
        # claim (real money) rather than a fill-status one, and this message posts unchanged
        # in the analyze channel too.
        f"FILLED | {symbol} {_dir(direction)}{_tag(strategy)} | {_now_ist()}\n"
        f"Fill: {fill_price:.2f} (slip {slip:+.2f}) | Qty: {qty}{sl_str}{tp_str}"
    )


async def notify_order_rejected(symbol: str, reason: str, strategy: str = "") -> None:
    await notify_event("order_rejected",
        f"ENTRY REJECTED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
        f"No trade taken. Reason: {reason}"
    )


async def notify_sl_placed(
    symbol: str, order_id: str,
    strategy: str = "", sl_price: float | None = None,
) -> None:
    price_str = f" sl={sl_price:.2f}" if sl_price is not None else ""
    logger.info(f"SL confirmed | {symbol} [{strategy}]{price_str} id={order_id}")
    sl_line = f"SL: {sl_price:.2f}" if sl_price is not None else "SL: —"
    await notify_event("sl_placed",
        f"SL PLACED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
        f"{sl_line} | Order: {order_id}"
    )


async def notify_sl_failed(symbol: str, reason: str, strategy: str = "") -> None:
    await notify_event("sl_failed",
        f"SL NOT PLACED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
        f"Position UNPROTECTED. Reason: {reason}\n"
        f"Place SL manually or close position."
    )


# ── EXIT signal handling ───────────────────────────────────────────────────────

async def notify_exit_signal_received(symbol: str, strategy: str) -> None:
    logger.info(f"EXIT signal | {symbol} [{strategy}]")


async def notify_partial_exit(
    symbol: str,
    exit_qty: int,
    remaining_qty: int,
    tp_level: str,
    pnl: float,
    strategy: str = "",
    entry_price: float | None = None,
    new_sl: float | None = None,
    next_tp_label: str | None = None,
    next_tp_price: float | None = None,
    direction: str | None = None,
    r_multiple: float | None = None,
    exit_price: float | None = None,
    hold_minutes: int = 0,
) -> None:
    logger.info(
        f"{tp_level} HIT | {symbol} [{strategy}] exit={exit_qty} remaining={remaining_qty} "
        f"pnl={_pnl(pnl)}{_r(r_multiple)} new_sl={new_sl}"
    )
    dir_str = f" {_dir(direction)}" if direction else ""
    dur_str = f" | held {_dur(hold_minutes)}" if hold_minutes > 0 else ""
    sl_str = f" | New SL: {new_sl:.2f}" if new_sl is not None else ""
    next_str = (
        f"\nNext: {next_tp_label} @ {next_tp_price:.2f}"
        if next_tp_label and next_tp_price is not None
        else ""
    )
    await notify_event("partial_exit",
        f"{tp_level} HIT | {symbol}{dir_str}{_tag(strategy)}{dur_str}\n"
        f"Booked: {exit_qty} | Remaining: {remaining_qty}\n"
        f"{_pnl(pnl)}{_r(r_multiple)}{sl_str}{next_str}"
    )


async def notify_exit_no_position(symbol: str, strategy: str) -> None:
    logger.info(f"EXIT ignored | {symbol} [{strategy}] | no open position")


async def notify_exit_failed(symbol: str, reason: str, strategy: str = "") -> None:
    await notify_event("exit_failed",
        f"EXIT FAILED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
        f"Reason: {reason}"
    )


# ── Position lifecycle ─────────────────────────────────────────────────────────

async def notify_position_closed(
    symbol: str,
    pnl: float,
    strategy: str = "",
    exit_price: float | None = None,
    direction: str | None = None,
    r_multiple: float | None = None,
    entry_price: float | None = None,
    hold_minutes: int = 0,
    exit_types: list[str] | None = None,
    day_context: str = "",
) -> None:
    last_exit = (exit_types or [])[-1].upper() if exit_types else ""
    logger.info(
        f"CLOSED | {symbol} [{strategy}] {last_exit} entry={entry_price} exit={exit_price} "
        f"pnl={_pnl(pnl)}{_r(r_multiple)} held={hold_minutes}min"
    )
    outcome = "WIN" if pnl >= 0 else "LOSS"
    dir_str = f" {_dir(direction)}" if direction else ""
    dur_str = f" | held {_dur(hold_minutes)}" if hold_minutes > 0 else ""
    entry_str = f"{entry_price:.2f}" if entry_price is not None else "—"
    exit_str = f"{exit_price:.2f}" if exit_price is not None else "—"
    ctx_str = f"\n{day_context}" if day_context else ""
    await notify_event("position_closed",
        f"{outcome} CLOSED [{last_exit}] | {symbol}{dir_str}{_tag(strategy)}{dur_str}\n"
        f"{entry_str} → {exit_str} | {_pnl(pnl)}{_r(r_multiple)}{ctx_str}"
    )


async def notify_be_stop_applied(
    symbol: str,
    be_price: float,
    ltp: float,
    progress: float,
    strategy: str = "",
    direction: str | None = None,
    age_minutes: int = 0,
    entry_price: float | None = None,
    original_sl: float | None = None,
) -> None:
    logger.info(
        f"BE stop | {symbol} [{strategy}] sl_moved={original_sl}->{be_price:.2f} ltp={ltp:.2f} "
        f"progress={progress:.0%} age={age_minutes}min"
    )
    dir_str = f" {_dir(direction)}" if direction else ""
    sl_move = f"{original_sl:.2f} → {be_price:.2f}" if original_sl is not None else f"→ {be_price:.2f}"
    await notify_event("be_stop_applied",
        f"STOP → BREAK-EVEN | {symbol}{dir_str}{_tag(strategy)} | {_now_ist()}\n"
        f"SL: {sl_move} | LTP: {ltp:.2f} | Progress: {progress:.0%} | Age: {age_minutes}min"
    )


async def notify_no_progress_exit(
    symbol: str,
    ltp: float,
    entry: float,
    progress: float,
    strategy: str = "",
    direction: str | None = None,
    age_minutes: int = 0,
) -> None:
    diff = ltp - entry
    logger.info(
        f"No-progress exit | {symbol} [{strategy}] ltp={ltp:.2f} entry={entry:.2f} "
        f"diff={diff:+.2f} progress={progress:.0%} age={age_minutes}min"
    )
    dir_str = f" {_dir(direction)}" if direction else ""
    await notify_event("no_progress_exit",
        f"NO-PROGRESS EXIT | {symbol}{dir_str}{_tag(strategy)} | {_now_ist()}\n"
        f"{entry:.2f} → {ltp:.2f} ({diff:+.2f}) | Progress: {progress:.0%} | Age: {age_minutes}min"
    )


async def notify_orphaned_position(
    symbol: str,
    strategy: str,
    direction: str,
    order_id: str,
    reason: str,
) -> None:
    """Entry order was rejected or never confirmed — no position was taken."""
    # Map technical reason strings to plain language
    if "rejected" in reason.lower() or "cancel" in reason.lower():
        plain_reason = "Entry order was rejected by the broker."
    elif "unresolved" in reason.lower() or "status" in reason.lower():
        plain_reason = "Entry order status could not be confirmed — assumed not filled."
    elif "zero pnl" in reason.lower() or "unconfirmed fill" in reason.lower():
        plain_reason = "No fill detected — order likely did not execute."
    else:
        plain_reason = reason

    await notify_event("orphaned_position",
        f"ORDER NOT FILLED | {symbol} {_dir(direction)}{_tag(strategy)} | {_now_ist()}\n"
        f"No position taken. {plain_reason}\n"
        f"Check broker terminal: order {order_id}"
    )


async def notify_time_exit(
    symbol: str,
    strategy: str = "",
    direction: str | None = None,
    pnl: float | None = None,
    r_multiple: float | None = None,
    entry_price: float | None = None,
    hold_minutes: int = 0,
    day_context: str = "",
) -> None:
    dir_str = f" {_dir(direction)}" if direction else ""
    dur_str = f" | held {_dur(hold_minutes)}" if hold_minutes > 0 else ""

    traj = f"{entry_price:.2f} → —" if entry_price is not None else "—"

    pnl_str = ""
    if pnl is not None:
        pnl_str = f"\n{traj} | {_pnl(pnl)}{_r(r_multiple)}"

    ctx_str = f"\n{day_context}" if day_context else ""
    await notify_event("time_exit",
        f"TIME EXIT | {symbol}{dir_str}{_tag(strategy)}{dur_str}{pnl_str}{ctx_str}"
    )


# ── Risk events ────────────────────────────────────────────────────────────────

async def notify_risk_limit_hit(reason: str) -> None:
    await notify_event("risk_limit_hit",
        f"TRADING HALTED | {_now_ist()}\n"
        f"Risk limit: {reason}\n"
        f"New entries blocked. Existing positions monitored normally."
    )


# ── Daily summary ──────────────────────────────────────────────────────────────

#: Where the id of whichever day summary is currently pinned in each notify_channel chat is
#: persisted, keyed by chat id (as a string) - see _send_and_pin_day_summary()'s docstring.
#: Same JSON-file idiom as strategy_cards.py's _STATE_PATH, for the same reason: this is UI
#: bookkeeping, not trading data.
_DAY_SUMMARY_PIN_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "data", "day_summary_pin_state.json"
)


def _load_day_summary_pin_state() -> dict:
    try:
        with open(_DAY_SUMMARY_PIN_STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_day_summary_pin_state(state: dict) -> None:
    """Best-effort persist - a failed write only means tomorrow's summary fails to find
    yesterday's pin to replace, leaving two pinned (a cosmetic annoyance), not a reason to
    fail the send."""
    try:
        os.makedirs(os.path.dirname(_DAY_SUMMARY_PIN_STATE_PATH), exist_ok=True)
        with open(_DAY_SUMMARY_PIN_STATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError as e:
        logger.warning(f"Day summary pin: could not persist pin state: {e}")


async def _send_and_pin_day_summary(text: str) -> bool:
    """Send the day summary exactly like any other notify_event(), then ALSO pin it, replacing
    yesterday's pin in the same channel - so "how did today go" is always one tap away without
    scrolling, while every day's summary still stays in the channel's ordinary history too.

    Pinning is strictly additive: the return value reflects only whether the SEND succeeded
    (same contract notify() already has, which tracker.py's day-done marker depends on) - a
    pin/unpin failure (bot lost admin rights, message too old to pin, etc.) is logged and
    swallowed, never turned into "the summary wasn't delivered".
    """
    if not settings.notify_channel:
        return False
    if not should_notify("day_summary", getattr(settings, "notify_level", "normal")):
        return False
    if _client is None:
        logger.warning("Notifier: client not ready, skipping (message dropped, not queued)")
        return False
    channel = _channel_for_phase(await _current_phase())
    if channel is None:
        return False

    try:
        message = await _client.send_message(channel.id, text)
    except asyncio.CancelledError:
        logger.debug("Notifier: send cancelled (event loop shutting down)")
        return False
    except Exception as e:
        logger.warning(f"Notifier: failed to send message: {e}")
        return False

    try:
        state = _load_day_summary_pin_state()
        key = str(channel.id)
        previous_message_id = state.get(key)
        await _client.pin_message(channel.id, message, notify=False)
        if previous_message_id:
            try:
                await _client.unpin_message(channel.id, previous_message_id)
            except Exception as e:
                logger.debug(f"Day summary pin: could not unpin yesterday's summary: {e}")
        state[key] = getattr(message, "id", None)
        _save_day_summary_pin_state(state)
    except Exception as e:
        logger.warning(f"Day summary pin: could not pin today's summary (non-fatal): {e}")

    return True


async def notify_day_summary(
    trades: int,
    wins: int,
    losses: int,
    net_pnl: float,
    capital: float,
    time_exits: int = 0,
    trade_records=None,
    strategy_capital: dict | None = None,
) -> bool:
    """Send today's summary three ways: one per-strategy summary to each strategy's OWN
    channel (best-effort, see _send_per_strategy_day_summaries()), plus ONE consolidated
    summary - a pooled total and, when more than one strategy traded today, a side-by-side
    comparison table - to notify_channel.

    Why split at all: each strategy sizes off its OWN cached day-start capital (RiskEngine's
    per-strategy isolation) and has its own edge, sample size, and promotion decision riding
    on it. A single blended win-rate/P&L/capital-trajectory number answers "how did everything
    combined do", which is not the question a trader asks when deciding whether a SPECIFIC
    strategy is working - it can hide one strategy's losses behind another's wins, and its
    capital-trajectory line does not correspond to any real account once more than one
    strategy is pooled into it.

    Returns whether the CONSOLIDATED summary reached Telegram - see notify()'s docstring.
    tracker.py's send_day_summary() uses this to decide whether it may mark the day done; a
    per-strategy send failing does not block that, or any other strategy's own send.
    """
    today = datetime.now(IST).strftime("%d-%b-%Y")

    if trades == 0:
        return await _send_and_pin_day_summary(f"DAY SUMMARY | {today}\nNo trades taken today.")

    trade_records = trade_records or []
    by_strategy = _group_by_strategy(trade_records)
    phase = await _current_phase()
    await _send_per_strategy_day_summaries(by_strategy, today, strategy_capital or {}, phase)

    lines = _day_summary_header(today, trades, wins, losses, net_pnl, capital, time_exits, trade_records)
    if len(by_strategy) > 1:
        lines.append("")
        lines.append("By strategy (best to worst avg R):")
        lines += _comparison_rows(by_strategy)
    if trade_records:
        lines.append("─" * 36)
        # Best trade first
        lines += [_trade_line(rec) for rec in sorted(trade_records, key=lambda r: r.total_pnl, reverse=True)]

    return await _send_and_pin_day_summary("\n".join(lines))


def _group_by_strategy(trade_records) -> dict:
    groups: dict = {}
    for r in trade_records:
        groups.setdefault(r.strategy or "UNKNOWN", []).append(r)
    return groups


def _aggregate_strategy_stats(records) -> dict:
    """trades/wins/losses/time_exits for one strategy's slice of today's trade_records - same
    win/loss classification tracker.py's own day counters use: a TIME exit is counted as a
    trade but excluded from wins/losses (it was force-closed by the clock, not decided by the
    strategy's own exit rule)."""
    time_exits = sum(1 for r in records if "TIME" in (r.exit_types or []))
    decided = [r for r in records if "TIME" not in (r.exit_types or [])]
    wins = sum(1 for r in decided if r.total_pnl >= 0)
    losses = sum(1 for r in decided if r.total_pnl < 0)
    return {"trades": len(records), "wins": wins, "losses": losses, "time_exits": time_exits}


def _best_worst_line(records) -> str:
    """One-line callout for the best and worst R-multiple trade, so a reader doesn't have to
    scan the whole per-trade table to find them."""
    scored = [r for r in records if r.r_multiple is not None]
    if not scored:
        return ""
    best = max(scored, key=lambda r: r.r_multiple)
    worst = min(scored, key=lambda r: r.r_multiple)
    if best is worst:
        return f"Only scored trade: {best.symbol} ({best.r_multiple:+.1f}R)"
    return f"Best: {best.symbol} ({best.r_multiple:+.1f}R) | Worst: {worst.symbol} ({worst.r_multiple:+.1f}R)"


def _comparison_rows(by_strategy: dict) -> list:
    """One row per strategy, ranked best-to-worst by avg R - not net ₹, since avg R is the
    fair comparison: it is agnostic to how much notional/risk-% each strategy happened to be
    sized with, which net ₹ is not."""
    rows = []
    for strategy, records in by_strategy.items():
        stats = _aggregate_strategy_stats(records)
        decided = stats["wins"] + stats["losses"]
        win_rate = stats["wins"] / decided * 100 if decided > 0 else 0.0
        avg_r = _average_r(records)
        net_pnl = sum(r.total_pnl for r in records)
        rows.append((strategy, stats["trades"], stats["wins"], stats["losses"], win_rate, avg_r, net_pnl))

    rows.sort(key=lambda row: row[5] if row[5] is not None else float("-inf"), reverse=True)
    width = max(len(row[0]) for row in rows)
    lines = []
    for strategy, trade_count, wins, losses, win_rate, avg_r, net_pnl in rows:
        r_str = f"{avg_r:+.1f}R" if avg_r is not None else "  —  "
        lines.append(
            f"{strategy:<{width}}  {trade_count}T  W{wins} L{losses}  {win_rate:>3.0f}%  "
            f"{r_str:>6}  {_pnl(net_pnl)}"
        )
    return lines


async def _send_per_strategy_day_summaries(
    by_strategy: dict, today: str, strategy_capital: dict, phase: str,
) -> None:
    """One summary per strategy, to that strategy's OWN -analyze/-live channel - best-effort:
    an unconfigured channel, or one strategy's send failing, must never block another
    strategy's summary or the consolidated admin one (see notify_day_summary())."""
    if _client is None:
        return
    if not should_notify("day_summary", getattr(settings, "notify_level", "normal")):
        return
    for strategy, records in by_strategy.items():
        channel = _channel_for_strategy(strategy, phase)
        if channel is None:
            continue
        stats = _aggregate_strategy_stats(records)
        net_pnl = sum(r.total_pnl for r in records)
        capital = strategy_capital.get(strategy, 0.0)
        lines = _day_summary_header(
            today, stats["trades"], stats["wins"], stats["losses"],
            net_pnl, capital, stats["time_exits"], records, title=f"{strategy} DAY SUMMARY",
        )
        best_worst = _best_worst_line(records)
        if best_worst:
            lines.append(best_worst)
        lines.append("─" * 36)
        lines += [_trade_line(rec) for rec in sorted(records, key=lambda r: r.total_pnl, reverse=True)]
        try:
            await _client.send_message(channel.id, "\n".join(lines))
        except Exception as e:
            logger.warning(f"Day summary: could not send {strategy}'s summary: {e}")


def _day_summary_header(
    today: str, trades: int, wins: int, losses: int, net_pnl: float,
    capital: float, time_exits: int, trade_records, title: str = "DAY SUMMARY",
) -> list:
    """Headline block: counts, win rate, net P&L, average R, capital trajectory."""
    decided = wins + losses
    win_rate = wins / decided * 100 if decided > 0 else 0
    pct = net_pnl / capital * 100 if capital > 0 else 0
    pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"

    # Avg R across all decided trades (wins+losses)
    avg_r = _average_r(trade_records)
    t_str = f"  T: {time_exits}" if time_exits > 0 else ""
    # `capital` is the OPENING balance, not the closing one: sizing runs off
    # RiskEngine.get_sizing_capital(), which caches the first funds-API fetch of the day when
    # use_day_start_capital is on, and calculate_quantity stamps that value into
    # _last_known_capital — which is what send_day_summary passes here. Deriving the opening
    # as (capital - net_pnl) therefore subtracted the day's P&L from a figure that already
    # WAS the opening, shifting both ends of the line down by net_pnl.
    closing_capital = capital + net_pnl

    return [
        f"{title} | {today}",
        f"Trades: {trades} | W: {wins}  L: {losses}{t_str} | Win Rate: {win_rate:.0f}%",
        f"Net: {_pnl(net_pnl)} ({pct_str})" + (f" | Avg R: {avg_r:+.1f}R" if avg_r is not None else ""),
        f"Capital: ₹{capital:,.0f} → ₹{closing_capital:,.0f}",
    ]


def _average_r(trade_records) -> float | None:
    """Mean R-multiple across trades that have one, or None."""
    if not trade_records:
        return None
    r_values = [r.r_multiple for r in trade_records if r.r_multiple is not None]
    return sum(r_values) / len(r_values) if r_values else None


def _trade_line(rec) -> str:
    """One row of the per-trade table in the day summary."""
    dir_icon = "▲" if rec.direction == "LONG" else "▼"
    exit_str = f"{rec.exit_price:.2f}" if rec.exit_price is not None else "—"
    pnl_str = _pnl(rec.total_pnl) if rec.total_pnl != 0 else "₹0"
    r_str = f"  ({rec.r_multiple:+.1f}R)" if rec.r_multiple is not None else ""
    types_str = "+".join(rec.exit_types) if rec.exit_types else ""
    # Flag orphan/suspicious trades (0 PnL and entry == exit price)
    orphan_flag = ""
    if (
        rec.total_pnl == 0.0
        and rec.exit_price is not None
        and abs(rec.entry_price - rec.exit_price) < 0.01
    ):
        orphan_flag = "  [CHECK]"
    return (
        f"{dir_icon} {rec.symbol:<12} {rec.entry_price:.2f}→{exit_str:<8} "
        f"{pnl_str:<10}{r_str}  {types_str}{orphan_flag}"
    )


async def notify_engine_stopped() -> None:
    await notify_event("engine_stopped", f"Engine stopped | {_now_ist()}")


async def _send_oneshot(msg: str) -> None:
    """Send a single message via a fresh, disposable Telegram client.

    Startup notifications fire before start_listener() calls set_client(), so the
    module-level _client is always None at this point — notify() would silently
    drop them. Used only for the startup path (notify_startup_result,
    notify_startup_summary), which is why it opens and tears down its own client
    against the same session file instead of reusing the long-lived one.
    """
    if not settings.notify_channel:
        return
    channel = _channel_for_phase(await _current_phase())
    if channel is None:
        return

    from telethon import TelegramClient

    session_path = "signal_engine/data/telegram"
    try:
        client = TelegramClient(
            session_path,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("Startup notifier: Telegram not authorized, skipping notification")
            await client.disconnect()
            return
        await client.send_message(channel.id, msg)
        await client.disconnect()
    except Exception as e:
        logger.warning(f"Startup notifier: could not send Telegram message: {e}")


async def notify_startup_result(all_passed: bool, summary: str) -> None:
    """Send a startup-failure alert via a one-shot Telegram client.

    Only used on the critical-failure path — a successful startup sends the
    single consolidated notify_startup_summary() instead, so the user never
    gets two overlapping startup messages.
    """
    icon = "READY" if all_passed else "STARTUP FAILED"
    await _send_oneshot(f"{icon} | Signal Engine | {_now_ist()}\n{summary}")


#: Checks that report on OpenAlgo/broker availability rather than the signal engine's
#: own modules — kept in their own message section so the two failure domains (an
#: OpenAlgo outage vs a signal_engine bug) are never conflated.
_OPENALGO_CHECK_NAMES = {
    "2. OpenAlgo reachable",
    "3. Broker auth (funds API)",
    "4. Quote API (SBIN LTP)",
}


def _check_line(check) -> str:
    label = check.name.split(". ", 1)[-1]
    status = "OK" if check.passed else "FAIL"
    return f"  {status} — {label}: {check.message}"


def build_startup_summary_message(report, mode: str, capital: float, broker_name: str) -> str:
    """Build the one consolidated startup/smoke-test message for Telegram.

    A routine, fully-green restart gets ONE line - the engine restarts daily (and after every
    code change), so a full checklist that never changes just trains the reader to stop
    looking at it. The moment there is something to troubleshoot - a warning-level check
    failed; a critical failure never reaches here, see run_startup_health_checks()'s docstring
    - the full breakdown returns: an OpenAlgo section (broker connectivity, auth, market data)
    and a Signal Engine section (own config, pipeline, risk state, storage), so a reader can
    tell at a glance which side of the integration is unavailable.
    """
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    if report.all_passed:
        return (
            f"Signal Engine Startup — READY | {now}\n"
            f"Broker: {broker_name} | Mode: {mode} | Capital: ₹{capital:,.0f}\n"
            f"All {len(report.checks)} checks passed."
        )

    by_name = {c.name: c for c in report.checks}
    openalgo_lines = [_check_line(by_name[n]) for n in sorted(_OPENALGO_CHECK_NAMES) if n in by_name]
    engine_checks = sorted(
        (c for c in report.checks if c.name not in _OPENALGO_CHECK_NAMES),
        key=lambda c: c.name,
    )
    engine_lines = [_check_line(c) for c in engine_checks]

    status = f"READY WITH WARNINGS ({report.fail_count} check(s) failed)"
    ch_list = ", ".join(ch.name for ch in settings.telegram_channels) or "none"

    lines = [
        f"Signal Engine Startup — {status}",
        f"Time: {now}",
        f"Broker: {broker_name} | Mode: {mode} | Capital: ₹{capital:,.0f}",
        "",
        "-- OpenAlgo --",
        *openalgo_lines,
        "",
        "-- Signal Engine --",
        *engine_lines,
        f"  Config: {settings.exchange}/{settings.product}/{settings.order_type} | "
        f"sizing={settings.sizing_mode} | risk/trade={settings.risk_per_trade * 100:.1f}% | "
        f"max_positions={settings.max_open_positions} | "
        f"daily_loss_limit={settings.daily_loss_limit * 100:.1f}%",
        "",
        f"Channels: {ch_list}",
    ]
    return "\n".join(lines)


async def notify_startup_summary(report, mode: str, capital: float, broker_name: str) -> None:
    """Send the one consolidated startup/smoke-test summary — fires once per engine
    start/restart, replacing the separate startup-result and engine-started messages."""
    await _send_oneshot(build_startup_summary_message(report, mode, capital, broker_name))
