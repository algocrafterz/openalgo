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
result: Gross +8.6 bps (t 3.2) beats both placebos but not the 11.55 bps cost; net -3.0 bps, fails G1.2. v2 wider balance (H >= 1.5 percent) net -2.1 bps, IS and OOS negative, fails too. v3 (15-minute limit at the broken edge) is recorded forward from 2026-10-29 by a no-order paper recorder; see PAPER-PLAN.md.
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

## v2 pre-registration: wider balance (written 2026-10-28, before any v2 result)

Rationale: the round trip costs a roughly fixed 11.55 bps whatever the trade, so a balance whose
measured-move target is large relative to price should keep more of its gross edge after cost.

- Everything identical to v1 configuration 1: N 5, k 1.5, 30-minute close trigger, edge stop,
  70 percent value area, first breakout per stock per day, 14:45 time exit, measured-move target H.
- New parameter `min_height_pct` (default 0.0 = v1 unchanged): the trade is taken only if
  H / composite-edge price x 100 >= min_height_pct, where the edge is the one being broken
  (composite high for a long, composite low for a short). H and the edge come only from the 5
  completed prior sessions, so the filter is known at 09:15 (no look-ahead).
- Primary: min_height_pct = 1.5. Secondary trials, all reported, none chosen after the fact:
  2.0 and 2.5.
- Descriptive only: v1 trades bucketed by H% (<1.0, 1.0-1.5, 1.5-2.0, 2.0-2.5, >=2.5) with gross
  and net bps.
- Placebo: P1 non-balance (H > 1.5 x ATR14) with the same min_height_pct = 1.5 filter.
- Decision rule: v2 passes only if the primary is net-positive in sample AND out of sample under
  the primary cost scenario, AND its gross beats the P1 placebo under the same H% filter.
- Honesty: the 2023-11-08 hold-out was already seen by v1, so it is NOT a clean hold-out for v2.
  n_trials grows from 6 to 9 (placebos are not trials).
- Implementation note (after the pre-registration, no rule change): a recorded run binds the
  strategy class defaults to the frozen spec, so the shipped default is the v2 primary
  (min_height_pct 1.5); min_height_pct 0.0 reproduces v1. The v1 run's own frozen spec has no
  such parameter, so `--reproduce` of the v1 run now fails parameter binding.

## v3 forward paper (frozen 2026-10-28)

v3 = the v2 primary with a limit entry at the broken edge, live 15 minutes, filled only when a 1m
bar trades one tick through it (the limit-order study's L1 15m). Its rules, acceptance criteria
and monitoring are frozen in `PAPER-PLAN.md`; `paper/FROZEN.json` holds the fingerprint the
recorder checks. The recorder (`analysis/idea034_paper.py`) places no orders: it records every
eligible signal, filled or not, with its modelled fill and exits, into its own ledger.

Stage stays `backtest`: the registry allows `paper` only with a promoted run, and v3 has none.
The forward record is the hold-out, from 2026-10-29. The years before it were already examined by
v1, v2 and the limit-order study and are not prospective evidence. IB% is recorded as information
only. A PASS on the frozen criteria permits only a separately approved sandbox-order stage.

## Analysis and backtest

See `RESULTS.md` in this folder. Recorded run: `runs/2026-10-28-multi-day-balance-breakout-v1.json` (non-reproducible, dirty tree). Gross +8.6 bps, net -3.0 bps, t -0.99; fails G1.2.

## Decision log

- 2026-10-28: onboarded and pre-registered before any backtest.
- 2026-10-28: run and evaluated. Gross edge real but below cost; fails G1.2. research_state weak_evidence. No paper test.
- 2026-10-28: v2 wider balance (min_height_pct 1.5, trials 2.0 and 2.5) pre-registered and run. Net -2.11 bps (IS -1.30, OOS -3.83); fails the v2 decision rule. n_trials 9. No paper test.
- 2026-10-28: owner approved v3 forward paper preparation. Rules, acceptance criteria and monitoring frozen in PAPER-PLAN.md before any hold-out session; rules moved to strategy_core/balance_breakout.py (backtest output unchanged); recorder analysis/idea034_paper.py places no orders. Hold-out from 2026-10-29. No tuning during the test.
- 2026-10-29: frozen v3 replayed on history through the recorder's own code (no ledger writes, no frozen change). Raw Historify: +0.02 bps per signal, recorder status KILL; adjusted data: +2.32 bps. Limit beats market on identical signals in every run. Historical evidence only, not out-of-sample proof. No schedule installed; forward recording is manual and optional. See RESULTS.md and replay/README.md.
