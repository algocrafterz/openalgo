# BreakingTrade channel ingestion: status and next phase

Hand-off note for a new session. State as of 2026-11-13 00:30 IST.
Branch: `feature/optimize-signal-engine-v2.0.2.1` (local, not pushed).

## 1. Goal

Capture the messages of the Telegram channel `@BreakingTrade_rajat` (BreakingTrade.com
alerts) so they can be studied and later mined for trade signals.

The channel has Protected Content switched on: Telegram refuses a native forward, and
the owner has disabled forwarding. Decision taken with the user (option 2):

- Local storage only. Messages are read as the logged-in member is allowed to read them.
- No reposting, mirroring or forwarding to any other channel. No bypass of the restriction.
- The earlier target channel (-1004338836400, "smi-bt") is therefore not used.

## 2. Done

| Commit | What |
| --- | --- |
| `fa628a896` | `feat(signal_engine): record source-channel Telegram messages to a local SQLite store` |
| `903b9204d` | `feat(signal_engine): restrict telegram ingestion to market hours` |
| `dad28a70d` | `refactor(signal_engine): name the ingest database after its source channel` |

Caveat on `fa628a896`: its `listener.py` also contains another session's E-15 hunks
(late EXIT alerts processed against the position, `_is_exit_text`). That work was later
committed separately as `43adcba75`. Splitting `fa628a896` needs an interactive rebase
and was deliberately not done. Behaviour is not affected.

### Files

| File | Role |
| --- | --- |
| `signal_engine/ingest.py` | Store, live handlers, backfill, market-hours scheduler |
| `signal_engine/listener.py` | Four hook points: `ingest.register(client)` after the trading handler, `ingest.start_backfill(client)` and `ingest.stop_backfill(task)` in `_serve`, `ingest.close()` at final teardown |
| `signal_engine/config.py` | `Settings.ingest_sources` (tuple of `TelegramChannel`, default empty) |
| `signal_engine/config.yaml` | `telegram.ingest_sources` block, source `BreakingTrade_rajat`, `enabled: true` |
| `signal_engine/tests/test_telegram_ingest.py` | 75 tests |

### Behaviour

Storage: `signal_engine/data/breakingtrade_channel.db` (gitignored, WAL).

- `source_messages`, primary key `(channel_id, message_id)`. `channel_id` is Telegram's
  marked id (`-1001363983698`). Columns: `source_name`, `sent_at`, `edit_date`, `text`
  (raw), `text_md` (formatted), `views`, `forwards`, `reply_to_msg_id`, `grouped_id`,
  `post_author`, `has_media`, `ingested_at`, `updated_at`, `revision`.
- Timestamps are UTC ISO 8601, fixed width, seconds precision.
- An edit replaces a row only when its `edit_date` is strictly newer. `ingested_at` and
  `sent_at` never change; `revision` counts accepted edits.
- `ingest_state` holds one watermark per channel. Only a completed backfill moves it, and
  it never moves back. Live messages never move it.
- Messages without text are skipped. Message text is never written to the log.

Window and scheduling:

- Runs 09:00 to 15:45 IST on days where `pipeline.service.is_session_day()` is true.
  That excludes weekends, NSE holidays, mock sessions and special sessions (Muhurat).
- One scheduler task, started by `register()`. Sleeps to the next boundary, at most one
  hour at a time (WSL/host suspension), re-reads the clock on every wake. No polling and
  no extra process.
- Open: attach the two handlers (`NewMessage`, `MessageEdited`) and run the backfill.
  Close: detach the handlers, cancel and await the backfill, close the database file.
- Backfill: newest first, from the later of (watermark minus 2 hours) and 7 days ago,
  stored in one transaction together with the new watermark. Retries 3 times per
  connection, honours FLOOD_WAIT, capped at 10,000 messages.
- The scheduler and `_serve` share one backfill task and never run two.

Isolation from trading (all pinned by tests):

- Never calls `on_message`, the trading pipeline or `trades.db`.
- Never connects, disconnects or restarts the Telegram client. Its only contact is adding
  and removing its own two handlers, which come after the trading handler.
- The handlers carry no `chats=` filter, because Telethon resolves that filter inside the
  dispatch loop and a bad source would break dispatch for the trading channels.
- Every ingest error is caught and logged inside ingest. A calendar failure is retried
  after 60 seconds and cannot hold the window open past 15:45.

### Verification

- Focused: 75 ingest tests, plus 38 listener and telegram tests, all pass (113).
- Full suite through `run_full_tests.sh`: 10,091 passed, 1 skipped. Ruff clean.
- Two guards were proven by mutation (removing each made a test fail): the late-event
  guard and the single-backfill reuse.
- Live: the first backfill (engine restart 2026-11-12 18:42 IST) stored 71 messages from
  2026-11-06 to 2026-11-12. The data file now sits at `breakingtrade_channel.db` with
  those 71 rows and the watermark intact.

## 3. Not yet verified on a live engine

1. The market-hours scheduler has not run live. The engine that wrote the rows above ran
   the earlier always-on version. Restart the engine (user runs
   `.\openalgoctl.ps1 restart`; agents must not restart it), then expect in
   `signal_engine/logs/openalgoctl.log`:
   - at 09:00 IST: `Ingest: window open, recording`, then a line
     `Ingest: backfill of breakingtrade-rajat from ...`
   - at 15:45 IST: `Ingest: window closed, inactive until the next session`
2. The live path (new messages and edits arriving while connected) has not been seen. The
   channel was quiet after the close on 2026-11-12. After 09:20 IST check that the row
   count grows and `ingested_at` is close to `sent_at`:
   ```
   sqlite3 -readonly signal_engine/data/breakingtrade_channel.db \
     "select count(*), max(sent_at), max(ingested_at), sum(revision>1) from source_messages"
   ```
3. A restart inside the window opens it at once; a restart after 15:45 stays inactive
   until the next session day. Both are tested, not yet seen live.

## 4. Known limitations

- Posts made after 15:45 are stored at the next open (the backfill reaches back from the
  watermark). Nothing is lost, but the data is not real time after the close.
- Muhurat and other special sessions are not recorded (decision taken).
- If the NSE calendar cannot be read, `is_session_day` falls back to the weekday rule, so
  a holiday may be treated as a session. Harmless: ingestion just runs that day.
- Ingestion lives inside the trading listener process. If `start_listener` returns early
  (no enabled trading channel) or gives up after its FLOOD_WAIT budget, ingestion stops.
- Media-only posts are not stored. `views` and `forwards` are as at first sight and
  refresh only on an edit.
- SQLite writes are short blocking calls on the event loop, the same as `db.py`.
- `startup.py` shutdown does not close the ingest database; `listener` teardown does.
- SSH to GitHub is denied from this WSL (`Permission denied (publickey)`), so nothing can
  be fetched or pushed from here.
- No existing code or branch had message-persistence logic to reuse. Searched:
  `development/smi-assistant`, `development/consolidated`,
  `feature/intraday-algo-trading-system`, `main`, and all 101 tags.

## 5. What the data looks like so far

71 messages, 2026-11-06 to 2026-11-12. Two types seen:

- "Big Move Alert": posted about every 35 minutes in the session. A time, then a list of
  symbols with percent change, up and down. No entry, stop or target.
- An end-of-day market report: index change, breadth, sector performance table.

Nothing seen so far is a trade signal. The vendor sells a web scanner and its own
alerts; the repo's BreakingTrade README already records that adding signal volume has
not produced edge. Treat "does this channel carry tradable information at all" as the
first question of the next phase, not an assumption.

## 6. Next phase (not started): study the data, then decide

Stage 1 was storage only. Signal extraction and strategy integration were explicitly out
of scope and are not designed yet. Suggested order:

1. Confirm the live path and the window (section 3), after at least one full session.
2. Classify messages. Count message types by a stable header or prefix, sizes, and times
   of day. Output a short table, not code. Read from the database read-only.
3. Parse the "Big Move Alert" into a structured research table (time, symbol, direction,
   percent). Keep it separate from `source_messages`. Do not edit `source_messages`.
4. Join it to prices. Use the engine's existing 1-minute or 5-minute history (see
   `signal_engine/backtest/`). Measure forward returns after each alert, net of the repo's
   cost model, against a naive baseline. The question: does an alert predict anything the
   move itself did not already show? The move has already happened when the alert posts.
5. Record the verdict. A new idea gets a folder in `signal_engine/research/` (copy
   `_template`) before any strategy code; run
   `uv run python -m signal_engine.research.build_registry` after editing it.
6. Only if the verdict is positive: a signal source, with its own approval.

Rules that apply to any of this work (from the repo `CLAUDE.md`):

- Nothing here may change trading behaviour implicitly. Anything that produces orders or
  alters entries, exits or sizing needs the user's explicit approval for that action.
- Do not feed channel text into the existing Telegram signal parser or the trading
  pipeline. The ingest store is deliberately isolated from it.
- Do not repost or redistribute channel content.
- Logging: `from loguru import logger` inside `signal_engine`, ASCII only, no icons.
- Tests: targeted files first. Full suite only through
  `signal_engine/scripts/run_full_tests.sh`. Never run `pytest signal_engine/tests`
  directly, and never relax an assertion or timeout to pass.
- Use `uv run`. Inspect git with `git --no-optional-locks`.

## 7. Operating notes

- The engine is supervised by `openalgoctl.sh run`, launched from Windows
  `openalgoctl.ps1`. Do not restart it from an agent terminal. The user runs
  `.\openalgoctl.ps1 restart`.
- Reading the store while the engine runs is safe: open it read-only
  (`sqlite3 -readonly ...`). Do not copy the Telethon session file to run a second
  Telegram client against it while the engine is up.
- To switch ingestion off without a code change: set `enabled: false` on the source in
  `signal_engine/config.yaml` (`telegram.ingest_sources`) and restart.
- Other sessions work in the same tree. Stage explicit paths only. Unrelated working-tree
  changes seen at the end of this session were `.claude/skills/isolated-commit/*` and
  `signal_engine/bugs/bugs.json`; they are not part of this work.
- Remaining local-only work: the branch is 35 commits ahead of origin and has not been
  pushed.
