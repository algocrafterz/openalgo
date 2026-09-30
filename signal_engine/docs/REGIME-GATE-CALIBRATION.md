# X.2 Regime Dispersion: Calibration

Date: 2026-10-01. Gate X.2 in `backtest/evaluation.py` is a HEURISTIC. It is not a gate in
`STRATEGY-ACCEPTANCE-CRITERIA.md`. Its threshold was first set by judgement ("net positive in
at least 2 sampled regimes"), then measured. The code is `backtest/regime_gate_calibration.py`;
rerun it with
`uv run --group analysis python -m signal_engine.backtest.regime_gate_calibration`.

## Method

Take a real trade list with entry-day regime labels (`backtest/regime.py`, version 1). Demean
R within each regime so every regime has a true mean of zero (the null), then add a uniform
`edge` in R to every trade (the alternative). Resample sessions with replacement (day-block
bootstrap, 3000 draws, seed 0) so trades sharing a day stay together. For each draw drop
regimes under 30 trades and apply each rule. The share of draws a rule passes is its
false-pass rate under edge 0 and its power under edge > 0.

Books: real swing trade lists from the shipped defaults on the full Historify universe
(IDEA-023 gap_rsi long-only 3572 trades, ibs_meanrev 37419, ema_pullback 5115,
liquidity_sweep 114).

## Result (share of draws passing; both dimensions, trend and volatility, must pass)

Zero edge (the false-pass rate):

| Rule | gap_rsi | ibs_meanrev | ema_pullback | liquidity_sweep (114 trades) |
| --- | --- | --- | --- | --- |
| positive in >= 1 | 0.80 | 0.81 | 0.81 | 0.68 |
| positive in >= 2 (the original) | 0.35 | 0.37 | 0.36 | 0.17 |
| positive in all sampled regimes | 0.06 | 0.06 | 0.06 | 0.11 |

Power at a true uniform edge (all-regimes rule):

| True edge per trade | gap_rsi | ibs_meanrev | ema_pullback | liquidity_sweep |
| --- | --- | --- | --- | --- |
| +0.05R | 0.11 | 0.96 | 0.15 | 0.19 |
| +0.10R | 0.21 | 1.00 | 0.32 | 0.27 |
| +0.20R | 0.42 | 1.00 | 0.73 | 0.50 |

## Decision

The original rule ("at least 2 positive regimes") fails the calibration: it passes roughly
36% of books that have no edge in any regime, against a target of 20%. With three regimes per
dimension a coin-flip regime is positive half the time, so two of three is easy to reach by
luck. The rule is now "net positive in every sampled regime of each judged dimension"
(`REGIME_RULE = "all"`), whose null false-pass rate is 6% on the three books with at least
3500 trades.

Cost of that choice: power is modest. A true edge of 0.10R per trade passes only 21% to 32% of
the time at 3500 to 5000 trades, because one noisy regime can flip negative. It passes almost
always when the sample is large (ibs_meanrev). This is why X.2 stays non-HARD: a fail is a
prompt to look at the regime table, not a verdict. A small book (114 trades) has too few
trades per regime for any sign rule to be reliable, and X.2 reports rather than judges when
fewer than two regimes reach 30 trades.

## Limits

- Sign counting ignores the size of each regime's mean. A per-regime t-test or a
  cross-regime heterogeneity test would be more powerful; not built.
- The null demeans within regime over the whole sample, so it tests the gate in isolation, not
  the gate combined with the headline t-statistic.
- All four books are swing books. Intraday books have more days and were not calibrated.
- Regime labels are one fixed rule (`REGIME_VERSION = 1`); a different rule would need a
  fresh calibration.
