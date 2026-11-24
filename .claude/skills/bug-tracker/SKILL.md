---
name: bug-tracker
description: >-
  The register of OpenAlgo/signal_engine defects: how to record them in the single bug tracker (signal_engine/bugs/bugs.json, shown at http://localhost:5000/logs/bugs). Use whenever a review (eod, paper-review, execution-health, a weekly report) finds a defect, an execution problem, a data error or a tuning idea; when asked to "log this bug" or "what bugs are open"; and in the unattended close-day review. Reviews find problems, this skill keeps them, and the resolve skill works them to closed.
---

# Bug tracker

One defect is one entry, however many days it recurs. Reviews describe a day; the tracker
describes a problem's whole life: first seen, every day seen again, cause, fix, proof.

- **Data**: `signal_engine/bugs/bugs.json`, git-tracked. Never edit it by hand or with a script;
  write only through the CLI or `signal_engine/bugs/store.py` (they validate, lock across
  processes and keep history). A bug reported from a Daily Workflow finding carries a
  `workflow-finding:<key>` entry in `sources`; leave it in place, it is the duplicate key.
- **Page**: http://localhost:5000/logs/bugs (read-only, `blueprints/bug_tracker.py`; UI in
  `frontend/src/pages/monitoring/BugTracker.tsx`, search/filter/sort logic in
  `frontend/src/lib/bugFilters.ts` with its vitest file; tab, filters and sort live in the URL
  query). The sibling `/logs/workflow` page is `monitoring/DailyWorkflow.tsx` over
  `blueprints/ops_workflow.py`. When changing either page: import router hooks from
  `react-router` (`react-router-dom` is not installed), put pure logic in `lib/` with a test, and
  verify with `npm run build` (runs `tsc -b`; a bare `tsc -p tsconfig.json` checks nothing) plus
  biome and vitest on Node 22. For a rendered check use a throwaway preview with a mock server
  (see the `openalgo-frontend-ops-pages` skill); the real app may stop at the broker page.
  The page's "Report as bug" action writes through the store, not the CLI binary; any new writer
  must follow that skill's `references/tracker-write-path.md`.
- **CLI**: `uv run python -m signal_engine.bugs <list|show|add|seen|update|note|apply>`; `--help`
- **Lifecycle** (enforced in `store.update`): open→in_progress|wontfix; in_progress→open|fixed|wontfix; fixed→verified|open|in_progress; verified→open; wontfix→open. Reopening or `wontfix` needs `--note` (the reason); `fixed` needs `--fix`; `verified` needs `--verification`. Old fixes with no commit: `--fix-explanation`, never an invented SHA. `--owner` is free text. A recurrence via `seen` reopens automatically (history says recurrence). `store.age_days/is_overdue/awaiting_verification` are computed (HIGH open/in_progress older than 3 days = overdue); `list --json` adds them.
  on each.

## Lifecycle

```
open -> in_progress -> fixed -> verified
                         ^         |
                         +-- seen again reopens it (back to open)
wontfix: a deliberate decision, with the reason in --note
```

| Status | Meaning | Who sets it |
| --- | --- | --- |
| open | Confirmed from evidence, not being worked | any review, interactive or unattended |
| in_progress | Someone is fixing it now (name the branch in `--note`) | interactive session |
| fixed | Code or data changed; `--fix` says what, `--commit` names it once committed | interactive session |
| verified | A later session exercised the fixed path and it held; `--verification` names the session and the evidence | interactive session, never unattended |
| wontfix | Not worth fixing or not ours (upstream core), reason in `--note` | owner decision |

A fix that has not met its trigger yet stays `fixed`. "No timed-out exits today" proves nothing
about a timed-out-exit fix: note it and wait for a session that exercised the path.

## 1. Record (every review)

1. `list --all` first. A finding that matches an existing entry is `seen ID --date DAY`, with new
   evidence, not a new entry. `add` refuses a duplicate open title.
2. Record only what the evidence confirms: a trades.db row, a tradebook fill, a log line, a query.
   A suspicion goes in the review's prose as unconfirmed, not in the tracker.
3. `add` fields:
   - `--kind`: BUG (code does not do what spec/config says), EXECUTION (broker, sandbox, timing,
     operations), DATA (stored data wrong, code may be fine), TUNING-IDEA (a parameter or design
     change; sample size and overfit risk go in `--root-cause`).
   - `--severity`: HIGH = can leave a position unprotected, send a wrong order, or misstate P&L the
     trader acts on; MEDIUM = wrong records or degraded execution with no direct position risk;
     LOW = wording, cosmetics, noise.
   - `--plain`: one or two sentences a trader reads first on `/logs/bugs`: what is wrong and what
     it means for them, no code names, ids or jargon (for example "After a restart, a strategy
     could buy a stock it already held, and the old trade lost its safety stop.").
   - `--impact`: what it costs a trader, in plain words. `--evidence`: exact ids, rows, times.
   - `--area`: signal_engine, openalgo-core, sandbox, broker, data, strategy, ops.
4. TUNING-IDEA entries never become code changes from fewer than 20 sessions, and frozen strategies
   (see memory) get bug fixes only.

## 2. Fix, verify and close

Working an entry (investigate, fix, prove, close) is the **`resolve`** skill (`/resolve`). This
skill only owns the register and its rules. Two of them bind every fix:

- A fix that has not met its trigger stays `fixed`; `verified` needs a session that exercised the
  fixed path, named in `--verification`.
- An EXECUTION entry with no code fix closes as verified only after the symptom is absent for 5
  sessions in a row with the trigger present (for example, slow broker but no timeout fallout).

## Automatic recording (no AI involved)

`uv run python -m signal_engine.ops track --day D` (run by `run_phase.sh` after each morning check and
by `close_day.sh` before the review) records every FAIL check and failed step of the day through
`store.report` with the same `workflow-finding:<key>` as the page's "Report as bug". It is
idempotent, always writes status `open`, severity MEDIUM (unreviewed default), and skips WARN
results and the derived `phase *` summary rows. Reuse that key scheme for any new automatic
writer, or the same problem gets two entries. Tests that exercise it must pass a temp `path`;
a real run edits `bugs.json`, so `git checkout` that file after a manual trial.

## Unattended auto-fix (Tier 1)

`signal_engine/analysis/autofix.sh` (cron 16:30 weekdays, never during market hours) picks one open
LOW or MEDIUM bug of kind BUG or DATA in area ops, data or signal_engine, and fixes it on a side
branch `autofix/<BUG>` in a separate git worktree. Policy lives in `signal_engine/bugs/autofix.py`
(eligibility, allowed paths, size limits) and is enforced on the resulting diff, never trusted from
the model. Rules that bind any change to it:

- Entries, exits, stops, sizing, order paths, `services/risk/`, config, migrations, broker code and
  the core are never in scope; HIGH bugs and EXECUTION or TUNING-IDEA kinds are Tier 2 (`/resolve`).
- Gates, in order: scope check; the new test must FAIL with the fix set aside and PASS with it;
  ruff clean; main working tree unchanged. Any miss discards the branch, reopens the bug and notes
  why (`autofix attempt` history lines cap it at two tries).
- On success it commits on the side branch only and leaves the bug `in_progress` with the commit
  named: an unmerged fix is not running code. The owner merges, then marks `fixed`.
- Pipeline tests use a stand-in for `claude` (`AUTOFIX_CLAUDE_BIN`) and
  `AUTOFIX_ANYTIME=1`; add bugs for them through the CLI and `git checkout` `bugs.json` after.
- A worktree has no `.env` (git-ignored) and tests need it: symlink it in, and deny the model
  Read/Edit on it. Tracker helpers must run from the main checkout (`env -C`), not
  with the worktree's `PYTHONPATH`, or they write a copy of `bugs.json` inside the worktree.
- After `git stash pop` the restored fix is unstaged: `git add -A` again before committing.
- Headless `claude -p` is given a minimal tool allowlist (`--allowedTools`, `--disallowedTools`),
  not `--dangerously-skip-permissions`; an optional long-lived token is read from
  `~/.config/openalgo/claude_token`.

## Unattended runs (the close-day review run by close_day.sh at 15:36)

The run is read-only. Do not call `add`, `seen`, `note` or `update` yourself. End the response with
one fenced block, info string `bug-tracker`, holding a JSON list; the cron script applies it with
`apply`, which accepts only these actions:

```bug-tracker
[
  {"action": "seen", "id": "BUG-001", "date": "2026-11-02",
   "evidence": ["trades.db row 3501 booked under stop 2611..."], "sources": ["signal_engine/reports/eod-2026-11-02.md"]},
  {"action": "add", "title": "...", "plain": "<one plain sentence>", "kind": "BUG", "severity": "HIGH", "area": "signal_engine",
   "date": "2026-11-02", "impact": "...", "strategies": ["ORB"], "evidence": ["..."], "root_cause": "..."},
  {"action": "note", "id": "BUG-003", "text": "2026-11-02: path exercised (restart 10:41, ORB re-entry declined), held"}
]
```

- One `note` for every `fixed` bug, saying whether the day exercised its path and what happened.
  A person promotes it to verified.
- An empty list `[]` when nothing applies. A missing block is reported in the report's footer.

## Weekly sweep

- Every entry not verified or wontfix: still occurring (days seen this week), fix status, and
  whether a fixed one was exercised.
- Fixed for more than 5 sessions and never exercised: say so, so the owner decides whether to
  force the path in a test or accept it.
- New entries for anything the daily reviews described but did not record.

## Known limitations

- `/api/bugs/<id>/<action>` and `/api/ops/report-finding` refuse a body over 64 KiB (`bug_tracker.MAX_BODY_BYTES`) with 413 before parsing it, including chunked requests. The app still sets no `MAX_CONTENT_LENGTH` (it would also cap uploads elsewhere), so every other route is unbounded as before. The chunked path is tested through the Flask test client, not a live gunicorn.
- The log detector (`ops/logscan.py`) treats yfinance noise (`possibly delisted`, `quoteSummary`, `NIFTYFPI`) as benign on the raw text. A two-letter ticker is masked only in an explicit symbol context (after `symbol`, `ticker`, `instrument`, `order for`, `position in`, or `for XX` before an order or trade word) or when sibling signatures reveal it; a two-letter symbol elsewhere in a message still makes its own signature. New benign noise has to be added to `BENIGN` by hand.
- Scheduling: `analysis/close_day.sh` (cron 15:36 weekdays) is the only script that scans the logs. After the `track` step it runs `track --day D --logs` outside `step()`: the log scan is always a dry run and its candidates go to `reports/logscan-candidates-<day>.txt` (git-ignored) for the owner to review, and a failure is logged to `logs/ops_cron.log` without changing the close-day status or retry. `--logs --write` stays manual until the candidates have been reviewed. `tests/test_ops_alert_scheduling.py` guards both rules.
- The workflow page times come from `workflow_status.DUE`, sent as `schedule` in `/api/ops/workflow`; the page keeps built-in times only as a fallback for an older server. `DUE` describes the cron times, it does not set them (the crontab does). `automation.FINAL_DEADMAN_CLOSE` is derived from `jobs.CRON_LINES`; `automation.RETRY_BEFORE` (09:10) is a cut-off kept as a literal. `tests/test_ops_alert_scheduling.py::test_every_schedule_definition_agrees_with_jobs_cron_lines` fails if `DUE`, `EXPECTED`, `RETRY_BEFORE`, `FINAL_DEADMAN_CLOSE`, `jobs.CRON_LINES` or the cron comments in `run_phase.sh`, `run_upkeep.sh` and `close_day.sh` disagree. The live crontab is audited by the jobs audit, not by that test.
