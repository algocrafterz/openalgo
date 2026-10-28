# IB% library and long-history narrow-IB test, October 2026

Isolated research. The reusable calculation lives in `signal_engine/features/ib_width.py`; the
scripts here only fit its definition to the BreakingTrade website and use it over the long history.

## Pre-registration (written 2026-10-28, before any result in this folder)

- Step 1 picks the IB% definition by match quality against `snapshots.ib_pct` (median absolute
  error, correlation, share within 5 points) at late-morning snapshots. Candidates: IB range or
  session range so far, divided by mean daily range (n = 5, 10, 14, 20), Wilder ATR (n), simple ATR
  (n), mean IB range (n), prior day range; and IB range as percent of price.
- Step 3 hypothesis: the narrow IB% third beats the wide third, in both periods. Terciles are cut
  on in-sample trades only (before 2023-11-08). Pass needs narrow > wide in IS AND OOS, gross and
  net, on both the IDEA-034 v1 trades and the non-balance placebo. A positive narrow-third net in
  both periods is required before IB% is suggested as an entry filter.
- Costs: primary scenario as recorded on each trade (Flattrade statutory plus 4 bps per side). t is
  clustered by day.

## Files

| File | Purpose |
|---|---|
| `fit_definition.py` | Fits 45 candidate definitions against `snapshots.ib_pct` at the 10:31 and 12:01 polls |
| `fit_refine.py` | Refines the best family: IB end time (10:00 to 10:45) and averaging length (3 to 10 sessions, mean, median, EMA, Wilder) |
| `long_history.py` | Applies the library to the IDEA-034 v1 trades and the non-balance placebo, 2017-2026 |
| `results/` | Outputs (ignored by git; rerun to regenerate) |

Inputs (read-only): `signal_engine/data/breakingtrade.db`, `db/historify.duckdb` (1m bars), and
for `long_history.py` the `frames.pkl` built by `../2026-10-ib-reference-levels/inputs_frames.py`
plus that folder's `results/sample_a_*.csv`.

## Results (2026-10-28)

**Definition.** The site does not publish a formula. Best fit: IB range (09:15-10:15) divided by
the mean high-low range of the previous 6 completed sessions, times 100. On 6,503 snapshots at
12:01: median absolute error 3.7 points, 60 percent within 5 points, 15 percent within 1 point,
rank correlation 0.94, no bias (median residual -0.03). It puts a stock in the same
narrow/mid/wide third as the site 85 percent of the time, and on the same side of the site's own
30 and 90 thresholds 98 and 96 percent of the time. The residual is most likely the site's own
data feed. The numerator is the IB, not the range so far (IB fits better at 12:01). Next best:
mean of 5 sessions (error 5.1), Wilder ATR 5 (5.3). It is a close proxy, not an exact copy.

**Long history (2017-2026, terciles cut in sample: IB% 47 / 65 for IDEA-034, 49 / 70 placebo).**
Net bps after the primary cost; t clustered by day.

| Set | Third | IS n | IS gross | IS net | OOS n | OOS gross | OOS net |
|---|---|---|---|---|---|---|---|
| IDEA-034 v1 | narrow | 366 | +19.6 | +8.1 (t 0.9) | 119 | +12.7 | +1.1 (t 0.2) |
| IDEA-034 v1 | mid | 365 | +9.6 | -2.0 | 191 | -2.4 | -13.9 |
| IDEA-034 v1 | wide | 366 | -1.7 | -13.2 | 250 | +12.1 | +0.5 |
| Placebo | narrow | 9,048 | +4.9 | -6.6 | 5,130 | +3.8 | -7.8 |
| Placebo | mid | 9,049 | +0.5 | -11.1 | 5,799 | +1.2 | -10.4 |
| Placebo | wide | 9,048 | -2.3 | -13.9 | 6,047 | +8.4 | -3.2 |

Verdict against the pre-registration: **not supported.** Narrow beats wide in sample in both sets,
but out of sample the gap closes in IDEA-034 (+12.7 vs +12.1 gross) and reverses in the 44,000
placebo trades (+3.8 vs +8.4). The IDEA-034 narrow third is net positive in both periods, but on
t 0.9 and 0.2, and its out-of-sample edge over wide is nil, so it is not a filter that can be
relied on. The site's own "<30" bucket looks best (IS +23.1, OOS +8.7 net) on 54 and 18 trades:
too few to act on. Look-ahead check: 80 trades recomputed from bars before entry, 0 mismatches.
No BREAKINGTRADE long-history proxy exists (its picks come from the website, from August 2026).
