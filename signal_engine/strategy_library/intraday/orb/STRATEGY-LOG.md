# BREAKOUT / BREAKOUT-PY - Strategy Log

Key-level breakout. Python runner (BREAKOUT-PY) is canonical, the Pine BREAKOUT is the oracle.
Rules: `BREAKOUT-SPEC.md`, `strategy_core/breakout.py`. Paper only.

### 2026-10-25 13:38 — Exit safety: no selling into a fired stop, safer timeouts and time exit
- **What changed:** Before it sells, the engine now checks with the broker whether the protective
  stop has already sold some or all of the shares, and sells only what is still held. When a
  network timeout hides an order's confirmation, the engine now matches it only to that exact
  order, never to an earlier order of the same size. At the 14:45 time exit it first proves from
  the broker's order list that the stops are cancelled, and only then closes.
- **Entry:** Not affected.
- **Exit (SL):** A stop is never left working beside an exit. If the time exit cannot prove a
  stop is cancelled, it does not close that position: the stop stays in place and an urgent alert
  asks for a manual close before the broker's own 15:00 square-off. If a "no-progress" exit (the
  early close of a trade that is going nowhere) is rejected, the stop is put back at once.
- **Exit (TP):** A target exit that arrives after the stop already fired sends nothing, or only
  the shares still held. An exit whose confirmation timed out is never sent a second time.
- **Consideration:** A skipped time exit can leave a position open after 14:45 until it is closed
  by hand or by the broker (broker charges apply). Watch for "TIME EXIT SKIPPED" alerts. Profit
  and loss is no longer distorted by a failed balance read.

NOTE: `main._cancel_sl_before_exit` reads the stop's status when its cancel fails and flags a fill;
the exit then uses the broker's own quantity (flat: book the stop, partly filled: exit the rest).
`order_reconcile.parse_broker_stamp` reads sandbox and Flattrade (day-first) stamps, and every
reconcile passes the strategy and today's booked order ids (`main._reconcile_scope`).
`tracker._close_after_confirmed_cancel` gates each time-exit close on `_clear_working_orders`; a
skipped strategy is neither swept nor booked and stays tracked. The no-progress market exit sends
through `main._send_exit_with_retries` and restores the stop on a definite failure. A failed
`fetch_realised_pnl` returns None and keeps the realised baseline. Built on the stop-lifecycle and
exit-watch branches; their slot reservation enforces the open-position cap.

### 2026-10-25 13:38 — Python runner keeps managing open trades through pauses and errors
- **What changed:** A pause on new entries (an operator hold, a Telegram outage) no longer stops
  the Python runner from managing trades it already holds. A bar whose data arrives late still
  updates open trades. One unexpected error now skips a single 5-minute bar instead of stopping
  the runner for the rest of the day.
- **Entry:** Not affected. No new trade opens during a pause or on late data.
- **Exit (SL):** Open trades keep being managed during an entry pause and on late bars. After a
  switch between paper and live mode the runner still stops completely; the broker stop and the
  time exit protect the trade.
- **Exit (TP):** A target touched during an entry pause or on a late bar is now acted on instead
  of missed.
- **Consideration:** The first runner error of the day sends a Telegram alert
  (`runner_cycle_failed`); later errors that day are only logged.

NOTE: `pipeline/cycle.py` steps open positions when `halted()` is true unless
`mode_guard.mode_halted()` (a mode flip); a stale cycle steps held symbols only.
`pipeline/runner.py` isolates warm-up and per-cycle exceptions and alerts once per session.
See `docs/PYTHON-RUNNER-SPEC.md` "Halt".

### 2026-10-14 13:03 — Stops exactly 0.30% away no longer rejected by rounding

- **What changed:** The pre-trade check that rejects stops placed too close to the entry price used
  a strict "less than 0.30%" test. The strategy places its stop at exactly 0.30%, and computer
  arithmetic stores that distance as 0.2999999999999996% for many prices, so the check wrongly
  rejected about 3 in 10 valid signals ("0.3000% below minimum 0.3000%"). The check now allows a
  shortfall of one billionth of the price, which is smaller than any real price step.
- **Entry:** Yes. Signals whose stop sits exactly at the 0.30% limit now open a position. Before
  the fix they were silently dropped. A stop that is truly tighter than 0.30% is still rejected
  (0.299% is rejected, 0.30% is accepted).
- **Exit (SL):** Not affected. The stop is still placed at 0.30% of the entry price.
- **Exit (TP):** Not affected.
- **Consideration:** This makes the live check match the frozen v1.0 rule (a minimum stop of
  0.30%). The 0.30% limit went live on 2026-10-14 (it was 0.20% before), so only that day lost
  signals to the rounding: M&M, SHRIRAMFIN and SUNPHARMA were dropped. About 3 in 10 recorded
  BREAKOUT-PY signals (48 of 162 since 2026-10-01) sit exactly on the limit, so expect the same
  share to be affected on any day the limit is enforced. Do not compare signal counts across
  2026-10-14. Nothing was tuned from the result.

NOTE: `validator._check_sl_distance` now tests `sl_pct < floor - _SL_FLOOR_EPS` with
`_SL_FLOOR_EPS = 1e-9`. The floor is 0.003 (`strategy_profiles.BREAKOUT-PY.min_sl_pct`). The
tolerance is 0.0001% of the entry price, 5e-5 rupees at Rs 50,000. The earlier config comment that
the 0.20% to 0.30% floor change "changes no signal" was wrong for the same reason and is corrected.
It applies to every strategy that uses a stop-distance floor, but only BREAKOUT-PY places stops
exactly on the floor. Tests: `tests/test_validator_sl_boundary.py`.
