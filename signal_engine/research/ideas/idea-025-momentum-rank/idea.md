---
id: IDEA-025
title: Cross-sectional momentum rank (12-1)
status: paper
verdict: edge
category: swing-momentum
horizon: swing
source:
  type: internal
summary: Long-only daily rank by 12-1 momentum, hold the top 12, CNC delivery.
result: Candidate, never traded, paper first. Validated as the reference edge; more concentrated variants
  than the shipped top-12 are worse.
strategy_path: strategy_library/swing/momentum-rank
analysis_doc: strategy_library/swing/momentum-rank/STRATEGY-ANALYSIS.md
revisit_when: Promote to live only after paper results.
last_reviewed: '2026-09-29'
tags:
- momentum
- cnc
---

# IDEA-025 - Cross-sectional momentum rank (12-1)

## Idea

Long-only daily rank by 12-1 momentum, hold the top 12, CNC delivery.

## Analysis and backtest

Full analysis: `strategy_library/swing/momentum-rank/STRATEGY-ANALYSIS.md`.

Current conclusion: Candidate, never traded, paper first. Validated as the reference edge; more concentrated variants than the shipped top-12 are worse.

## Decision log

- 2026-09-29: registered in the idea registry (status paper).
