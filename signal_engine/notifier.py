"""Telegram notification sender for order state and session events.

Sends messages to notify_channel (anand_smi_assistant) so you can monitor
order placement, position lifecycle, risk events, and daily summary in real-time.

Uses the same TelegramClient as the listener (set via set_client once connected).
"""

from __future__ import annotations

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
    except Exception as e:
        logger.warning(f"Notifier: failed to send message: {e}")


def _dir(direction: str) -> str:
    return "⬆️ LONG" if direction.upper() == "LONG" else "⬇️ SHORT"


def _now_ist() -> str:
    return datetime.now(_IST).strftime("%H:%M IST")


# ── Order placement ────────────────────────────────────────────────────────────

async def notify_order_placed(symbol: str, direction: str, order_id: str) -> None:
    await notify(f"✅ ENTRY | {symbol} {_dir(direction)} | id={order_id} | {_now_ist()}")


async def notify_order_rejected(symbol: str, reason: str) -> None:
    await notify(f"❌ ENTRY FAILED | {symbol} | {reason}")


async def notify_sl_placed(symbol: str, direction: str, order_id: str) -> None:
    await notify(f"✅ SL placed | {symbol} {_dir(direction)} | id={order_id}")


async def notify_sl_failed(symbol: str, reason: str) -> None:
    await notify(f"❌ SL FAILED | {symbol} | {reason}")


async def notify_tp_placed(symbol: str, direction: str, order_id: str) -> None:
    await notify(f"✅ TP placed | {symbol} {_dir(direction)} | id={order_id}")


async def notify_tp_failed(symbol: str, reason: str) -> None:
    await notify(f"❌ TP FAILED | {symbol} | {reason}")


# ── Position lifecycle ─────────────────────────────────────────────────────────

async def notify_position_closed(symbol: str, pnl: float) -> None:
    icon = "✅ TP HIT" if pnl >= 0 else "❌ SL HIT"
    pnl_str = f"+₹{pnl:,.0f}" if pnl >= 0 else f"-₹{abs(pnl):,.0f}"
    await notify(f"{icon} | {symbol} | P&L: {pnl_str} | {_now_ist()}")


async def notify_time_exit(symbol: str) -> None:
    await notify(f"⏰ TIME EXIT | {symbol} | {_now_ist()}")


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
