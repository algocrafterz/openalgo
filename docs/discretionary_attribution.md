# Discretionary attribution: the coverage rule and what it means

Source of truth: `services/discretionary_ledger_service.py` (`classify`, `get_coverage_anchor`,
`is_covered`, `attribute_unresolved`). This note describes what that code does today.

## What is decided

Every broker fill in `ledger_fills` gets one state. Only `manual` and `ui_manual` count as
discretionary. `unresolved` fills are listed on the page and excluded from every figure.

Order of checks (first match wins):

1. Live strategy order tag whose label is `UI Exit Position` -> `ui_manual`
2. Any other live strategy order tag -> `strategy_tag`
3. Order id found in `signal_engine/data/trades.db` -> `strategy_engine`
4. Neither knows the order **and the fill date is covered** -> `manual`
5. Anything else -> `unresolved`

Also `unresolved`: no order id, an unknown-mode tag, or an unreadable `trades.db`.

## The coverage rule

```
anchor   = earliest of
             - oldest live StrategyOrderTag.created_at
             - start of the earliest ledger sync run with status ok or partial (any broker/account)
             - start of the sync run in progress
covered  = fill date >= anchor date + 1 day
manual   = covered, absent from tags and trades.db, and fill_time is at least
           DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN (default 30) minutes old
```

A fill that is covered but younger than the grace period stays `unresolved` and is
re-evaluated on the next sync. Runs with status `no_auth`, `skipped_window` or `error` never
start coverage.

Why: an order placed through OpenAlgo is tagged when it is placed, before its fill reaches the
broker TradeBook. "No tag" therefore only means "not automated" for days on which the tagging
was in place and the ledger was collecting.

## What follows from it

**Fills before coverage are unresolved, not manual.** Nothing can show that a strategy did not
place them. They stay unresolved until a tag or `trades.db` row proves a strategy order.

**First enablement.** The first successful sync sets the anchor. Fills dated that day or
earlier stay unresolved; fills from the next day on can be `manual`. History from before the
first sync is never classed manual. The broker TradeBook is a current-day book, so most older
fills are not in the ledger at all.

**Restart.** The anchor is recomputed on every sync from stored rows (nothing prunes
`ledger_sync_runs`), so a restart does not move it. A boot catch-up sync runs once at start.
It is shared by all brokers and accounts. It moves later only if the oldest live tag predates
the first sync and is then pruned (`STRATEGY_TAG_RETENTION_DAYS`, default 30); it then falls
back to the first sync start.

**Gaps.** Coverage is a date threshold, not a continuity check. A weekday with no successful
sync still counts as covered; its fills are simply missing if they were never fetched. The
summary lists such weekdays as `days_without_sync` and warns.

## Known misclassification

These are documented, not fixed. Each makes an automated order count as discretionary:

- **Broker auto square-off** of an automated position: no OpenAlgo tag, no engine row ->
  `manual`.
- **UI "close all"** on an automated position: label `UI Exit Position` -> `ui_manual`, which
  counts as discretionary.
- **Empty strategy name**: `record_order_tag` writes no tag when the strategy name is empty ->
  `manual`.
- **signal_engine placeholder ids**: `trades.db` rows whose `order_id` is `-TRACKER-`,
  `-EXECUTION-`, `-DECLINED-`, `-RECONCILED-`, `RECONCILED_ORDER_ID` or any id starting with
  `-` are never matched, so the real broker fill of that order -> `manual`.

**Realized-P&L double counting** happens when such a fill opens or closes a position that the
strategy side also counts: the same P&L then appears in the strategy figures and in the
discretionary figures.

A tag can also arrive late or a pruned tag can disappear: `manual` rows are re-checked on every
sync and may move to `strategy_tag`, `strategy_engine` or `ui_manual`, never back. If the
strategy book failed to initialise at boot, untagged automated orders may be classed manual.

## What is not collected

- **MCX evening session** (17:00 to 23:55) and anything outside the scheduler windows
  (periodic 09:15 to 15:30, one end-of-day sync at 16:15, boot catch-up). Unsupported for now.
- **Muhurat and special sessions**: the scheduler runs on weekdays only.
- **TradeBook reset time is not verified.** The TradeBook is a current-day book, so fills made
  outside the sync windows can be lost. Verified once: a read at 22:51 IST still returned that
  day's fills. Not verified: whether a boot before 05:00 next morning sees yesterday's fills.
- **Expired derivatives are unsupported.** There is no settlement close and no expiry
  handling: an open derivative position is never settled at expiry and stays open. Do not rely
  on derivative P&L here.
