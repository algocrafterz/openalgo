---
name: paper-review
description: Read-only review of one paper-trading strategy's session (or every paper strategy) for a given IST day - signals, fills, declines, skips, P&L reconciled to the sandbox fills, and defects classified BUG / EXECUTION / TUNING-IDEA - written to signal_engine/docs/paper-monitoring/<day>-<STRATEGY>.md. Use when asked to "review today's paper trades", "paper run review for BREAKOUT-PY", "check strategy X for issues", "P&L does not match", or "report the session results". Not the EOD regression triage (use eod for that).
---

# Paper run review

One run: `/paper-review [STRATEGY|all] [YYYY-MM-DD]`. Defaults: `all`, today (IST).

This skill is **read-only**. It never edits code or config, never restarts the engine, never places,
modifies or cancels orders, and never posts to Telegram. A fix is a proposal for the owner.

## 0. Scope

- The day is IST. A day is a trading session; on a weekend or NSE holiday, say so and stop.
- Strategy names come from `strategy_profiles` in `signal_engine/config.yaml`. `all` means every
  profile that traded or signalled that day (check `trades.db` and the log, not the config alone).
- If the session is still live (before 15:30 IST), state the time the data covers.
- Check the frozen-strategy rules in memory before you suggest anything. A frozen strategy
  (for example EVWAP-EARLY-PY, BREAKOUT v1.0) gets bug fixes only, never tuning from early P&L.

## 1. Read the existing outputs first (do not redo their work)

```bash
DAY=YYYY-MM-DD
ls signal_engine/reports/eod-$DAY*.md                     # cron report and unattended Claude review
PYTHONPATH=. uv run python -m signal_engine.analysis.pages_vs_fills --mode analyze $DAY
PYTHONPATH=. uv run python -m signal_engine.analysis --since $DAY --declined
PYTHONPATH=. uv run python -m signal_engine.analysis.evwap_early_report --day $DAY   # EVWAP-EARLY-PY only
```

`pages_vs_fills` is the P&L reconciliation: the Strategy P&L pages against the raw sandbox fills.
Any FAIL there goes into the review as a BUG or EXECUTION item with the exact numbers.

## 2. Sources for the detail

| What | Where |
| --- | --- |
| Engine log | `signal_engine/logs/signal_engine_analyze_$DAY.log` (grep the strategy tag, `declined`, `skip`, `timeout`, `halted`, `ENTRY_HOLD`) |
| Trades | `signal_engine/data/trades.db` table `trades` (open read-only: `file:...?mode=ro`) |
| Runner signals and skips | `signal_engine/data/runner_state.db` table `shadow_events` (`bar_epoch + 19800` = IST) |
| Sandbox fills | `db/sandbox.db` table `sandbox_trades` (read-only) |
| Strategy rules | the strategy's spec under `signal_engine/strategy_library/`, `strategy_core/`, `validator.py`, `pipeline/` |

Every claim names its source line, row or query. Recompute numbers; do not copy log text as proof.
Convert every timestamp to IST before you compare it with a session time.

## 3. What to check

1. Every signal: dispatched, declined (reason), or skipped before dispatch (gate and value).
2. Every fill: slippage against the signal price, also in R (`abs(entry - sl)` is 1R).
3. Every exit: reason (SL, TP, time exit, square-off), fill, P&L in INR and in R.
4. Totals: realised P&L per strategy and for the account, open positions at the cut-off.
5. Execution health: order and SL timeouts, duplicate or orphan stops, late bars, cycle duration
   against the deadline, operator holds, positions shared by two strategies on one symbol.
6. Spec conformance: each rule the day exercised, and the rules the day did not exercise (say "not checked").
7. Parity eligibility: say if the session counts as a clean session for any parity or evidence count,
   and why not if it does not (partial session, hold, restart, stale data).

## 4. Classify

- **BUG** (HIGH / MEDIUM / LOW): code does not do what the spec or the config says. Prove it with data.
- **EXECUTION** (same severities): broker, sandbox, timing or operations issue. "Working as designed" is allowed.
- **TUNING-IDEA**: a possible parameter or design change. Always give the sample size and the overfit risk.
  Never recommend a change from fewer than 20 sessions.

The same classes and severities are the tracker's. Check `uv run python -m signal_engine.bugs list
--all` first and record every confirmed item there per the **`bug-tracker`** skill (`seen` for a
tracked one, `add` for a new one). The review file names the tracker ids.

## 5. Output

Write `signal_engine/docs/paper-monitoring/<DAY>-<STRATEGY>.md`, one file per strategy, plain text
headings, no emojis:

```
<STRATEGY> paper run review, <DAY> (IST)

Scope and status        read-only, data cut-off time, sources, engine version/fingerprint, universe
1. Trades and signals   one line per signal: bar, symbol, side, entry/sl/tp, outcome, fill, slip, qty
                        closed-trade P&L (INR and R), open positions, strategy total
2. Execution issues     EXECUTION-n (SEVERITY). cause, evidence, impact
3. Logic review         BUG-n (SEVERITY). verified evidence, scale, candidate fix, approval needed
                        spec checks that came out clean; not checked
4. Tuning ideas         TUNING-IDEA-n (value, overfit risk), with the sample size
Summary in plain words  4-8 sentences for a trader with no coding background
```

Then reply in chat with: P&L per strategy, the BUG list with severities, whether the session counts
as clean evidence, and the plain-words summary. Do not commit; the owner decides (see `isolated-commit`).

## Weekly report

For a week (Monday to Friday, IST), one report across every paper strategy, not a file per day:

1. Per day: `pages_vs_fills --mode analyze <day>` (step 1). Use the **fills** column as each
   strategy's gross P&L and its "Estimated charges" block for charges. The engine's own
   `context.pnl` double counts partial exits: a final row's pnl includes its TP legs.
2. Per strategy, a table: days traded, entries, gross, charges, net, and one note (one day carries
   the week, retired or started mid-week, frozen). Counts from `trades` (`status='SUCCESS'`,
   `trade_mode='analyze'`); declines grouped by reason.
3. Strategy behaviour: win rate, average win and loss, exit-type mix (`context.exit_types`).
4. Config changes in the week (`git log --since <monday> -- signal_engine/config.yaml`): a strategy
   that stopped or started trading gets its recorded reason, not a guess.
5. Clean-evidence count: which sessions qualify, and why the others do not.
6. Tracker: the weekly sweep in the `bug-tracker` skill; fix scope ranked by severity, naming ids.
7. Close in plain words with a ₹1,00,000-account example (standing preference).

An interactive run replies in chat. On Fridays the scheduled close-day review includes this
section, next to `weekly.sh`'s rollup (`signal_engine/reports/weekly-<monday>-to-<friday>.md`).
