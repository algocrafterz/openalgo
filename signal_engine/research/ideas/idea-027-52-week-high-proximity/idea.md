---
id: IDEA-027
title: Proximity to the 52-week high
stage: inbox
strategy_category: Momentum
strategy_tags: [cross-sectional, relative-strength, 52-week-high, ranked-portfolio, weekly-rebalance]
horizon: swing
backtest_module: backtest/strategies/high_proximity.py
source:
  type: self
  author: researcher
  ref: _inbox/52-week-high-proximity.txt
  captured: 2026-10-04
summary: Each week, hold the 20 stocks whose close is nearest to their 252-session high close, provided they are within 5% of it; sell stocks that drop out of that set.
revisit_when: After the first backtest result is reviewed.
last_reviewed: 2026-10-04
---

# IDEA-027 - Proximity to the 52-week high

## Idea

Stocks that trade close to their 52-week high tend to keep outperforming, so buy the ones nearest it.
This is a distinct strategy from the 12-1 momentum rank: it ranks by distance to the high, not by past return.

## Analysis and backtest

Not yet run. The specification was resolved question by question with the researcher on 2026-10-04.

## Decision log

- 2026-10-04: chosen as the first strategy through the new onboarding; legacy ideas are not used as inputs.
- 2026-10-04: classification confirmed: Momentum, swing, long only, NSE cash equities, daily. Momentum is the strategy family; cross-sectional, relative-strength, 52-week-high, ranked-portfolio and weekly-rebalance are descriptive tags.
- 2026-10-04: holding_period recorded as none (no maximum holding period), as decided by the researcher.
- 2026-10-04: researcher confirmed three v1 assumptions. (1) Ties at the cut are broken by a hash of symbol and date. (2) The 252-session window is counted on the derived calendar from the first bar the engine sees, after eligibility and warm-up handling, so a first signal about 312 sessions after a symbol's first observed bar is accepted; it is not 252 sessions from listing. (3) Sessions missing or dropped inside the window are skipped with no minimum-coverage rule; what was dropped is in the run's exclusions and data-quality evidence.
- 2026-10-04: eligibility corrected to match what runs. The platform default close floor (Rs 10, A-046) is recorded but NOT applied to this strategy's split-adjusted daily data, because back-adjusted prices are not the traded price and a nominal rupee floor would not be reliable. The researcher chose to keep the engine behaviour and correct the record. Effective eligibility for v1: listing age (60 prior sessions), a bar on the session and non-zero volume, plus the strategy's own 252-session history and warm-up. No raw-price floor was added.
