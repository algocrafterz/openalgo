---
id: IDEA-014
title: Narrow-range breakout
status: rejected
verdict: no-edge
category: intraday-breakout
horizon: intraday
source:
  type: internal
summary: Breakout from narrow-range days, with a volatility-regime filter.
result: 'Dead end, do not trade: measured 30.9% follow-through versus 33.3% for random entries; the regime
  filter helps directionally but not enough.'
strategy_path: strategy_library/intraday/nr-breakout
analysis_doc: strategy_library/intraday/nr-breakout/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/nr_breakout.py
revisit_when: Not planned.
last_reviewed: '2026-09-29'
tags:
- nr
- volatility
---

# IDEA-014 - Narrow-range breakout

## Idea

Breakout from narrow-range days, with a volatility-regime filter.

## Analysis and backtest

Full analysis: `strategy_library/intraday/nr-breakout/STRATEGY-ANALYSIS.md`.

Current conclusion: Dead end, do not trade: measured 30.9% follow-through versus 33.3% for random entries; the regime filter helps directionally but not enough.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
