---
id: IDEA-033
title: EMA-VWAP early displacement with aggressive trailing stop (Python, paper)
stage: backtest             # paper-traded as a behaviour check, not promoted (see decision log)
research_state: needs_more_evidence
strategy_category: Other
strategy_tags: [vwap, ema, displacement, trailing-stop, no-target]
horizon: intraday
source:
  type: self
  author: Owner discretionary method
  ref: strategy_library/lipiscripts/intraday/ema-vwap-atr-early-displacement-v2.lipi
  captured: 2026-10-08
summary: Enter when (EMA9 - session VWAP(hlc3)) / ATR14 crosses back through -0.50 (long) or +0.50 (short), before the EMA/VWAP crossover, with momentum and EMA20 alignment. No take-profit; an initial swing stop plus a hybrid ATR/structure trailing stop that activates at the crossover.
strategy_path: strategy_library/intraday/ema-vwap-early
analysis_doc: strategy_library/intraday/ema-vwap-early/STRATEGY-LOG.md
backtest_module: backtest/strategies/ema_vwap_early.py
revisit_when: After 10 paper sessions, compare the paper trades with the owner's discretionary reading of the same charts, then run baselines A-D on the full F&O universe with costs.
last_reviewed: 2026-10-08
---

# IDEA-033 - EMA-VWAP early displacement with aggressive trailing stop

## Idea

The owner's discretionary method, from the LipiScript chart study (`strategy_library/lipiscripts/intraday/ema-vwap-atr-early-displacement-v2.lipi`; the
early-entry, watch, crossover and reversal rules the Python port follows are the same in v2) and the
`.txt` framework in `_inbox/`: the EMA9/VWAP crossover is late, so enter on the early displacement transition and let an
aggressive trailing stop, not a target, decide the exit.

## Relation to IDEA-032 (acknowledged)

IDEA-032 tested the same entry with a zero-cross exit and no stop: -2.3 bps gross, -13.9 bps net per
trade over 498,811 trades, and no selective continuation beyond the state placebo
(`idea-032-ema-vwap-atr-early-displacement/evidence/phase12-evidence.md`). Its revisit rule asks for
a materially different signal. This idea changes the exit model (initial swing stop, trailing stop,
no target) and adds EMA20 alignment, but the entry trigger is the same, so the IDEA-032 evidence is a
weak prior against it.

Owner decision 2026-10-08: paper-trade it anyway. The purpose of the paper run is to check that the
Python implementation behaves like the discretionary method, not to claim an edge. It is not
optimised or rejected on the IDEA-032 result until the paper behaviour is checked.

## Analysis and backtest

Not yet run on the full universe. A smoke run (shared 40, 2026-09-10 to 2026-10-08, gross, no costs)
only checked plumbing: about 62 accepted signals a day, exits split between trailing and initial
stop. It is not evidence of an edge. Translation assumptions are in the strategy log.

## Decision log

- 2026-10-08: stage stays `backtest`. The paper run is an owner-authorised behaviour check outside
  the promotion gates (no promoted run exists), the same arrangement as BREAKOUT-PY for IDEA-031.
- 2026-10-08: implemented as EVWAP-EARLY-PY in the Python runner, paper only, shared 40 universe,
  trailing exit checked at each 5m close and sent as a market exit; broker SL-M stays at the initial stop.
