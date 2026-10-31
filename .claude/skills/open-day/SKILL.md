---
name: open-day
description: The start-of-day entry point for the OpenAlgo algo system - "/open-day [YYYY-MM-DD]". Reads the 09:12 readiness check and the 09:27 liveness check (signal_engine/ops), re-runs them read-only when missing or stale, and says in plain words whether the stack is ready to trade and what to do if not. Use when asked "are we ready for the open", "is everything up", "morning check", "is the engine trading", or when an open-day / liveness RED alert arrives on Telegram. The end of day is the close-day skill.
---

# Open the day

The morning has two phases, each with one duty. Neither places, modifies or cancels orders.

| Phase | When | Owns | Output |
| --- | --- | --- | --- |
| open-day | 09:12 | readiness: services, health URL, broker session (funds > 0), mode, the engine's own 08:5x startup checks (read from its log, never re-run), poller start, ports 8765/5555, NTP, disk, trades.db, risk counters fresh, errors since the last close, morning Windows tasks, yesterday's close-day | `signal_engine/reports/open-day-<day>.md/.json` |
| liveness | 09:27 | the first runner cycle (symbols evaluated, late bars), the BreakingTrade poller's first scan, signals reaching the pipeline, timeout/429 and retry storms vs yesterday, stale prices | `signal_engine/reports/liveness-<day>.md/.json` |

Both run from cron through `signal_engine/ops/run_phase.sh <phase>`; the `notify` step sends
Telegram only when the verdict is RED. Results are on `/logs/workflow`.

## Interactive run (/open-day)

1. Read `open-day-<day>.json` and, after 09:27, `liveness-<day>.json`. Missing or older than the
   phase time: run `PYTHONPATH=. uv run python -m signal_engine.ops open-day` (or `liveness`).
2. For every FAIL, find the cause from evidence before saying anything, in this order:
   services and health (`signal_engine/scripts/openalgoctl.sh status`, `log/errors.jsonl`), broker
   session (token expires about 03:00 IST; a failed morning login shows in the scheduler log and on
   Telegram), clock (`timedatectl`; WSL2 drift has broken TOTP login before, see memory), then the
   engine log `signal_engine/logs/signal_engine_analyze_<day>.log`.
3. Propose the fix. Restarting services or logging in again is the owner's call; ask first.
4. Record a new confirmed defect per the `bug-tracker` skill.
5. Reply: one verdict line (GREEN / AMBER / RED), the failed or warned checks with evidence, what
   to do before the open, and a plain-words close for a trader.

Words: the simulated account is **sandbox** (analyzer mode), never "paper trading". No emojis.
