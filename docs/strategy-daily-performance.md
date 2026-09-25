# Strategy Daily Performance — Plan & Implementation Log

Status tracker for the day-by-day strategy performance dashboard ("bird's eye
view" of how each strategy performs over time, with professional trade
evaluation metrics). Built on top of the existing Strategy P&L page
([strategy-pnl-fork-modification.md](strategy-pnl-fork-modification.md)),
which is real-time-only and keeps no history.

**Read this file before touching this feature again** — especially before an
Opus-driven refactor/cleanup pass. It records *why* each design choice was
made, not just what the code does; re-derive intent from git blame otherwise
and you will likely re-litigate a decision that was already made deliberately.

## Goal

Given: `blueprints/strategy_pnl.py`, `services/strategy_pnl_service.py`,
`database/strategy_book_db.py` already track live realized/unrealized P&L per
strategy (live and analyze mode, separately).

Wanted: a second page showing, per strategy and per day: win rate, profit
factor, expectancy, Sharpe/Sortino/Calmar, max drawdown, Ulcer Index,
recovery factor, streaks, an equity curve, a daily-P&L calendar heatmap, and
a side-by-side comparison across all strategies.

## Design decisions (read before changing the architecture)

1. **Trade-level ledger, not an EOD snapshot job.** The original plan (see
   conversation history) proposed a `strategy_daily_performance` snapshot
   table populated by a scheduled EOD job, mirroring
   `database/sandbox_db.py`'s `SandboxDailyPnL`. Rejected during
   implementation: `strategy_positions.today_realized_pnl` is a *netted*
   per-leg number — it cannot answer win rate, profit factor, expectancy, or
   streaks, which need individual closing-trade outcomes. Instead,
   `database/strategy_book_db.py` gained a new `StrategyClosedTrade` table,
   written **at the moment a fill realizes P&L** (inside
   `_apply_fill_locked`'s closing branch — see that function's comment). Daily
   aggregates are computed on the fly (`GROUP BY trade_date`) from this
   ledger. No scheduler, no snapshot-drift risk, no backfill-job idempotency
   to get right. Trade-off: a query scans the requested date range on every
   request — fine at single-user SQLite scale.

2. **No historical backfill in v1.** `StrategyClosedTrade` only has rows from
   the moment this feature shipped forward — nothing reconstructs trades that
   closed before that. A backfill from `signal_engine/data/trades.db` /
   `database/sandbox_db.SandboxTrades` is possible later but is
   strategy-source-specific (Flow, Python Strategy Host, and webhook-tagged
   orders have no equivalent trade-level log outside `strategy_book_db`
   itself) and was deliberately deferred rather than shipped half-covering
   the strategy universe. **A strategy's day-by-day history starts on the day
   it first traded after this feature deployed.**

3. **₹-denominated metrics, not %-normalized.** Strategies here have no
   isolated allocated capital to divide by (unlike a mutual fund's NAV), so
   Sharpe/Sortino/Calmar are computed on the ₹ daily-realized-P&L series
   directly — this is what retail trading journals (Tradervue, Edgewonk,
   TraderSync) do, not a deviation from practice. They are valid for
   comparing one strategy's risk-adjusted consistency against another's, but
   are **not** directly comparable to a %-based benchmark (e.g. a fund's
   Sharpe). A per-strategy allocated-capital setting to switch to %-normalized
   metrics is a plausible future enhancement, not built here (see Future
   work).

4. **Zero-trade days are included as 0-P&L days** in every metrics
   computation. Excluding them (only counting days with a trade) inflates
   Sharpe artificially — a pitfall specific to intraday/low-frequency
   strategies, confirmed during research for this feature.

5. **Server resolves live vs analyze mode** the same way
   `services/strategy_performance_service.py` does (`get_analyze_mode()`) —
   the client never passes `mode`, keeping this consistent with the existing
   Strategy P&L page's behavior of always showing the *currently active*
   mode.

6. **This extends the existing approved core-fork exception**, it does not
   open a new one. Per CLAUDE.md, "never touch OpenAlgo core" — Strategy P&L
   is the one documented exception (see
   [strategy-pnl-fork-modification.md](strategy-pnl-fork-modification.md)).
   This feature adds one new table + one new service + one new route module +
   one new frontend page, all inside that same already-approved surface
   (`database/strategy_book_db.py`, `blueprints/strategy_pnl.py`,
   `frontend/src/pages/`). Re-confirm this framing still holds before
   merging any future upstream OpenAlgo release.

## Data model

`database/strategy_book_db.py` — new table `strategy_closed_trades`:

| Column | Notes |
|---|---|
| user_id, strategy, symbol, exchange, product, mode | Same identity fields as `StrategyPosition` |
| direction | `LONG` / `SHORT` — the side that was closed |
| closed_quantity, entry_price, exit_price, realized_pnl | The closing fill's economics |
| trade_date | Session date (03:00 IST rollover, matches `StrategyPosition.trade_date`) |
| closed_at | Wall-clock timestamp |

No `upgrade/` migration script needed — this is a brand-new table, and
`Base.metadata.create_all(bind=engine)` (called from
`init_strategy_book_db()`) creates missing tables on any existing install
automatically. (Migration scripts are for column adds/schema reshapes on
tables that already have rows — see CLAUDE.md's migration-script rule and
`upgrade/migrate_sandbox_pnl.py` for that case; a pure new table doesn't hit
the failure mode that rule exists to prevent.)

## API surface

Both under the existing `blueprints/strategy_pnl.py` (session-authed, UI-only,
same rate-limit pattern as `/api/strategy-pnl`):

- `GET /api/strategy-pnl/daily?strategy=<name>&period=30d|90d|ytd|all` — full
  metrics table + daily P&L series + equity curve for one strategy, or the
  whole portfolio if `strategy` is omitted.
- `GET /api/strategy-pnl/compare?period=...` — the same metrics, condensed,
  for every strategy in the current mode side by side.

## Frontend

New page, new route (see App.tsx for the existing `/strategy-pnl` pattern).
Reuses `lightweight-charts` (already a dependency — see
`frontend/src/pages/PnLTracker.tsx` for the baseline-series + drawdown-pane
convention this borrows) rather than adding a new charting library.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | `StrategyClosedTrade` table + `_apply_fill_locked` hook + `get_closed_trades()` query fn + tests | Done — `test/test_strategy_closed_trades.py` (6 tests) |
| 2 | `services/strategy_metrics_service.py` (pure metrics math) + `services/strategy_daily_performance_service.py` (orchestration) + tests | Done — `test/test_strategy_metrics_service.py` (14 known-answer tests), `test/test_strategy_daily_performance_service.py` (7 tests) |
| 3 | `/api/strategy-pnl/daily` and `/api/strategy-pnl/compare` routes + tests | Done — added to `test/test_strategy_pnl_route.py` |
| 4 | Frontend page (`frontend/src/pages/StrategyDailyPerformance.tsx`, route `/strategy-pnl/daily`): metrics cards, equity curve + drawdown chart (`lightweight-charts`, same baseline-series pattern as `PnLTracker.tsx`), a calendar heatmap of daily P&L (custom, no new charting dependency), and a strategy comparison table. Nav entry added to `frontend/src/config/navigation.ts` and `frontend/src/hooks/usePageTitle.ts`. | Done — `npx tsc -b` and `npx biome check` both clean |

All 56 backend tests in the strategy P&L / strategy book test group pass
(`uv run pytest test/test_strategy_closed_trades.py test/test_strategy_metrics_service.py
test/test_strategy_daily_performance_service.py test/test_strategy_pnl_route.py
test/test_strategy_book_mode_separation.py test/test_strategy_book_prune_lock.py
test/test_strategy_performance_service.py test/test_orderbook_tradebook_strategy_tag.py
test/test_strategy_pnl_leg_order.py -q`). fd-audit run and clean — see conversation
history for the full pass (no new engine/session/thread/cache introduced; the
new ledger write reuses `strategy_book_db.py`'s existing scoped session and
commit/rollback path).

**Deployment gotcha hit and fixed during rollout** (read this before adding
any future React page to this app): a new client-side route needs **two**
separate registrations, not one, or it silently 404s for anyone not already
authenticated:

1. `frontend/src/App.tsx` — the React Router route (client-side navigation,
   works fine once JS has loaded).
2. `blueprints/react_app.py` — a matching Flask route that calls
   `serve_react_app()`. Without this, an unauthenticated request to the new
   path (or a hard refresh / direct link) falls through to
   `Error404Tracker` instead of serving the SPA shell — see CLAUDE.md's React
   routing note. Added `react_strategy_daily_performance()` alongside the
   existing `react_strategy_pnl()`.

Also: `frontend/dist/` is a **built artifact** — editing `frontend/src/` alone
does nothing for a server not running the Vite dev server. This deployment
runs `uv run app.py` (serves `dist/` directly, not `vite dev`), so
`cd frontend && npm run build` had to run before the new page existed for
real users, and the backend process had to be restarted to pick up the new
Flask route in `react_app.py` (`app.py` still uses Werkzeug's dev server, not
an auto-reloading one, for this deployment — see `ps` history in this
feature's implementation conversation).

**Verified after restart** (2026-09-25): `/api/strategy-pnl/daily` and
`/api/strategy-pnl/compare` return 302 (session-auth redirect, correct for an
unauthenticated `curl`) instead of 404; `/strategy-pnl/daily` returns 200 and
serves the SPA shell; `dist/assets/StrategyDailyPerformance-*.js` exists in
the rebuilt `dist/`. Not verified: the rendered page's actual content in a
logged-in browser session (no test credentials available from this
environment) — do that manually once convenient.

## Future work (not built here — deliberately out of scope for v1)

- Historical backfill from `signal_engine/data/trades.db` / `SandboxTrades`.
- Per-strategy allocated-capital config for %-normalized Sharpe/Sortino/Calmar/CAGR.
- Rolling-window metrics (trailing 20/50 trades) for trend detection.
- Trade-level drill-down UI (currently day-level only).
