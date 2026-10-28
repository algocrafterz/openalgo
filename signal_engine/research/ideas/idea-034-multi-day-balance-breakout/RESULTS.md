# IDEA-034 results (2026-10-28)

Verdict: FAIL at the first HARD gate (G1.2, net expectancy). The balance breakout has a real but small
gross edge (about +8.6 bps per trade, day-clustered t 3.2) that is smaller than the 11.55 bps round-trip
cost, so the net result is negative in sample and out of sample. Not worth a paper test as defined.

## What was run

- Data: published dataset `nse-intraday-5m-20261021-002` (5m bars, 2017-01-02 to 2026-10-19, 2,407
  sessions, READY_WITH_WARNINGS), 206 eligible NSE F&O symbols after the run system's eligibility
  policy. The full eligible universe, no subset. No Historify access outside the published version.
- Rules: exactly as pre-registered in `idea.md` (written before the first backtest ran, not committed; the only
  spec edits after that were adding the `data:` block and listing the three string parameters so the
  frozen spec binds to the strategy class, no rule changed).
- Costs: primary scenario, Flattrade statutory model at the deployed notional plus 4 bps slippage per
  side. Mean round trip 11.55 bps.
- Hold-out: last 30 percent of sessions (cut 2023-11-08) from the run system.
- Code: `backtest/strategies/balance_breakout.py`, tests `tests/test_balance_breakout.py` (15 pass,
  including a mutation check that breaking the fresh-cross rule fails two of them).

## Recorded run

`runs/2026-10-28-multi-day-balance-breakout-v1.json` (config 1). Recorded with `--allow-dirty`:
the tree held other sessions' uncommitted work, so the record is marked `reproducible: false`,
code_version `93c4f0daa-dirty`, and is not promotable. `n_trials` = 6 (1 material + 5 external).

| Window | Trades | Gross bps | Net bps | Net t |
| --- | --- | --- | --- | --- |
| All | 1,657 | +8.57 | -2.98 | -0.99 (hurdle 2.66) |
| In sample | 1,097 | +9.24 | -2.31 | -1.17 |
| Out of sample | 560 | +7.25 | -4.29 | +0.02 |

Net expectancy -0.031R, profit factor 0.925, win rate 45.0%, max drawdown 83R, breakeven cost
8.57 bps. Day-clustered gross t: all 3.23, IS 2.18, OOS 2.65 (computed in the driver, not in the
record). Long 770 trades (gross +6.4, net -5.2); short 887 (gross +10.4, net -1.1). Exits: time 990,
stop 461, target 202, gap 4. By year net bps: 2017 +5.7, 2018 +1.5, 2019 -6.4, 2020 +9.5, 2021 -9.8,
2022 -7.8, 2023 -2.7, 2024 -6.8, 2025 -5.7, 2026 -1.3 (4 of 10 years positive). 44 percent of
symbols net positive. Position caps (3, 5, 10 slots) change nothing; peak concurrency 7.

The matched same-symbol random-entry baseline in the record: gross -0.72 bps, net -12.27; the
strategy's excess over it is +9.29 bps net (t 3.52). So the entry timing carries information; it is
not enough to pay for the trade.

## Every trial (unrecorded driver for 2 to 6, same engine, data, costs and hold-out)

| # | Config | Trades | Gross bps | Net bps | Net t (bps) | PF | OOS net bps (n) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N5 k1.5 p30 edge (primary) | 1,657 | +8.53 | -3.02 | -0.99 | 0.92 | -4.28 (560) |
| 2 | N5 k1.0 p30 edge | 1,178 | +9.35 | -2.20 | -0.92 | 0.92 | -2.13 (400) |
| 3 | N3 k1.5 p30 edge | 15,917 | +6.19 | -5.36 | -6.32 | 0.83 | -6.18 (5,810) |
| 4 | N3 k1.0 p30 edge | 13,857 | +5.70 | -5.85 | -6.63 | 0.82 | -6.20 (5,075) |
| 5 | N5 k1.5 two 5m closes edge | 1,754 | +9.13 | -2.41 | -0.61 | 0.91 | -4.34 (611) |
| 6 | N5 k1.5 p30 midpoint stop | 1,657 | +8.09 | -3.46 | -1.20 | 0.92 | -3.02 (560) |

(The small differences between row 1 here and the record may come from the record's own per-trade
costing path (not investigated); both are negative.) No configuration is net positive in or out of sample. Gross is
positive everywhere and falls as N falls and trade count rises: the N=3 balance is a looser
condition, close to the placebo.

## Placebo comparison (same stocks, same exits, same cost, same hold-out)

| Variant | Trades | Gross bps | Net bps | Gross t |
| --- | --- | --- | --- | --- |
| Balance breakout (config 1) | 1,657 | +8.53 | -3.02 | 3.23 |
| P1 non-balance range breakout (N5, H above 1.5 ATR) | 44,121 | +2.38 | -9.17 | -0.67 |
| P2 random entry on balance days, same stop/target distances | 5,628 | +0.60 | -10.95 | 0.72 |

Balance adds about +6.2 bps gross over the non-balance placebo (approx t 2.4) and the breakout
trigger adds about +7.9 bps over random entry on balance days (approx t 2.9). Both ingredients
carry gross information. The placebos' gross is mostly a short-side drift (shorts +9.3 and +5.0,
longs -3.6 and -3.8), consistent with the known NSE intraday drift; the real rule's long side
(+6.4) is above that drift, which is the clearest sign of a genuine long-side contribution.

## Acceptance criteria

Evaluation of the recorded run: 12 pass, 11 fail, 16 report, 30 not evaluated. Hard failures: G1.2
net expectancy -0.031R against +0.05R, G1.3, G2.2, G4.4, G4.5. Per the lifecycle the verdict stops
at the first HARD fail: G1.2. Also failing: dispersion (44% of symbols net positive), OOS result,
walk-forward (9 of 19 windows), sub-periods (4 of 10 years), day-clustered t (-0.99 vs 2.64),
deflated Sharpe (0.008), slippage stress (net -0.123R at 2x). `run_verdict` INCONCLUSIVE, promotion
NOT_READY.

## Limitations and doubts

- Daily value areas come from 5m bars with volume spread over each bar's range, not 1m or tick data.
  The 5m profile is coarser, though the 70 percent area is a smooth statistic.
- The universe is today's F&O list (survivorship bias); the dataset has no point-in-time membership.
- Spec window mismatch: the declared entry window "10:15-13:00" is read against bar open times by
  the session check, while the signal bars open 10:10 to 12:40 (they close 10:15 to 12:45). G6.1
  flags 446 fills as outside the window. The rule itself is as pre-registered. A fix is a new
  version with the window written as signal-bar open times; it does not change the P&L.
- The 5 non-recorded trials come from a driver outside the run system (same engine, data, cost
  model and cut). They are counted in `n_trials`. Placebo t values are approximate (Welch on trades,
  not day-clustered) except where marked.
- Rarity: the primary balance fires on about 1,650 stock-days in 9.8 years across 206 symbols, so
  per-year samples are 110 to 230 trades.
- Costs dominate: median stop is 0.92 percent of price, cost 11.55 bps. Anything below about 12 bps
  gross per trade cannot pass; the gross edge here is 8.6.
- Gap-away opens that never crossed the edge are skipped by the fresh-cross rule (a pre-registered
  choice); that excludes some strong trend days.

## Is it worth a paper test

No, as defined: the net loss is consistent across IS, OOS, both stops and both triggers. If revisited,
the only lever worth studying is cost: the strategy needs about 4 bps less round-trip cost, or a
higher-payoff variant (the short side alone is net -1.1 bps, closest to breakeven). Tuning that now
would be a post-hoc search on seen data and needs a fresh trial count and a hold-out not yet used.

## v2 wider balance (2026-10-28)

Verdict: FAIL the pre-registered decision rule. The primary (H at least 1.5 percent of the broken
edge) is net negative in sample (-1.30 bps) and out of sample (-3.83 bps). It does beat the
non-balance placebo under the same filter (+7.1 bps gross, approx t 2.3), but that was already
true of v1. Requiring a wider balance does not lift the gross edge enough to pay the 11.55 bps
cost, and the H% buckets show no "wider is better" trend.

Pre-registration: `idea.md`, "v2 pre-registration", written before any v2 result. Same data,
universe, engine, cost model and 2023-11-08 cut as v1. That hold-out was already seen by v1, so it
is NOT a clean hold-out for v2. n_trials 6 -> 9 (primary material + 2 external secondary trials;
placebos are not trials).

| Config | Trades | Gross bps | Net bps | Net t (day) | Gross t (day) | PF | IS net | OOS net (n) | Long net | Short net |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v1 (H% >= 0) | 1,657 | +8.53 | -3.02 | -0.99 | 3.23 | 0.92 | -2.37 | -4.28 (560) | -5.18 | -1.14 |
| v2 primary H% >= 1.5 | 1,352 | +9.44 | -2.11 | -0.47 | 3.20 | 0.97 | -1.30 | -3.83 (433) | -4.09 | -0.45 |
| v2 trial H% >= 2.0 | 981 | +8.90 | -2.64 | -0.45 | 2.51 | 0.95 | -2.54 | -2.89 (288) | -3.24 | -2.13 |
| v2 trial H% >= 2.5 | 680 | +9.58 | -1.97 | -0.14 | 2.14 | 0.95 | -0.91 | -4.51 (200) | -1.04 | -2.79 |

Mean round-trip cost 11.55 bps in every row (statutory 3.55 bps plus 4 bps slippage per side).
Rows come from the unrecorded driver; the recorded primary run is below.

v1 trades by H% bucket (descriptive, not a trial):

| H% of broken edge | Trades | Gross bps | Net bps | OOS trades | OOS net bps |
| --- | --- | --- | --- | --- | --- |
| < 1.0 | 63 | +8.89 | -2.66 | 18 | +12.07 |
| 1.0 - 1.5 | 242 | +3.38 | -8.17 | 109 | -8.79 |
| 1.5 - 2.0 | 371 | +10.85 | -0.70 | 145 | -5.68 |
| 2.0 - 2.5 | 301 | +7.39 | -4.16 | 88 | +0.77 |
| >= 2.5 | 680 | +9.58 | -1.97 | 200 | -4.51 |

The primary's improvement over v1 comes almost entirely from dropping the weak 1.0-1.5 bucket,
not from a monotonic width effect, and no bucket is net positive both in total and out of sample
with a usable sample.

Placebo P1 (non-balance, H > 1.5 x ATR14) with the same H% >= 1.5 filter: 44,119 trades, gross
+2.38, net -9.17 (the filter removes almost nothing, non-balance ranges are already wide). Primary
minus placebo: +7.06 bps gross, approx Welch t 2.34.

Decision rule: net positive IS - fail; net positive OOS - fail; beats P1 under the same filter -
pass. Overall: FAIL. The width lever is exhausted on this data; the cost gap (about 2 bps at best)
remains.

Recorded run: `runs/2026-10-28-multi-day-balance-breakout-v2.json` (strategy version v2, parent
v1). 1,352 trades, gross +9.48 bps, net -2.06 bps, t -0.47 against hurdle 2.80, PF 0.97,
expectancy -0.012R, max drawdown 54R, IS net -1.23 bps (919 trades). n_trials 9. Recorded with
`--allow-dirty` (code_version `3d6424063-dirty`, reproducible false, not promotable).
run_verdict INCONCLUSIVE. The small differences from the driver row (+9.44 / -2.11) are the same
per-trade costing difference seen for v1.
