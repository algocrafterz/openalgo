---
id: IDEA-029
title: VWAP 2SD range mean reversion
stage: inbox
research_state: active
strategy_category: Mean Reversion
strategy_tags: [vwap, vwap-bands, range-regime, intraday, long-short]
horizon: intraday
source:
  type: self
  author: researcher
  ref: _inbox/vwap-2sd-range-mean-reversion.txt
  captured: 2026-10-05
summary: In a range-bound regime (15m ADX and flat EMA20), fade a 5m excursion beyond the session VWAP 2SD band once price closes back inside the band and across EMA9; target VWAP, stop beyond the extreme.
last_reviewed: 2026-10-06
---

# IDEA-029 - VWAP 2SD range mean reversion

## Idea

The researcher's draft is in `_inbox/vwap-2sd-range-mean-reversion.txt`. It is a draft: no spec,
no strategy version, no freeze and no run exist.

## Analysis and backtest

Not tested. There is no verdict and no counted trial. The published 5m dataset
`nse-intraday-5m-20261006-001` now exists. Twelve specification decisions stay open (listed in the
inbox draft); they are resolved before the spec is frozen.

## Data contract (proposed, not frozen)

What the strategy needs from `nse-intraday-5m-20261006-001`, enforced at preflight and on read.
It goes into `spec.yaml` as written here when the spec is frozen.

```yaml
data:
  frequency: 5m
  field_families: [price, volume]
  min_n_minutes: 1
  complete_bins: {first: "09:15", last: "15:10"}
```

- **Price** (required): `price_valid`. OHLC of every bar the strategy reads is canonical NSE.
- **Volume** (required): `volume_valid`. Session VWAP and its SD bands are volume weighted, so a
  session with invalid volume is not used. Volume is never withheld-and-served for this strategy.
- **final_bar** (not required): the strategy reads no bar after 15:10 (entries end earlier and the
  profile's MIS exit is 14:45), and its 15m series ends with the 15:00-15:15 bar, so the closing
  auction print never enters a signal.
- **Bin completeness**: all 72 5m bins from 09:15 to 15:10 must exist. A missing bin breaks the
  bar count of EMA9, ATR14 and the 15m aggregation.
- **min_n_minutes = 1**: any published bar is usable. Measured on the price+volume-available
  sessions: n_minutes < 5 falls from about 10% of bars (2017-2019) to under 0.5% (2025-2026) and is
  concentrated in the least liquid names (OFSS, BAJAJHLDNG, FORCEMOT), which reads as minutes with
  no trade rather than lost data. A bar from fewer minutes has the right OHLCV for the trades that
  happened. Session exclusion by threshold (with complete bins): 1 -> 2.5%, 2 -> 4.9%, 3 -> 7.6%,
  5 -> 19.4% (2017: 6.9%, 12.5%, 18.4%, 38.7%). A higher minimum is a liquidity filter, not a
  data-quality rule; if wanted it belongs in the strategy's filters.
- **When the contract is not met**: the symbol-session is not traded and supplies no signal; nothing
  is filled, repaired or interpolated. Excluded symbol-sessions are listed in the preflight report
  with their reason and counted by year in the run record. Preflight is NOT_READY when no symbol
  is eligible. Indicator series continue across an excluded session (decision 11).
- **Not enforced yet**: the spec schema does not accept a `data:` block, and the read path does not
  expose `n_minutes` or check bin completeness. Both are needed before preflight.

## Decision log

- 2026-10-05: recorded as blocked (block_reason missing-published-intraday-dataset). Blocked is a data-readiness state, not research evidence: not rejected, not no-edge, not inconclusive, not shelved. The legacy Historify minute data is not used for a recorded run.
- 2026-10-05: plan: build the minimum 1m -> 5m published intraday dataset, then resolve the open decisions, freeze v1, preflight and run once.
- 2026-10-06: unblocked (research_state active): `nse-intraday-5m-20261006-001` is published. Data contract proposed (price + volume, complete 09:15-15:10 bins, min_n_minutes 1); the twelve decisions go to review before the freeze.
