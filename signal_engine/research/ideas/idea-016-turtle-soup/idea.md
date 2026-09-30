---
id: IDEA-016
title: Turtle Soup false-breakout fade
status: rejected
verdict: marginal
category: intraday-mean-reversion
horizon: intraday
source:
  type: internal
summary: Fade a failed breakout of a prior extreme.
result: 'Dead end, do not trade: real gross edge but too thin per trade to survive costs; adding refinements
  made results worse.'
strategy_path: strategy_library/intraday/turtle-soup
analysis_doc: strategy_library/intraday/turtle-soup/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/turtle_soup.py
revisit_when: Only if execution costs fall materially.
last_reviewed: '2026-09-29'
tags:
- fade
- false-breakout
---

# IDEA-016 - Turtle Soup false-breakout fade

## Idea

Fade a failed breakout of a prior extreme.

## Analysis and backtest

Full analysis: `strategy_library/intraday/turtle-soup/STRATEGY-ANALYSIS.md`.

Current conclusion: Dead end, do not trade: real gross edge but too thin per trade to survive costs; adding refinements made results worse.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
