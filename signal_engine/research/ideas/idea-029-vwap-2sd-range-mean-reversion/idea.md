---
id: IDEA-029
title: VWAP 2SD range mean reversion
stage: inbox
research_state: blocked
block_reason: missing-published-intraday-dataset
strategy_category: Mean Reversion
strategy_tags: [vwap, vwap-bands, range-regime, intraday, long-short]
horizon: intraday
source:
  type: self
  author: researcher
  ref: _inbox/vwap-2sd-range-mean-reversion.txt
  captured: 2026-10-05
summary: In a range-bound regime (15m ADX and flat EMA20), fade a 5m excursion beyond the session VWAP 2SD band once price closes back inside the band and across EMA9; target VWAP, stop beyond the extreme.
revisit_when: When a published, verified 5m NSE intraday dataset version exists.
last_reviewed: 2026-10-05
---

# IDEA-029 - VWAP 2SD range mean reversion

## Idea

The researcher's draft is in `_inbox/vwap-2sd-range-mean-reversion.txt`. It is a draft: no spec,
no strategy version, no freeze and no run exist.

## Analysis and backtest

Not tested. There is no verdict and no counted trial. The idea needs 5-minute bars, and recorded
runs read only immutable published datasets; only daily versions exist
(`nse-daily-20261005-002`). The preflight correctly refuses intraday reads
(`PublishedSource.intraday`).

Twelve specification decisions stay open until the dataset exists (listed in the inbox draft).

## Decision log

- 2026-10-05: recorded as blocked (block_reason missing-published-intraday-dataset). Blocked is a data-readiness state, not research evidence: not rejected, not no-edge, not inconclusive, not shelved. The legacy Historify minute data is not used for a recorded run.
- 2026-10-05: plan: build the minimum 1m -> 5m published intraday dataset, then resolve the open decisions, freeze v1, preflight and run once.
