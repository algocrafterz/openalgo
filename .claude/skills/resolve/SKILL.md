---
name: resolve
description: Work an OpenAlgo/signal_engine issue from the bug tracker to closed - "/resolve" (the next issue in the queue), "/resolve BUG-007", "/resolve verify" (prove or reopen the fixed ones), or "/resolve" with a new symptom in words. Reproduces the issue from the stored evidence (tracker entry, /logs/workflow reports, trades.db, sandbox fills, logs), finds the cause, fixes code or data under the repo's rules, proves the fix against the evidence that found it, and records every step in the tracker. Use when asked to "fix the open bugs", "look into BUG-00N", "why did X happen", "troubleshoot", "is the fix working", "improve/optimise strategy Y", or after a RED alert. Detection is open-day / close-day; the register is bug-tracker.
---

# Resolve an issue

The pipeline has three owners and this skill is the last one:

| Stage | Owner | Output |
| --- | --- | --- |
| Detect | open-day, liveness, close-day (`/open-day`, `/close-day`, cron) | `signal_engine/reports/<step>-<day>.md/.json`, shown at `/logs/workflow` |
| Record | the close-day review and interactive reviews, per `bug-tracker` | `signal_engine/bugs/bugs.json`, shown at `/logs/bugs` |
| **Resolve** | **this skill** | a fix, its proof, and the tracker entry moved to `fixed` / `verified` |

So an issue reaches the tracker without anyone asking: the morning checks write reports, and the
15:36 close-day review turns confirmed findings (morning ones included) into entries or new
occurrences. A RED alert that cannot wait until the close is recorded by `/open-day`.

## Modes

- `/resolve` - the next issue: `uv run python -m signal_engine.bugs list` ranked HIGH first, then
  most days seen, then oldest. TUNING-IDEA entries are skipped unless named. Show the top five in
  one line each, take the first unless the owner names another.
- `/resolve BUG-00N` - that entry.
- `/resolve <symptom in words>` - check `list --all` for a match first (`seen` it); otherwise
  investigate, and `add` it once the evidence confirms it.
- `/resolve verify` - every `fixed` entry: did a session since the fix exercise its path? Held:
  `update --status verified --verification "<day>: <evidence>"`. Recurred: `seen --date <day>`
  (reopens it). Not exercised: `note` that, and say what event would prove it.

## Working one issue

1. **Load it.** `uv run python -m signal_engine.bugs show BUG-00N`: impact, evidence, sources,
   history. Open the reports it names and the day's `/logs/workflow` outputs
   (`reports/<step>-<day>.md`). `update --status in_progress --note "<what you are checking>"`.
2. **Reproduce from data before reading code.** Find the query or log line that shows the defect
   on its recorded day, and check whether it still shows on the latest session:
   - P&L, books: `pages_vs_fills --mode analyze <day>`, `trades.db`, `db/sandbox.db` `sandbox_trades`,
     `db/openalgo.db` `strategy_positions` / `strategy_closed_trades`.
   - Orders, timing: `signal_engine/logs/signal_engine_analyze_<day>.log`, `log/openalgo_<day>.log*`
     (`Slow sandbox place_order`, `Slow sandbox fill`, `rate_limit` summaries), `execution-health-<day>.md`.
   - Errors: `logs-<day>.md` (signatures, NEW/RISING), `log/errors.jsonl`.
   - Data: the backfill log's `verify` lines, `value-areas-<day>.md`, the `data-check` skill.
   Cannot reproduce: say so, `note` what was checked, stop. Never fix what the data does not show.
3. **Find the cause.** Trace the code path the evidence points at; measure before assuming (the
   BUG-007 timeouts looked like rate limiting and were a sandbox lock). Write it:
   `update --root-cause "<cause, with the evidence>"`.
4. **Choose the route by kind.**
   - **BUG** - code change. Trading behaviour (entries, exits, stops, sizing, timing, filters):
     state old and new behaviour and get the owner's approval first. Regression test first, from
     the real day's symbols, quantities and order ids.
   - **DATA** - stored data wrong. Back up the database (`sqlite3 <db> ".backup db/backups/<name>.db"`),
     dry-run the repair and compare it with the fills, guard every UPDATE on the exact bad values,
     then re-run the check that found it. Fills are the truth: rebuild derived rows from them
     (the BUG-002 and BUG-010 replays are the pattern). Record a remaining accepted difference in
     `signal_engine/analysis/pages_vs_fills_exceptions.json` with its reason.
   - **EXECUTION** - broker, sandbox, timing, operations. Find whose code owns the delay. Fix it
     in signal_engine when it is ours; OpenAlgo core needs explicit owner approval and a line in
     `docs/strategy-pnl-fork-modification.md`.
   - **TUNING-IDEA** - never a code change from fewer than 20 sessions, never on a frozen strategy
     (memory). The route is research: `signal-engine-backtest` and the acceptance criteria, with
     the trial recorded in `signal_engine/research/`. The outcome is a note on the entry.
5. **Prove it.** The targeted tests, and the wider suites the CLAUDE.md risk rules under "Tests"
   require (full suite through `signal_engine/scripts/run_full_tests.sh`, gthread suite); then
   re-run the exact check that found the issue for its recorded day(s) and quote the before and after.
6. **Record it.** `update --status fixed --fix "<what changed, which test, backup path>"`, and add
   `--commit SHA` once committed. `note` the event a future session must show to verify it (for
   example "a burst of 4+ simultaneous orders with no 'Timeout sending order'"). The close-day
   review notes that event when it happens; `/resolve verify` promotes it.
7. **Ship it only when asked.** Commits go through `isolated-commit`; a running-code change needs
   OpenAlgo or the engine restarted (`openalgoctl.ps1 restart`), which is the owner's call.

## Report

1. One line: the issue, the cause, fixed / not reproduced / needs a decision.
2. Evidence before and after (the same query or report line).
3. What changed (files, data with backup), the tests, and what is still unproven.
4. In plain words for a trader: what was wrong, what it cost, what is different from the next session.

Model: Sonnet by default; Opus only for order-lifecycle races or concurrency (ai-model-workflow).
Words: sandbox (analyzer mode), never "paper trading". No emojis.
