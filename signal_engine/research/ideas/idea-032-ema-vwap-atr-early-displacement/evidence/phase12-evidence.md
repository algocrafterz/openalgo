# IDEA-032 Phase 1 and 2 evidence (2026-10-08)

Exploratory, not a recorded run: nothing counts against the trials ledger, and the spec was never
frozen. Machine-readable results: `phase12-results.json`. Data provenance: `phase12-provenance.json`.
Code: `backtest/strategies/ema_vwap_atr_displacement.py`, `backtest/mfe_mae.py`,
`backtest/mfe_mae_report.py`, `backtest/engine.py` (RunConfig.custom_exit_fill, no-stop trades).
Tests: `tests/test_exit_next_open.py`, `tests/test_ema_vwap_atr_displacement.py`, `tests/test_mfe_mae.py`.

## Conclusion (specific to the IDEA-032 hypothesis)

The hypothesis that the +/-0.50 ATR displacement of EMA9 from session VWAP gives useful early-entry
information is NOT supported. This does not disprove EMA/VWAP methods in general. It covers this
signal, 5m bars, this universe and period, entry at the next open, and exit at the zero cross.
Do not re-test it as a new idea. A new idea must differ in its signal definition, not in a
threshold, timeframe, stop or target applied to this signal, and must cite this file.

## Method

- Signal: displacement = (EMA9(close) - session VWAP(hlc3)) / ATR14. Long: previous <= -0.50 and
  current > -0.50. Short: previous >= +0.50 and current < +0.50. First bar of a session cannot
  signal (VWAP resets). EMA9 and ATR14 carry across sessions. Warm-up 100 bars.
- Timing (IST): signal bars open 09:20 to 14:40, judged at the bar close. The 14:40 bar is judged at
  its 14:45 close. Entry at the next bar open, 09:25 to 14:45. Exit at the next open after
  displacement reaches zero. Standing time exit is the 14:45 bar close (14:50). No stop, no target.
  The cutoff is the profile standing time exit (flattrade-nse-cash), not a strategy-specific time.
- Data: published `nse-intraday-5m-20261006-001` (content hash d93dd60a...f517f96f), 203 eligible
  current-F&O symbols, 2017-01-02 to 2026-10-01, 2,400 sessions. In-sample before 2023-11-01.
  Survivorship bias disclosed. Known dataset price-basis defects were not corrected.
- Cost: Flattrade MIS statutory schedule at a fixed Rs 1,00,000 notional plus 4 bps slippage per
  side, 11.55 bps round trip.
- Complete-bins last bin 15:10, the IDEA-029 and IDEA-031 precedent. Requiring 15:25 would drop
  every session since the 2026-08-03 closing-auction template (73 bins) for every symbol, about
  8,300 symbol-sessions. 10,504 symbol-sessions were dropped by the 15:10 rule.
- MFE/MAE: measured from the actual next-open fill, never the signal-bar close. When one bar touches
  both a favorable and an adverse level, the adverse level counts first. 1 ATR is a unit of measure,
  not a risk, because there is no stop.
- Placebo, a direction-matched state placebo: for each real signal, one bar from the same symbol and
  same session that passes the same gates (warm-up, same-session previous bar, next bar exists,
  before the cutoff, valid ATR), is not a real signal, and has displacement already on the signal's
  side of zero within 0.5 ATR with no crossing event. Same direction as the signal, so per-direction
  counts match exactly. Direction is not randomised, because randomising it would add a directional
  confound. 20 seeds, spread at most 0.08. A looser same-session "day" placebo was also run; it
  starts at arbitrary displacement states, so it is a floor and not a fair comparison.

## Phase 1: Benchmark A through the real engine

| | trades | win % | gross bps | net bps | day-clustered t |
|---|---|---|---|---|---|
| all | 498,811 | 46.8 | -2.30 | -13.85 | -50.2 |
| long | 274,620 | 45.8 | -3.98 | -15.53 | -33.3 |
| short | 224,191 | 48.1 | -0.25 | -11.80 | -30.3 |
| in-sample | 320,973 | 46.7 | -3.04 | -14.59 | -40.9 |
| out-of-sample | 177,838 | 47.1 | -0.96 | -12.51 | -31.0 |

- Net per trade -0.459 ATR. Exits: ZERO_CROSS 81.9 percent, TIME_EXIT 18.1 percent.
- Every year 2017 to 2026 is negative (-15.5 to -11.3 bps net). 0 of 203 symbols have a positive mean.
- Up to 150 positions open at once; the result is negative at every level, so concurrency is not the cause.
- Entries at the 14:45 open: 2,902 trades (0.58 percent), mean net -12.5 bps against -13.9 for the rest.
- Jump case (displacement already at or beyond zero on the signal bar): 5,893 trades (1.18 percent),
  mean net -9.5 bps.
- All 498,811 engine trades match the diagnostic's recomputation exactly.

## Phase 2: MFE and MAE (751,264 executable signals, uncapped)

- 78.9 percent reached zero before the time exit. Median MFE 1.79 ATR, median MAE 1.84 ATR: nearly
  symmetric. Long, short, IS and OOS agree within about 0.1 ATR.
- Winners (net > 0) had median MAE 0.45 ATR before the exit; losers had median 1.51 ATR.
- Share of signals whose stop at distance d (ATR) would have been touched before the benchmark exit,
  all signals / eventual winners: 0.5: 66/47 percent; 0.75: 54/33; 1.0: 45/24; 1.25: 38/17; 1.5: 33/13.
  No distance separates winners from losers, so there is no obvious stop boundary to justify a
  TP/SL grid.
- Reach table (favorable level before adverse level), adverse 1.0 ATR: 0.62 (0.5), 0.47 (1.0),
  0.37 (1.5), 0.30 (2.0), 0.25 (2.5). IS and OOS differ by less than 1.5 points in every cell.
- Continuation after the zero cross (trades that reached zero with bars left), real against the
  direction-matched state placebo: post-zero incremental MFE mean 1.96 against 2.07 ATR, median 1.15
  against 1.26 ATR. Share reaching at least 1.0 ATR more: 53.2 against 55.7 percent. Net bps: real
  -13.8, state placebo -10.8. Long -15.3 against -12.7, short -11.8 against -8.4. The placebo is at
  least as strong, so the signal selects no continuation.

## Not done, by decision

No Phase 3 (stop and target grid), no trailing stop, no threshold or timeframe search, no further
parameter search for IDEA-032. Random-direction placebo not run (direction confound).
