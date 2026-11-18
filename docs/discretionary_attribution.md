# Discretionary attribution: the coverage rule and what it means

Source of truth: `services/discretionary_ledger_service.py` (`classify`, `get_coverage_anchor`,
`is_covered`, `same_day_covered`, `attribute_unresolved`). This note describes what that code does today.

## What is decided

Every broker fill in `ledger_fills` gets one state. Only `manual` and `ui_manual` count as
discretionary. `unresolved` fills are listed on the page and excluded from every figure.

Order of checks (first match wins):

1. Live strategy order tag whose label is `UI Exit Position` -> `ui_manual`
2. Any other live strategy order tag -> `strategy_tag`
3. Order id found in `signal_engine/data/trades.db` -> `strategy_engine`
4. Neither knows the order **and the fill is covered** (date rule or same-day rule) -> `manual`
5. Anything else -> `unresolved`

Also `unresolved`: no order id, an unknown-mode tag, or an unreadable `trades.db`.

## The coverage rule

```
anchor   = earliest of
             - oldest live StrategyOrderTag.created_at
             - start of the earliest ledger sync run with status ok or partial (any broker/account)
             - start of the sync run in progress
covered  = fill date >= anchor date + 1 day

T        = effective session start of the fill's exchange on the fill date
           (market_calendar_db.get_effective_session_window: holidays, weekends and
           special sessions included) minus 15 minutes; e.g. NSE 09:15 -> T = 09:00
G        = DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN (default 30 minutes)
covered_same_day =
           the session exists that day (closed or calendar error -> not covered)
           AND fill_time >= T
           AND an ok/partial run started that day FINISHED strictly before T
           AND the start times of ok/partial runs started that day at/after T (any broker or
               account; the run in progress counts), from T up to the first one at/after
               fill_time, are never more than G apart (a gap of exactly G passes)

manual   = (covered OR covered_same_day), absent from tags and trades.db, and fill_time is
           at least DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN (default 30) minutes old
```

The audit reason says which rule applied: `absent from tags and engine; tag coverage ok` or
`absent from tags and engine; same-day coverage ok`.

A fill that is covered but younger than the grace period stays `unresolved` and is
re-evaluated on the next sync. Runs with status `no_auth`, `skipped_window` or `error` never
start coverage, and they are not heartbeats for the same-day rule. An error reading the
strategy tags aborts the sync before any attribution, on every rule.

**Gaps leave fills unresolved.** Under the same-day rule, once the gap between successful run
starts exceeds G, every later fill of that day stays `unresolved` (the safe outcome) and stays so: the date
rule never covers the anchor day. The same-day rule is evaluated only for fills the date rule
does not cover, so in practice it decides the anchor (first-sync) day; days the date rule
covers are unaffected by gaps. Gaps are
measured from T, not from the pre-session run, because no periodic sync is scheduled before
the session opens. A sync cycle that finds no non-revoked auth row (for example right after
the scheduled stop revokes the token) records no run at all, so it is a gap too. Example,
2026-11-18: runs at 08:30:30 (finished 08:30:32), 09:34:38, 09:41:21, 09:56:38, 10:11:58,
10:27:22, 10:42:46; the first gap from T = 09:00 is 34m38s > 30, so that day's five fills
(09:46:18 x2, 10:07:49, 10:16:38 x2) stay unresolved. With G = 45 they would be manual
(the 10:16:38 fills only from a sync at or after 10:46:38, after the grace period).

Why: an order placed through OpenAlgo is tagged when it is placed, before its fill reaches the
broker TradeBook. "No tag" therefore only means "not automated" for days on which the tagging
was in place and the ledger was collecting.

## Residual misclassification and operating limits

- **Failed tag write.** `record_order_tag` (`database/strategy_book_db.py`) logs the
  exception and returns False; the order then has no tag, its fill cannot be classed as
  strategy and becomes `manual`. The same-day rule does not protect against this, and neither
  does the date rule: it holds on covered days too.
- **Broker-created order ids** (GTT triggers, bracket / cover-order legs, broker-side
  replacements) were never tagged by OpenAlgo and end up `manual`.
- **Coupling of the same-day rule to the scheduler.** The ticker wakes every 60 s and a
  periodic sync starts once `DISCRETIONARY_LEDGER_INTERVAL_MIN` (default 15) minutes have
  passed, so syncs start about every 15m20s (observed 15m20s to 15m24s). With G = 30 one
  missed periodic sync makes a gap of about 30m40s, so **G tolerates zero missed syncs**:
  after one miss every later fill of that day stays unresolved (it fails safe). An interval
  of G or more disables the same-day rule in practice (the spacing is the interval plus
  drift). No sync is scheduled before the session opens (periodic starts at 09:15, after
  T = 09:00), so a run finished before T exists only after a boot before T (boot catch-up;
  a boot in the 05:00 to 06:00 skip window records `skipped_window`, which is not a
  heartbeat). Do not relax G or the 15-minute lead to compensate.

## What follows from it

**Fills before coverage are unresolved, not manual.** Nothing can show that a strategy did not
place them. They stay unresolved until a tag or `trades.db` row proves a strategy order.

**First enablement.** The first successful sync sets the anchor. Fills dated that day or
earlier stay unresolved UNLESS same-day coverage holds (a run finished before T that day and
no heartbeat gap above G up to the fill); fills from the next day on can be `manual`. History from before the
first sync is never classed manual. The broker TradeBook is a current-day book, so most older
fills are not in the ledger at all.

**Restart.** The anchor is recomputed on every sync from stored rows (nothing prunes
`ledger_sync_runs`), so a restart does not move it. A boot catch-up sync runs once at start.
It is shared by all brokers and accounts. It moves later only if the oldest live tag predates
the first sync and is then pruned (`STRATEGY_TAG_RETENTION_DAYS`, default 30); it then falls
back to the first sync start.

**Gaps.** The date rule is a threshold, not a continuity check: a weekday with no successful
sync still counts as covered; its fills are simply missing if they were never fetched. The
summary lists such weekdays as `days_without_sync` and warns. The same-day rule, in contrast,
requires continuity (see above).

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
- **After-market (AMO) or pre-open order placed by a build without tagging** (new residual
  risk of the same-day rule): an order entered earlier that day, or the previous evening, by
  a build or process whose tagging was not running, that fills during a covered session ->
  `manual`. The ledger stores no order-entry time, so this cannot be detected.

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
