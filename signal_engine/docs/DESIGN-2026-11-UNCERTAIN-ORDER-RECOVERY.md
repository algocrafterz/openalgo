# Design proposal: recovery from stalled or unanswered broker requests

Covers BUG-019 (open) and audit findings E-09 and E-07
(`AUDIT-2026-11-10-ARCHITECTURE.md`). Status: **PROPOSAL, not implemented**. Nothing here is
built until the owner chooses the options in section 6.

Source: a read-only Opus analysis at HEAD `251ea1eaf`. Line numbers are from that commit and
must be re-read before implementation. Statements about Flattrade behaviour that code cannot
prove are marked "not verified".

## 1. Owner's principle

Reconciliation before resubmission. A request with no definite answer is never resent blindly.
Before any retry the engine establishes whether the original request was accepted, reconciles
orders and position, checks whether a protective or exit order is still active, and prevents
two active orders for one intent. The position stays tracked and protected while the outcome
is unknown. The existing uncertain-order code is reused. No new order-management framework.

## 2. Two facts that decide the design

**Fact 1. No moment exists at which a clean order book proves the request will never land.**

- Sandbox: the order row is written only after pricing. A MARKET order makes up to 3 quote
  calls on a client with a 120 s timeout, plus retries and rate-limit sleeps
  (`sandbox/order_manager.py:337-390`, `734-763`; `utils/httpx_client.py:23`). The handler keeps
  running after the engine's own 20 s timeout. The row then appears with the time it was
  written, not the time it was sent.
- Live Flattrade: the request waits behind a quote fetch
  (`broker/flattrade/mapping/transform_data.py:41-51`) and a rate limiter, then makes a 120 s
  POST (`broker/flattrade/api/order_api.py:179-220`). A response that is lost after the broker
  accepted the order becomes an HTTP 500 from OpenAlgo
  (`services/place_order_service.py:241-259`).
- Only two answers prove that this request did not land: HTTP 429 (refused before sending) and
  the sandbox's HTTP 409 "position busy" (`sandbox/position_locks.py:139-144`). A 409 also
  proves that another order for the same position is still in flight.

**Fact 2. There is no idempotency handle.** The place-order payload has no client order id or
remarks field (`signal_engine/executor.py:234-249`, `restx_api/schemas.py:22-47`). A late order
is matched by symbol, side, quantity and a time stamp at or after the send
(`signal_engine/order_reconcile.py:215-292`). That separates the request from older orders. It
cannot separate the request from its own resend. On the live order book a copy whose response
was lost carries no strategy tag at all (`services/orderbook_service.py:166-171`).

**Consequence.** During a stall a resend cannot land before the original: both wait behind the
same quote call or position lock. A resend therefore adds only duplicates. It never adds
earlier protection.

## 3. Behaviour today, and the transitions that are defects

Timers today: order timeout 20 s; reconcile 3 reads 2 s apart; late-fill watch 120 s for
entries and exits; late-duplicate watch 600 s; sweeper every 30 s; stop reconcile 3 reads 3 s
apart; stop resend grace 25 s; at most 2 unknown stop sends per cycle; stop re-cycle every 30 s
(6 times) then every 300 s. The longest stall recorded is 354 s (BUG-007, 2026-11-04).

| Intent | Transition today | Why it is a defect |
| --- | --- | --- |
| Exit | Book clean for 120 s after a timeout: the exit is resent (`main.py:715`) | BUG-019: the first copy lands later; one exit filled three times |
| Exit | Held exit, next exit signal after the window, clean read: "exiting afresh" (`main.py:964-971`) | A resend by another route |
| Exit | Sweeper finds the book clean after 120 s and the position open: the stop is restored and the hold is dropped (`main.py:2238`, `2266`, `2354-2377`) | If the first exit lands later nothing watches it: a flat position with a live stop |
| Entry | Book clean for 120 s: the hold and the risk slot are released (`main.py:2144`, `2243`) | E-09: the order lands later as a position with no stop and no tracking |
| Stop | Timeout, 9 s clean scan, second send in the same call (`executor.py:448-457`); another send each cycle once 25 s have passed (`executor.py:386`) | Stops stack: about two per 30 s over a long stall |
| Stop | After a full exit a single late-landing stop is not caught; the audit looks only for twins (`executor.py:562-567`) | A stop on a flat position opens a new position when it triggers |
| All | HTTP 5xx or a transport error after sending counts as a definite failure and is resent with no reconcile (`executor.py:229-231`, `259-264`; `main.py:693-717`, `1612`) | E-07. The classification part is in Phase 1 (routes these into the timeout path) |

## 4. Proposed states (one table for entry, exit and stop)

An intent is `(strategy, symbol, kind, action)` with kind ENTRY, EXIT or STOP.

| State | Entered when | Proof required | Protection while in this state | May a new order for this intent be sent |
| --- | --- | --- | --- | --- |
| SENT | The HTTP call is in flight | none | unchanged | no |
| ACCEPTED | A response with an order id | the response | normal flow | not applicable |
| REFUSED | HTTP 4xx, or an error status in a 2xx body | the response | unchanged | yes, unless another request for the intent is UNCERTAIN. A 409 on an exit or stop is never retried: it proves another order is in flight |
| UNCERTAIN | Timeout, HTTP 5xx, or a transport error after the connection was made | none available | entry: slot and symbol held. Exit: position tracked, see options. Stop: position tracked, missing-stop alert | **no** |
| FOUND | A definite copy is read on the order book | the order row; for an exit also the position | entry: adopted and its stop placed. Exit: booked, extra copies flattened on proof. Stop: adopted, twins cancelled | not applicable |
| CLEAN_UNPROVEN | The book reads clean (today's "ABSENT") | a clean book is **not** proof | same as UNCERTAIN | no |
| RELEASED | Position proof, end of session, or the quiet period (if the owner allows one) | see section 5 | see section 5 | only as section 5 allows |

While an intent is UNCERTAIN or CLEAN_UNPROVEN an alert repeats at the existing interval
(`_UNRESOLVED_ALERT_SECONDS`).

What always blocks a new send: an UNCERTAIN or CLEAN_UNPROVEN request for the same intent; a
working copy on the book; ambiguous copies; an unreadable order book or position book; a 409.

## 5. Recovery per intent

**Entry.** Never resent (as today). The slot and the symbol stay held until FOUND, or release.
A fill that appears at any later time in the session is adopted and given a stop by the
existing `_adopt_untracked_entry`. Change from today: the watch does not end at 120 s.

**Exit.** Not resent while UNCERTAIN. The position is protected instead:

1. The engine reads the position. If it proves the full position is still open, the stop is
   put back through the existing `_restore_stop_after_absent_exit` and `_retry_missing_sl`.
2. The original exit stays watched for the rest of the session (the existing
   `LateDuplicateWatch`, extended from 600 s).
3. If the original exit lands: the restored stop is cancelled, the close is booked under the
   exit's own id and fill, and `pos.sl_order_id` is not overwritten while a restored stop is
   live (`main.py:2251-2254` does that today).
4. Position flat: released with no stop (exists). Position reversed, partial or unreadable:
   held and alerted (exists).

**Stop.** At most one live stop per position, by three rules: one submission at a time per key
(exists, `executor.py:355`); at most **one** unknown send per episode instead of two per cycle,
and no further send until FOUND or release; the key stays in the duplicate-cleanup registry for
the whole episode so a late twin is cancelled (exists). New: after a full exit the stop's key
stays watched, so a single late stop on a flat position is cancelled.

**New pieces, all small.** One constant for the quiet period, replacing the 120 s window and
the 25 s grace. Removal of the in-call exit resend and of "exiting afresh". The late-duplicate
watch runs to session end and also after a stop restore. The stop cap becomes one per episode.
A watch for a lone stop after a full exit. No new registry and no new module.

## 6. Decisions for the owner

**Decision 1. What happens to an exit whose outcome is unknown.**

| Option | Behaviour | Consequence |
| --- | --- | --- |
| A (recommended) | Never resend. Restore the stop once the position is proven open. Cancel the stop and book the close when the exit is seen | A duplicate exit becomes impossible. The position is unprotected only until the position read succeeds. Remaining risk: the original exit lands and price reaches the restored stop before the next 30 s sweep; the stop then opens a reversed position, which the existing duplicate handling flattens on proof |
| B | Resend only when the position is proven open, no copy is on the book, and the book has stayed clean for N seconds beyond the longest stall seen (for example 420 s) | The position has no stop for N seconds. A later copy can still duplicate |
| C | Never resend and never re-protect until proof; alert only | No duplicate is possible. An open loss runs until a person acts |

**Decision 2. Quiet period.** A value (for example 420 s, above the 354 s stall seen), or no
time-based release at all. Any value is a heuristic: the code gives no upper bound on how late
a request can land.

**Decision 3. Entry slot.** Hold the slot and the symbol for the rest of the session while an
entry is unresolved, or release after the quiet period and accept that a late fill can exceed
the position cap by one.

## 7. Tests

New tests that would pin the behaviour:

- An exit stalled 354 s with a clean book: exactly one order is sent; the stop is restored only
  after position proof; when the late exit lands the stop is cancelled and the close is booked
  under the exit id.
- An entry that fills at 300 s is adopted with a stop; slot handling per decision 3.
- A stop over a 354 s stall: one send per episode; at most one live stop at the end.
- A lone stop landing after a full exit is cancelled.
- `pos.sl_order_id` is not overwritten while a restored stop is live.
- A 409 on an exit or stop is not retried while the intent is UNCERTAIN.

Existing tests that pin today's behaviour and would have to change. **Each needs the owner's
approval before it is edited.**

- `test_exit_timeout_safety.py`: `test_book_clean_for_the_whole_watch_then_resends_once`;
  `test_connection_back_and_no_order_releases_the_hold_and_exits_normally`;
  `test_non_timeout_failure_retries_unchanged`; the sweeper tests
  `test_exit_absent_and_stop_not_working_restores_protection` and
  `test_exit_absent_and_stop_restored_drops_the_exit_claim`;
  `test_order_confirmed_absent_releases_the_hold_and_allows_entry`;
  `test_entry_not_on_the_broker_just_releases_the_hold`.
- `test_late_entry_recovery.py`: the three tests that release the hold or slot when the window
  ends with a clean book.
- `test_stop_pending_duplicates.py`: `test_unknown_outcomes_are_capped_per_cycle`,
  `test_the_cap_is_a_tunable_policy_value`,
  `test_after_the_grace_window_the_next_cycle_reconciles_then_sends`.
- `test_executor_stop_reconcile.py::test_next_call_sends_when_the_book_proves_nothing_landed`.
- `test_tracker_stop_recovery.py::test_no_stop_found_leads_to_a_slow_cycle_send_and_an_alert`.

## 8. Risks and what is not established

- **Option A on live depends on Flattrade accepting a SELL stop while a SELL exit may still be
  pending.** `main.py:575` says brokers treat a second SELL as a new short. Not verified. This
  must be answered from Flattrade before option A is relied on in live mode.
- Whether Flattrade's PlaceOrder accepts a client tag (Noren `remarks`) that the order book
  echoes. Not verified. If it does, a small core change would give certain matching and would
  remove most of the heuristics above. That is a core change and needs its own approval.
- No server-side deadline was found that ends a stalled `/api/v1/placeorder` handler. If one
  exists it gives a provable upper bound.
- The uncertain registries are in memory (`order_reconcile.py:68`, `executor.py:59`). A restart
  during an unresolved request loses them. Startup recovery was not read for this proposal.
- Session-long holds take risk slots and symbols out of use for the day.
- The root cause of the stalls (BUG-007, the quote call inside order placement) is separate.
  Removing the stall reduces how often this design is exercised; it does not replace it.
