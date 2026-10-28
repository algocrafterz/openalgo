BREAKINGTRADE-RI paper run review, 2026-10-28 (IST)

Scope and status
- Read-only review. First session of this version (launched 2026-10-28, STRATEGY-LOG 02:31).
  Session closed; data cut-off 16:06 IST.
- Mode: analyze (sandbox). Capital override 1,00,000; risk 1% = Rs 1,000 per trade (1R).
- Sources: as in 2026-10-28-BREAKINGTRADE.md (trades.db, sandbox.db, breakingtrade.db alerts,
  engine log, errors.jsonl, EOD reports, /api/v1/history 5m bars).
- Market: gap-down open, first-hour sell-off, then a recovery of about 1.1% from the NIFTY low.
  All signals were longs.

1. Trades and signals

Signals: 12 trade_signal_ri alerts, 9 filled, 3 declined (R:R below 0.75). Every RI signal was
on a symbol BREAKINGTRADE also signalled. RI entries are after 10:15 and close above the IB
high (range_integrity 1, entry_vs_ib above 0 on all 12).

| Sym | Entry time | Entry / SL / TP1 | R:R | Fill (slip, R) | Qty | Exit | P&L Rs | R |
|---|---|---|---|---|---|---|---|---|
| APOLLOHOSP | 10:51 | 8035 / 7707.95 / 8345.5 | 0.95 | 8035.5 (+0.5, 0.00R) | 2 | NO-PROGRESS 12:36 @8055 | +39.0 | +0.04 |
| HCLTECH | 11:16 | 1383.8 / 1363.22 / 1403.5 | 0.96 | 1384.2 (+0.4, +0.02R) | 44 | NO-PROGRESS 14:39 @1388.5 | +189.2 | +0.19 |
| HDFCBANK | 11:31 | 681.2 / 668.82 / 691.7 | 0.85 | 681.85 (+0.65, +0.05R) | 73 | NO-PROGRESS 13:11 @683.7 | +135.1 | +0.14 |
| ANGELONE | 11:47 | 291.95 / 285.11 / 297.25 | 0.77 | 290.8 (-1.15, -0.17R) | 132 | TIME 14:45 @293.05 | +297.0 | +0.30 |
| DATAPATTNS | 11:47 | short 3978 / 4123.14 / 3893.2 | 0.58 | DECLINED | - | - | - | - |
| COFORGE | 12:01 | 1953.3 / 1902.41 / 2002.5 | 0.97 | 1954.7 (+1.4, +0.03R) | 17 | NO-PROGRESS 13:59 @1964.0 | +158.1 | +0.16 |
| WIPRO | 12:01 | 185.73 / 182.16 / 188.97 | 0.91 | 185.65 (-0.08, -0.02R) | 254 | NO-PROGRESS 13:31 @185.47 | -45.7 | -0.05 |
| M&M | 12:16 | 2874.7 / 2824.07 / 2898.5 | 0.47 | DECLINED | - | - | - | - |
| TECHM | 12:16 | 1769.5 / 1732.13 / 1796.2 | 0.71 | DECLINED | - | - | - | - |
| MCX | 12:32 | 3192.3 / 3131.0 / 3250.3 | 0.95 | 3194.1 (+1.8, +0.03R) | 14 | NO-PROGRESS 14:02 @3183.3 | -151.2 | -0.15 |
| TMPV | 12:32 | 262.9 / 259.67 / 265.45 | 0.79 | 262.8 (-0.1, -0.03R) | 281 | TP1 140 @265.55 12:59, runner stop 13:19 @264.7 | +652.9 | +0.65 |
| CONCOR | 12:47 | 419.4 / 411.21 / 425.95 | 0.80 | 419.6 (+0.2, +0.02R) | 111 | NO-PROGRESS 14:17 @416.7 | -321.9 | -0.32 |

- Closed trades: 9. Winners 6, losers 3. Gross +952.4 from the fills (matches the Strategy P&L
  page). +0.95R, +0.11R per trade.
- Charges (EOD report): 146.0. After charges: +806.5.
- shadow.py with 4 bps slippage per side and charges: +476, PF 1.70, +0.06R per trade.
- Open at the cut-off: none.

2. Side-by-side with BREAKINGTRADE (same scanner picks, same day)

| Group | Symbols | BREAKINGTRADE Rs | BREAKINGTRADE-RI Rs |
|---|---|---|---|
| Same entry (BT signal already above the IB high) | ANGELONE, APOLLOHOSP, CONCOR, TMPV | +670.7 | +667.0 |
| RI entered later, higher | COFORGE, HCLTECH, HDFCBANK, MCX, WIPRO | +2,370.1 | +285.5 |
| BT only (price never closed above the IB high after the signal) | BEL, COLPAL, HDFCLIFE, RVNL, WAAREEENER | +3,612.8 | not traded |
| Total | | +6,653.5 | +952.4 |

- Where the two enter together, the results are the same, as designed.
- Where RI waited, it filled 0.3 to 9.5 rupees per share higher on all five names, and with fewer
  shares (HDFCBANK 73 against 131). It did worse on all five.
- The five names RI never entered made +3,612.8 for BREAKINGTRADE, more than half of its day.
- Move against NIFTY over each trade's own minutes: RI mean -10 bps excess, BREAKINGTRADE +17 bps.

3. Execution issues

EXECUTION-1 (MEDIUM). Morning broker stall (see the BREAKINGTRADE review). RI's APOLLOHOSP entry
timed out at 10:50:17 and was adopted at 10:51:09. After the 11:06 restart its leg was restored
with "sl_order=UNKNOWN (multi-leg restore)" (log line 1061), re-protected at 11:06:40 (line 1089),
and the duplicate stop was cancelled at 11:08:44 (line 1107). The leg had a stop throughout.

EXECUTION-2 (LOW). RI shares the broker position with BREAKINGTRADE.
- All 9 RI trades were on symbols BREAKINGTRADE held at the same time. The sandbox nets the two
  into one position, so the account held up to twice the intended size in those names.
- BREAKINGTRADE's 14:45 closeposition closed RI's ANGELONE leg too (one SELL 264 @293.05, no
  strategy tag). Harmless today, because both versions exit at 14:45.
- For live trading, running both versions doubles the exposure on shared names.

4. Logic review

BUG-1 (HIGH, shared with BREAKINGTRADE; FIXED 2026-10-28 17:17). Restore uses the broker's netted average as the fill.
APOLLOHOSP: the RI leg booked +36.5 against +39.0 in the fills; the BREAKINGTRADE leg absorbed
the opposite 2.5. See the BREAKINGTRADE review for the evidence and the candidate fix
(signal_engine/startup.py:523).

DESIGN-1 (not a bug; the code does what the spec says). RI's reward-to-risk is below 1 by
construction.
- RI enters only on a close above the IB high, but the stop stays at the IB low minus 0.25 ATR
  (trigger.stop_level, unchanged from BREAKINGTRADE). TP1 is entry plus one IB range. So the
  risk is at least one IB range plus the buffer, and the target is exactly one IB range.
- Evidence: all 12 RI signals today had R:R between 0.47 and 0.97. Three were declined by the
  0.75 minimum. BREAKINGTRADE's entries inside the IB had R:R of 1.04 to 2.46.
- Effect: smaller positions (risk per share is larger) and a target that pays less than the stop
  costs. RI cannot win on a modest follow-through day even when its entry rule is right.
- Not a defect to fix in BREAKINGTRADE-RI. The STRATEGY-LOG entry says the two versions differ
  only in the entry, on purpose. A different stop belongs in a new version (TUNING-IDEA-1).

Spec checks that came out clean
- Entries only after 10:15 and only on a close beyond both the signal bar and the IB high
  (alerts rows: range_integrity 1, entry_vs_ib 0.01 to 1.05).
- Stop, targets, TP1 50% partial, runner stop, no-progress gate and 14:45 exit match
  BREAKINGTRADE's settings (config strategy_profiles BREAKINGTRADE-RI).
- Day summary dedupe (tracker._dedupe_breakingtrade_overlap) is a reporting rule only; trades.db
  keeps both legs (verified: both rows present for all 9 shared symbols).
- No-progress gate: 7 RI legs. Held to 14:45 instead, the same 7 would have made -0.30R against
  +0.02R booked. The gate helped.

Not checked
- Short-side RI entries (only DATAPATTNS signalled and it was declined).
- An RI signal on a symbol that BREAKINGTRADE did not trade (did not occur today).

Parity and evidence
- Counts as session 1 for the shadow.py comparison. Not clean: the 11:06 restart mispriced the
  APOLLOHOSP leg (2.5 rupees). The fill-backed figures above are correct.

5. Tuning ideas (each one as a new, separately tagged paper strategy)

Sample: 1 session, 9 trades. Far below the 20-session floor. Nothing here changes BREAKINGTRADE
or BREAKINGTRADE-RI, and neither of them is modified. Each idea needs a replay on the 35-day
history first (experiments/2026-10-amt-replay/amt_replay.py). The 2026-08-18 to 09-23 holdout is
spent, so a forward paper run is the only out-of-sample test left.

TUNING-IDEA-1 (candidate new version BREAKINGTRADE-RI-MS; overfit risk moderate). RI entry with a
stop that matches the RI thesis.
- RI's thesis is "the range has extended". A return into the middle of the IB disproves it, long
  before the IB low is reached. Candidate stop: the IB midpoint (or the IB high) minus 0.25 ATR.
- This would raise R:R above 1 and restore position size. It would also be stopped out more often
  by retests of the IB high. Only the replay can say which effect is bigger.
- The memory note of 2026-10-28 says exit tuning has not helped this strategy and that selection
  is the real limit. This idea changes the stop and sizing, not the exits, but the same caution
  applies: it can make RI less bad without making it profitable. Owner decision.
- RESULT (2026-10-28 17:40, STRATEGY-LOG): replayed as E17M (midpoint) and E17H (IB edge).
  Neither holds in both periods (net4 R per trade: E17M 0.000 / -0.118, E17H +0.089 / -0.345,
  against RI's +0.013 / -0.026). Rejected; no new paper version was started.

TUNING-IDEA-2 (track only, no new version yet). Results by scan and by range-integrity flag.
- Today Gap-Down Rescue gave most of the profit, and entries inside the IB beat entries above it.
  That is what a V-shaped recovery day does. The 35-day replay showed the opposite.
- Keep the shadow.py splits running. Revisit after 20 sessions that include at least a few
  trend-down and range days.

TUNING-IDEA-3 (operational, before any further variant). Make the time exit per strategy.
- A variant with its own exit time cannot run safely while one strategy's time exit closes the
  whole account (BREAKINGTRADE review, EXECUTION-3). This is a prerequisite, not a strategy change.

Summary in plain words
BREAKINGTRADE-RI made about Rs 950 on paper today (about Rs 800 after fees) from 9 trades, while
the original BREAKINGTRADE made about Rs 6,650 on the same stock picks. RI waits until a stock
has broken above its first-hour high before buying. Today the market fell first and then rallied,
so that wait meant buying later, at higher prices, with fewer shares, and missing five stocks
that never broke out but still rose. RI also keeps its stop loss far below, at the bottom of the
first hour's range, so every RI trade today risked more than its first target could pay. One day
proves nothing either way: the past-data replay favoured RI. The fair comparison is several
weeks of both, including down days. If we want to test a closer stop for RI, it should run as a
third, separate paper strategy so that today's two records stay untouched.
