# Discretionary attribution: the coverage rule and what it means

Source of truth: `services/discretionary_ledger_service.py` (`classify`, `get_coverage_anchor`,
`is_covered`, `same_day_covered`, `attribute_unresolved`, `resolve_account`,
`normalize_run_accounts`). This note describes what that code does today.

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
- **Order-event syncs.** A live fill seen by the 5 s OrderBook poller (`order.update`, mode
  live, filled quantity above 0) queues one sync `DISCRETIONARY_LEDGER_EVENT_DEBOUNCE_SEC`
  (default 8) seconds later (`subscribers/discretionary_ledger_subscriber.py`; events inside
  the window are coalesced; events during that sync cause one trailing re-check a window
  later). It is the same `sync_all` path, so it records the same `ledger_sync_runs` rows and is
  a same-day heartbeat. A successful one (every account ok or partial) restarts the periodic
  timer, so the next periodic sync is a full interval later: the largest gap between
  successful starts is unchanged (interval plus drift) and G still tolerates zero missed syncs.
  Event syncs run only in the periodic window (weekdays 09:15 to 15:30, never 05:00 to 06:00);
  later fills wait for the end-of-day sync. The event payload is never written to the ledger,
  and the grace, G and classify are unchanged: a fill synced seconds after it happened stays
  unresolved until the grace has passed, as before.

## Safe production repair procedure

1. Stop the app (nothing may write the DB during the repair).
2. Back up the DB (the CLI also writes its own `<db>.bak-<YYYYmmdd-HHMMSS>` before writing).
3. Dry run: `uv run repair_discretionary_accounts.py --db <path>`; read the items. Brokers
   reported `ambiguous_login` or `ambiguous` are left alone.
4. Apply: `uv run repair_discretionary_accounts.py --db <path> --apply --yes`. `--apply`
   alone only prints what would change and exits non-zero.
5. Re-run the dry run: expect `runs_reported=0`.
6. Verify checksums: `ledger_fills` must be identical before and after (for example a hash
   of `SELECT * FROM ledger_fills ORDER BY id` taken from the backup and from the DB).
7. Roll back if needed: stop the app and copy the backup over the DB (the CLI prints the
   exact command).

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

**What the Performance page shows meanwhile.** Unresolved fills are excluded from every P&L
figure and listed under "Unresolved fills". When the selected period and filters hold
executions but no completed trade and no open position, the page says "No attributable trades
available for performance analysis yet. N executions collected; N awaiting attribution. Their
P&L is excluded until classification is resolved." and opens the Unresolved fills list. The
counts come from the summary fields `executions_collected` (ledger fills of the period and
filters whose state is manual, ui_manual or unresolved, by session date of the fill; strategy
fills are not counted; for the period "all" from the reliable-since date) and `executions_unresolved` (the unresolved part of that set). They are
read-only and change no figure. With no such execution the page keeps "No discretionary
trades yet". The Unresolved fills list itself is not limited to the period: it lists every
unresolved fill matching the filters.

**Today's Performance (current session).** The summary's `today` block and the page section of
the same name show the current trading session whatever period is selected (broker / account /
symbol filters apply). The session date is `session_date(now)`: the IST date, or the previous
day before `SESSION_EXPIRY_TIME` (default 03:00), the same date the daily table uses; it is not
calendar-aware, so on a weekend or holiday the block is empty. Everything is read from the same
rebuilt book (no second accounting path):

* *Realised* (gross, estimated charges, net) sums every exit fill of the session, partial exits
  included, on the day of the exit; it equals the daily row of the session. Net is marked
  incomplete when an exit has no charge estimate.
* *Completed positions* are positions that went from flat to flat with their final exit in the
  session (a position opened earlier and closed today counts today; its entry date is shown).
  Win rate, profit factor, average win / loss, expectancy and the trade count use completed
  positions only; a partial exit adds realised P&L but never a completed trade.
* *Open positions* are open now (whatever day they opened); *partially closed* are open
  positions with at least one exit. *Unrealised* needs "Fetch live prices" (not requested by
  default).
* *Pending attribution* lists unresolved fills that were inside the manual grace window when the
  attribution was last evaluated (the newest successful sync start minus the fill time is under
  `DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN`, or the fill arrived after that sync). Each carries
  `attributable_from` = fill time + grace: the earliest time a sync can classify it, at the
  first successful sync that starts after it, and only if no tag or engine order appears and
  coverage holds. Other unresolved fills of the session are counted separately. None of them is
  in any figure, and this view changes no attribution.

When a position becomes reportable: once every fill of it is classified manual / ui_manual.
A fill positively matched to a strategy tag or a signal_engine order is classified at once and
never enters discretionary P&L; a UI-placed order tag is classified ui_manual at once and counts
at once. A fill without such a match is manual only after the grace window, at the next
successful sync. Fills already classified are never delayed by anything else: the book is
rebuilt on the next read after any ledger change (the fingerprint covers every fill and its
state), and the page polls the summary, so the position, its realised P&L and the completed
count update on the first poll after the sync that classified its last fill.

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

## Account identity

A ledger account label is always a broker account id, never the OpenAlgo user name.

- A fill's account is the one the broker reports (Flattrade: `actid`, else `uid`). A
  Flattrade row with neither is skipped and counted (`unverifiable_skipped`).
- For a broker whose fills carry no account, the login's canonical account is used:
  `ledger_account_identity` (broker, Auth name) -> account, else the Auth row's broker
  `user_id`. With neither, the fill is skipped and counted.
- When one fetch reports exactly one account, it is remembered in `ledger_account_identity`
  for that (broker, Auth name). Two accounts in one fetch are stored as reported but not
  remembered.
- A sync run is labelled with the account of its fills when they share one, otherwise with
  the canonical account, otherwise `""` (for example an empty first sync before any fill
  was seen). `""` runs count for the coverage anchor and the same-day heartbeat (both ignore
  accounts) and never produce a per-account warning, but a bad latest `""` run warns at
  broker level (see Sync health below).
- Reconcile finds the session of an account by the Auth row's `user_id`, then through
  `ledger_account_identity`, then by the Auth name (all exact).
- Older runs labelled with a user name are repaired by
  `upgrade/repair_discretionary_accounts.py --db <path>` (dry run by default; writing needs
  BOTH `--apply` and `--yes`; output masked). A run qualifies when its account equals an Auth
  name (or an identity auth name), its broker has exactly ONE login (distinct Auth names of
  that broker plus its identity rows) and that login is the run label, and the broker has
  exactly one other account in `ledger_fills`. The runs of that broker item are relabelled in
  ONE transaction per broker item (not one transaction for the whole repair) and the
  identity row is stored in the same transaction. A broker with two or more logins is
  skipped and reported as `ambiguous_login` (count and reason in the output); a broker with
  two or more fill accounts is skipped (`ambiguous`), and so is a name that the broker itself
  reports as a fill account. `ledger_fills` is never touched; a second run finds nothing.
- Sync health: besides the per-account "latest ledger sync for broker B account A ended S"
  warning, the latest run of each broker over ALL labels (broker `""` is its own group)
  warns "latest ledger sync for broker B (no account known) ended S" when S is in
  partial / error / no_auth / broker_error and that very run has not already produced the
  per-account warning. A later ok run clears it.
- The new table is created by `init_discretionary_ledger_db` and by
  `upgrade/migrate_discretionary_ledger.py` (create-only, idempotent).

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

## Provisional figures and data-quality states (page only)

Neither changes attribution, the grace window or any figure.

* `summary.provisional`: the unresolved fills (broker / account / symbol filters, all
  periods) replayed in memory with the same engine as the book (`reconstruct`,
  `allocate_charges`, `group_completed`), per broker / account / exchange / symbol / product.
  Nothing is written; ids are local and carry no build id, so the fills drill-down does not
  take them (each trade lists its fills inline). Charges are estimates. These trades are in
  no total, metric, breakdown, daily row, equity curve or the today block. Only unresolved
  fills are replayed: an unresolved exit of a position whose opening is already manual shows
  as its own provisional position until it is classified.
* `summary.data_quality`: the manual + unresolved fills of the period and filters, each in
  one state by what it is now: awaiting attribution (grace-held or other), attributed with a
  link to a still-open position (a reversing fill counts here), included (every link in a
  completed trade), or not placed (refused by the engine). collected = the sum of the four.

## Source identification by order tags and broker order evidence (all OFF by default)

Settings (environment, read at call time, all default off; none changes anything until set):

| Setting | Effect |
|---|---|
| `DISCRETIONARY_ORDER_TAG_ENABLED` | live `placeorder` with a strategy name sends a 12-char tag `OA1L<8 hex>` (sha256 of the strategy name) in the Flattrade `remarks`, at the broker call. Not for `UI Exit Position`, `Squareoff`, blank names, analyze/sandbox, smart / basket / close-all paths. |
| `DISCRETIONARY_SOURCE_FROM_ORDERBOOK` | each sync reads the OrderBook once (broker rate limiter) and stores per-order evidence (`ledger_fills.source_evidence`: remark kind/tag, order source; never the raw remark). Enables the remark-tag and "API, untagged" rules. |
| `DISCRETIONARY_ORDERSOURCE_MANUAL` + `DISCRETIONARY_ORDERSOURCE_API_VERIFIED` | both needed: an allow-listed human order source on a complete OrderBook read proves MANUAL at once. Set the second only after one real API order showed `ordersource=API`. |
| `DISCRETIONARY_MANUAL_ORDERSOURCES` (default `WEB,MOB`) | the human-channel allow-list, comma separated, case-insensitive. Documented Noren values are `MOB / WEB / TT` (meanings not documented); only `WEB` is observed (7/7 manual orders). `TT` is not in the default; `API` can never be listed. |
| `DISCRETIONARY_ENGINE_LIVE_ONLY` | engine ids match only trades.db rows with `trade_mode=live`; analyze rows never; NULL/unknown rows keep the fill unresolved. Changes attribution: needs approval. |

Decision order (first match wins; evidence rules only with `..._FROM_ORDERBOOK`):

1. no usable order id: UNCLASSIFIED.
2. registered live order tag: a remark tag with another strategy code (or on a UI-labelled
   order) gives UNCLASSIFIED; otherwise SIGNAL_LIVE (`strategy_tag`) or MANUAL (`ui_manual`).
3. remark tag v1, mode L: SIGNAL_LIVE at once (registered name for the code, else `tag:<code>`;
   a code shared by two registered names also gives `tag:<code>`, marked ambiguous, never a
   guessed name). A tag on an order whose order source is an allow-listed human channel is an
   evidence conflict (someone typed it, or the broker relabelled it): UNCLASSIFIED.
4. remark with the OpenAlgo marker but another version / broken body / non-live mode, or a
   tag embedded in longer text or in other case: UNCLASSIFIED.
5. registered tag of unknown mode, engine db unreadable, engine row of unknown mode: UNCLASSIFIED.
6. engine order id: SIGNAL_LIVE (`strategy_engine`).
7. system marker (`utils/order_source_tag.py` `SYSTEM_REMARK_PATTERNS` and `system_marker`):
   remark classes square-off, auto exit, RMS next to an exit word, MTM, liquidation,
   expiry/physical close, GTT/OCO, bracket/cover; a cover/bracket child leg (`snonum`); a broker
   algo (`algo_name`); a source user (`src_uid`) other than the account: UNCLASSIFIED for good.
   A bare "RMS" is not a marker (the RMS also annotates human orders, e.g. the observed
   51-character price note on a manual WEB order, which stays MANUAL).
8. complete OrderBook read, order source `API`, no tag: UNCLASSIFIED for good (automated,
   strategy unknown; never manual through the grace).
9. complete read, order source present but NOT in the allow-list: UNCLASSIFIED for good.
10. complete read, allow-listed order source, both manual flags on, covered (date or same-day
    rule): MANUAL at once. Not covered: UNCLASSIFIED.
11. otherwise (no evidence: setting off, OrderBook unread, order missing from it, blank order
    source, allow-listed source with the gate off): the legacy rule, coverage plus the
    30-minute grace.

The grace is kept only for rule 11 (no positive evidence: legacy/untagged orders, tag-write
lag of the post-order registered tag, OrderBook unread or order missing). It is not applied
when a remark tag (rule 3) or an allow-listed order source with the gate (rule 10) decides,
and rules 7 to 9 never reach it.

A broker/RMS square-off of a manual position stays UNCLASSIFIED: the manual position shows an
EXIT PENDING / open leg until that fill is resolved by hand. This is deliberate: booking a
system exit as a manual trade would be silent misattribution.

States only move up (unresolved, then manual, then strategy_tag / strategy_engine /
ui_manual; `can_change_state`). Turning a setting on later (for example
`DISCRETIONARY_ENGINE_LIVE_ONLY`) re-decides only rows that are still unresolved, may upgrade
manual rows, and never downgrades a stored manual or strategy row (no audit row, nothing
changes). Coverage stays as completeness evidence. Audit rows
carry the evidence label of each change (`ledger_attribution_audit.evidence`).
Read-time labels (`source_label`): SIGNAL_LIVE, MANUAL, UNCLASSIFIED in the unresolved list,
the provisional block, trade fills and `summary.source_labels`; accounting does not read them.
