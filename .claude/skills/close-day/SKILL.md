---
name: close-day
description: The single end-of-day entry point for the OpenAlgo algo system - "/close-day [YYYY-MM-DD]". Checks that today's after-market jobs ran (close_day.sh), then reports how the system worked (flat at close, unprotected or orphan positions, execution health), how every paper strategy performed (gross, charges, net, week to date), risk-limit usage, bugs found or proven fixed (tracker), data readiness for tomorrow, and anything the owner must decide before the next open. Fridays add the weekly report. Use when asked to "close the day", "how did today go", "end of day review", "EOD", "how did the paper strategies do", or "are we ready for tomorrow". Replaces invoking eod, paper-review, execution-health and data-check one by one.
---

# Close the day

One prompt, one report, finished by 16:00 IST. The building blocks keep their own detail:
`eod` (regression triage), `paper-review` (per strategy, weekly), `execution-health`,
`bug-tracker`, `data-check`. This skill decides the order and what goes in the report.

## The schedule this sits on (weekdays, IST)

| Time | What | Where |
| --- | --- | --- |
| 14:45 | Engine time exit, each strategy's own legs | signal_engine |
| 14:55 | Square-off failsafe | Windows task `openAlgoSquareOff` |
| 15:35 | Engine stopped | Windows task `breakingTradeAutoStop` |
| 15:36 | `close_day.sh`: backfill (bg), `eod.sh`, execution health, Friday `weekly.sh`, unattended review | cron, one line |
| ~15:50 | Review report + data readiness + step table | `signal_engine/reports/eod-<day>-claude-review.md` |
| 15:58 | Hard deadline inside `close_day.sh` | |
| 16:00 | OpenAlgo stopped | Windows task `openAlgoAutoStop` |

After 16:00 OpenAlgo is down: a missed tradebook snapshot or backfill cannot be redone until the
next start. Say so; do not pretend the numbers are fill-backed.

## Interactive run (/close-day)

1. **Did the day close?** Read `signal_engine/reports/close-day-<day>-status.tsv`. Every step
   should show exit 0 (`eod` exit 2 = tradebook snapshot failed). Missing file = the cron did not
   run. If it is before 15:58 and OpenAlgo is up, run `./signal_engine/analysis/close_day.sh
   --no-review` for the missing deterministic steps; after 16:00, report what is missing.
2. **Read, do not redo.** If the unattended review exists (`eod-<day>-claude-review.md`), start
   from it: check its claims that matter (P&L, any HIGH item) against the sources, then extend it.
   If it is missing or cut off by the deadline, do the review below yourself.
3. **Fix with the owner.** Interactive runs may fix confirmed bugs per the `bug-tracker` skill
   (state old and new behaviour for trading logic; regression test first; targeted tests).
4. **Reply** with the report below, in chat. Do not write a second report file.

## The report (both modes, in this order)

Lead with the verdict; keep each section short. Every number names its source.

1. **Verdict, three lines.** Did the system run clean (yes / with issues / no)? Today's P&L, gross
   and after charges. Is anything open or unprotected now?
2. **Safety at the close.** Every MIS position flat (eod report "still open", `pages_vs_fills`
   whole-account rebuild), no working orders left, no untracked broker fills (ledger "broker
   fills the engine never sent"), no position that ran without a stop (log: `TRACKING BLIND`,
   `no stop`, `UNCONFIRMED`). Any of these is HIGH and goes first.
3. **Paper strategies, today.** One table: strategy, entries, gross (pages_vs_fills **fills**
   column), charges, net, win/loss, best and worst trade, declines by reason (grouped). Use fills,
   never the engine's `context.pnl` (it double counts partial exits). Note any strategy that did
   not trade and why (config change, filter, retired), from evidence.
4. **Week to date and since start**, per strategy: net so far this week; sessions observed versus
   the 20-session decision threshold; frozen strategies are observed, never tuned (memory).
5. **Execution health.** The verdict line of `execution-health-<day>.md`, and only the incidents
   that touched a trade (timeouts at entry or exit, EXIT UNCERTAIN, stale prices while held).
6. **Risk.** `signal_engine/data/risk.db` table `risk_counters` for the day: daily loss and net
   P&L per strategy against the configured limits in `signal_engine/config.yaml` (`risk:`); a
   limit hit or within 20% of it is called out.
7. **Bugs.** Per the `bug-tracker` skill: new entries, recurrences, and for each `fixed` entry
   whether today exercised its path and held. Name ids; do not re-describe tracked bugs.
8. **Data for tomorrow.** The backfill verify lines (symbols behind must be 0 for both
   intervals). In an interactive run, if anything is behind, use the `data-check` skill.
9. **Tomorrow.** Next session date from `db/openalgo.db` `market_holidays` (NSE) - a holiday
   tomorrow means no warm-up concerns. Code the engine will run: `git status --short --
   signal_engine` and the branch; uncommitted engine changes mean tomorrow runs untested code, so
   say which files. Config changes today (`git log --since <day> -- signal_engine/config.yaml`).
   Open decisions for the owner, one line each.
10. **Fridays: the week.** `weekly.sh` output (`signal_engine/reports/weekly-*.md`) plus the
    `paper-review` skill's "Weekly report" and the `bug-tracker` "Weekly sweep".
11. **In plain words**, 4-8 sentences for a trader: what the system did, what it made or lost after
    charges in a ₹1,00,000-account example, what (if anything) needs attention before the open.

## Unattended run (from close_day.sh)

Same report and order, with these limits:

- Report-only: no file writes, no commits, no database changes, no fixes. Describe fixes.
- Read the deterministic outputs (`eod-<day>.md`, `execution-health-<day>.md`, the status file);
  never re-run them. Do not query `historify.duckdb`: the backfill is writing it.
- Sections 8 (data) and the step table are appended by `close_day.sh` after the backfill ends;
  write "appended below" for section 8.
- Time is bounded: if short, finish sections 1-3 and the bug-tracker block first.
- End with the bug-tracker block (`bug-tracker` skill, "Unattended runs").

## Holidays and partial days

`close_day.sh` exits on weekends and on NSE holidays in OpenAlgo's calendar. If there were no
signals and no trades (engine down all day), say that in the verdict and check why from the
watchdog and engine logs before anything else; an engine that never started is HIGH.
