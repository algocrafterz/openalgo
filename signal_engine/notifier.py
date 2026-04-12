"""Telegram notification sender for order state and session events.

Sends messages to notify_channel (anand_smi_assistant) so you can monitor
order placement, position lifecycle, risk events, and daily summary in real-time.

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


def _dir(direction: str) -> str:
    return "⬆️ LONG" if direction.upper() == "LONG" else "⬇️ SHORT"


def _now_ist() -> str:
    return datetime.now(_IST).strftime("%H:%M IST")


# ── Order placement ────────────────────────────────────────────────────────────

async def notify_order_placed(symbol: str, direction: str, order_id: str, strategy: str = "") -> None:
    tag = f" [{strategy}]" if strategy else ""
    await notify(f"✅ ENTRY | {symbol} {_dir(direction)}{tag} | id={order_id} | {_now_ist()}")


async def notify_entry_filled(
    symbol: str,
    direction: str,
    fill_price: float,
    qty: int,
    signal_price: float,
    strategy: str = "",
) -> None:
    """Sent after fetching actual broker fill price for the entry order."""
    tag = f" [{strategy}]" if strategy else ""
    diff = fill_price - signal_price
    diff_str = f"+{diff:.2f}" if diff >= 0 else f"{diff:.2f}"
    await notify(
        f"💰 FILLED | {symbol} {_dir(direction)}{tag} | "
        f"fill={fill_price:.2f} (signal={signal_price:.2f}, {diff_str}) | "
        f"qty={qty} | {_now_ist()}"
    )


async def notify_order_rejected(symbol: str, reason: str, strategy: str = "") -> None:
    tag = f" [{strategy}]" if strategy else ""
    await notify(f"❌ ENTRY FAILED | {symbol}{tag} | {reason}")


async def notify_sl_placed(symbol: str, direction: str, order_id: str, strategy: str = "") -> None:
    tag = f" [{strategy}]" if strategy else ""
    await notify(f"✅ SL placed | {symbol} {_dir(direction)}{tag} | id={order_id}")


async def notify_sl_failed(symbol: str, reason: str, strategy: str = "") -> None:
    tag = f" [{strategy}]" if strategy else ""
    await notify(f"❌ SL FAILED | {symbol}{tag} | {reason}")


async def notify_tp_placed(symbol: str, direction: str, order_id: str) -> None:
    await notify(f"✅ TP placed | {symbol} {_dir(direction)} | id={order_id}")


async def notify_tp_failed(symbol: str, reason: str) -> None:
    await notify(f"❌ TP FAILED | {symbol} | {reason}")


# ── Tracker-based TP monitoring (replaces broker TP LIMIT order) ───────────────

async def notify_tp_level_hit(symbol: str, ltp: float, tp: float, strategy: str = "") -> None:
    """Sent when LTP crosses the TP price — before the exit order is placed."""
    tag = f" [{strategy}]" if strategy else ""
    await notify(
        f"🎯 TP DETECTED | {symbol}{tag} | LTP={ltp:.2f} >= TP={tp:.2f} | Cancelling SL + exiting at market | {_now_ist()}"
    )


async def notify_tp_exit_placed(symbol: str, order_id: str, strategy: str = "") -> None:
    """Sent when the market exit order placed after TP detection is accepted."""
    tag = f" [{strategy}]" if strategy else ""
    await notify(f"✅ TP EXIT placed | {symbol}{tag} | Market exit id={order_id} | {_now_ist()}")


async def notify_tp_exit_failed(symbol: str, reason: str, strategy: str = "") -> None:
    """URGENT: Market exit failed after TP detection. Position is now unprotected (SL cancelled)."""
    tag = f" [{strategy}]" if strategy else ""
    await notify(
        f"🚨 TP EXIT FAILED | {symbol}{tag} | SL already cancelled — MANUAL EXIT REQUIRED | {reason} | {_now_ist()}"
    )


async def notify_sl_cancel_failed(symbol: str, sl_order_id: str, strategy: str = "") -> None:
    """Sent when SL cancellation fails before TP market exit. Non-critical — exit proceeds anyway."""
    tag = f" [{strategy}]" if strategy else ""
    await notify(
        f"⚠️ SL CANCEL FAILED | {symbol}{tag} | id={sl_order_id} | Proceeding with TP market exit | {_now_ist()}"
    )


# ── EXIT signal handling (swing strategy closes) ──────────────────────────────

async def notify_exit_signal_received(symbol: str, strategy: str) -> None:
    await notify(f"EXIT signal received | {symbol} | strategy={strategy} | {_now_ist()}")


async def notify_exit_placed(symbol: str, order_id: str, strategy: str = "") -> None:
    tag = f" [{strategy}]" if strategy else ""
    await notify(f"EXIT order placed | {symbol}{tag} | MARKET SELL id={order_id} | {_now_ist()}")


async def notify_partial_exit(
    symbol: str, exit_qty: int, remaining_qty: int, tp_level: str,
    pnl: float, strategy: str = "", new_sl: float | None = None,
) -> None:
    """Partial TP exit — position remains open with reduced quantity.

    new_sl: if provided and > 0, appended as 'SL→<price>' so the operator sees
    the runner's new floor (moved to TP1 price after a 50% partial exit).
    """
    tag = f" [{strategy}]" if strategy else ""
    pnl_str = f"+₹{pnl:,.0f}" if pnl >= 0 else f"-₹{abs(pnl):,.0f}"
    sl_str = f" | SL→{new_sl:.2f}" if new_sl and new_sl > 0 else ""
    await notify(
        f"🎯 PARTIAL EXIT | {symbol}{tag} | {tp_level} | "
        f"Exited {exit_qty} qty, remaining {remaining_qty} | P&L: {pnl_str}{sl_str} | {_now_ist()}"
    )


async def notify_exit_no_position(symbol: str, strategy: str) -> None:
    await notify(f"EXIT ignored | {symbol} | No open position for strategy={strategy} | {_now_ist()}")


async def notify_exit_failed(symbol: str, reason: str, strategy: str = "") -> None:
    tag = f" [{strategy}]" if strategy else ""
    await notify(f"EXIT FAILED | {symbol}{tag} | {reason} | {_now_ist()}")


# ── Position lifecycle ─────────────────────────────────────────────────────────

async def notify_position_closed(
    symbol: str, pnl: float, strategy: str = "", exit_price: float | None = None
) -> None:
    icon = "✅ TP HIT" if pnl >= 0 else "❌ SL HIT"
    tag = f" [{strategy}]" if strategy else ""
    pnl_str = f"+₹{pnl:,.0f}" if pnl >= 0 else f"-₹{abs(pnl):,.0f}"
    price_str = f" | exit={exit_price:.2f}" if exit_price is not None else ""
    await notify(f"{icon} | {symbol}{tag}{price_str} | P&L: {pnl_str} | {_now_ist()}")


async def notify_time_exit(symbol: str, strategy: str = "") -> None:
    tag = f" [{strategy}]" if strategy else ""
    await notify(f"⏰ TIME EXIT | {symbol}{tag} | {_now_ist()}")


# ── Risk events ────────────────────────────────────────────────────────────────

async def notify_risk_limit_hit(reason: str) -> None:
    await notify(f"🚫 RISK LIMIT | Trading paused | {reason}")


# ── Daily summary ──────────────────────────────────────────────────────────────

async def notify_day_summary(
    trades: int,
    wins: int,
    losses: int,
    net_pnl: float,
    capital: float,
) -> None:
    if trades == 0:
        await notify(f"📊 Day Summary | No trades taken today | {_now_ist()}")
        return

    win_rate = wins / trades * 100 if trades > 0 else 0
    pnl_str = f"+₹{net_pnl:,.0f}" if net_pnl >= 0 else f"-₹{abs(net_pnl):,.0f}"
    pct = net_pnl / capital * 100 if capital > 0 else 0
    pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"

    lines = [
        f"📊 Day Summary | {_now_ist()}",
        f"Trades: {trades} | W: {wins} L: {losses} | WR: {win_rate:.0f}%",
        f"Net P&L: {pnl_str} ({pct_str})",
    ]
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
