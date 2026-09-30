---
id: IDEA-024
title: IBS mean reversion
status: shelved
verdict: marginal
category: swing-mean-reversion
horizon: swing
source:
  type: internal
summary: Buy low internal bar strength closes and exit on reversion.
result: Marginal, not proven, do not trade; meaningfully improved from clearly negative.
strategy_path: strategy_library/swing/ibs-meanrev
analysis_doc: strategy_library/swing/ibs-meanrev/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/ibs_meanrev.py
revisit_when: If a further independent improvement is found and passes IS/OOS.
last_reviewed: '2026-09-29'
tags:
- ibs
- mean-reversion
---

# IDEA-024 - IBS mean reversion

## Idea

Buy low internal bar strength closes and exit on reversion.

## Analysis and backtest

Full analysis: `strategy_library/swing/ibs-meanrev/STRATEGY-ANALYSIS.md`.

Current conclusion: Marginal, not proven, do not trade; meaningfully improved from clearly negative.

## Decision log

- 2026-09-29: registered in the idea registry (status shelved).
