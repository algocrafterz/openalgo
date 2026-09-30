---
id: IDEA-012
title: EMA9 x VWAP crossover
status: rejected
verdict: no-edge
category: intraday-momentum
horizon: intraday
source:
  type: article
  ref: strategy_library/intraday/ema9-vwap/vwap-ema-cross-strategy.pdf
summary: Momentum entry on the 9 EMA crossing VWAP.
result: 'Built and measured 2026-09-10, verdict do not trade: on 208 NSE F&O names the cross follows through
  less than random, so it behaves as mean reversion, not momentum.'
rejection_basis: no-gross-edge
strategy_path: strategy_library/intraday/ema9-vwap
analysis_doc: strategy_library/intraday/ema9-vwap/STRATEGY-LOG.md
backtest_module: backtest/strategies/ema9_vwap.py
revisit_when: Only as a deliberately mean-reversion variant.
last_reviewed: '2026-09-29'
tags:
- ema
- vwap
---

# IDEA-012 - EMA9 x VWAP crossover

## Idea

Momentum entry on the 9 EMA crossing VWAP.

## Analysis and backtest

Full analysis: `strategy_library/intraday/ema9-vwap/STRATEGY-LOG.md`.

Current conclusion: Built and measured 2026-09-10, verdict do not trade: on 208 NSE F&O names the cross follows through less than random, so it behaves as mean reversion, not momentum.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
- 2026-09-30: recorded rejection_basis no-gross-edge (gross +3.0 bps vs 16 bps cost; follows through less than random).
