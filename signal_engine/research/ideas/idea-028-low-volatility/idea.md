---
id: IDEA-028
title: Lowest realised volatility
stage: backtest
research_state: rejected
rejection_basis: not-robust
result: "MARGINAL on nse-daily-20261005-002 (run 2026-10-05-low-volatility-v1): net +1,610 bps per trade over 301 trades, G2.2 t 5.66 (pass), no hard gate failed; G3.2 out-of-sample FAIL; G3.3 recorded 0 of 19 by an evaluator bug, 17 of 19 (pass) on the corrected evaluation. Diagnostics: below random entry into the same stocks and below the equal-weight eligible universe."
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
revisit_when: Not by tuning this version. Only a new, pre-specified hypothesis, recorded as a separate idea.
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

The specification was resolved with the researcher on 2026-10-05.

The only run: `2026-10-05-low-volatility-v1`, frozen version v1 (strategy fingerprint
`ada16131...`), published dataset `nse-daily-20261005-002` (root `429123fb...`), code `4123f5cdb`,
206 eligible symbols, 2016-01-01 to 2026-10-01. Run status VALID, trial 1, verdict MARGINAL.

| Metric | Value |
| --- | ---: |
| Trades | 301 (30 open at the data end, marked at the last close) |
| Gross / net per trade | +1,634 / +1,610 bps (24 bps round-trip cost); median net +617 bps |
| Win rate / profit factor | 65.1% / 5.41 |
| Day-clustered t (G2.2, hard) | 5.66 against 2.50: pass |
| Out-of-sample (G3.2) | +477 bps, 36% of in-sample against 40%: FAIL |
| Walk-forward (G3.3) | recorded 0 of 19 (evaluator bug); corrected 17 of 19: pass |
| Dispersion (G4.5, hard) | top 10 symbols 37% of net profit, 79% of 99 symbols positive: pass |
| Symbol hold-out (G3.4) | +1,634 / +1,586 bps: pass |

Corrected G3.3: the evaluator scored walk-forward windows in R, which is undefined for trades
without a stop, so every window counted as not positive. After the fix, the sealed trades were
re-scored for G3.3 only, in `evaluations/2026-10-05-low-volatility-v1/`; the run folder is
unchanged, no new trial was counted, and the verdict stays MARGINAL (G3.2 fails, and gates not
yet implemented are NOT_RUN).

Supporting diagnostics (not gates; they change no verdict):

- Random entry (the run's baseline): the same stocks for the same holding period on random days
  made +1,776 bps net per trade against the strategy's +1,467 on 299 matched trades (excess
  -309 bps, t -1.81).
- Equal-weight eligible universe (2026-10-05, read-only script, not stored as a run): every stock
  with a valid 252-return volatility on the same rebalance days, same next-open fills, same data
  and costs, held equal weight. Low-volatility 14.6% a year at 13.6% volatility (drawdown -19%);
  universe 20.8% a year at 19.4% volatility (drawdown -32%). Return per unit of volatility is the
  same, 1.07 for both. Monthly excess -52 bps (t -1.83); before the out-of-sample start -32 bps
  (t -0.88), after it -94 bps (t -2.06). It lost less in the 40 down months (-2.2% against -3.8%)
  and gained less in the 77 up months (+3.0% against +4.6%).

### Final evidence (IDEA-028 v1)

IDEA-028 v1 does not provide convincing evidence of a low-volatility selection edge. The
portfolio was profitable, but the controlled comparisons do not show that low-volatility
selection caused the return.

| Question | Answer |
| --- | --- |
| Absolute profitability | YES (+1,610 bps net per trade) |
| Statistical significance (G2.2) | YES, t 5.66 |
| Out-of-sample robustness (G3.2) | FAIL (36% of in-sample against 40%) |
| Walk-forward (G3.3, corrected) | PASS, 17 of 19 |
| Dispersion (G4.5) / symbol hold-out | PASS / PASS |
| Matched random-entry advantage | NO: strategy +1,467, random +1,776, difference -309 bps, t -1.81 |
| Broad-universe advantage | NO: 14.6% a year against 20.8%; return/volatility 1.07 against 1.07 |
| Low-volatility selection edge | NOT DEMONSTRATED |
| Final research conclusion | Evidence against the low-volatility selection hypothesis |
| Disposition | MARGINAL / rejected (not-robust) |
| Next action | Do not pursue IDEA-028 v1 further; revisit only through a separately specified hypothesis |

Holding the 30 lowest-volatility stocks made money and had smaller drawdowns, but it did not
outperform either matched random entry or the equal-weight eligible universe. The evidence
therefore does not support a distinct low-volatility selection edge.

## Decision log

- 2026-10-05: chosen as the second strategy through the onboarding, an independent hypothesis.
- 2026-10-05: researcher decisions. Monthly rebalance on the last session of the calendar month that has a next session; 30 holdings at equal fixed notional (no inverse-volatility weighting, no volatility scaling); simple returns r_t = Close_t / Close_(t-1) - 1 with sample standard deviation (ddof 1), not annualised.
- 2026-10-05: missing data. 252 returns need 253 consecutive closes on the official NSE sessions. A stock missing any session in the window is not ranked on that rebalance day (sessions are never compressed into one return), and a held stock that is not ranked is sold like any stock that leaves the 30. The dropped sessions themselves are in the run's exclusions ledger.
- 2026-10-05: universe declared in the spec as the current NSE F&O list (current membership, not point-in-time, survivorship bias); dataset availability, exclusions and eligibility are resolved by the preflight.
- 2026-10-05: data is the published dataset nse-daily-20261005-002 as provided: backward-adjusted for splits, bonuses, rights and demergers; dividends are not adjusted, so ex-dividend drops add to measured volatility. Documented limitation; the data layer is not changed for this strategy.
- 2026-10-05: evaluation is the platform's, unchanged: costs 24 bps round trip and 4 bps slippage per side, the existing out-of-sample split and embargo, all hard gates. The random-entry baseline is supporting evidence only. Kill criterion: net expectancy per trade zero or negative after costs; it does not replace the gates.
- 2026-10-05: researcher approval before the single v1 run. Six platform assumptions confirmed as they are (bar size 1d, warm-up 252, listing age 60, minimum volume 1, price floor 10 recorded but NOT applied to adjusted prices, carry-forward true). Missing-data behaviour confirmed: a missing required observation means no valid 252-return volatility, so the symbol is not ranked and cannot be in the 30; a held symbol that cannot be ranked is sold at the next open. 252 returns need 253 consecutive closes; the loader minimum stays 252. No change to v1.
- 2026-10-05: G3.3 evaluator bug fixed (walk-forward windows now use the same per-trade unit as the other gates: R with a stop, net bps without). Sealed trades re-scored for G3.3 only: 17 of 19 windows positive, pass. The run record is not rewritten and no trial was added. G3.2 still fails; verdict stays MARGINAL. The same bug made IDEA-027's G3.3 read 0 of 19; G3.3 is not a hard gate there, so its NO_EDGE stands.
- 2026-10-05: research state evidence_against_edge (existing taxonomy; not rejected, because no hard gate failed and the diagnostics are not gates). The frozen hypothesis, superior risk-adjusted returns from the lowest-volatility stocks, is not supported: G3.2 fails, the book trails random entry into the same stocks, and it trails the equal-weight eligible universe with the same return per unit of volatility, more so out of sample. Not tuned, not rerun, no v2. All runs and evidence are kept.
- 2026-10-05: closed. The G3.3 change is an evaluator correctness fix, not a strategy revision or research iteration; 17 of 19 replaces the erroneous 0 of 19 in this summary, and the sealed run keeps its original record. No further work on v1: no tuning, no v2, no more trials or benchmarks. The universe comparison stays a diagnostic, not a gate, verdict rule or promotion requirement.
- 2026-10-05: researcher decision: research state changed from evidence_against_edge to rejected (rejection_basis not-robust, existing taxonomy). Basis: G3.2 out-of-sample FAIL, the book trails random entry into the same stocks and the equal-weight eligible universe, it does worse out of sample, and the low-volatility edge is not demonstrated. The run verdict stays MARGINAL as recorded. The corrected G3.3 (17 of 19, pass) remains an evaluator correctness fix and does not change this conclusion. No v2, no tuning, no further trials or benchmarks; all evidence kept.
