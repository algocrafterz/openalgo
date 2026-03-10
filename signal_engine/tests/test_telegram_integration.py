"""Integration tests for Telegram channel connectivity.

These tests require a valid Telegram session (run test_telegram.py first).
They connect to the real Telegram API and verify channel access.

Skip with: pytest -m "not integration"
"""

import os

import pytest
from telethon import TelegramClient
from telethon.tl.types import Channel

from signal_engine.config import settings

pytestmark = pytest.mark.integration

# Skip entire module if credentials are missing or no session exists
_SESSION_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "telegram.session")
_has_session = os.path.exists(_SESSION_PATH)
_has_credentials = bool(settings.telegram_api_id and settings.telegram_api_hash)
_has_channels = bool(settings.telegram_channels)

skip_reason = None
if not _has_credentials:
    skip_reason = "Telegram credentials not configured in .env"
elif not _has_session:
    skip_reason = "No Telegram session file. Run: PYTHONPATH=. uv run python signal_engine/test_telegram.py"
elif not _has_channels:
    skip_reason = "No channels configured in config.yaml"

if skip_reason:
    pytestmark = [pytestmark, pytest.mark.skip(reason=skip_reason)]


async def _get_client():
    session_path = os.path.join(os.path.dirname(__file__), "..", "data", "telegram")
    c = TelegramClient(session_path, settings.telegram_api_id, settings.telegram_api_hash)
    await c.connect()
    return c


class TestTelegramConnection:
    @pytest.mark.asyncio
    async def test_client_authorized(self):
        client = await _get_client()
        try:
            assert await client.is_user_authorized(), (
                "Session expired. Re-run: PYTHONPATH=. uv run python signal_engine/test_telegram.py"
            )
            me = await client.get_me()
            assert me is not None
            assert me.phone is not None
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_configured_channels_accessible(self):
        """Verify every channel in config.yaml is accessible."""
        client = await _get_client()
        try:
            for ch in settings.telegram_channels:
                entity = await client.get_entity(ch.id)
                assert entity is not None, f"Cannot access channel: {ch.name} ({ch.id})"
                assert isinstance(entity, Channel), (
                    f"Entity {ch.name} ({ch.id}) is not a Channel, got {type(entity).__name__}"
                )
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_can_read_recent_messages(self):
        """Verify we can read messages from each configured channel."""
        client = await _get_client()
        try:
            for ch in settings.telegram_channels:
                entity = await client.get_entity(ch.id)
                messages = await client.get_messages(entity, limit=5)
                assert isinstance(messages, list), (
                    f"Failed to fetch messages from {ch.name} ({ch.id})"
                )
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_channel_names_match_config(self):
        """Log if the Telegram channel title differs from the configured name."""
        client = await _get_client()
        try:
            for ch in settings.telegram_channels:
                entity = await client.get_entity(ch.id)
                if hasattr(entity, "title") and entity.title != ch.name:
                    print(
                        f"  Note: config name '{ch.name}' differs from "
                        f"Telegram title '{entity.title}' for {ch.id}"
                    )
        finally:
            await client.disconnect()
