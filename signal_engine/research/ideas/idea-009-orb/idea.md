---
id: IDEA-009
title: Opening Range Breakout (production)
status: live
verdict: marginal
category: intraday-breakout
horizon: intraday
source:
  type: internal
summary: 'The production ORB strategy: frozen and live, alert identity intraday-orb.'
result: 'Backtest verdict 2026-08-28: keep running small to accumulate forward data; do not scale and
  do not expect monthly income.'
strategy_path: strategy_library/intraday/orb
analysis_doc: strategy_library/intraday/orb/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/orb.py
revisit_when: Reassess after enough forward data; note the Pine edits noted in the analysis doc were not
  compiled on TradingView.
last_reviewed: '2026-09-29'
tags:
- orb
- live
---

# IDEA-009 - Opening Range Breakout (production)

## Idea

The production ORB strategy: frozen and live, alert identity intraday-orb.

## Analysis and backtest

Full analysis: `strategy_library/intraday/orb/STRATEGY-ANALYSIS.md`.

Current conclusion: Backtest verdict 2026-08-28: keep running small to accumulate forward data; do not scale and do not expect monthly income.

## Decision log

- 2026-09-29: registered in the idea registry (status live).
