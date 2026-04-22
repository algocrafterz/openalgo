"""Telegram notification sender for order state and session events.

Sends messages to notify_channel (signal-engine) so you can monitor
order placement, position lifecycle, risk events, and daily summary.

Uses the same TelegramClient as the listener (set via set_client once connected).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger

from signal_engine.config import settings

if TYPE_CHECKING:
    from telethon import TelegramClient

_client: TelegramClient | None = None
_IST = timezone(timedelta(hours=5, minutes=30))


def set_client(client: TelegramClient) -> None:
    """Called by listener once the Telegram client is connected."""
    global _client
    _client = client


async def notify(text: str) -> None:
    """Send a message to the notify_channel. No-op if not configured or client not ready."""
    if not settings.notify_channel:
        return
    if _client is None:
        logger.debug("Notifier: client not ready, skipping")
        return
    try:
        await _client.send_message(settings.notify_channel.id, text)
    except asyncio.CancelledError:
        logger.debug("Notifier: send cancelled (event loop shutting down)")
    except Exception as e:
        logger.warning(f"Notifier: failed to send message: {e}")


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
    return datetime.now(_IST).strftime("%H:%M IST")


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
    """Compact slot usage line: 'Slot 2/3 used'."""
    return f"Slot {open_positions}/{max_positions} used"


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
    await notify(
        f"📤 ENTRY SENT | {symbol} {_dir(direction)}{_tag(strategy)} | {_now_ist()}\n"
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


async def notify_order_rejected(symbol: str, reason: str, strategy: str = "") -> None:
    await notify(
        f"🚫 ENTRY REJECTED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
        f"No trade taken. Reason: {reason}"
    )


async def notify_sl_placed(
    symbol: str, order_id: str,
    strategy: str = "", sl_price: float | None = None,
) -> None:
    """Log only — SL placement confirmation is embedded in the LIVE message."""
    price_str = f" sl={sl_price:.2f}" if sl_price is not None else ""
    logger.info(f"SL confirmed | {symbol} [{strategy}]{price_str} id={order_id}")


async def notify_sl_failed(symbol: str, reason: str, strategy: str = "") -> None:
    await notify(
        f"🚨 SL NOT PLACED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
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


async def notify_exit_no_position(symbol: str, strategy: str) -> None:
    logger.info(f"EXIT ignored | {symbol} [{strategy}] | no open position")


async def notify_exit_failed(symbol: str, reason: str, strategy: str = "") -> None:
    await notify(
        f"❌ EXIT FAILED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
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

    await notify(
        f"⚠️ ORDER NOT FILLED | {symbol} {_dir(direction)}{_tag(strategy)} | {_now_ist()}\n"
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
    await notify(
        f"⏰ TIME EXIT | {symbol}{dir_str}{_tag(strategy)}{dur_str}{pnl_str}{ctx_str}"
    )


# ── Risk events ────────────────────────────────────────────────────────────────

async def notify_risk_limit_hit(reason: str) -> None:
    await notify(
        f"🛑 TRADING HALTED | {_now_ist()}\n"
        f"Risk limit: {reason}\n"
        f"New entries blocked. Existing positions monitored normally."
    )


# ── Daily summary ──────────────────────────────────────────────────────────────

async def notify_day_summary(
    trades: int,
    wins: int,
    losses: int,
    net_pnl: float,
    capital: float,
    time_exits: int = 0,
    trade_records=None,
) -> None:
    today = datetime.now(_IST).strftime("%d-%b-%Y")

    if trades == 0:
        await notify(f"📊 DAY SUMMARY | {today}\nNo trades taken today.")
        return

    decided = wins + losses
    win_rate = wins / decided * 100 if decided > 0 else 0
    pct = net_pnl / capital * 100 if capital > 0 else 0
    pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"

    # Avg R across all decided trades (wins+losses)
    avg_r: float | None = None
    if trade_records:
        r_values = [r.r_multiple for r in trade_records if r.r_multiple is not None]
        if r_values:
            avg_r = sum(r_values) / len(r_values)

    t_str = f"  T: {time_exits}" if time_exits > 0 else ""
    opening_capital = capital - net_pnl

    lines = [
        f"📊 DAY SUMMARY | {today}",
        f"Trades: {trades} | W: {wins}  L: {losses}{t_str} | Win Rate: {win_rate:.0f}%",
        f"Net: {_pnl(net_pnl)} ({pct_str})" + (f" | Avg R: {avg_r:+.1f}R" if avg_r is not None else ""),
        f"Capital: ₹{opening_capital:,.0f} → ₹{capital:,.0f}",
    ]

    if trade_records:
        # Sort by total_pnl descending (best trade first)
        sorted_records = sorted(trade_records, key=lambda r: r.total_pnl, reverse=True)
        lines.append("─" * 36)
        for rec in sorted_records:
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
                orphan_flag = "  ⚠️"
            lines.append(
                f"{dir_icon} {rec.symbol:<12} {rec.entry_price:.2f}→{exit_str:<8} "
                f"{pnl_str:<10}{r_str}  {types_str}{orphan_flag}"
            )

    await notify("\n".join(lines))


# ── Engine lifecycle ───────────────────────────────────────────────────────────

async def notify_engine_started(capital: float, mode: str) -> None:
    await notify(
        f"🟢 Engine started | {mode} | Capital: ₹{capital:,.0f} | {_now_ist()}"
    )


async def notify_engine_stopped() -> None:
    await notify(f"🔴 Engine stopped | {_now_ist()}")


async def notify_startup_result(all_passed: bool, summary: str) -> None:
    """Send startup check result via a one-shot Telegram client.

    Called before the main listener connects, so creates its own client
    using the same session file. Used for both pass and fail notifications
    so the user knows the engine state before market open.
    """
    if not settings.notify_channel:
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
        icon = "🟢 READY" if all_passed else "🔴 STARTUP FAILED"
        msg = f"{icon} | Signal Engine | {_now_ist()}\n{summary}"
        await client.send_message(settings.notify_channel.id, msg)
        await client.disconnect()
    except Exception as e:
        logger.warning(f"Startup notifier: could not send Telegram message: {e}")
