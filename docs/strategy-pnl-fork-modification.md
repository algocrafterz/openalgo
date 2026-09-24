# Strategy P&L — Fork Modification (Core OpenAlgo Changes)

**Status:** Implemented and deployed to production. 2026-09-24 to 2026-09-25.

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
