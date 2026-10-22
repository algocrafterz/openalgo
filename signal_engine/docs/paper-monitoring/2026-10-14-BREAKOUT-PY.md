BREAKOUT-PY paper run review, 2026-10-14 (IST)

Scope and status
- Read-only review. Data covers the log up to 11:45 IST; the session was still live. Nothing was changed or restarted.
- Sources: logs/signal_engine_analyze_2026-10-14.log, data/trades.db (table trades), data/runner_state.db (table shadow_events), pipeline/cycle.py, pipeline/pretrade.py, validator.py, strategy_core/breakout.py, config.yaml.
- Engine ran BREAKOUT v1.0, fingerprint a7247069a3dd, 40-symbol universe, product MIS, paper_only, max entries per direction per day 2.
- One day is not evidence about the strategy. Items below are labelled BUG, EXECUTION or TUNING-IDEA with a severity.

1. Today's trades and signals (up to 11:45)

BREAKOUT-PY fired 6 signals and skipped 3 more at a pre-trade gate:
  10:25 bar  PFC LONG   343.90 sl 342.8683 tp 345.4476   dispatched 10:30:15, filled 344.40 (slip +0.50), qty 881
  10:35 bar  SBIN SHORT 947.85 sl 950.6936 tp 943.4213   declined: Max positions (1/1)
  10:35 bar  AXISBANK LONG 1272.30 sl 1268.4831 tp 1278.0254   declined: Max positions (1/1)
  10:45 bar  M&M LONG   2844.20 sl 2835.6674 tp 2852.7326  dispatched 10:50:21, filled 2844.40 (slip +0.20), qty 106, still open at 11:45
  11:30 bar  NTPC LONG  314.05 sl 313.1079 tp 314.9922   declined: Max positions (1/1) (M&M open)
  11:35 bar  M&M LONG   2844.30 sl 2835.7671 tp 2852.8329 declined: POSITION_OPEN (M&M already held by this strategy)
  Skipped before dispatch by the SL-distance gate: M&M 11:05 bar, SHRIRAMFIN 11:20 bar, SUNPHARMA 11:35 bar (see BUG 1).

Closed trade: PFC, exit by tracker at TP1, fill 345.45 at 10:45:24, P&L +925.05 INR, held 15 min.
- R multiple on the signal risk (entry 343.90 to stop 342.8683 = 1.0317 per share, about 909 INR per R): +1.02R.
- R multiple on the actual fill (risk 344.40 - 342.8683 = 1.5317 per share, about 1,349 INR): +0.69R. The engine log reports +0.7R.
- The planned target was 1.5R from the signal price. Entry slippage of +0.50 consumed about 0.48R of it.
Open: M&M 106 shares at 2844.40, stop 2835.67, TP1 2852.73. No exit yet in the log.
BREAKOUT-PY realised so far: +925.05 INR on 1 closed trade. Win rate and averages are meaningless at n=1.

Pine BREAKOUT tag today (for parity): AXISBANK SHORT 10:10 and ASHOKLEY SHORT 10:15 refused by ENTRY_HOLD or superseded; SBIN SHORT 947.85 at 10:45 superseded (same price as the Python 10:35-bar signal, Pine alert arrives about 5 min after the Python dispatch); NTPC LONG 312.45 at 10:50 superseded (Python did not fire NTPC until 11:30 at 314.05); VEDL LONG declined on sizing/min price 300; RECLTD LONG executed (Pine book, not a Python-owned symbol). Parity is partial and cannot be scored from one partial day.

2. Execution issues

EXECUTION-1 (HIGH, operational). ENTRY_HOLD kept the engine halted from the first cycle (09:25) to just before 10:25. Eleven cycles were logged "skipped: engine halted" (09:25 to 10:20), plus the engine refused ORB TATAPOWER 09:50, ORB PFC 10:00 and Pine ASHOKLEY 10:15 ("operator hold (ENTRY_HOLD): restart verification after safety fixes 616386b19 5346431dd"). BREAKOUT-PY was blind for roughly the first hour of the session, which is the busiest breakout window, and the bars were not caught up afterwards. Today is therefore a partial session and must not count as a clean v1.0 session in the parity count (Wilson gate, 20 sessions). Pine AXISBANK 10:10 has no Python counterpart for this reason.

EXECUTION-2 (MEDIUM). "Max open positions (1/1)" declined 3 valid signals (SBIN, AXISBANK at 10:40, NTPC at 11:35). Cause: BREAKOUT-PY runs paper_limits: live, and live max_open_positions = -1 resolves to 1 on 100k sandbox capital. This is the known capacity-model issue already recorded by the owner (deployment-profile effect, not a BREAKOUT rule); redesign is pending. Today it cost two simultaneous signals at one bar (SBIN and AXISBANK both fired on the 10:35 bar; the engine handled them in the order SBIN then AXISBANK with no stated tie-break rule seen in the log). EVWAP-EARLY-PY runs 6 or more concurrent positions on the same broker, so the cap is strategy-book only.

EXECUTION-3 (LOW, working as designed). POSITION_OPEN decline for M&M at 11:40: the policy correctly blocks pyramiding. Note the core re-fires on a fresh break of the same level while a position is open (10:45 and 11:35 bars, entries 2844.20 and 2844.30), so this decline will recur.

EXECUTION-4 (LOW, working as designed). "Pine exit not acted on" at 10:15 (AXISBANK), 11:20 and 11:35 (NTPC). The Python strategy owns those symbols and holds no position under the Pine tag, so the Pine EXIT is correctly ignored. Verified: for NTPC the Pine position never existed in the engine (entry superseded at 10:50). No defect. Operator caveat: the Pine exit text implies a TP1/TP2 hit on a trade the engine never took, which can confuse the Telegram trail.

EXECUTION-5 (LOW for this strategy). Order timeouts at 10:36 (HINDALCO, LAURUSLABS) belong to EVWAP-EARLY-PY, not BREAKOUT-PY. Both timed out sending the SL (10:36:04, 10:36:06). LAURUSLABS: the SL was found live on the broker despite the client timeout (id 26101457992577). HINDALCO: attempt 2 placed a new SL at 10:36:14 (id 26101459059042) with no "confirmed live despite timeout" line for attempt 1, so a duplicate protective stop at the broker is possible. This is the same orphan/duplicate stop family as the live-blocker already on file. The timeouts coincide with the bar-history ReadTimeouts below, which points to a short server stall around 10:35-10:36.

EXECUTION-6 (MEDIUM). Bar history ReadTimeouts at 10:35:15 to 10:35:46 (LICI, TATAPOWER, APOLLOTYRE; TATAPOWER and APOLLOTYRE retried 4 times). history_timeout_seconds is 10 and cycle_deadline_seconds is 25. Result: BREAKOUT-PY skipped TATAPOWER and APOLLOTYRE for the 10:30 bar as "bar late (not published by the cycle deadline)". Silent signal-loss path: if either name had a break on that bar, it was never evaluated and never retried. Also, cycle durations are long and growing (15 to 72 s; 47 s at 11:40 and 11:45) against a 25 s deadline for the history phase.

EXECUTION-7 (LOW). Min-price filter: the only "below min price 300" line today is VEDL on the Pine BREAKOUT tag (253.45), declined by sizing/price band. No BREAKOUT-PY signal was dropped by the price band. No issue for the Python strategy.

EXECUTION-8 (MEDIUM, cross-strategy). At 11:45 EVWAP-EARLY-PY opened M&M (qty 133, fill 2844.20) while BREAKOUT-PY still held M&M (qty 106, fill 2844.40). The tracker keys positions as symbol:strategy, but a netted broker or sandbox position is per symbol. Two protective stops and two independent exits on the same symbol may mis-size when either strategy closes. Check the M&M exits when they happen. No evidence of harm yet.

3. Logic review against the frozen spec

BUG-1 (HIGH). The SL-distance gate rejects stops that sit exactly at the floor because of floating point noise. Verified, not assumed.
- Core: strategy_core/breakout.py sets the stop at min_dist = max(0.5 ATR, close * SL_MIN_PCT) from entry, with SL_MIN_PCT = 0.003. Whenever the 0.5 ATR term is smaller, the stop is exactly 0.30% from the close.
- Gate: validator._check_sl_distance computes abs(entry - sl) / entry and tests it with "<" against 0.003 (config BREAKOUT-PY min_sl_pct, raised from 0.002 to 0.003 in commit 5346431dd, effective today). Result is a coin flip on the last bit.
- Evidence from today's shadow_events: AXISBANK gives 0.0030000000000000256 (pass), M&M 10:45 0.003000000000000001 (pass), M&M 11:35 0.003000000000000054 (pass), NTPC 0.003000000000000084 (pass), PFC 0.0030000000000000022 (pass). The three rejected ones (M&M 11:05, SHRIRAMFIN 11:20, SUNPHARMA 11:35) were logged as "SL distance 0.3000% below minimum 0.3000%", identical when printed at 4 decimals, so the true value is just under 0.003.
- Scale: of 101 BREAKOUT-PY signals recorded since 2026-10-01, 80 sit within 1e-9 of 0.30%, and recomputing with the same formula 29 of the 101 (about 29%) fall just below 0.003. Under the old 0.002 floor none were affected. Under the new floor about 3 in 10 valid v1.0 signals will be dropped at random for the rest of the run. Rejected signals are logged as skips, not as declines, so they will not appear in the trades table or the decline counts.
- The config comment on the new floor says "changes no signal today" and "all 92 entries since 2026-09-25 sit at or above 0.30%". That is true for the core and false for the validator; it overlooked the float boundary.
- Impact today: zero in rupees (M&M was already held; SHRIRAMFIN and SUNPHARMA would have been declined by the 1-position cap while M&M was open). Impact going forward: it breaks the "one shared code path" principle and the spec (a stop exactly at the spec floor is legal) and distorts the parity count against Pine.
- Fix is not applied (frozen). Candidate fix for after the observation window: compare with a small tolerance (for example round the percentage to 6 decimals, or test sl_pct < floor - 1e-9) in validator._check_sl_distance. This does not change the spec; it makes the gate agree with it. Needs owner approval and a restart.

BUG-2 (LOW, accounting). The skip for BUG-1 is recorded as a runner skip only (shadow_events kind skip). The daily report's decline counts will understate lost signals unless the report reads shadow_events skips. Check the EOD report treats these as lost signals.

Spec checks that came out clean
- Re-entry cap, break-stamp reset, POSITION_OPEN policy and supersede logic behaved as the spec describes in every case seen today.
- TP1 only: PFC exited at the TP1 level on the bar that touched it (exit fill 345.45 vs TP 345.4476).
- Stop placement: SL confirmed on the broker for both PFC (id 26101473900973) and M&M (26101410932967) at 342.87 and 2835.67.
- All timestamps in logs, shadow_events (bar_epoch + 19800 = IST) and signal times are IST and consistent. No UTC/IST mismatch found.
- Not checked (no data yet at 11:45): the 14:45 time exit, the 15:00 square-off, DATA_GAP handling, bad-fill close.

4. Tuning ideas (one day of data: all low confidence)

TUNING-IDEA-1 (MEDIUM value, high overfit risk if acted on). Entry slippage is large relative to the stop. PFC slipped +0.50 on a 1.03 stop (0.48R). M&M slipped +0.20 on 8.5 (0.02R). Orders go out 15 s after the bar close as market orders. Memory shows MARKET entry was chosen on purpose (limit orders got a 30 to 50 percent fill rate), so do not switch to LIMIT. Possible lever: send the order earlier in the cycle (cycles take 13 to 72 s). n=2, do not tune; collect slippage in R across 20 or more sessions first.

TUNING-IDEA-2 (MEDIUM). Position cap of 1 hides valid signals. Today's 3 cap declines would be the first evidence for the pending capacity redesign. The paper book should see every valid signal; a one-position cap makes the paper P&L unrepresentative of the strategy and of Pine. This is a deployment design decision, not a rule change. Do not pick a number from today.

TUNING-IDEA-3 (LOW). The history timeout (10 s) and cycle deadline (25 s) are shorter than observed stalls (about 30 s at 10:35). A longer deadline for the last retry, or a catch-up pass for late bars, would remove the silent loss path. Risk: late bars push the order later and increase slippage (idea 1). Needs several sessions of late-bar counts.

TUNING-IDEA-4 (LOW). The first-hour hold cost the best breakout window. This is an operator process matter, not a strategy parameter: schedule restart verification before 09:15 so an authoritative session is not lost. No parameter change suggested.

Overfitting note: 1 closed trade, 1 open, 6 signals, 3 skips. No parameter (stop multiple, floor, target, cap) should be changed on this data. The only defect with solid evidence is BUG-1, and its fix removes a bug rather than tuning an edge.

Summary in plain words
BREAKOUT-PY made one winning trade so far (PFC, about +925 rupees) and holds one open trade (M&M). The engine was paused by the operator hold for the first hour, so today is not a full session. The strategy got six real signals but only took two: three were turned away because only one position is allowed at a time, and one because it was a repeat of a trade already open. Separately, a real bug is throwing away about three in ten signals today and in the days ahead: the stop-distance check compares a number that should equal 0.30% with 0.30% and sometimes it comes out a hair under because of computer rounding, so the signal is wrongly thrown out. Nothing was changed because the run is frozen; the fix is small and should be approved and applied at the next restart. The order timeouts and data timeouts at 10:35 mostly hit the other strategy (EVWAP-EARLY-PY), but the data timeouts did cause BREAKOUT-PY to miss checking two symbols for one bar.
