# Strategy P&L — Fork Modification (Core OpenAlgo Changes)

**Status:** Implemented and deployed to production. 2026-09-24 to 2026-09-25.
Verified live against a trading session on 2026-09-25 — this page's Realized
P&L figure checked out against an independent trade-ledger recomputation; two
open OpenAlgo core bugs were found in the process (not in this feature's own
code) — see "Known OpenAlgo core P&L bugs" below.

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

Three phases, built on a per-strategy position/P&L tracker
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

## Known OpenAlgo core P&L bugs (found 2026-09-25, still open)

Found while verifying this page's numbers against a live trading session. Both
are in OpenAlgo core (sandbox engine), not in this fork's `signal_engine/` or
in the Strategy P&L files above — out of scope to fix here, but worth
checking against the fixed-issues list on any future upstream sync, and worth
reporting upstream if not already known.

### Bug 1 — sandbox position silently zeroed without a covering trade

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
3. Run `cd upgrade && uv run migrate_strategy_book_mode.py --status` after
   any upgrade — if it reports "Migration needed", something reset the
   schema (e.g. a fresh install) and the migration needs to run again. It is
   idempotent and safe to re-run.
4. Run `uv run pytest test/test_strategy_tag_enrichment.py
   test/test_strategy_performance_service.py test/test_strategy_pnl_route.py
   test/test_orderbook_tradebook_strategy_tag.py
   test/test_strategy_book_prune_lock.py test/test_migrate_strategy_book_mode.py
   test/test_strategy_book_mode_separation.py -v` after any merge touching
   these files.
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
