# IB width and multi-day reference levels as entry criteria, October 2026

Read-only research. Nothing here is imported by the engine, the scanner or the backtest system.

## Pre-registration (written 2026-10-28, before any result of this study was seen)

Directions come from auction market theory. They were fixed before the scripts ran. The
direction of a trade is +1 long, -1 short; "in the trade direction" means above for a long and
below for a short.

- **H1 IB width.** IB = 09:15-10:15 IST range. Width = IB range / daily ATR14 (ATR from completed
  prior sessions only). Terciles are cut on the in-sample period only (before 2023-11-08 for
  sample A; P1 for sample B) and applied unchanged to the later period.
  Narrow IB (bottom tercile) -> more range extension, better breakout follow-through.
  Wide IB (top tercile) -> worse, more rotation and fade.
- **H2 open vs prior value.** Open outside the prior day's 70% value area in the trade direction
  -> better continuation than an open inside value. Open outside against the trade -> worst.
- **H3 naked POCs.** A naked POC is a prior session's POC (last 10 sessions) not traded through
  by any later bar up to the entry bar. One lying between entry and target in the trade direction
  -> magnet, better. One lying between entry and stop -> no effect or worse.
- **H4 entry vs value.** Entry beyond the prior day's POC and beyond the 5-day composite POC in
  the trade direction (trading away from value in the direction of acceptance) -> better than an
  entry on the value side of them.

Decision rule: a hypothesis is "supported" only if the predicted ordering holds with the same sign
in both periods (IS and OOS for A; P1 and P2 for B). A filter is worth a new pre-registered
version only if it is supported AND its bucket is net positive after costs in both periods.

## Samples

- **A, 10 years** (`sample_a.py`): IDEA-034 v1 recorded trades plus the P1 non-balance placebo
  trades (same engine, dataset `nse-intraday-5m-20261021-002`, primary Flattrade cost plus 4 bps
  slippage per side). IS before 2023-11-08, OOS from it.
- **B, BreakingTrade live era** (`sample_b.py`): BREAKINGTRADE baseline replay trades from
  `../2026-10-amt-replay/`, joined to `breakingtrade.db` `snapshots.ib_pct` at the entry poll, with
  the same features from Historify 1m bars. About two months; shadow evidence only.
  P1 2026-08-18..09-23, P2 2026-09-24..10-27.

All profiles use `signal_engine.backtest.strategies.balance_breakout.value_area` (70% VA, 0.05%
bins). t is clustered by day. Results CSVs go to `results/` (ignored by git).

## Files

| File | What it does |
|---|---|
| `features.py` | Entry-time features (IB/ATR, IB %, open vs prior VA, naked POCs, prior and 5-day composite POC) and the bucket table |
| `sample_a.py` | Sample A study; needs `frames.pkl` and `trades.pkl` |
| `inputs_frames.py`, `inputs_trades.py` | The unrecorded IDEA-034 drivers that build those two pickles (V1 and placebo trades, same engine and costs as the recorded run) |
| `sample_b.py` | Sample B study |
| `results/` | `sample_a_*.csv`, `sample_b.csv`, `*.out` (ignored by git) |

## Results (2026-10-28)

Look-ahead check: features recomputed from bars truncated before entry matched exactly
(60 sampled trades in A, 25 in B, 0 mismatches).

Net is after Flattrade charges plus 4 bps slippage per side (about 11.55 bps round trip). t is
day-clustered on net.

**H1 IB width: not supported.**

| Sample | Bucket | IS / P1 n, gross, net (t) | OOS / P2 n, gross, net (t) |
|---|---|---|---|
| A IDEA-034 | narrow | 366, +19.6, +8.0 (1.2) | 127, +6.8, -4.7 (-0.7) |
| A IDEA-034 | wide | 366, -2.3, -13.8 (-2.9) | 242, +7.9, -3.6 (0.3) |
| A placebo | narrow | 9,048, +5.1, -6.5 | 5,372, +3.8, -7.7 |
| A placebo | wide | 9,049, -4.7, -16.3 | 5,747, +7.5, -4.0 |
| B BREAKINGTRADE, IB/ATR | narrow | 19, -3.9, -15.4 | 20, +21.8, +10.2 |
| B BREAKINGTRADE, IB/ATR | wide | 18, -10.1, -21.6 | 45, -3.1, -14.6 |
| B BREAKINGTRADE, site ib_pct | narrow | 19, +2.9, -8.6 | 14, +29.1, +17.6 |
| B BREAKINGTRADE, site ib_pct | wide | 19, -4.0, -15.5 | 56, +1.5, -10.1 |

Narrow beats wide in sample A in-sample only; out of sample the order reverses in both the
strategy and the 44,121-trade placebo. In B the site's narrow ib_pct tercile is best in both
periods, but on 19 and 14 trades (t about 1); shadow evidence only.

**H2 open vs prior value area: not supported; the opposite showed.** An open outside value in
the trade direction was the worst or near-worst bucket. In the placebo, an open outside value
AGAINST the trade was best in both periods (IS +6.0 gross, OOS +7.9, against +2.4 and +5.0 for an
inside open). That is a post-hoc observation, not a tested rule.

**H3 naked POC: not supported.** IDEA-034 target-side naked POC: IS net +1.2 (149), OOS +13.0 (62),
t under 1. The placebo shows no magnet (target side -11.3 / -5.6 vs none -10.3 / -7.0). In B the
target-side bucket is worse than none in both periods.

**H4 entry vs prior/composite POC: untestable in A** (every breakout of a multi-day range is
already beyond both POCs). In B the sign flips between periods.

**Filters:** none is net positive in both periods in either sample. No IDEA-034 or BREAKINGTRADE
filter from H1-H4 is worth a new version on this evidence.
