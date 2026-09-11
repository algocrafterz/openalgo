"""Integration tests for Telegram channel connectivity.

These tests require a valid Telegram session and connect to the real Telegram API.
The session is created on first engine start (`uv run python -m signal_engine.main`),
which prompts for the login code and writes signal_engine/data/telegram.session.

Skip with: pytest -m "not integration"
"""

import os
import shutil
import tempfile

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
    skip_reason = (
        "No Telegram session file. Start the engine once to create it: "
        "PYTHONPATH=. uv run python -m signal_engine.main"
    )
elif not _has_channels:
    skip_reason = "No channels configured in config.yaml"

if skip_reason:
    pytestmark = [pytestmark, pytest.mark.skip(reason=skip_reason)]


#: Copies of the live session, one per test process, cleaned up on exit.
_session_copies = []


def _session_copy() -> str:
    """A private COPY of the live session file, so these tests never open the one the running
    engine holds.

    Opening it directly meant Telethon's SQLite session raced the engine's own handle:
    `sqlite3.OperationalError: database is locked` whenever `python -m signal_engine.main`
    was up, so all four tests failed in a full run and passed in isolation — indistinguishable
    from a real regression, every time. It is also exactly what alerts.py's docstring warns
    about ("Two processes sharing one Telethon session file is a good way to corrupt it").

    A copy is read-only in effect: these tests only connect and read, so nothing the copy
    records needs to survive.
    """
    source = os.path.join(os.path.dirname(__file__), "..", "data", "telegram.session")
    handle, path = tempfile.mkstemp(prefix="telegram_test_", suffix=".session")
    os.close(handle)
    shutil.copyfile(source, path)
    _session_copies.append(path)
    return path[: -len(".session")]


@pytest.fixture(scope="module", autouse=True)
def _cleanup_session_copies():
    yield
    for path in _session_copies:
        try:
            os.remove(path)
        except OSError:
            pass


async def _get_client():
    c = TelegramClient(_session_copy(), settings.telegram_api_id, settings.telegram_api_hash)
    await c.connect()
    return c


class TestTelegramConnection:
    @pytest.mark.asyncio
    async def test_client_authorized(self):
        client = await _get_client()
        try:
            assert await client.is_user_authorized(), (
                "Session expired. Delete signal_engine/data/telegram.session and restart the engine "
                "to re-authenticate."
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
