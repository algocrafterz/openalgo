---
id: IDEA-023
title: Gap plus RSI swing
status: rejected
verdict: no-edge
category: swing-mean-reversion
horizon: swing
source:
  type: internal
summary: Swing entry on gap down combined with an RSI condition.
result: 'Not shipped: does not clear the cost line, and real figures are somewhat worse than reported.'
strategy_path: strategy_library/swing/gap-rsi
analysis_doc: strategy_library/swing/gap-rsi/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/gap_rsi.py
revisit_when: Not planned.
last_reviewed: '2026-09-29'
tags:
- gap
- rsi
---

# IDEA-023 - Gap plus RSI swing

## Idea

Swing entry on gap down combined with an RSI condition.

## Analysis and backtest

Full analysis: `strategy_library/swing/gap-rsi/STRATEGY-ANALYSIS.md`.

Current conclusion: Not shipped: does not clear the cost line, and real figures are somewhat worse than reported.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
