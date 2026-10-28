# Orca Automation Prompts

Prompts for scheduled, unattended Claude runs in Orca. Times are IST, Mon-Fri unless stated.
Confirm that Orca's scheduler uses IST, not UTC.

## What already runs (do not duplicate)

| Job | Time | Source |
| --- | --- | --- |
| `analysis/eod.sh` | 15:25 | cron |
| `analysis/eod_claude_review.sh` (read-only Claude review) | 15:40 | cron |
| `backtest/backfill_cron.sh` | 15:35 | cron |
| `analysis/weekly.sh` | weekly | manual or cron |

Read-only skills available in `.claude/skills/`: `eod`, `paper-review`, `data-check`, `fd-audit`,
`security-audit`, `status-report`. The prompts below call them instead of re-deriving their work.

## How to configure in Orca

Point each automation at the repo, set the schedule, and paste the guard block followed by the task.
Use Sonnet for prompts 4, 5 and 6. Do not use Opus.

## Guard block (paste at the top of every prompt)

```
GUARD: This is an unattended, read-only run. Do not edit code or config, do not commit,
do not restart services, do not place/modify/cancel orders, do not post to Telegram,
do not mutate any database. Write only the one report file named below. Follow CLAUDE.md
terminology (sandbox/analyzer mode, never "paper trading"). All times are IST. No emojis.
Every claim must cite evidence (file:line, log line, DB row, command output). If a check
cannot run, say "NOT VERIFIED" and why; never report a pass you did not observe.
Finish with: (1) one-line verdict GREEN / AMBER / RED, (2) findings table, (3) a plain-language
summary a trader can read in 30 seconds.
```

## Daily

### 1. Pre-market startup check (08:45)

```
Task: confirm the stack is ready for today's session. Skip with "NSE holiday" if today is not an NSE trading day.
Checks, in order, recording the actual output of each:
1. signal_engine/scripts/openalgoctl.sh status: OpenAlgo, signal engine, breakingtrade poller and watchdog all running; no stale pid/lock (openalgo.pid, openalgo.lock).
2. OpenAlgo health URL responds; broker session is valid today (Indian broker tokens expire ~03:00 IST, so confirm a fresh login happened). Check funds API returns capital > 0.
3. cd /home/anand/github/openalgo && PYTHONPATH=. uv run python -m signal_engine.main --smoke-test (all 7 checks). Do NOT run --dry-run in live mode unless mode_guard confirms it cannot place orders.
4. Websocket proxy (8765) and ZMQ bus (5555) are up; one test quote returns a recent timestamp.
5. Clock: NTP synchronized (WSL2 clock drift has broken TOTP login before).
6. log/errors.jsonl and signal_engine/logs: any ERROR since last evening's close; any traceback during startup.
7. Disk space, trades.db readable, risk counters reset for today, mode (live vs sandbox) matches signal_engine/config.yaml.
8. Scheduled tasks that should have fired overnight (backfill, Task Scheduler heartbeat) did fire.
Report: signal_engine/docs/daily-checks/YYYY-MM-DD-startup.md
```

### 2. Post-open liveness check (09:25)

```
Task: verify the engine is actually alive after the open, not just running.
Check: ticks are flowing (latest tick age under 10s for the traded symbols), the breakingtrade poller completed its first scan and logged it (signal_engine/logs/breakingtrade_poller.log), the first signals/pages of the day reached the pipeline, no 429 or timeout storms in the history/quote paths (rate limits are not acceptable for order timing), and no repeated retry loops. Compare against yesterday's 09:15-09:25 window. Report: signal_engine/docs/daily-checks/YYYY-MM-DD-open.md
```

### 3. Strategy performance review (15:55)

```
Task: run the paper-review skill for "all" strategies for today, and for any strategy running live, review that too.
First read today's output of signal_engine/analysis/eod.sh and eod_claude_review.sh (signal_engine/reports/eod-<today>.md and signal_engine/logs/eod_claude_cron.log) and do not repeat their work; add only what they missed.
For each strategy (live or sandbox, whichever is enabled): signals received, entered, skipped (with reason), fills vs intended price (slippage in bps and in R), exits by type (SL/TP/time/safety), P&L reconciled to broker/sandbox fills, and any gap between expected and actual. Classify each defect BUG / EXECUTION / TUNING-IDEA. Do not suggest tuning from a single day's P&L: strategies in paper observation are frozen. Add a running 20-day table (win rate, avg R, net after India costs) per strategy.
Report: signal_engine/docs/paper-monitoring/YYYY-MM-DD-<STRATEGY>.md as the skill specifies.
```

### 4. Log analysis (16:15)

```
Task: analyze today's logs across everything that writes one. Sources: log/errors.jsonl, log/openalgo_<today>.log, signal_engine/logs/*.log (poller, eod_cron, eod_claude_cron, backfill_cron), signal_engine/log, scripts/*.log (openalgoctl, breakingtradectl), and cron output.
Do: group errors by signature (logger + message pattern), count them, and compare with the last 5 days to separate NEW from recurring. For each NEW or rising signature: first occurrence time, trigger, whether it touched order flow, and a likely cause with evidence. Flag: rate-limit (429), timeouts, reconnects, "database is locked", "too many open files", token expiry, greenlet/lock errors, order rejections, and silent gaps (a job that should log every N minutes but did not). Ignore known noise listed in prior reports; say what you ignored. Never paste secrets or tokens from logs.
Report: signal_engine/docs/daily-checks/YYYY-MM-DD-logs.md
```

### 5. Scheduled-job audit (16:45)

```
Task: prove every scheduled job ran and succeeded today. Read crontab -l and the Windows task scripts in signal_engine/scripts (createTask*.ps1, heartbeat_check.ps1). For each job (eod.sh 15:25, eod_claude_review.sh 15:40, backfill_cron.sh 15:35, the poller start, watchdog, heartbeat) show: expected time, actual start/end from its log, exit status, and output file produced. Flag any job with no log line today, a runtime far from its 10-day median, or a non-zero exit. Report: signal_engine/docs/daily-checks/YYYY-MM-DD-jobs.md
```

### 6. Data freshness (16:30)

```
Task: run the data-check skill for the paper universe and the previous/current NSE session at 1m and D. Report Historify coverage vs NSE calendar, missing sessions, bar-count anomalies (including 361-bar closing-auction sessions), timestamp basis (must be IST), and broker-history vs Historify mismatches for 5 random symbols. Confirm backfill_cron.sh log shows success today. Report: signal_engine/docs/daily-checks/YYYY-MM-DD-data.md
```

## Weekly and monthly

### 7. Weekly strategy health (Sat 10:00)

```
Task: read the newest signal_engine/reports/weekly-*.md (produced by weekly.sh) and the week's paper-monitoring reports. For each strategy: week's trades, net R after India costs, slippage trend, fill rate, skip reasons, and drift against its backtest expectation. Using signal_engine/docs/STRATEGY-ACCEPTANCE-CRITERIA.md, state how many paper-incubation days each strategy has completed and which gates (G0-G9) it still has to pass. State plainly when the sample is too small to conclude anything. Do not recommend parameter changes while a strategy is frozen for observation. Report: signal_engine/reports/weekly-health-YYYY-MM-DD.md
```

### 8. Weekly reconciliation and accounting (Sat 11:00)

```
Task: audit the week's P&L integrity against signal_engine/docs/ACCOUNTING-INVARIANTS.md. Compare trades.db, the strategy P&L ledger, and the sandbox/broker tradebook for each day. List every day with a reconciliation gap, positions held by the broker that trades.db does not know about (see RUNBOOK-UNTRACKED-BROKER-POSITION.md), orphaned SL orders, and any exit that was not preceded by an SL cancel. Known limitation: the sandbox undercounts today_realized_pnl on multi-leg closes; do not report that as new. Report: signal_engine/reports/reconcile-week-YYYY-MM-DD.md
```

### 9. Weekly test and quality health (Sat 22:00)

```
Task: run the full suite once, through the wrapper only: signal_engine/scripts/run_full_tests.sh (use --status first; use --wait if locked; never call pytest on the whole directory, never kill other sessions' processes). Also run uv run ruff check . and report counts only. Report pass/fail/skip counts, new failures vs last week, flaky tests (name them, do not re-run into green), and coverage change on signal_engine/risk.py, executor.py, tracker.py. Report: signal_engine/reports/test-health-YYYY-MM-DD.md
```

### 10. Weekly resource-leak watch (Sun 10:00)

```
Task: use the fd-audit skill's method on the running system. From the last 7 days, trend: process RSS, open file descriptor count, thread count for OpenAlgo, the poller and the signal engine; log and db file growth (log/, signal_engine/logs, *.db, trades.db); errors.jsonl size. Flag anything rising monotonically across days without a restart. Report: signal_engine/reports/resource-trend-YYYY-MM-DD.md
```

### 11. Weekly calendar and config sanity (Sun 18:00)

```
Task: prepare for the coming week. List NSE holidays and special sessions (muhurat, early close), index and stock expiry days, and lot-size or freeze-limit changes for the next 7 days, from the repo data and official sources only (cite the source; mark anything unconfirmed). Check signal_engine/config.yaml and cron/Task Scheduler entries against them: anything scheduled on a holiday, MIS square-off windows (broker 15:00-15:25) vs strategy exit times, and symbols in the universe that were removed from F&O or renamed. Report: signal_engine/reports/week-ahead-YYYY-MM-DD.md
```

### 12. Monthly security and upgrade review (1st, 10:00)

```
Task: run the security-audit skill (it writes the dated xlsx report). Add: uv pip list --outdated filtered to security-relevant packages, a check that no secrets appear in tracked files or logs, .env permissions, and whether a newer OpenAlgo release exists with a breaking change for the order path. Do not upgrade anything. Report: signal_engine/reports/security-YYYY-MM.md
```

## Notes

- Priority: prompts 1, 2 and 3 protect money; add 4 and 5 next; add the weekly set once the daily ones prove useful.
- Telegram alerts are forbidden inside the prompts. To get a ping on RED, grep the report in a small shell wrapper.
- `signal_engine/docs/daily-checks/` does not exist yet. Create it or change the report paths.
- If the login is manual, prompt 1 will correctly report RED. That is the intended signal.
