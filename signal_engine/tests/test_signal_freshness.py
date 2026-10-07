"""Stale signals must never become trades, whatever the reason they are late.

The Telegram handler already drops a message older than stale_signal_seconds. That check is
tied to Telegram: it cannot catch a signal that was fresh on arrival and then waited (a slow
API call, a lock, a feed that returned and delivered a backlog). The freshness rule is
therefore source-independent: the origin time travels with the signal into dispatch_signal,
which re-checks every ENTRY. EXITs are never refused for age - closing risk must stay possible.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import freshness, listener, main
from signal_engine.config import settings
from signal_engine.tests.conftest import make_signal as _make_signal

LIMIT = settings.stale_signal_seconds


def _ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


class TestStaleAge:
    def test_fresh_is_none(self):
        assert freshness.stale_age(_ago(1), LIMIT) is None

    def test_old_returns_its_age(self):
        age = freshness.stale_age(_ago(LIMIT + 600), LIMIT)
        assert age is not None and age > LIMIT

    def test_unknown_origin_is_not_judged(self):
        assert freshness.stale_age(None, LIMIT) is None

    def test_naive_datetime_is_read_as_utc(self):
        naive = (datetime.now(UTC) - timedelta(seconds=LIMIT + 600)).replace(tzinfo=None)
        assert freshness.stale_age(naive, LIMIT) is not None


class TestOriginScope:
    def test_origin_is_visible_inside_and_cleared_after(self):
        origin = _ago(5)
        assert freshness.current_origin() is None
        with freshness.origin_scope(origin):
            assert freshness.current_origin() == origin
        assert freshness.current_origin() is None


class TestOriginScopeIsolation:
    def test_the_previous_value_is_restored_after_an_exception(self):
        outer = _ago(5)
        with freshness.origin_scope(outer):
            with pytest.raises(RuntimeError):
                with freshness.origin_scope(_ago(9999)):
                    raise RuntimeError("pipeline blew up")
            assert freshness.current_origin() == outer
        assert freshness.current_origin() is None

    @pytest.mark.asyncio
    async def test_concurrent_signals_never_see_each_others_origin(self):
        import asyncio

        seen = {}

        async def process(name, origin, delay):
            with freshness.origin_scope(origin):
                await asyncio.sleep(delay)
                seen[name] = freshness.current_origin()

        old, new = _ago(9999), _ago(1)
        await asyncio.gather(process("old", old, 0.05), process("new", new, 0.01))

        assert seen == {"old": old, "new": new}
        assert freshness.current_origin() is None


class TestDispatchRefusesStaleEntries:
    @pytest.mark.asyncio
    async def test_a_stale_entry_never_reaches_the_entry_path(self):
        with patch.object(main, "_decline", AsyncMock()) as decline, \
             patch.object(main, "_handle_entry", AsyncMock()) as entry, \
             patch.object(main, "validate") as validate:
            with freshness.origin_scope(_ago(LIMIT + 3600)):
                await main.dispatch_signal(_make_signal())
        entry.assert_not_awaited()
        validate.assert_not_called()
        assert decline.await_args.kwargs["stage"] == "stale"

    @pytest.mark.asyncio
    async def test_a_fresh_entry_proceeds(self):
        valid = MagicMock(status=main.ValidationStatus.VALID)
        with patch.object(main, "_decline", AsyncMock()) as decline, \
             patch.object(main, "_handle_entry", AsyncMock()) as entry, \
             patch.object(main, "validate", return_value=valid):
            with freshness.origin_scope(_ago(1)):
                await main.dispatch_signal(_make_signal())
        entry.assert_awaited_once()
        decline.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_source_with_no_origin_time_is_not_blocked(self):
        """The Python runner passes real Signal objects with no Telegram timestamp; it has its
        own bar-close staleness check and must not be affected."""
        valid = MagicMock(status=main.ValidationStatus.VALID)
        with patch.object(main, "_handle_entry", AsyncMock()) as entry, \
             patch.object(main, "validate", return_value=valid):
            await main.dispatch_signal(_make_signal())
        entry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_stale_exit_is_still_processed(self):
        valid = MagicMock(status=main.ValidationStatus.VALID)
        with patch.object(main, "_handle_exit", AsyncMock()) as exit_, \
             patch.object(main, "_decline", AsyncMock()) as decline, \
             patch.object(main, "validate", return_value=valid):
            with freshness.origin_scope(_ago(LIMIT + 3600)):
                await main.dispatch_signal(_make_signal(direction=main.Direction.EXIT))
        exit_.assert_awaited_once()
        decline.assert_not_awaited()


class TestListenerStampsTheOrigin:
    @pytest.mark.asyncio
    async def test_on_message_runs_inside_the_origin_scope(self):
        seen = {}

        async def on_message(_text):
            seen["origin"] = freshness.current_origin()

        event = MagicMock()
        event.message.text = "ORB LONG SBIN entry=800 sl=790 tp=820"
        event.message.date = (datetime.now(UTC) - timedelta(seconds=2)).replace(tzinfo=None)
        event.chat_id = -1
        with patch.object(listener, "_phase_mismatch", AsyncMock(return_value=None)):
            await listener._make_handler(on_message, {})(event)

        assert seen["origin"] is not None
        assert freshness.current_origin() is None


def test_the_listener_never_asks_telegram_to_replay_missed_updates():
    """Telethon only replays what was missed while disconnected if catch_up() is called.
    There must be no implicit backlog that survives an outage and drains after reconnect."""
    import inspect

    assert "catch_up" not in inspect.getsource(listener)
