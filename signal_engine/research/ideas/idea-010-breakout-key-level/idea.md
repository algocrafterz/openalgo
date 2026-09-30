---
id: IDEA-010
title: Key-level breakout engine (breakout.pine)
status: evaluating
verdict: inconclusive
category: intraday-breakout
horizon: intraday
source:
  type: internal
summary: Key-level engine (previous day, initial balance, opening range confluence) feeding intraday-breakout;
  changelog in breakout.md.
result: In development; key-level adapter covers PD and IB families only, value-area levels are not reproducible
  from 5-minute bars.
strategy_path: strategy_library/intraday/orb
analysis_doc: strategy_library/intraday/orb/breakout.md
backtest_module: backtest/strategies/breakout.py
revisit_when: When the adapter coverage gap for value-area levels is closed or accepted.
last_reviewed: '2026-09-29'
tags:
- breakout
- key-levels
---

# IDEA-010 - Key-level breakout engine (breakout.pine)

## Idea

Key-level engine (previous day, initial balance, opening range confluence) feeding intraday-breakout; changelog in breakout.md.

## Analysis and backtest

Full analysis: `strategy_library/intraday/orb/breakout.md`.

Current conclusion: In development; key-level adapter covers PD and IB families only, value-area levels are not reproducible from 5-minute bars.

## Decision log

- 2026-09-29: registered in the idea registry (status evaluating).
