---
id: IDEA-013
title: Initial balance extension
status: rejected
verdict: no-edge
category: intraday-breakout
horizon: intraday
source:
  type: internal
summary: Trade continuation after the initial balance range is exceeded.
result: 'Backtest verdict 2026-08-28: do not trade, there is no edge for costs to eat into; retire.'
rejection_basis: no-gross-edge
strategy_path: strategy_library/intraday/ib-extension
analysis_doc: strategy_library/intraday/ib-extension/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/ib_extension.py
revisit_when: Not planned.
last_reviewed: '2026-09-29'
tags:
- initial-balance
---

# IDEA-013 - Initial balance extension

## Idea

Trade continuation after the initial balance range is exceeded.

## Analysis and backtest

Full analysis: `strategy_library/intraday/ib-extension/STRATEGY-ANALYSIS.md`.

Current conclusion: Backtest verdict 2026-08-28: do not trade, there is no edge for costs to eat into; retire.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
- 2026-09-30: recorded rejection_basis no-gross-edge (gross -1.6 bps, no edge before costs).
