---
id: IDEA-005
title: Liquidity sweep with engulfing candle (daily adaptation)
status: rejected
verdict: no-edge
rejection_basis: no-gross-edge
category: swing-mean-reversion
horizon: swing
source:
  type: video
  author: Omar Agag
  ref: research/ideas/_inbox/ideas-evaluated.txt (strategy:5)
summary: A 4-hour engulfing candle that sweeps the prior low marks a buy zone in the lower half of its
  body; enter on a 15-minute structure shift with a 2:1 target. Adapted to daily bars for this platform.
result: No edge gross or net. Loosening filters to rescue it reproduced the textbook in-sample versus
  out-of-sample overfit signature.
strategy_path: strategy_library/swing/liquidity-sweep
analysis_doc: strategy_library/swing/liquidity-sweep/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/liquidity_sweep.py
revisit_when: Only with genuine intraday multi-timeframe data and a fresh pre-registered test.
last_reviewed: '2026-09-29'
tags:
- liquidity-sweep
- engulfing
- smc
---

# IDEA-005 - Liquidity sweep with engulfing candle (daily adaptation)

## Idea

A 4-hour engulfing candle that sweeps the prior low marks a buy zone in the lower half of its body; enter on a 15-minute structure shift with a 2:1 target. Adapted to daily bars for this platform.

## Analysis and backtest

Full analysis: `strategy_library/swing/liquidity-sweep/STRATEGY-ANALYSIS.md`.

Current conclusion: No edge gross or net. Loosening filters to rescue it reproduced the textbook in-sample versus out-of-sample overfit signature.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
- 2026-10-01: rejection_basis no-gross-edge. Stored run gross is +52 bps at t 0.55, indistinguishable from zero, and excess over the random-entry baseline is -106 bps. The in-sample +106 versus out-of-sample -111 split is a consequence of there being no gross edge, not the primary reason.
