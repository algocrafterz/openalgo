Strategy performance analysis, mode LIVE, cut-off n/a (IST)

Example of the output when no live data exists. Produced by running the strategy-performance-analyst skill
(--mode live) on real data on 2026-11-15. Read-only. Nothing was simulated.

1. Overall assessment

No live strategy data exists. There is nothing to assess. No result is reported and none is invented.

2. Scope and sources

- Script output: `no live data: 0 closed trades and 0 signal rows with mode 'live' since 2026-09-25.`
- Checked (read-only): db/openalgo.db strategy_closed_trades (mode live: 0 rows), strategy_positions (0), strategy_order_tags (0);
  signal_engine/data/trades.db trade_mode live (0 rows); live engine logs 2026-10-22 to 2026-11-15 (17 empty;
  2026-10-31 and 2026-11-02 hold start-up lines only, with EVWAP-EARLY-PY refused as paper-only).
- Reliability start date for live: unknown (the ledger floor 2026-09-25 is a code constant and has not been tested on live rows).

3. Comparison table

n/a - insufficient data

4. Per-strategy findings

n/a - insufficient data. Which strategies the owner plans to run live is not recorded in the data.

5. Mode section: live execution quality and operational risk

- Actual fills, realised P&L, actual charges, slippage, rejected orders: n/a - no live rows.
- Actual charges: no source exists in the databases even for live. Charges would be estimates.
- HYPOTHESIS only: the first live session will need a check of the order-tag mapping, because pages_vs_fills --mode live depends on strategy_order_tags with mode live.

6. Recommendations

- Owner decision: none can be made from data. Recommendations for live configs are never made by this skill.
- When live trading starts, run this skill after the first full session. Expect LOW CONFIDENCE for a long time (under 20 own live sessions: no tuning idea is allowed).

7. Limitations

- No live data. Paper results do not transfer to live (fills, queueing, rejections and real charges differ).
- Live reliability start date unknown. Broker rejections are not stored in a mode-split table.
- Not computed: MAE/MFE, portfolio correlation, market regime.

Bottom Line
- No effect on trading. This was a read-only check.
- There is no live trading history yet, so there is nothing to judge.
- Decision for you: none now. Tell me when live trading starts and I will run it after the first full session.
- Next step: validate the first live day against the broker records.
- Risk: harmless.
