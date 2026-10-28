---
id: IDEA-035
title: ORB market-regime gate and variant study
stage: inbox
research_state: evidence_against_edge
strategy_category: Breakout
strategy_tags: [orb, regime, gate, opening-range]
horizon: intraday
source:
  type: self
  author: Owner
  ref: owner request 2026-10-28 (re-enable ORB paper, study a regime gate)
  captured: 2026-10-28
summary: Test whether simple, causal-at-entry market-regime features separate ORB days that work from days that do not, selected on in-sample years and judged out of sample. Then replay gate, side and size variants on the 67 ORB paper trades and on the 87,279-trade ten-year population.
backtest_module: backtest/strategies/orb.py
analysis_doc: strategy_library/intraday/orb/STRATEGY-ANALYSIS.md
revisit_when: A regime feature passes the pre-registered out-of-sample test below, or 60 more ORB paper sessions exist for a forward check.
last_reviewed: 2026-10-28
---

# IDEA-035 - ORB market-regime gate and variant study

## Context

The 2026-10-28 verdict in `strategy_library/intraday/orb/STRATEGY-ANALYSIS.md` stands: 87,279
backtest trades, no gross edge, net negative in every year. The owner re-enabled ORB paper trading
the same day as an observation run. This study asks only whether a gate, a side rule or a size rule
can turn the existing population positive after costs. It does not revisit the verdict.

## Pre-registration (written before any feature was joined to trades, 2026-10-28)

Population: the legacy-settings ORB trades (what TradingView runs), dataset
`nse-intraday-5m-20261021-002`, one entry per symbol per day, net R at 2 bps and 5 bps per side
with the Flattrade MIS schedule at Rs 900 risk. IS is before 2023-11-08, OOS from it.

Day-level features, all cross-sectional medians over the eligible universe, all known by 09:45:

| Id | Feature | States |
| --- | --- | --- |
| F1 | prior-day market return | up / down |
| F2 | opening gap | up / down |
| F3 | opening drive 09:15 to 09:45 | up / down |
| F4 | prior-day breadth (share of names up) | above / below 0.5 |
| F5 | prior-day range expansion vs own 20-day mean | above / below 1.0 |
| F6 | share of names above their 20-day mean | above / below 0.5 |
| F7 | volatility regime (20-day mean absolute market return vs 250-day median) | high / low |

Rule: for each feature and each side, the better state is chosen on IS net R at 2 bps only. The
gate passes only if that state is also positive net at 2 bps on OOS, positive in a majority of
OOS years, and has a day-clustered t of at least 2.7 on OOS (7 features, Bonferroni at 0.05).
Seven features is seven trials. The same-day full market return is descriptive only and never a
gate, because it is not known at entry.

Variants (replayed on the paper trades first, then on the ten-year population):

- B0 baseline, B1 shorts only, B2 best passing gate (if any), B3 half size outside the good state,
  B4 hold exits (TP 0.8R, stop, 14:45; paper replay only, the adapter cannot model the
  no-progress exit).

## Decision log

- 2026-10-28: opened. Nothing is wired into config or strategies.
- 2026-10-28: ran `regime_study.py` on the 86,967 ten-year legacy-settings trades that have features
  (2,389 days, IS 57,826, OOS 29,141). Trials: 7 features x 2 sides = 14 cells, plus B1 (1) and B3 (1)
  = 16. Result: no cell passes. OOS net R at 2 bps is negative in 13 of 14 cells, with day-clustered
  t between -2.3 and -5.7. The one positive cell (F7 SHORT, low vol, OOS net +0.013 pooled) fails on
  t -2.31 (equal-weight daily mean) and 2/4 positive years. B2 therefore has no member.
  B1 shorts only: OOS net -0.007 R per baseline slot at 2 bps (t -5.5), -0.019 at 5 bps; better than
  baseline by +0.028 to +0.040 R per slot but still negative. B3 half size outside F5: essentially
  baseline (-0.033 vs -0.035).
- 2026-10-28: paper replay of the 67 paper trades, same cost model. Shorts +0.216 R (n 27, t 2.84),
  longs -0.061 R (n 40) at 2 bps; shorts stay positive at 10 bps (+0.131 R, t 1.73). Shorts-only
  minus all: +0.036 R per slot [-0.026, +0.097] at 2 bps, +0.084 [+0.022, +0.147] at 10 bps.
  Regime features exist only to 2026-10-19, so the feature-gated paper replay covers 42 of 67.
- 2026-10-28: verdict `no_gate`. The paper month's short edge is one falling-market window and does
  not appear in the ten-year OOS. No variant is wired. Next step: shadow tracking (see Next).

## Next

Record, per ORB paper signal, the pre-registered features and the side, so a shorts-only rule and
every F1-F7 gate can be scored on forward trades without placing any extra order (a post-hoc filter
of ORB's own fills). Revisit at 60 more ORB paper sessions, as `revisit_when` says. Pass bar stays as
pre-registered above; the 67 paper trades cannot pass it because they are in-sample for the idea.
