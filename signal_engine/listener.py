"""Telegram client connection and message polling."""

import asyncio
import random
from datetime import datetime, timezone
from typing import Callable, Coroutine

from loguru import logger

from signal_engine.config import settings
from signal_engine import mode_guard, notifier

# Check the connection every 90s so a stale one is noticed (common in WSL2, where TCP
# keepalives across the NAT bridge can silently die without triggering a reconnect).
#
# This used to call client.get_me(), which issues a real GetUsersRequest. On 2026-09-08 that
# was the source of 64 of the 80 FLOOD_WAIT errors in errors_2026-09-08.jsonl — the keepalive
# triggered the throttle it exists to detect. client.is_connected() is a local state read
# and costs no API call.
_KEEPALIVE_INTERVAL = 90

# Cap on how long a single FLOOD_WAIT is honoured before giving up on this connect attempt.
# Telegram's waits are usually seconds to minutes; anything beyond this is better surfaced
# to the operator than slept through in silence.
_MAX_FLOOD_WAIT_SECONDS = 900

# Jitter added on top of Telegram's stated wait, so a reconnect never lands on the exact
# tick the ban lifts (and so two processes sharing a session don't retry in lockstep).
_FLOOD_WAIT_JITTER_SECONDS = 5

# Consecutive FLOOD_WAITs tolerated before giving up. A flood wait is not a connection
# failure — the wait IS the remedy — so it must not consume the reconnect budget meant for
# genuine failures. It still needs its own bound, or a permanent throttle loops forever.
_MAX_CONSECUTIVE_FLOOD_WAITS = 5

# Waits shorter than this are absorbed by Telethon itself rather than surfacing as an
# exception. Default is 60s; raising it means only the genuinely long bans reach our loop.
_FLOOD_SLEEP_THRESHOLD = 120

#: Phase suffixes a channel name may carry. A channel without one (smidestn, or any ad-hoc
#: channel) is phase-agnostic and always processed — the suffix encodes the promotion
#: workflow, and a channel outside that workflow should not be forced into it.
_PHASE_SUFFIXES = ("analyze", "live")


def _channel_phase(name: str) -> str | None:
    """"analyze" / "live" from the channel's own name suffix, or None if it has neither."""
    lowered = (name or "").strip().lower()
    for phase in _PHASE_SUFFIXES:
        if lowered.endswith(f"-{phase}"):
            return phase
    return None


def _flood_wait_error():
    """Telethon's FloodWaitError class, imported lazily so this module stays importable
    (and testable) without telethon present."""
    from telethon.errors import FloodWaitError

    return FloodWaitError


def _flood_wait_seconds(exc) -> float:
    """How long to actually sleep for a FLOOD_WAIT: Telegram's own stated wait plus jitter,
    capped. The old loop backed off 2/4/8/16/32s against a stated 349 seconds, so every
    retry landed inside the ban and extended it."""
    seconds = getattr(exc, "seconds", None)
    if not isinstance(seconds, (int, float)):
        return _MAX_FLOOD_WAIT_SECONDS
    return min(seconds + random.uniform(1, _FLOOD_WAIT_JITTER_SECONDS), _MAX_FLOOD_WAIT_SECONDS)


def _build_client():
    """Construct the Telethon client. Separated so the retry loop is testable without it."""
    from telethon import TelegramClient

    client = TelegramClient(
        "signal_engine/data/telegram",
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    client.flood_sleep_threshold = _FLOOD_SLEEP_THRESHOLD
    return client


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


def _record_phase_decline(text: str, source: str, reason: str) -> None:
    """Write a DECLINED row for a signal refused before it ever reached the pipeline.

    A signal that vanishes with nothing in the ledger is unauditable — which is exactly the
    gap save_declined() exists to close for every other refusal stage. Parsing can fail (the
    message may not be a signal at all); that is not an error here, just nothing to record.
    """
    try:
        from signal_engine.db import save_declined
        from signal_engine.models import Direction
        from signal_engine.parser import parse

        signal = parse(text)
        if signal is None or signal.direction not in (Direction.LONG, Direction.SHORT):
            return
        save_declined(signal, stage="channel_phase", reason=f"[{source}] {reason}")
    except Exception as e:
        logger.warning(f"Could not record declined signal from [{source}]: {e}")


async def _phase_mismatch(channel_phase: str | None) -> str | None:
    """The refusal reason if this channel's phase does not match OpenAlgo's mode, else None.

    Checked PER MESSAGE, not at subscribe time: subscriptions are fixed once the client
    connects, so a subscribe-time check would miss a mid-session flip entirely.
    """
    if channel_phase is None:
        return None
    running = await mode_guard.current_phase()
    if channel_phase == running:
        return None
    return (
        f"channel is a {channel_phase.upper()} channel but OpenAlgo is in {running.upper()} "
        f"mode — refusing. Point this strategy's alert at its -{running} channel and enable "
        f"that channel in config.yaml, or put OpenAlgo back into {channel_phase.upper()}."
    )


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
            # WARNING, not DEBUG, and recorded: after the 2026-09-08 Telegram outage there
            # was no way to answer "what did we miss" — the drops left no trace at all.
            reason = f"stale by {stale_age:.0f}s (limit {settings.stale_signal_seconds}s)"
            logger.warning(f"[{source}] Skipping stale message: {reason}")
            _record_phase_decline(msg.text, source, reason)
            return

        # Gate 2 of config.yaml's two-gate rule. Gate 1 is `enabled:` (see
        # _split_by_enabled); this one is the half that used not to exist.
        reason = await _phase_mismatch(_channel_phase(source))
        if reason is not None:
            logger.critical(f"[{source}] SIGNAL REFUSED: {reason}")
            _record_phase_decline(msg.text, source, reason)
            await _notify_phase_mismatch(source, reason)
            return

        clean_text = " | ".join(line.strip() for line in msg.text.strip().splitlines() if line.strip())
        logger.info(f"[{source}] Signal received: {clean_text}")
        await on_message(msg.text)

    return handler


#: Channels already alerted about a phase mismatch this session. A mode mismatch affects
#: every message from that channel for as long as it lasts; one alert per channel says what
#: the operator needs to know without turning a config mistake into a message storm.
_phase_alerted: set = set()


async def _notify_phase_mismatch(source: str, reason: str) -> None:
    if source in _phase_alerted:
        return
    _phase_alerted.add(source)
    try:
        await notifier.notify_event(
            "channel_phase_mismatch",
            f"SIGNALS REFUSED from {source}\n{reason}",
        )
    except Exception as e:
        logger.warning(f"Could not send phase-mismatch alert for [{source}]: {e}")


async def _keepalive(client) -> None:
    """Periodically check the connection is still up, WITHOUT making an API call.

    On a dropped connection, disconnect so run_until_disconnected returns and triggers a
    retry. See _KEEPALIVE_INTERVAL for why this no longer calls get_me().
    """
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL)
        try:
            if client.is_connected():
                logger.debug("Keepalive: connection up")
                continue
            logger.warning("Keepalive: client reports disconnected — forcing a reconnect")
            await client.disconnect()
            return
        except Exception as e:
            logger.warning(f"Keepalive check failed: {e} — connection may be stale")
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

    # Checked on every startup, but only actually (re)pins a channel whose card content is new
    # or changed since the last successful pin - see strategy_cards.py's module docstring.
    # Includes BTST's channels even though they're outside `channels:` above (never subscribed
    # to - BTST is a manual daily call, not auto-traded) because they still need the same
    # ANALYZE/LIVE reference card as everything else.
    try:
        from signal_engine import strategy_cards

        card_channels = list(watching) + list(settings.breakingtrade_btst_channels.values())
        pinned = await strategy_cards.send_and_pin_cards(client, card_channels)
        if pinned:
            logger.info(f"Strategy reference cards (re)pinned: {pinned}")
    except Exception as e:
        logger.warning(f"Strategy card refresh failed (non-fatal): {e}")


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
    from telethon import events

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

    client = _build_client()
    chat_ids = [ch.id for ch in watching]
    channel_names = _channel_names()
    client.on(events.NewMessage(chats=chat_ids))(
        _make_handler(on_message, channel_names)
    )

    flood_error = _flood_wait_error()
    retries = 0
    floods = 0
    while retries < settings.listener_max_retries and floods < _MAX_CONSECUTIVE_FLOOD_WAITS:
        try:
            await _connect(client)
            # Only a successful connect clears either counter.
            retries = 0
            floods = 0
            await _serve(client)
        except flood_error as e:
            floods += 1
            wait = _flood_wait_seconds(e)
            logger.warning(
                f"Telegram FLOOD_WAIT: {e}. Honouring the stated wait — sleeping {wait:.0f}s "
                f"(flood {floods}/{_MAX_CONSECUTIVE_FLOOD_WAITS}). This does not consume the "
                "reconnect budget."
            )
            await asyncio.sleep(wait)
        except Exception as e:
            retries += 1
            wait = settings.listener_base_backoff * (2 ** (retries - 1))
            logger.error(
                f"Telegram disconnected: {e}. "
                f"Retry {retries}/{settings.listener_max_retries} in {wait}s"
            )
            await asyncio.sleep(wait)

    logger.critical(
        "Telegram listener giving up "
        f"(retries={retries}/{settings.listener_max_retries}, "
        f"floods={floods}/{_MAX_CONSECUTIVE_FLOOD_WAITS}). No NEW signals will be received. "
        "The tracker and time exit keep running so open positions stay managed — see "
        "startup._serve_until_shutdown()."
    )
