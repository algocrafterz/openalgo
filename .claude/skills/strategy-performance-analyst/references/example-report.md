Strategy performance analysis, mode PAPER, cut-off 2026-11-13 (IST)

Example report produced by running the strategy-performance-analyst skill (--mode paper) on real data. Read-only run.
Run date: 2026-11-15. Session counts re-run with the strategy-specific rule (own trading days in the mode). No code, config, service or order was touched.

1. Overall assessment

Five strategies are active in paper mode (analyze): EVWAP-EARLY-PY, BREAKINGTRADE, BREAKINGTRADE-RI, ORB, ORB-PY.
Buckets: Improving 1 (EVWAP-EARLY-PY), Deteriorating 1 (BREAKINGTRADE), Needs investigation 3
(BREAKINGTRADE-RI, ORB, ORB-PY; each has a window under 20 trades). Consistent performer: none.
Net after estimated charges, recent window (5 sessions with data, 2026-11-04 to 2026-11-13), all five together:
about -12,000 INR (BREAKINGTRADE -7,383; ORB-PY -3,293; ORB -1,619; EVWAP-EARLY-PY +44; BREAKINGTRADE-RI +116).
Full history since 2026-09-25, net after charges: EVWAP-EARLY-PY -100,751; BREAKINGTRADE +1,849;
BREAKINGTRADE-RI +1,488; ORB +3,068; ORB-PY -3,293. Only the previous window carried the gains.

2. Scope and sources

- Data cut-off: 2026-11-13 (last session with rows). Windows count sessions that have data.
  recent = 2026-11-04, 11-05, 11-11, 11-12, 11-13. previous = 2026-10-28, 10-29, 10-30, 11-02, 11-03.
  all = 2026-09-25 to 2026-11-13 (30 sessions with data).
- Missing sessions (weekday, not a holiday, no rows, and the paper engine log is missing or empty): 2026-10-07 (no log), 2026-11-06 (empty log), 2026-11-09 (no log).
  Cause not investigated. Legitimate quiet days (engine log non-empty, no rows): none. Weekends and NSE holidays are not listed.
  2026-11-10 is a trading holiday (Diwali Balipratipada). The recent window therefore spans calendar days
  2026-11-04 to 2026-11-13 but holds only 5 sessions with data, and EVWAP-EARLY-PY traded on 4 of them.
- Sources: db/openalgo.db table strategy_closed_trades (mode analyze, trade_date from 2026-09-25) through
  services.strategy_metrics_service.compute_metrics and services.strategy_trade_enrichment (enrich_trades,
  summarize_enrichment, symbol_breakdown, entry_hour_breakdown); signal_engine/data/trades.db table trades
  (trade_mode analyze) for declines, timeouts and exit types; pages_vs_fills for 2026-11-13;
  execution_health for 2026-11-13; breakingtrade.shadow --since 2026-10-28; docs/strategy-daily-performance.md.
- Sample sizes (trades recent / previous / all): EVWAP-EARLY-PY 150 / 205 / 856; BREAKINGTRADE 30 / 32 / 142;
  BREAKINGTRADE-RI 15 / 18 / 33; ORB 11 / 7 / 95; ORB-PY 11 / 0 / 11.
- Own paper trading sessions (distinct days with at least one closed paper trade for the strategy, to the cut-off): EVWAP-EARLY-PY 20, BREAKINGTRADE 28, BREAKINGTRADE-RI 9, ORB 25, ORB-PY 4.
  The global count is 30. The old method (global sessions after the first trade) gave 22, 30, 10, 30, 5 and is not used.
- Data reliability start: strategy_closed_trades 2026-09-25 (LEDGER_RELIABLE_SINCE, paper); trades.db paper rows used from 2026-09-25.
- Inactive (in config, no rows in the last 10 sessions): BREAKOUT (last 2026-10-14), BREAKOUT-PY (last 2026-10-27),
  BREAKINGTRADE-WATCHLIST (last 2026-10-27). Not analysed.

3. Comparison table (recent / previous / all)

| Strategy | Bucket | Trades | Win rate % | Profit factor | Net exp. R | Net after charges INR | Slip bps | Confidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EVWAP-EARLY-PY | Improving | 150 / 205 / 856 | 52.0 / 44.9 / 45.8 | 1.25 / 0.67 / 0.75 | +0.06 / -0.10 / -0.06 | +44 / -27,686 / -100,751 | 1.4 / 1.2 / 1.4 | Medium (recent has 4 traded days) |
| BREAKINGTRADE | Deteriorating | 30 / 32 / 142 | 40.0 / 81.3 / 59.9 | 0.27 / 5.14 / 1.23 | -0.28 / +0.39 / +0.08 | -7,383 / +6,943 / +1,849 | 1.8 / -2.0 / 0.6 | Medium |
| BREAKINGTRADE-RI | Needs investigation | 15 / 18 / 33 | 60.0 / 72.2 / 66.7 | 1.20 / 3.03 / 1.77 | +0.01 / +0.12 / +0.07 | +116 / +1,373 / +1,488 | -4.1 / -1.1 / -2.6 | LOW CONFIDENCE |
| ORB | Needs investigation | 11 / 7 / 95 | 45.5 / 71.4 / 53.7 | 0.42 / 2.29 / 1.47 | -0.09 / +0.24 / +0.15 | -1,619 / +614 / +3,068 | -2.9 / 1.7 / 0.3 | LOW CONFIDENCE |
| ORB-PY | Needs investigation | 11 / 0 / 11 | 18.2 / n.a. / 18.2 | 0.05 / n.a. / 0.05 | -0.37 / n.a. / -0.37 | -3,293 / n.a. / -3,293 | -11.3 / n.a. / -11.3 | LOW CONFIDENCE (4 sessions) |

Slippage: negative means the fill was better than the alert price. Net exp. R = average R after estimated charges.
R coverage (recent): EVWAP-EARLY-PY 125 of 150, BREAKINGTRADE 30 of 30, BREAKINGTRADE-RI 15 of 15, ORB 11 of 11, ORB-PY 10 of 11.

4. Per-strategy findings

EVWAP-EARLY-PY (FROZEN for observation: bug fixes only) - IMPROVING, medium confidence
- Working. EVIDENCE (psa script, strategy_closed_trades): win rate 44.9 to 52.0 percent, profit factor 0.67 to 1.25,
  net expectancy -0.10 R to +0.06 R. All three move the improving way past the thresholds. Entry slippage is steady (about 1.3 bps).
- Not working. EVIDENCE: full history net after charges is -100,751 INR over 20 own trading sessions (gross -58,530, charges 42,221).
  Estimated charges were 6,251 INR in the recent window against gross 6,295 INR, so charges took almost all of the gross.
  Drawdown over the history is -74,552 INR.
- What changed. EVIDENCE: daily P&L in the recent window was -5,269 (11-04), -4,458 (11-05), +9,673 (11-12), +6,349 (11-13).
  Trades per session fell from 41 to 30. The previous window had gross -18,211. Removing the best and worst day leaves the sign positive.
- Exit mix, recent. EVIDENCE: TRAILING_STOP 122, SL 12, RECONCILED 6, INITIAL_STOP_SAFETY 4, TIME 4. Trailing exits are most trades.
- Time of day (entry, IST), all history. EVIDENCE: 09:00 hour 268 trades -27,887; 10:00 hour 158 trades -21,068; 11:00 hour 141 trades +10,614;
  12:00 hour -16,949. Recent window: 11:00 hour +4,609 at 76.9 percent win; 13:00 hour 23.5 percent win.
- Execution. EVIDENCE: 7 TIMEOUT rows in the recent window (31 in all). Declines: 5 "symbol already held", 2 APOLLOTYRE on the broker MIS reject list.
  execution_health for 2026-11-13 reports REGRESSION on limiter max wait (17.8 s), data calls per minute (130.6), slow place_order (8 above 5 s),
  websocket drops (11, longest outage 3,582 s), stale-price warnings (51), late-skipped cycles (49 symbols) and entry signal-to-ack p95 (12.4 s).
  pages_vs_fills 2026-11-13: PASS (EVWAP-EARLY-PY fills 6,349.3, page 6,349.2).
- HYPOTHESIS. The two good sessions (11-12, 11-13) reflect market conditions that suit trailing entries, not a change in the strategy.
  The code did not change in the window. No regime tag exists, so this is not tested.
- HYPOTHESIS. Execution delays on 11-13 may have cost fills. Slippage did not rise, so the effect is not visible in the fills.
- Recommendation. Keep unchanged. Frozen: no parameter change. Investigate the execution REGRESSIONs (operational). The full history is still negative after charges.

BREAKINGTRADE (no tuning until the re-evaluation; owner decision 2026-10-28) - DETERIORATING, medium confidence
- Not working. EVIDENCE: win rate 81.3 to 40.0 percent, profit factor 5.14 to 0.27, net expectancy +0.39 R to -0.28 R. Average win is +0.24 R and average loss -0.58 R in the recent window.
- What changed. EVIDENCE: daily gross in the recent window was -739, -1,358, +941, +214, -5,831. The 2026-11-13 session (10 trades) carries -5,831, which is 86 percent of the window loss.
  Entry hour 10:00: 12 trades, -5,597, 25 percent win. The previous window had one day at +6,654 (2026-10-28, 18 trades), which flatters it.
  Removing the best and worst days leaves the window still negative (-1,883), so it is not only one day.
- Exit mix, recent. EVIDENCE: NO-PROGRESS 25 of 30, SL 2, TIME 2, RECONCILED 1. No TP1 hit in the window. In all history TP1 is 16 of 142.
- Selection check. EVIDENCE (breakingtrade.shadow, since 2026-10-28, with 4 bps per side slippage, so a different basis): BREAKINGTRADE 58 trades, net -2,990, profit factor 0.77, -0.058 R per trade;
  range-integrity fail 37 trades -4,170, pass 21 trades +1,181.
- HYPOTHESIS. A weak, falling market on 2026-11-13 hurt long breakout picks. No regime tag is stored, so this is not proven.
- Recommendation. Keep unchanged. Per the owner decision, re-evaluate after about 20 sessions from 2026-10-28 with shadow.py (10 sessions so far).
  Do not tune on the 2026-11-13 loss or on the entry hour (overfit risk high: 30 trades, 5 sessions).

BREAKINGTRADE-RI (same owner decision) - NEEDS INVESTIGATION (LOW CONFIDENCE: 15 and 18 trades, 9 own sessions)
- Working. EVIDENCE: still positive after charges in all windows (+116 recent, +1,373 previous, +1,488 all). Drawdown is small (-890 INR).
- Not working. EVIDENCE: all measures fell from the previous window (win 72.2 to 60.0, profit factor 3.03 to 1.20). Direction only, not a trend.
- What changed. EVIDENCE: the worst day is 2026-11-13 (-890, 2 trades). 20 R:R declines in the recent window (R:R below 0.75 minimum).
- Shadow. EVIDENCE: 32 trades since 2026-10-28, net +878, profit factor 1.28, +0.031 R per trade. This is positive but under the owner's 20-session bar.
- Recommendation. Keep unchanged. Revisit at the planned re-evaluation. Fewer than 20 sessions: no tuning idea is allowed.

ORB - NEEDS INVESTIGATION (LOW CONFIDENCE: 11 and 7 trades)
- Working. EVIDENCE: all history net +3,068 after charges over 95 trades (25 own sessions), profit factor 1.47, +0.15 R. The best symbols are RELIANCE (11 trades, +2,790) and HDFCBANK.
- Not working. EVIDENCE: recent window profit factor 0.42, win rate 45.5 percent. TCS has 7 trades and 0 wins in all history (-2,316).
- What changed. EVIDENCE: 5 declines in the recent window with reason "the Python strategy is the sole execution source on this symbol" (superseded by ORB-PY). Trade count is 11 vs 7. Direction only.
- HYPOTHESIS. ORB and ORB-PY may overlap on the same symbols and days (TCS appears in both). Not tested here.
- Recommendation. Investigate the ORB / ORB-PY overlap (operational). No freeze record found for ORB or ORB-PY: ask the owner. Do not tune.

ORB-PY - NEEDS INVESTIGATION (LOW CONFIDENCE: 11 trades, 4 own sessions)
- Not working. EVIDENCE: 2 wins in 11 trades. Profit factor 0.05. Net -3,293 after charges. Net expectancy -0.37 R. Average win +0.17 R, average loss -0.45 R. 4 losing sessions out of 4 traded.
  Entry hour 10:00: 10 of 11 trades, 20 percent win. Exit mix: NO-PROGRESS 11 of 11, no TP and no SL exits.
- Execution. EVIDENCE: slippage -11.3 bps (fills better than the alert price). 0 declines and 0 timeouts. pages_vs_fills 2026-11-13: PASS.
- HYPOTHESIS. All trades exit by NO-PROGRESS before TP, which may be a design property or a bug in the exit driver. A spec check is needed. The sample (11 trades) cannot show which.
- Recommendation. Investigate (spec conformance of the NO-PROGRESS exit versus the ORB spec). Fewer than 20 sessions: no tuning. Ask the owner whether ORB-PY has a freeze record.

5a. Mode section: paper performance and differences from expected live execution

- Paper performance above uses sandbox fills and estimated charges (Performance page schedule). No actual-charge source exists.
- EVIDENCE (sandbox fills): slippage here is the gap between alert price and the sandbox fill, 1.4 bps for EVWAP-EARLY-PY. HYPOTHESIS: live slippage, queueing and partial fills would be larger.
- Paper has no broker rejections of the kind live has. The 2 APOLLOTYRE declines come from the broker MIS reject list in the engine, so they would also apply live.
- No live data exists to test these differences (see example-report-live-unavailable.md).

5. Significant changes

- BREAKINGTRADE: profit factor 5.14 to 0.27. One session (2026-11-13, -5,831) is 86 percent of the recent loss.
- EVWAP-EARLY-PY: from -27,686 net in the previous window to +44, with two strong sessions (11-12, 11-13).
- ORB-PY: first paper window, 2 wins in 11 trades.
- Data gaps on 2026-11-06 and 2026-11-09 (no rows; empty or missing engine log).

6. Likely explanations (all HYPOTHESIS unless stated)

- Market conditions (no regime data), exits dominated by NO-PROGRESS in the BREAKINGTRADE family and ORB-PY, and execution stress on 2026-11-13.
- The previous window's strength came from single days (BREAKINGTRADE 2026-10-28 +6,654), not from a steady edge.

7. Recommendations (ranked by impact then confidence)

1. EVWAP-EARLY-PY: investigate execution REGRESSIONs on 2026-11-13 (operational, high impact: this strategy has 150 of 217 recent trades). Frozen: no parameter change. Confidence: medium.
2. ORB-PY: investigate the all-NO-PROGRESS exit pattern against the spec (possible bug). Confidence: low (11 trades). Overfit risk: not applicable, this is a spec check.
3. ORB and ORB-PY: investigate the overlap on shared symbols. Ask the owner whether either is frozen.
4. BREAKINGTRADE and BREAKINGTRADE-RI: keep unchanged. Re-evaluate with shadow.py after about 20 sessions (10 so far).
5. Investigate the data gaps on 2026-11-06 and 2026-11-09 (why no rows, why no log).

8. Limitations

- Paper fills from the sandbox. Live results will differ.
- Small samples: only EVWAP-EARLY-PY (20 own sessions), BREAKINGTRADE (28) and ORB (25) reach 20 own sessions. BREAKINGTRADE-RI (9) and ORB-PY (4) do not. Recent windows hold 5 sessions. Sharpe and Sortino not reported (need 60 sessions).
- Charges are estimates (Flattrade schedule). R needs a matched signal stop (EVWAP-EARLY-PY 125 of 150 recent trades).
- No market regime data. Time-of-day and symbol splits have small cells and are descriptive.
- Not computed: MAE/MFE, portfolio correlation. The paper ledger is reliable from 2026-09-25 only (source- and mode-specific date).
- The shadow report uses a different cost basis (4 bps per side extra slippage). Its numbers are not comparable to the table.
- The recent window skips 2026-11-06 and 2026-11-09 because they have no rows.

9. Plain-words summary

Five strategies are trading on paper. Over the last five trading days with data, together they lost about 12,000 rupees after costs.
Most of that came from BREAKINGTRADE, which lost heavily on one day (13 November) after a very good earlier week.
EVWAP-EARLY-PY, the busiest strategy, broke even over the last five days after two bad weeks, but it is still about 100,000 rupees down since it started.
ORB-PY has only 11 trades and won 2 and lost 8 (one was flat). That is too few to judge, but its exits look odd and are worth checking.
ORB and BREAKINGTRADE-RI have too few trades to call a trend. Nothing here justifies changing any setting.
On a 1,00,000 rupee account, the recent loss of about 12,000 rupees would be about 12 percent, but these are paper results with estimated costs.

Bottom Line
- No effect on live trading or the sandbox. This was a read-only review.
- Nothing is broken that needs a quick fix. The loss in the last five days is mostly one bad day in BREAKINGTRADE.
- Decisions for you: do ORB and ORB-PY have a freeze record, and do you want the ORB-PY exit pattern checked.
- Next steps: look at the missing data on 6 and 9 November, and at the broker-feed problems on 13 November.
- Risk: operational for the feed problems and data gaps; trading risk is paper-only.
