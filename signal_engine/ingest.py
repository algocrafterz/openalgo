"""Inbound Telegram channel ingestion: record messages from a source channel, trade nothing.

Stage 1 only. A source channel listed under `telegram.ingest_sources` in config.yaml has its
messages written verbatim to signal_engine/data/breakingtrade_channel.db so they can be studied
later. Nothing here parses a message, produces a signal, calls the trading pipeline or reposts
the content anywhere. The data stays on this machine, and a channel that restricts forwarding
is read exactly as the logged-in account is allowed to read it.

ISOLATION FROM THE TRADING LISTENER

  * The handlers are separate callbacks registered after the trading handler. They never call
    on_message, and every exception inside them is caught and logged here, so nothing can
    reach Telethon's dispatch loop or listener._retry_until_exhausted().
  * The handlers carry no `chats=` filter. Telethon resolves a builder's chats inside the
    dispatch loop for every update, outside any try block, so an unresolvable source would
    break dispatch for the trading channels too. The handlers compare event.chat_id with the
    resolved source ids themselves instead.
  * Backfill runs as its own asyncio task, started after the connection is up and cancelled
    when the connection ends. A failed backfill is logged and abandoned until the next
    reconnect. It is never raised into the listener.

MARKET-HOURS WINDOW

  * One scheduler task (started by register) sleeps to the next boundary: 09:00 to 15:45 IST
    on regular NSE session days, as decided by pipeline.service.is_session_day (weekends,
    holidays, mock and special sessions such as Muhurat are excluded). It sleeps at most an
    hour at a time and re-reads the wall clock on every wake. It does not poll anything.
  * At the open it attaches the live handlers and starts the backfill. At the close it detaches
    the handlers, cancels and awaits the backfill, then closes the database file. Outside the
    window nothing of ours is attached to the Telegram client.
  * The Telegram connection itself belongs to the trading listener. The scheduler never
    connects, disconnects or restarts it. Its only contact with the client is adding and
    removing its own two handlers, which are always the last in Telethon's handler list.
  * A connection that comes up outside the window starts nothing. One that comes up inside it
    runs the backfill through listener._serve, and the scheduler and _serve never run two
    backfills at once.

STORAGE

  * One row per (channel_id, message_id). channel_id is Telegram's marked peer id (for a
    channel, -100 followed by the channel number), the same form config.yaml uses.
  * Writes are idempotent. A repeated message changes nothing. An edited message replaces the
    stored content only when its edit_date is newer than the stored one, so a stale copy from
    a backfill can never overwrite a newer edit. ingested_at and sent_at never change.
  * ingest_state keeps one watermark per channel: the time up to which history is known to be
    stored without a gap. Only a completed backfill moves it. Live messages never do, because
    a live message stored beyond a gap would otherwise hide that gap from the next recovery.
  * Timestamps are UTC ISO 8601 with a fixed width, so string comparison is time comparison.
  * Message text is never written to the log. Log lines are ASCII only.
"""

import asyncio
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from loguru import logger

from signal_engine.config import settings
from signal_engine.timeutils import IST

INGEST_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "breakingtrade_channel.db"
)

#: How far back the first backfill reaches, in calendar days.
BACKFILL_DAYS = 7

#: A recovery backfill restarts this far BEFORE the watermark, so a message that Telegram
#: delivered late, or edited after it was stored, is still picked up.
BACKFILL_OVERLAP = timedelta(hours=2)

#: Ingestion runs only inside this window (IST) on regular NSE session days. Outside it no
#: handler is attached, no backfill runs and the database file is closed.
WINDOW_START = time(9, 0)
WINDOW_END = time(15, 45)

#: The scheduler never sleeps longer than this. A monotonic timer stops while the host is
#: suspended (WSL sleep), so one long sleep could wake hours late; the cap bounds the lateness.
_MAX_SLEEP_SECONDS = 3600

#: Pause after a scheduler error, and how far ahead the next session day is looked for.
_SCHEDULER_RETRY_SECONDS = 60
_LOOKAHEAD_DAYS = 14

#: Safety stop on one backfill. A week of this channel is a few hundred messages.
_MAX_BACKFILL_MESSAGES = 10000

#: Tries per source in one connection, and the pause between them when Telegram gives no wait.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 30
_MAX_WAIT_SECONDS = 900

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_messages (
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    source_name     TEXT,
    sent_at         TEXT NOT NULL,
    edit_date       TEXT,
    text            TEXT NOT NULL,
    text_md         TEXT,
    views           INTEGER,
    forwards        INTEGER,
    reply_to_msg_id INTEGER,
    grouped_id      INTEGER,
    post_author     TEXT,
    has_media       INTEGER NOT NULL DEFAULT 0,
    ingested_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_source_messages_sent ON source_messages (channel_id, sent_at);
CREATE TABLE IF NOT EXISTS ingest_state (
    channel_id         INTEGER PRIMARY KEY,
    backfilled_through TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
"""

# The WHERE clause is the edit rule: an existing row is touched only by a strictly newer edit.
_UPSERT = """
INSERT INTO source_messages (
    channel_id, message_id, source_name, sent_at, edit_date, text, text_md,
    views, forwards, reply_to_msg_id, grouped_id, post_author, has_media,
    ingested_at, updated_at, revision
) VALUES (
    :channel_id, :message_id, :source_name, :sent_at, :edit_date, :text, :text_md,
    :views, :forwards, :reply_to_msg_id, :grouped_id, :post_author, :has_media,
    :now, :now, 1
)
ON CONFLICT (channel_id, message_id) DO UPDATE SET
    edit_date = excluded.edit_date,
    text = excluded.text,
    text_md = excluded.text_md,
    views = excluded.views,
    forwards = excluded.forwards,
    reply_to_msg_id = excluded.reply_to_msg_id,
    grouped_id = excluded.grouped_id,
    post_author = excluded.post_author,
    has_media = excluded.has_media,
    updated_at = excluded.updated_at,
    revision = source_messages.revision + 1
WHERE excluded.edit_date IS NOT NULL
  AND (source_messages.edit_date IS NULL OR excluded.edit_date > source_messages.edit_date)
"""

_WATERMARK = """
INSERT INTO ingest_state (channel_id, backfilled_through, updated_at)
VALUES (:channel_id, :through, :now)
ON CONFLICT (channel_id) DO UPDATE SET
    backfilled_through = excluded.backfilled_through,
    updated_at = excluded.updated_at
WHERE excluded.backfilled_through > ingest_state.backfilled_through
"""


def _utc_text(moment: datetime | None) -> str | None:
    """Fixed-width UTC text for a datetime. A naive value is taken to be UTC."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def build_row(msg, channel_id: int, source_name: str) -> dict | None:
    """The storable form of a Telegram message, or None when there is nothing to keep.

    A message with no text (a bare photo, a service message) is skipped: Stage 1 records text.
    """
    text = getattr(msg, "message", None)
    message_id = getattr(msg, "id", None)
    sent_at = _utc_text(getattr(msg, "date", None))
    if not text or message_id is None or sent_at is None:
        return None
    try:
        text_md = msg.text
    except Exception:  # noqa: BLE001 - the formatted copy is optional; the raw text is kept
        text_md = None
    return {
        "channel_id": int(channel_id),
        "message_id": int(message_id),
        "source_name": source_name,
        "sent_at": sent_at,
        "edit_date": _utc_text(getattr(msg, "edit_date", None)),
        "text": text,
        "text_md": text_md,
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "reply_to_msg_id": getattr(msg, "reply_to_msg_id", None),
        "grouped_id": getattr(msg, "grouped_id", None),
        "post_author": getattr(msg, "post_author", None),
        "has_media": 1 if getattr(msg, "media", None) else 0,
    }


class IngestStore:
    """SQLite store for source messages. One long-lived connection, safe from any thread."""

    def __init__(self, db_path: str):
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        """Close the connection. Idempotent."""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - a close failure must not break teardown
                pass
            self._conn = None

    def upsert_many(self, rows: list[dict], watermark: tuple[int, str] | None = None) -> dict:
        """Store rows and optionally advance a watermark, all in ONE transaction.

        Returns {"inserted": n, "updated": n, "unchanged": n}. The watermark never moves back.
        """
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        now = _utc_text(datetime.now(UTC))
        with self._lock, self._conn:
            for row in rows:
                existed = (
                    self._conn.execute(
                        "SELECT 1 FROM source_messages WHERE channel_id = ? AND message_id = ?",
                        (row["channel_id"], row["message_id"]),
                    ).fetchone()
                    is not None
                )
                changed = self._conn.execute(_UPSERT, {**row, "now": now}).rowcount
                if not changed:
                    counts["unchanged"] += 1
                elif existed:
                    counts["updated"] += 1
                else:
                    counts["inserted"] += 1
            if watermark is not None:
                self._conn.execute(
                    _WATERMARK,
                    {"channel_id": watermark[0], "through": watermark[1], "now": now},
                )
        return counts

    def watermark(self, channel_id: int) -> datetime | None:
        """Time up to which this channel's history is stored without a gap, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT backfilled_through FROM ingest_state WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def get(self, channel_id: int, message_id: int) -> dict | None:
        """One stored message as a dict, or None."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM source_messages WHERE channel_id = ? AND message_id = ?",
                (channel_id, message_id),
            )
            row = cur.fetchone()
            names = [d[0] for d in cur.description]
        return dict(zip(names, row, strict=True)) if row else None

    def count(self, channel_id: int | None = None) -> int:
        """Number of stored messages, optionally for one channel."""
        with self._lock:
            if channel_id is None:
                return self._conn.execute("SELECT COUNT(*) FROM source_messages").fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM source_messages WHERE channel_id = ?", (channel_id,)
            ).fetchone()[0]


def _default_peer_id(entity) -> int:
    from telethon import utils

    return utils.get_peer_id(entity)


def _default_session_day(day: date) -> bool:
    """The runner's NSE calendar verdict, imported lazily to keep this module light."""
    from signal_engine.pipeline.service import is_session_day

    return is_session_day(day)


class SourceIngester:
    """Records the configured source channels. See the module docstring for the guarantees."""

    def __init__(
        self,
        sources,
        db_path: str = INGEST_DB_PATH,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable = asyncio.sleep,
        peer_id_of: Callable = _default_peer_id,
        session_day: Callable[[date], bool] | None = None,
        days: int = BACKFILL_DAYS,
        overlap: timedelta = BACKFILL_OVERLAP,
        max_messages: int = _MAX_BACKFILL_MESSAGES,
    ):
        self._sources = tuple(s for s in sources if s.enabled)
        self._db_path = db_path
        self._clock = clock
        self._sleep = sleep
        self._peer_id_of = peer_id_of
        self._session_day_fn = session_day or _default_session_day
        self._day_cache: dict[date, bool] = {}
        self._cache_day: date | None = None
        self._days = days
        self._overlap = overlap
        self._max_messages = max_messages
        self._store: IngestStore | None = None
        self._client = None  # the trading listener's Telegram client; used only for handlers
        self._served_client = None  # set while the listener holds a live connection
        self._handlers_attached = False
        self._window_open = False
        self._scheduler: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        # chat id -> source name. A numeric id in config is known from the start; a username
        # is added once it has been resolved on a live connection.
        self._channels: dict[int, str] = {
            int(s.id): s.name for s in self._sources if isinstance(s.id, int)
        }

    @property
    def active(self) -> bool:
        return bool(self._sources)

    def _get_store(self) -> IngestStore:
        """Open the database on first use, so an idle or unconfigured ingester touches no file."""
        if self._store is None:
            self._store = IngestStore(self._db_path)
        return self._store

    def _close_store(self) -> None:
        """Close the database file. store.close() takes the write lock, so a write that is
        running in another thread finishes first. Never raises."""
        store, self._store = self._store, None
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001 - teardown must not become a failure
                pass

    def close(self) -> None:
        """Stop the scheduler and any backfill, detach the handlers, close the database.
        Idempotent, never raises."""
        self.stop_backfill(self._scheduler)
        self._scheduler = None
        self.stop_backfill(self._task)
        self._task = None
        self._window_open = False
        self._detach_handlers()
        self._close_store()

    # ---- window ----------------------------------------------------------------------

    def register(self, client) -> None:
        """Remember the trading client and start the window scheduler. Once per client.

        Nothing is attached to the client here; the handlers appear only while the window is
        open. Needs a running event loop (start_listener provides one).
        """
        if not self.active or self._client is client:
            return
        self._client = client
        if self._scheduler is None or self._scheduler.done():
            self._scheduler = asyncio.create_task(self._schedule(), name="telegram-ingest-window")
            self._scheduler.add_done_callback(self._log_scheduler_end)
        names = ", ".join(s.name for s in self._sources)
        logger.info(
            f"Ingest: {names} is recorded {WINDOW_START:%H:%M}-{WINDOW_END:%H:%M} IST on NSE "
            "session days (storage only, no trading)"
        )

    @staticmethod
    def _log_scheduler_end(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.opt(exception=task.exception()).error("Ingest: window scheduler ended")

    def _ist_now(self) -> datetime:
        return self._clock().astimezone(IST)

    def _is_session_day(self, day: date) -> bool:
        """The calendar verdict for a day, asked once per day and then remembered."""
        today = self._ist_now().date()
        if self._cache_day != today:
            self._day_cache.clear()
            self._cache_day = today
        if day not in self._day_cache:
            self._day_cache[day] = bool(self._session_day_fn(day))
        return self._day_cache[day]

    def _plan(self, now: datetime) -> tuple[bool, datetime]:
        """(window is open now, when the state next changes). `now` is IST."""
        today = now.date()
        if self._is_session_day(today):
            start = datetime.combine(today, WINDOW_START, tzinfo=IST)
            end = datetime.combine(today, WINDOW_END, tzinfo=IST)
            if now < start:
                return False, start
            if now < end:
                return True, end
        day = today
        for _ in range(_LOOKAHEAD_DAYS):
            day += timedelta(days=1)
            if self._is_session_day(day):
                return False, datetime.combine(day, WINDOW_START, tzinfo=IST)
        return False, now + timedelta(days=1)

    async def _schedule(self) -> None:
        """Open and close the window for as long as the engine runs.

        An error here is logged and retried; it can never reach the trading listener, and a
        calendar failure cannot hold the window open past the close time.
        """
        while True:
            wait = float(_SCHEDULER_RETRY_SECONDS)
            try:
                now = self._ist_now()
                is_open, wake = self._plan(now)
                await self._set_window(is_open)
                wait = min(max((wake - now).total_seconds(), 1.0), float(_MAX_SLEEP_SECONDS))
            except Exception:  # noqa: BLE001 - cancellation is a BaseException and passes
                logger.exception("Ingest: window scheduler error; retrying shortly")
                try:
                    if self._window_open and self._ist_now().time() >= WINDOW_END:
                        await self._set_window(False)
                except Exception:  # noqa: BLE001
                    logger.exception("Ingest: could not close the window after a scheduler error")
            await self._sleep(wait)

    async def _set_window(self, want_open: bool) -> None:
        """Move to the requested state. Does nothing when already there."""
        if want_open == self._window_open:
            return
        if want_open:
            self._attach_handlers()
            self._window_open = True
            logger.info("Ingest: window open, recording")
            if self._served_client is not None:
                self._start_task(self._served_client)
            return
        # Closing. The flag goes first, so an event already queued for dispatch is ignored.
        self._window_open = False
        self._detach_handlers()
        task, self._task = self._task, None
        self.stop_backfill(task)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._close_store()
        logger.info("Ingest: window closed, inactive until the next session")

    def _attach_handlers(self) -> None:
        client = self._client
        if client is None or self._handlers_attached:
            return
        from telethon import events

        client.add_event_handler(self._on_event, events.NewMessage())
        try:
            client.add_event_handler(self._on_event, events.MessageEdited())
        except Exception:
            client.remove_event_handler(self._on_event, events.NewMessage)
            raise
        self._handlers_attached = True

    def _detach_handlers(self) -> None:
        """Remove our two handlers. Only callbacks equal to ours match, so the trading
        handler is never touched. Never raises."""
        client = self._client
        if client is None or not self._handlers_attached:
            return
        self._handlers_attached = False
        try:
            from telethon import events

            client.remove_event_handler(self._on_event, events.NewMessage)
            client.remove_event_handler(self._on_event, events.MessageEdited)
        except Exception:  # noqa: BLE001 - _on_event ignores events outside the window anyway
            logger.exception("Ingest: could not detach the live handlers")

    async def _on_event(self, event) -> None:
        """Store a new or edited message from a source channel. Never raises."""
        try:
            if not self._window_open:
                return
            chat_id = getattr(event, "chat_id", None)
            name = self._channels.get(chat_id)
            if name is None:
                return
            row = build_row(event.message, chat_id, name)
            if row is None:
                return
            self._get_store().upsert_many([row])
        except Exception:  # noqa: BLE001 - ingestion must never reach the dispatch loop
            logger.exception("Ingest: could not store a live message")

    # ---- backfill --------------------------------------------------------------------

    def start_backfill(self, client) -> asyncio.Task | None:
        """Called by listener._serve once a connection is up. Never raises.

        Records the live connection, so the scheduler can start the backfill if the window
        opens later. Starts the backfill now only if the window is open.
        """
        if not self.active:
            return None
        self._served_client = client
        if not self._window_open:
            return None
        return self._start_task(client)

    def _start_task(self, client) -> asyncio.Task | None:
        """The one backfill task: a running one is reused, never doubled. Never raises."""
        try:
            asyncio.get_running_loop()  # no loop: nothing to start, and no coroutine is built
            if self._task is not None and not self._task.done():
                return self._task
            self._task = asyncio.create_task(self._run(client), name="telegram-ingest-backfill")
            self._task.add_done_callback(self._log_outcome)
            return self._task
        except Exception:  # noqa: BLE001 - the trading listener must not see this
            logger.exception("Ingest: could not start the backfill task")
            return None

    def connection_ended(self, task: asyncio.Task | None) -> None:
        """The listener's connection is gone: its backfill dies with it. Never raises."""
        self.stop_backfill(task)
        self.stop_backfill(self._task)
        self._task = None
        self._served_client = None

    @staticmethod
    def stop_backfill(task: asyncio.Task | None) -> None:
        """Cancel a backfill task without waiting for it. Never raises."""
        try:
            if task is not None and not task.done():
                task.cancel()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _log_outcome(task: asyncio.Task) -> None:
        if task.cancelled():
            logger.info("Ingest: backfill stopped before it finished")
            return
        exc = task.exception()
        if exc is not None:
            logger.opt(exception=exc).error("Ingest: backfill task failed")

    async def _run(self, client) -> None:
        """Backfill every source. A failing source never blocks the next one."""
        for source in self._sources:
            await self._run_source(client, source)

    async def _run_source(self, client, source) -> None:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                await self.backfill_source(client, source)
                return
            except Exception as exc:  # noqa: BLE001 - cancellation is a BaseException and passes
                if attempt == _MAX_ATTEMPTS:
                    logger.error(
                        f"Ingest: backfill of {source.name} gave up after {attempt} attempts "
                        f"({type(exc).__name__}). It is retried on the next reconnect."
                    )
                    return
                seconds = getattr(exc, "seconds", None)
                if isinstance(seconds, (int, float)):
                    wait = min(float(seconds) + 5.0, _MAX_WAIT_SECONDS)
                else:
                    wait = _RETRY_BACKOFF_SECONDS
                logger.warning(
                    f"Ingest: backfill of {source.name} failed ({type(exc).__name__}), "
                    f"attempt {attempt}/{_MAX_ATTEMPTS}, retrying in {wait:.0f}s"
                )
                await self._sleep(wait)

    async def backfill_source(self, client, source) -> dict:
        """Fetch one source's history since the window start and store it.

        The window starts at the watermark minus the overlap, but never earlier than
        BACKFILL_DAYS ago. History is read newest first and held in memory, then written in
        one transaction together with the new watermark. A cancel or an error before that
        point therefore changes nothing, and the next run starts from the same place.
        """
        t0 = self._clock()
        horizon = t0 - timedelta(days=self._days)
        store = self._get_store()

        entity = await client.get_entity(source.id)
        channel_id = int(self._peer_id_of(entity))
        self._channels[channel_id] = source.name

        mark = store.watermark(channel_id)
        start = horizon if mark is None else max(horizon, mark - self._overlap)

        rows: list[dict] = []
        capped = False
        async for msg in client.iter_messages(entity):
            msg_date = getattr(msg, "date", None)
            if msg_date is None:
                continue
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=UTC)
            if msg_date < start:
                break
            row = build_row(msg, channel_id, source.name)
            if row is not None:
                rows.append(row)
            if len(rows) >= self._max_messages:
                capped = True
                break
            if len(rows) % 100 == 0:
                await asyncio.sleep(0)

        counts = store.upsert_many(rows, watermark=(channel_id, _utc_text(t0)))
        logger.info(
            f"Ingest: backfill of {source.name} from {_utc_text(start)}: "
            f"{counts['inserted']} new, {counts['updated']} edited, "
            f"{counts['unchanged']} already stored"
        )
        if capped:
            logger.warning(
                f"Ingest: backfill of {source.name} stopped at {self._max_messages} messages; "
                "older messages in the window may be missing"
            )
        return counts


# ---- module-level entry points used by listener.py ------------------------------------
# Each one swallows its own failures: the listener calls them and must not have to guard them.

_service: SourceIngester | None = None


def _get_service() -> SourceIngester:
    global _service
    if _service is None:
        _service = SourceIngester(settings.ingest_sources)
    return _service


def register(client) -> None:
    """Hand over the client and start the market-hours scheduler. Attaches nothing itself."""
    try:
        _get_service().register(client)
    except Exception:  # noqa: BLE001
        logger.exception("Ingest: could not register the live handlers; trading is unaffected")


def start_backfill(client) -> asyncio.Task | None:
    """Note the live connection. Returns the backfill task, or None when the window is closed."""
    try:
        return _get_service().start_backfill(client)
    except Exception:  # noqa: BLE001
        logger.exception("Ingest: could not start the backfill; trading is unaffected")
        return None


def stop_backfill(task: asyncio.Task | None) -> None:
    """The connection has ended: cancel its backfill and forget the connection."""
    try:
        if _service is not None:
            _service.connection_ended(task)
        else:
            SourceIngester.stop_backfill(task)
    except Exception:  # noqa: BLE001
        pass


def close() -> None:
    """Release the database. Called once when the listener exits."""
    try:
        if _service is not None:
            _service.close()
    except Exception:  # noqa: BLE001
        pass
