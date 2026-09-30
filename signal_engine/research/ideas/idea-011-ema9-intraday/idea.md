---
id: IDEA-011
title: 9 EMA intraday (PDF article)
status: rejected
verdict: no-edge
category: intraday-momentum
horizon: intraday
source:
  type: article
  ref: strategy_library/intraday/ema9/791187576-9-EMA-Trading-Strategy.pdf
summary: Nine-EMA retest strategy from a HowToTrade.com explainer, implemented as ema9-intraday.pine.
result: Both the Pine script and the source-PDF rules have negative gross expectancy with costs set to
  zero; the entries themselves have no edge.
rejection_basis: no-gross-edge
strategy_path: strategy_library/intraday/ema9
analysis_doc: strategy_library/intraday/ema9/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/ema9.py
revisit_when: Not planned.
last_reviewed: '2026-09-29'
tags:
- ema
---

# IDEA-011 - 9 EMA intraday (PDF article)

## Idea

Nine-EMA retest strategy from a HowToTrade.com explainer, implemented as ema9-intraday.pine.

## Analysis and backtest

Full analysis: `strategy_library/intraday/ema9/STRATEGY-ANALYSIS.md`.

Current conclusion: Both the Pine script and the source-PDF rules have negative gross expectancy with costs set to zero; the entries themselves have no edge.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
- 2026-09-30: recorded rejection_basis no-gross-edge (gross -1.1 bps at zero-cost measurement, 276k trades).
