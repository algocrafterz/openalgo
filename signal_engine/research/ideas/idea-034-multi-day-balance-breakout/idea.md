---
id: IDEA-034
title: Multi-day balance breakout (auction market theory, intraday)
stage: backtest
research_state: weak_evidence
strategy_category: Breakout
strategy_tags: [market-profile, value-area, balance, breakout, measured-move]
horizon: intraday
source:
  type: self
  author: Owner (auction market theory)
  ref: _inbox/multi-day-balance-breakout.txt
  captured: 2026-10-28
result: Gross +8.6 bps (t 3.2) beats both placebos but not the 11.55 bps cost; net -3.0 bps, fails G1.2 in and out of sample.
summary: Find stocks whose last N completed sessions form a balance (overlapping 70 percent value areas, composite range narrow versus daily ATR14), then trade the first accepted breakout of the composite range after 10:15 and before 13:00. Stop back inside balance, target one balance height from the broken edge, time exit 14:45.
backtest_module: backtest/strategies/balance_breakout.py
revisit_when: 'A cheaper execution path than 11.55 bps round trip exists, or a point-in-time universe or 1m data allows a materially different balance definition; the pre-registered grid is exhausted.'
last_reviewed: 2026-10-28
---

# IDEA-034 - Multi-day balance breakout

## Idea

Auction theory says a stock that has rotated inside an overlapping range for several sessions has
agreed on value; price accepted outside that range is a move to a new value area. Every level is
computed from COMPLETED prior sessions, so it is known at 09:15, before the move. This is the
opposite of the BreakingTrade scanners, which pick stocks after the move.

## Pre-registration (written before any backtest was run, 2026-10-28)

All times IST. All decisions use only data available at the decision time.

### Daily profile (per completed session, continuous trading only, bars stamped before 15:15)

- Volume at price from 5m bars (the only intraday frequency in the published datasets). Each bar's
  volume is spread evenly over the price bins its High-Low range touches (not close-only).
- Bin width: 0.05 percent of the session's mean close, floored at the 0.05 tick.
- POC = bin with the most volume (ties: the bin nearest the session's volume-weighted mean).
- 70 percent value area: expand from the POC, each step adding the side whose next two bins hold
  more volume, until at least 70 percent of volume is inside. VAH = top edge, VAL = bottom edge.
- A session with fewer than 60 continuous 5m bars (Muhurat, half days, bad data) is not a balance
  day and is skipped as history.
- Daily ATR14: simple mean of the true range of the 14 sessions before the trade day (session
  high/low/close from the same continuous bars).

### Balance on trade day t (computable at 09:15 from sessions before t)

Take the last N completed sessions, N in {3, 5}. Balance holds when ALL are true:

1. Consecutive value areas overlap: for each adjacent pair, intersection width / narrower VA width
   is at least 0.5.
2. Composite range height H = max(VAH) - min(VAL) over the N sessions is at most k x ATR14, k in
   {1.0, 1.5}.

Composite high = max(VAH), composite low = min(VAL).

### Breakout (primary trigger: 30-minute close)

- 30-minute periods are anchored at 09:15. Signal periods end 10:15, 10:45, 11:15, 11:45, 12:15,
  12:45 (the period closes at or after 10:15 and by 13:00).
- Long: the 30-minute period close is above the composite high AND the previous 30-minute period
  close (09:45 for the first) was at or below it (a fresh cross, so gap-away opens that never
  crossed are not chased). Short mirrors below the composite low.
- Entry at the next 5m bar open after the signal bar closes. One trade per stock per day: the
  first accepted breakout, either side.
- Alternative trigger: two consecutive 5m closes beyond the edge (both bars closing at or after
  10:15, the second closing by 13:00), with the 5m close before the first one at or inside the edge.

### Exits

- Primary stop ("edge"): composite edge -/+ 0.25 x ATR14 (long: high - buffer; short: low + buffer).
  Alternative stop ("mid"): composite midpoint.
- Target: measured move, the broken edge projected by H (long: composite high + H).
- Time exit: 14:45 (Flattrade MIS square-off is 15:00). If the next-bar fill is already at or beyond
  the stop or target the trade is skipped, not taken.
- Sizing: 1 percent risk, MIS, as the live engine; costs are the repo statutory model (Flattrade,
  primary scenario) plus the 4 bps per side slippage default.

### Trial grid (6 configurations, all reported)

| # | N | k | trigger | stop |
| --- | --- | --- | --- | --- |
| 1 (primary) | 5 | 1.5 | 30m close | edge |
| 2 | 5 | 1.0 | 30m close | edge |
| 3 | 3 | 1.5 | 30m close | edge |
| 4 | 3 | 1.0 | 30m close | edge |
| 5 | 5 | 1.5 | two 5m closes | edge |
| 6 | 5 | 1.5 | 30m close | mid |

The primary is configuration 1, fixed before the run. n_trials = 6. Placebos are not trials.

### Placebos (same stocks, same exits, same period)

- P1 non-balance: the same breakout rule on the N=5 composite range, but with H > 1.5 x ATR14 (not
  a balance). Isolates what the balance condition adds.
- P2 random entry: on days where the primary balance holds, enter at a random one of the six signal
  slots, random side, with the same stop distance (0.25 x ATR14) and target distance (H) measured
  from the entry. Isolates what the breakout trigger adds. Seeded, deterministic.

### Validation

Hold-out from the run system (last 30 percent of sessions out of sample). Report gross and net bps
and R, t (clustered by day), profit factor, max drawdown, trades, long/short split, by year, and
the acceptance criteria gates. Verdict stops at the first HARD fail.

## Analysis and backtest

See `RESULTS.md` in this folder. Recorded run: `runs/2026-10-28-multi-day-balance-breakout-v1.json` (non-reproducible, dirty tree). Gross +8.6 bps, net -3.0 bps, t -0.99; fails G1.2.

## Decision log

- 2026-10-28: onboarded and pre-registered before any backtest.
- 2026-10-28: run and evaluated. Gross edge real but below cost; fails G1.2. research_state weak_evidence. No paper test.
