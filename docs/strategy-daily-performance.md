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

2. **Backfill exists, but only for signal_engine-driven strategies.**
   `StrategyClosedTrade` is written forward-only by `_apply_fill_locked()` —
   nothing reconstructs trades automatically. On launch day this meant every
   strategy showed zero data, including trades that closed hours earlier the
   same day, which is a bad first impression for a "day-by-day performance"
   feature. **Corrected after initial ship** (2026-09-25): a one-time,
   manually-run script, `upgrade/backfill_strategy_daily_performance.py`,
   reads `signal_engine/data/trades.db`'s `direction='EXIT'` rows — written
   by `signal_engine/db.py:save_tracker_exit()`, called from
   `signal_engine/tracker.py`, entirely independent of
   `_apply_fill_locked()` — and backfills `strategy_closed_trades` from
   them. Each EXIT row carries an accurate realized P&L in its `context` JSON
   column (`{"pnl": ..., "exit_types": [...]}`), so this is a real
   backfill, not an approximation; direction (LONG/SHORT) is derived from the
   sign of `pnl` relative to `(exit_price - entry_price)`, needing no
   correlation back to the original entry row. Idempotent (safe to re-run;
   dedupes on user_id/strategy/symbol/mode/trade_date/exit_price/
   realized_pnl) and deliberately **not** registered in
   `upgrade/migrate_all.py` — same reasoning as `upgrade/rotate_pepper.py`:
   operator-run, not unattended.

   **This does NOT cover Flow or Python Strategy Host trades** — those close
   through the ordinary `_apply_fill_locked()` path with no separate
   per-trade exit log anywhere else to backfill from, so for them the
   original "starts from deploy day forward" limitation still applies. Two
   earlier claims in this conversation about this data source were wrong and
   are worth recording so the mistake isn't repeated: first, a subagent
   research summary claimed `trades.db` had `exit_price`/`pnl` *columns* —
   false, checked directly via `PRAGMA table_info`, no such columns exist.
   Second, a follow-up claim that therefore *no* per-trade source existed at
   all was also wrong — the data is real, just shaped as a JSON blob inside
   `context` on a `direction='EXIT'` row, findable only by actually reading
   rows rather than trusting the column list.

3. **₹-denominated metrics are the source of truth; %-of-capital is an optional view.**
   Strategies here have no isolated allocated capital to divide by (unlike a
   mutual fund's NAV), so Sharpe/Sortino/Calmar are computed on the ₹
   daily-realized-P&L series directly — this is what retail trading journals
   (Tradervue, Edgewonk, TraderSync) do, not a deviation from practice. Rupee
   figures are valid for comparing one strategy's consistency against
   another's. **Sharpe and Sortino no longer use this rupee series**: see
   "Statistics methodology" below. Calmar still does and is unchanged. The page
   accepts one optional capital per strategy (`capital` query value on
   `/api/strategy-pnl/daily`, rupees, above zero, at most 1,000,000,000,000).
   The recorded trades are taken as sized on Rs 1,00,000 per strategy. When a
   capital is given, the response adds one `capital_view` object: the rupee
   metrics scaled by capital / 1,00,000 (a straight-line what-if, not a
   record), plus return, drawdown and best and worst day as a percent of the
   total capital (capital x number of strategies in the selection;
   `compute_capital_view` in `services/strategy_metrics_service.py`). Without
   a capital the response is unchanged. The comparison table and the CSV
   export stay in rupees and unscaled.

4. **Zero-trade days are included as 0-P&L days** in every metrics
   computation. Excluding them (only counting days with a trade) inflates
   Sharpe artificially — a pitfall specific to intraday/low-frequency
   strategies, confirmed during research for this feature. Sharpe and Sortino
   count trading sessions, not calendar days (see "Statistics methodology").

5. **Server resolves live vs analyze mode** the same way
   `services/strategy_performance_service.py` does (`get_analyze_mode()`) —
   the client never passes `mode`, keeping this consistent with the existing
   Strategy P&L page's behavior of always showing the *currently active*
   mode.

   **Live mode support is architectural, not yet directly observed.** Both
   pages read through the same `mode` column
   (`live`/`analyze`/`unknown`) that `upgrade/migrate_strategy_book_mode.py`
   introduced for the *existing* Strategy P&L page, which has already been in
   production use for live mode. `_apply_fill_locked()` — the hook this
   feature's ledger write was added to — does not branch on mode at all; it
   books whatever `tag.mode` the order was tagged with. So a live-mode
   closing fill should write a `StrategyClosedTrade` row exactly like an
   analyze-mode one does. However, every trade backfilled and reconciled
   during this feature's initial rollout (2026-09-25) was `mode=analyze` —
   the account was not trading live that session — so this is a code-review
   conclusion, not something watched happen end-to-end in live mode yet.
   Confirm on the next live trading session before fully trusting it.

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

- `GET /api/strategy-pnl/daily?strategy=<name>&period=1d|7d|15d|30d|90d|ytd|all|custom` (custom adds `&start=YYYY-MM-DD&end=YYYY-MM-DD`) — full
  metrics table + daily P&L series + equity curve for one strategy, or the
  whole portfolio if `strategy` is omitted. Also returns `est_costs`,
  `net_after_costs`, `expectancy_r`, `total_r`, `r_covered`/`r_total` and
  `by_symbol` (see "2026-09-29 additions"); when the resolved range is a single
  day (1D, or a one-day custom range), a `trades` list of that day's closed
  trades, newest first.
- `GET /api/strategy-pnl/compare?period=...` — the same metrics, condensed,
  for every strategy in the current mode side by side, plus the resolved
  `start_date`/`end_date`. One ledger read per request, grouped by strategy.
  Also returns the combined `portfolio` block, the daily `correlation` matrix
  and per-strategy `contribution` (see "Statistics methodology").
- `GET /api/strategy-pnl/daily/excursions?strategy=&exclude=&period=` — MAE/MFE
  quality for the selection. Separate from `/daily` because it reads one-minute
  bars per trade; above 2000 trades it returns `excursions: null` with a message.
- `GET /api/strategy-pnl/trades/export?period=&start=&end=&strategy=` — the enriched closed trades in range as a CSV file (`strategy-trades-<start>_<end>.csv`).

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

## 2026-09-29 additions

**1D period, 7D default.** `resolve_period("1d")` is today only. The 1D view
drops Sharpe, Sortino, Calmar, drawdown, streaks, the equity curve and the
calendar (all meaningless over one day; the API still returns them, the page
hides them) and shows summary cards plus a table of today's closed trades
instead. The dropdown order is 1D, 7D, 30D, 90D, YTD, All; the page opens on
7D. The strategy comparison table also hides its Sharpe and Max Drawdown
columns on 1D. `VALID_PERIODS` in `services/strategy_daily_performance_service.py`
gates the period, so a running app that predates this change answers
"Invalid period: 1d" until it is restarted.

**Enrichment** (`services/strategy_trade_enrichment.py`, tests in
`test/test_strategy_trade_enrichment.py`). Nothing is invented: an
unknowable value is `None` and the page shows "-" plus how many trades the
figure covers.

- **Expectancy in R / total R.** The strategy book stores no stop, so each
  closed trade is matched to the signal engine's own record
  (`signal_engine/data/trades.db`, read-only, status SUCCESS, LONG/SHORT entry
  rows with a non-zero stop) by strategy, symbol, direction, mode, session
  date and entry price within 0.5 percent. Risk = |signal entry - stop| x
  closed quantity; R = realized P&L / risk. No match, or two matches with
  different (entry, stop), gives no R for that trade. Expectancy in R is the
  mean over covered trades only; `r_covered`/`r_total` is shown next to it.
  If the signal DB is missing (a deployment without signal_engine) R is simply
  unavailable.
- **Estimated charges and net after charges.** Uses the existing
  `portfolio/costs.py` schedules (`india_intraday` for MIS, `india_delivery`
  for CNC; NSE and BSE only) with 2 orders per closed-trade row. Slippage is
  not included (the sandbox fills at the requested price). A trade on an
  instrument the schedules do not model has no cost, and if any trade in the
  range has none, `net_after_costs` is `None` rather than a partial sum.
  Partial exits each count two orders, which slightly overstates flat
  brokerage on multi-exit trades; the 0.03 percent rate leg dominates at
  these sizes so the effect is small. Hand-check: INFY short 45 @ 986 costs
  about 46.86 (brokerage 31.4, STT 11.1, exchange 2.6, stamp 1.3).
- **P&L by symbol.** Net P&L, trade count and win rate per symbol; the page
  shows the top 5 contributors and 5 biggest detractors.

**Live vs Analyze.** Every ledger row (`strategy_closed_trades`,
`strategy_positions`) carries `mode`, and every service reads only the
current mode (`_current_mode()` from the global Analyze toggle); legacy
`mode='unknown'` rows are excluded from both. Both pages now show a
`StrategyModeBadge` (`ANALYZE (paper)` blue / `LIVE (real money)` red) taken
from the `mode` the server returned for the data on screen, not from the
client toggle. The Strategy P&L page's old green "Live" badge (which only
meant auto-refreshing) was renamed "Streaming" so the two cannot be confused.
There is deliberately no combined all-modes view.
`test/test_strategy_performance_mode_isolation.py` books one analyze, one live
and one unknown trade and asserts each mode sees only its own, on both the
daily and comparison endpoints.

## Backfill run log

- **2026-09-25**: `uv run python upgrade/backfill_strategy_daily_performance.py --date 2026-09-25` — backfilled 19/19 signal_engine EXIT rows (BREAKOUT, BREAKINGTRADE, BREAKINGTRADE-WATCHLIST, ORB, all `mode=analyze`). Verified idempotent (re-run: 0 inserted, 19 skipped) and cross-checked the resulting portfolio `net_profit` against the sum of per-strategy rows.
- **2026-09-25 (same day, follow-up fix)**: user reported the new page's Net P&L (-2,529.59) didn't match the existing Strategy P&L page's Total (-2,773.19). Root-caused to a genuine **signal_engine bug**, not a backfill bug: `tracker.py`'s `check_positions()` (~line 1020) computes a closed leg's `pnl_delta` from the broker/sandbox's own reported "realised" figure on the single-close path, rather than from OpenAlgo's actually-booked fill prices — and for BREAKOUT/TCS that figure was wrong (-578.00 vs. the correct -821.60, verified by hand from `StrategyOrderTag.applied_notional / applied_quantity` on the entry and exit orders). This is an existing, deeper signal_engine issue with its own documented history (see the code comments at that line referencing 2026-09-11 and 2026-09-21 incidents) — **not fixed here**, deliberately, since it needs its own focused investigation.

  What *was* fixed here: the backfill script now cross-checks every leg against `strategy_positions.realized_pnl` (verified against actual fills — see `docs/strategy-pnl-fork-modification.md`'s conclusion) and uses the verified figure whenever a leg closed in exactly one trade that day (unambiguous). For a leg that closed in multiple trades the same day, the position-level total can't be safely split between them, so the individual signal_engine figures are kept and a warning is logged instead — the mismatch stays visible, not silently swallowed either way. Re-ran the backfill for 2026-09-25 after this fix: 1 leg (BREAKOUT/TCS) corrected, portfolio net_profit now -2,773.19, matching the P&L page exactly. Verified via the new `--verify` flag.

## Future work (not built here — deliberately out of scope for v1)

- ~~**Investigate `tracker.py`'s `pnl_delta` computation on the single-close path**~~ — **fixed 2026-09-25** in `signal_engine/tracker.py`'s `_book_broker_close()`. In ANALYZE mode, the single-close path now derives `pnl_delta` from price (`ltp` vs. the position's own fill/entry price) instead of trusting the sandbox's "realised" figure, the same way the partial-exit path already did (2026-09-21 fix). LIVE mode is unchanged — a real broker's `ltp` is the last traded market price, not a guaranteed fill price, so its own realised figure remains the trusted source there. Regression tests: `signal_engine/tests/test_close_accounting.py::TestSandboxSingleCloseTrustsPriceNotTheBrokerFigure`. See `signal_engine/docs/PRD.md`'s 2026-09-25 entry for the fix detail.
- **Wire `upgrade/backfill_strategy_daily_performance.py --verify --date <today>` into the daily `eod` workflow** so a strategy_positions vs. strategy_closed_trades mismatch is caught automatically every trading day, rather than only when someone happens to compare two pages by eye — which is how the TCS mismatch above was actually found. Not wired up yet.
- Backfill for Flow / Python Strategy Host trades — no per-trade exit log exists anywhere to source this from today; would need one added at the point those paths close a position, mirroring what `save_tracker_exit()` already does for signal_engine.
- Backfill for dates further back than `signal_engine/data/trades.db` retains (check retention before assuming full history is recoverable).
- Per-strategy allocated-capital config for %-normalized Calmar/CAGR. (An optional capital per strategy for a scaled what-if view is built. Sharpe and Sortino are normalised on assumed capital, see "Statistics methodology".)
- Trade-level drill-down UI (currently day-level only).

## Charges: verification and charge-drag percentages (2026-10-01)

**Verified.** The estimate (`services/strategy_trade_enrichment.py` with `portfolio.costs.india_intraday`) uses the
standard discount-broker intraday schedule: brokerage 0.03% or Rs 20 per order (whichever is lower), STT 0.025% on the
sell side, NSE exchange fee 0.00297% (BSE 0.00375%), SEBI Rs 10 per crore, stamp duty 0.003% on the buy side, GST 18% on
brokerage, exchange and SEBI fees. Rebuilt independently from the sandbox's real fills with one brokerage per executed
order, 2026-10-01 came to Rs 5,574 on 111 strategy orders. The page showed Rs 5,734, 2.9% high.

**Why it ran high, fixed.** A closed-trade row is one exit leg, and the page charged every row a full entry+exit pair, so a
position closed in several legs paid entry brokerage repeatedly. `estimate_trade_costs` now counts each entry order once
(legs share strategy, symbol, mode, date, direction and entry price; only the earliest leg carries the entry order). The
same day now estimates Rs 5,528, within 1% of the order-count figure. Rates are the 2024-25 published schedule: confirm
them against a real contract note from the broker in use before relying on the last rupee. Slippage is excluded.

**What was added.** `summarize_enrichment` / `summarize_costs` return `charges_pct_of_gross` (charges as a share of gross
profit; null when there is no gross profit, above 100 when charges exceed it), `turnover`, `charges_pct_of_turnover` and
`avg_charge_per_trade`. Shown on the Performance page summary cards ("charges Rs X = Y% of gross profit"), as three columns
in the strategy comparison table (Charges, Charges % of profit with amber at 50% and red at 100%, After charges), and in
the nightly `pages_vs_fills` report and EOD digest headline. The 2026-10-01 picture: charges took 80.6% of the day's gross
profit (BREAKOUT 57%, BREAKOUT-PY 90%, BREAKINGTRADE 290%).

## Charges per broker (2026-10-01)

**Why.** Brokerage is the broker's own, so the Performance page prices each trade on the broker that placed it, in live
and paper mode alike. Flattrade's published schedule (https://flattrade.in/charges/, read 2026-10-01) is NIL brokerage on
equity intraday and delivery; STT 0.025% sell side (intraday), exchange fee NSE 0.00307% / BSE 0.00375%, SEBI Rs 10 per
crore, stamp duty 0.003% buy side, GST 18% on exchange fee plus SEBI (and brokerage where there is any), DP Rs 20 per
debit plus GST on a delivery sell. Statutory lines are identical at every broker; only brokerage and DP differ.
`portfolio/costs.py`'s intraday NSE exchange fee was also out of date (0.00297%) and is now the published 0.00307%.

**How.** `portfolio/broker_fees.py` holds one `BrokerFees` per broker (rate, per-order cap, delivery flat, DP charge,
source URL, date read) plus `DEFAULT_FEES`, a typical discount broker (0.03% or Rs 20 per intraday order) flagged
`assumed`. `india_intraday(exchange, broker=)` / `india_delivery(exchange, broker=)` swap only the brokerage rule. A broker
with no entry, or a trade with no recorded broker, is priced on the default and labelled "assumed rates".
**To add a broker**: read its charges page, add a `BrokerFees` entry with `source` and `verified_on`, set `assumed=False`.

**Where the broker comes from.** Stamped on the order when it is placed (`strategy_order_tags.broker`, resolved from the
event's API key via `get_auth_token_broker`), copied to the closing trade (`strategy_closed_trades.broker`; a close with no
order of its own takes the latest order's broker). Run `upgrade/migrate_strategy_book_broker.py` (registered in
`migrate_all.py`): it adds both columns and backfills from `db/latency.db`'s `order_latency` log (by order id, else the
day's dominant broker, else "unknown"). On this install 666 of 667 tags matched by order id and every trade resolved to
Flattrade. A position opened at one broker and closed at another is priced at the closing broker.

**Surfaced.** `by_broker` (isolated per broker: trades, gross, charges, % of profit, after charges, assumed flag) and
`fee_basis_assumed` in the performance API; a "Charges by broker" card and a Broker column on the Performance page; per
broker lines in the nightly `pages_vs_fills` report. Effect on 2026-10-01: estimated charges fell from Rs 5,528 (assumed
Rs 20-per-order brokerage) to Rs 3,372 at Flattrade, 49.1% of gross profit instead of 80.6%; after charges +3,490.
Not modelled: slippage, DP charge when several legs sell the same scrip on one day beyond the first, and exchange-fee
changes after the date read above (re-verify against a contract note).

**If the broker changes (2026-10-01).** There is no broker API in this codebase that returns a brokerage schedule (none of the
38 plugins implements one; Upstox's `charges/margin` is a margin calculator), so a new broker is NOT priced automatically.
What is automatic is the warning: a trade whose broker has no entry in `portfolio/broker_fees.py` is priced on the assumed
default, logged once by name, shown as "[assumed broker rates]" on the Performance cards and in the broker card with the
reason, and flagged in the nightly `pages_vs_fills` report. A verified schedule also asks for a re-check after 180 days.
`uncovered_brokers()` lists the plugin brokers still lacking a verified entry.

## 2026-10-31 additions

**1D is the last traded session, not the calendar day.** On a weekend or
holiday `resolve_period("1d")` returns the most recent `trade_date` (within
`LEDGER_RELIABLE_SINCE`..today) that has a closed trade in the current mode,
across all strategies, so the strategy view and the comparison table name the
same day. With no trades at all it is today. The page shows the resolved date
("Showing 30 Oct 2026 (last traded session)").

**15D and Custom periods.** 15D is a rolling 15 calendar days. Custom takes
`start`/`end`; the server rejects a missing, malformed or reversed range, or
one that starts in the future, with a 400 and a plain message, and clamps the
start to `LEDGER_RELIABLE_SINCE` and the end to today. The page keeps period,
range and strategy in the URL (`?period=custom&start=..&end=..&strategy=..`),
so a refresh or a shared link reopens the same view.

**Last session on the P&L page.** `/api/strategy-pnl` returns `last_session` = {date, strategies: {name: realized}, total, trades_count} from the closed-trade ledger for the current mode, or null. The P&L page shows a "Last session" card when that date is not today (IST) and no strategy has realized P&L today, with a link to the Performance page's 1D view.

**Breakdowns.** The daily response has `by_hour` (bucketed by EXIT hour from `closed_at`; the ledger stores no entry time) and `by_weekday`. The page shows them in a "When trades close" card.

**Net equity curve.** `equity_curve_net` is cumulative daily gross P&L minus that day's estimated charges. A trade with no cost estimate counts as 0. `equity_net_cost_coverage` {covered, total} says how many trades were priced. The chart draws it as a dashed "After est. charges" line.

**MAE/MFE.** Single-day views only. Each trade gets `mfe` and `mae` in rupees times quantity, or null. Entry time comes from the matching signal in `signal_engine/data/trades.db` (same match rule as R). Prices come from Historify 1-minute bars between entry and `closed_at`, using full-bar highs and lows, so the values are an upper estimate. `excursion_covered`/`excursion_total` give coverage. Results for past sessions are cached in-process for 6 hours (`LockedTTLCache`). Today's results are recomputed on each request because bars may still be arriving.

**Heatmap click-through.** Clicking a day in the calendar heatmap opens that day as a one-day custom range.

**CSV export.** `GET /api/strategy-pnl/trades/export?period=&start=&end=&strategy=` returns the enriched closed trades in range as `strategy-trades-<start>_<end>.csv`. Text cells that start with `=`, `+`, `-` or `@` get a leading quote, so a spreadsheet cannot run them as formulas. The page has an "Export CSV" button.

**Strategy list.** `/api/strategy-pnl/compare` returns `all_strategies`: every strategy with a closed trade in the current mode since the reliable ledger date. The dropdown no longer drops a strategy that has no trades in the selected range.

**Entry time (`opened_at`).** `strategy_positions.opened_at` is set when a leg
opens from flat or flips through zero; adding to or reducing a position keeps
it. Every `strategy_closed_trades` row carries the leg's `opened_at` from
before the closing fill, so a partial exit is timed from when the whole
position opened. `apply_fill(..., filled_at=)` takes the fill's own time (the
reconciler passes the order-book timestamp); both `opened_at` and `closed_at`
use that clock. `upgrade/migrate_strategy_opened_at.py` adds the columns and
backfills Analyze-mode rows from `sandbox_trades` (latest opening-side fill
within 0.5% of the entry price, up to 10 days before the close). Live rows
before the column existed stay NULL.

**Holding-time analytics.** The daily response adds `hold_stats` (avg,
median, p90, winners' and losers' average hold, coverage), `by_entry_hour`
and `by_hold_bucket` (<15 min, 15-60 min, 1-3 h, 3 h+ same day, Overnight);
comparison rows add `avg_hold_min` and `median_hold_min`. MAE/MFE use the
trade's own `opened_at` and fall back to the signal match only when it is
missing.

**Edge check.** Per-trade net P&L (realized minus estimated charges, unknown
charges counted as 0) gives `net_expectancy`, `expectancy_ci95`, `t_stat`.
Because trades on one day share that day's market, the same net P&L summed
per trading day gives `day_t_stat` over `edge_days`. `edge_verdict` follows the rules in "Statistics methodology" below, which
replace the original t-stat-only rules. It is a screening statistic, not an
acceptance gate.

**P&L page session view.** `/strategy-pnl` always shows the current session,
or the last traded session when nothing has traded today: the same summary
cards, holding-time row and trade table the Performance page shows for 1D.

**CSV export columns** (INR and minutes, timestamps ISO 8601 with +05:30):
trade_date, opened_at, closed_at, hold_min, entry_hour, weekday, mode,
strategy, broker, symbol, exchange, product, direction, closed_quantity,
entry_price, exit_price, sl_price, realized_pnl, est_cost, net_pnl,
return_pct, r_multiple, entry_slippage, mfe, mae. MFE/MAE are left empty for a
range over 2000 trades.

## Statistics methodology (2026-11-03)

Every formula lives in `services/performance_stats.py` (pure, no I/O). Sections
are assembled in `services/performance_insights_service.py`. Tests:
`test/test_performance_stats.py` and `test/test_performance_insights.py`, each
with an independent reference implementation and a golden test on a real trades
export (read from `OPENALGO_GOLDEN_TRADES_CSV`, skipped when absent, never
committed).

### Gross and net

- **Gross** is `realized_pnl`. **Net** is gross minus estimated charges
  (`net = realized_pnl - est_cost`, charges deducted once; an unknown charge
  counts as 0). Charges are estimates from the fee schedule, never broker-reported.
- Net: Sharpe, Sortino, edge check, net R, net profit factor, net drawdown,
  concentration, rolling and monthly figures, entry-quality tables, grading.
- Gross: the Net P&L (gross) card, the Max Drawdown card, Calmar, the comparison
  table's Profit Factor and Max Drawdown columns, gross R and gross profit
  factor (shown next to their net versions). Labels on the page say which is which.
- Slippage is already inside the fills, so it is reported and never deducted again.

### Daily series, Sharpe and Sortino

- **One observation per NSE trading session** in the range: weekdays that are
  not exchange holidays (`market_calendar_db.is_market_holiday(day, "NSE")`),
  plus any date a trade closed on. Zero-trade sessions are 0, not missing.
  Weekends and holidays are not observations. A failed calendar lookup falls
  back to weekdays.
- **Daily return** = daily net P&L (all selected strategies pooled) / assumed
  allocated capital. **Assumed capital** = the capital per strategy from the
  capital box (default Rs 1,00,000) x the number of selected strategies. It is
  an assumption, not measured deployed capital. The comparison table uses Rs
  1,00,000 per strategy.
- **Sharpe** = (mean(r) - rf/252) / std(r, ddof=1) x sqrt(252). rf defaults to 0.
- **Sortino** = (mean(r) - target) / DD x sqrt(252), with
  DD = sqrt(mean(min(r - target, 0)^2)) taken over ALL sessions, zero days
  included. target defaults to 0 (minimum acceptable daily return).
- **Annualisation** uses 252 sessions a year.
- **Capital invariance:** with a constant capital, rf = 0 and target = 0, both
  ratios do not change with capital (mean and spread scale together). A
  non-zero rf or target breaks that on purpose.
- **Withheld, not estimated:** both are `null` below **60 sessions**, and also
  below **10 sessions under the target** (10 losing sessions at the default
  target of 0). A series with no
  variation (checked with a relative tolerance, so float noise counts as flat)
  also gives `null`, never infinity. `ratio_warning` carries the plain-language
  reason; `ratio_sessions`, `ratio_losing_sessions` and `ratio_capital` carry
  the counts. A non-finite daily result withholds both.
- The earlier Sharpe/Sortino (gross rupee P&L on calendar days, population
  std, downside deviation measured as the spread of losing days around their own
  mean) is gone. Its values were inflated and are not comparable.

### Expectancy and the edge check

- Expectancy is the mean net P&L per trade. `t_stat` = mean / (sample std /
  sqrt(n)). `day_t_stat` is the same on per-day totals (`edge_days`).
- `expectancy_ci90` and `expectancy_ci95` use the Student-t critical value for
  n - 1 degrees of freedom (computed locally, since production has no scipy;
  checked against scipy in tests). They treat every trade as independent.
- `bootstrap_ci90` / `bootstrap_ci95`: whole trading days are resampled with
  replacement, 5,000 draws, fixed seed, drawn in chunks of 250 to bound memory.
  `bootstrap_p_nonpositive` is the share of draws with a mean at or below zero.
- Verdict (`edge_verdict`): `insufficient_sample` (under 30 trades, under 10
  days, or non-finite input), `positive_supported`, `positive_inconclusive`,
  `negative`, `inconclusive`. Positive is supported only when the trade t-stat
  and the day t-stat each pass the two-sided 95% Student-t critical value for
  their own degrees of freedom AND the lower 95% bootstrap bound is above
  zero. Negative needs the mirror conditions. A series with no variation is
  `inconclusive`. An "unstable across windows" verdict is not implemented.
- It is a screening statistic, not proof of an edge.

### R multiples

- `r_multiple` (gross) = realized_pnl / (|signal entry - signal stop| x quantity).
  `net_r_multiple` = (realized_pnl - est_cost) / the same risk. `expectancy_r`
  and `expectancy_net_r` are their means over trades with an R.
- A trade has no R when no signal stop is found. `r_excluded` counts these by
  reason (`no_signal_data`, `no_unique_signal_match`, `zero_stop_distance`) and
  the page shows the counts. R is dimensionless: capital scaling never changes it.

### Data flags

`data_flag` marks rows whose timestamps cannot support timing statistics:
`overnight_mis` (an intraday MIS position opened on one date and closed on
another) and `missing_entry_time`. Flagged rows keep their P&L in every profit,
drawdown, concentration and R figure. They are left out of holding-time,
hold-bucket, entry-hour, first-hour, deployed-capital and excursion statistics,
and the page shows how many.

### Concentration and consistency

Over days with at least one closed trade, on net P&L: profitable-day share,
average winning and losing day, median and mean day, best and worst day, the
share of net profit from the best 1, 3 and 5 days, profit without the best day
and without the best five days, the share from the best 10% of trades, and the
same per strategy (including each strategy's profit without the portfolio's
best day). Shares are `null` unless total net profit is above zero; a share can
exceed 100%. Rolling expectancy (last 20 trades) and rolling profit factor (last
50), by closing order, are shown combined and per strategy. The monthly table
groups by calendar month.

### Drawdown, return on capital and deployment

- Drawdown is on **realized net equity**: the cumulative net P&L at each session
  close, starting from zero, so a loss in the first session is already a
  drawdown. It is not mark-to-market: the unrealised P&L of open positions is
  not in it.
- `max_drawdown_pct` = fall / (reference capital + equity at the peak).
  `recovery_factor` = net profit / |max drawdown|. Duration is in sessions from
  the peak to recovery (or to the end if not recovered).
- A second, per-trade figure (`max_drawdown_trade_sequence`, in closing order)
  shows swings that a session close hides.
- **Calmar** (net, in this block) = net x 252 / (sessions x |max drawdown|),
  `null` below 60 sessions. The Calmar in the older metrics block is unchanged
  (gross rupee series).
- `return_on_capital_pct` = net / (reference capital x strategies selected).
  Percent figures use the reference capital the recorded trades were sized on,
  so they do not change with the capital box; rupee figures do (the page scales
  `capital_base` with them).
- **Deployed capital** is the peak of the entry value of positions open at the
  same time, per day (mean and maximum). Leverage and margin are not netted.

### MAE and MFE

- `mfe` and `mae` are rupees (price move x quantity), from one-minute Historify
  bars between entry and exit. They use full-bar highs and lows, so they are
  **upper bounds**: the market may have touched them where the quantity could
  not have been filled.
- In R: divided by the signal-stop risk in rupees. Trades without a stop do not
  appear in R figures (the page shows how many do).
- `mfe_capture_pct` = winners' realized profit / their MFE. `gave_back_pct` =
  among trades whose MFE reached 0.5R, the share that closed at a net loss.
  `mae_reached_stop_pct` = share whose adverse move reached 1R. Winners and
  losers are compared on average MAE in R.
- Flagged rows are excluded and counted. Above 2000 trades the card says so
  and shows nothing.

### Execution quality

Entry slippage is the entry fill against the signal's alert price, split into
worse and better than the alert, in rupees and basis points, by strategy and by
symbol (largest adverse first). Exit slippage, slippage by order type, broker
reported charges and a Sandbox-versus-live comparison are **not available**:
the data is not recorded. In Sandbox mode fills are simulated, so slippage
there is not a measure of live execution.

### Comparison

Per strategy: gross and net profit factor, net R, return on capital, net
drawdown percent, recovery factor, share of profit from the best day and best 5
days, and a grade. Plus a combined portfolio row, the Pearson correlation of
daily net P&L (`null` under 20 sessions or for a flat series) and, for each
strategy, the portfolio's drawdown with and without it.

### Benchmark grading

Bands live in one place: `BENCHMARK_BANDS` in `services/performance_stats.py`.
They are screening guides, not guarantees.

| Metric | Poor | Acceptable | Good | Excellent |
|---|---|---|---|---|
| Net profit factor | below 1.0 | 1.0 to 1.3 | 1.3 to 2.0 | above 2.0 |
| Win rate | below 40% | 40 to 50% | 50 to 60% | above 60% |
| Expectancy (R, net) | below 0 | 0 to 0.10 | 0.10 to 0.30 | above 0.30 |
| Sharpe | below 0.5 | 0.5 to 1.0 | 1.0 to 2.0 | above 2.0 |
| Sortino | below 1.0 | 1.0 to 1.5 | 1.5 to 3.0 | above 3.0 |
| Recovery factor | below 1 | 1 to 2 | 2 to 3 | above 3 |
| Charges / gross profit | above 30% | 20 to 30% | 10 to 20% | below 10% |

- Return metrics (profit factor, win rate, expectancy R, charges share) need 30
  trades **and** 10 trading sessions. Risk metrics (Sharpe, Sortino, recovery
  factor) need 60 sessions. Below that the row shows the reason, not a grade.
- The overall grade is the **weakest** graded metric, so a few strong numbers
  cannot lift it. Win rate is shown but never sets it.
- The **validated** overall grade is withheld unless profit factor, expectancy
  in R, the charges share and the recovery factor are all graded. A missing
  Sharpe or Sortino does not block it and cannot raise it. The charges share is
  not defined when gross profit is not above zero.
- Until then, if profit factor, expectancy in R and the charges share are graded
  (30 trades and 10 sessions), the page shows a **preliminary** grade: the
  weakest of those three, labelled "Preliminary", and described as not a
  validated assessment and not evidence of a proven edge. `assessment` is
  `validated`, `preliminary` or `none`. A preliminary grade never becomes
  validated by itself: it needs the risk measures (60 sessions).

### Withheld because of insufficient data

| Figure | Needs | Shown instead |
|---|---|---|
| Sharpe, Sortino | 60 sessions and 10 sessions under the target | dash and a warning |
| Calmar | 60 sessions | dash |
| Edge verdict | 30 trades and 10 days | "Insufficient sample" |
| Return grades | 30 trades and 10 sessions | reason text |
| Risk grades | 60 sessions | reason text |
| Correlation | 20 sessions | dash |
| Net R | a signal stop and a known charge | counted in `r_excluded` |
| Concentration shares | total net profit above zero | dash |
| Rolling series | 20 / 50 trades | "not enough trades yet" |
| Validated status | all 15 validation checks, including live fills | Under validation at most (Sandbox), with the missing check listed as not assessed |
| Single-day views | | the trade list instead of the sections above |

### Validation status

One status says how far the observed results can be trusted. It sits under the metric
cards on the Performance page, and a compact badge appears in the comparison table. The
formula is one function, `validation_status` in `services/performance_stats.py`; it reuses
the grading, edge check, concentration, rolling, drawdown and slippage figures already
described above. The same function assesses the combined selection, the portfolio row and
each strategy, so the results are consistent (the status ignores the capital box).

**The five states**

| State | Meaning |
|---|---|
| Not enough data yet | Under 30 trades or under 10 trading sessions. Nothing else is judged. Also shown when a profit figure is unavailable, because missing evidence is never read as a pass. |
| Not encouraging so far | Enough data, but net profit factor is not above 1.0, the average net result per trade is not positive, **or the edge check does not show a positive average** (a negative verdict, or an inconclusive one on a non-positive average). It applies at any history length, including 60 sessions or more with positive profit figures. |
| Preliminary | The profit figures are positive and the edge check has not failed, but there are fewer than 60 sessions, or the edge check cannot judge yet (too few trading days, or not run). |
| Under validation | 60 sessions or more and an edge verdict of at least "positive but inconclusive", with one or more other checks failed, pending or not assessed. |
| Validated | Every check passes. |

A failed robustness or execution check blocks Validated but never lowers the status and is
never hidden. "Negative" and "inconclusive" edge verdicts stay distinct: the check text says
"negative average" for the first and "does not settle whether it is negative or just noise"
for the second, and the summary names which one applies.

**The four dimensions and their checks** (15 checks; `needed_for` is the level each one gates)

| Dimension | Check | Passes when | Gates |
|---|---|---|---|
| Performance | Enough trades and sessions | 30 trades over 10 sessions | Preliminary |
| Performance | Net profit factor above 1.0 | net profit factor above 1.0 | Preliminary |
| Performance | Positive net result per trade | average net P&L per trade above 0 | Preliminary |
| Performance | Net profit factor above 1.3 | net profit factor above 1.3 | Validated |
| Statistical | Edge at least positive but inconclusive | verdict `positive_supported` or `positive_inconclusive` | Under validation |
| Statistical | Edge statistically supported | verdict `positive_supported` | Validated |
| Robustness | 60 trading sessions of history | 60 sessions | Under validation |
| Robustness | No single day carries the profit | best day under 40% of net profit | Validated |
| Robustness | Still profitable without the best day | net P&L without it above 0 | Validated |
| Robustness | Still profitable without the best five days | net P&L without them above 0 (needs more than five traded days) | Validated |
| Robustness | Last 20 trades still positive | average of the last 20 trades above 0 | Validated |
| Robustness | Largest drawdown recovered | recovered, or no drawdown | Validated |
| Execution | Evidence from live fills | the trading mode is Live | Validated |
| Execution | Still profitable if charges were 50% higher | net profit factor with estimated charges raised by half is above 1.0 | Validated |
| Execution | Entry slippage measured on most trades | slippage measured on at least 80% of trades with a reliable entry time | Validated |

The thresholds are constants in `performance_stats.py` (`VALIDATION_*`), next to the sample
gates they reuse (`MIN_TRADES_FOR_GRADE`, `MIN_SESSIONS_FOR_RETURN_GRADE`, `MIN_SESSIONS`).
Trades flagged as unreliable (overnight intraday, no entry time) are not counted in the
slippage coverage. Check text carries percentages and counts only, never rupee amounts, so
the capital what-if scaling cannot make it stale.

**Why Sandbox cannot be Validated.** Sandbox fills are simulated, so they say nothing about
how live orders would execute. "Evidence from live fills" is `not_assessed` whenever the mode
is not Live (or unknown), and Validated needs every check to pass. A Sandbox selection can
therefore reach Under validation at most, however good its figures. The cost and slippage
checks are still computed and shown in Sandbox, but they do not stand in for live fills.

**Edge cases.** A figure that is not a finite number is treated as unavailable evidence (not
assessed), never as a pass or a failure. Every threshold is strict where the table says
"above" (a net profit factor of exactly 1.0 or 1.3 does not pass) and inclusive where it
says "at least" (slippage coverage of exactly 80% passes); detail text shows enough decimals
to explain a result that fails by a hair. If every trade made exactly the same positive
result, the edge check has no spread to test and returns inconclusive; the status is then
Not encouraging with a message saying so, not a claim that the average is not positive.
Strategy rows in the comparison table use the same trading-session list as the combined
portfolio, so a session on which only another strategy traded (a special session on a
holiday or weekend) counts for every row; the single-strategy view counts only its own
traded dates, and the two can differ by that session near a threshold.

**Failed, pending and not assessed.** Each check has one of four results, and the card keeps
them apart:

- **Failed:** the evidence is there and does not meet the requirement (for example the best
  day is 45% of profit). Shown in red, listed on its own, and it blocks the level that needs it.
- **Pending:** more data would settle it (for example 40 of 60 sessions, or fewer than six
  traded days for the best-five-days check). Not a failure.
- **Not assessed:** the evidence is unavailable (no edge result, no drawdown figure, Sandbox
  fills for live execution). Shown as not assessed and never counted as a pass.
- **Passed:** the requirement is met.

Below 30 trades or 10 sessions every data-dependent check is shown as pending, so a handful
of trades cannot produce a misleading failure.

The response carries `insights.validation` for the selection (status, label, summary,
`dimensions`, `checks`, and the lists `failed`, `waiting` and `blocking`, plus the next
level) and `validation_status`, `validation_label` and `validation_summary` on each
comparison row and the portfolio row. The status describes the evidence so far; it is not a
guarantee of future results.

### Not built

Market-regime classification (needs a historical-data definition; regimes must
not be inferred from strategy results), exit slippage, slippage by order type,
broker-reported versus estimated charges, a Sandbox versus live comparison,
mark-to-market drawdown, and an "unstable across windows" verdict.
