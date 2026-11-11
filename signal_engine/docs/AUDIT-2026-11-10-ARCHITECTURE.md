# signal_engine Architecture and Engineering Audit, 2026-11-10

Scope: the eight-stage algo trading architecture, applied to the existing OpenAlgo
`signal_engine` and its boundary with OpenAlgo core.
Baseline: branch `feature/optimize-signal-engine-v2.0.2.1`, HEAD `251ea1eaf`.
Status: AUDIT ONLY. No source, configuration or database was changed. No test was run. No
service was started or stopped. The working tree was snapshotted before and after the
analysis and is identical.

## How to read this report

- **Method.** Ten read-only analysts each covered one area (five on Opus for order
  execution, tracker, main loop, risk and backtest semantics; five on Sonnet for data,
  strategy runner, validation, monitoring and infrastructure). The orchestrator then re-read
  the code for every P0 and for most P1 findings.
- **Verification column.** `V` means the orchestrator re-read the cited code at HEAD and the
  mechanism is as stated. `A` means the finding rests on the analyst's reading only.
- **Class.** `CONFIRMED` means the failure path is traced in code. `PROBABLE` means the code
  path is traced but the trigger (a broker reply, a timing window) is not proven to occur.
  `IMPROVEMENT` means no defect, but a meaningful gain.
- **A code path that is confirmed is not proof that the failure has happened.** No finding
  below was reproduced by a test. Section 10 lists the test that turns each one into a
  demonstrated defect.
- **Known items** (BUG-007, BUG-008, BUG-019, the W1 crash window in `AUDIT-2026-10-22.md`)
  are not re-reported. Only new information about them is given.
- Line numbers are from HEAD `251ea1eaf`.

---

## 1. Executive summary

The system is substantially built and, in most places, carefully built. The audit does not
recommend replacing any component, any storage technology or the custom backtest engine.

**What is sound.** Timeout handling treats an unanswered order as unknown, never as rejected.
Exits are claimed before dispatch. The time exit never closes without a confirmed stop
cancel. Slot reservation is atomic and released on every path. Risk checks fail closed. The
closed-bar rule and bar labelling are identical in live, sandbox and backtest. The backtest
has an always-on look-ahead guard. Promotion fails closed on missing evidence, and promotion
to live is refused in code.

**Where the risk is.** The serious findings share one shape: **the engine takes a single
unconfirmed observation as fact and then removes its own protection**. The observation is a
missing positionbook row, a Pine "SL HIT" alert, an order id returned by the place-order
call, or the positionbook of the wrong mode. In each case the engine books a close, cancels
the stop or unregisters the position, while the broker may still hold it. A second shape is
**a task or handler that can end without anyone noticing**: the Telegram handler that is
cancelled mid-order, and the tracker poll loop that has no exception guard.

**Counts.** 3 P0, 22 P1, 34 P2, and a short P3 list.

| ID | Sev | One line |
| --- | --- | --- |
| E-01 | P0 | A Telegram disconnect or an engine shutdown cancels an alert that is in the middle of placing an order |
| E-02 | P0 (live) | A symbol missing from the positionbook is read as "closed": the live stop is cancelled and the position is dropped |
| E-03 | P0 (live) | An exit order that the broker accepts and then rejects is booked as a completed close |

**Sandbox versus live.** All trading so far is in sandbox mode (`trades.db` holds 3,585 rows,
all `analyze`). E-02 and E-03 need broker behaviour that the sandbox does not produce, so
they are live-readiness blockers, not current losses. E-01 applies in both modes today.

**Overall.** The system is fit to continue sandbox trading with the Phase 1 fixes scheduled.
It is **not ready for live capital** until Phase 1 and Phase 2 are complete. Section 11
gives the per-stage rating.

---

## 2. Eight-stage assessment

| Stage | Exists | Rating | Main gaps |
| --- | --- | --- | --- |
| 1 Market data | Historify (DuckDB), published versioned datasets, calendar-checked warm-up, one closed-bar rule, data-quality gates, shadow tick candles | Good for backtest; adequate for live | Calendar ends 2026 (D-01); live bars skip the cleaning that backtest bars get (D-02); no ex-date guard (D-03) |
| 2 Strategy and signal | Pine alerts over Telegram, Python runner pipeline, pure `strategy_core`, fail-fast config, per-strategy isolation, persistent dedupe | Good | No holiday gate (S-01); ladder exits miss skipped bars (S-02); cycle deadline not enforced (S-03) |
| 3 Backtest | Event-driven intraday and swing engines, guarded context, truncation check, per-leg statutory costs, ledgers with reconcile | Good | Headline Sharpe ignores idle days (B-01); stop floor not bound to the live profile (B-02); documented parity gaps |
| 4 Validation | Gate matrix, Newey-West t, DSR, walk-forward, purge and embargo, trial ledger, verdict and promotion with fail-closed rules | Good design, three weak controls | OOS gate has no minimum size (V-01); `reproducible` flag is mostly constants (V-02); promotion state is not read by the runtime (V-03) |
| 5 Risk | Sizing, price band, loss limits, atomic slot reservation, symbol and sector caps, operator hold, mode halts | Sound at one slot; latent at several | Unrealised loss is overwritten, not summed, in live (R-02); partial P&L counted differently per close path (R-01) |
| 6 Execution | Timeout reconcile, uncertain-order registry, late-duplicate watch, single-flight stops, per-leg time exit | Strong on timeouts, weak on confirmation | E-01 to E-15 |
| 7 Monitoring | In-engine alerts, 09:12 and 09:27 checks, 15:36 close-day, pages-vs-fills, execution health, bug tracker UI | Good after the close, thin during the session | Tracker death is silent (E-10); a vanished stop is not detected (E-14); no sandbox-versus-backtest per-trade comparison (M-03) |
| 8 Infrastructure | Supervisor with flock, restart budget, health probe, WAL SQLite, 416 test files with DB and network isolation | Good | Tests write the live `risk.db` (I-01); the 14:55 failsafe ignores its own failures (I-02); unknown mode is treated as live at startup (I-03) |

Responsibilities are separated well enough. The eight stages do not need to become eight
packages. The one structural weakness is that `main.py` (2,958 lines) and `tracker.py`
(2,642 lines) each hold several responsibilities, which is why the same rule (for example
"how a close is booked") exists in several slightly different copies.

---

## 3. Component and capability inventory

| Capability | Where | State |
| --- | --- | --- |
| Signal intake (Telegram) | `listener.py`, `normalizer.py`, `parser.py` | Implemented; E-01, E-15 |
| Signal intake (Python runner) | `pipeline/service.py`, `runner.py`, `cycle.py`, `sink.py` | Implemented; S-01 to S-03 |
| Validation and dedupe | `validator.py`, `pipeline/state.py` (`sig_ids`, `last_bar`) | Implemented, sound |
| Freshness | `freshness.py`, `listener.py:176`, `main.py:2918` | Implemented; checked once, early (E-19) |
| Entry pipeline | `main.py:1563-1631`, `2408-2485` | Implemented |
| Risk engine and store | `risk.py`, `risk_store.py`, `runtime.py` | Implemented |
| Trade policy | `strategy_core/trade_policy.py` | Implemented, pure |
| Order submission | `executor.py`, `api_client.py` | Implemented; E-07 |
| Uncertain-order handling | `order_reconcile.py`, `main.py:2025-2377` | Implemented, sound; BUG-019 open |
| Position tracker | `tracker.py` | Implemented; E-02, E-10 |
| Stop lifecycle | `executor.place_sl_order`, `tracker._retry_missing_sl` | Implemented; E-14 |
| Exit drivers | `pipeline/exit_driver.py`, `trail_driver.py` | Implemented; S-02 |
| Time exit | `tracker.time_exit_all`, `TimeExitScheduler` | Implemented; E-06 |
| Restart recovery | `startup.py:236-622` | Implemented; E-12, E-13, R-03 |
| Mode guard | `mode_guard.py` | Implemented for entries; E-04 |
| Historical data | `database/historify_db.py`, `backtest/data.py`, `backtest/data_readiness/` | Implemented |
| Live bars | `pipeline/bars.py`, `context.py`, `clock.py` | Implemented; D-02 |
| Backtest engines | `backtest/engine.py`, `swing_engine.py`, `harness.py`, `causal.py` | Implemented |
| Costs | `backtest/costs.py`, `portfolio/` | Implemented; B-03 |
| Evaluation and promotion | `backtest/evaluation.py`, `verdict.py`, `promotion.py`, `research/intake/` | Implemented; G7 and G8 gates have no evaluators (known) |
| Monitoring and reports | `analysis/`, `ops/`, `notifier.py`, `reconcile.py` | Implemented |
| Supervision and scheduling | `scripts/openalgoctl.sh`, `.ps1`, `openalgoscheduler.py`, cron | Implemented; I-02, I-04 |
| Shared risk core `services/risk/` | platform | **Not used by signal_engine.** No import. See section 8 |
| Kill switch that flattens | none | **Missing** (known, gate G8.1) |
| Partial-fill handling | none | **Missing**; live only (E-17) |
| Sandbox versus backtest per-trade comparison | none | **Missing** (M-03) |
| Engine liveness signal (as opposed to process liveness) | none | **Missing** (E-10) |
| Database backup of `data/trades.db` | none found | **Missing** |

---

## 4. Architecture and dependency map

```
 Pine alert --> Telegram --> listener --> normalizer --> parser --+
                                                                  |
 Historify (warm-up) --+                                          v
 /api/v1/history ------+--> pipeline.bars --> runner/cycle --> dispatch_signal (main.py)
                             (closed bars)     strategy_core        |
                                               exit drivers         |  freshness, validate
                                                                    v
                                              entry: halt -> symbol rules -> trade policy
                                                     -> risk gates + reserve_entry (atomic)
                                                     -> capital -> size -> margin
                                                     -> executor.send_order -> save row
                                                     -> record_trade -> place stop -> fill
                                                     -> bad-fill guard -> notify -> register
                                                                    |
                                              exit:  exit lock + exit_pending claim
                                                     -> cancel/sweep stops -> send with
                                                        timeout reconcile -> book -> unregister
                                                                    |
 tracker poll (5 s): sweep unresolved -> reconcile stops -> positionbook -> mode check
                     -> book broker closes -> retry missing stops -> no-progress
 TimeExitScheduler (14:45): cancel -> confirm cancel -> close per leg -> verify -> book
 Windows task (14:55): cancel all + close all per strategy (out of process)

 State owners:  trades.db (ledger)   risk.db (counters)   runner_state.db (dedupe)
                tracker._positions (memory, rebuilt from broker + trades.db at startup)
                OpenAlgo core: order routing by the analyzer toggle, sandbox.db, broker
```

**Concurrency model.** One asyncio loop owns all trading state. The only threads are the
runner's four-thread executor, which computes strategy evaluation and touches no trading
state. Guards are the per-position `asyncio.Lock`, the `exit_pending` claim (checked and set
with no `await` between), `_pnl_lock`, and `_STOP_LOCKS`. No deadlock was found: the only
lock order is exit lock then `_pnl_lock`.

**Dependency on OpenAlgo core.** The engine is an HTTP client of `/api/v1/*`. Every call is
routed by the platform-wide analyzer toggle at the moment of the call
(`services/place_order_service.py:166`). The REST endpoint cannot pass `force_live`, so the
engine cannot pin an exit to the pipe the position was opened on. This is the root of E-04.

---

## 5. Signal engine deep dive

### 5.1 Position state machine

```
 (order accepted) --register--> REGISTERED --first non-zero qty--> CONFIRMED
 CONFIRMED --partial exit--> PARTIAL (quantity reduced, realized_pnl accumulated)
 any open state --exit_pending=True--> CLAIMED --booked--> CLOSED (unregister / pop / del)
                                       CLAIMED --exit failed--> open again, stop id cleared
 side flags: ORPHANED (entry never traded), TIME_EXIT_LEFT_OPEN (kept tracked)
```

Writers of CLOSED: the signal exit handler (`main.py:1168`), the poll (`tracker.py:1239`),
the no-progress exit (`tracker.py:1903`), the time exit (`tracker.py:2498`), the SL-HIT
reconcile (`main.py:567`) and startup reconciliation (`startup.py:294`). Six writers is the
reason accounting rules drift between paths (R-01, E-13).

### 5.2 Shared-state inventory

| State | Owner | Guard | Persisted |
| --- | --- | --- | --- |
| `tracker._positions` | tracker | `exit_pending`, exit locks | No. Rebuilt from broker and `trades.db` |
| `pos.entry_time`, `be_stop_applied`, `realized_pnl`, `exit_types` | tracker | same | **No. Lost on restart** (E-12) |
| `risk_engine._by_strategy`, `_pending_entries` | risk | single loop | Counters yes; pending no |
| `_positions_by_symbol`, `_positions_by_sector` | risk | single loop | No. Re-seeded at startup |
| `unrealised_loss`, `last_known_capital` | risk | single loop | No |
| `order_reconcile._uncertain`, `_late_duplicates` | order_reconcile | single loop | No |
| `executor._UNCONFIRMED_SL`, `_PENDING_DUPLICATE_STOPS` | executor | `_STOP_LOCKS` | No |
| `validator._recent_signals` | validator | sync check-and-set | No |
| `sig_ids`, `last_bar` | `pipeline/state.py` | loop only | Yes |
| `mode_guard._cache`, halt flags | mode_guard | none | No |

### 5.3 Critical paths that were traced and found sound

Keep these as they are. Do not refactor them as part of any fix below.

- Timeout is never a rejection: exits `main.py:703-715`, `754-787`; stops
  `executor.py:419-455`; entries `main.py:2025-2077`. An unreadable book is UNCERTAIN, never
  ABSENT (`order_reconcile.py:330-359`).
- Adoption of a late fill needs proof from the broker position (`main.py:807-835`).
- Claim before dispatch, released by every holder (`main.py:296-332`,
  `tracker.py:1224-1241`, `1705-1711`).
- One stop submission per key; cleanup never cancels the last live stop
  (`executor.py:355`, `628-642`).
- The time exit closes per leg, only after a confirmed cancel, capped at the strategy's own
  quantity (`tracker.py:2039-2156`).
- A synchronous exit failure leaves the position registered and clears the dead stop id
  (`main.py:1477-1514`).
- Atomic check-and-reserve, with pending reservations counted against every cap
  (`main.py:1733-1758`, `risk.py:664-727`, `798`, `811`).
- The entry row is written as soon as the order is accepted (`main.py:1617-1620`).

---

## 6. Cross-stage integration findings

| # | Interface | Verdict | Evidence and gap |
| --- | --- | --- | --- |
| 1 | Data to Strategy | Sound for timing; gap in content | One closed-bar rule and one epoch-to-IST conversion (`bars.py:285-289`, `336`). Live bars are not sanity-cleaned (D-02). Warm-up is resampled 1m, today is native 5m, and the two have never been compared (D-04) |
| 2 | Strategy to Risk | Sound | Every entry passes halt, symbol rules, trade policy and risk gates before any order. The runner's signals carry no origin time, so the freshness gate does not apply to them (S-06) |
| 3 | Risk to Execution | Sound for reservation; gap for quantity | The reservation is handed to `record_trade` with no `await` between. The approved quantity is assumed to be the filled quantity; `filled_quantity` is never read (E-17) |
| 4 | Execution to Position | **Weakest interface** | An order id is treated as a fill (E-03). A missing book row is treated as flat (E-02). A cancelled handler leaves an order with no position (E-01). Notification is awaited before registration (E-11) |
| 5 | Position to Risk | Inconsistent | Partial P&L reaches the counters on one close path of four (R-01). Pooled unrealised loss is overwritten (R-02). The restart counter is derived from broker rows, not legs (R-03) |
| 6 | Backtest to Validation | Mostly sound | The gate G2.3 zero-fills idle days but the stored headline Sharpe does not (B-01). The backtest stop floor is not the live floor (B-02) |
| 7 | Sandbox to Monitoring | Sound after the close | Pages-vs-fills rebuilds P&L from raw fills. The engine-versus-funds canary is suppressed for every sandbox mismatch (M-02). Gross and net P&L are mixed across reports (M-04) |
| 8 | Monitoring to Research | **Open loop** | Nothing in `backtest/` or `research/` reads `trades.db` or sandbox fills. Gates G7.x have no evaluators. The promotion stage is not read by the runtime (V-03, M-03) |
| 9 | Restart to Recovery | Partly sound | Positions, stop ids and counters are rebuilt. Entry time and trail state are not (E-12). Reconciliation of closes that happened while down can double-book or miss (E-13). A cancelled or crashed send leaves no trace (E-01, known W1) |

**Boundary that most needs improvement: Execution to Position.** A single rule would remove
E-02, E-03, E-04 and E-05 together: *a position leaves the tracker only on positive proof
that it is flat*. Positive proof is one of: the exit or stop order's own status is
`complete`, or the positionbook holds a row for the symbol with quantity zero **and** the
mode is the mode the position was opened in.

---

## 7. Defect and improvement register

### 7.1 P0

#### E-01. A Telegram disconnect or an engine shutdown cancels an in-flight order pipeline

- **Stage:** 6 (and 2). **Class:** CONFIRMED mechanism. **Verification:** V.
- **Location:** `listener.py:198-199` (`await on_message(msg.text)` inside the Telethon
  handler); `listener.py:236`, `240`, `322`; `startup.py:983-991`; Telethon
  `client/telegrambaseclient.py:747-751` and `client/updates.py:289-291`.
- **Current:** every Pine alert runs the whole entry or exit pipeline inside a Telethon
  event-handler task. Telethon's `disconnect()` cancels every event-handler task. The engine
  calls `disconnect()` from the keepalive on any detected drop and at shutdown. Shutdown does
  not wait for in-flight dispatches.
- **Weakness:** a network event unrelated to trading delivers `CancelledError` into the
  middle of `send_order`, `_establish_position` or the exit path.
- **Scenario:** an entry alert arrives at a bar close. `send_order` is slow (BUG-007 stalls
  of 25 s and more are recorded; `order_timeout` is 20 s). Telegram drops in that window
  (the logs hold 170 "Telegram disconnected" lines). The handler is cancelled. OpenAlgo
  still places the order. Result: no `trades.db` row, no uncertain mark, no stop, no tracked
  position, and the slot is released. A cancel between `save` and `tracker.register`
  (`main.py:2420-2467`) leaves a row but an unmanaged position until restart. A cancel after
  the stop is cancelled in the exit path leaves the position without its stop.
- **Fix:** in the handler, run the pipeline as its own task held in a module-level set and
  `await asyncio.shield(task)`. In `_run_engine`'s `finally`, wait (bounded) for that set and
  for the runner's in-flight dispatches before `_close_resources`.
- **Benefit:** removes the largest known source of an untracked position.
- **Regression risk:** low. No trading rule changes. Check that the freshness ContextVar is
  copied into the task (`create_task` copies the context).
- **Tests:** cancel the handler while `send_order` is suspended and assert the row is saved
  and the position is registered; assert shutdown waits for an in-flight dispatch.
- **Known:** the crash window during send is W1 in `AUDIT-2026-10-22.md:371-386`. New: a
  network blip and a normal shutdown both trigger it.

#### E-02. A symbol missing from the positionbook is read as closed

- **Stage:** 6. **Class:** PROBABLE (code path V; broker trigger not proven). **P0 in live.**
- **Location:** `tracker.py:1206-1207` (`qty = entry.quantity if entry else 0`);
  `tracker.py:182-191` (index keyed by symbol only); `tracker.py:1508-1514` (cancel the stop
  if it is not `complete`); `broker/flattrade/mapping/order_data.py:330-335` (any
  `stat: Not_Ok` becomes `{}`); `services/positionbook_service.py:157-170` (returns
  `success` with the empty list).
- **Current:** a tracked symbol with no row in the book gets quantity 0 and goes down the
  close-booking path. For Flattrade, a `Not_Ok` reply (which covers "no data" and errors)
  reaches the engine as a successful empty book.
- **Scenario:** one positionbook read returns an empty list while positions are held. For
  each position past the minimum age with a known fill, the poll books a close, finds the
  stop "trigger pending", **cancels the live stop**, writes an `SL` row and drops the
  position. The broker still holds the position, now with no stop and nothing tracking it.
  The same happens when the book has two rows for one symbol (two products) and the flat row
  is indexed last.
- **Fix:** distinguish *absent* from *present with quantity 0*. When a tracked symbol is
  absent, or the whole book is empty while positions are tracked, do not book. Confirm
  first: require the stop order's status to be `complete`, or a second independent read.
  Key the index on (symbol, exchange, product).
- **Benefit:** a transient read can no longer remove protection.
- **Regression risk:** low; a real close may be booked one poll later.
- **Tests:** `test_tracker_close_detection.py:323-332` pins "absent = closed". **This
  finding conflicts with that test, so the owner must decide the rule** before any change.
  Add: empty book while positions are held; two product rows for one symbol.
- **What would change the rating:** Flattrade order and position logs showing whether
  `Not_Ok` is ever returned mid-session for a session or rate error.

#### E-03. An exit that the broker accepts and then rejects is booked as a close

- **Stage:** 6. **Class:** PROBABLE (code path V). **P0 in live, P1 in sandbox.**
- **Location:** `main.py:383-397`, `1064-1101` (`_book_exit_result`); `tracker.py:1849-1877`
  (`_no_progress_market_exit`); `executor.py:252-269`; `api_client.py:418-422`.
- **Current:** `SUCCESS` means the place-order call returned an order id. When the fill price
  is unavailable the code logs a warning and books the portfolio realised delta. It never
  reads the exit order's status.
- **Scenario:** the stop is cancelled, a market exit is accepted, and the broker's RMS then
  rejects it. The engine books a close with an estimated P&L and unregisters. The broker
  holds the position with no stop. In the no-progress path the position stays tracked but
  `sl_order_id` is set to the rejected exit's id, so the missing-stop retry skips it.
- **Fix:** when the fill is `None`, read `fetch_order_status`. On rejected or cancelled,
  take the existing failure path (`_handle_exit_order_failure`, `main.py:1464`). In the
  no-progress path call `_restore_stop_after_failed_exit`.
- **Regression risk:** low; one extra read on the fallback path only.
- **Tests:** "accepted then rejected" for the signal full exit and for the no-progress exit.

### 7.2 P1

| ID | Stage | Class | Ver | Location | Current implementation and weakness | Failure scenario | Recommended fix | Regression risk | Tests |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E-04 | 6 | CONFIRMED path | V | `tracker.py:1018-1033`; `mode_guard.py:26-29` | The poll reads the positionbook, then calls `_check_mode_flip`, then evaluates every position against that book. Nothing skips close-booking after a mode change. Every call follows the analyzer toggle of the moment | The operator switches the toggle mid-session. The next poll reads the other mode's book, sees quantity 0, books every position closed with a P&L mixed across modes, and stops managing them. `mode_guard`'s docstring says existing positions keep their tracker; they do not | In `check_positions`, check the mode before using the book and skip close-booking while the mode is halted; alert that positions rest on their broker stops. Longer term: let a trusted key pin the pipe from REST | Low | Both switch directions; assert no close is booked |
| E-05 | 6 | CONFIRMED path | V | `main.py:541-569` | A Pine "SL HIT" alert cancels the tracked stop, books the close at the stop price and unregisters. The broker position is never read | Pine's stop level differs from the engine's stop (the engine recomputes it after TP1), or the chart touches the level and the exchange price does not. The engine cancels a working stop and drops an open position. The Python runner checks the broker first; Pine strategies get no check | Read `tracker.fetch_own_position` first. If not flat: do not cancel, do not unregister, clear `exit_pending`, alert | Low | SL HIT while the broker still holds, with and without a stop id. `test_main_characterization.py:348-410` pins "never send a second SELL", which stays true |
| E-06 | 6 | PROBABLE | V | `tracker.py:2062-2099`, `2372` | `_close_strategy` sends a close for every MIS leg. It does not check `exit_pending` or take the exit lock. Only the booking step skips claimed positions | A signal exit starts at 14:45:01 and is inside its cancel-and-sweep awaits. The time exit reads the full net position and sells. The handler resumes and sells again. A reversed, untracked position remains | Per leg, take the exit lock with a bounded wait (or wait for `exit_pending` to clear), re-check registration, re-read the net position, then send | Medium (time-exit path) | Barrier test: hold a signal exit in `_cancel_sl_before_exit`, fire `time_exit_all`, assert exactly one exit order |
| E-07 | 6 | PROBABLE | V | `executor.py:226-231`, `258-263`; `main.py:693-717`, `1612` | HTTP 4xx and 5xx are both REJECTED. Any non-timeout exception is ERROR. Only TIMEOUT is reconciled; REJECTED and ERROR exits are resent up to 3 times | Core turns a broker exception after the request was sent into HTTP 500. The order exists. The exit is resent (the BUG-019 result by another route), or an entry is dropped while the broker holds it | Treat 5xx and post-send transport errors as TIMEOUT so they use the existing reconcile paths. Keep 4xx as REJECTED | Low | `_interpret_placeorder` with 500, 502, 504; `send_order` raising `RemoteProtocolError` |
| E-08 | 6 | PROBABLE | A | `tracker.py:1206-1211`, `2101-2130` | A leg is booked closed only when the symbol's net quantity is 0. No poll reads a stop's order status | Two strategies hold the same symbol. One stop fills; net is still non-zero, so the leg is never booked. At 14:45 the other leg computes a close quantity of 0 and is left to the broker square-off | For a leg whose symbol another tracked leg holds, read the stop's status each poll (throttled) and book from its own fill | Medium | Two legs, one stop fills |
| E-09 | 6 | PROBABLE; extends BUG-019 | A | `main.py:2064-2082`, `2229-2243`; `executor.py:136-137` | A timed-out entry is watched for 120 s; exits get 600 s. Stop resend grace is 25 s | A stall longer than 120 s (354 s was recorded) lands the entry after the watch ends: an untracked position with no stop. Several stops can stack | Decide with BUG-019. Interim: an entry late-fill watch that adopts and protects. **Trading behaviour: needs approval** | Medium | Late entry fill after the watch window |
| E-10 | 6, 7, 8 | PROBABLE (three analysts independently) | V | `tracker.py:2553-2556`; `startup.py:977` | The poll loop has no `try/except`. The task has no done-callback. `RiskStore.save` and `record_close` run inside it unguarded | One exception ends the poll task. The process, PID and heartbeat stay healthy. Close detection, stop retry, no-progress and the outage alert all stop, silently | Guard each iteration with `logger.exception` and a repeating alert. Add a done-callback. Publish a last-poll timestamp for the liveness check | None | `check_positions` raises once; the loop continues and alerts |
| E-11 | 6, 7 | PROBABLE (ordering V; delay not measured) | V | `main.py:2455-2467`; `notifier.py:289-335`; `listener.py:43` | `notify_entry_filled` is awaited before `tracker.register`. Sends have no timeout. The Telethon flood-sleep threshold is 120 s | Telegram throttles. The entry has filled, but for up to two minutes the position is not in the tracker: no close detection, no time exit | Register first, then notify. Add a bounded wait inside `notify()` | Low | A fake client that sleeps 60 s; `register` must still complete promptly |
| E-12 | 6 | CONFIRMED | V | `startup.py:587-607`; `tracker.py:233` | Restored positions do not set `entry_time`, so it is the restart time. `be_stop_applied`, `realized_pnl`, `exit_types` are also lost | The trailing exit replays only from the first bar after restart, so a stop that had ratcheted up falls back to the initial stop. No-progress timing restarts. This contradicts the "a restart rebuilds the same stop" docstrings | Pass `entry_time` from the entry row's `executed_at` | Low | Restore test for `entry_time`; trail replay after restart equals the stop without a restart |
| E-13 | 6, 9 | CONFIRMED | V | `startup.py:276-306`; `db.py:465-508` | A leg closed while the engine was down is booked with the broker row's symbol-wide realised P&L and LTP as fill. `fetch_all_open_positions` drops a pair whose latest row is an EXIT, including a partial exit | Two strategies on one symbol each get the full symbol P&L, and the daily-loss gate is corrupted. A runner stopped out after a TP partial while down is never reconciled | Price each leg from its own closing order. Alert instead of using the symbol figure when more than one leg exists. Treat partial EXIT rows as not closing | Medium | Two strategies flat on one symbol; a runner closed after a partial |
| E-14 | 6, 7 | CONFIRMED gap | A | `tracker.py:1075-1106` | The missing-stop retry acts only when `sl_order_id` is empty. The audit looks only for duplicates | A stop that is rejected or cancelled by the broker after placement leaves the position unprotected with no alert | In the existing 120 s audit, compare each tracked stop id with the book already fetched; on cancelled or rejected, clear the id and alert | Low | A stop cancelled by the broker after placement |
| E-15 | 2, 6 | CONFIRMED against stated intent | V | `listener.py:176-183`, `140` | The listener drops any message older than 60 s, exits included, and records a decline only for entries. `main.py:2915-2917` says exits are exempt | A delayed "TP1 HIT" or safety "EXIT" is discarded with no trace | Apply the listener's stale check to entries only; record every drop. **Owner to confirm intent** | Low | Listener-level test with a stale TP1 HIT and a stale EXIT |
| I-01 | 8 | CONFIRMED pollution, PROBABLE cause | V | `main.py:87`; `tests/conftest.py` (no `RISK_DB_PATH` redirect) | `RiskStore(RISK_DB_PATH)` runs at import. 77 test files import `signal_engine.main`. The engine defaults to mode `live` | The live `data/risk.db` holds `PORTFOLIO/live` rows with trades and open positions on Saturday 2026-10-24 and Sunday 2026-10-25, while `trades.db` has no live row at all. A test run on a live day can seed today's counters and refuse real entries | Autouse redirect of `RISK_DB_PATH` and the store; a guard test that the live `risk.db` is unchanged after a run | None | The guard test |
| I-02 | 8 | CONFIRMED | V | `scripts/openalgoscheduler.py:1349-1357` | The 14:55 failsafe discards the results of `cancel_all_orders` and `close_all_positions`, logs "done" and exits 0 | The app is down or rejects the key (this happened on 2026-11-06). Nothing closes and nobody is told | Collect results, retry, alert outside the routine-notice gate, exit non-zero | Low | `_run_squareoff` with a failing client |
| I-03 | 8, 6 | CONFIRMED | V | `api_client.py:96-112`; `startup.py:944-957` | A failed mode lookup returns `("unknown", False)`. Startup ignores the string and configures `live` | One blip at startup on a sandbox day labels rows, logs and channels as live, then the first poll halts entries for the day. The direction is fail-safe, but the day is lost and rows are mislabelled | Retry; refuse to start while the mode is unknown | Low | Startup with an unknown mode |
| D-01 | 1 | CONFIRMED | A | `research/reference/nse_calendar.yaml`; `backtest/data_readiness/calendar.py:74-77`; `pipeline/context.py:105-108` | The calendar covers 2016 to 2026. A date outside raises, and warm-up then rejects every symbol | From the first session of 2027 the runner warms no symbol. It fails closed and alerts, but trading stops | Add 2027 from the official NSE circular. Add a readiness check that warns from 1 December when next year is missing | None | Readiness check with a stub calendar |
| S-01 | 2 | CONFIRMED | V | `pipeline/service.py:213` | `run_days` tests only `weekday() < 5` | On 2026-10-20 (a holiday) the runner ran 24 cycles per strategy, each re-requesting all symbols, against an endpoint already near its limit | Skip days the loaded calendar marks as non-sessions; fail closed if unreadable | Low; handle Muhurat sessions deliberately | `run_days` on a holiday and on a special session |
| S-02 | 2, 3 | CONFIRMED | A | `pipeline/cycle.py:275-279`; `exit_driver.py:133`, `199` | The ladder is stepped with one bar per cycle and never replays a missed bar | TP1 trades in a bar that is late for the symbol. The partial exit never happens. The backtest ladder sees every bar, so the two diverge on late-data days (377 late-bar lines on 2026-11-04) | Step the ladder over every closed bar since its last step. **Trading behaviour: needs approval** | Medium | Ladder with a bar gap |
| S-03 | 2 | CONFIRMED mechanism; extends BUG-007 | A | `pipeline/bars.py:432-443`, `481-499`; `clock.py:26` | The 25 s deadline is checked only between fetch rounds. One lock is held across a round. An overrun past the next boundary skips a bar with no log | A 330 s cycle drops one bar for evaluation and for ladder exits | Wrap the fetch in `wait_for`; log "bar skipped by overrun" | Moderate | A slow source; a clock that jumps a boundary |
| V-01 | 4 | CONFIRMED weak control | A | `backtest/evaluation.py:640-679`, `654` | Gate G3.2 has no minimum OOS trade or session count. G3.1 is report-only. `--oos-start` is free-form | Five trades in the last three weeks with a positive mean pass the out-of-sample gate. The cut can be moved until it passes | Minimum OOS size (G3.4 already uses a 100-trade floor); make G3.1 pass or fail; record the cut in the trial ledger | Old runs become MARGINAL | Small OOS; out-of-range fraction; repeated run with a moved cut |
| V-02 | 4 | CONFIRMED | A | `backtest/run_artifact.py:448-460`; `research/intake/lifecycle.py:90-91` | Six of eight reproducibility checks are the literal `True`. Promotion does not require a passing `reproduce` attempt | The dataset is refreshed after the run and the run is promoted anyway | Require the latest reproduce attempt to pass at `intake stage paper` | Slower promotion | A missing or failing attempt blocks promotion |
| V-03 | 4, 8 | PROBABLE design gap | A | idea frontmatter only; `config.py:496-500` | No runtime module reads the research stage or the promotion decision | A strategy is wired to a channel in `config.yaml` without passing any stage, and nothing notices. The approval step has no recorded actor | A startup check that each enabled strategy maps to an idea at the required stage: warn in sandbox mode, refuse in live mode. **Owner to confirm intent** | Low | A strategy with no qualifying idea |

### 7.3 P2

| ID | Stage | Class | Location | Finding | Recommended fix |
| --- | --- | --- | --- | --- | --- |
| R-01 | 5 | CONFIRMED; intent unclear | `main.py:1117-1132`, `1169`; `tracker.py:1459`, `2484` | Partial-exit P&L reaches the risk counters only when the final close is a time exit. A TP1 of +500 then a runner stop of -300 counts as a 300 loss | One rule on all close paths. **Owner decides: trade total or per leg** |
| R-02 | 5 | CONFIRMED; latent P1 | `tracker.py:925-926`; `risk.py:467-469`, `223-227` (V) | In live, every strategy maps to one bucket, and `update_unrealised` replaces the value per strategy. The last strategy in the loop wins. Masked today because the live cap is 1 | Sum per `risk_engine._key(strategy)` before the call |
| R-03 | 5, 9 | CONFIRMED; latent | `startup.py:319`, `357`, `390-454` | The restart counter is the number of broker symbols, not engine legs, and hidden-leg booking then decrements again | Count restored legs per strategy; book hidden legs without a second decrement |
| R-04 | 5, 8 | PROBABLE | `risk_store.py:123-139`; `risk.py:847-853`; `main.py:2420` | A `risk.db` write error escapes into `_establish_position` before the stop is placed, and into the poll, where the retry double-counts | Catch, log and alert inside `_persist`; memory stays authoritative |
| E-16 | 6 | IMPROVEMENT | `main.py:2212-2217`, `2322-2326`; `executor.py:611`, `218` | The orderbook is read once per unconfirmed order, exactly when OpenAlgo is slow. `send_order` opens a new HTTP client per order | One book read per poll, passed in; reuse the shared client |
| E-17 | 6 | IMPROVEMENT; live only | `main.py:2437-2474` | `filled_quantity` is never read. The stop and the tracked quantity use the ordered quantity | Read the filled quantity before sizing the stop |
| E-18 | 6 | IMPROVEMENT | `main.py:1563-1584` | Nothing blocks a new entry during or after the time exit | Entry gate on the time-exit state. **Needs approval** |
| E-19 | 2, 6 | IMPROVEMENT | `main.py:2918-2925`, `1611` | Freshness is checked once, before waits that can exceed the limit | Re-check immediately before `send_order`, entries only |
| E-20 | 6 | CONFIRMED | `tracker.py:1279-1281`, `1451-1457` (V) | An entry confirmed `complete` does not record a fill price, so a real break-even stop-out is booked as an orphan and no EXIT row is written | Set `fill_price` on `complete`, as the stuck-order branch does |
| D-02 | 1 | PROBABLE | `pipeline/bars.py:330-340`; `backtest/data.py:239-270` | Live bars get a float cast, dedupe and sort only. Backtest bars also get the session window and impossible-bar checks. A bar with low 0 would trigger a ladder stop | One shared `sane_bars` predicate applied in `parse_history` |
| D-03 | 1 | PROBABLE | `pipeline/`, `features/` (no corporate-action handling) | On an ex-date, warm-up levels are on the old price scale and today's bars on the new. The backtest excludes these sessions | At warm-up, exclude a symbol whose first price fails the 0.75/1.33 test against the previous close |
| D-04 | 1 | IMPROVEMENT | `backtest/data.py:359-364`; `pipeline/context.py:202-212` | Native 5m and resampled 1m bars are joined in one frame and have never been compared. The shadow recorder has no recorded session | Run the comparison for a few sessions before changing anything |
| D-05 | 1 | PROBABLE | `backtest/data.py:311` | The warm-up DuckDB read has no lock wait, unlike `backfill.py:56-68` | Reuse the backfill lock-wait helper |
| S-04 | 2 | PROBABLE | `pipeline/cycle.py:279`; `trail_driver.py:273` | The trailing-stop replay rebuilds its inputs synchronously on the event loop that also runs the tracker | Run it in the executor, or cache per bar. Measure first |
| S-05 | 2, 3 | CONFIRMED | `strategy_core/orb.py:29`; `backtest/strategies/orb.py` | ORB-PY shares no code with the ORB backtest adapter. Seven rules differ (stop multiple 2.0 against 3.0, TP floor, volume warm-up, and others). The docstring claims they are shared | Correct the docstring and strategy card now. A real adapter over `strategy_core.orb` before any ORB-PY promotion |
| S-06 | 2 | IMPROVEMENT | `pipeline/cycle.py:163`, `251` | Staleness is checked before evaluation, not before submit. A 112 s cycle submitted an entry against a 60 s limit | Re-check before `sink.submit`, entries only. **Needs approval** |
| B-01 | 3, 4 | CONFIRMED | `backtest/tearsheet.py:94-98`; `metrics.py:114-120` | The headline Sharpe uses traded days only and annualises by 252. A strategy trading 60 of 250 sessions is inflated about twofold. The gate is correct; the stored and displayed number is not | Reuse `evaluation._session_axis`, under a new tearsheet version |
| B-02 | 3 | CONFIRMED | `backtest/types.py:57`; `validator.py:141-165`; `config.yaml:1063` | The backtest stop-distance floor is 0.2%. The live floor is per strategy (0.4% for EVWAP-EARLY-PY). The backtest trade set is not the set that trades | Bind `min_sl_pct` from the live profile in `record_run`, or fail on mismatch |
| B-03 | 3 | CONFIRMED | `backtest/costs.py:63-78`, `257` | A swing trade that exits on its entry day is charged delivery costs (about 18 bps plus the DP charge too much). The bias is pessimistic | Price same-day exits on the intraday schedule, under a new cost-model version |
| B-04 | 3 | Parity gap | `backtest/engine.py:469-480` | The default time exit is the close of the 14:45 bar (14:50). Live closes at 14:45 | Make `time_exit_at_open=True` the default for new runs |
| B-05 | 3 | Parity gap; partly known | `config.yaml:704-708`; `tracker.py:119-128` | The live no-progress exit is not modelled for Pine-alert strategies | Record it as a qualification in the run record, or model it as an opt-in exit |
| V-04 | 4 | CONFIRMED; documented | `research/intake/trials.py:33-37`, `223-246` | The trial fingerprint excludes dataset and date range. A re-run on a refreshed window is not a new trial, and the OOS window moves | Record `oos_start` and dataset end in the ledger; count a new cut as a trial |
| V-05 | 4 | CONFIRMED; disclosed | `backtest/stats.py:111` | The DSR trial variance is assumed, not measured. PBO is not evaluated when trials exceed one | Feed the per-combination P&L that `tuning.py` already has |
| M-01 | 7 | CONFIRMED | `pipeline/runner.py:63`, `116-127` | The runner failure alert fires once per session with no re-arm | Alert again after N consecutive failures or no successful cycle for two bars |
| M-02 | 7 | CONFIRMED | `reconcile.py:76-79` | Every sandbox P&L mismatch is treated as a known limitation | Narrow the suppression to days with a multi-exit position |
| M-03 | 7, 4 | CONFIRMED gap | `analysis/ledger.py:96-110` | Slippage is measured against the alert price, not the backtest's assumed fill. Nothing compares a sandbox trade with its backtest counterpart | A daily per-trade table from replaying the day's signals through `engine.simulate` |
| M-04 | 7 | CONFIRMED | `analysis/execution_health.py:1109`; `pages_vs_fills.py:461-505` | The Telegram summary is gross; the UI is net. Two engine P&L algorithms exist | Label every figure gross or net; one P&L function |
| M-05 | 7 | CONFIRMED | `ops/verdict.py:19-32` | The close-day verdict is written by a model and is not floored by the deterministic checks | Take the worse of the model verdict and `eod_review.all_passed` |
| I-04 | 8 | PROBABLE | `scripts/openalgoctl.sh:72` | The flock file descriptor is inherited by the children. If the supervisor dies and they survive, every restart is refused and the engine runs unsupervised | Launch children with `9>&-` |
| I-05 | 8 | CONFIRMED | `scripts/openalgoctl.sh:208-219` | SIGKILL follows SIGTERM after 2 s; the engine's shutdown needs up to 5 s. With the E-01 fix it needs longer | Poll for exit up to about 15 s |
| I-06 | 8 | CONFIRMED | `risk_store.py:33`; `pipeline/state.py:62` | One shared SQLite connection, no lock, against the project's own recorded lesson (`db.py:96-100`). Loop-only today | A lock per method, or thread-local as in `db.py` |
| I-07 | 8 | IMPROVEMENT | `main.py`, `startup.py` | The single-instance guard is in the shell only | A non-blocking flock inside `main()` |
| I-08 | 8 | PROBABLE | `db.py:266-267`, `299-301`, `381-382` | Every save catches, logs one line and returns `None`, with no retry. Synchronous SQLite with a 10 s timeout runs on the event loop | One short retry; alert on final failure |
| I-09 | 8 | IMPROVEMENT | `config.py` | Unknown keys in `config.yaml` are ignored. An empty API key is accepted | Warn on unknown keys; require the key |

### 7.4 P3

- `_pnl_lock` is held across an HTTP call (`main.py:1025`, `tracker.py:1366-1372`); under
  slow calls every close booking queues behind it.
- `_book_time_exit_trades` advances the realised baseline without `_pnl_lock`
  (`tracker.py:2350`).
- A partially filled stop whose cancel succeeds is not detected (`main.py:590-604`).
- A watched entry sends "order rejected" wording (`main.py:1621`, `2399-2405`).
- Price band implemented three times with small differences (`pretrade.py:43-48`,
  `risk.py:545-562`, `live_caps.py:221`).
- The operator hold file fails open on a read error (`mode_guard.py:182-186`).
- Dynamic position cap is 1 until the first sizing of the day (`risk.py:445-446`, `644`).
- Backtest: time exit at open not flagged `at_open` (`engine.py:408`); notional about 10%
  above live sizing (`costs.py:218`); `ib_extension` `confirm_bars` off by one; `htf_bias`
  uses the wrong close (`indicators.py:114-119`, ema9 only); a session cut short is not
  flagged as a data gap; EVWAP tags can come from a cached symbol.
- Data: the data-quality policy text says auction prints are dropped and they are not
  (`dq_run.py:47-50`); timestamps depend on the host timezone being IST in two core paths;
  `SharedBarSource` grid differs from the clock grid for intervals other than 5 and 15.
- Runner: `bar_interval_minutes` is not cross-checked against the strategy interval;
  `sig_ids` is never pruned; a failed startup warm-up needs an engine restart.
- Ops: task-time comments are stale; `kill_from_pidfile` does not check the command; no
  mid-run log rotation; the heartbeat proves the shell loop, not the engine; no automated
  backup of `data/trades.db`; no dead-man alert for a missed cron run.
- Docs: `ACCOUNTING-INVARIANTS.md` time-exit paragraph and `mode_guard.py:28-29` describe
  behaviour the code no longer has.

---

## 8. Recommended target architecture

No new process, package, framework or storage. The target is the present architecture with
four boundaries made explicit.

**1. One close authority.** Six code paths write CLOSED today. Introduce one internal
function that every path calls. It takes the proof of flatness as an argument (the closing
order's status, or a same-mode book row with quantity zero), refuses without it, and applies
one P&L rule and one risk-counter rule. This removes E-02, E-03, E-04, E-05, E-13, E-20 and
R-01 as a class. It is a consolidation of code that exists, not a new layer.

**2. Supervised tasks.** Every long-lived task (tracker poll, time exit, runner, listener)
gets an iteration guard, a done-callback and a last-progress timestamp. The 09:27 liveness
check and the heartbeat read that timestamp. This removes E-10 and makes "engine alive"
mean "engine working".

**3. Order pipelines that cannot be cancelled from outside.** A dispatch runs as its own
shielded task, and shutdown drains them (E-01). Notification never precedes a state change
(E-11).

**4. A closed research loop.** Sandbox fills feed a per-trade comparison with the backtest's
assumed fills (M-03), which is the evidence gates G7.x need. The runtime checks the
promotion stage at startup (V-03).

Per stage:

| Stage | Retain | Fix | Add | Refactor or simplify | Depends on |
| --- | --- | --- | --- | --- | --- |
| 1 | Historify, published datasets, closed-bar rule, calendar-checked warm-up | D-01, D-02, D-05 | Ex-date guard (D-03); calendar-coverage readiness check | One `sane_bars` predicate for live and backtest | none |
| 2 | Pipeline, `strategy_core`, dedupe store, fail-fast config | S-01, S-03, E-15 | Ladder catch-up (S-02, with approval) | ORB-PY onto a shared core (S-05) | 1 |
| 3 | Both engines, guarded context, costs, ledgers | B-01, B-02, B-03 | Qualification notes for unmodelled live exits (B-05) | Default `time_exit_at_open` (B-04) | 2 |
| 4 | Gate matrix, trial ledger, fail-closed verdict and promotion | V-01, V-02 | G7 evaluators fed by M-03; runtime stage check (V-03) | none | 3, 7 |
| 5 | Atomic reservation, fail-closed gates, mode profiles | R-02, R-03, R-04 | Flatten-all control (known, G8.1) | One price-band helper; one P&L-to-risk rule (R-01) | 6 |
| 6 | Timeout reconcile, uncertain registry, single-flight stops, per-leg time exit | E-01 to E-15, BUG-019 | Partial-fill read (E-17) before live | One close authority; one book read per poll (E-16) | none |
| 7 | Pages-vs-fills, execution health, close-day, bug tracker | E-10, E-14, M-01, M-02, M-05 | Engine liveness; sandbox-versus-backtest table | Gross and net labelling (M-04) | 6 |
| 8 | Supervisor, WAL SQLite, test isolation for `trades.db` | I-01, I-02, I-03, I-04, I-05 | In-process instance lock; `trades.db` backup | Lock the shared-connection stores | none |

**On `services/risk/`.** The engine does not use the platform's shared risk core. That is
acceptable: the core decides stop, target and trailing for a position, and the engine's
`risk.py` decides entry gating. They do not overlap except for one P&L formula. No merge is
recommended. If the engine ever evaluates stops itself instead of resting them at the
broker, it must call the core and not write a second evaluator.

---

## 9. Prioritised implementation roadmap

Each item is one focused change with its own tests. Items marked (A) change trading
behaviour and need the owner's decision before implementation.

**Phase 1. Critical correctness and trading safety**

1. E-01: shield dispatch tasks; drain on shutdown; lengthen the supervisor's grace (I-05).
2. E-10: guard and supervise the tracker poll loop.
3. E-11: register before notify; bound notification waits.
4. E-02 (A: decide the "absent row" rule against the pinned test), E-03, E-05, E-04: no
   close without positive proof of flatness.
5. E-07: 5xx and transport errors go through the timeout reconcile path.
6. I-01: stop tests writing the live `risk.db`. I-02: the 14:55 failsafe reports failure.
7. I-03: no start with an unknown mode.
8. D-01: add the 2027 calendar and the coverage check. This has a fixed date.

**Phase 2. Lifecycle, concurrency and recovery**

1. E-06: time exit serialised with in-flight exits.
2. E-12, E-13, R-03: restart restores entry time and trail state; per-leg reconciliation.
3. E-14, E-08: stop liveness and per-leg stop-fill detection.
4. BUG-019 with E-09 (A): one decision for exits, entries and stops under long stalls.
5. R-01 (A), R-02, R-04, E-20: risk-counter consistency.
6. E-15 (A), E-18 (A), E-19, S-06 (A): signal timing rules.
7. S-01, S-03, S-02 (A): runner calendar gate, deadline, ladder catch-up.

**Phase 3. Performance and resource optimisation**

1. E-16: one orderbook read per poll; shared HTTP client for orders.
2. S-04: trail replay off the event loop (measure first).
3. I-08, I-06: SQLite writes with retry and a lock; consider moving them off the loop.
4. D-05: DuckDB lock wait for warm-up.

**Phase 4. Architecture and maintainability**

1. The single close authority (section 8, item 1), done after Phase 1 and 2 have added the
   tests that pin each path.
2. Split `main.py` along its existing seams (entry pipeline, exit pipeline, unresolved-order
   sweep) with no behaviour change.
3. S-05: ORB-PY backtest adapter over `strategy_core.orb`.
4. One price-band helper; one P&L function for reports (M-04); I-09 config checks.
5. Correct the stale docstrings and docs listed in 7.4.

**Phase 5. Integration testing and production readiness**

1. B-01, B-02, B-03, B-04, V-01, V-02, V-04: backtest and validation corrections, each under
   a new version so old runs stay comparable.
2. M-03 then G7 evaluators: the sandbox-versus-backtest evidence loop.
3. V-03: runtime stage check. M-01, M-02, M-05: monitoring corrections.
4. E-17 partial fills and the flatten-all control (G8.1): required before live.
5. D-02, D-03, D-04: live data parity checks.
6. The fault-injection suite in section 10, then the live-readiness review.

---

## 10. Testing and verification recommendations

**Principle.** Each P0 and P1 above gets a test that fails on the current code before the
fix is written. That converts "confirmed code path" into "demonstrated defect" and stops
the fix from being judged by its own author.

| Test | Proves |
| --- | --- |
| Cancel the listener handler while `send_order` is suspended | E-01 |
| Empty positionbook while positions are tracked; two product rows for one symbol | E-02 |
| Exit order accepted, then status `rejected` | E-03 |
| Mode changes between two polls, both directions | E-04 |
| "SL HIT" alert while the broker still holds | E-05 |
| Barrier: signal exit held in its cancel step, then `time_exit_all` | E-06 |
| `_interpret_placeorder` with 500, 502, 504; `RemoteProtocolError` after send | E-07 |
| `check_positions` raises once | E-10 |
| Notifier that sleeps 60 s during an entry | E-11 |
| Restart mid-trade: `entry_time` and trail stop equal the no-restart values | E-12 |
| Restart with two strategies flat on one symbol; a runner closed after a partial | E-13 |
| Full run of the suite leaves the live `risk.db` byte-identical | I-01 |
| `_run_squareoff` with a failing client exits non-zero and alerts | I-02 |
| LIVE engine, two strategies, one losing: pooled unrealised loss is the sum | R-02 |
| Tearsheet Sharpe with idle sessions | B-01 |

**Fault-injection layer (new, small).** Most findings need the same three fakes: a broker
client that can time out, return 5xx, accept-then-reject and return an empty book; a clock;
and a notifier that can block. `pipeline/sim_sink.py` already has a simulated broker. Extend
it; do not build a second one.

**Existing rules that apply.** These changes touch the order lifecycle and risk, so each
needs targeted tests, the full `signal_engine` suite through
`signal_engine/scripts/run_full_tests.sh`, and an independent Opus review. Tests that pin
current behaviour (`test_tracker_close_detection.py:323-332`, `test_time_exit_at_open.py:110`,
`test_backtest_tearsheet.py:269-276`) must not be edited to make a fix pass: the owner
decides the intended behaviour first.

**Evidence still needed from outside the code.**

1. Flattrade logs: does PositionBook ever return `Not_Ok` mid-session for an error (E-02)?
   Does RMS reject a market exit after acceptance (E-03)? Does a 5xx ever follow an accepted
   order (E-07)?
2. Engine logs: how often "Time exit triggered" overlaps an "EXIT:" line (E-06).
3. A shadow-candle recording to compare native 5m, late-fetched 5m and resampled 1m (D-04).
4. A timing probe for the trail replay (S-04) and for a Telegram flood wait (E-11).

---

## 11. Overall production-readiness assessment

| Area | Sandbox trading | Live trading |
| --- | --- | --- |
| Market data | Ready, with D-01 before 2027 | Ready after D-02 and D-03 |
| Strategy and signal | Ready | Ready after S-01 to S-03 |
| Backtest | Ready | Ready; correct B-01 and B-02 before relying on stored metrics |
| Validation | Ready | Not ready: G7 and G8 have no evaluators (known), V-01, V-02 |
| Risk | Ready | Not ready above one position slot (R-02, R-03) |
| Execution | Acceptable with Phase 1 scheduled | **Not ready**: E-01 to E-07, BUG-019, E-17 |
| Monitoring | Ready after the close; thin intraday | Not ready: E-10, E-14 |
| Infrastructure | Ready after I-01 | Ready after I-02, I-03, I-04 |

**Verdict.** The architecture is right and should be kept. The engineering quality of the
timeout, claim and reservation logic is high, and the record of fixed incidents shows a
system that is being hardened in the correct direction. The remaining weakness is
concentrated, not spread out: the engine still removes its own protection on unconfirmed
evidence, and two of its tasks can stop without a signal. Phase 1 addresses both with
small, local changes and no change to any trading rule except where marked (A).

Promotion to live should wait for Phase 1 and Phase 2, the partial-fill and flatten-all
items in Phase 5, and the broker evidence listed in section 10.

**Decisions needed from the owner before implementation**

1. E-02: is "symbol absent from the positionbook" allowed to mean closed? (Recommended: no.)
2. R-01: do the risk counters take a trade's total P&L or each leg's P&L?
3. BUG-019 and E-09: the option for long stalls, applied to exits, entries and stops alike.
4. E-15: are late exit alerts processed or dropped? (Recommended: processed.)
5. E-18, S-02, S-06: entry gate after the time exit; ladder catch-up; staleness before submit.
6. V-03: should the runtime enforce the research stage?
