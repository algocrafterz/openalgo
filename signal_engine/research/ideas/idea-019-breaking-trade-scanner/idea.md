---
id: IDEA-019
title: BreakingTrade market-profile scanner
status: shelved
verdict: no-edge
category: market-profile
horizon: intraday
source:
  type: vendor
  author: breakingtrade.com
  ref: strategy_library/intraday/breaking-trade/README.md
summary: Vendor market-profile scanner snapshots ranked by an internal extractor and scorer, used as a
  manual watchlist aid.
result: 'BTST measured 2026-09-04: no edge detected; the data does not support swing holds. Extractor
  kept manual-only.'
strategy_path: strategy_library/intraday/breaking-trade
analysis_doc: strategy_library/intraday/breaking-trade/STRATEGY-LOG.md
revisit_when: If the vendor data source changes or gains an API.
last_reviewed: '2026-09-29'
tags:
- market-profile
- scanner
---

# IDEA-019 - BreakingTrade market-profile scanner

## Idea

Vendor market-profile scanner snapshots ranked by an internal extractor and scorer, used as a manual watchlist aid.

## Analysis and backtest

Full analysis: `strategy_library/intraday/breaking-trade/STRATEGY-LOG.md`.

Current conclusion: BTST measured 2026-09-04: no edge detected; the data does not support swing holds. Extractor kept manual-only.

## Decision log

- 2026-09-29: registered in the idea registry (status shelved).
