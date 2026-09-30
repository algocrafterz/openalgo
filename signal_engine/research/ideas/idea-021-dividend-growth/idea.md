---
id: IDEA-021
title: Dividend growth
status: rejected
verdict: no-edge
rejection_basis: no-gross-edge
category: swing-value
horizon: swing
source:
  type: internal
summary: Buy dividend-growth names in a value zone.
result: 'Backtest verdict 2026-08-28: do not trade.'
strategy_path: strategy_library/swing/dividend-growth
analysis_doc: strategy_library/swing/dividend-growth/STRATEGY-ANALYSIS.md
backtest_module: backtest/strategies/value_zone.py
revisit_when: Not planned.
last_reviewed: '2026-09-29'
tags:
- dividend
- value
---

# IDEA-021 - Dividend growth

## Idea

Buy dividend-growth names in a value zone.

## Analysis and backtest

Full analysis: `strategy_library/swing/dividend-growth/STRATEGY-ANALYSIS.md`.

Current conclusion: Backtest verdict 2026-08-28: do not trade.

## Decision log

- 2026-09-29: registered in the idea registry (status rejected).
- 2026-10-01: rejection_basis no-gross-edge. The stored run shows gross +404 bps, but the same-symbol random-entry baseline earns +434 bps net, so the rule adds -54 bps (t -3.07) over the symbols' own drift. There is no edge before costs once drift is removed.
