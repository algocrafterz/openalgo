# Accounting invariants

The ledger is `trades.db`. This file owns the rules the ledger and everything that reads it
must keep. They hold for every way a position can close: stop loss, take profit, signal
exit, time exit, bad-fill protection, and a manual or forced exit.

A report that agrees with another report is not proof. A ledger that keeps these rules is.

## The invariants

**I1. Lineage.** Every actual broker fill that changes position state has deterministic
lineage into the ledger. Every ledger close is attributable to the corresponding entry and
close order and fill.

**I2. At most once.** One actual close or fill produces at most one ledger close event.
Two code paths that both see the same close must not both book it.

**I3. Money and evidence are separate questions.**
- What happened to the money? Every realized rupee counts in net P&L, in the risk counters
  and in reconciliation.
- What does it say about the strategy? A close the clock (`TIME_EXIT`) or the execution
  guard (`BAD_FILL_CLOSE`) caused is a trade event, not a decided win or loss. Reports show
  two views. The economic view includes every realized event in net P&L, expectancy and
  drawdown, and it decides economic viability. The strategy view excludes these events from
  win rate and average R and lists them separately. No realized loss is ever left out of the
  economic view.

**I4. A realized loss is real, and an unrealized one is not booked.** A realized loss
reaches the ledger, net P&L and the daily risk limits. If the close order failed and the
broker still holds the position, no row is written and no risk counter moves.

**I5. A close row claims the close happened.** No closing row is written for a position the
broker still holds.

## How each one is kept today

| Invariant | Mechanism | Tests |
| --- | --- | --- |
| I1 | `sig_id` on every row. `order_id` is the closing order. Context carries `entry_order_id`, `exit_order_id` and `exit_fill` for time-exit and bad-fill closes. One writer, `PositionTracker._save_close_row`, for tracker closes and time exits. | `test_time_exit_ledger.py`, `test_bad_fill_ledger.py`, `test_close_accounting.py` |
| I2 | One owner per close. `pos.exit_pending` is set in the same synchronous step as its check by a signal exit (including the SL-hit reconcile), by the poll's close booking (before the fill verification) and by the no-progress action. Each stands down if it is already set, and each releases it unless it closed the position. A crash or a cancelled exit handler releases it. The poll removes a booked position at once. `_time_exit_active` is held for the whole time exit, the poll stands down while it is set, and the time exit skips a position another path booked or owns. Order ids are not reused across positions. `fetch_day_trades` counts a partial leg once, using `includes_leg_pnl` on the closing row. A failing close notification cannot unwind a booked close. | `test_close_single_booking.py`, `TestTimeExitAndThePollDoNotBookTheSameClose`, `test_day_summary_from_db.py` |
| I3 | `db.NOT_DECIDED_EXIT_TYPES`, one set used by the headline summary and the per-strategy lines. Every day-trade row carries `exit_class`. `db.pnl_by_exit_class` splits P&L by class. | `test_time_exit_ledger.py`, `test_notifier_strategy_comparison.py` |
| I4 | `main._auto_close_on_bad_fill` passes the realized P&L to `risk_engine.record_close`. The failed-close path does not. | `TestBadFillLossReachesTheDailyLossLimit`, `TestCloseOrderFailureNeverLeavesAnUntrackedPosition` |
| I5 | The time exit writes a row only for a position the broker confirmed closed. | `test_a_position_the_broker_still_holds_gets_no_closing_row` |

## Known gaps

These break or weaken an invariant and are not fixed.
- **A position rebuilt from the broker after a restart.** The exit handler rebuilds such a
  position, and it is never registered, so the tracker poll cannot see it. If its exit order
  fails and the broker is flat, the handler releases the risk slot with a close of 0.0 and books
  no P&L, because there is no stop fill to price it from. The loss is missing from the ledger
  and the daily-loss counter. Pricing it from the portfolio delta would repeat the defect in
  `ACCOUNTING-DEFECT-2026-09-ESTIMATED-EXIT-PNL.md`, so it is left unbooked and logged.
- **A failed exit on a flat position is labelled `SL`.** The flat case includes an exit that did
  fill but was reported as a failure. The poll books it with the cancelled stop's id. The money
  is right; the label and the order id are not.
- **A time-exit skip books late.** A position the time exit skips because another path owns it
  is booked by the first poll afterwards as `STRATEGY_EXIT`, after the one-shot day summary has
  gone out. The ledger total is correct; the summary omits it.
- **A signal exit can stand down during the poll's claim.** While the poll verifies an
  unconfirmed fill it holds the claim for one network call. A TP or EXIT signal arriving then is
  skipped as "exit already in progress" and is not retried. The window is one HTTP call on a
  position whose entry is not yet confirmed.
- **Historical rows, 21 Sep to 1 Oct.** Their exit P&L came from the portfolio-wide realized
  delta. See `ACCOUNTING-DEFECT-2026-09-ESTIMATED-EXIT-PNL.md`. Do not backfill without a
  separate decision.
- **Legacy rows without `includes_leg_pnl`.** `fetch_day_trades` infers which leg P&L a
  closing row contains from its exit labels. The inference is right on the real rows checked
  and can be wrong when a later alert reuses a label.
- **A time exit the broker did not confirm.** The tracker entry is still removed and an error
  is logged. The position stays open at the broker, untracked, until the broker's own
  square-off.
- **Netted sandbox positions.** The sandbox nets positions per symbol across strategies. When
  two strategies hold the same symbol, a fill cannot always be attributed to one of them.
  Order-id lineage is exact where order ids exist and ambiguous where they do not.
- **Time-exit P&L attribution.** When a fill replaces an estimated P&L for one position, the
  shared leftover of the other positions is not redistributed, and the day counter still
  uses the portfolio delta.
