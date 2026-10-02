---
id: IDEA-000                # next free number; folder must be idea-000-<slug>
title: Short human title
stage: inbox                # inbox | backtest | paper | live | retired (move with `intake stage`)
# research_state: active    # active | promising | needs_more_evidence | weak_evidence | evidence_against_edge | shelved | rejected (default active; `intake state`)
# strategy_category: Other  # see research/taxonomy.yaml (`intake classify`); strategy_tags: [a, b]
category: intraday-momentum # see CATEGORIES in build_registry.py
horizon: intraday           # intraday | swing | positional
source:
  type: video               # video | article | self | internal | vendor | telegram
  author: Name or channel
  ref: URL, or a file under research/ideas/_inbox/
  captured: 2026-01-01
summary: One or two sentences of the rules, as the source states them.
result: One sentence conclusion once tested (delete while untested).
# Optional links, checked to exist by the registry builder (paths relative to signal_engine/):
# strategy_path: strategy_library/intraday/<name>
# analysis_doc: strategy_library/intraday/<name>/STRATEGY-ANALYSIS.md
# backtest_module: backtest/strategies/<name>.py
revisit_when: The specific condition that should make us look at this again.
last_reviewed: 2026-01-01
tags: []
---

# IDEA-000 - Title

## Idea

Rules as stated by the source. Quote vague terms verbatim, then pin each to a number
under "Analysis" so a different reading is visibly a different strategy.

## Analysis and backtest

Findings, assumptions, and links to run records under `runs/`.

## Decision log

- YYYY-MM-DD: what was decided and why.
