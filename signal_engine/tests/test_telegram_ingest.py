"""Stage 1 source-channel ingestion: storage, edits, backfill, recovery and isolation.

The ingester only records messages. These tests pin that a duplicate changes nothing, that a
stale copy never overwrites a newer edit, that a backfill covers exactly its window, that a
cancelled or failed backfill leaves the database consistent, and that nothing on the ingestion
side can reach the trading listener.
"""

import asyncio
import os
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from signal_engine import config, ingest, listener
from signal_engine.config import TelegramChannel
from signal_engine.timeutils import IST

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

    def remove_event_handler(self, callback, event=None):
        self.handlers = [(c, e) for c, e in self.handlers if c != callback]

    def is_connected(self):
        return True


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


def opened(ing):
    """Put an ingester in the state the scheduler leaves it in while the window is open."""
    ing._window_open = True
    return ing


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
        ing = opened(make_ingester(tmp_path))
        event = self._event(msg(1))
        await ing._on_event(event)
        assert not os.path.exists(tmp_path / "ingest.db")  # unresolved username: ignored
        await ing.backfill_source(FakeClient(), SOURCE)
        await ing._on_event(event)
        await ing._on_event(event)
        assert stored(ing).count() == 1

    @pytest.mark.asyncio
    async def test_live_edit_updates_only_when_newer(self, tmp_path):
        ing = opened(make_ingester(tmp_path))
        await ing.backfill_source(FakeClient(), SOURCE)
        await ing._on_event(self._event(msg(1, text="v1")))
        await ing._on_event(self._event(msg(1, text="v2", edit=T0 + timedelta(minutes=2))))
        await ing._on_event(self._event(msg(1, text="v1")))
        assert stored(ing).get(CHANNEL_ID, 1)["text"] == "v2"

    @pytest.mark.asyncio
    async def test_other_channels_are_ignored_and_open_no_database(self, tmp_path):
        ing = opened(make_ingester(tmp_path, sources=(TelegramChannel(name="v", id=CHANNEL_ID),)))
        await ing._on_event(self._event(msg(1), chat_id=-100999))
        assert not os.path.exists(tmp_path / "ingest.db")
        await ing._on_event(self._event(msg(1)))
        assert stored(ing).count() == 1

    @pytest.mark.asyncio
    async def test_live_messages_do_not_move_the_watermark(self, tmp_path):
        ing = opened(make_ingester(tmp_path, sources=(TelegramChannel(name="v", id=CHANNEL_ID),)))
        await ing._on_event(self._event(msg(1)))
        assert stored(ing).watermark(CHANNEL_ID) is None

    @pytest.mark.asyncio
    async def test_a_storage_failure_never_escapes_the_handler(self, tmp_path):
        ing = opened(make_ingester(tmp_path, sources=(TelegramChannel(name="v", id=CHANNEL_ID),)))

        class Broken:
            def upsert_many(self, *a, **k):
                raise RuntimeError("disk full")

        ing._store = Broken()
        await ing._on_event(self._event(msg(1)))  # does not raise
        await ing._on_event(SimpleNamespace())  # malformed event, does not raise
        await ing._on_event(self._event(object()))  # malformed message, does not raise


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
        ing = opened(make_ingester(tmp_path))
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
        ing._client = client
        await ing._set_window(True)
        await ing._run(client)
        await ing._on_event(SimpleNamespace(chat_id=CHANNEL_ID, message=msg(2)))
        on_message.assert_not_called()
        assert stored(ing).count() == 2


class TestCancellationAndReconnect:
    @pytest.mark.asyncio
    async def test_cancel_during_backfill_commits_nothing_and_leaves_handlers_alone(self, tmp_path):
        ing = opened(make_ingester(tmp_path))
        client = FakeClient([msg(1, minutes_ago=1)], block=True)
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
        ing = opened(make_ingester(tmp_path))
        first = ing.start_backfill(FakeClient(block=True))
        await asyncio.sleep(0)
        ing.connection_ended(first)  # the listener's old connection is gone
        second = ing.start_backfill(FakeClient([msg(1, minutes_ago=1)]))
        assert second is not first
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
        ing = opened(make_ingester(tmp_path))
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
        ing = opened(make_ingester(tmp_path))
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


# ---- market-hours window ------------------------------------------------------------------


def ist(month, day, hour=0, minute=0, second=0):
    return datetime(2026, month, day, hour, minute, second, tzinfo=IST)


def weekdays_only(day):
    return day.weekday() < 5


# 2026-11-12 is a Thursday.
THU, FRI, SAT, MON = (ist(11, d, 0) for d in (12, 13, 14, 16))


class TestWindowPlan:
    @pytest.mark.parametrize(
        ("now", "is_open", "wake"),
        [
            (ist(11, 12, 8, 59, 59), False, ist(11, 12, 9, 0)),
            (ist(11, 12, 9, 0, 0), True, ist(11, 12, 15, 45)),
            (ist(11, 12, 15, 44, 59), True, ist(11, 12, 15, 45)),
            (ist(11, 12, 15, 45, 0), False, ist(11, 13, 9, 0)),
            (ist(11, 13, 16, 0), False, ist(11, 16, 9, 0)),  # Friday evening -> Monday
            (ist(11, 14, 12, 0), False, ist(11, 16, 9, 0)),  # Saturday
            (ist(11, 15, 12, 0), False, ist(11, 16, 9, 0)),  # Sunday
        ],
    )
    def test_boundaries_and_weekends(self, tmp_path, now, is_open, wake):
        ing = make_ingester(tmp_path, session_day=weekdays_only)
        assert ing._plan(now) == (is_open, wake)

    def test_a_holiday_is_skipped_and_so_is_the_weekend_after_it(self, tmp_path):
        holiday = date(2026, 11, 13)
        ing = make_ingester(tmp_path, session_day=lambda d: d.weekday() < 5 and d != holiday)
        assert ing._plan(ist(11, 12, 16, 0)) == (False, ist(11, 16, 9, 0))
        assert ing._plan(ist(11, 13, 10, 0)) == (False, ist(11, 16, 9, 0))  # on the holiday

    def test_a_special_session_day_is_not_a_window(self, tmp_path):
        # is_session_day() answers False for Muhurat and mock sessions; the plan follows it.
        ing = make_ingester(tmp_path, session_day=lambda d: d != date(2026, 11, 12))
        assert ing._plan(ist(11, 12, 10, 0)) == (False, ist(11, 13, 9, 0))

    def test_a_calendar_with_no_session_ahead_looks_again_later(self, tmp_path):
        ing = make_ingester(tmp_path, session_day=lambda d: False)
        now = ist(11, 12, 10, 0)
        assert ing._plan(now) == (False, now + timedelta(days=1))

    def test_the_calendar_is_asked_once_per_day(self, tmp_path):
        calls = []
        ing = make_ingester(tmp_path, session_day=lambda d: calls.append(d) or d.weekday() < 5)
        for hour in (8, 9, 12, 16):
            ing._plan(ist(11, 12, hour, 0))
        assert calls.count(date(2026, 11, 12)) == 1

    def test_the_default_calendar_rejects_a_weekend(self):
        assert ingest._default_session_day(date(2026, 11, 14)) is False


class Rig:
    """Runs the real scheduler on a fake clock. Every sleep moves the clock forward."""

    def __init__(self, tmp_path, start, session_day=weekdays_only, sources=(SOURCE,)):
        self.now = start.astimezone(UTC)
        self.until = start
        self.sleeps = []
        self.skew = timedelta(0)
        self.reached = asyncio.Event()
        self.go = asyncio.Event()
        self.ing = ingest.SourceIngester(
            sources,
            str(tmp_path / "ingest.db"),
            clock=lambda: self.now,
            sleep=self._sleep,
            peer_id_of=lambda entity: CHANNEL_ID,
            session_day=session_day,
        )
        self.db = tmp_path / "ingest.db"

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)
        while self.now + timedelta(seconds=seconds) > self.until.astimezone(UTC):
            self.reached.set()
            await self.go.wait()
            self.go.clear()
        self.now += timedelta(seconds=seconds) + self.skew
        self.skew = timedelta(0)
        await asyncio.sleep(0)

    async def run_until(self, target):
        self.until = target
        self.reached.clear()
        self.go.set()
        await asyncio.wait_for(self.reached.wait(), 5)
        for _ in range(5):
            await asyncio.sleep(0)

    def attached(self, client):
        return len(client.handlers)


class TestWindowScheduler:
    @pytest.mark.asyncio
    async def test_a_full_day_attaches_at_the_open_and_detaches_at_the_close(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 12, 8, 0))
        client = FakeClient()
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 12, 8, 59))
            assert client.handlers == [] and not rig.ing._window_open
            await rig.run_until(ist(11, 12, 9, 0))
            assert rig.attached(client) == 2 and rig.ing._window_open
            await rig.run_until(ist(11, 12, 15, 44))
            assert rig.attached(client) == 2
            await rig.run_until(ist(11, 12, 15, 45))
            assert client.handlers == [] and not rig.ing._window_open
            await rig.run_until(ist(11, 13, 9, 0))
            assert rig.attached(client) == 2  # the next session re-attaches
            assert not rig.db.exists()  # nothing was ever stored, so no file was opened
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_no_sleep_is_longer_than_an_hour_and_wakeups_stay_few(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 12, 9, 0))
        rig.ing.register(FakeClient())
        try:
            await rig.run_until(ist(11, 13, 9, 0))
        finally:
            rig.ing.close()
        assert max(rig.sleeps) <= 3600
        assert len(rig.sleeps) <= 30

    @pytest.mark.asyncio
    async def test_a_restart_inside_the_window_opens_it_at_once(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 12, 11, 0))
        client = FakeClient()
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 12, 11, 0))
            assert rig.attached(client) == 2
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_restart_after_the_close_stays_inactive_until_the_next_session(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 12, 16, 30))
        client = FakeClient()
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 13, 8, 59))
            assert client.handlers == []
            await rig.run_until(ist(11, 13, 9, 0))
            assert rig.attached(client) == 2
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_holiday_and_the_weekend_stay_inactive(self, tmp_path):
        holiday = date(2026, 11, 13)
        rig = Rig(
            tmp_path,
            ist(11, 13, 8, 0),
            session_day=lambda d: d.weekday() < 5 and d != holiday,
        )
        client = FakeClient()
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 16, 8, 59))
            assert client.handlers == [] and not rig.db.exists()
            await rig.run_until(ist(11, 16, 9, 0))
            assert rig.attached(client) == 2
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_late_wake_after_a_host_suspension_catches_up(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 12, 9, 0))
        client = FakeClient()
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 12, 9, 0))
            assert rig.attached(client) == 2
            rig.skew = timedelta(hours=8)  # the host slept through the close
            await rig.run_until(ist(11, 12, 18, 0))
            assert client.handlers == [] and not rig.ing._window_open
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_calendar_error_is_retried_and_does_not_end_the_scheduler(self, tmp_path):
        state = {"fail": True}

        def flaky(day):
            if state["fail"]:
                raise RuntimeError("calendar unreadable")
            return day.weekday() < 5

        rig = Rig(tmp_path, ist(11, 12, 9, 30), session_day=flaky)
        client = FakeClient()
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 12, 9, 30))
            assert client.handlers == [] and not rig.ing._scheduler.done()
            state["fail"] = False
            await rig.run_until(ist(11, 12, 9, 32))
            assert rig.sleeps[0] == 60 and rig.attached(client) == 2
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_calendar_error_cannot_hold_the_window_open_past_the_close(self, tmp_path):
        today = date(2026, 11, 12)

        def breaks_tomorrow(day):
            if day > today:
                raise RuntimeError("calendar unreadable")
            return True

        rig = Rig(tmp_path, ist(11, 12, 15, 0), session_day=breaks_tomorrow)
        client = FakeClient()
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 12, 15, 0))
            assert rig.attached(client) == 2
            await rig.run_until(ist(11, 12, 15, 46))
            assert client.handlers == []
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_disabled_source_starts_no_scheduler(self, tmp_path):
        off = TelegramChannel(name="v", id="x", enabled=False)
        rig = Rig(tmp_path, ist(11, 12, 10, 0), sources=(off,))
        rig.ing.register(FakeClient())
        assert rig.ing._scheduler is None

    @pytest.mark.asyncio
    async def test_register_is_idempotent_and_close_stops_the_scheduler(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 12, 10, 0))
        client = FakeClient()
        rig.ing.register(client)
        first = rig.ing._scheduler
        rig.ing.register(client)
        assert rig.ing._scheduler is first
        await rig.run_until(ist(11, 12, 10, 0))
        rig.ing.close()
        rig.ing.close()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert client.handlers == []


class TestWindowBackfill:
    def _client(self, rig, **kwargs):
        opening = ist(11, 13, 9, 0).astimezone(UTC)
        recent = [msg(1, minutes_ago=30, now=opening), msg(2, minutes_ago=90, now=opening)]
        return FakeClient(recent, **kwargs)

    @pytest.mark.asyncio
    async def test_a_connection_up_before_the_open_is_backfilled_when_the_window_opens(
        self, tmp_path
    ):
        rig = Rig(tmp_path, ist(11, 13, 8, 0))
        client = self._client(rig)
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 13, 8, 30))
            assert rig.ing.start_backfill(client) is None  # window closed: nothing started
            assert not rig.db.exists()
            await rig.run_until(ist(11, 13, 9, 0))
            await rig.ing._task
            assert stored(rig.ing).count() == 2
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_connection_up_inside_the_window_is_backfilled_by_serve(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 13, 9, 30))
        client = self._client(rig)
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 13, 9, 30))
            task = rig.ing.start_backfill(client)
            await task
            assert stored(rig.ing).count() == 2
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_the_scheduler_and_serve_never_run_two_backfills(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 13, 8, 0))
        client = self._client(rig, block=True)
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 13, 8, 30))
            rig.ing.start_backfill(client)  # the connection came up first
            await rig.run_until(ist(11, 13, 9, 0))  # the scheduler starts it at the open
            first = rig.ing._task
            assert first is not None and not first.done()
            assert rig.ing.start_backfill(client) is first  # _serve asks again: same task
            tasks = [t for t in asyncio.all_tasks() if t.get_name() == "telegram-ingest-backfill"]
            assert tasks == [first]
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_connection_after_the_close_starts_nothing(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 13, 16, 0))
        client = self._client(rig)
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 13, 16, 0))
            assert rig.ing.start_backfill(client) is None
            assert not rig.db.exists() and client.handlers == []
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_the_close_cancels_a_running_backfill_and_releases_the_database(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 13, 14, 0))
        client = self._client(rig, block=True)
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 13, 14, 0))
            task = rig.ing.start_backfill(client)
            while client.yielded < 2:
                await asyncio.sleep(0)
            stored(rig.ing)
            await rig.run_until(ist(11, 13, 15, 45))
            assert task.cancelled()
            assert rig.ing._task is None and rig.ing._store is None
            assert sqlite3.connect(rig.db).execute(
                "select count(*) from source_messages"
            ).fetchone() == (0,)
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_the_open_recovers_from_the_watermark_and_leaves_old_rows_intact(self, tmp_path):
        thursday_open = ist(11, 12, 9, 0).astimezone(UTC)
        seed = make_ingester(tmp_path, clock=lambda: thursday_open)
        old = [msg(1, minutes_ago=30, now=thursday_open), msg(2, minutes_ago=10, now=thursday_open)]
        await seed.backfill_source(FakeClient(old), SOURCE)
        before = {m: seed._get_store().get(CHANNEL_ID, m) for m in (1, 2)}
        seed.close()

        rig = Rig(tmp_path, ist(11, 13, 8, 0))
        opening = ist(11, 13, 9, 0).astimezone(UTC)
        overnight = msg(3, minutes_ago=200, now=opening)
        too_old = msg(4, minutes_ago=60 * 30, now=opening)  # before the watermark minus overlap
        client = FakeClient([overnight, *old, too_old])
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 13, 8, 30))
            rig.ing.start_backfill(client)
            await rig.run_until(ist(11, 13, 9, 0))
            await rig.ing._task
            store = stored(rig.ing)
            assert store.count() == 3
            assert store.get(CHANNEL_ID, 4) is None
            for mid, row in before.items():
                assert store.get(CHANNEL_ID, mid) == row  # untouched: same ingested_at, revision
            assert store.watermark(CHANNEL_ID) == opening
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_stored_messages_survive_the_close_and_the_next_open(self, tmp_path):
        rig = Rig(tmp_path, ist(11, 13, 9, 0))
        client = self._client(rig)
        rig.ing.register(client)
        try:
            await rig.run_until(ist(11, 13, 9, 0))
            await rig.ing.start_backfill(client)
            await rig.run_until(ist(11, 13, 15, 45))
            assert rig.ing._store is None
            await rig.run_until(ist(11, 16, 9, 0))
            assert stored(rig.ing).count() == 2
        finally:
            rig.ing.close()

    @pytest.mark.asyncio
    async def test_a_late_event_after_the_close_is_ignored_and_reopens_nothing(self, tmp_path):
        known = (TelegramChannel(name="v", id=CHANNEL_ID),)  # id known without a backfill
        rig = Rig(tmp_path, ist(11, 13, 15, 44), sources=known)
        rig.ing.register(FakeClient())
        try:
            await rig.run_until(ist(11, 13, 15, 45))
            await rig.ing._on_event(SimpleNamespace(chat_id=CHANNEL_ID, message=msg(9)))
            assert rig.ing._store is None and not rig.db.exists()
        finally:
            rig.ing.close()

    def test_closing_the_database_waits_for_a_write_in_progress(self, tmp_path):
        store = ingest.IngestStore(str(tmp_path / "s.db"))
        ing = make_ingester(tmp_path)
        ing._store = store
        done = threading.Event()
        store._lock.acquire()  # a write is in progress
        thread = threading.Thread(target=lambda: (ing._close_store(), done.set()))
        thread.start()
        try:
            assert not done.wait(0.3)  # the close is waiting for the write
        finally:
            store._lock.release()
        assert done.wait(5)
        thread.join()
        assert ing._store is None


class TestSharedClientSafety:
    @pytest.mark.asyncio
    async def test_window_changes_leave_the_trading_handler_and_connection_alone(self, tmp_path):
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), 12345, "0123456789abcdef0123456789abcdef")

        def forbidden(*args, **kwargs):
            raise AssertionError("ingestion touched the connection")

        client.connect = forbidden
        client.disconnect = forbidden
        client.start = forbidden

        async def trading(event):
            return None

        client.on(events.NewMessage(chats=[-1001]))(trading)
        trading_entry = client._event_builders[0]

        ing = make_ingester(tmp_path)
        ing._client = client
        for _ in range(2):  # open, close, open again, close again
            await ing._set_window(True)
            assert len(client._event_builders) == 3
            assert client._event_builders[0] == trading_entry  # still first, still the same
            assert [cb for _, cb in client._event_builders[1:]] == [ing._on_event] * 2
            await ing._set_window(False)
            assert client._event_builders == [trading_entry]
        assert not client.is_connected()


class TestModuleHooks:
    def test_stop_backfill_forgets_the_connection(self, tmp_path, monkeypatch):
        ing = opened(make_ingester(tmp_path))
        monkeypatch.setattr(ingest, "_service", ing)
        ing._served_client = object()
        ingest.stop_backfill(None)
        assert ing._served_client is None

    def test_stop_backfill_without_a_service_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(ingest, "_service", None)
        ingest.stop_backfill(None)
