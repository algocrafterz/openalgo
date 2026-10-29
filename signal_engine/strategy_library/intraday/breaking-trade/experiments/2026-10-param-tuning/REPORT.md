# BreakingTrade parameter tuning: report (2026-10-28)

Research only, on replay. Nothing here is proven profitable. No file outside this folder was changed.

## Headline

Baseline B loses money after costs in both periods (P1 -0.099R, P2 -0.047R net of 4 bps per side).
Of 23 one-at-a-time trials (46 trial-periods), NONE met the pre-registered acceptance rule, and
none met even the single expectancy test (+0.03R) in both periods. The closest signals are
directional only and below the bar (section d). The data do not support changing any parameter on
this evidence. The baseline replay was proven identical to B before any variant ran
(`tune.py check`: 55 and 96 rows, identical side, fill, stop, target, quantity, exit, P&L).

## (a) Current rules and where they live

| Rule | Value | Source |
|---|---|---|
| Scan picks (vendor Market Profile scans, direction) | 12 scans, unchanged here | `analysis/breakingtrade/scans.py` |
| Scanner polls | 09:20-10:30 every 5 min; 10:30-13:00 at :01/:16/:31/:46; 14:50-15:10 | `analysis/breakingtrade/__main__.py:90-94` |
| Confirmation window after pick | 2 h | `analysis/breakingtrade/entry_watch.py:48` |
| Entry trigger | first completed 5m bar closing beyond the signal bar high (long) / low (short) | `analysis/breakingtrade/trigger.py:132-150` (rule text at :23) |
| Order | at the poll after the trigger bar closes (replay: that minute's 1m open) | `entry_watch.py:98` `check()` |
| Initial Balance | 09:15-10:15 | `trigger.py:51-52` |
| Stop | far IB edge -/+ 0.25 x ATR(14 x 5m). `stop_level()` accepts a tail level, but `plan_trade()` never passes one, so the tail is never used (a tail-outer stop was identical in replay anyway) | `trigger.py:56` (buffer), `:154-168`, call in `plan_trade()` |
| TP1 / ladder levels | entry +/- 1.0 / 1.5 / 2.0 x IB range | `trigger.py:59`, `:172-174` |
| Ladder split | 50 / 30 / 20 % | `trigger.py:63`, `tp_watch.py:66` |
| min_rr | 0.75 | `config.yaml:366` |
| min_sl_pct (BREAKINGTRADE) | 0.20 % | `config.yaml:1041` |
| Risk per trade / slippage factor | 1 % of capital / 0.10 | `config.yaml:220`, `:249` |
| Product | MIS | `config.yaml:1030` |
| Runner stop after each level | level minus 0.3 x level R, tighten-only | `config.yaml:842` (`tp1_runner_sl_buffer`), `:866` (extended tiers), `main.py:1318` `_replace_runner_sl` |
| No-progress gate | after 90 min if progress < 20 %: market exit if pace cannot reach TP by 14:45, else stop to entry | `config.yaml:655-660` |
| Loss cut | after 20 min if progress < -0.80 | `config.yaml:714-716` |
| Time exit | 14:45 | `config.yaml:515-518` |
| Replay mirror of these exits | `LADDER`, `RUNNER_BUFFER`, `TIME_EXIT`, `simulate()` | `experiments/2026-10-amt-replay/bt_replay.py` |

## (b) Pre-registered grid and rule

Full text in `PREREG.md` (written before the first trial). 21 pre-registered trials: confirmation
window (1h, 1.5h); ATR buffer (0, 0.5); TP1 multiple (0.75, 1.25); min_rr (0.5, 1.0); no-progress
threshold (10%, 30%); no-progress time (120 min); loss cut (-0.6, off); ladder (100% at TP1,
33/33/34); runner buffer (0.2, 0.5); time exit (14:15, 14:30); latest entry (12:00, 12:30). Two
exploratory neighbours were added afterwards and counted (tp 1.5, no-progress 150 min), so
23 trials in total.

Rule: candidate only if, in BOTH periods separately, versus B on net4: expectancy +0.03R or more,
profit factor higher, max drawdown no worse, at least 30 trades, and neither long nor short
expectancy worse than its baseline. Any pass is provisional (about 46 trial-periods were tested).

## (c) Results (net4 = Flattrade charges + 4 bps per side; net8 = 8 bps; R = planned risk)

P1 = 2026-08-18..2026-09-23 (already looked at before, not a clean holdout). P2 = 2026-09-24..2026-10-27.
"fails" lists the rule clauses missed; "dExp" is the expectancy change versus B. The side clause
is strict (any worsening, even 0.001R, counts), so it fires on noise; it never decided an outcome, because every trial misses the +0.03R clause in at least one period.

## (d) What passed and failed, in plain language

Nothing passed. Per parameter (change in net4 expectancy versus B, P1 / P2):

- Confirmation window 1h / 1.5h: 0.000 / -0.017 and 0.000 / -0.002. Fewer late entries change little; 1h is worse in P2.
- Stop ATR buffer 0: -0.044 / +0.011 (P1 much worse, wider-drawdown). 0.5: +0.006 / +0.002 (flat; P2 drawdown improves from -3,025 to -2,162 but P1 PF is lower). No clear effect.
- TP1 multiple 0.75: -0.017 / -0.106 (worse; and it cuts trades to about 34 because the R:R gate then rejects most setups). 1.25: +0.010 / +0.021 with P2 PF 0.85. This is the most consistent direction but under the bar, and it also lets in many more trades through the unchanged 0.75 R:R gate (76 vs 55, 144 vs 96), so it mixes two effects. 1.5 (exploratory): +0.006 / +0.011, so the gain does not grow.
- min_rr 0.5: +0.003 / +0.015 (more trades, same quality). 1.0: -0.013 / -0.134 (loses most trades; clearly worse). The 0.75 gate looks fine.
- No-progress threshold 10%: +0.036 / +0.004 (P1 only; max drawdown slightly worse in P1). 30%: +0.018 / -0.001. No gain.
- No-progress time 120 min: +0.079 / -0.009 (big P1 gain, none in P2; short side in P1 worse). 150 min (exploratory): +0.133 / +0.013. The direction (hold longer before the gate fires) helps in P1 consistently with the size growing, but P2 stays below +0.03R and PF 0.80 vs 0.71. 60 min was already rejected earlier. This is the only parameter whose P1 gain is large, and it is the one most likely to be a regime effect (P1 was a rising, long-heavy period: 46 of 55 trades long) rather than a real improvement.
- Loss cut -0.6: -0.017 / -0.005 (worse). Off: -0.003 / 0.000 (no effect: the loss cut almost never changes an outcome).
- Ladder 100% at TP1: +0.014 / 0.000. 33/33/34: -0.005 / -0.001. Targets are reached too rarely for the split to matter.
- Runner buffer 0.2 / 0.5: +0.004 / 0.000 and -0.003 / -0.003. No effect, same reason.
- Time exit 14:15 / 14:30: +0.002 / +0.007 and +0.002 / +0.004. Negligible: only 2 to 12 trades per period reach the time exit.
- Latest entry 12:00 / 12:30: +0.016 / -0.031 and +0.009 / -0.023. Dropping late entries does not help; P2 gets worse.

Stability test: no value passed, so no stability test of a passing value was required. The two
exploratory neighbours (tp 1.5, no-progress 150) were run only to see whether the two largest
directional moves were smooth or cliff-edges; neither passes. No combinations were tested.

Why nothing works: the strategy has no gross edge to tune (about +0.02R before costs, median best
favourable move about 0.30 of the IB range). The gates mostly decide how a flat, losing-by-costs
trade is closed. Exit parameters change the shape of the loss, not its sign.

## (e) Suggested changes for a SEPARATE experimental paper version (not proven)

Ranked by how much the evidence leans toward them. None meets the acceptance rule; each is a
hypothesis to be tested on fresh data, not a recommendation to trade.

1. Hold longer before the no-progress gate (90 to 120 or 150 min). Old: market exit or break-even stop at 90 min if progress < 20%. New: same rule at 120 or 150 min. Expected effect if real: fewer premature exits of trades that later recover; P1 net4 improved from -0.099R to +0.034R at 150 min, P2 from -0.047R to -0.034R. Weak evidence: one period carries it; P2 gain is 0.013R. Needs a fresh forward sample before any change.
2. Test a wider TP1 (1.25 x IB range) together with a strict R:R gate kept at the planned value, to separate the target effect from the extra trades the gate admits. Old: TP1 1.0 x IB. New: 1.25 x. Expected: a few more trades, slightly better PF in P2. Below the bar in both periods.
3. Keep everything else. The R:R gate 0.75, loss cut, ladder split, runner buffer, time exit and entry cutoff show no sensitivity worth a change. The loss cut and the runner buffer are nearly inert in this data; they can be left alone.

Larger effects than any parameter here would have to come from entry quality or from avoiding the
weak long scans, which are outside this task's scope (the vendor scan conditions were not touched).

## (f) Limitations

- One market regime (a rising Aug-Sep period, then a falling Oct period); small samples (55 to 184 trades per cell). Standard errors of expectancy are about 0.07 to 0.10R, larger than every effect measured here except the no-progress time.
- Multiple testing: 23 trials in 46 trial-periods. At that count a few cells beating B by 0.03R are expected by chance alone.
- P1 had been examined in earlier experiments, so it is not a pristine holdout.
- Historify as of 2026-10-27; a later backfill that corrects old bars can move results slightly.
- Slippage is unknown: 4 bps and 8 bps per side are assumptions, and sandbox fills carry none. Replay fills are the 1m open at the poll minute.
- Replay stops use the IB-edge stop; live uses a reported tail when one exists (tail-outer was identical in earlier tests, tail-inner worse).
- tp_mult and min_rr trials change which trades are taken, so they are not like-for-like trade sets; compare via expectancy and PF, not total rupees.
- Same-bar stop and target count as the stop; gates act at the next 1m open. Both are conservative and identical across trials.
- Look-ahead check was active on every accepted trade in every trial (a mismatch aborts the run); none aborted.
