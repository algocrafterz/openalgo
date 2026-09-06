"""Telegram client connection and message polling."""

import asyncio
from datetime import datetime, timezone
from typing import Callable, Coroutine

from loguru import logger

from signal_engine.config import settings
from signal_engine import notifier

# Ping Telegram every 90s to detect stale connections (common in WSL2 where
# TCP keepalives across the NAT bridge can silently die without triggering a reconnect).
_KEEPALIVE_INTERVAL = 90


def _split_by_enabled(channels) -> tuple:
    """(subscribed, skipped) — the channels to watch, and the ones deliberately switched off.

    Both halves are returned because a skipped channel still has to be reported at startup.
    An `enabled: false` entry that produced no log line would be indistinguishable from a
    channel nobody ever added, which is the failure mode this flag exists to remove.
    """
    watching = tuple(ch for ch in channels if ch.enabled)
    skipped = tuple(ch for ch in channels if not ch.enabled)
    return watching, skipped


def _channel_names(channels=None) -> dict:
    """Chat-id -> channel name, with the string form mapped too for lookup safety.

    Covers DISABLED channels as well: subscriptions are per chat id, but if a message from
    one ever reaches the handler it should be logged by name rather than as a bare id.
    """
    names = {}
    for ch in settings.telegram_channels if channels is None else channels:
        names[ch.id] = ch.name
        names[str(ch.id)] = ch.name
    return names


def _is_stale(msg) -> float | None:
    """Age of a message in seconds if it is too old to act on, else None."""
    msg_time = msg.date.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - msg_time).total_seconds()
    return age if age > settings.stale_signal_seconds else None


def _make_handler(on_message, channel_names: dict):
    """Build the NewMessage handler bound to this run's channel-name lookup."""

    async def handler(event):
        msg = event.message
        if not msg.text:
            return

        chat_id = event.chat_id
        source = channel_names.get(chat_id, channel_names.get(str(chat_id), str(chat_id)))

        stale_age = _is_stale(msg)
        if stale_age is not None:
            logger.debug(f"Skipping stale message from [{source}] ({stale_age:.0f}s old)")
            return

        clean_text = " | ".join(line.strip() for line in msg.text.strip().splitlines() if line.strip())
        logger.info(f"[{source}] Signal received: {clean_text}")
        await on_message(msg.text)

    return handler


async def _keepalive(client) -> None:
    """Periodically ping Telegram to detect and surface stale connections.

    On failure, disconnect so run_until_disconnected returns and triggers a retry.
    """
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL)
        try:
            await client.get_me()
            logger.debug("Keepalive ping OK")
        except Exception as e:
            logger.warning(f"Keepalive ping failed: {e} — connection may be stale")
            await client.disconnect()
            return


async def _connect(client) -> None:
    """Authenticate and register the client as the notification transport."""
    await client.start(phone=settings.telegram_phone)
    notifier.set_client(client)
    watching, skipped = _split_by_enabled(settings.telegram_channels)
    for ch in watching:
        logger.info(f"Watching channel: {ch.name} ({ch.id})")
    for ch in skipped:
        logger.info(
            f"Channel DISABLED, no trades will be taken from it: {ch.name} ({ch.id}) "
            "— set enabled: true in config.yaml to go live on it"
        )


async def _serve(client) -> None:
    """Hold a connected session open until Telegram drops it."""
    keepalive_task = asyncio.create_task(_keepalive(client))
    try:
        await client.run_until_disconnected()
    finally:
        keepalive_task.cancel()


async def start_listener(
    on_message: Callable[[str], Coroutine],
) -> None:
    """Connect to Telegram and listen for signals on all configured channels."""
    from telethon import TelegramClient, events

    if not settings.telegram_channels:
        logger.error("No Telegram channels configured in config.yaml (telegram.channels)")
        return

    watching, skipped = _split_by_enabled(settings.telegram_channels)
    if not watching:
        logger.error(
            f"All {len(skipped)} configured Telegram channels are disabled "
            f"({', '.join(ch.name for ch in skipped)}) — the engine will trade nothing. "
            "Set enabled: true on at least one channel in config.yaml."
        )
        return

    client = TelegramClient(
        "signal_engine/data/telegram",
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    chat_ids = [ch.id for ch in watching]
    channel_names = _channel_names()
    client.on(events.NewMessage(chats=chat_ids))(
        _make_handler(on_message, channel_names)
    )

    retries = 0
    while retries < settings.listener_max_retries:
        try:
            await _connect(client)
            # Only a successful connect clears the backoff counter.
            retries = 0
            await _serve(client)
        except Exception as e:
            retries += 1
            wait = settings.listener_base_backoff * (2 ** (retries - 1))
            logger.error(
                f"Telegram disconnected: {e}. "
                f"Retry {retries}/{settings.listener_max_retries} in {wait}s"
            )
            await asyncio.sleep(wait)

    logger.critical("Max retries exceeded, listener shutting down")
