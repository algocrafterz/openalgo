---
id: IDEA-028
title: Lowest realised volatility
stage: inbox
strategy_category: Statistical / Quantitative
strategy_tags: [low-volatility, low-risk-anomaly, cross-sectional, ranked-portfolio, monthly-rebalance]
horizon: swing
backtest_module: backtest/strategies/low_volatility.py
source:
  type: self
  author: researcher
  ref: _inbox/low-volatility.txt
  captured: 2026-10-05
summary: Each month, hold the 30 F&O stocks with the lowest sample standard deviation of their last 252 daily returns; sell stocks that drop out of that set.
revisit_when: After the first backtest result is reviewed.
last_reviewed: 2026-10-05
---

# IDEA-028 - Lowest realised volatility

## Idea

Stocks with the lowest trailing realised volatility may generate superior risk-adjusted forward
returns (the low-risk anomaly). It ranks on risk, not on price trend, so it is independent of
IDEA-027 (proximity to the 52-week high); nothing in it was chosen from IDEA-027's result.

The NIFTY Low Volatility 30 index is research context only. Its universe, weighting,
rebalancing, execution and costs differ, so its published performance is not evidence for
this strategy.

## Analysis and backtest

Not yet run. The specification was resolved with the researcher on 2026-10-05.

## Decision log

- 2026-10-05: chosen as the second strategy through the onboarding, an independent hypothesis.
- 2026-10-05: researcher decisions. Monthly rebalance on the last session of the calendar month that has a next session; 30 holdings at equal fixed notional (no inverse-volatility weighting, no volatility scaling); simple returns r_t = Close_t / Close_(t-1) - 1 with sample standard deviation (ddof 1), not annualised.
- 2026-10-05: missing data. 252 returns need 253 consecutive closes on the official NSE sessions. A stock missing any session in the window is not ranked on that rebalance day (sessions are never compressed into one return), and a held stock that is not ranked is sold like any stock that leaves the 30. The dropped sessions themselves are in the run's exclusions ledger.
- 2026-10-05: universe declared in the spec as the current NSE F&O list (current membership, not point-in-time, survivorship bias); dataset availability, exclusions and eligibility are resolved by the preflight.
- 2026-10-05: data is the published dataset nse-daily-20261005-002 as provided: backward-adjusted for splits, bonuses, rights and demergers; dividends are not adjusted, so ex-dividend drops add to measured volatility. Documented limitation; the data layer is not changed for this strategy.
- 2026-10-05: evaluation is the platform's, unchanged: costs 24 bps round trip and 4 bps slippage per side, the existing out-of-sample split and embargo, all hard gates. The random-entry baseline is supporting evidence only. Kill criterion: net expectancy per trade zero or negative after costs; it does not replace the gates.
