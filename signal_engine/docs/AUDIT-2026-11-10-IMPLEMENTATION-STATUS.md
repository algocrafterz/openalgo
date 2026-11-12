# Audit 2026-11-10: implementation status

Companion to [`AUDIT-2026-11-10-ARCHITECTURE.md`](AUDIT-2026-11-10-ARCHITECTURE.md). Finding IDs
refer to that report. This file records what is implemented, what is planned, and the owner's
decisions. Update it when a finding changes state.

Status date: 2026-11-12 (updated 20:10 IST after the Phase 2 batch). Branch: `feature/optimize-signal-engine-v2.0.2.1`.

## 1. Implemented (Phase 1)

Commits, all on 2026-11-11:

| Commit | Subject |
| --- | --- |
| `2635eb7da` | fix(signal_engine): harden order, exit and tracker paths from the 2026-11-10 audit |
| `e5c474554` | fix(scheduler): verify the 14:55 failsafe cancel and square-off results before reporting done |
| `9a8bb1720` | fix(ops): stop signal_engine before app.py and wait for its shutdown drain on restart |
| `11f3535a3` | fix(ops): add NSE holiday calendar coverage to the readiness check |
| `c682712ea` | docs(signal_engine): add 2026-11-10 architecture audit and uncertain-order recovery design |

| Finding | Implemented behaviour | Main code |
| --- | --- | --- |
| E-01 | Each order pipeline runs as its own shielded task, so a Telegram disconnect cannot cancel it mid-order. Shutdown waits up to 20 s for running pipelines before the tracker stops. | `listener.py` (`track_dispatch`, `drain_inflight`), `pipeline/service.py` (`_shielded`), `startup.py` |
| E-02 | A position with no position-book row is UNKNOWN, not closed. A close is booked only on a matching row with quantity 0, or when the closing order's own status is `complete`. Rows are matched by symbol, exchange and product. Alert after 6 unproven polls, then every 10 minutes. | `tracker.py` (`_entry_for_position`, `_absent_row_proven_flat`, `_check_one_position`) |
| E-02 (orphan) | A position with no row is released as an orphan only on an explicit `rejected` or `cancelled` entry status. An unreadable status keeps the position and its stop. | `tracker.py` (`_verify_unconfirmed_fill`, `_resolve_stuck_order`) |
| E-03 | An accepted exit with no fill price has its status read. Rejected or cancelled: nothing booked, position stays, stop is re-placed. Complete: booked. Otherwise booked only on a proven position-book row; if not proven, held as an uncertain order with its own alert text. | `main.py` (`_exit_not_proven_filled`, `_hold_accepted_exit`), `tracker.py` (`_no_progress_market_exit`) |
| E-04 (part) | After a mid-session mode change the tracker poll stands down before any read. Positions rest on their broker stops until the engine is restarted. | `tracker.py` (`check_positions`, `_log_standdown`) |
| E-05 | A chart "SL HIT" alert needs proof: the tracked stop is `complete`, or a position-book row shows quantity 0. A working stop means not proven, and nothing is cancelled, booked or unregistered. | `main.py` (`_sl_hit_is_proven`, `_reconcile_sl_hit`) |
| E-07 | HTTP 5xx and post-send transport errors on an order are "outcome unknown" and go through the existing timeout reconciliation. 4xx stays a rejection. Same behaviour in sandbox mode and live mode (owner decision). | `executor.py` (`send_order`, `_interpret_placeorder`) |
| E-10 | The tracker poll loop survives an exception, alerts on the first failure and then at most every 5 minutes, and sends a recovery notice. One position's failure does not skip the others. The tracker and time-exit tasks alert if they end unexpectedly. | `tracker.py` (`start`, `check_positions`), `startup.py` (`_background_task_ended`) |
| E-10 (follow-up), R-04 | A close is counted once even when recording it fails. A `risk.db` write failure is logged and does not raise into the order path; in-memory counters stay in force. | `tracker.py` (three booking sites), `risk.py` (`_persist`) |
| E-11 | A position is registered before its fill notice is sent. A Telegram send is bounded at 10 s. | `main.py` (`_establish_position`), `notifier.py` (`_deliver`) |
| Zero-read proof | Four decision points use a position-book row read, not `openposition`, which answers 0 for a failed read. | `tracker.py` (`fetch_own_position_proven`); `main.py` (`_sl_hit_is_proven`, `_exit_not_proven_filled`, `_handle_exit_order_failure`, `_absent_exit_decision`) |
| Late partial exit | A partial exit confirmed late reduces the tracked quantity and clears the stop id so the remainder is re-protected. Its P&L is not booked there (see R-01). | `main.py` (`_apply_late_partial_exit`) |
| I-01 | Tests cannot reach the live `risk.db`, `runner_state.db`, strategy-card state, bug tracker, shadow store or log files. A guard test asserts it. | `tests/conftest.py`, `logger_setup.py`, `tests/test_operational_db_isolation.py`, `tests/test_log_isolation.py` |
| I-02 | The 14:55 failsafe collects every cancel and close result, verifies the position book, alerts and exits non-zero on failure. A failed close is retried only when the book still shows an open intraday position; never blind. | `scripts/openalgoscheduler.py` (`_run_squareoff`) |
| I-03 | Startup retries the mode read 5 times, then aborts with an alert. It never defaults to live. | `startup.py` (`_confirm_trading_mode`) |
| I-05 | The supervisor stops the engine first, waits up to 30 s for it, then stops the app. | `scripts/openalgoctl.sh` (`kill_from_pidfile`) |
| D-01 (part) | The 09:12 readiness check is RED when today or the next session is outside the calendar, AMBER from 1 December when next year is missing. | `ops/readiness.py` |

Verification on the final tree: full `signal_engine` suite 9,914 passed, 1 skipped;
`tests/test_openalgoctl.sh` 24 passed; three independent Opus reviews (Packages A and B; Package
C; the fix rounds), the last one "approve". All broker behaviour was verified against mocks only.

## 1b. Implemented (Phase 2 batch, 2026-11-12)

Engine restarted at 20:06 IST after these commits. All verified against mocks only; the full
`signal_engine` suite was green on the final code (10,058 passed, 1 skipped).

| Commit | Finding | Implemented behaviour | Main code |
| --- | --- | --- | --- |
| `94bbbcf5d` | R-01 | Each completed exit leg reaches the risk counters once at its own P&L (`record_partial_close`); the time exit books only its closing leg. A restart restores the legs already booked. | `risk.py`, `main.py` (`_book_exit_result`, `_apply_late_partial_exit`), `tracker.py` (`_book_one_time_exit`), `startup.py`, `db.py` |
| `94bbbcf5d` | E-18 | MIS entries are declined at or after the time-exit boundary or while the time exit runs (CNC and off-hours testing excluded). | `main.py` (`_entry_after_time_exit`) |
| `94bbbcf5d` | E-06 | The time exit waits up to 45 s for a signal exit holding the leg's exit lock, then re-checks the leg before selling. | `tracker.py` (`_close_strategy`, `_close_one_leg`) |
| `94bbbcf5d` | E-14 | A tracked stop shown rejected or cancelled by the broker is cleared and re-placed, with an alert. Legs the time exit is closing are skipped. | `tracker.py` (`_release_dead_stops`) |
| `94bbbcf5d` | E-20 | An entry confirmed `complete` records its fill price, so a break-even stop-out is not booked as an orphan. | `tracker.py` (`_verify_unconfirmed_fill`) |
| `eb851aa48` | R-02 | Unrealised loss is summed per risk bucket in live mode. | `tracker.py` (`_push_unrealised`), `risk.py` (`bucket_for`) |
| `43adcba75` (+ listener lines in `fa628a896`) | E-15, E-19, S-06 | A late EXIT alert is processed against the position if it was open when the alert was sent, else declined as `late_exit`. Entries re-check freshness immediately before the order (Telegram path and runner cycle). | `listener.py`, `main.py` (`_late_exit_refused`, `_entry_stale_before_send`), `pipeline/cycle.py` |
| `f8c530881`, `bbf84dfe8` | E-12 (entry time, runner stop, exit labels, entry context) | A restored position keeps its real entry time and gets back the runner stop, exit labels and entry context from trades.db. Partial exit rows record `sl_after`. | `startup.py`, `db.py` (`fetch_open_position_state`), `main.py` |
| `bbf84dfe8` | E-13, R-03 (startup reconciliation) | A partial exit row no longer hides an open runner. Risk books only what the legs did not cover. Two strategies on one flat symbol: nothing booked, operator alerted. | `db.py` (`fetch_all_open_positions`), `startup.py` |
| `911c3a347` | E-08 | A leg whose own stop filled on a symbol shared with another leg is booked from that stop's fill (checked every 20 s). | `tracker.py` (`_book_shared_symbol_stop_fill`) |
| `39763de6b` | S-01, S-02, S-03 (log only) | The runner skips non-session days from the NSE calendar (weekday rule if the calendar is unreadable). Overrun skips are logged and alerted. A running Ladder replays closed bars it missed; never after a restart. | `pipeline/service.py`, `pipeline/runner.py`, `pipeline/exit_driver.py`, `pipeline/cycle.py` |

Review: an independent Opus review of the first commit found two medium issues (partial legs
double-counted after a restart; dead-stop release beside a pending time-exit close). Both were
fixed before commit.

## 2. Phase 1 items not finished

| Item | State | Next action |
| --- | --- | --- |
| D-01 calendar data | 2027 NSE holidays not added: the circular was not published on 2026-11-11 (still true on 2026-11-12). | Add 2027 to `research/reference/nse_calendar.yaml` from the official circular before the first 2027 session. |
| I-01 origin | The `PORTFOLIO/live` weekend rows in `data/risk.db` (2026-10-24, 2026-10-25) are not explained. No test at HEAD writes them (1,555 tests run against a copy with the old fixture). Rows untouched. | Owner decides whether to investigate further or leave. Evidence copy was kept in the session scratch directory. |
| E-04 remainder | The 14:45 time exit and signal exits still send orders through whatever mode OpenAlgo is in. | Needs an OpenAlgo core option to pin the pipe from REST. Owner decision. |

## 3. Known residual risks from Phase 1 (review findings not fixed)

1. Three older sites in `main.py` still trust a bare `openposition` zero: stop executed during
   cancel (about line 364), missed-stop close (about 708), bad-fill close after a timeout (about
   2851). A failed Flattrade position read looks like flat there. Durable fix: make the Flattrade
   mapping return a failed PositionBook reply as an error, not an empty success
   (`broker/flattrade/mapping/order_data.py:330-335`). Core change; needs owner approval.
2. `_apply_late_partial_exit` clears the stop id without checking that the old full-size stop is
   no longer working. The normal partial-exit path has the same pattern.
3. A position with no book row whose entry status never becomes final waits all day with a live
   stop and repeated alerts. The alert does not say that the resting stop could open a position.
4. Sandbox mode: a position closed by hand at exactly zero P&L has no row, so it stays tracked
   with alerts and a live stop until the 14:45 time exit.
5. Three proven-read sites do not check for another strategy's exit in flight on a shared symbol.
6. The stop status spelling `canceled` is treated as a working stop (errs on the safe side).
7. `executor.py`: the not-sent message says "could not reach OpenAlgo"; "the order could not be
   sent" is more accurate.
8. `tracker.last_poll_ok_at` advances on an early return (book unreadable, time exit active), so
   it means "loop alive", not "positions verified". Fix before wiring it to the heartbeat.
9. Notices still awaited before a state change on the order path (now bounded at 10 s each):
   `_notify_entry_outcome` before `_establish_position`, `notify_sl_placed` / `notify_sl_failed`
   in `_place_entry_bracket`, the day-summary pin and unpin calls (unbounded).
10. Broker evidence still needed from Flattrade logs: whether PositionBook returns `Not_Ok`
    mid-session on errors; whether RMS rejects an exit after accepting it; whether a 5xx can
    follow an accepted order.

## 4. Planned

Owner decisions of 2026-11-10 are noted where they fix the rule. Nothing below is implemented.
Each item needs a test that fails on the current code first, the full suite, and an Opus review
where it touches execution, risk or concurrency.

### Phase 2: lifecycle, concurrency and recovery (remaining)

Everything else in Phase 2 is implemented (section 1b).

| Finding | Work still open | Decision |
| --- | --- | --- |
| BUG-019, E-09 | Unified recovery for long stalls on entries, exits and stops. Design: `DESIGN-2026-11-UNCERTAIN-ORDER-RECOVERY.md`. | Owner reviews the state transitions before implementation. |
| E-12 remainder | The break-even flag is not restored after a restart. | Needs the flag persisted. |
| E-13 remainder | A partial exit confirmed late writes no trades.db row, so that leg is missing from a restored position. | Small; not done. |
| E-14 remainder | A dead stop re-placed while an untracked live twin exists leaves two stops until the next 120 s audit; a stop cancelled after a partial fill is re-placed at full size. | Not done. |
| E-06 remainder | The 45 s wait applies per leg; a stop placed during the wait is cancelled only by the later sweep. | Not done. |
| R-01 remainder | With a live position book that has no LTP, a position with earlier legs can still be priced from the broker's symbol total. | Rare; not done. |
| S-03 | Hard deadline around the bar fetch (`wait_for`). Only the skipped-bar log exists. | Not done; needs a safe test of fetch behaviour. |
| Residual 1 | Remaining bare-zero sites, or the Flattrade mapping fix. | Needs owner approval (core). |

### Phase 3: performance and resources

- E-16: one orderbook read per poll; reuse the shared HTTP client for orders.
- S-04: trailing-stop replay off the event loop (measure first).
- I-06, I-08: lock or thread-local connections for `risk.db` and `runner_state.db`; retry and alert on a failed `trades.db` write.
- D-05: DuckDB lock wait for the runner warm-up read.

### Phase 4: architecture and maintainability

- One close authority: a single internal function that every close path calls, taking the proof of flatness as an argument (removes the remaining duplication behind E-02, E-03, E-05, E-13, R-01).
- Split `main.py` along its existing seams with no behaviour change.
- S-05: ORB-PY backtest adapter over `strategy_core/orb.py`; correct the docstring that claims it is shared.
- One price-band helper; one P&L function for reports with gross and net labelled (M-04); config unknown-key warnings (I-09); I-04 lock file descriptor not inherited by children; I-07 in-process single-instance lock.
- Correct stale docs: `ACCOUNTING-INVARIANTS.md` time-exit paragraph, `mode_guard.py` docstring, scheduled-task times in the `.ps1` comments.

### Phase 5: integration testing and production readiness

- E-17: filled quantity, not ordered quantity, drives tracking and stop size. **Required before live. Not started: the stop is placed before the fill is known, and it needs a real partial fill to verify. Test with a small live order.**
- G8.1: flatten-all kill switch. **Required before live. Not started.**
- V-03: runtime promotion check. Research, backtest and sandbox unrestricted; live requires an explicitly approved promotion record. **Approved; keep it minimal.**
- V-01, V-02, V-04: minimum out-of-sample size and enforced window; promotion requires a passing reproduce attempt; trial ledger records the cut.
- B-01 to B-04: session-axis Sharpe in the tearsheet; backtest stop floor bound to the live profile; same-day swing exits priced as intraday; time exit at 14:45 by default. Each under a new version.
- M-03 then the G7 evaluators: per-trade comparison of sandbox fills with backtest assumptions.
- M-01, M-02, M-05: runner alert re-arm; narrow the sandbox reconcile suppression; floor the close-day verdict with the deterministic checks.
- D-02, D-03, D-04: one bar-sanity predicate for live and backtest; ex-date guard at warm-up; native 5m versus resampled 1m comparison.
- Engine liveness in the heartbeat and the 09:27 check (after residual 8); automated backup of `data/trades.db`; dead-man alert for missed cron runs.
- Fault-injection fakes (broker timeout, 5xx, accept-then-reject, empty book; blocking notifier) built on `pipeline/sim_sink.py`.

## 5. Operational notes open on 2026-11-12

- The history gap behind the 2026-11-10 and 2026-11-11 runner non-start is closed: Historify
  holds all 212 symbols for 2026-11-06 and 2026-11-09 (375 bars each) as of 2026-11-12. The
  runners started on 2026-11-12.
- The supervisor stop-order change (`9a8bb1720`) and the Phase 2 batch took effect with the
  restart at 20:06 IST on 2026-11-12.
- `signal_engine/bugs/bugs.json` and the BreakingTrade critical-alert files were modified by
  another session and are not part of this work.
