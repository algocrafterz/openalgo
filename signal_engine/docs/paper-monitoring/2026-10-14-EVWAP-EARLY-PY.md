EVWAP-EARLY-PY paper session review, 2026-10-14 (snapshot taken 11:46 IST, session still live)

Scope: read-only review. Strategy is FROZEN for observation. No code, config or process was changed.
Sources: signal_engine/logs/signal_engine_analyze_2026-10-14.log, logs/evwap-early-py_diagnostics_2026-10-14.jsonl,
data/trades.db (read-only), strategy_core/ema_vwap_early.py, pipeline/strategies/ema_vwap_early.py,
pipeline/trail_driver.py, executor.py, config.yaml strategy_profiles.EVWAP-EARLY-PY, project memory note.
All times are IST. R = P&L divided by (qty x |fill - initial stop|).

======================================================================
1. TODAY'S TRADES (to 11:46)
======================================================================

Entries: 13 (8 LONG 10:25-10:50, then 5 SHORT 11:10-11:30, plus M&M LONG at 11:45 still open).
Closed: 11 trades. Open at snapshot: INDIGO (short), KOTAKBANK (short), M&M (long).

Sym         Dir    Entry fill  Exit fill  Exit type        Hold  P&L Rs   R      MFE R  Crossed
INDUSINDBK  LONG   877.80      874.65     INITIAL stop     27m   -1348    -1.06  0.94   no
HINDALCO    LONG   896.35      896.05     TRAILING stop    24m   -96      -0.11  0.51   yes 10:50
TVSMOTOR    LONG   3984.10     3967.60    INITIAL stop     40m   -941     -1.02  0.92   no
PAYTM       LONG   1713.50     1715.80    TRAILING stop    19m   +281     +0.31  1.43   yes 11:00
TCS         LONG   2316.00     2306.70    INITIAL stop     47m   -753     -1.04  0.28   no
LAURUSLABS  LONG   2042.00     2043.80    TRAILING stop    49m   +83      +0.08  0.30   yes 10:50
BAJFINANCE  LONG   971.70      967.90     INITIAL stop     55m   -878     -0.98  0.47   no
LT          LONG   3646.70     3636.00    INITIAL stop     65m   -984     -0.96  0.51   no
AXISBANK    SHORT  1264.90     1260.40    TRAILING stop    29m   +351     +0.39  0.66   yes 11:20
LICI        SHORT  393.60      394.35     TRAILING stop    40m   -321     -0.34  0.05   yes 11:25
TATAPOWER   SHORT  346.85      347.45     TRAILING stop    15m   -127     -0.14  0.08   yes 11:30

Totals (11 closed): net Rs -4,733 (about -4.9R) before charges. 3 wins, 8 losses.
  Initial-stop exits: 5, all losers, all about -1R (-0.96 to -1.06). Sum Rs -4,904.
  Trailing exits:     6, 3 winners (+0.31, +0.08, +0.39) and 3 small losers (-0.11, -0.34, -0.14). Sum Rs +171.
Hold times: median 40 min, range 15-65 min. Nothing has hit the 14:45 time exit yet.
Pattern: every initial-stop loser entered before the crossover and never crossed; 2 of the 5
(INDUSINDBK 0.94R, TVSMOTOR 0.92R) had run almost +1R in favour before reversing to the full stop.
Clustering: 7 longs were opened inside 25 minutes (10:25-10:50) and 5 of them stopped out
between 10:52 and 11:36. This behaves like one market-wide bet, not seven independent ones.
Re-entries/shorts: all 5 shorts were first trades of the day on those symbols.

======================================================================
2. EXECUTION FINDINGS
======================================================================

[EXECUTION - HIGH] Operator hold removed the whole opening window.
  The ENTRY_HOLD file ("restart verification after safety fixes 616386b19 5346431dd") halted the engine.
  Pipeline cycles 09:25 through 10:20 logged "skipped: engine halted"; CRITICAL "ENTRY REFUSED" lines
  appear for other strategies at 09:50, 10:00, 10:15. The first EVWAP-EARLY-PY signal dispatched was
  10:25:36 (bar 10:20). The strategy's allowed window starts 09:20 and the strategy is named "early",
  so the highest-activity part of the day (09:20-10:15) was not evaluated at all today. Today's
  sample is therefore not comparable to the 2026-10-09 (37 trades) and 2026-10-12 (55 trades)
  sessions, and its entries are bunched into one regime (a late-morning drop, then a bounce).
  Number of missed signals is unknown; it can be counted offline by replaying the 09:20-10:15 bars
  through strategy_core (read-only, not done here to respect the freeze).

[EXECUTION - MEDIUM] Signal-to-order latency is 3-6x higher than on 2026-10-09 (median 11 s).
  Dispatch happened 36 s after the bar close at 10:25, 54 s at 10:35 (TCS), 75 s for HINDALCO and
  LAURUSLABS (fill at 10:36:15). Cycle durations were 10-72 s (two runners, 40 symbols each, shared
  app). The 10:35 cycle hit /api/v1/history ReadTimeouts (LICI, TATAPOWER, APOLLOTYRE) and a
  500 (NTPC); TATAPOWER and APOLLOTYRE were skipped as "bar late" for bar 10:30.
  A trigger on those two symbols at that bar, if any, was not traded. Count unknown.

[EXECUTION - MEDIUM] Order timeouts at 10:36:04-10:36:06.
  "Timeout sending order" on the SL-M for HINDALCO and LAURUSLABS ("SL attempt 1/5 failed: Request
  timed out"). HINDALCO's stop was finally placed on attempt 2 at 10:36:14; positions were
  filled and unprotected for roughly 10-15 s. Entries were fine. Check in the sandbox order book that
  no duplicate SL-M exists for either symbol (executor has a trigger/qty match to avoid it; this
  was not verified from the log).

[EXECUTION - LOW] Entry slippage is small and symmetric.
  Fill minus signal close, in R, adverse positive: INDUSINDBK +0.40, LT +0.13, LAURUSLABS +0.10,
  LICI +0.05, TVSMOTOR +0.03, AXISBANK 0, BAJFINANCE -0.01, PAYTM -0.01, HINDALCO -0.04, KOTAKBANK -0.04,
  INDIGO -0.07, TATAPOWER -0.01, TCS -0.21. Mean about +0.02R. One outlier (INDUSINDBK, 0.85 Rs on a
  2.12 Rs stop distance) cost 0.4R; its 428-share size made it the biggest loss (-Rs 1,348).
  Slippage is not a driver of the day's result.

[EXECUTION - LOW] Stop fills vs the strategy's stop level.
  Broker stop fill minus model stop: INDUSINDBK -0.18, TVSMOTOR -0.25, TCS -0.39 (adverse); BAJFINANCE
  +0.07, LT +0.44 (the broker trigger was rounded toward entry, see BUG 1). Average loss at stop is
  -1.00R (range -0.96 to -1.06) so stop execution is clean.

[EXECUTION - LOW] Trailing exits are market orders after the bar close, not resting stops.
  By design (spec: decided at bar close, filled next bar open). Cost versus the trailing level that
  decided the exit: PAYTM exit 1715.80 versus stop 1718.71 (-2.91 Rs, 0.39R, equal to more than the
  whole +0.31R it booked), AXISBANK -0.22 Rs adverse, HINDALCO +0.05, LAURUSLABS -0.05. Delay is
  about 10-15 s after the 5-minute close, but the price had already moved through the level.

[EXECUTION - INFO] "Cancel order ... returned HTTP 400" appeared 5 times: INDUSINDBK 10:52:48, TVSMOTOR
  11:06:04, TCS 11:23:40, BAJFINANCE 11:35:53, LT 11:36:05. Exactly the five initial-stop exits, each
  within the same second as the stop fill. The tracker tries to cancel the SL-M that has just
  filled; the order is already complete so the sandbox answers 400. No orphan orders, no later
  errors, positions closed cleanly ("Position closed" logged each time). Harmless noise, no action.
  (No such warning on trailing exits, where the SL is cancelled before the market exit.)

[EXECUTION - INFO] Risk-limit skips: none for EVWAP-EARLY-PY (paper_limits: mode, max entries per
  direction null). "Max positions (1/1)" and "Pine entry not traded" warnings in the log belong to
  BREAKOUT-PY / BREAKOUT. Strategy-level skips were all FILTERED_EMA20_CONFLICT on shorts
  (BSE 11:00, HAL 11:05, INFY 11:20, HAL 11:25) which is the specified strict EMA20 rule.

[EXECUTION - MEDIUM] Paper exposure is far above anything a real account could carry.
  Day-start capital 100,000; the 7 longs open between 10:25 and 10:50 had a combined notional of
  about Rs 17 lakh (INDUSINDBK 3.75 lakh, LT 3.35 lakh, HINDALCO 2.86 lakh, TVSMOTOR 2.27 lakh,
  BAJFINANCE 2.24 lakh, TCS 1.88 lakh, LAURUSLABS 0.94 lakh), roughly 17x capital. Analyze mode skips
  the margin check ("skipping margin check ... risk-based qty"), so the 1%-risk sizing is unconstrained.
  Live MIS margin (about 5x) would have rejected or shrunk most of these. Paper R results are
  valid per trade, but paper P&L cannot be read as an achievable account return.

======================================================================
3. LOGIC REVIEW (entry, trail, exit vs the spec)
======================================================================

Result: no entry or trail rule violation found. All 13 entries were genuine +/-0.50 displacement
crossings with strict EMA20 alignment, positive momentum change, displacement under the 1.5 cap, and
a confirmed swing stop (stop_basis SWING for every trade; no FALLBACK_EXTREME today). The trailing stop
never loosened, activated only at the EMA9/VWAP crossover, and exited on "touched" in every trail case.
The 14:45 time exit and Python-owned exits are active (trail_driver logged "missed-stop safety ON").
Timestamps in the diagnostics carry +05:30 and agree with the log. No UTC/IST problem seen.

[BUG - LOW] Broker stop trigger is rounded to a coarse tick but the strategy model is not.
  executor.place_sl rounds the LONG trigger UP and SHORT trigger DOWN onto features.instruments.safe_tick
  (0.5 for prices near 3,600-4,000; 0.1 near 875-2,300; 0.05 below 400). The trail driver replay
  (TrailState) uses the raw unrounded initial stop. Effects seen today:
    - LT: model stop 3635.56, broker trigger 3636.00 (0.44 Rs tighter, 0.045R). The broker stop fired
      at 11:36:05 but the model saw no touch, so the diagnostics record exit_reason CLOSED_ELSEWHERE
      with stop_touch_bar None and mae_r 0.96 (never reached 1.0). The memory/report convention treats
      that label as a mismatch to investigate; here it is an ordinary stop hit.
    - TVSMOTOR 3967.85 -> 3968.0, INDUSINDBK 874.83 -> 874.9, BAJFINANCE 967.83 -> 967.9,
      INDIGO 4828.90 -> 4828.5, AXISBANK 1276.43 -> 1276.4: all tighter than modelled, by 0.01-0.1R.
  Consequences: (a) paper stop-outs happen slightly earlier than the backtest/replay would say, which
  inflates the stop-out rate a little; (b) evwap_early_report will label some real stops
  CLOSED_ELSEWHERE. The backtest side has backtest/tickgrid.py to model this, so check that the
  frozen backtest and the paper run use the same grid before comparing hit rates. Fix is a technical
  one (record the rounded trigger as the model stop) but belongs after the freeze review.

[BUG - NONE FOUND] Checked for silent failures: sizing used entry=signal close and sl=initial stop
  with a 10% slip buffer (qty = floor(1000 / 2.33) for INDUSINDBK, as intended); tp=0 on every row
  (validator rule); no TP order placed; every entry has an SL id and "SL confirmed" line; every
  position got a matching close row in trades.db and a "Position closed" log line (11 of 11). trades.db
  P&L equals the tracker P&L for all closed rows.

[NOTE - naming] trades.db signal_time stores the signal bar OPEN time (e.g. 10:20) while the order
  goes out after that bar closes (10:25). This is consistent across rows but reads like a 5-minute
  lag if compared to executed_at without knowing it.

======================================================================
4. TUNING IDEAS (NOT TO BE APPLIED; strategy is frozen. One day, 11 closed trades, no statistical weight)
======================================================================

[TUNING-IDEA - MEDIUM value, HIGH overfit risk] Pre-crossover protection.
  Evidence: the 5 initial-stop losers never reached the crossover, so the trail was never active.
  Two of them had run +0.94R and +0.92R, a third +0.51R, before returning to -1R. Trailing activation
  mode is "crossover" (spec); a break-even or partial-lock rule at some R would have changed 2 of 5.
  Risk: 5 trades, one regime (bounce then fade), selected after seeing the outcome. A break-even rule
  also raises the number of scratch exits on trades that later hit the crossover (PAYTM MFE 1.43R,
  AXISBANK 0.43R pre-cross). Test only on the full 214-symbol history with MFE/MAE tooling, never from
  today.

[TUNING-IDEA - LOW-MEDIUM value, MEDIUM overfit risk] Trailing exit as a resting stop.
  Evidence: PAYTM gave back 2.91 Rs (0.39R) between the trailing level and the market fill; the other
  three trailing exits show +0.05 to -0.22 Rs. Market-after-close exits are what the backtest models
  ("next_open"), so changing live only would break backtest/paper parity (the project rule is one
  shared code path). Would need a backtest variant first.

[TUNING-IDEA - MEDIUM value, HIGH overfit risk] Same-direction concurrency cap / correlation guard.
  Evidence: 7 longs in 25 minutes, 5 stopped within 45 minutes of each other (-Rs 4,904) while the
  shorts that followed were net slightly positive. Risk: single regime, and the morning hold made the
  cluster artificially dense because every setup that formed 09:20-10:15 was released into one cycle
  family. Not evidence about normal days. Compare with the two prior sessions (92 trades) before
  concluding anything.

[TUNING-IDEA - LOW] Execution speed rather than rules. Cutting cycle time from 25-70 s to under 15 s
  would remove most of the 0.0-0.4R entry drift; it is infrastructure, not strategy, and touches no
  frozen parameter. Two runners and a loaded history API were the cause today.

Not supported by today's data (do not act): stop buffer or swing parameters (stop_atr 0.66-1.45 on
the stopped trades gives no pattern), the 0.50 threshold, ATR multiples, entry window, EMA20 filter
(the 4 shorts it rejected were never traded so no counterfactual exists).

======================================================================
5. OPEN CHECKS FOR EOD
======================================================================
 1. Run: uv run python -m signal_engine.analysis.evwap_early_report --day 2026-10-14 ; expect LT to show a
    driver-vs-trades.db mismatch label (tick rounding, BUG - LOW above).
 2. Confirm the 14:45 time exit cancels the working SL-Ms by id (fix dd39289b2) for INDIGO, KOTAKBANK, M&M
    and any later entries.
 3. Verify there is no duplicate SL-M for HINDALCO and LAURUSLABS from the 10:36 timeouts.
 4. Decide whether the missed 09:20-10:15 window should be replayed offline to count lost signals.

======================================================================
LAYMAN SUMMARY
======================================================================
The strategy did what its rules say, with no coding mistakes found. The engine was held back by a
manual safety switch until about 10:20, so it missed the entire early-morning window the strategy is
built for. After that, it opened seven buy trades in 25 minutes and the market turned against five of
them, each losing about one unit of planned risk (about Rs 900-1,350 each). The trades that reached the
trailing stage were roughly break-even to slightly positive. So far 11 trades are closed, 3 won and
8 lost, for about minus Rs 4,700 (paper money, before charges). Order costs from price slippage were
small. The few warnings in the log (cancel errors after stops filled, a short burst of timeouts around
10:36) were harmless. Nothing here is enough evidence to change the strategy: it is one short, odd
morning, and the ideas listed above need testing on the full history first.
