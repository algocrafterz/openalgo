---
id: IDEA-027
title: Proximity to the 52-week high
stage: backtest
research_state: rejected
rejection_basis: not-robust
result: "NO_EDGE on the published NSE dataset nse-daily-20261005-002 (run 2026-10-05-52-week-high-proximity-v3): net +50.3 bps over 4,481 trades, but G2.2 statistical significance FAIL (t 2.01 vs hurdle 2.25), G4.5 dispersion FAIL, and out-of-sample confirmation weak (t 0.19)."
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
revisit_when: Not by tuning this version. Only a new, pre-specified hypothesis, recorded as a separate idea.
last_reviewed: 2026-10-05
---

# IDEA-027 - Proximity to the 52-week high

## Idea

Stocks that trade close to their 52-week high tend to keep outperforming, so buy the ones nearest it.
This is a distinct strategy from the 12-1 momentum rank: it ranks by distance to the high, not by past return.

## Analysis and backtest

The specification was resolved question by question with the researcher on 2026-10-04.

Clean result (the only one that counts): run `2026-10-05-52-week-high-proximity-v3`, frozen
version v1, published dataset `nse-daily-20261005-002` (root `429123fb...`), code `7c0665d3b`,
206 eligible symbols, 2016-01-01 to 2026-10-01. Run status VALID, verdict NO_EDGE.

| Metric | Value |
| --- | ---: |
| Trades | 4,481 |
| Gross / net per trade | +74.3 / +50.3 bps (24 bps round-trip cost) |
| Day-clustered t / hurdle (G2.2) | 2.01 / 2.25: FAIL |
| Dispersion (G4.5) | FAIL |
| In-sample net / t | +61.9 bps / 2.16 |
| Out-of-sample net / t | +30.3 bps / 0.19 |

Random-entry baseline (supporting diagnostic, not a gate): same symbol, same holding period in
bars, open entry, open or close exit as the trade, 24 bps cost, 20 uniform random entry bars per
trade over the symbol's eligible post-warm-up history (89,620 samples, seed 0). Gross +95.9 bps,
net +71.9 bps; the strategy's net is 21.6 bps per trade below it (excess t -2.03). It is a per-trade
comparison, not a portfolio one: no 20-position cap, no weekly rebalance timing, entries on any
session. It says picking stocks near their high did not beat holding the same stocks for the same
time on random days.

The two 2026-10-04 runs (v1, v2) used the legacy mixed-basis store and are kept only as history.

## Decision log

- 2026-10-04: chosen as the first strategy through the new onboarding; legacy ideas are not used as inputs.
- 2026-10-04: classification confirmed: Momentum, swing, long only, NSE cash equities, daily. Momentum is the strategy family; cross-sectional, relative-strength, 52-week-high, ranked-portfolio and weekly-rebalance are descriptive tags.
- 2026-10-04: holding_period recorded as none (no maximum holding period), as decided by the researcher.
- 2026-10-04: researcher confirmed three v1 assumptions. (1) Ties at the cut are broken by a hash of symbol and date. (2) The 252-session window is counted on the derived calendar from the first bar the engine sees, after eligibility and warm-up handling, so a first signal about 312 sessions after a symbol's first observed bar is accepted; it is not 252 sessions from listing. (3) Sessions missing or dropped inside the window are skipped with no minimum-coverage rule; what was dropped is in the run's exclusions and data-quality evidence.
- 2026-10-04: eligibility corrected to match what runs. The platform default close floor (Rs 10, A-046) is recorded but NOT applied to this strategy's split-adjusted daily data, because back-adjusted prices are not the traded price and a nominal rupee floor would not be reliable. The researcher chose to keep the engine behaviour and correct the record. Effective eligibility for v1: listing age (60 prior sessions), a bar on the session and non-zero volume, plus the strategy's own 252-session history and warm-up. No raw-price floor was added.
- 2026-10-05: rejected (rejection_basis not-robust; the existing taxonomy has no KILLED state). First clean run v3 on the published NSE dataset: NO_EDGE, G2.2 statistical significance FAIL, G4.5 dispersion FAIL, out-of-sample confirmation weak. Not tuned and not rerun: the hypothesis failed its predefined gates, and post-hoc variants would be overfitting. All runs and evidence are kept.
