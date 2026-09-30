---
id: IDEA-022
title: Dual momentum (Antonacci)
status: shelved
verdict: inconclusive
category: swing-momentum
horizon: swing
source:
  type: article
  author: Gary Antonacci
summary: Relative 12-1 momentum rank plus an absolute-momentum filter that moves a slot to cash.
result: Adds nothing over plain momentum-rank on this universe and construction; not wrong, structurally
  redundant.
strategy_path: strategy_library/swing/dual-momentum
analysis_doc: strategy_library/swing/dual-momentum/STRATEGY-ANALYSIS.md
backtest_module: backtest/dual_momentum.py
revisit_when: If drawdown control becomes a priority over simplicity.
last_reviewed: '2026-09-29'
tags:
- momentum
- dual
---

# IDEA-022 - Dual momentum (Antonacci)

## Idea

Relative 12-1 momentum rank plus an absolute-momentum filter that moves a slot to cash.

## Analysis and backtest

Full analysis: `strategy_library/swing/dual-momentum/STRATEGY-ANALYSIS.md`.

Current conclusion: Adds nothing over plain momentum-rank on this universe and construction; not wrong, structurally redundant.

## Decision log

- 2026-09-29: registered in the idea registry (status shelved).
