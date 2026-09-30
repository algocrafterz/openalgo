---
id: IDEA-004
title: 9/21 EMA cross, base, pullback, breakout
status: rejected
verdict: no-edge
category: swing-momentum
horizon: swing
source:
  type: video
  ref: research/ideas/_inbox/ideas-evaluated.txt (strategy:4)
summary: The 9/21 EMA cross is only the trigger; the tradeable setup is cross, tight base, pullback to
  the EMA zone, buyers defend, then breakout.
result: 'Disqualified 2026-09-22: the apparent edge was a concurrency artifact (needs median 54 and max
  138 simultaneous positions) and is negative at a realistic 10-slot cap.'
strategy_path: strategy_library/swing/ema-pullback
analysis_doc: strategy_library/swing/ema-pullback/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/ema_pullback.py
revisit_when: Only if a version is found that holds up under a realistic position cap.
last_reviewed: '2026-09-29'
tags:
- ema
- pullback
- swing
---

# IDEA-004 - 9/21 EMA cross, base, pullback, breakout

## Idea

The 9/21 EMA cross is only the trigger; the tradeable setup is cross, tight base, pullback to the EMA zone, buyers defend, then breakout.

## Analysis and backtest

Full analysis: `strategy_library/swing/ema-pullback/STRATEGY-ANALYSIS.md`.

Current conclusion: Disqualified 2026-09-22: the apparent edge was a concurrency artifact (needs median 54 and max 138 simultaneous positions) and is negative at a realistic 10-slot cap.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
