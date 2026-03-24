"""Telegram client connection and message polling."""

import asyncio
from datetime import datetime, timezone
from typing import Callable, Coroutine

from loguru import logger

from signal_engine.config import settings
from signal_engine import notifier


async def start_listener(
    on_message: Callable[[str], Coroutine],
) -> None:
    """Connect to Telegram and listen for signals on all configured channels."""
    from telethon import TelegramClient, events

    if not settings.telegram_channels:
        logger.error("No Telegram channels configured in config.yaml (telegram.channels)")
        return

    session_path = "signal_engine/data/telegram"
    client = TelegramClient(
        session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )

    # Build chat list and name lookup from configured channels
    chat_ids = []
    channel_names = {}
    for ch in settings.telegram_channels:
        chat_ids.append(ch.id)
        channel_names[ch.id] = ch.name
        # Also map string version for lookup safety
        channel_names[str(ch.id)] = ch.name

    @client.on(events.NewMessage(chats=chat_ids))
    async def handler(event):
        msg = event.message
        if not msg.text:
            return

        # Identify which channel the message came from
        chat_id = event.chat_id
        source = channel_names.get(chat_id, channel_names.get(str(chat_id), str(chat_id)))

        # Stale signal guard
        msg_time = msg.date.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - msg_time).total_seconds()
        if age > settings.stale_signal_seconds:
            logger.debug(f"Skipping stale message from [{source}] ({age:.0f}s old)")
            return

        clean_text = " | ".join(line.strip() for line in msg.text.strip().splitlines() if line.strip())
        logger.info(f"[{source}] Signal received: {clean_text}")
        await on_message(msg.text)

    retries = 0
    while retries < settings.listener_max_retries:
        try:
            await client.start(phone=settings.telegram_phone)
            notifier.set_client(client)
            for ch in settings.telegram_channels:
                logger.info(f"Watching channel: {ch.name} ({ch.id})")
            retries = 0
            await client.run_until_disconnected()
        except Exception as e:
            retries += 1
            wait = settings.listener_base_backoff * (2 ** (retries - 1))
            logger.error(
                f"Telegram disconnected: {e}. "
                f"Retry {retries}/{settings.listener_max_retries} in {wait}s"
            )
            await asyncio.sleep(wait)

    logger.critical("Max retries exceeded, listener shutting down")
