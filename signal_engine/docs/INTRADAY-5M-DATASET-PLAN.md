# Intraday 5m Dataset: Design and Acceptance (for IDEA-029)

Status: APPROVED by the owner on 2026-10-05 (Option B), with the locked policies of sections 2 to
11. The build may start. Nothing is published until every item of section 12 holds.

The plan builds the first published intraday dataset, `nse-intraday-5m-YYYYMMDD-001`, from the
existing 1m store plus a targeted AngelOne recovery. It reuses the daily architecture (staging,
audit, snapshot, publish, verify, preflight). It adds only what IDEA-029 needs.

Every count marked "measured" comes from read-only queries on 2026-10-05 against the live
`db/historify.duckdb`. The build recomputes every count on the frozen snapshot (section 2).

## 0. Recorded decisions (owner, 2026-10-05)

```yaml
intraday_dataset_strategy: existing_plus_targeted_recovery   # Option B
source: AngelOne
frequency_source: 1m
published_frequency: 5m
history_start: 2017-01-01
history_end: 2026-10-01          # aligned to nse-daily-20261005-002
price_basis: canonical_nse       # identical factors to nse-daily-20261005-002
boundary_gap_recovery: 1m requests, 29 days or fewer (5m requests are a fallback only)
full_fresh_download: not performed
session_replacement: whole session (legacy OR recovered, never mixed)
provenance_pass_mark: 0.99 exact match on a stratified sample (section 5.5)
yearly_exclusion_ceiling: 0.03
overall_exclusion_ceiling: 0.02
muhurat_sessions: excluded
readiness_model: READY | READY_WITH_WARNINGS | NOT_READY (same as the daily dataset)
```

Two rules apply to the whole workstream:

- **Dataset first.** No IDEA-029 indicator, setup or rule is implemented until the dataset is
  published and verified.
- **The dataset is validated on its own.** No IDEA-029 result, and no strategy result of any
  kind, is an input to any decision in this plan. Every threshold that is set from measured data
  (section 7.3) is set from data-quality measurements only, and is frozen in the contract before
  IDEA-029 runs.

## 1. Pipeline

```text
live historify.duckdb (still written by cron; read exactly once, in step 1)
  1. frozen snapshot (hashed)                                   section 2
  2. cleanup of legacy data -> cleanup ledger                   section 4
  3. classification -> recovery list (deterministic)            sections 4, 5
  4. targeted recovery + provenance sample -> landing + ledger  section 5
  5. unified 1m staging (session-level replacement)             section 5.2
  6. price-basis conversion -> conversion ledger                section 6
  7. 1m validation: structure + NSE cross-check                 section 7
  8. missing-minute policy: measure, fix N, apply               section 7.3
  9. 5m aggregation                                             section 8
 10. 5m validation (independent of step 7)                      section 8.2
 11. validation report + readiness state                        sections 10, 11
 12. owner review of the dry-run report -> publish -> verify    section 12
 13. read path: PublishedSource.intraday
 14. IDEA-029: blocked -> active; then its 12 decisions, freeze, preflight, one run
```

The research vocabulary has no `ready` state: unblocking IDEA-029 sets `research_state: active`
and removes `block_reason`. This happens only after step 13.

## 2. Frozen snapshot (step 1)

- Read the live store once, read-only: `interval = '1m'`, `exchange = 'NSE'`, the 213 symbols of
  `nse-daily-20261005-002`, timestamps from 2016-10-01 to 2026-10-01 inclusive (IST).
- Write `db/staging/intraday/legacy-1m-<YYYYMMDDTHHMMSS>-ist/` as parquet files, one per symbol
  and year, rows sorted by (symbol, timestamp). Columns: symbol, timestamp (epoch seconds), open,
  high, low, close, volume, created_at. No row is changed. Files are set read-only.
- Manifest `snapshot.json`:
  - `source_id`: `historify.duckdb:market_data:NSE:1m` and the absolute path;
  - `description`: the legacy 1m store, written by the Historify cron from AngelOne, plus the
    March 2026 Flattrade UI download (no row-level vendor field);
  - `snapshot_time_ist`, the exact SQL;
  - `row_count`, `symbol_count`, first and last timestamp (IST), the `created_at` range;
  - sha256 per file, and `root_hash` = sha256 of the canonical JSON of the file list.
- Verify in the same run: re-count rows per (symbol, year) against the live query; recompute the
  root hash from the files.
- No later step opens `db/historify.duckdb`. Every later step names the snapshot root hash in its
  own output, so a build that used another source cannot pass verification. Later cron writes
  cannot reach the build.
- Measured size: 165.2 million rows, 213 symbols, 2016-10-03 to 2026-10-05.

## 3. Dataset contract (frozen with the publication)

```yaml
frequency: 5m
source:
  primary: AngelOne historical API (getCandleData ONE_MINUTE)
  legacy_store: true                       # db/historify.duckdb, frozen snapshot (section 2)
  legacy_snapshot_root_hash: <sha256>
  legacy_row_level_provenance: unavailable # the store keeps only created_at, not the vendor
  sample_verification: performed           # section 5.5; result and confidence recorded here
provenance_statement: "legacy Angel store + stratified verification + targeted Angel recovery"
                                           # never "100% row-level Angel provenance"
recovery:
  source: AngelOne
  requests: <count>; ledger sha256: <sha256>
  per_request: symbol, from, to, request time (IST), outcome, sha256 of the raw response
history_start: 2017-01-01
history_end: 2026-10-01
session_calendar: nse_calendar.yaml with the dated session template (section 7.4)
bar_alignment: section 8.1
closing_auction: section 8.1
price_basis: canonical NSE, back-adjusted with the factors of nse-daily-20261005-002
conversion: section 6 in full (factor, agreement, fallback order, tolerances, NSE-range rule,
            unresolved rule, exclusion rule); conversion_spec_version: "1"
volume_policy: section 6.4
missing_bar_policy: a minute with no trade is absent (no fill)
missing_minute_threshold: N = <number>, method of section 7.3, frozen before IDEA-029
missing_session_policy: an expected session with no bars after recovery is MISSING and listed
daily_crosscheck: section 7.1
exclusion_policy: session-level; symbol-level only through a listed range exclusion (section 9)
exclusion_ceilings: overall <= 2%, every year <= 3% (section 10.2)
validation_rules: section 7 (1m) and section 8.2 (5m)
readiness: READY | READY_WITH_WARNINGS | NOT_READY
dataset_version: nse-intraday-5m-YYYYMMDD-001
fingerprint: snapshot-v1 root hash (same algorithm as the daily versions)
```

## 4. Legacy cleanup policy (step 2)

Applied to the frozen snapshot. Every removed bar goes to a cleanup ledger (symbol, timestamp,
rule). Nothing is repaired by inventing a value.

| Rule | Action | Measured | Why |
|---|---|---|---|
| C1 off-minute timestamp | remove bars whose timestamp is not on a whole minute | 395,315 bars, 1,603 sessions | A second vendor's convention (Flattrade, stamped :55 to :59). The Angel bars of the same minute stay |
| C2 pre-open bar | remove bars before 09:15 | 31,207 sessions with a 09:14 bar | Not part of the continuous session. Its volume would enter VWAP, so it never stays |
| C3 negative volume | mark the whole session for recovery | 17,860 bars in 9,204 sessions (8,971 in 2026) | Vendor volume derived from cumulative differences; the session's volume is unusable |
| C4 out-of-scope sessions | drop sessions with no NSE daily row for the symbol | 24,582 sessions | Pre-rename history under today's name (LTM, GMRAIRPORT, GVT&D, UNITDSPR, ...; renames are not joined, as in the daily dataset) and Muhurat sessions stored under the next date |
| C5 replaced history | replace all sessions of APOLLOTYRE, EMAMILTD, EXIDEIND | about 4,800 sessions | Flattrade-basis history from the March 2026 UI download; replaced by Angel recovery, not normalised |

Contaminated sessions (C1, C2): remove the contaminated bars, then validate the rest of the
session with the full conversion (section 6) and cross-check (section 7). If the session passes,
it stays as a legacy session. If it fails, it goes to recovery. Measured on the 31,221 mixed
sessions: 10,639 fail (8,854 of them in 2026) and are recovered; the rest stay.

## 5. Recovery policy (step 4)

### 5.1 Request rules

- 1m only; one request covers 29 calendar days or fewer (verified: a 30-day request drops its
  last day; a 61-day request drops the last day of each internal chunk).
- The recovery client records the outcome of every request. An error, an empty answer for a
  session NSE traded, or an exhausted rate-limit retry is a ledger entry, never a silent gap.
  (The broker plugin logs a failed chunk only as "POSSIBLE GAP".)
- Every raw response is stored under `db/landing/angel-1m/<request id>.json.gz` with symbol,
  range, request time (IST) and the sha256 of the bytes. The ledger is append-only, so the
  recovery is resumable and independently reproducible from the landing files.
- Pacing: the measured effective rate is about 1.1 s per request including rate-limit retries.
- No full historical download.

### 5.2 Session-level replacement

A recovered session replaces every legacy bar of that session. Legacy and recovered bars are
never mixed inside one (symbol, session). Each published session carries one origin:
`legacy` or `recovered`.

### 5.3 What is recovered (measured, 2017 onward)

The recovery list is computed deterministically from the cleaned snapshot. The counts below are
the measured estimates.

| Class | Sessions | Requests (29-day cover) |
|---|---|---|
| Boundary gaps (80 dates about 30 days apart, missing for 100+ symbols) | 12,495 | 12,276 |
| Mixed sessions that still fail after cleanup | 10,639 | 1,272 |
| APOLLOTYRE, EMAMILTD, EXIDEIND (full range), MAHABANK (2017-01 to 2025-09), ATHERENERG | about 7,000 | about 480 |
| Scattered and partial gaps (BSE 199-session run, PATANJALI 125-session run, 7 market-wide dates, others) | 595 | 169 |
| Total | | about 14,200 requests, about 4.5 hours |

The scattered gaps are recovered, not excluded: they fall on normal trading days (median NSE
volume 1.09 times the symbol's median), so they are data holes, not quiet days. A gap that
recovery cannot fill becomes a MISSING session (section 3).

### 5.4 Not recoverable

BDL before its first Angel session (mid-2022; Angel has no older 1m data; NSE lists BDL from
2018-03-23). BDL is published from its first Angel session; the earlier range is a listed
exclusion and counts in the exclusion statistics.

### 5.5 Provenance verification sample

**Definition of the pass mark.** At least 99% of the sampled legacy sessions must exactly match a
fresh AngelOne fetch with the same request parameters (symbol, NSE, 1m, same range). A session
matches exactly when both have the same set of minute timestamps and identical open, high, low,
close and volume on every bar.

**Stratification (not a random sample of recent data).**

| Stratum | Rule | Requests |
|---|---|---|
| Year x symbol | one 29-day window for every symbol in every year it has legacy sessions, window start drawn with a fixed seed (29) | about 2,100 |
| Regime and problem windows, every symbol | 2018-10-01..10-29 (IL&FS stress; doubled-volume era); 2020-03-02..03-30 (COVID crash); 2023-09-01..09-29 (fallback-heavy period); 2025-07-01..07-29 (mixed-vendor era, after cleanup) | about 850 |

This covers every year (history age), every symbol, four named regimes and the three known
problem periods. Only sessions that remain legacy after cleanup are scored.

**Reported.** The exact-match share overall, by year, by symbol and by stratum, with the
sample size and the 95% Wilson lower bound of each share, and the coverage (sampled sessions /
legacy sessions) of each stratum. A second, separate count reports sessions that match after one
per-session price multiplier (a vendor re-adjustment); it does not count toward the pass mark.

**Use.** A sampled session that does not match exactly is replaced by the re-fetched session
(section 5.2). The pass mark is a statement about the unsampled legacy sessions; it is not proof
that every legacy row came from Angel. The provenance statement stays: "legacy Angel store +
stratified verification + targeted Angel recovery".

## 6. Price-basis transformation specification (step 6)

The published 5m dataset is on one canonical research price basis: the NSE basis of
`nse-daily-20261005-002`. Angel's adjustment basis never remains in the published data.

The input is Angel-adjusted 1m data whose adjustment is inconsistent across events and also
across sessions (measured: the same symbol alternates between factor 1.0 and 0.1 in 2025; 2,319
factor changes between consecutive sessions fall on no NSE corporate-action date, half of them
one-session blips). So the factor is estimated independently for every session against NSE raw
daily values for that exact session. No assumption is made that the factor stays constant
between corporate-action dates.

### 6.1 Inputs per (symbol, session)

- From the unified 1m staging: O = first bar open, H = max high, L = min low,
  C = reference bar close (last continuous bar before 2026-08-03, the auction bar from
  2026-08-03), V = sum of volume.
- From `nse-daily-20261005-002` (`daily_raw_factors`) and NSE raw: raw_open, raw_high,
  raw_low, raw_volume, price_factor, volume_factor; and the reference close: `last` (last traded
  price) before 2026-08-03, the official close (the auction price) from 2026-08-03.

### 6.2 Session factor k (Angel price divided by NSE raw price)

Ratios: rH = H / raw_high, rL = L / raw_low, rO = O / raw_open, rC = C / reference close.
Two ratios agree when |a / b - 1| <= 0.001.

1. **Primary.** If rH and rL agree: k = (rH + rL) / 2; method `high+low`.
2. **Fallback.** Otherwise, the first agreeing pair in this fixed order: (rO, rC), (rH, rO),
   (rH, rC), (rL, rO), (rL, rC); k = the mean of the pair; method `fallback:<pair>`.
3. **Unresolved.** No pair agrees: the session is excluded (`unresolved`).
4. **Bound.** k must be in (0, 1.01]. A vendor back-adjustment never raises a past price above the
   raw price; k above 1.01 excludes the session (`excluded:k_bound`).

Justification. The method is valid when the vendor applied one multiplier to the whole session.
Two independent prices that give the same multiplier within 0.1% are the evidence for that. A
session whose fields disagree is not converted.

Justification of the fallback, and its 2023 concentration (measured). 79% of all fallback
sessions fall between 2023-06 and 2024-03, where 17.6% of sessions use it. In every one of
those sessions the minute range lies inside the NSE range: the minute high is at or below the NSE
high and the minute low is at or above the NSE low, never the other way (median gaps 0.04% and
0.11%). So the minute bars miss some extreme trades; they do not carry a different price basis.
The open and the close are single prints that still agree, and the fallback uses them. The cause
of the missing extreme trades is not verified; the effect is one-sided and bounded (rule 6.3).

### 6.3 Conversion and the NSE-range rule

```text
raw_equivalent = angel_price / k
canonical      = raw_equivalent * price_factor(symbol, session)
```

Prices are not rounded to the tick grid (the adjusted daily dataset is not either).

Every converted session must satisfy, against the NSE adjusted daily bar (blocking):

- containment: converted H <= NSE high x 1.0005 + 0.05 and converted L >= NSE low x 0.9995 -
  0.05. A failure is `excluded:range_not_contained` (minute data outside the NSE range is not
  possible for a correct factor);
- fallback independence: for a `fallback:<pair>` session, every field not used for k is checked
  independently: a high or low not used must satisfy containment and also lie within 1% of the
  NSE value (converted H >= NSE high x 0.99; converted L <= NSE low x 1.01), and the volume rule
  (6.4) must hold. A failure is `excluded:fallback_unconfirmed`. Measured: 503 sessions (0.12%).

### 6.4 Volume

- vr = V / raw_volume. Candidates c in {1, 1/k, k}: the vendor left volume raw, scaled it
  inversely with a split or bonus, or scaled it with a demerger factor (NMDC 2022 shows 0.705).
- Pick the c that brings vr / c closest to 1. Accept when vr / c is in [0.85, 1.02]; otherwise
  exclude the session (`excluded:volume`; this removes the doubled-volume sessions of 2018-19).
- Per bar: raw_equivalent_volume = volume / c; canonical_volume = raw_equivalent_volume /
  volume_factor (the daily dataset's convention: adjusted volume = raw volume / volume factor).
  Canonical volume is a float and is not rounded, so 5m and daily sums carry no rounding error.
- The Angel total is normally 0.95 to 1.00 of NSE because minute bars exclude the pre-open
  auction volume.

### 6.5 Corporate-action edge cases

- Ex-date sessions use the same rule. A session whose fields disagree (NMDC 2022-10-27: high
  ratio 0.3996, low ratio 0.3333) has no agreeing pair and is excluded.
- Demerger ex-dates open late (about 330 bars: RELIANCE 2023-07-20, NMDC 2022-10-27, ITC
  2025-01-06). They are not special-cased; the missing-minute rule (7.3) applies as to any day.
- Dividends: NSE does not adjust them and neither does the canonical basis. If Angel adjusted a
  dividend, the per-session k removes it.

### 6.6 Conversion ledger

One row per (symbol, session): origin (`legacy`/`recovered`), k, the method, the volume
candidate c, the containment and independence results, and the status (`converted`,
`converted_fallback`, `excluded:<reason>`, `unresolved`). The validation report keeps the four
outcome classes apart and never reports one combined "converted" share (section 10.1).

## 7. 1m validation and NSE cross-check (steps 7 and 8)

### 7.1 Field rules (each converted session, rolled up to one daily bar)

| Field | Rule | Blocking? | Reason for the tolerance |
|---|---|---|---|
| High | containment (6.3) | Yes | NSE's daily extreme can come from a trade outside the minute bars (2023-24), so equality is not required |
| Low | containment (6.3) | Yes | Same |
| Open | within 0.5% of NSE open | No: counted and reported | NSE open is the pre-open auction price; the first minute's open usually equals it, not always |
| Close before 2026-08-03 | last continuous bar close within 0.1% of NSE `last` | Yes | Same definition (last traded price); the official close was a 30-minute average then |
| Close from 2026-08-03 | auction bar close within 0.1% of the NSE official close | Yes | The official close is the auction price |
| Volume | canonical ratio in [0.85, 1.02] | Yes | Minute bars exclude pre-open volume |

### 7.2 Structure rules (blocking; zero violations allowed in published data)

No duplicate (symbol, timestamp); every timestamp on the whole minute and inside the session's
windows (7.4); no bad OHLC (VAL-OHLC); no non-positive price; no negative volume; one origin per
session.

### 7.3 Missing-minute policy (method locked now; N measured after recovery)

- **Metric.** m = the number of minutes of the continuous window (7.4) that have no bar.
- **Population.** Every session after recovery and conversion that passes every blocking rule of
  7.1 and 7.2, 2017-01-01 to 2026-10-01. Volume within [0.85, 1.02] of NSE shows that the session's
  trading is captured, so this is the distribution of complete sessions.
- **Threshold.** N = the 99th percentile of m over that population, rounded up to the next
  multiple of 5 minutes. Computed once, before 5m aggregation.
- **Application.** A session with m > N is excluded (`excluded:missing_minutes`). The same N
  applies to every symbol and year.
- **Recording.** N, the full distribution of m by year, and the count excluded by year go into
  the contract and the report. N is frozen with the version and cannot change without a new
  version.
- **Independence.** N is a data-quality policy. It is fixed before any IDEA-029 work and is
  never revisited after a strategy result is seen.

Measured before cleanup: in 2016-18 half of the sessions are complete (95th percentile about
205 missing minutes); in 2025-26 93% are complete. If this concentrates exclusions in early years
beyond the ceiling, the dataset is NOT_READY (section 10.2); the threshold is not loosened to
avoid that.

### 7.4 Session template (required)

`nse_calendar.yaml` has one fixed `regular_session` (09:15-15:30). The dated template lives in its
own file, `signal_engine/research/reference/nse_session_template.yaml`, not in
`nse_calendar.yaml`: the calendar's sha256 is the calendar version that the published daily
datasets and the sealed IDEA-027 and IDEA-028 runs record, and an edit would break their
reproduce check. The template's sha256 is recorded in the intraday contract.

- until 2026-07-31: continuous window 09:15-15:29 (375 minutes);
- from 2026-08-03: continuous window 09:15-15:14 (360 minutes) and one closing-auction print
  stamped 15:28 or 15:29 (measured: 361 bars);
- 2024-03-02 and 2024-05-18 (NSE special live sessions with a switch to the DR site): two
  windows, 09:15-09:59 and 11:30-12:29, as in `nse_calendar.yaml`;
- Muhurat sessions have no template (section 9).

The window check reads the template; it is not a new calendar implementation.

## 8. 5m aggregation and its own validation (steps 9 and 10)

### 8.1 Aggregation rules

- Bins are fixed 5-minute intervals anchored at 09:15 IST: [09:15, 09:20), [09:20, 09:25), ...
  The bar is labelled with its bin start.
- A bin aggregates only the 1m bars of one (symbol, session). No bin crosses a session.
- open = first 1m open in the bin; high = max; low = min; close = last 1m close; volume = sum.
- A bin with no 1m bar is absent (never filled).
- Closing auction (from 2026-08-03): the auction print forms its own bar labelled 15:25, alone in
  that bin; the continuous window ends with the 15:10 bin.
- Each 5m bar carries `n_minutes` (1m bars aggregated) so that sparse bins are visible.

### 8.2 5m validation (blocking; a valid 1m dataset is not assumed to give a valid 5m dataset)

1. Every 5m timestamp is a bin start: (minutes since 09:15) mod 5 = 0.
2. The first bin of every session is 09:15 (or later only when the 09:15-09:19 minutes are absent).
3. No 5m bar contains 1m bars of two sessions (checked by recomputing each bar's session from its
   source 1m bars).
4. OHLC: each 5m bar equals the recomputed first/max/min/last of its source 1m bars, exactly.
5. Volume: each 5m bar equals the sum of its source 1m volumes, exactly; the session sums of 5m
   and 1m volume are equal.
6. No duplicate (symbol, 5m timestamp).
7. Expected counts: at most 75 bars per session until 2026-07-31; at most 73 (72 continuous plus
   one auction bar) from 2026-08-03; the count distribution by year is reported.
8. Special sessions: no Muhurat or out-of-calendar session is present; demerger late-open
   sessions start at their first bin with data.
9. Closing auction: from 2026-08-03 every session's 15:25 bar has n_minutes = 1 and its close
   equals the 1m auction bar close; before that date, no 15:25 bar comes from a 15:28/15:29
   auction print.
10. Daily roll-up: 5m rolled up to one daily bar equals 1m rolled up to one daily bar, exactly,
    for every published session.

## 9. Expected exclusions

| Exclusion | Size | Level | Counts in 10.2? |
|---|---|---|---|
| BDL before its first Angel session (mid-2022) | about 1,000 sessions | Symbol range (listed) | Yes |
| Pre-rename history (C4) | about 24,500 sessions | Out of scope: no NSE row for the symbol | No (not requested) |
| Misdated Muhurat sessions (C4) and Muhurat sessions | 6 dates per symbol at most | Out of scope: no reliable mapping to an NSE session identity; none is manufactured | No (not requested) |
| Conversion: unresolved, k bound, range not contained, fallback unconfirmed | about 2,300 sessions (0.55%) before recovery | Session | Yes |
| Volume rule | measured after recovery | Session | Yes |
| Missing minutes above N | measured after recovery | Session | Yes |
| Recovery failures (MISSING) | measured after recovery | Session | Yes |

An excluded session breaks cross-day indicators (ATR, EMA, ADX). How IDEA-029 treats one is its
own decision 11, made before its freeze; the dataset publishes the exclusion ledger so that the
strategy rule is deterministic.

## 10. Validation report and readiness

### 10.1 Conversion statistics (never one combined share)

Overall and for every year, a table with: total symbol-sessions; primary (`high+low`) %;
fallback % (and by pair); excluded % (by reason); unresolved %. The same table by symbol, by
corporate-action event (the ex-date session and the session before it) and by basis class (k = 1,
k = the NSE factor, other k). The 2023-06 to 2024-03 period has its own row with the one-sided
extreme-gap evidence of 6.2.

### 10.2 Exclusion statistics (locked definition)

```text
requested symbol-sessions = expected NSE sessions (nse-daily-20261005-002 rows, series EQ/BE)
                            for the 213 symbols, 2017-01-01 to 2026-10-01, minus Muhurat
                            sessions (out of scope, listed separately with their count)
excluded symbol-sessions  = requested sessions that are not published, for any reason in 9
                            marked "Yes" (listed ranges, conversion, volume, missing minutes,
                            MISSING after recovery)
exclusion rate            = excluded symbol-sessions / requested symbol-sessions
```

Not excluded symbols / total symbols. Gates (the research period is the whole history range):

- overall exclusion rate <= 2%;
- every calendar year's exclusion rate <= 3%.

A breach makes the dataset NOT_READY. No further data is excluded to pass, and no threshold is
changed to pass.

### 10.3 Readiness state

- NOT_READY: any hard gate of section 12 fails. IDEA-029 stays blocked.
- READY_WITH_WARNINGS: every hard gate passes and there is at least one listed exclusion or
  reported (non-blocking) finding, for example open-field differences.
- READY: every hard gate passes with no exclusion and no finding.

## 11. Code changes (smallest set)

- Snapshot export of the legacy 1m store.
- Recovery client with its ledger and landing files (the existing history service path; the
  in-memory broker login, never `auth_db`).
- Cleanup, classification, conversion, cross-check, missing-minute policy, 5m aggregation and 5m
  validation (new modules in `data_readiness/`), each rule with tests on synthetic fixtures.
- Calendar: the dated session template.
- `snapshot.publish` and `verify`: a 5m projection besides the daily one.
- Read path: `PublishedSource.intraday`.

No change to the engine, the gates or the evidence framework.

## 12. Acceptance criteria for publishing `nse-intraday-5m-YYYYMMDD-001`

Every item is a hard gate. A failed item makes the dataset NOT_READY and blocks publication.

| # | Criterion | Evidence |
|---|---|---|
| 1 | Frozen source snapshot hashed | `snapshot.json` root hash recomputes from the files; row counts equal the live query of the same run |
| 2 | Recovery requests completed | every request of the recovery list has a final ledger outcome; no request is pending |
| 3 | Recovery responses hashed and provenanced | every landing file's sha256 equals its ledger entry |
| 4 | Mixed-vendor contamination handled | zero off-minute bars, zero pre-09:15 bars, zero negative volume in the published data; every contaminated session is `legacy` (passed after cleanup) or `recovered` |
| 5 | Known missing sessions handled | every gap of the recovery list is recovered or listed as MISSING |
| 6 | Price-basis normalisation completed | every published session has status `converted` or `converted_fallback` |
| 7 | Conversion statistics generated | the 10.1 tables, by year, symbol, event and basis class |
| 8 | Yearly exclusion statistics generated and within ceilings | 10.2 table; overall <= 2%, every year <= 3% |
| 9 | NSE cross-check passed | every published session passes every blocking rule of 7.1 |
| 10 | Missing-minute policy applied | N computed by the 7.3 method and recorded; no published session has m > N |
| 11 | 1m validation passed | 7.2 with zero violations |
| 12 | 5m aggregation validation passed | 8.2 checks 1-9 with zero violations |
| 13 | 5m validation passed | 8.2 check 10 exact; 7.2 structure rules on the 5m bars |
| 14 | Calendar and session rules validated | every published session is an expected NSE session; every bar is inside the dated template |
| 15 | Expected exclusions documented | the exclusion ledger lists every excluded session with its reason |
| 16 | Provenance verification passed | exact-match share >= 99% (5.5) overall, with the by-year, by-stratum shares, Wilson bounds and coverage reported |
| 17 | Dataset fingerprint generated | the manifest records the snapshot-v1 fingerprint |
| 18 | Root hash generated | the manifest root hash; git copy in `research/data_versions/` |
| 19 | Immutable publication verified | `snapshot verify` passes; objects are read-only; a second build from the same snapshot and landing files gives the same root hash |
| 20 | `PublishedSource.intraday` reads only the published dataset | test: it returns exactly the published bars and cannot open `historify.duckdb` or staging; existing strategy regression tests pass |

The owner reviews the dry-run report (all 20 items evaluated before the version exists) before
the publish command runs. Only after publication and item 20 does IDEA-029 move from
`blocked` to `active`, then to its specification freeze, preflight and backtest.

## 13. Deferred

Phase 5 automation, other frequencies (1m and 15m are not published), the full Phase 6
`/datasets` API and Phase 7 GUI pages, rename chaining, dividend adjustment, point-in-time
membership, cross-vendor reconciliation.
