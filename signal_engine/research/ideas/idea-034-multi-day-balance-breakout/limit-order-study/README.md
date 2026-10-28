# Limit versus market orders (IDEA-034 v1/v2 and BREAKINGTRADE), 2026-10-28

Read-only research. Nothing here is imported by the engine. Question: would LIMIT entries (and
resting limit targets) cut cost enough to make IDEA-034 or BREAKINGTRADE profitable, once missed
fills and adverse selection are counted? Prior belief to test, not assume (2026-03-13 ORB note):
limit entries on momentum breakouts filled 30-50 percent of the time and the best trades never
came back.

## Pre-registration (written 2026-10-28 before any limit result was computed)

Fill model (conservative). A buy limit fills only if a later 1m bar's LOW is at least one tick
BELOW the limit (strict penetration; a touch does not fill); a sell limit mirrors on the HIGH.
The fill is at the limit price with zero entry slippage. The order rests from the minute the
market order would have been sent (the bar after the signal closes) and is cancelled at the end
of the fill window; unfilled = no trade (0 bps for that signal). If the fill bar also reaches
the stop, the trade is stopped on that bar (the worst case). Data: Historify 1m bars, read only,
IST. Tick: the dated NSE tick table (`backtest.tick_reference.tick_for_price`).

Variants (same signals, stop, target and 14:45 exit as the market baseline):
- L0: limit at the signal price (A: the signal 5m bar close; B: the decision price).
- L1: limit at the broken edge (A: the composite balance edge; B: the IB edge in the trade
  direction). B fallback: when the entry is not beyond the IB edge, L1 = L0.
- L2: limit at signal price - 0.25 x (signal price - edge), toward the edge (same B fallback).
- Each with fill windows of 15 and 30 minutes.
- T1 (sample A only): market entry, target exit as a resting limit (strict penetration, no exit
  slippage); stop and time exits stay market. Also L0..L2 combined with T1. Not run for B: its
  staged targets are sent as market alerts by design.

Costs: Flattrade statutory model (`portfolio.costs.india_intraday`) plus 4 bps slippage per
MARKET leg only (stress 8 bps). Market baseline = 2 market legs.

Report per variant: fill rate; net bps per signal (unfilled = 0) and per filled trade; gross;
day-clustered net t; IS/OOS split at 2023-11-08 (A) or P1 2026-08-18..09-23 / P2 2026-09-24..10-27
(B); long/short; adverse selection = the market-order net of the signals a variant filled versus
the ones it missed.

Decision rule: a variant is interesting only if net per signal > 0 in both IS and OOS (both
periods for B) AND it beats the market baseline per signal. Trials: every variant above counts.

Calibration gate: the 1m market-order replay of sample A must reproduce the recorded gross and
net within about 1 bps before any limit variant is read.

Samples: (A) IDEA-034 v1 recorded signals and trades; v2 primary = the subset with balance height
>= 1.5 percent of the broken edge. (B) BREAKINGTRADE baseline replay (amt_replay variant B),
two months, shadow evidence only.

## Results (computed after the pre-registration above)

Run: `PYTHONPATH=. uv run python <dir>/study_a.py <scratchpad>/trades_v2.pkl` (first run caches
the edges to results/edges.csv), `study_b.py`, then `analyze.py a` / `analyze.py b`.

Calibration. Sample A 1m market replay: gross +8.48 vs recorded +8.70, net -3.07 vs -2.85 (within
1 bps), exit reason agreement 87 percent (1m vs 5m bars), 1,617 of 1,657 trades (40 lack 1m bars).
Sample B reproduces the amt_replay baseline per share exactly (max diff 0.0000), 152 signals.

Sample A, net bps per signal (unfilled = 0); t day-clustered:

| Variant | Fill % | Net/filled | Net/signal | t | IS | OOS | Stress 8 bps | Mkt net of filled / missed |
|---|---|---|---|---|---|---|---|---|
| v1 MKT | 100 | -3.07 | -3.07 | -0.92 | -2.67 | -3.86 | -11.07 | |
| v1 L0 w15 | 92.6 | -0.91 | -0.84 | -0.10 | -1.01 | -0.51 | -4.54 | -6.17 / +35.63 |
| v1 L1 w15 | 41.7 | +5.30 | +2.21 | 1.68 | +1.62 | +3.35 | +0.54 | -12.11 / +3.39 |
| v1 L1 w30 | 51.0 | +2.72 | +1.39 | 1.09 | +0.99 | +2.16 | -0.65 | -17.98 / +12.46 |
| v1 L2 w15 | 77.4 | -0.60 | -0.46 | 0.33 | -0.81 | +0.21 | -3.56 | -11.63 / +26.19 |
| v2 MKT | 100 | -2.14 | -2.14 | -0.38 | -1.64 | -3.19 | -10.14 | |
| v2 L1 w15 | 40.9 | +7.12 | +2.92 | 1.94 | +1.98 | +4.89 | +1.28 | -10.92 / +3.95 |
| v2 L1 w15 + T1 | 40.9 | +7.39 | +3.02 | 1.99 | +2.11 | +4.97 | +1.49 | |

T1 (resting limit targets) adds only +0.1 to +0.3 bps: targets are hit on about 8 percent of
trades. v1 L1 w15 beats the market baseline per signal in 9 of 10 calendar years and is positive
in 6 of 10; its fill rate is 36 to 49 percent every year.

Sample B (BREAKINGTRADE, 2 months, shadow): market -8.88 net per signal (P1 -12.23, P2 -6.99).
Best limit L0 w15: -3.39 per signal (P1 -10.78, P2 +0.80), fill 87.5 percent; L1/L2 are worse.
No variant is positive in both periods.

Adverse selection is real and large, as the ORB note said: in every variant the signals a limit
missed were the market order's winners (L0: missed +35.6 vs filled -6.2 bps; L1: +3.4 vs -12.1).
Limit entries win anyway only where the price improvement on the filled trades (entering at the
broken edge rather than about 13 bps beyond it, plus no entry slippage) outweighs the winners
lost.

Verdict against the decision rule: IDEA-034 L1 w15 and L1 w30, with or without T1, in v1 and v2
meet it (positive IS and OOS, beat market per signal). None is significant: the best t is 1.99
against a hurdle near 3, with 28 sample-A variants tried, and the hold-out was already seen by v1.
BREAKINGTRADE: no variant meets it. Treat L1 (retest limit at the broken edge, 15 min) as a
candidate for a new pre-registered IDEA-034 version, not as a result.
