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
rejection_basis: no-gross-edge
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
- 2026-09-30: recorded rejection_basis no-gross-edge (gross -8.8 bps at 24 bps cost, t -2.6).
- 2026-09-30: v4 re-run with the swing engine long-only (v1-v3 included short trades that cannot be executed on CNC). 3572 trades, gross +46.2 bps, net +22.2 bps, but excess over the same-symbol random-entry baseline is +15.9 bps (t -0.85) and books capped at 3, 5 and 10 positions are all net negative. Rejection stands with rejection_basis no-gross-edge: the uncapped gross is positive only through unrealistic concurrency (peak 104 positions), and at every deployable cap the gross is negative.
