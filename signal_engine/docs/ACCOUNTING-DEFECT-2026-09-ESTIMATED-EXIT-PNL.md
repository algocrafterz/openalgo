# Accounting defect: estimated exit P&L, 21 Sep to 1 Oct 2026

**Status.** Fixed going forward from 5 Oct 2026. **Open for the historical rows.** The old
rows are not corrected and are not backfilled. A backfill needs its own decision, taken
separately from this record.

This is a separate defect. It is not part of B1 (exit order id), B2 (partial-leg double
count) or B3 (bad-fill ledger rows). It breaks invariant I1 in
`ACCOUNTING-INVARIANTS.md` for the affected rows: the P&L on a close was not derived from
that close's own fills.

## What was wrong

A signal-driven exit booked its P&L from the broker's portfolio-wide realized delta,
`fetch_realised_pnl()` minus `_last_realised_pnl`, and not from the exit order's own fill.
That delta covers the whole account. When two closes landed close together, the first one
booked absorbed the entire delta and the second got almost nothing. A close could also be
credited with another strategy's P&L.

An earlier fix on 2026-09-11 corrected tracker-detected closes. The signal-driven exit path
kept the delta until commit `63c710e23` (2026-10-01 15:35 IST, after that day's session)
priced exits from the order's own fill.

## Evidence

Method: for every EXIT row, group it with its entry, compute the P&L its own two fills imply
(`(exit fill - entry fill) x quantity`, sign by direction), and compare it with the recorded
P&L. Closing rows are judged on their own leg only, using the same grouping
`db.fetch_day_trades` uses. Reconciled rows and bad-fill rows are excluded. The run read a
copy of `trades.db`.

| Day | Rows | Signed error | Absolute error | Rows off by more than 25 |
| --- | ---: | ---: | ---: | ---: |
| 2026-09-21 | 5 | +1,136.9 | 1,136.9 | 2 |
| 2026-09-22 | 5 | +158.4 | 158.4 | 1 |
| 2026-09-23 | 9 | -2.8 | 212.1 | 2 |
| 2026-09-24 | 34 | -178.2 | 2,515.5 | 14 |
| 2026-09-25 | 19 | +248.3 | 248.3 | 1 |
| 2026-09-28 | 4 | 0.0 | 0.0 | 0 |
| 2026-09-29 | 14 | -42.7 | 163.1 | 2 |
| 2026-09-30 | 28 | -330.4 | 335.9 | 4 |
| 2026-10-01 | 59 | -2,957.6 | 6,063.3 | 19 |
| **21 Sep to 1 Oct** | **177** | **-1,968.1** | **10,833.5** | **45** |
| 2026-10-05 | 27 | 0.0 | 0.0 | 0 |
| 2026-10-06 | 17 | 0.0 | 0.0 | 0 |

Signed error is recorded P&L minus fill-implied P&L. The 5 and 6 Oct rows, 44 in all, show
exactly zero error.

Worst rows, recorded against fill-implied:
- BREAKOUT-PY ADANIPORTS, row 829, 1 Oct: +0.00 against +2,902.3. The close was booked after
  another close had consumed the delta.
- BREAKOUT-PY RELIANCE, row 859, 1 Oct: +1,871.0 against +1,335.3. This is the incident
  that led to the fix.
- BREAKOUT BHARTIARTL, row 421, 21 Sep: +1,382.4 against +522.9.
- BREAKOUT ICICIBANK leg, row 459, 24 Sep: +724.4 against +128.6.
- BREAKOUT RELIANCE, row 514, 24 Sep: +704.7 against +1,654.7. **Ambiguous.** ORB held the
  same symbol that day, and the sandbox nets positions, so the entry fill may not be this
  position's cost.

## What is not known

- **Whether the historical tradebook is authoritative.** `data/tradebook/tradebook_<date>.json`
  exists for each day. It has not been checked for completeness, for order-id match with the
  ledger, or for the netting ambiguity. The fill-implied figures above come from the fill
  prices stored on the ledger rows, not from the tradebook.
- **Which rows are ambiguous.** Days where two strategies held one symbol, such as 24 Sep
  RELIANCE, cannot be settled from fills alone.
- **Whether a correction would change any decision already taken** on the 21 Sep to 1 Oct
  paper results.

## Decision pending

Do not backfill yet. A wrong backfill is worse than a record that says the rows are
estimates. Decide separately, in this order:
1. Is the historical tradebook complete and matched to ledger order ids for each day?
2. Can the netted days be resolved, or must they stay marked as ambiguous?
3. If yes to both, reconstruct only the rows that can be proven. Leave the rest marked.

Until then, use the fill-backed reports (the strategy pages and the pages-versus-fills check
in the EOD report) for any analysis of 21 Sep to 1 Oct. Do not use the ledger P&L of those
days as strategy evidence.

## Guard going forward

Exit P&L comes from the order's own fill (`main._book_exit_result`,
`main._bad_fill_close_pnl`, `PositionTracker._book_one_time_exit`). The portfolio delta is a
logged fallback only when the fill cannot be read.
