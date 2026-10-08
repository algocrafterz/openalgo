---
id: IDEA-032
title: EMA-VWAP ATR early displacement
stage: inbox
research_state: evidence_against_edge
strategy_category: Other
strategy_tags: [ema, vwap, atr, early-entry, displacement, intraday, long-short]
horizon: intraday
source:
  type: self
  author: owner
  ref: _inbox/ema-vwap-atr-early-displacement.txt
  captured: 2026-10-08
summary: On 5m NSE bars, enter long when (EMA9 - session VWAP) / ATR14 crosses up through -0.50 and short when it crosses down through +0.50, ahead of the EMA9/VWAP crossover, and exit when displacement reaches zero (the crossover itself).
result: Not supported. Benchmark A is -2.3 bps gross and -13.9 bps net per trade over 498,811 trades, negative in every split, and the signal selects no continuation beyond the state placebo.
revisit_when: 'Only with a materially different signal definition, not a threshold, timeframe, stop or target change of this one. Read evidence/phase12-evidence.md first and acknowledge it in the new idea.'
last_reviewed: 2026-10-08
---

# IDEA-032 - EMA-VWAP ATR early displacement

Status: research_state evidence_against_edge (2026-10-08). The spec was never frozen and the Phase 1
and 2 results are exploratory, not a recorded run. Evidence and method: `evidence/phase12-evidence.md`.
Phase 3 was cancelled by the owner.

## Idea

    displacement = (EMA9(close) - session VWAP(hlc3)) / ATR14

- Long: previous displacement <= -0.50 and current displacement > -0.50.
- Short: previous displacement >= +0.50 and current displacement < +0.50.
- Zero displacement is the actual EMA9/VWAP crossover. It is the benchmark exit, not an entry.
- WATCH levels at -0.35 and +0.35 are chart and alert aids only. They never generate trades.
- Excluded from the baseline: ADX, DI, EMA slope, VWAP slope, any other momentum filter.
- Baseline timeframe is 5m. 3m and 1m are separate future ideas, not mixed into this one.

The hypothesis: the EMA9/VWAP gap starts closing before the crossover, so entering at 0.5 ATR
before zero gives a better entry than waiting for the cross. The first benchmark isolates this by
holding the exit at the crossover with no stop or target.

## Analysis and backtest

Result: the hypothesis that +/-0.50 ATR EMA9/VWAP displacement gives useful early-entry information
is not supported. Details, tables and method are in `evidence/phase12-evidence.md`; machine-readable
numbers in `evidence/phase12-results.json`.

- Benchmark A (entry at the early cross, exit at the zero cross, next-open fills, no stop) on 203 F&O
  symbols, 2017 to 2026, 498,811 trades: gross -2.3 bps, net -13.9 bps per trade, day-clustered t -50.
- The loss holds for long and short, in-sample and out-of-sample, every year, and all 203 symbols.
  The sample is large, so lack of data is not the concern.
- No selective continuation after the zero cross: against a direction-matched state placebo the real
  signal continues slightly less (post-zero incremental MFE 1.96 against 2.07 ATR).
- MAE shows no stop boundary separating winners from losers, so a TP/SL grid is not justified.

Scope of the conclusion: it is about this signal definition only. It is not a verdict on EMA/VWAP
methods in general. The earlier plain EMA9 x VWAP cross result (memory: disqualified) points the same
way but was a different test.

Prior evidence this was read against: the plain 9 EMA x VWAP cross on 208 NSE F&O names, 5m, 59
sessions, about zero gross, and an "anticipate the cross 1 to 2 bars early" variant that was better
in-sample and worse out-of-sample (`backtest/strategies/ema9_vwap.py`, header of
`strategy_library/intraday/ema9-vwap/ema9-vwap.pine`).

## Decision log
- 2026-10-08: 5m is the baseline timeframe; EMA9, session VWAP on hlc3 and ATR14 are the inputs.
- 2026-10-08: Early thresholds -0.50 long and +0.50 short. Zero is not an entry. WATCH levels are not trades.
- 2026-10-08: ADX, DI and slope filters excluded from the baseline. 3m and 1m deferred.
- 2026-10-08: First benchmark exits at zero displacement, no stop, no take profit, fixed notional sizing (needed because stop is none).
- 2026-10-08 owner: DO NOT FREEZE v1. Zero-cross is Benchmark A, one exit variant, not the presumed final strategy. TCS 5m charts motivate testing continuation after the zero cross; they are not evidence for any stop or target and must not be used to pick one.
- 2026-10-08 D1 resolved: existing F&O universe allowed for the benchmark with the survivorship limitation disclosed; prefer a historically valid NSE equity universe for final evaluation if the framework supports one.
- 2026-10-08 D2 resolved: signal at the completed 5m bar close, entry at the next eligible bar open. No fill at an unavailable intrabar price.
- 2026-10-08 D3 resolved: the profile standing time exit is a fallback only and is not evidence for the strategy. Report how many trades reach it without hitting the primary exit (zero cross for A, TP or SL for B).
- 2026-10-08 D4 resolved: EMA9 and ATR14 carry across sessions. Session VWAP resets each session.
- 2026-10-08 D5 resolved: prior_variants 1 and chart_peeking true stay.
- 2026-10-08 D6 resolved: Rs 1,00,000 fixed notional is for the no-stop benchmark only and is not the final sizing. Revisit risk-based sizing after the stop structure is chosen.
- 2026-10-08 research plan (no trailing stop, no extra filters, no ADX/DI/slope):
  - Phase 1: does the +/-0.50 ATR early signal have positive expectancy at all.
  - Phase 2: MFE/MAE per signal from the actual entry fill: MFE and MAE to the session cutoff, price at the zero cross, MFE before and after the cross, MAE before the cross, bars from signal to cross and to MFE, share of signals that reach zero, share reaching R multiples before given adverse levels. Report stop distance against the MAE distribution.
  - Phase 3: Benchmark A (zero cross) against a fixed ATR grid. ATR is captured at entry and fixed for the trade. Stops 0.75, 1.0, 1.25 ATR; targets 1.0, 1.5, 2.0, 2.5 ATR; results also in R.
  - Phase 4: choose a stable region across stocks, periods, IS/OOS, long and short, regimes. Never the single best cell.
  - Phase 5: out-of-sample validation, then consider freezing.
  - Evidence of continuation after zero is judged from the MFE distribution first.
- 2026-10-08 engine capability check (read only): backtest/indicators.py already has ema, session_vwap (hlc3, resets by session) and atr (Wilder rma), and prepare() runs over the full per-symbol frame so EMA and ATR carry across sessions. The displacement needs a new adapter only, backtest/strategies/ema_vwap_atr_displacement.py, with no change to the signal definition. Two gaps: (1) Strategy.custom_exit closes at the bar CLOSE, but this spec fills exits at the NEXT bar open, so the exit fill timing must be reconciled before Benchmark A is run; (2) no MFE/MAE tooling exists under backtest/, so Phase 2 needs a small diagnostic. The fixed-ATR stop and target at entry fit the existing EntrySignal sl and tp fields.
- 2026-10-08 owner: engine change approved: custom exit fills at the NEXT bar open via RunConfig.custom_exit_fill = "next_open" (default "bar_close" keeps older behavior); no-stop trades carry gross_ret and net_ret.
- 2026-10-08 owner: first session bar cannot signal. Session and entry cutoff come from the profile: signal bars 09:20 to 14:40, fills to the 14:45 open, standing time exit at the 14:45 bar close (14:50), IST. The 14:45-open entry edge case kept with no minimum hold (0.58 percent of trades, no distortion).
- 2026-10-08 Phase 1 and 2 run on the full published universe: negative gross and net, in every split. See evidence/phase12-evidence.md.
- 2026-10-08 owner: stop. research_state set to evidence_against_edge. No Phase 3, no TP/SL optimisation, no further parameter search. Conclusion limited to the IDEA-032 hypothesis; EMA/VWAP in general is not disproven.
- 2026-10-08 data contract: complete-bins last bin 15:10 (IDEA-029 and IDEA-031 precedent); 15:25 would drop all post-2026-08-03 sessions.
- 2026-10-08 placebo documented as a direction-matched state placebo; random-direction placebo not run to avoid a directional confound.
- Resolved: D1 to D6 (see earlier entries). Open items from the first draft are closed by the stop decision.
