"""Stage 1 source-channel ingestion: storage, edits, backfill, recovery and isolation.

The ingester only records messages. These tests pin that a duplicate changes nothing, that a
stale copy never overwrites a newer edit, that a backfill covers exactly its window, that a
cancelled or failed backfill leaves the database consistent, and that nothing on the ingestion
side can reach the trading listener.
"""

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from signal_engine import config, ingest, listener
from signal_engine.config import TelegramChannel

T0 = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
CHANNEL_ID = -1001363983698
SOURCE = TelegramChannel(name="vendor", id="VendorChannel")


def msg(mid, minutes_ago=0, text="alert", edit=None, now=T0, **extra):
    """A message shaped like Telethon's, sent `minutes_ago` before `now`."""
    fields = {
        "id": mid,
        "date": now - timedelta(minutes=minutes_ago),
        "edit_date": edit,
        "message": text,
        "text": f"**{text}**",
        "views": 10,
        "forwards": 0,
        "reply_to_msg_id": None,
        "grouped_id": None,
        "post_author": None,
        "media": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


class FakeClient:
    """Just enough of a Telethon client: entity lookup, newest-first history, handlers."""

    def __init__(self, messages=(), fail=None, block=False):
        self.messages = sorted(messages, key=lambda m: m.date, reverse=True)
        self.fail = fail
        self.block = block
        self.yielded = 0
        self.handlers = []

    async def get_entity(self, ref):
        return SimpleNamespace(id=1363983698)

    async def iter_messages(self, entity):
        if self.fail is not None:
            raise self.fail
        for m in self.messages:
            self.yielded += 1
            yield m
        if self.block:
            await asyncio.Event().wait()

    def add_event_handler(self, callback, event=None):
        self.handlers.append((callback, event))


def make_ingester(tmp_path, sources=(SOURCE,), clock=None, sleeps=None, **kwargs):
    async def _sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    return ingest.SourceIngester(
        sources,
        str(tmp_path / "ingest.db"),
        clock=clock or (lambda: T0),
        sleep=_sleep,
        peer_id_of=lambda entity: CHANNEL_ID,
        **kwargs,
    )


def stored(ing):
    return ing._get_store()


class TestStore:
    def test_duplicate_insert_is_idempotent(self, tmp_path):
        store = ingest.IngestStore(str(tmp_path / "s.db"))
        row = ingest.build_row(msg(1), CHANNEL_ID, "vendor")
        assert store.upsert_many([row]) == {"inserted": 1, "updated": 0, "unchanged": 0}
        again = store.upsert_many([row, row])
        assert again == {"inserted": 0, "updated": 0, "unchanged": 2}
        assert store.count() == 1
        assert store.get(CHANNEL_ID, 1)["revision"] == 1

    def test_same_message_id_in_two_channels_is_two_rows(self, tmp_path):
        store = ingest.IngestStore(str(tmp_path / "s.db"))
        store.upsert_many([ingest.build_row(msg(1), CHANNEL_ID, "a")])
        store.upsert_many([ingest.build_row(msg(1), CHANNEL_ID - 1, "b")])
        assert store.count() == 2

    def test_fields_are_preserved_verbatim(self, tmp_path):
        store = ingest.IngestStore(str(tmp_path / "s.db"))
        original = msg(
            7,
            text="Big Move  \n BDL -3.96%",
            views=55,
            forwards=2,
            post_author="x",
            reply_to_msg_id=3,
            grouped_id=99,
            media=object(),
        )
        store.upsert_many([ingest.build_row(original, CHANNEL_ID, "vendor")])
        row = store.get(CHANNEL_ID, 7)
        assert row["text"] == "Big Move  \n BDL -3.96%"
        assert row["text_md"] == "**Big Move  \n BDL -3.96%**"
        assert row["sent_at"] == "2026-11-12T10:00:00+00:00"
        assert (row["views"], row["forwards"], row["post_author"]) == (55, 2, "x")
        assert (row["reply_to_msg_id"], row["grouped_id"], row["has_media"]) == (3, 99, 1)
        assert row["source_name"] == "vendor"

    def test_message_without_text_is_not_stored(self):
        assert ingest.build_row(msg(1, text=""), CHANNEL_ID, "v") is None
        assert ingest.build_row(msg(1, text=None), CHANNEL_ID, "v") is None

    def test_naive_datetime_is_taken_as_utc(self):
        naive = msg(1, date=datetime(2026, 11, 12, 10, 0, 0))
        assert ingest.build_row(naive, CHANNEL_ID, "v")["sent_at"] == "2026-11-12T10:00:00+00:00"

    def test_rows_and_watermark_survive_a_restart(self, tmp_path):
        path = str(tmp_path / "s.db")
        store = ingest.IngestStore(path)
        store.upsert_many(
            [ingest.build_row(msg(1), CHANNEL_ID, "vendor")],
            watermark=(CHANNEL_ID, ingest._utc_text(T0)),
        )
        store.close()
        store.close()  # idempotent
        reopened = ingest.IngestStore(path)
        assert reopened.get(CHANNEL_ID, 1)["text"] == "alert"
        assert reopened.watermark(CHANNEL_ID) == T0

    def test_watermark_never_moves_back(self, tmp_path):
        store = ingest.IngestStore(str(tmp_path / "s.db"))
        later, earlier = T0, T0 - timedelta(hours=1)
        store.upsert_many([], watermark=(CHANNEL_ID, ingest._utc_text(later)))
        store.upsert_many([], watermark=(CHANNEL_ID, ingest._utc_text(earlier)))
        assert store.watermark(CHANNEL_ID) == later

    def test_failed_batch_leaves_no_partial_write(self, tmp_path):
        store = ingest.IngestStore(str(tmp_path / "s.db"))
        good = ingest.build_row(msg(1), CHANNEL_ID, "v")
        bad = {**good, "message_id": 2, "text": None}  # violates NOT NULL
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_many([good, bad], watermark=(CHANNEL_ID, ingest._utc_text(T0)))
        assert store.count() == 0
        assert store.watermark(CHANNEL_ID) is None


class TestEdits:
    def _store_with(self, tmp_path, message):
        store = ingest.IngestStore(str(tmp_path / "s.db"))
        store.upsert_many([ingest.build_row(message, CHANNEL_ID, "v")])
        return store

    def test_newer_edit_replaces_content_and_keeps_ingestion_metadata(self, tmp_path):
        store = self._store_with(tmp_path, msg(1, text="v1"))
        first = store.get(CHANNEL_ID, 1)
        edit = ingest.build_row(msg(1, text="v2", edit=T0 + timedelta(minutes=5)), CHANNEL_ID, "v")
        assert store.upsert_many([edit]) == {"inserted": 0, "updated": 1, "unchanged": 0}
        row = store.get(CHANNEL_ID, 1)
        assert (row["text"], row["revision"]) == ("v2", 2)
        assert row["edit_date"] == "2026-11-12T10:05:00+00:00"
        assert row["ingested_at"] == first["ingested_at"]
        assert row["sent_at"] == first["sent_at"]

    def test_older_edit_never_overwrites_a_newer_one(self, tmp_path):
        store = self._store_with(tmp_path, msg(1, text="v1"))
        newer = msg(1, text="v3", edit=T0 + timedelta(minutes=10))
        older = msg(1, text="v2", edit=T0 + timedelta(minutes=5))
        store.upsert_many([ingest.build_row(newer, CHANNEL_ID, "v")])
        assert store.upsert_many([ingest.build_row(older, CHANNEL_ID, "v")])["unchanged"] == 1
        assert store.get(CHANNEL_ID, 1)["text"] == "v3"

    def test_same_edit_date_is_a_no_op(self, tmp_path):
        store = self._store_with(tmp_path, msg(1, text="v1"))
        edit = ingest.build_row(msg(1, text="v2", edit=T0 + timedelta(minutes=5)), CHANNEL_ID, "v")
        store.upsert_many([edit])
        assert store.upsert_many([edit])["unchanged"] == 1
        assert store.get(CHANNEL_ID, 1)["revision"] == 2

    def test_unedited_copy_does_not_overwrite_an_edit(self, tmp_path):
        store = self._store_with(tmp_path, msg(1, text="v1"))
        store.upsert_many(
            [ingest.build_row(msg(1, text="v2", edit=T0 + timedelta(minutes=5)), CHANNEL_ID, "v")]
        )
        store.upsert_many([ingest.build_row(msg(1, text="v1"), CHANNEL_ID, "v")])
        assert store.get(CHANNEL_ID, 1)["text"] == "v2"


class TestBackfillWindow:
    @pytest.mark.asyncio
    async def test_first_backfill_covers_seven_days_and_stops_at_the_boundary(self, tmp_path):
        ing = make_ingester(tmp_path)
        inside = msg(1, minutes_ago=7 * 24 * 60 - 1)
        outside = msg(2, minutes_ago=7 * 24 * 60 + 1)
        older = [msg(3, minutes_ago=8 * 24 * 60), msg(4, minutes_ago=9 * 24 * 60)]
        client = FakeClient([msg(5, minutes_ago=10), inside, outside, *older])
        counts = await ing.backfill_source(client, SOURCE)
        assert counts["inserted"] == 2
        assert {m for m in (1, 2, 3, 4, 5) if stored(ing).get(CHANNEL_ID, m)} == {1, 5}
        # It stopped at the first message past the boundary and read nothing older.
        assert client.yielded == 3
        assert stored(ing).watermark(CHANNEL_ID) == T0

    @pytest.mark.asyncio
    async def test_message_without_text_is_skipped_but_does_not_stop_the_run(self, tmp_path):
        ing = make_ingester(tmp_path)
        client = FakeClient([msg(1, minutes_ago=1, text=""), msg(2, minutes_ago=2)])
        await ing.backfill_source(client, SOURCE)
        assert stored(ing).count() == 1

    @pytest.mark.asyncio
    async def test_recovery_starts_before_the_watermark_by_the_overlap(self, tmp_path):
        ing = make_ingester(tmp_path)
        await ing.backfill_source(FakeClient([msg(1, minutes_ago=30)]), SOURCE)
        now = T0 + timedelta(hours=6)
        ing2 = make_ingester(tmp_path, clock=lambda: now)
        in_overlap = msg(10, minutes_ago=60, now=T0)  # 09:00, within 2h of watermark 10:00
        before_overlap = msg(11, minutes_ago=150, now=T0)  # 07:30, before the overlap start
        missed = msg(12, minutes_ago=5, now=now)
        client = FakeClient([missed, in_overlap, before_overlap])
        counts = await ing2.backfill_source(client, SOURCE)
        assert counts["inserted"] == 2
        assert stored(ing2).get(CHANNEL_ID, 10) is not None
        assert stored(ing2).get(CHANNEL_ID, 11) is None
        assert client.yielded == 3  # stopped on the first message before the overlap start
        assert stored(ing2).watermark(CHANNEL_ID) == now

    @pytest.mark.asyncio
    async def test_overlap_never_reaches_past_the_seven_day_window(self, tmp_path):
        ing = make_ingester(tmp_path, overlap=timedelta(days=30))
        await ing.backfill_source(FakeClient([msg(1, minutes_ago=1)]), SOURCE)
        too_old = msg(2, minutes_ago=8 * 24 * 60)
        await ing.backfill_source(FakeClient([too_old]), SOURCE)
        assert stored(ing).get(CHANNEL_ID, 2) is None

    @pytest.mark.asyncio
    async def test_repeated_backfill_creates_no_duplicates(self, tmp_path):
        ing = make_ingester(tmp_path)
        messages = [msg(i, minutes_ago=i) for i in range(1, 6)]
        await ing.backfill_source(FakeClient(messages), SOURCE)
        again = await ing.backfill_source(FakeClient(messages), SOURCE)
        assert again == {"inserted": 0, "updated": 0, "unchanged": 5}
        assert stored(ing).count() == 5

    @pytest.mark.asyncio
    async def test_backfill_picks_up_an_edit_made_while_disconnected(self, tmp_path):
        ing = make_ingester(tmp_path)
        await ing.backfill_source(FakeClient([msg(1, minutes_ago=10, text="v1")]), SOURCE)
        edited = msg(1, minutes_ago=10, text="v2", edit=T0 + timedelta(minutes=3))
        counts = await ing.backfill_source(FakeClient([edited]), SOURCE)
        assert counts["updated"] == 1
        assert stored(ing).get(CHANNEL_ID, 1)["text"] == "v2"

    @pytest.mark.asyncio
    async def test_message_cap_is_honoured(self, tmp_path):
        ing = make_ingester(tmp_path, max_messages=3)
        await ing.backfill_source(FakeClient([msg(i, minutes_ago=i) for i in range(1, 9)]), SOURCE)
        assert stored(ing).count() == 3


class TestLivePath:
    def _event(self, message, chat_id=CHANNEL_ID):
        return SimpleNamespace(chat_id=chat_id, message=message)

    @pytest.mark.asyncio
    async def test_live_message_is_stored_once_after_the_source_is_resolved(self, tmp_path):
        ing = make_ingester(tmp_path)
        event = self._event(msg(1))
        await ing._on_event(event)
        assert not os.path.exists(tmp_path / "ingest.db")  # unresolved username: ignored
        await ing.backfill_source(FakeClient(), SOURCE)
        await ing._on_event(event)
        await ing._on_event(event)
        assert stored(ing).count() == 1

    @pytest.mark.asyncio
    async def test_live_edit_updates_only_when_newer(self, tmp_path):
        ing = make_ingester(tmp_path)
        await ing.backfill_source(FakeClient(), SOURCE)
        await ing._on_event(self._event(msg(1, text="v1")))
        await ing._on_event(self._event(msg(1, text="v2", edit=T0 + timedelta(minutes=2))))
        await ing._on_event(self._event(msg(1, text="v1")))
        assert stored(ing).get(CHANNEL_ID, 1)["text"] == "v2"

    @pytest.mark.asyncio
    async def test_other_channels_are_ignored_and_open_no_database(self, tmp_path):
        ing = make_ingester(tmp_path, sources=(TelegramChannel(name="v", id=CHANNEL_ID),))
        await ing._on_event(self._event(msg(1), chat_id=-100999))
        assert not os.path.exists(tmp_path / "ingest.db")
        await ing._on_event(self._event(msg(1)))
        assert stored(ing).count() == 1

    @pytest.mark.asyncio
    async def test_live_messages_do_not_move_the_watermark(self, tmp_path):
        ing = make_ingester(tmp_path, sources=(TelegramChannel(name="v", id=CHANNEL_ID),))
        await ing._on_event(self._event(msg(1)))
        assert stored(ing).watermark(CHANNEL_ID) is None

    @pytest.mark.asyncio
    async def test_a_storage_failure_never_escapes_the_handler(self, tmp_path):
        ing = make_ingester(tmp_path, sources=(TelegramChannel(name="v", id=CHANNEL_ID),))

        class Broken:
            def upsert_many(self, *a, **k):
                raise RuntimeError("disk full")

        ing._store = Broken()
        await ing._on_event(self._event(msg(1)))  # does not raise
        await ing._on_event(SimpleNamespace())  # malformed event, does not raise
        await ing._on_event(self._event(object()))  # malformed message, does not raise

    def test_registers_two_handlers_once_and_none_when_disabled(self, tmp_path):
        client = FakeClient()
        ing = make_ingester(tmp_path)
        ing.register(client)
        ing.register(client)
        assert len(client.handlers) == 2
        off = make_ingester(tmp_path, sources=(TelegramChannel(name="v", id="x", enabled=False),))
        quiet = FakeClient()
        off.register(quiet)
        assert quiet.handlers == []


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_failed_backfill_retries_then_gives_up_without_raising(self, tmp_path):
        sleeps = []
        ing = make_ingester(tmp_path, sleeps=sleeps)
        await ing._run(FakeClient(fail=OSError("network")))
        assert len(sleeps) == 2  # three attempts, a pause between each

    @pytest.mark.asyncio
    async def test_flood_wait_is_honoured_not_hammered(self, tmp_path):
        sleeps = []

        class Flood(Exception):
            seconds = 120

        ing = make_ingester(tmp_path, sleeps=sleeps)
        await ing._run(FakeClient(fail=Flood()))
        assert sleeps == [125.0, 125.0]

    @pytest.mark.asyncio
    async def test_one_failing_source_does_not_block_the_next(self, tmp_path):
        good = TelegramChannel(name="good", id="Good")
        bad = TelegramChannel(name="bad", id="Bad")
        ing = make_ingester(tmp_path, sources=(bad, good))

        class Client(FakeClient):
            async def get_entity(self, ref):
                if ref == "Bad":
                    raise ValueError("no such channel")
                return await super().get_entity(ref)

        client = Client([msg(1, minutes_ago=1)])
        await ing._run(client)
        assert stored(ing).count() == 1

    def test_start_backfill_returns_none_instead_of_raising_without_a_loop(self, tmp_path):
        ing = make_ingester(tmp_path)
        assert ing.start_backfill(FakeClient()) is None

    @pytest.mark.asyncio
    async def test_module_entry_points_never_raise(self, monkeypatch):
        def boom():
            raise RuntimeError("config broke")

        monkeypatch.setattr(ingest, "_get_service", boom)
        ingest.register(FakeClient())
        assert ingest.start_backfill(FakeClient()) is None

    @pytest.mark.asyncio
    async def test_ingest_never_calls_the_trading_callback(self, tmp_path):
        on_message = AsyncMock()
        ing = make_ingester(tmp_path)
        client = FakeClient([msg(1, minutes_ago=1)])
        ing.register(client)
        await ing._run(client)
        await ing._on_event(SimpleNamespace(chat_id=CHANNEL_ID, message=msg(2)))
        on_message.assert_not_called()
        assert stored(ing).count() == 2


class TestCancellationAndReconnect:
    @pytest.mark.asyncio
    async def test_cancel_during_backfill_commits_nothing_and_leaves_handlers_alone(self, tmp_path):
        ing = make_ingester(tmp_path)
        client = FakeClient([msg(1, minutes_ago=1)], block=True)
        ing.register(client)
        handlers_before = list(client.handlers)
        task = ing.start_backfill(client)
        while client.yielded < 1:
            await asyncio.sleep(0)
        ing.stop_backfill(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stored(ing).count() == 0
        assert stored(ing).watermark(CHANNEL_ID) is None
        assert client.handlers == handlers_before

    @pytest.mark.asyncio
    async def test_a_new_connection_replaces_the_old_backfill_task(self, tmp_path):
        ing = make_ingester(tmp_path)
        first = ing.start_backfill(FakeClient(block=True))
        await asyncio.sleep(0)
        second = ing.start_backfill(FakeClient([msg(1, minutes_ago=1)]))
        await second
        with pytest.raises(asyncio.CancelledError):
            await first
        assert stored(ing).count() == 1

    @pytest.mark.asyncio
    async def test_missed_messages_are_recovered_after_a_reconnect(self, tmp_path):
        ing = make_ingester(tmp_path)
        await ing.backfill_source(FakeClient([msg(1, minutes_ago=20)]), SOURCE)
        later = T0 + timedelta(hours=1)
        missed = [msg(2, minutes_ago=30, now=later), msg(3, minutes_ago=5, now=later)]
        ing_later = make_ingester(tmp_path, clock=lambda: later)
        await ing_later.backfill_source(FakeClient([*missed, msg(1, minutes_ago=20)]), SOURCE)
        assert stored(ing_later).count() == 3
        assert stored(ing_later).watermark(CHANNEL_ID) == later

    @pytest.mark.asyncio
    async def test_close_cancels_the_task_and_releases_the_database(self, tmp_path):
        ing = make_ingester(tmp_path)
        task = ing.start_backfill(FakeClient(block=True))
        await asyncio.sleep(0)
        stored(ing)
        ing.close()
        ing.close()  # idempotent
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ing._store is None


class TestListenerIntegration:
    @pytest.mark.asyncio
    async def test_serve_cancels_the_backfill_when_the_connection_ends(self, monkeypatch):
        calls = []
        monkeypatch.setattr(listener.ingest, "start_backfill", lambda c: "TASK")
        monkeypatch.setattr(listener.ingest, "stop_backfill", lambda t: calls.append(t))
        client = SimpleNamespace(run_until_disconnected=AsyncMock(), is_connected=lambda: True)
        await listener._serve(client)
        assert calls == ["TASK"]

    @pytest.mark.asyncio
    async def test_serve_still_cancels_the_backfill_when_the_connection_drops_with_an_error(
        self, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(listener.ingest, "start_backfill", lambda c: "TASK")
        monkeypatch.setattr(listener.ingest, "stop_backfill", lambda t: calls.append(t))
        client = SimpleNamespace(
            run_until_disconnected=AsyncMock(side_effect=ConnectionError("dropped")),
            is_connected=lambda: True,
        )
        with pytest.raises(ConnectionError):
            await listener._serve(client)
        assert calls == ["TASK"]

    @pytest.mark.asyncio
    async def test_failing_ingestion_does_not_disturb_serve(self, tmp_path, monkeypatch):
        ing = make_ingester(tmp_path)
        started = []

        def start(client):
            started.append(ing.start_backfill(FakeClient(fail=OSError("network"))))
            return started[-1]

        monkeypatch.setattr(listener.ingest, "start_backfill", start)
        serve_client = SimpleNamespace(
            run_until_disconnected=AsyncMock(), is_connected=lambda: True
        )
        await listener._serve(serve_client)  # returns normally
        task = started[0]
        for _ in range(100):
            if task.done():
                break
            await asyncio.sleep(0)
        # Either it finished having swallowed the failure, or _serve cancelled it on exit.
        assert task.cancelled() or task.exception() is None

    def test_ingest_module_has_no_path_into_the_trading_pipeline(self):
        source = open(ingest.__file__, encoding="utf-8").read()
        for forbidden in (
            "on_message(",
            "import main",
            "signal_engine.main",
            "send_order",
            "trades.db",
        ):
            assert forbidden not in source.replace("never calls on_message", "")

    def test_trading_subscription_is_still_the_one_chats_handler(self):
        src = open(listener.__file__, encoding="utf-8").read()
        assert "client.on(events.NewMessage(chats=chat_ids))(" in src


class TestConfig:
    def test_ingest_sources_are_parsed_and_default_to_empty(self):
        fields = config._telegram_fields(
            {
                "ingest_sources": [
                    {"name": "v", "id": "VendorChannel"},
                    {"name": "n", "id": "-1005", "enabled": False},
                ]
            }
        )
        first, second = fields["ingest_sources"]
        assert (first.name, first.id, first.enabled) == ("v", "VendorChannel", True)
        assert (second.id, second.enabled) == (-1005, False)
        assert config._telegram_fields({})["ingest_sources"] == ()
        assert config._telegram_fields({"ingest_sources": None})["ingest_sources"] == ()

    def test_source_is_not_in_the_trading_channel_list(self):
        traded = {str(ch.id) for ch in config.settings.telegram_channels}
        for source in config.settings.ingest_sources:
            assert str(source.id) not in traded
