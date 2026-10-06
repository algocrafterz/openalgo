---
id: IDEA-029
title: VWAP 2SD range mean reversion
stage: backtest
research_state: rejected
rejection_basis: no-gross-edge
result: "NO_EDGE on the published dataset nse-intraday-5m-20261006-001 (run 2026-10-06-vwap-2sd-range-mean-reversion-v1): gross -4.18 bps and net -20.18 bps over 26,036 trades, negative in every year, 4.5 bps per trade below the random-entry baseline (excess t -3.58). An independent Opus review rebuilt the strategy from the spec and confirmed the result."
strategy_category: Mean Reversion
strategy_tags: [vwap, vwap-bands, range-regime, intraday, long-short]
horizon: intraday
backtest_module: backtest/strategies/vwap_reversion.py
source:
  type: self
  author: researcher
  ref: _inbox/vwap-2sd-range-mean-reversion.txt
  captured: 2026-10-05
summary: In a range-bound regime (15m ADX and flat EMA20), fade a 5m excursion beyond the session VWAP 2SD band once price closes back inside the band and across EMA9; target VWAP, stop beyond the extreme.
revisit_when: Not by tuning this version. Only a new, pre-specified hypothesis, recorded as a separate idea.
last_reviewed: 2026-10-06
---

# IDEA-029 - VWAP 2SD range mean reversion

## Idea

The researcher's draft is in `_inbox/vwap-2sd-range-mean-reversion.txt`. The rules, with the twelve
draft decisions resolved, are in `spec.yaml`.

## Analysis and backtest

Clean result (the only one): run `2026-10-06-vwap-2sd-range-mean-reversion-v1`, frozen version v1
(spec commit `a7a3c7de9`), published dataset `nse-intraday-5m-20261006-001`, code `3840244cd`,
203 symbols, 2,380 traded sessions. Run status VALID, verdict NO_EDGE. One counted trial.

| Metric | Value |
| --- | ---: |
| Trades | 26,036 (16,012 long, 10,024 short) |
| Gross / net per trade | -4.18 / -20.18 bps (16 bps flat round trip) |
| Statutory MIS model | 16.27 bps (8.27 statutory + 8 slippage); net -0.29R, -0.41R at 2x slippage |
| In-sample gross / net | -4.99 / -20.99 bps |
| Out-of-sample gross / net (from 2023-11-01) | -2.01 / -18.01 bps |
| Random-entry baseline gross | -0.17 bps; excess -4.50 bps per trade, t -3.58 |
| Win rate / payoff | 39% / 0.38 |
| Hard fails | G1.2, G1.3, G2.2, G4.4, G4.5 |

By exit: target 17,328 trades at +23.5 bps gross, stop 6,858 at -70.7, gap through stop 93 at
-77.7, time exit 1,757 at -13.6. Longs -6.25 bps gross, shorts -0.88. Gross is negative in every
year. The 0.5% minimum stop removed 47,337 of 73,596 signals (STOP_TOO_TIGHT).

The loss is the payoff shape: the VWAP target is close and moves, the stop is beyond the extreme.
The strategy loses before costs, so no cost or execution assumption rescues it.

### Independent review (Opus, 2026-10-06)

Verdict: trustworthy with caveats. The reviewer rebuilt the strategy from the spec text alone
(float64, no shared code) and reproduced the recorded trades exactly on 27 of 28 sampled symbols
(about 15% of trades; sample gross -3.69 bps). The 28th differed by one trade (A1). No lookahead
and no bar misalignment. Bias bounds on the sample: target fills first on a both-levels bar -3.48
bps; target needs a 0.05 trade-through -5.77 bps; no 0.5% stop floor -2.57 bps; skip-at-fill at
most about 0.9 bps. None makes gross positive. The one optimistic assumption (touch fills at VWAP)
means the true gross is worse.

Defects found. None changes the verdict; v1 is not rerun.

- A1 (low): `backtest/types.py` `Ctx` stores decision columns as float32. A float64 tie can flip
  a comparison (JINDALSTEL 2019-09-03 11:05: a VWAP touch that float64 does not see). 1 trade in
  about 3,900. Generic; affects every strategy.
- A2 (low): the Rs 10 eligibility price floor runs on back-adjusted intraday prices
  (`adjusted_prices` is true only for swing runs), so a later corporate action can exclude
  earlier sessions. 3,139 sessions (about 0.7%; ADANIPOWER, IDEA, SUZLON, CGPOWER). Generic.
- A3 (info): `strategy_core/vwap_reversion.py` returns on the first confirmation, so the opposite
  side is not evaluated on that bar. The rebuild that evaluates both sides matched exactly.
- C1 (low): the spec's "first fill at the 09:20 open" is unreachable; the extreme bar cannot
  confirm itself. Earliest entry is 09:25.
- C2 (low): the 14:45 time exit fills at the close of the 14:45 bar (14:50).
- C3 (low): the headline net uses the flat 16 bps, not the statutory model the spec names (it is
  in `cost_model`); the 32 bps secondary reference line is not reported. Gross is unaffected.
- D1 (medium, execution): targets fill on a touch, with no trade-through. Optimistic by about
  2 bps gross.
- D5 (suspected): the rate table charges 0.03% brokerage capped at Rs 20 per order; if the
  deployed broker's plan has zero intraday brokerage, cost is overstated by about 4.7 bps.
  Not checked.
- Open, not verified: the other 175 symbols; real fill quality at VWAP; survivorship direction;
  sensitivity to bars built from fewer than 5 minutes; a day-clustered t-stat.

## Data contract (approved 2026-10-06, in spec.yaml)

What the strategy needs from `nse-intraday-5m-20261006-001`, enforced at preflight and on read.
It goes into `spec.yaml` as written here when the spec is frozen.

```yaml
data:
  frequency: 5m
  field_families: [price, volume]
  min_n_minutes: 1
  complete_bins: {first: "09:15", last: "15:10"}
```

- **Price** (required): `price_valid`. OHLC of every bar the strategy reads is canonical NSE.
- **Volume** (required): `volume_valid`. Session VWAP and its SD bands are volume weighted, so a
  session with invalid volume is not used. Volume is never withheld-and-served for this strategy.
- **final_bar** (not required): the strategy reads no bar after 15:10 (entries end earlier and the
  profile's MIS exit is 14:45), and its 15m series ends with the 15:00-15:15 bar, so the closing
  auction print never enters a signal.
- **Bin completeness**: all 72 5m bins from 09:15 to 15:10 must exist. A missing bin breaks the
  bar count of EMA9, ATR14 and the 15m aggregation.
- **min_n_minutes = 1**: an availability threshold, not a statement that a 5m bar built from
  fewer minutes is as reliable as a full one. `n_minutes` is kept on every bar and its distribution
  is reported, so sensitivity at 2, 3 and 5 can be tested later as a separate experiment (not now).
  Any published bar is usable. Measured on the price+volume-available
  sessions: n_minutes < 5 falls from about 10% of bars (2017-2019) to under 0.5% (2025-2026) and is
  concentrated in the least liquid names (OFSS, BAJAJHLDNG, FORCEMOT), which reads as minutes with
  no trade rather than lost data. A bar from fewer minutes has the right OHLCV for the trades that
  happened. Session exclusion by threshold (with complete bins): 1 -> 2.5%, 2 -> 4.9%, 3 -> 7.6%,
  5 -> 19.4% (2017: 6.9%, 12.5%, 18.4%, 38.7%). A higher minimum is a liquidity filter, not a
  data-quality rule; if wanted it belongs in the strategy's filters.
- **When the contract is not met**: the symbol-session is not traded and supplies no signal; nothing
  is filled, repaired or interpolated. Excluded symbol-sessions are listed in the preflight report
  with their reason and counted by year in the run record. Preflight is NOT_READY when no symbol
  is eligible. Indicator series continue across an excluded session (decision 11).
- **Enforcement**: the spec schema validates the `data:` block, and the same session-exclusion
  rule runs at preflight and on read.

## Decision log

- 2026-10-05: recorded as blocked (block_reason missing-published-intraday-dataset). Blocked is a data-readiness state, not research evidence: not rejected, not no-edge, not inconclusive, not shelved. The legacy Historify minute data is not used for a recorded run.
- 2026-10-05: plan: build the minimum 1m -> 5m published intraday dataset, then resolve the open decisions, freeze v1, preflight and run once.
- 2026-10-06: unblocked (research_state active): `nse-intraday-5m-20261006-001` is published. Data contract proposed (price + volume, complete 09:15-15:10 bins, min_n_minutes 1); the twelve decisions go to review before the freeze.
- 2026-10-06: the twelve decisions approved with amendments, and written into `spec.yaml`:
  - (1) costs are the platform per-trade India MIS model (statutory lines plus 4 bps slippage per side) at a fixed Rs 1,00,000 notional (`sizing.basis: fixed_notional`). The run records the charge lines, slippage, notional, product and cost-model version, and stresses slippage at 1.5x and 2x. The draft's 32 bps is a secondary reference line only, not a gate.
  - (2) to (10) and (12) as recommended.
  - (11) reworded: stateful indicators are continuous across sessions, and an excluded session contributes no observation. Warm-up is at least 100 completed 15m bars from eligible sessions per symbol, not "the first four sessions".
  - Added an explicit information boundary (`information_boundary` in the spec), covered by regression tests.
  - Stop ATR: the 0.20 ATR stop buffer uses ATR14_5m at the confirmation bar's close. The draft did not name the bar; this is the decision point.
  - A deeper extreme that fails the band or displacement test cancels the setup, so the stop is never left inside a traded-through range.
- 2026-10-06: five implementation gaps found in review, decided before the freeze. These replace the two choices above that the draft left open.
  - (1) The stop buffer uses ATR14_5m at the extreme bar. A deeper extreme restates both the extreme and the ATR.
  - (2) A deeper extreme that fails the band or displacement test cancels the setup.
  - (3) A would-be confirmation with an invalid regime, or before warm-up, cancels the setup. It is not carried to a later bar.
  - (4) A confirmation consumes only its own setup, also when the 2-trade cap blocks it. An opposite setup keeps its state. A fill cancels any remaining setup, because of the existing re-entry rule.
  - (5) The live validator's minimum stop distance is an execution constraint, not a filter. A new tag with no `strategy_profiles` override gets the global `risk.min_sl_pct` of 0.5%, so the baseline uses 0.5%, not 0.2% (BREAKOUT's override). Rejected signals are counted as STOP_TOO_TIGHT.
- 2026-10-06: first run attempt refused at load, before any backtest (no run, no trial). The cause was outside the strategy.
  - The generic load-time completeness audit assumed a fixed 75-bar 5m session (09:15-15:29).
  - The published dataset's own session template has 73 bins from 2026-08-03 (closing auction), so 8,276 complete sessions (2.07%) counted as incomplete, above the 2% limit.
  - Fixed generically: every intraday run now measures completeness against the dataset's session template. Daily runs and the legacy fixed count are unchanged.
  - Verified on the loaded data: every 73-bar session has all 72 bins from 09:15 to 15:10 plus the 15:25 auction bar.
  - The strategy and its data contract are unchanged.
- 2026-10-06: second run attempt refused at load, before any backtest (no run, no trial). The cause was outside the strategy.
  - The run calendar was derived from the bars with an 80% symbol quorum, and had to equal the official NSE calendar.
  - Fixed generically: the official calendar now owns the run calendar. Bars on a non-session still reject the run. Empty official sessions are named.
  - On this data: 2,408 trading days and 9 Muhurat sessions listed out of scope. 8 empty sessions are named (5 ALL_SYMBOLS_INVALID; 3 ALL_EXCLUDED_BY_CONTRACT, which are the 2020-03-13 and 2020-03-23 halts and the 2024-05-18 special session). 18 sessions with valid data that the quorum had dropped are now kept.
  - The strategy and its data contract are unchanged.
- 2026-10-06: the single baseline ran on the third attempt (run `2026-10-06-vwap-2sd-range-mean-reversion-v1`, trial 1): VALID, NO_EDGE, gross -4.18 bps, net -20.18 bps.
- 2026-10-06: independent Opus review: trustworthy with caveats; the defects above are logged, none changes the sign. Rejected (no-gross-edge). v1 is not tuned or rerun.
