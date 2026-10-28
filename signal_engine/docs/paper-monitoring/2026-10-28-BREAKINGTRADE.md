BREAKINGTRADE paper run review, 2026-10-28 (IST)

Scope and status
- Read-only review. Session closed; data cut-off 16:06 IST. Engine stopped 16:00:05, restarted 16:03:44.
- Mode: analyze (sandbox). Capital override 1,00,000; risk 1% = Rs 1,000 per trade (1R).
- Sources: signal_engine/data/trades.db (trades, read-only), db/sandbox.db (sandbox_trades,
  sandbox_orders), signal_engine/data/breakingtrade.db (alerts: scan, range_integrity,
  entry_vs_ib), signal_engine/logs/signal_engine_analyze_2026-10-28.log, errors_2026-10-28.jsonl,
  signal_engine/reports/eod-2026-10-28.md and eod-2026-10-28-claude-review.md, OpenAlgo
  /api/v1/history 5m bars (fetched 16:10 IST) for the counterfactuals.
- Market: NIFTY prior close 22,749.75, open 22,689.15 (gap down 0.27%), IB 22,528.0 to 22,691.5,
  low 22,497.7, high 22,778.05, close 22,698.85. A gap-down day that sold off in the first hour
  and then recovered about 1.1% from the low. Every BreakingTrade signal today was a long.
- Companion review: 2026-10-28-BREAKINGTRADE-RI.md (side-by-side comparison is in that file).

1. Trades and signals

Signals: 17 trade signals, 14 filled, 3 declined by the validator (R:R below 0.75).
Slip = fill minus signal entry. R = abs(entry - sl).

| Sym | Entry time | Scan | RI flag | Entry / SL / TP1 | Fill (slip, R) | Qty | Exit | P&L Rs | R |
|---|---|---|---|---|---|---|---|---|---|
| HDFCBANK | 10:30 | Gap-Down Rescue | fail | 675.6 / 668.68 / 686.1 | 675.15 (-0.45, -0.07R) | 131 | TP1 65 @686.25 13:00, rest TIME 684.90 | +1,365.0 | +1.37 |
| WIPRO | 10:30 | Gap-Down Rescue | fail | 185.01 / 182.08 / 188.25 | 185.00 (-0.01, 0.00R) | 310 | NO-PROGRESS 12:36 @185.22 | +68.2 | +0.07 |
| APOLLOHOSP | 10:47 | Gap-Down Rescue | pass | 8035 / 7707.95 / 8345.5 | 8038.0 (+3.0, +0.01R) | 2 | NO-PROGRESS 12:36 @8055 | +34.0 | +0.03 |
| COLPAL | 10:49 | Gap-Down Rescue | fail | 1592.1 / 1574.74 / 1628.0 | 1590.3 (-1.8, -0.10R) | 52 | TIME 14:45 @1611.2 | +1,086.8 | +1.09 |
| RVNL | 10:50 | Gap-Down Rescue | fail | 181.72 / 178.81 / 186.62 | 181.65 (-0.07, -0.02R) | 312 | TIME 14:45 @184.34 | +839.3 | +0.84 |
| BEL | 11:16 | Gap-Down Rescue | fail | 366.15 / 361.68 / 372.6 | 365.70 (-0.45, -0.10R) | 203 | NO-PROGRESS 12:46 @365.25 | -91.3 | -0.09 |
| COFORGE | 11:16 | Gap-Down Rescue | fail | 1942.9 / 1902.24 / 1992.1 | 1947.5 (+4.6, +0.11R) | 22 | NO-PROGRESS 14:06 @1956.2 | +191.4 | +0.19 |
| HCLTECH | 11:16 | Gap-Down Rescue | fail | 1381.1 / 1363.22 / 1400.8 | 1383.9 (+2.8, +0.16R) | 50 | TP1 25 @1400.9 13:10, runner stop 14:39 @1394.8 | +697.5 | +0.70 |
| ANGELONE | 11:46 | Neutral Day Res. Up | pass | 291.95 / 285.11 / 297.25 | 290.65 (-1.3, -0.19R) | 132 | TIME 14:45 @293.05 | +316.8 | +0.32 |
| DATAPATTNS | 11:46 | Neutral Day Res. Down | pass | short 3978 / 4123.14 / 3893.2 | DECLINED R:R 0.58 | - | - | - | - |
| WAAREEENER | 11:47 | Gap-Down Rescue | fail | 2209.9 / 2185.53 / 2269.8 | 2217.0 (+7.1, +0.29R) | 37 | TP1 18 @2269.6 12:39, runner stop 14:04 @2251.6 | +1,604.2 | +1.60 |
| M&M | 12:16 | Neutral Day Res. Up | pass | 2874.7 / 2824.07 / 2898.5 | DECLINED R:R 0.47 | - | - | - | - |
| TECHM | 12:16 | Gap-Down Rescue | pass | 1769.5 / 1732.13 / 1796.2 | DECLINED R:R 0.71 | - | - | - | - |
| MCX | 12:16 | Neutral Day Res. Up | fail | 3186.3 / 3130.62 / 3244.3 | 3184.6 (-1.7, -0.03R) | 16 | NO-PROGRESS 13:46 @3187.6 | +48.0 | +0.05 |
| HDFCLIFE | 12:31 | Live Print in Formation Up | fail | 513.5 / 507.78 / 519.8 | 513.1 (-0.4, -0.07R) | 158 | NO-PROGRESS 14:30 @514.2 | +173.8 | +0.17 |
| TMPV | 12:32 | Neutral Day Res. Up | pass | 262.9 / 259.67 / 265.45 | 262.8 (-0.1, -0.03R) | 281 | TP1 140 @265.55 12:59, runner stop 13:19 @264.7 | +652.9 | +0.65 |
| CONCOR | 12:47 | Neutral Day Res. Up | pass | 419.4 / 411.21 / 425.95 | 419.8 (+0.4, +0.05R) | 111 | NO-PROGRESS 14:17 @416.8 | -333.0 | -0.33 |

P&L is computed from the sandbox fills (sandbox_trades; the 14:45 closes are the untagged
closeposition fills). It matches the fill-backed Strategy P&L page: +6,653.5.

- Closed trades: 14. Winners 11, losers 3. Gross +6,653.5 (+6.65R, +0.48R per trade).
- Charges (EOD report): 307.2. After charges: +6,346.4.
- shadow.py with 4 bps slippage per side and charges: +5,210, PF 10.06, +0.41R per trade.
- Open at the cut-off: none.
- Mean entry slippage: +0.01R. Largest: WAAREEENER +0.29R. Sandbox fills at the LTP, so this
  slippage is not a measure of real market impact.

2. Execution issues

EXECUTION-1 (MEDIUM). Broker API stall in the morning.
- Evidence: 329 "Timeout sending order" or ReadTimeout lines in the engine log; errors.jsonl has
  47 "Failed to fetch positionbook after 3 attempts" and one "POSITION TRACKING BLIND". The engine
  restarted at 11:06:38 (startup restore lines 1037 to 1065).
- Impact on this strategy: 4 entries (HDFCBANK, APOLLOHOSP, COLPAL, RVNL) were adopted "after a
  timeout". RVNL logged "SL failed after 2 attempts - no SL protection" at 10:51:08, then found a
  working stop (26102876490815) at 10:51:22, so the gap was about 14 seconds. No trade lost money
  to the stall. The restart caused BUG-1 below.

EXECUTION-2 (LOW, working as designed). The target is checked by polling, so a short touch is missed.
- Evidence: RVNL TP1 186.62; the 13:00 5m bar has high 186.66. tp_watch polls the LTP about every
  20 seconds and did not see it. The trade ran to the 14:45 time exit at 184.34 (+0.93R).
- Impact: a 4-paise touch. A resting limit order may not have filled either. No change proposed;
  a resting TP order beside an SL-M is the margin problem recorded in memory (Indian broker OCO).

EXECUTION-3 (LOW; FIXED 2026-10-28 17:17, per-leg close orders). The 14:45 time exit is account-wide.
- Evidence: the closeposition fills at 14:45:03 to 14:45:21 carry no strategy tag. They closed
  BREAKINGTRADE-RI's ANGELONE leg with this strategy's (one SELL 264 @293.05) and three
  EVWAP-EARLY-PY positions (JSWENERGY, TATAPOWER, BEL). The EOD Claude review (root cause 4)
  covers the EVWAP side.
- Impact here: none today, because BREAKINGTRADE-RI also exits at 14:45. Any future variant
  with a different exit time would be closed early by this call. Fix before running such a variant.

3. Logic review

BUG-1 (HIGH; FIXED 2026-10-28 17:17, STRATEGY-LOG). A restored leg takes the broker's netted average price as its entry fill.
- Evidence: signal_engine/startup.py:523 sets `fill_price=float(bp.get("average_price"))` from the
  broker positionbook for every restored leg, although trades.db holds the leg's own fill.
  - HDFCBANK: this strategy bought 131 @675.15 (order 10:30). EVWAP-EARLY-PY held a short on the
    same symbol, and its stop bought 259 @678.6 at 11:05:21. The netted position then showed an
    average of 678.6. After the 11:06 restart the leg was restored with fill 678.6.
  - Log line 2059: "EXIT fill for HDFCBANK: 686.25 -> leg pnl 497.25". The true figure is
    65 x (686.25 - 675.15) = 721.5. The time exit then booked 66 x (684.90 - 678.6) = 415.8,
    total 913.05, against 1,365.0 from the fills. Under-booked by 451.95.
  - APOLLOHOSP: both versions were restored with the same netted average, so each booked +36.5
    against fills of +34.0 (this strategy) and +39.0 (RI).
  - Together: -451.95 + 2.5 = -449.45. This is the full "engine books 6,204.1 vs page 6,653.5"
    gap that the EOD report flagged as FAIL and the EOD review left unresolved.
- Scale: every restart while two strategies hold the same symbol. The wrong fill also feeds the
  break-even stop and the no-progress calculation. Here the HDFCBANK runner stop sat at 682.95,
  measured from 678.6 rather than 675.15, so it was 3.45 higher than intended (about 0.5R).
- Candidate fix: restore `fill_price` from the leg's trades.db entry row (`found` already carries
  entry, sl, tp, order_id) and use the broker average only when there is one leg and no recorded
  fill. Add a regression test with two strategies on one symbol. Needs owner approval because it
  changes the stop placed on restored positions.

Spec checks that came out clean
- Entry window 10:15 to 13:00: all 14 entries between 10:30 and 12:47.
- Entry trigger: each signal is the first poll with a 5m close beyond the signal bar.
- Stop: the IB low minus 0.25 ATR (no tail on these picks). Values match the signal rows.
- TP1 staged exit: 50% booked at TP1 (65/131, 25/50, 18/37, 140/281); runner stop moved to
  TP1 minus 0.3R (log line 2032 for TMPV).
- Validator: three signals below the 0.75 R:R minimum were declined (DATAPATTNS, M&M, TECHM).
- Time exit at 14:45: COLPAL, RVNL, ANGELONE and the HDFCBANK runner closed then.
- No-progress gate (90 minutes, 20%): 7 legs. Counterfactual held to 14:45 with the same stop
  and target: the 7 legs booked +0.11R combined against -0.03R if held (WIPRO, BEL, COFORGE,
  MCX and CONCOR were lower at 14:45; HDFCLIFE and APOLLOHOSP were higher). The gate helped
  slightly today.

Not checked
- Short-side rules (no short reached an order).
- Trend-day split (25/25/50). tp_watch still uses a fixed 50% split by design.
- Flip warning handling. HDFCLIFE got a "BT FLIP WARNING" at 12:46; it is advisory only.

Parity and evidence
- Counts as a session for the shadow.py comparison. Not a clean session: the 11:06 restart
  wrote the wrong fill on HDFCBANK and APOLLOHOSP. Use the fill-backed figures above, not the
  engine log.

4. Tuning ideas

Sample: 1 session, 14 trades. Under the 20-session floor. None is a recommendation to change
this strategy. Any test runs as a new, separately tagged paper strategy.

TUNING-IDEA-1 (observation, high overfit risk). Today's profit is mostly the market's recovery.
- Each trade against NIFTY over the same minutes: mean stock move +64 bps, NIFTY +47 bps,
  excess +17 bps (median +10). WAAREEENER alone gave +214 bps excess; without it the mean
  excess is about +2 bps.
- 10 of 14 trades came from Gap-Down Rescue, the scan built for exactly this kind of day.
- This agrees with the 35-day selection check (STRATEGY-LOG 2026-10-28 02:50): the picks add
  about 0 to 4 bps over the market. One up day does not change that conclusion.

TUNING-IDEA-2 (track only). Entries inside the IB beat entries above it today.
- The 10 trades with range_integrity fail made +4,739 (shadow.py); the 4 that passed made +471.
  The 35-day replay found the opposite. A V-shaped recovery day favours a buy low in the range.
  Keep splitting results by this flag; decide only on 20 or more sessions.

Summary in plain words
BREAKINGTRADE made about Rs 6,650 on paper today (about Rs 6,350 after fees) from 14 trades.
11 of them made money. The market fell in the first hour and then rallied all day, and this
strategy bought early, while prices were still low. Most of the profit came from that rally
rather than from picking especially good stocks, so do not expect a normal day to look like
this. One restart in the morning made the program misprice one HDFCBANK trade, so its own
record shows about Rs 450 less than the broker's fills. The broker's figure is the correct one.
That is a program bug that needs a fix. It did not change any trade decision except to place
one protective stop a little higher than intended.
