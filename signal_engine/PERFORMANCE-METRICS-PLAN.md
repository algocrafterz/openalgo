# Performance Metrics Plan (Draft — not implemented)

Status: **draft only**, written 2026-09-21. Implementation deferred to a separate conversation.
This file is the spec to start from then — no code changes here.

## Why

`trades.db`'s `total_pnl` per closed position is now correct and independently verified against
real broker fills (see `PRD.md`'s 2026-09-21 entries — the double-counting and phantom-trade bugs
are fixed). Absolute P&L alone doesn't say much about trade *quality* though: a good day and a
lucky day look the same in a single rupee number. The goal is a small set of standardized,
StonkJournal-style metrics computed from the same trade records, so performance can be judged the
way a trading journal would — win rate, profit factor, expectancy, drawdown — not just "P&L was
up/down today."

## Scope: explicitly excluded

Per user instruction 2026-09-21: **no behavioral-pattern detection.** Not in scope for this round:
disposition effect, revenge-sizing detection, confidence miscalibration, rule-violation tracking,
"win rate drops after N consecutive wins" pattern mining, or anything else that infers trader
psychology. Those are individually complex (each needs its own definition of "rule" and its own
false-positive risk) and not needed right now. This plan is **basic/essential/critical numeric
metrics only** — the kind that are a single deterministic formula over `trades.db`, nothing that
requires judgment calls about what counts as a "pattern."

## Data source

All metrics below are computable from `db.fetch_day_trades()` / the same `trades` table join
`ledger.py` already does, extended to a date range instead of one day. No new data collection is
needed — every metric is a rollup over rows that already exist:
`{symbol, strategy, entry, exit_price, quantity, total_pnl, exit_types}` per closed position, plus
`entry_time`/`exit_time` (already in the table, not currently surfaced by `fetch_day_trades()` —
would need adding to that query's SELECT and returned dict for hold-time/time-of-day metrics).

## Metrics (grouped)

### Core P&L shape
- **Win rate** — wins / decided trades. *Already computed* (day summary, ledger) — just needs
  extending from one day to an arbitrary range.
- **Profit factor** — gross profit / gross loss (absolute value). Undefined (report as `∞` or
  `—`) when gross loss is 0. New.
- **Average win / average loss** — mean `total_pnl` of winners, mean of losers, in both ₹ and
  R-multiple (R already computed per trade via `_compute_r`). New.
- **Expectancy per trade** — `(win_rate × avg_win_R) − (loss_rate × avg_loss_R)`, in R. The single
  number that answers "is this edge positive per trade, on average." New.
- **Net P&L / Gross P&L** — already exist (ledger's "gross P&L", day summary's "Net"). Keep as-is.

### Risk / drawdown
- **Max drawdown** — largest peak-to-trough decline on the cumulative-P&L equity curve (sequence
  of `total_pnl` ordered by `exit_time` or `executed_at`, running cumulative sum, track the
  running max and the largest drop below it). Needs an explicit equity-curve series, which nothing
  currently builds — new, and the one metric here needing actual sequencing logic, not just a
  rollup.
- **Max consecutive win streak / max consecutive loss streak** — a plain count over the ordered
  win/loss sequence. Not behavioral inference (no claim about *why* the streak happened) — just a
  count, in scope. New.

### Timing
- **Hold time** — median/mean minutes held. *Partially exists* (`ledger.py`'s "EXECUTION QUALITY"
  section, single-day only) — extend to a range.
- **Time-of-day breakdown** — win rate / avg R bucketed by entry hour (or half-hour, given intraday
  trades cluster 09:15-15:30 IST). New; needs `entry_time` surfaced (see Data source above).
- **Day-of-week breakdown** — same, bucketed by weekday. New, same prerequisite.

### Segmentation (already partially exists, extend don't rebuild)
- **Per-strategy** — `_comparison_rows()` in `notifier.py` and `weekly_review.py`'s
  `strategy_table()` already do trades/W/L/win%/avg R/net ₹ per strategy, just scoped to one day
  or one week. Reuse, don't reimplement — generalize to an arbitrary date range if that's not
  already possible with `--since`/`--until`.
- **Per-symbol** — win rate, trade count, net P&L, avg R per symbol. New, same shape as the
  per-strategy table.
- **"Market-type segmentation"** (StonkJournal's term) — not directly applicable; this system only
  trades NSE/BSE F&O-eligible equities. Closest equivalent already exists: per-strategy (BREAKOUT
  vs BREAKINGTRADE vs RSI-TP-MR etc.) already segments by "type" of setup. No new metric needed.

### Explicitly NOT included (StonkJournal features, out of scope this round)
- Rule Compliance Score, disposition-effect detection, revenge-sizing detection, confidence
  miscalibration, streak-triggered win-rate-drop analysis, per-rule sparklines, "examples from your
  trades" pattern callouts. Revisit only if/when there's an actual "rules" concept in
  `config.yaml`/`strategy_profiles` to measure compliance against — there isn't one now.

## Where this would surface (open question for the implementation conversation)

Three non-exclusive options, not decided yet:
1. **Extend `weekly_review.py`** — it already does date-range rollups and has the
   verified/unverified P&L discipline this needs to inherit (a metric computed over unverified-fill
   days would be misleading the same way a raw P&L number would). Natural home for win
   rate/profit factor/expectancy/drawdown/streaks at the weekly cadence.
2. **New script**, e.g. `signal_engine/analysis/performance_metrics.py`, callable standalone
   (`--since`/`--until`) for an ad-hoc range, not tied to the daily/weekly cron cadence.
3. **A `/tools` dashboard page** (per `CLAUDE.md`'s tools registry, `frontend/src/lib/tools.ts`) if
   this should be visually browsable rather than markdown-report-only. Bigger effort — a full
   frontend page, not just a Python report — decide only if the markdown reports turn out to be
   insufficient in practice.

Recommendation when this gets picked back up: start with (2), a standalone script reusing
`ledger.py`'s join and `weekly_review.py`'s verified/unverified split, output as a markdown table
matching the existing EOD/weekly report style. Wire into `weekly.sh` (option 1) once the metric
set is validated, rather than building the cron integration before the metrics themselves are
trusted. Only build (3) if there's a concrete reason a markdown report isn't enough.

## Test/verification plan (for whenever this is implemented)

Same standard as today's fixes: every formula verified by hand against a real day's trades before
trusting it (see `test_close_accounting.py`'s pattern of using real symbols/quantities/order_ids
from an actual session, not synthetic round numbers that could hide a sign error or off-by-one).
Cross-check profit factor / expectancy / drawdown against `2026-09-21`'s corrected 5-trade day
(net +1,619.65, 4W/1L, 80% win rate) as the first test fixture — the numbers are already known and
independently verified in `PRD.md`.
