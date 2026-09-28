# Strategy P&L — Fork Modification (Core OpenAlgo Changes)

**Status:** Implemented and deployed to production. 2026-09-24 to 2026-09-25.
Verified live against a trading session on 2026-09-25 — this page's Realized
P&L figure checked out against an independent trade-ledger recomputation; two
open OpenAlgo core bugs were found in the process (not in this feature's own
code) — see "Known OpenAlgo core P&L bugs" below.

**Follow-up, 2026-09-28:** root-caused and fixed Bug 1 below (it was a whole
class of bug, not a one-off — see "Square-off/settlement reconciliation gap"),
added a Strategy column to the Positions page, and did a full merge-conflict
risk assessment against `upstream/main` ahead of the next OpenAlgo version
bump. See the three new sections below. Deployed: `frontend/dist/` rebuilt
and verified the Positions chunk contains the new column code, served from
the correct path (`blueprints/react_app.py`'s `FRONTEND_DIST`) — a build
that lands while a browser tab is already open needs a hard reload of that
tab to pick it up, since client-side page navigation in this SPA never
re-fetches the JS bundle.

This is a **local fork modification to OpenAlgo core**, not a `signal_engine/`-only
change. It exists because `signal_engine/` (this repo's custom trading pipeline)
had no way to see, in the OpenAlgo GUI, which strategy placed a given order or
whether it was paper (Analyze mode) or live — visibility that previously
required checking EOD reports or Telegram. See `signal_engine/PRD.md` and this
repo's other `signal_engine/` docs for the pipeline itself; this file covers
only the core-side surface it now relies on.

**Read this file before merging or rebasing onto a newer `upstream/main`.** It
exists specifically so this change is not silently lost or half-broken by a
future OpenAlgo upgrade.

## What this is

Four phases, built on a per-strategy position/P&L tracker
(`database/strategy_book_db.py`) that OpenAlgo's own maintainers already
built upstream in July 2026 for the Flow no-code builder's `StrategyPnlNode`
— this work extends that existing foundation rather than inventing a new one.

1. **Strategy column on Order Book / Trade Book** — every order/trade row now
   shows the `strategy` tag it was placed with (already present for sandbox
   orders; enriched for live orders by looking up the tag recorded at
   `order.placed`), plus a filter dropdown.
2. **Strategy P&L page** (`/strategy-pnl`) — a new, session-authed, UI-only
   page showing per-strategy open quantity, average price, realized P&L,
   today's realized P&L, and unrealized P&L (marked to the live position
   book). Live during market hours: realized P&L and quantity update
   immediately on a fill (Socket.IO), unrealized P&L polls every 30s.
3. **Live/paper mode separation in the strategy book** — a schema migration
   so a paper position and a live position of the same
   `(strategy, symbol, exchange, product)` are tracked as separate rows
   instead of silently merging into one blended figure.
4. **Strategy column on the Positions page** (added 2026-09-28) — same
   visibility as phase 1, extended to `/positions`. Unlike Order Book/Trade
   Book, a position has no `orderid` to look up and OpenAlgo's own
   `SandboxPositions`/broker positionbook carry no strategy column at all
   (by design — the broker only knows the *net* quantity per symbol, and
   more than one strategy can share it, confirmed for JIOFIN, HDFCBANK, TCS
   and others during the 2026-09-28 audit below). So this column is resolved
   differently: matched against every currently-open leg in
   `database/strategy_book_db.py` by `(symbol, exchange, product)`, joining
   every strategy with a nonzero quantity there — one name normally, several
   comma-separated when genuinely shared, blank when untagged.

## Files touched (core, not `signal_engine/`)

Watch these specifically during a future `git merge upstream/main` /
`git rebase` — a conflict here is a normal, resolvable git conflict, not data
corruption, but it needs a human to reconcile rather than a blind
`git checkout --theirs`.

**Backend:**
- `database/strategy_book_db.py` — added `get_strategies_for_orderids()`
  (bulk lookup for the orderbook/tradebook column), added `mode` to
  `StrategyOrderTag` and to `StrategyPosition`'s unique constraint, made
  `record_order_tag`/`_apply_fill_locked`/`get_strategy_legs` mode-aware.
  Also fixed a pre-existing bug here: `_prune_old_tags()` was skipping
  `commit()` on the common 0-row path, holding SQLite's write lock
  indefinitely in a process that never restarts.
- `subscribers/strategy_book_subscriber.py` — passes `event.mode` through to
  `record_order_tag`.
- `services/orderbook_service.py`, `services/tradebook_service.py` — enrich
  live rows with their strategy tag (sandbox already had it).
- `services/strategy_tag_enrichment.py` (new) — the enrichment helper.
- `services/strategy_performance_service.py` (new) — adapts the existing
  `services/strategy_pnl_service.pnl_from_book()` (untouched) for this page,
  filtered to the current live/analyze mode.
- `blueprints/strategy_pnl.py` (new) — `GET /api/strategy-pnl`, session-authed,
  rate-limited, **not** exposed on `/api/v1`.
- `blueprints/react_app.py`, `app.py` — route/blueprint registration.
- `upgrade/migrate_strategy_book_mode.py` (new), `upgrade/migrate_all.py` —
  migration 012. Idempotent, `--status` support, rebuilds `strategy_positions`
  (SQLite can't alter a UNIQUE constraint in place) following the existing
  `upgrade/migrate_sandbox_trigger_pending.py` pattern. Already applied to the
  live database on 2026-09-24 23:57 IST — pre-migration backup kept at
  `db/openalgo.db.pre-strategy-mode-migration-20260924-235551.bak`.
- `database/strategy_book_db.py` (2026-09-28) — added
  `close_all_legs_for_position(user_id, symbol, exchange, product, mode,
  exit_price, book_today_pnl)`, a force-close helper for legs that went flat
  outside the normal order-tag/`apply_fill` path. See "Square-off/settlement
  reconciliation gap" below.
- `sandbox/position_manager.py` (2026-09-28) — `close_position()` no longer
  tags its reversing order `"AUTO_SQUARE_OFF"` (that tag never reached
  `apply_fill` anyway — see below); it now calls
  `close_all_legs_for_position()` directly with the real fill price.
  `_settle_expired_position()` and `cleanup_expired_contracts()` do the same
  at the settlement price. Deleted `process_session_settlement()` (dead code,
  no caller anywhere, and would have raised `AttributeError` on
  `position.realized_pnl` — a column that does not exist — if ever wired up).
- `sandbox/catch_up_processor.py` (2026-09-28) —
  `catch_up_mis_squareoff()` now also calls `close_all_legs_for_position()`
  (`book_today_pnl=False`, matching its own funds accounting) after settling
  a stale MIS position, for the same reason.
- `services/strategy_performance_service.py` (2026-09-28) — also fetches
  holdings (`services/holdings_service.get_holdings()`) and merges their LTP
  into the position-book lookup `pnl_from_book()` uses to mark unrealized
  P&L. Needed because T+1 settlement moves a CNC leg out of the position
  book into holdings entirely (`sandbox/holdings_manager.py`'s
  `process_t1_settlement()`) while the leg is still genuinely open in the
  strategy book — **deliberately not** treated as a close (see below).
- `services/strategy_tag_enrichment.py` (2026-09-28) — added
  `attach_strategy_to_positions()`, the position-book equivalent of
  `attach_strategy()` (see phase 4 above for why it works differently).
- `services/positionbook_service.py` (2026-09-28) — calls
  `attach_strategy_to_positions()` on both the sandbox and live-broker
  branches of `get_positionbook_with_auth()` before returning.
- `frontend/src/pages/StrategyPnL.tsx` (2026-09-28) — the "P&L" page now
  shows only today's figures (`today_realized`, `unrealized`, `today_total`);
  the all-time cumulative `realized`/`total` fields it used to display are
  gone from this page (they belong on `/strategy-pnl/daily` "Performance",
  which already did this correctly). No backend field was removed — this was
  a display-only fix for the page showing past-day data it was never meant to.
- `signal_engine/main.py` (2026-09-28, not core but related) —
  `_send_exit_with_retries()` gained a duplicate-fill recovery check
  (`_find_recent_market_exit_fill()`) before retrying a timed-out MARKET
  exit, mirroring the guard `signal_engine/executor.py`'s `place_sl_order()`
  already has after a confirmed production incident (2026-09-24) where
  "failed" SL retries had actually succeeded on the broker.

**Frontend:**
- `frontend/src/types/trading.ts`, `frontend/src/pages/OrderBook.tsx`,
  `frontend/src/pages/TradeBook.tsx` — strategy column + filter.
- `frontend/src/pages/StrategyPnL.tsx`, `frontend/src/api/strategyPnl.ts` (new)
  — the P&L page.
- `frontend/src/App.tsx`, `frontend/src/config/navigation.ts`,
  `frontend/src/hooks/usePageTitle.ts` — route/nav registration.
- `frontend/e2e/strategy-pnl.spec.ts` (new).

**Explicitly NOT touched** (a different, unrelated feature with an easily
confused name): `database/strategy_db.py`, `blueprints/strategy.py`,
`frontend/src/pages/StrategyPortfolio.tsx` — TradingView/Chartink webhook
automation config, nothing to do with the free-text `strategy` tag on
`/api/v1/placeorder`.

## Deliberate design decisions

- **No legacy/pre-migration data shown.** Rows recorded before the mode-
  separation migration (`mode='unknown'`) are excluded entirely from
  `/strategy-pnl`, not shown as a separate "legacy" group as originally
  built. Decision made 2026-09-25: this data predates several known
  accounting bugs already fixed in this fork (see `signal_engine/PRD.md`'s
  P&L reliability history — the sandbox today_realized_pnl undercount and
  the EOD partial-exit fill/ledger-date bug), so it is untrusted rather than
  merely unattributed. The rows still exist in the database (nothing was
  deleted); only the page's query excludes `mode='unknown'`.
- **`/api/v1/orderbook` and `/api/v1/tradebook` gained an additive `strategy`
  field** in their JSON response. Treated as non-breaking (existing API
  consumers ignore unknown fields) but is a public API contract change if
  anyone downstream does strict schema validation.
- **No per-row Live/Paper badge** on Order Book/Trade Book — the app's
  existing global Navbar mode badge already covers this, so a duplicate
  would be redundant.

## Known OpenAlgo core P&L bugs (found 2026-09-25; Bug 1 resolved 2026-09-28)

Found while verifying this page's numbers against a live trading session. Both
are in OpenAlgo core (sandbox engine), not in this fork's `signal_engine/` or
in the Strategy P&L files above — out of scope to fix here, but worth
checking against the fixed-issues list on any future upstream sync, and worth
reporting upstream if not already known.

### Bug 1 — sandbox position silently zeroed without a covering trade (RESOLVED 2026-09-28)

Around 12:19:41-42 IST, two open sandbox positions (BHARTIARTL/BREAKOUT short
168 @ 1797.1; INFY/BREAKINGTRADE long 90 @ 1000.0) had their resting SL-M
order **cancelled** (not filled — `sandbox_orders.filled_quantity=0`,
`order_status='cancelled'`, order ids `26092504800203` and `26092507423916`).
Immediately after, OpenAlgo's live `/api/v1/positionbook` and
`sandbox_positions.quantity` began reporting both as flat (0) — but
`sandbox_trades` (the append-only execution ledger) has no covering BUY/SELL
for either. As of 14:00 IST the same day, ~1h40m later, nothing had
self-corrected: both are still reported flat everywhere except
`strategy_positions` (this feature's table), which still correctly shows them
open because it only updates on a real fill event and none occurred.

Net effect: two real open positions became invisible to the OpenAlgo UI and
to `signal_engine`'s tracker (which polls the same, now-wrong, positionbook
and concluded they had closed - it cancelled the stale SL and logged an EXIT,
in good faith, off a corrupted read). They are now unmonitored and
unprotected for the rest of that session. Coincided with a `signal_engine`-
side positionbook `403` outage (12:18:07-12:19:13 IST, see `tracker.py`'s
`_note_positionbook_failure`) — plausibly the same root incident, but the
zeroing is a core OpenAlgo behavior, not something `signal_engine` triggered
or can correct from outside.

Repro signature: cross-check `sandbox_trades` (grouped by symbol+strategy,
net quantity) against `sandbox_positions.quantity` and the live
`/api/v1/positionbook` response for the same symbol - a nonzero ledger net
with a zero reported position is this bug.

**Root cause found 2026-09-28, three days later, while investigating a user
report of the exact same two positions still showing as open on the P&L
page.** `sandbox/position_manager.py:close_position()` (used by both the
3:15 PM MIS auto-square-off and the manual "close position" action) placed
its reversing order tagged `strategy="AUTO_SQUARE_OFF"`. That tag never
reached `database/strategy_book_db.py` at all: `close_position()` calls
`sandbox.order_manager.OrderManager.place_order()` directly, which is a
lower-level entry point than the generic `/api/v1/placeorder` service layer
that normally publishes the `order.placed` event `record_order_tag()`
listens for — so no `StrategyOrderTag` row was ever created for these
orders, regardless of what string was in `strategy`, and the fill was
silently buffered and pruned. `sandbox/catch_up_processor.py:
catch_up_mis_squareoff()` (the 3 AM stale-MIS-position catch-up) had the
same defect from the other direction: it mutates `SandboxPositions` directly
and never places an order at all, so no event fires for it either.

The "SL-M cancelled, position flattened seconds later" sequence above is
consistent with a manual or scheduled square-off action triggering both (a
close typically cancels the resting SL first, then reverses the position) -
but the exact trigger for *that specific instance* was not reconstructable
three days after the fact; what mattered for the fix was the mechanism, which
is deterministic and fully reproduced.

**Fix:** `close_position()` no longer tags the reversing order at all (a
literal `"AUTO_SQUARE_OFF"` string would have opened a bogus strategy bucket
of its own via the normal path, not helped); it now calls the new
`database/strategy_book_db.py:close_all_legs_for_position()` directly with
the order's actual fill price. `catch_up_mis_squareoff()` does the same.
Audited for the same defect class and found two more live occurrences
(`_settle_expired_position()`, `cleanup_expired_contracts()` — both F&O
expiry settlement, one gated behind viewing the positions page, one on a
1-minute background timer) and fixed identically; one dead-code landmine
(`process_session_settlement()`, unreachable, deleted). See "Files touched"
above for the full list. The two stale legs from this incident were also
manually reconciled in the live database at their actual sandbox settlement
price (recovered from `sandbox_positions.ltp`, which the original settlement
had left unchanged).

### Bug 2 — `sandbox_funds.today_realized_pnl` drifts from the trade ledger

Dashboard's "Realized P&L" card reads `m2mrealized` from `/api/v1/funds`,
which is `sandbox_funds.today_realized_pnl` - an incrementally-updated
counter, not something recomputed from trades. On 2026-09-25 it read
**-3672.51**, while an independent bottom-up FIFO recomputation from
`sandbox_trades` for the same day gave **-2773.19** - a **-899.32** gap that
did not change across a 1h40m window with no new trades. The FIFO figure
exactly matched the sum of `strategy_positions.today_realized_pnl` (also
-2773.19) and a manual sum of the Strategy P&L page's Realized column -
three independent methods agreeing, against one drifted counter.

This is the same class of issue already tracked in `signal_engine/PRD.md`'s
P&L reliability history (the sandbox `today_realized_pnl` undercount noted
2026-09-21) - evidently still present. No partial-exit (>2-leg) closes
occurred on 2026-09-25, so this specific occurrence was not the previously-
identified multi-leg trigger; Bug 1's phantom close may be a second, separate
trigger for the same counter.

**Which number to trust:** Strategy P&L's Realized figure (this page, or
`strategy_positions.today_realized_pnl` summed). It is the only one verified
against the raw execution ledger. Do not trust Dashboard's "Realized P&L" for
reconciliation purposes.

**Positions page "Total P&L" is a different, non-comparable metric** (see
`frontend/src/pages/Positions.tsx`): it sums a per-symbol `pnl` field across
*every* returned row, not just open ones, and that field does not appear to
clear to 0 once a position closes - so it converges to neither the realized
total nor 0, even with zero genuinely-open positions. It answers a different
question ("mark-to-market of the position book as OpenAlgo currently sees
it") and will not match Strategy P&L's realized/total by design, independent
of the two bugs above.

## T+1/holdings pricing gap (found and fixed 2026-09-28)

A CNC swing position surviving T+1 settlement
(`sandbox/holdings_manager.py:process_t1_settlement()`) is moved from
`sandbox_positions` into `sandbox_holdings` — the underlying position is
still genuinely open, nothing was sold. `get_open_positions()` (which feeds
the position-book lookup the Strategy P&L page marks unrealized P&L against)
only ever reads `sandbox_positions`, never holdings — so a swing leg past
T+1 read as "unpriced" with zero unrealized P&L for its entire holding
period on the P&L page, even though it was correctly still shown as open.

**Deliberately not fixed with the same `close_all_legs_for_position()`
pattern used for the square-off bugs above** — that would have booked a
fake realized close on a position nothing had actually sold, which is worse
than the bug it would "fix" for a fork whose whole point is trustworthy
paper-trading data. Confirmed instead that selling a CNC holding later
correctly routes through the normal `order_manager.place_order()` path with
the strategy tag intact (`order_manager.py`'s "CNC SELL of owned shares"
validation checks holdings + positions together), so the eventual sale
self-heals the strategy leg's realized P&L against its original,
untouched cost basis. The only real gap was pricing during the holding
window, fixed by having `services/strategy_performance_service.py` also
fetch holdings and feed their LTP into the same lookup - see "Files touched"
above.

## Merge-conflict risk assessment (2026-09-28)

Checked ahead of the next OpenAlgo version bump
(`project_openalgo_rms_upgrade_eval.md` tracks 2.0.2.1 → 2.0.2.5). Method:
`git log <merge-base>..upstream/main -- <file>` for every file this fork
modifies, then read the actual hunks to see whether upstream touched the
same functions.

**Elevated but currently manageable risk.** Upstream has independently and
substantially reworked the exact files this fork's square-off reconciliation
touches, all within the last ~5 weeks:

- `sandbox/catch_up_processor.py` — upstream commit `5292719f9` ("square off
  MIS by updated_at session boundary", #1801) rewrote
  `catch_up_mis_squareoff()`'s query and removed `get_last_session_boundary()`
  entirely in favor of `sandbox/session_boundary.py`. As read on 2026-09-28,
  this rewrite stops short of the loop body our reconciliation call is
  anchored to (`db_session.commit()` + the "Catch-up: Settled..." log line
  right after it are untouched context), so a 3-way merge should apply
  cleanly - but re-verify this by hand after merging, not just trust this
  note, since upstream could touch that region next. Commit `e49181051`
  ("resolve the T+1 settlement cutoff in the database clock") touches the
  same file but a different function (`catch_up_t1_settlement`) - no overlap.
- `sandbox/position_manager.py` — upstream commit `7403631b0` ("resolve the
  position-book session boundary in the DB clock", #1789) rewrote
  `get_open_positions()`'s session-boundary logic - a different function
  from the three this fork touches (`close_position`,
  `_settle_expired_position`, `cleanup_expired_contracts`). No overlap as of
  2026-09-28, but this file is clearly under active upstream development for
  exactly the class of bug (session-boundary/timezone correctness) this
  fork's own fix is adjacent to - watch it closely on every future sync.
- `database/strategy_book_db.py` — upstream commit `e62d6cc7f` ("cash and
  equity leg parity, and three defects that stranded runs", #1976) touched
  `_prune_old_tags()`/`_prune_pending_fills()` near the top of the file
  (independently fixing the same "commit() skipped on the 0-row path" bug
  this fork already fixed locally - convergent discovery, not a conflict).
  This fork's addition (`close_all_legs_for_position()`) lives near the
  bottom of the file, before `list_strategies()`. No overlap.

**What this means in practice:** a future `git merge upstream/main` will
very likely show *some* conflict noise in these three files, but based on
what upstream has done so far, the actual overlapping lines should be
minimal-to-none - expect the merge tool to auto-resolve most of it, with a
human pass needed only to confirm the auto-resolution kept both sides'
intent. This is a normal, resolvable git conflict, not a sign of impending
data loss - same characterization as the original "Files touched" section
above, now with concrete evidence behind it. If upstream's activity in these
files continues at this pace, revisit "Longer-term option" below sooner
rather than later.

**Files with no conflict risk found:** `services/strategy_performance_service.py`,
`services/strategy_tag_enrichment.py`, `services/positionbook_service.py`,
`frontend/src/pages/StrategyPnL.tsx`, `frontend/src/pages/Positions.tsx` -
either fork-only additions or files upstream has not touched since the
merge-base at all.

## Upgrade safety checklist

When syncing from `upstream/marketcalls/openalgo`:

1. `git diff`/`git merge` will flag conflicts, if any, in the files listed
   above under "Files touched" — resolve by hand, don't auto-accept either
   side blindly.
2. If upstream has independently modified `database/strategy_book_db.py`
   (e.g. for Flow), re-verify after merging that:
   - `StrategyOrderTag` and `StrategyPosition` still have the `mode` column.
   - `get_strategy_legs()` still accepts an optional `mode` kwarg.
   - `_prune_old_tags()` still calls `commit()` unconditionally (the lock
     fix) — this is easy to silently lose in a three-way merge.
   - `close_all_legs_for_position()` is still present and still called from
     `sandbox/position_manager.py`'s `close_position()`,
     `_settle_expired_position()`, `cleanup_expired_contracts()`, and from
     `sandbox/catch_up_processor.py`'s `catch_up_mis_squareoff()` — a merge
     that silently drops one of these four call sites reopens Bug 1 for that
     specific settlement path without any error to notice it by.
   - `close_position()` still does NOT put anything in the reversing order's
     `strategy` field (see "Merge-conflict risk assessment" above for why a
     literal string there is actively wrong, not just unhelpful).
3. Run `cd upgrade && uv run migrate_strategy_book_mode.py --status` after
   any upgrade — if it reports "Migration needed", something reset the
   schema (e.g. a fresh install) and the migration needs to run again. It is
   idempotent and safe to re-run.
4. Run `uv run pytest test/test_strategy_tag_enrichment.py
   test/test_strategy_performance_service.py test/test_strategy_pnl_route.py
   test/test_orderbook_tradebook_strategy_tag.py
   test/test_strategy_book_prune_lock.py test/test_migrate_strategy_book_mode.py
   test/test_strategy_book_mode_separation.py
   test/test_close_all_legs_for_position.py -v` after any merge touching
   these files. `test_close_all_legs_for_position.py` (2026-09-28) covers
   long/short close, the `book_today_pnl` flag, the closed-trade ledger
   write, the no-op-on-no-match case, and the multi-strategy-sharing-one-
   symbol case — a failure here means the merge broke the fix for Bug 1.
   `attach_strategy_to_positions()` still has no dedicated test beyond the
   service-level ones above.
5. If upstream ships its own future migration touching `strategy_positions`
   or `strategy_order_tags`, check it against migration 012 in
   `upgrade/migrate_all.py` for ordering/compatibility before applying both.

## Longer-term option

Since the underlying per-strategy book (`database/strategy_book_db.py`) is
itself an upstream-authored file, this extension is a plausible candidate to
propose back to `marketcalls/openalgo` as a PR — doing so would eliminate the
upgrade-conflict risk in this checklist entirely rather than managing it
indefinitely. Not done as of 2026-09-25; revisit if merge conflicts on these
files become a recurring cost.
