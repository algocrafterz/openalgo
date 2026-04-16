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


# ── Order placement ────────────────────────────────────────────────────────────

async def notify_order_placed(
    symbol: str,
    direction: str,
    order_id: str,
    strategy: str = "",
    signal_price: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    rr: float | None = None,
) -> None:
    """Brief confirmation that the entry order reached the broker."""
    rr_str = f" | R:R 1:{rr:.1f}" if rr is not None else ""
    sl_str = f" | SL: {sl:.2f}" if sl is not None else ""
    tp_str = f" | TP: {tp:.2f}" if tp is not None else ""
    price_str = f"Signal: {signal_price:.2f}" if signal_price is not None else "Signal: —"
    await notify(
        f"📤 ENTRY SENT | {symbol} {_dir(direction)}{_tag(strategy)} | {_now_ist()}\n"
        f"{price_str}{sl_str}{tp_str}{rr_str}"
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
    """Position is live: entry filled and SL is active. Definitive confirmation."""
    if fill_price > 0:
        diff = fill_price - signal_price
        diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
        fill_line = f"Fill: {fill_price:.2f} (slip {diff_str}) × {qty} qty"
    else:
        fill_line = f"Fill: pending — signal {signal_price:.2f} × {qty} qty"

    sl_str = f" | SL: {sl:.2f}" if sl is not None else ""
    tp_str = f" | TP: {tp:.2f}" if tp is not None else ""
    risk_str = ""
    if sl is not None and fill_price > 0:
        risk_inr = qty * abs(fill_price - sl)
        risk_str = f" | Risk: ₹{risk_inr:,.0f}"

    await notify(
        f"💰 LIVE | {symbol} {_dir(direction)}{_tag(strategy)} | {_now_ist()}\n"
        f"{fill_line}{sl_str}{tp_str}{risk_str}"
    )


async def notify_order_rejected(symbol: str, reason: str, strategy: str = "") -> None:
    await notify(
        f"🚫 ENTRY REJECTED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
        f"Reason: {reason} | Slot free"
    )


async def notify_sl_placed(
    symbol: str, direction: str, order_id: str,
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


async def notify_tp_placed(symbol: str, direction: str, order_id: str) -> None:
    logger.info(f"TP placed | {symbol} {_dir(direction)} | id={order_id}")


async def notify_tp_failed(symbol: str, reason: str) -> None:
    logger.warning(f"TP FAILED | {symbol} | {reason}")


# ── Tracker-based TP monitoring ────────────────────────────────────────────────

async def notify_tp_level_hit(symbol: str, ltp: float, tp: float, strategy: str = "") -> None:
    logger.info(f"TP DETECTED | {symbol} [{strategy}] | LTP={ltp:.2f} >= TP={tp:.2f}")


async def notify_tp_exit_placed(symbol: str, order_id: str, strategy: str = "") -> None:
    logger.info(f"TP EXIT placed | {symbol} [{strategy}] | id={order_id}")


async def notify_tp_exit_failed(
    symbol: str, reason: str, strategy: str = "",
    entry_price: float | None = None, qty: int = 0,
) -> None:
    """URGENT: TP market exit failed after SL was cancelled — position unprotected."""
    context = ""
    if entry_price is not None and qty > 0:
        context = f"\nEntry: {entry_price:.2f} | Qty: {qty} — act NOW"
    await notify(
        f"🚨 MANUAL EXIT REQUIRED | {symbol}{_tag(strategy)} | {_now_ist()}\n"
        f"TP exit failed. SL was cancelled — position is UNPROTECTED.\n"
        f"Reason: {reason}{context}"
    )


async def notify_sl_cancel_failed(symbol: str, sl_order_id: str, strategy: str = "") -> None:
    """SL cancellation failed — non-critical, TP exit still proceeds."""
    logger.warning(f"SL cancel failed | {symbol} [{strategy}] id={sl_order_id} — proceeding with TP exit")


# ── EXIT signal handling ───────────────────────────────────────────────────────

async def notify_exit_signal_received(symbol: str, strategy: str) -> None:
    logger.info(f"EXIT signal | {symbol} [{strategy}]")


async def notify_exit_placed(symbol: str, order_id: str, strategy: str = "") -> None:
    logger.info(f"EXIT order placed | {symbol} [{strategy}] | id={order_id}")


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
    """Partial TP exit — position remains open with reduced quantity."""
    dur_str = f" | held {_dur(hold_minutes)}" if hold_minutes > 0 else ""

    # Price trajectory: entry → exit if both known
    if entry_price is not None and exit_price is not None:
        traj = f"{entry_price:.2f} → {exit_price:.2f} × {exit_qty}"
    elif entry_price is not None:
        traj = f"{entry_price:.2f} → — × {exit_qty}"
    else:
        traj = f"{exit_qty} qty"

    pnl_str = f"{_pnl(pnl)}{_r(r_multiple)}"

    sl_str = f" | SL → {new_sl:.2f}" if new_sl and new_sl > 0 else ""
    next_str = (
        f" | Next {next_tp_label}: {next_tp_price:.2f}"
        if next_tp_label and next_tp_price
        else ""
    )

    dir_str = f" {_dir(direction)}" if direction else ""
    await notify(
        f"🎯 {tp_level} HIT | {symbol}{dir_str}{_tag(strategy)}{dur_str}\n"
        f"{traj} → {pnl_str}\n"
        f"Runner: {remaining_qty} qty{sl_str}{next_str}"
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
) -> None:
    if pnl >= 0:
        icon = "✅ TP WIN"
    else:
        icon = "❌ SL HIT"

    dir_str = f" {_dir(direction)}" if direction else ""
    dur_str = f" | held {_dur(hold_minutes)}" if hold_minutes > 0 else ""

    if entry_price is not None and exit_price is not None:
        traj = f"{entry_price:.2f} → {exit_price:.2f}"
    elif entry_price is not None:
        traj = f"{entry_price:.2f} → —"
    elif exit_price is not None:
        traj = f"— → {exit_price:.2f}"
    else:
        traj = "—"

    await notify(
        f"{icon} | {symbol}{dir_str}{_tag(strategy)}{dur_str}\n"
        f"{traj} | {_pnl(pnl)}{_r(r_multiple)}"
    )


async def notify_be_stop_applied(
    symbol: str,
    be_price: float,
    ltp: float,
    progress: float,
    strategy: str = "",
    direction: str | None = None,
    age_minutes: int = 0,
) -> None:
    dir_str = f" {_dir(direction)}" if direction else ""
    await notify(
        f"⚠️ STOP → BREAK-EVEN | {symbol}{dir_str}{_tag(strategy)}\n"
        f"Stuck {age_minutes}min: LTP {ltp:.2f} only {progress:.0%} toward TP → SL now {be_price:.2f}\n"
        f"Risk eliminated. Next: TP hit or flat exit at {be_price:.2f}"
    )


async def notify_orphaned_position(
    symbol: str,
    strategy: str,
    direction: str,
    order_id: str,
    reason: str,
) -> None:
    """Order was placed but never filled — slot released without recording a trade."""
    await notify(
        f"⚠️ ORDER NOT FILLED | {symbol} {_dir(direction)}{_tag(strategy)} | {_now_ist()}\n"
        f"Slot released. Reason: {reason}\n"
        f"Order ID: {order_id} — verify in broker terminal"
    )


async def notify_time_exit(
    symbol: str,
    strategy: str = "",
    direction: str | None = None,
    pnl: float | None = None,
    r_multiple: float | None = None,
    entry_price: float | None = None,
    hold_minutes: int = 0,
) -> None:
    dir_str = f" {_dir(direction)}" if direction else ""
    dur_str = f" | held {_dur(hold_minutes)}" if hold_minutes > 0 else ""

    traj = f"{entry_price:.2f} → —" if entry_price is not None else "—"

    pnl_str = ""
    if pnl is not None:
        pnl_str = f"\n{traj} | {_pnl(pnl)}{_r(r_multiple)}"

    await notify(
        f"⏰ TIME EXIT | {symbol}{dir_str}{_tag(strategy)}{dur_str}{pnl_str}"
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
