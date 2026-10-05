# Backtest Data Quality and Readiness: Implementation Plan

Status: APPROVED FOR IMPLEMENTATION, revision 4 (2026-10-04: NSE is the canonical daily source; see
"Revision 4" before Phase 2). Revision 3 (final). The owner approved revision 2 on 2026-10-04
with the final decisions D8, D15, D17, D18, D19 and D20. Implement the MVP in the phase order of
section 7. Add no architecture or features beyond this document.
Requirements: [BACKTEST-DATA-QUALITY-AND-READINESS-REQUIREMENTS.md](BACKTEST-DATA-QUALITY-AND-READINESS-REQUIREMENTS.md)
(called "the requirements" below; section numbers like R10 refer to it).
Evidence: a read-only inspection of the code and of `db/historify.duckdb` on 2026-10-04.

This document is written for a later Claude session to execute phase by phase. Each phase lists its
files, its tests (written first), its exit criteria, and the ECC agents and skills to use.

Changes from revision 1:
- D2 now excludes symbols for symbol-local defects, and blocks only for dataset-level defects or too many exclusions.
- An immutable dataset snapshot is a hard requirement. Before-images and rollback CSVs are no longer
  the reproducibility mechanism.
- The trading calendar is versioned and recorded by every run.
- Renames are an identity/continuity limitation. Negative volume is invalid data. High = low is a diagnostic only.
- The rules are grouped into eight separate data dimensions.
- The phases follow the owner's sequence. IDEA-027 reruns last.
- The scope is reduced to an MVP. The rest is listed as deferred.

Changes from revision 2 (final owner decisions):
- D15: block when defect exclusions are above 10 symbols OR above 5% of the requested symbols,
  whichever is reached first. The values are versioned policy.
- D17: INVALID_DATA is a run status, not a verdict: `run_status = INVALID_DATA`, `verdict = null`,
  `trial_counted = false`.
- D18: rebuilding IDEA-027 v1/v2 is a separate task after the data system is complete. It is not a
  prerequisite for any phase.
- D19: the readiness state machine is explicit (section 5.2).
- D8: backup is operational and kept simple. It is not part of the reproducibility architecture.
- D20: the backtester never reads the landing store. A published version is immutable by contract
  and verifiable by hash (section 3.0).
- IDEA-027: a missing or corrupt daily session excludes the whole symbol (section 6.2).

---

## 0. How to execute this plan

1. Load the project skill `signal-engine-backtest` before you touch `signal_engine/backtest/` or
   `signal_engine/research/`.
2. Execute one phase at a time, in the order of section 7. Do not start a phase until the exit
   criteria of the previous phase pass.
3. For every phase, write the tests first with `ecc:tdd-guide`. Run them RED, implement, then run them GREEN.
   Before you call a phase done, run `PYTHONPATH=. uv run pytest signal_engine/tests/ -q` and
   `uv run pytest test/ -q -k backtest_results`.
4. After every phase, review with the agents named in that phase. Fix CRITICAL and HIGH findings.
5. Run the `fd-audit` skill after every phase that opens DuckDB connections, writes files, or changes
   the cron job (Phases 3, 4, 5).
6. Do not commit unless the owner says so. When the owner asks for a commit, use one commit per phase.
7. Update `signal_engine/docs/PRD.md` at the end of each phase (owner preference).
8. Do not change OpenAlgo core. These files are fork-owned and can be changed:
   - `blueprints/backtest_results.py`
   - `services/backtest_results_service.py`
   - the `frontend/src/**/backtest-results*` tree
   - the `/backtest-results` route registration in `blueprints/react_app.py` (commit ad206c4f0)

   These files are core. Read them, but never edit them: `database/historify_db.py`,
   `services/historify_service.py`, `database/market_calendar_db.py`.
9. The working tree has uncommitted work that is not part of this plan: `reproduce.py`,
   `data_revision.py`, `research/data_revisions/`, the IDEA-027 folder, and the P2 plan. Ask the owner
   to commit or stash it before Phase 1, because the clean-tree check in `record_run` blocks test runs.

---

## 1. Findings: what exists today (verified 2026-10-04)

### 1.1 Data store

- `db/historify.duckdb` is 14.8 GB. The table is `market_data(symbol, exchange, interval, timestamp,
  open, high, low, close, volume, oi, created_at)`. `timestamp` is in epoch seconds (UTC).
- The store has 165,791,547 rows:
  - `1m`: 165.48M rows, 214 symbols, 2016-10-03 to 2026-10-01.
  - `D`: 314,497 rows, 211 symbols, 2019-12-03 to 2026-10-01.
- The upsert key is (symbol, exchange, interval, timestamp). Every `D` row is stamped 05:30 IST
  (midnight UTC of the session date).
- `created_at` is set only from 2026-03-12 onward.
- Two writers upsert straight into the live table:
  - `signal_engine/backtest/backfill.py` (cron, weekdays at 15:35 IST);
  - the core Historify download jobs.

  The metadata tables (`data_catalog`, `download_jobs`, `job_items`) belong to core Historify.

### 1.2 Material data issues found in the store

| # | Issue | Evidence | Status |
|---|---|---|---|
| F1 | Duplicate SBIN daily rows (the incident) | 2,479 extra rows on 1,239 dates, stamped 11:00 and 16:30 IST beside the 05:30 row. 108 of them differed from the 05:30 row. | Deleted in place on 2026-10-04 16:15 IST by `data_revision.py --apply`. The manifest and rollback CSV are in `research/data_revisions/`. |
| F2 | Sunday session 2026-09-13 | `D` rows for 208 symbols with tiny volume (SBIN: 1,050 shares; a normal day is 5-10M). There is one 1m row on that day. | Present. The derived calendar accepts it as a trading day. IDEA-027 v1 and v2 used it. |
| F3 | Other weekend sessions | 2020-02-01, 2020-11-14, 2023-11-12, 2024-01-20, 2024-03-02, 2024-05-18, 2025-02-01, 2026-02-01 | They look like real special sessions (Budget, Muhurat, live DR drills). Not yet confirmed against NSE circulars. |
| F4 | Rename discontinuities | ETERNAL starts 2025-04-09, GMRAIRPORT 2024-12-11, TMPV 2025-10-24, LTM 2026-02-27, JIOFIN 2023-08-21. ZOMATO, GMRINFRA, TATAMOTORS and LTIM are not in the store. | Present. The listing-age logic reads a rename as a new listing. |
| F5 | Delisted and merged names are absent | The universe is the current F&O list (`data.py:33`, `eligibility.py:30`). | Survivorship bias. It is disclosed but cannot be fixed with the current data. |
| F6 | Negative volume in 1m bars | 17,450 rows in 210 symbols | Present. No check exists for it. |
| F7 | Impossible OHLC | 1m: 327 rows in 177 symbols. D: 2 rows in COCHINSHIP. | The loaders drop and count these rows. |
| F8 | 1m bars outside 09:15-15:29 IST | 297,641 rows (0.18%) | The loaders drop and count these rows. |
| F9 | Daily bars with high equal to low and volume above zero | 418 rows | Not flagged |
| F10 | Short history | 6 symbols have fewer than 252 daily sessions | The loaders drop them with `min_sessions`. |
| F11 | The daily close is not the last 1m close | SBIN median difference: 9.8 bps | Expected: the NSE close is a closing-period price. The closing auction session sets it from 3 Aug 2026. |

Canonical-row evidence for F1. On the 108 dates where the duplicates differed:
- The kept 05:30 rows have a volume ratio to the 1m data of 1.001 to 1.186 (median 1.005).
- The removed rows have a ratio of 0.0 to 1.244, so some of them were partial fragments.
- The kept close is nearer the 1m close on 8 dates. The removed close is nearer on none.

Phase 2 confirms the rule against a second source.

### 1.3 Code

| Area | Today | File evidence |
|---|---|---|
| Loading | Converts epoch to IST at load. Duplicates are resolved with `keep="last"`. `exchange` is not in the WHERE clause. | `backtest/data.py:255, 288, 420, 455-470` |
| Run-time DQ | PASS, WARNING or NOT_RUN. It sets `affects_validity` when the bad-bar share is above 0.1% or the incomplete-session share is above 2%. It samples 60 symbols. It runs after the load and before the engine, but it only records. | `dq_run.py:33, 82, 163-197`; `record_run.py:433, 481-514` |
| Blocking | Nothing blocks before execution. Validity fails at verdict time, after the engine ran and after a trial was registered. | `validity.py:31`; `verdict.py:75-89`; `record_run.py:1016` |
| Fingerprint | An aggregate hash of the current rows. It is captured after the engine. It ignores `exchange` and misses offsetting edits. | `manifest.py:253-296`; `run_artifact.py:666` |
| Reproduce | Reruns against the current store. It can detect a change but cannot restore the old data. | `reproduce.py:221-260` |
| Revisions | Deletes in place, with a CSV of the removed rows. The fingerprints in the manifest were written by hand. | `backtest/data_revision.py` (untracked) |
| Backfill | No staging, no validation, no version. | `backfill.py:118, 227, 267` |
| Calendar | Derived from the data (a day counts if 80% of the active symbols traded). | `session.py:89-119` |
| Adjustment | The store holds raw prices. The daily loader back-adjusts splits with a ratio heuristic. The adjustment mode is recorded only as prose. | `data.py:273-285, 380-417` |
| Trials | Keyed on the rules, params, execution and cost fingerprints only. The dataset is ignored. The ledger is append-only. | `research/intake/trials.py:1-15, 183-197` |
| UI | GET-only. Shows `scope.dataset`, `fingerprints` and `data_quality`. No readiness, no versions. | `blueprints/backtest_results.py`; `services/backtest_results_service.py` |
| Name collision | `research/readiness.py` covers engine capability readiness. Data readiness goes in a new package, `backtest/data_readiness/`. | `research/readiness.py:13-20` |

### 1.4 IDEA-027 state

- Runs v1 and v2 are both INCONCLUSIVE and NOT_READY. Their dataset fingerprint is `0749fc7d...a943`,
  which is the store before the dedup.
- `trials.yaml` has two material trials. They differ only in the cost fingerprint.
- The runs stay as historical evidence (R20). They are not the strategy verdict.

---

## 2. Answers to the pre-implementation questions (R21)

| Q | Answer |
|---|---|
| 1 Canonical daily row | The row stamped 05:30 IST of the session date. Section 1.2 has the 1m evidence. Phase 2 confirms it against a second source. |
| 2 Timestamp normalization | Epoch seconds in UTC, converted to `Asia/Kolkata` at load (`data.py:328, 455`). |
| 3 Trading session | Today the calendar is derived from the data. The plan replaces it with a versioned NSE calendar file that every run records. |
| 4 Existing checks | `dataquality.audit_frame` (CLI), `dq_run.build_data_quality` (run path), the loader drop counters, `truncation.py`, `validity.py` |
| 5 Checks to centralize in preflight | Every rule in section 5. Run-time DQ stays unchanged. |
| 6 Issues across the store | F1 to F11. Phase 1 produces the full symbol/date evidence. |
| 7 What blocks all backtests | Section 6.1 |
| 8 What blocks only some contracts | Section 6.1, items marked "contract" |
| 9 Adjusted vs raw | The store and the snapshots hold raw prices. Adjustment is a loader step with a machine-readable `adjustment_mode`. |
| 10 Corporate actions | Detected only by the ratio heuristic. The detected events are recorded per dataset version. A corporate-action master is deferred. |
| 11 Point-in-time universe | Not available. It is recorded as a limitation (WARNING). A contract that requires it is NOT_READY. |
| 12 Backfill corrections | Detected at publish time, when the landing store is compared with the latest snapshot (Phase 5). |
| 13 Late data | A missing calendar session stays PENDING for a grace window, then becomes MISSING (Phase 5). |
| 14 Smallest implementation | A content-addressed parquet snapshot store, published from the existing DuckDB store. Research reads only snapshots. No second database engine and no full copy per version. Details in section 3. |

---

## 3. Immutable dataset snapshots (hard requirement)

### 3.0 Architectural invariant (D20)

**The backtester never reads the live landing store.** The only data path into a backtest is:

```text
landing store -> normalize / validate -> publish -> immutable dataset version -> content hashes -> backtest
```

- A published dataset version is immutable **by contract**. No code path may modify or delete the
  objects or the manifest of a published version.
- A published dataset version is **cryptographically verifiable**:
  - every object is named by its sha256;
  - the manifest carries a root hash over the object hashes;
  - the root hash is recorded in git and in every run that uses the version.

  Verification recomputes the hashes. It does not trust file metadata.
- Read-only filesystem permissions are an extra safeguard only. They are not the reproducibility
  guarantee.
- Enforcement in code: `record_run` and `reproduce` accept only a `Snapshot` source. Passing the
  landing store raises. A test asserts that no run-path module opens `historify.duckdb`.

### 3.1 Design

The live `historify.duckdb` becomes the **landing store**. Core Historify and the backfill write into
it as they do today. Research never reads the landing store.

A **dataset version** is an immutable snapshot published from the landing store:

1. **Object store.** `db/datasets/objects/<sha256[:2]>/<sha256>.parquet`. Each object is one
   partition of canonical rows. The file name is the sha256 of its content.
   - An object is written once. It is never modified or deleted while any version refers to it.
     It is also set read-only, as an extra safeguard (section 3.0).
   - Partitions: `D` has one object per (symbol, year). `1m` has one object per (symbol, month).
     Phase 3 measures the file count and read time, and confirms these choices.
2. **Partition index.** For each interval, a parquet object lists every partition of the version:
   interval, symbol, exchange, period, object hash, row count, first/last timestamp. The index is
   itself a content-addressed object.
3. **Version manifest.** `db/datasets/versions/<version_id>.json` contains:
   - the version id (`historify-YYYYMMDD-NNN`) and the parent version id;
   - the time created (IST) and the cause (`publish`, `correction`, `bootstrap_legacy`);
   - the status: `published`, `legacy` (for reproduction only) or `rejected`;
   - the hash of each interval's partition index;
   - the calendar version;
   - the canonicalization rules and their versions;
   - the health report hash, row counts, and the detected split events;
   - the **root hash**: the sha256 of the canonical manifest body.
4. **Git index.** `signal_engine/research/data_versions/<version_id>.json` is a small copy (about
   2 KB): version id, root hash, parent, status, cause, and a summary.
   - Git records the root hash, so any later change to an object or manifest is detectable.
5. **Structural sharing.** A correction or a daily append writes new objects only for the partitions
   that changed. Every unchanged partition is shared with the parent version. Disk cost: about one
   compressed copy of the store, plus the changed partitions. Phase 3 measures the real size.

### 3.2 How the snapshots guarantee reproducibility

- `record_run` refuses to load data from anything except a `published` version.
  - The run manifest records the version id, the root hash, the calendar version, and the run-scoped
    fingerprint.
  - The run-scoped fingerprint is the sha256 of the sorted object hashes of the partitions the run
    read.
- The loaders (`from_historify`, `from_historify_daily`) get a `snapshot` source. It reads exactly the
  objects listed in the version index, through DuckDB `read_parquet`.
  - Before use, each object's sha256 is verified against its name. A mismatch is a global block.
- When data is corrected, a new version is published. The old version, its index and its objects stay
  unchanged. A run that used the old version reads the same bytes again.
- `reproduce.py` rebuilds the loader from the run's version id, not from the current store.
  - The `dataset_unchanged` check becomes "the version root hash and the object hashes verify".
  - The check no longer depends on the landing store, a rollback CSV, or before-images.
- Objects are never pruned automatically. A future `data-version prune --dry-run` command may delete
  only objects that no version references. It may never delete an object that a version referenced by
  a run uses. This command is deferred.
- The immutable published snapshots are the reproducibility mechanism. Backup (D8) is a separate
  operational concern for disaster recovery only.

### 3.3 Legacy runs (IDEA-027 v1 and v2): separate task after Phase 11 (D18)

This is NOT a prerequisite for any phase in section 7. Attempt it only after the data system is
complete and the clean IDEA-027 evaluation is done. If an exact rebuild is impossible, keep the
historical runs as they are and document the limitation in the idea folder. Never weaken the snapshot
architecture (for example, by letting a legacy run read the landing store) to reproduce them.

The steps to attempt:
1. Rebuild the pre-dedup daily state: the current store plus the removed rows from
   `historify-daily-dedup-2026-10-04-removed-rows.csv`.
2. Verify the rebuilt state with the old v1 fingerprint: `manifest.dataset_fingerprint` over the
   197 symbols must equal `0749fc7d...a943`.
3. Publish it as version `historify-20261001-L00` with status `legacy`.
   - The daily objects contain the SBIN duplicates exactly as they were.
   - The 1m objects are shared with the first clean version, because the dedup changed only `D` rows.
4. Run manifests are sealed and must not be edited. Record the mapping in
   `signal_engine/research/data_versions/legacy_run_map.json`, with the run id, the version id, and the
   v1 fingerprint that proves the match.

After the bootstrap, the CSV is not needed for reproduction. It stays only as history of the old
revision. A `legacy` version can reproduce old runs. Preflight never accepts a `legacy` version for a
new run.

### 3.4 What happens to the in-place revision tool

`data_revision.py --apply` mutates the landing store in place. After Phase 3, corrections are
canonicalization rules that run at publish time. They are versioned, they support dry runs, and they
are recorded in the version manifest. The in-place `--apply` path is removed. The rule logic moves
into `data_readiness/canonical.py`.

---

## 4. Decisions

| ID | Decision | Status |
|---|---|---|
| D1 | The readiness screen is read-only and shows the CLI command to copy. No run trigger from the browser. | KEEP (owner, 2026-10-04) |
| D2 | A symbol-local defect excludes the symbol, or the session when the contract allows it. A dataset-level defect blocks the run. Too many exclusions block the run. Every exclusion is reported with its reason. | CHANGED (owner, 2026-10-04). Section 6. |
| D3 | Objects and full manifests go in `db/datasets/` (not in git). Version summaries and root hashes go in `signal_engine/research/data_versions/` (in git). | Revised for snapshots |
| D4 | Every run references an immutable published snapshot (section 3). This replaces the per-run parquet copy of revision 1. | CHANGED (owner hard requirement) |
| D5 | The data contract has its own fingerprint and the change class `DATA_CONTRACT_CHANGE`. It is not part of the strategy fingerprint. If a contract is not declared, it is derived from the spec, with provenance. | KEEP (recommended) |
| D6 | At publish, a correction to the last 2 sessions is accepted and recorded. A correction to an older bar is held until `data-version approve`. | KEEP (recommended) |
| D7 | Writes by core Historify land in the landing store. They reach research only through a validated publish. An unpublished difference is INFO (freshness). Tampering with a published object is a global block. | Revised for snapshots |
| D8 | Backup is an operational task, kept simple and outside the research architecture. A plain periodic copy of `db/datasets/` and the landing store. No backup tooling is built in this MVP. | FINAL (owner, 2026-10-04) |
| D9 | READY_WITH_WARNINGS may run by default. `--require-ready` blocks it. | KEEP (recommended) |
| D10 | 2026-09-13 is an ERROR until the versioned NSE calendar classifies it from an authoritative source. Every run records the calendar version. | KEEP (owner) |
| D11 | Renames are an identity/continuity limitation (WARNING). Old and new symbols are never joined. It is a blocker only when a contract requires security continuity. | NEW (owner) |
| D12 | Negative volume is invalid data. It causes a session or symbol exclusion, or a global block when it is widespread. It is never repaired. | NEW (owner) |
| D13 | High = low is a WARNING diagnostic. It escalates only together with another invalidity signal. | NEW (owner) |
| D14 | Eight separate data dimensions (section 5.1). No single quality score. | NEW (owner) |
| D15 | Block when defect exclusions are above `max_defect_excluded_symbols: 10` OR above `max_defect_excluded_symbol_share: 0.05` of the requested symbols, whichever is reached first. Eligibility exclusions do not count. The values live in the versioned `signal_engine/research/data_policy.yaml` (policy version = sha256 of the file), and every run records the policy version, so later policy changes stay distinguishable from historical runs. No threshold is hard-coded in logic. | FINAL (owner, 2026-10-04) |
| D16 | Session-level exclusion is allowed only when the contract declares `session_exclusion: allowed`. For lookback-based daily strategies, removing a bar shifts the lookback window, so the whole symbol is excluded instead. | FINAL (owner, 2026-10-04; IDEA-027 in section 6.2) |
| D17 | INVALID_DATA is a run status, not a research verdict: `run_status = INVALID_DATA`, `verdict = null`, `trial_counted = false`. The research verdicts stay EDGE, MARGINAL, NO_EDGE and INCONCLUSIVE. Section 8. | FINAL (owner, 2026-10-04) |
| D18 | The IDEA-027 v1/v2 legacy rebuild is a separate task after Phase 11 (section 3.3). | FINAL (owner, 2026-10-04) |
| D19 | The readiness state machine is NOT_READY, READY_WITH_WARNINGS, READY (section 5.2). | FINAL (owner, 2026-10-04) |
| D20 | The backtester never reads the landing store (section 3.0). | FINAL (owner, 2026-10-04) |

---

## 5. Data-quality model

### 5.1 Dimensions

Every rule belongs to exactly one dimension. The readiness report shows a status for each dimension
(PASS, WARNING or FAIL), with counts. It also shows one overall state. It shows no score.

| Dimension | Question it answers |
|---|---|
| `validity` | Is each bar internally possible? |
| `completeness` | Is every expected bar present? |
| `identity_continuity` | Is a symbol the same security over the whole period? |
| `coverage` | Does the dataset cover the requested symbols, fields, period and history? |
| `calendar` | Are the sessions the real NSE sessions? |
| `universe_membership` | Which symbols were tradeable on which date? |
| `survivorship` | Are delisted or merged securities included? |
| `adjustment` | Are prices adjusted as the contract requires? |

Two further groups belong to the dataset itself, not to the data: `snapshot_integrity` (version
status and hashes) and `freshness` (how recent the data is).

### 5.2 Actions

Each rule result maps to one action:

| Action | Meaning |
|---|---|
| `BLOCK` | The run is NOT_READY. No run id is used and no trial is counted. |
| `EXCLUDE_SYMBOL` | The symbol is removed from the run. The exclusion is recorded with its reason. |
| `EXCLUDE_SESSION` | One symbol-session is removed. Allowed only under D16. Otherwise the action becomes `EXCLUDE_SYMBOL`. |
| `WARNING` | The run proceeds. The limitation is persisted and shown. |
| `INFO` | Shown for context only. |

Readiness state machine (D19). There are exactly three states:

| State | Effect |
|---|---|
| `NOT_READY` | The backtest cannot start. |
| `READY_WITH_WARNINGS` | The backtest may run. The warnings are persisted in the run and visible in the GUI. No manual acknowledgement is required, unless the run uses `--require-ready`, in which case this state blocks. |
| `READY` | The backtest runs normally. |

The state is computed in this order:
1. Any `BLOCK` gives `NOT_READY`.
2. Otherwise, the state is `NOT_READY` when the number of symbols excluded for **defects** is above
   `max_defect_excluded_symbols` (10), OR above `max_defect_excluded_symbol_share` (5%) of the
   requested symbols, whichever is reached first (D15). It is also `NOT_READY` when eligible symbols
   fall below `min_symbols`.
3. Otherwise, any WARNING or any exclusion gives `READY_WITH_WARNINGS`.
4. Otherwise, the state is `READY`.

Example: 197 requested symbols. 5% is 9.85, so a 10th defect exclusion blocks (share rule). With 400
requested symbols, an 11th defect exclusion blocks (absolute rule), although it is under 5%.

Exclusions have two classes:
- `defect`: the data is invalid or incomplete. These count toward the threshold.
- `eligibility`: the strategy's own rules exclude the symbol, for example insufficient history or
  listing age. These are expected, so they are reported but do not count toward the defect threshold.

### 5.3 Rule catalogue

The thresholds come from `data_policy.yaml` (D15). A value that already exists in `dq_run.py:33` is
reused. Every other value is set from the Phase 1 audit, with its rationale written down.

| Rule id | Dimension | Detects | Action |
|---|---|---|---|
| SNAP-STATUS | snapshot_integrity | The version is missing, `legacy`, or `rejected` | BLOCK |
| SNAP-HASH | snapshot_integrity | An object or manifest hash does not verify (unversioned mutation) | BLOCK |
| SNAP-DUP-KEY | snapshot_integrity | More than one row for a (symbol, exchange, interval, session date or timestamp) in a published snapshot. Canonical snapshots cannot contain this, so it means the snapshot is broken. | BLOCK |
| SNAP-EXCHANGE | snapshot_integrity | A symbol exists on more than one exchange in the interval | BLOCK |
| CAL-VALID | calendar | The calendar file fails validation, or the snapshot was validated with a different calendar version than the run uses | BLOCK |
| CAL-UNEXPECTED-DATE | calendar | The snapshot has bars on a date that the calendar does not list as a session (for example 2026-09-13), for at least `calendar_date_symbol_share` of the symbols | BLOCK |
| CAL-UNEXPECTED-ISOLATED | calendar | The same, but for fewer symbols | EXCLUDE_SESSION |
| VAL-OHLC | validity | high below max(open, close), low above min(open, close), or high below low | EXCLUDE_SESSION |
| VAL-NONPOS | validity | A price at or below zero | EXCLUDE_SESSION |
| VAL-NEG-VOL | validity | Volume below zero (D12) | EXCLUDE_SESSION |
| VAL-NULL-ROW | validity | Null OHLC in individual rows | EXCLUDE_SESSION |
| VAL-WIDESPREAD | validity | The invalid-bar share across the run scope is above `bad_bar_share` (0.1%, from `dq_run.py:33`) | BLOCK |
| VAL-PLACEHOLDER | validity | High = low together with session volume below `placeholder_volume_ratio` of the symbol's 20-session median, or on an unexpected date (D13 escalation) | EXCLUDE_SESSION |
| DIAG-LOCKED | validity | High = low with normal volume (locked or illiquid) | WARNING (count) |
| COV-FIELD | coverage | A required field is absent, or null in the whole scope | BLOCK |
| COV-PERIOD | coverage | The snapshot does not cover the requested period plus warm-up | BLOCK |
| COV-SYMBOL-ABSENT | coverage | A requested symbol is not in the snapshot | EXCLUDE_SYMBOL (defect) |
| COV-HISTORY | coverage | The symbol has fewer sessions than the contract's `min_history` | EXCLUDE_SYMBOL (eligibility) |
| COV-MIN-SYMBOLS | coverage | Eligible symbols are below the contract's `min_symbols` | BLOCK |
| CMP-MISSING | completeness | The share of calendar sessions missing inside the symbol's active range is above `max_missing_session_share` (2%, from `dq_run.py`) | EXCLUDE_SYMBOL (defect) |
| CMP-MISSING-FEW | completeness | Missing sessions at or below that share | WARNING (list) |
| CMP-LATEST | completeness | The latest session is incomplete (bar count or write time before the close) | EXCLUDE_SESSION, or BLOCK when the contract sets `uses_latest_bar` and the session is inside the run |
| ID-RENAME | identity_continuity | The symbol starts after the store start, and the start is a known rename (`symbol_changes.yaml`, a small list of known renames) | WARNING; BLOCK only if the contract requires `security_continuity` (D11) |
| UNI-PIT | universe_membership | Membership comes from the current list, not point-in-time | WARNING; BLOCK if the contract requires `point_in_time` |
| SURV-BIAS | survivorship | Delisted or merged names are absent | WARNING (always shown) |
| ADJ-MODE | adjustment | The loader's adjustment mode differs from the contract | BLOCK |
| ADJ-FAILED | adjustment | The contract requires split adjustment, and the adjustment step failed or found an unresolved event | BLOCK |
| ADJ-SPLIT-EVENTS | adjustment | Split events detected and applied by the heuristic | WARNING (list) |
| ADJ-DIVIDENDS | adjustment | The series is price-return only | WARNING; BLOCK if the contract requires `total_return` |
| FRESH-LAG | freshness | The latest published session is behind the expected session | INFO; BLOCK only if the contract sets `max_staleness_sessions` |
| FRESH-UNPUBLISHED | freshness | The landing store has changes not yet published | INFO |
| INFO-OUTSIDE-HOURS | validity | 1m bars outside the session window (dropped by the session filter, as today) | INFO (count) |
| INFO-CLOSE-SEMANTICS | adjustment | The daily period spans 2026-08-03 (closing auction) | INFO |

---

## 6. Run outcome summary (owner request)

### 6.1 What blocks a run (NOT_READY; no run id, no trial)

Dataset or global conditions:
1. Invalid dataset version or fingerprint: the version is missing, `legacy` or `rejected`, or a hash
   does not verify (SNAP-STATUS, SNAP-HASH).
2. Unversioned dataset mutation: an object or manifest changed after publication (SNAP-HASH).
3. A broken canonical invariant in a snapshot: duplicate keys, or exchange ambiguity (SNAP-DUP-KEY,
   SNAP-EXCHANGE).
4. An invalid trading calendar: the file is invalid, the version does not match, or a panel-wide
   unexpected session such as 2026-09-13 (CAL-VALID, CAL-UNEXPECTED-DATE).
5. Widespread corruption: the invalid-bar share is above 0.1% of the run scope (VAL-WIDESPREAD).
6. Missing required fields (COV-FIELD).
7. Coverage below the strategy requirement: the period is not covered, or eligible symbols are below
   `min_symbols` (COV-PERIOD, COV-MIN-SYMBOLS).
8. A broken adjustment model when the contract requires adjustment (ADJ-MODE, ADJ-FAILED).
9. Too many symbol exclusions: defect exclusions above 10 symbols OR above 5% of the requested
   symbols, whichever is reached first (D15, versioned policy). Eligibility exclusions do not count.

Contract-specific conditions (only when the contract requires them):
10. Point-in-time universe, total-return prices, security continuity, a staleness limit, or a
    complete latest bar. The plan does not build the first three (they are deferred), so a contract
    that requires one of them is NOT_READY by design.

Policy:
11. `--require-ready` when the state is READY_WITH_WARNINGS.

### 6.2 What only excludes a symbol or a session

| Condition | Default action |
|---|---|
| Impossible OHLC, a non-positive price, negative volume, null OHLC in a row | Session; symbol when `session_exclusion` is not allowed (D16) |
| A placeholder bar (high = low plus very low volume or an unexpected date) | Session; symbol under D16 |
| A session on a date not in the calendar, for a few symbols only | Session; symbol under D16 |
| An incomplete latest session (when the contract does not use the last bar) | Session |
| Missing sessions above 2% of the symbol's active range | Symbol (defect) |
| A requested symbol is not in the snapshot | Symbol (defect) |
| Insufficient history for the contract's lookback | Symbol (eligibility) |

Each exclusion is written to `evidence/data_exclusions.parquet` with the symbol, session (or all),
rule id, dimension, class (defect or eligibility) and evidence. The run report shows requested,
loaded, eligible and excluded symbols, with the count per reason. The loader applies the same list,
so the engine never sees an excluded symbol or session.

IDEA-027 contract (owner decision): its contract declares `session_exclusion: not_allowed`.
Dropping one daily session is not safe, because it shifts the 252-session lookback. That can change:
- the 252-session high close;
- the proximity ratio;
- the ranking;
- the top-20 portfolio.

So a relevant missing or corrupt daily session excludes the whole symbol. The run never removes the
session and continues the rolling calculation.

### 6.3 WARNING and INFO

- WARNING:
  - survivorship bias (always);
  - non-point-in-time universe;
  - rename discontinuities (ETERNAL, LTM, TMPV, GMRAIRPORT and others);
  - split events applied by the heuristic;
  - unadjusted dividends;
  - locked bars (high = low);
  - missing sessions under the threshold;
  - zero-volume share above 5%;
  - every applied exclusion, as a summary.
- INFO:
  - freshness lag (without a contract limit);
  - unpublished landing-store changes;
  - 1m bars outside the session window that were dropped;
  - closing-auction close semantics;
  - dataset size and last update.

### 6.4 What remains deferred

Each deferred item is disclosed as a WARNING, or it blocks only a contract that requires it.

- A security master and corporate-action intelligence. This includes rename chaining (joining
  ZOMATO and ETERNAL), demergers, rights issues, and dividend or total-return adjustment.
- Point-in-time F&O membership and a delisted-symbol universe (gate G4.7).
- Reference data: daily price bands and circuits (G6.3), the F&O ban list, and a liquidity or capacity
  table (G5.4).
- Cross-vendor reconciliation beyond the one-off check of the canonical rule.
- Spike and stale-price detectors as rules. They stay as Phase 1 audit diagnostics only.
- Promotion of the daily-against-1m cross-check to a rule. It stays an audit diagnostic.
- A browser run trigger (D1), Telegram alerts for failed publishes, and object pruning.
- Any change to the gate evaluators or to the verdict rules (R4).

---

## 7. Phases (in the owner's required order)

### Phase 1: Data-store audit

Goal: the inventory and issue report required by R6, with symbol and date evidence.

- `signal_engine/backtest/data_readiness/rules.py`:
  - a `Rule` dataclass with id, version, dimension, action, rationale, and `evaluate(conn, scope)`;
  - one function per rule in section 5.3.

  The SQL uses DuckDB parameters only, on a read-only connection. A SQL error raises. It is never
  turned into a pass.
- `data_readiness/audit.py` and the CLI `python -m signal_engine.backtest data-audit`. It writes:
  - `audit.json`: the global summary and the status per dimension;
  - `per_symbol.parquet`;
  - `evidence/<rule_id>.parquet`;
  - `AUDIT.md`: a short human summary.
- The audit also writes the deferred diagnostics: spikes, stale repeats, and the daily-against-1m check.
- The audit runs against the landing store. The calendar used is the derived one, until Phase 2.
- Exit:
  - The audit reproduces F1 to F11 on the real store (F1 is now 0).
  - It reports how many symbols each defect rule would exclude, compared with the D15 thresholds
    (10 symbols or 5%).
  - The owner reviews `AUDIT.md`.
- ECC: `ecc:tdd-guide`, `ecc:python-reviewer`, `ecc:silent-failure-hunter`.

#### Phase 1 status (2026-10-04): built, awaiting owner review of the audit

Files:
- Code: `signal_engine/backtest/data_readiness/{policy,rules,state,audit}.py`
- Policy: `signal_engine/research/data_policy.yaml`
- Tests (105, all passing): `signal_engine/tests/{dq_fixtures,test_data_policy,test_dq_state,test_dq_rules,test_data_audit}.py`
- Report: `signal_engine/reports/data-audit/2026-10-04/` (`AUDIT.md`, `audit.json`, `per_symbol.parquet`, `evidence/`)

Deviations from the plan text:
- **CLI name.** The CLI is `python -m signal_engine.backtest.data_readiness.audit`, following the existing
  module-CLI pattern (`data_revision.py`). The strategy CLI in `backtest/__main__.py` has no subcommands.
- **Rules added beyond section 5.3:**
  - `CANON-DAILY-TS` (BLOCK): a daily bar not stamped 05:30 IST.
  - `FRESH-SYMBOL-LAG` (WARNING): a symbol's data stops before the calendar end.
  - Audit diagnostics: `DIAG-LOCKED`, `DIAG-SPIKE`, `DIAG-STALE-REPEAT`, `DIAG-LATE-START`,
    `DIAG-CROSS-INTERVAL`, `DIAG-ZERO-VOL`.
- **Proxy calendar.** The Phase 1 calendar is the union of the calendars derived from the daily and 1m
  data, weekdays only. A daily-only derived calendar hid the F12 gap below.
- **Missing sessions under D16.** When session exclusion is not allowed, `CMP-MISSING-FEW` (a missing
  session) also excludes the symbol (owner decision, IDEA-027).
- **Blocked dates.** Exclusions on a date that already blocks the run (a panel-wide calendar error) are
  attributed to that block. They are not counted as separate symbol defects.
- **Skipped rules.** A rule that applies to the interval but cannot run (for example, a missing
  `created_at` column) is never a pass. Its dimension reads NOT_EVALUATED. The skipped rule becomes a
  warning, or a block if it is a BLOCK rule.
- **Session-share cap.** On 1m, a symbol that loses more than 2% of its sessions to exclusions is
  excluded as a whole symbol.

Review fixes (ecc:python-reviewer, ecc:silent-failure-hunter), each with a regression test:
- NaN and infinite values count as invalid rows.
- Out-of-hours 1m bars do not count as defects.
- The IST session date and minute come from integer arithmetic (IST is a fixed +05:30 offset), not a
  time-zone conversion per row.
- An empty calendar or zero rows is never a pass.
- Policy values are checked strictly: finite numbers, real HH:MM times, a lookback of at least 1.
- A NULL `created_at` on the latest bar is a finding.
- `--fail-on-not-ready` makes the CLI exit with code 1.

Real-store audit, projected for a run over every symbol: **D is NOT_READY and 1m is NOT_READY.**
New material findings:
- **F12. Daily sessions missing for almost every symbol.** On 31 trading days (for example 2021-07-23,
  2021-11-26, and a run from 2022-02-15 to 2022-03-07), the daily data has a bar for SBIN only. The 1m
  data has 190-197 symbols on the same days. 183 daily symbols miss these sessions. Under D16 every one
  of them is excluded, so the daily data is NOT_READY for IDEA-027 until the missing daily bars are
  re-fetched. The daily-only derived calendar dropped these days, so the gap was invisible.
- **F13. Periodic 1m gaps.** On 55 trading days, roughly every 30 days from 2019-12-10 to 2026-05-07,
  the 1m data is missing for almost every symbol, while the daily data has 160-207 symbols. The 30-day
  spacing matches the 30-day 1m download chunks (`backfill.py` `chunk_days`). A chunk-boundary day is
  probably being lost (to verify in Phase 5). 171 of 214 1m symbols miss more than 2% of their
  sessions. 1m history older than about 12 months cannot be re-fetched from the broker.
- **F14. Calendar.** The daily data has 9 weekend dates and the 1m data has 10 (including 2016-10-30
  and 2019-10-27). All of them block until the Phase 2 calendar classifies them.
- **F15. Invalid 1m bars.**
  - Negative volume: 17,450 rows on only 47 sessions (for example 2023-05-03, 2024-03-02, 2025-10-21,
    2026-08-03).
  - Impossible OHLC: 326 rows.
  - Non-positive prices: 15 rows on 2025-10-21.
  - The invalid share is 0.011%, below the 0.1% widespread-corruption limit.
- **F16. COCHINSHIP.** The 2026-10-01 daily bar has zero volume and a non-positive price.

The owner must decide (before Phase 2):
- Whether to re-fetch the F12 daily bars, and how to treat the F13 1m gaps.
- What the F13 1m gaps mean for intraday contracts.

### Revision 4 (2026-10-04): NSE is the canonical daily source

This section supersedes Phase 2 to Phase 5 below where they conflict. Owner decision of 2026-10-04,
after the source investigation.

#### Evidence (read-only investigation, 2026-10-04)

| | Flattrade (current = 2026-09-13 legacy) | AngelOne | NSE bhavcopy |
|---|---|---|---|
| Depth | 2019-12-03 | 2010 | complete archive |
| NSE agreement (15 dates) | 100% on unadjusted bars | about 72 sessions use the last traded price, not the official close | reference |
| Corporate actions | about 64 events price-adjusted with raw volume, 29 fully raw, 4 split days divided twice | back-adjusted as of the fetch date, some dividends too | raw |
| Completeness | 31 market-wide sessions missing, 13 symbols miss about 30 sessions | 2020-11-14 Muhurat missing for 137 symbols | complete |

- The current Flattrade download is identical to the legacy store (311,517 rows), so the legacy daily
  data is Flattrade's.
- `PREVCLOSE` in the bhavcopy is NOT adjusted on an ex-date (RELIANCE 2024-10-28: 2655.70). Factors
  therefore come from a corporate-action source, not from the bhavcopy.
- The NSE corporate-action feed (`/api/corporates-corporateActions`) answers without a session and
  lists, for the 211 symbols from 2019-12 to 2026-10: 50 bonuses, 28 splits, 13 rights issues, 10
  demergers and 2 bonus-debenture/NCRPS schemes (BRITANNIA 2021, TVSMOTOR 2025).

#### Decisions

- **D21.** The canonical daily source is the NSE CM bhavcopy. Flattrade and AngelOne are reference
  and diagnostic data only. They are never merged into the canonical daily dataset. A missing or
  invalid NSE row is reported, never filled.
- **D22.** Raw NSE values are never modified. Adjustment is a separate, versioned layer. A published
  daily version stores raw OHLCV plus a per-row `price_factor` and `volume_factor`. Adjusted values are
  raw times the factor (volume: raw divided by the volume factor).
- **D23. Corporate-action layer (MVP).**
  - Events come from a dated snapshot of the NSE corporate-action feed, kept as evidence. The parsed
    events and the factor method are versioned; the version is recorded in every dataset version.
  - Factors:
    - split "From Rs A To Rs B": price factor B/A, volume factor B/A;
    - bonus "a:b" (a new for b held): b/(a+b) for price and volume;
    - several events on one ex-date: the product;
    - rights "a:b @ premium P" with face value F: subscription S = F + P, TERP =
      (b*C + a*S)/(a+b), C = the official close before the ex-date, price factor TERP/C (1 when
      S >= C), volume factor 1;
    - demerger: method set by the owner (open question below); volume factor 1;
    - bonus debentures/NCRPS and dividends: not adjusted; a documented limitation.
  - Backward adjustment: a bar's factor is the product of the factors of every event with an ex-date
    after the bar's session.
  - An event the parser cannot read, or a factor outside (0, 1], blocks publication until an entry
    in `corporate_action_overrides.yaml` (with a source) resolves it.
- **D24.** Validation of the layer: after adjustment, `ADJ-JUMP` must find no unexplained jump
  (BLOCK). Each applied factor is compared with the raw ex-date gap (diagnostic). The AngelOne step
  factors are a reference comparison only.
- **D25.** Expected sessions come only from `nse_calendar.yaml` (done; `audit.py` uses it by default).
- **D26.** Ingestion: NSE EOD -> landing (raw zip files with sha256, kept) -> staging DuckDB (parsed
  EQ rows) -> validate -> adjust -> readiness audit -> publish an immutable version. A failure
  publishes nothing; the last published version stays active.
- **D27.** The daily cron stops writing daily bars into the live store for research. It runs the D26
  pipeline. Core Historify is not touched.
- **D28.** 1m: AngelOne stays the candidate. It goes through the same staging pipeline after the daily
  version exists (downloader fixed: 29-day requests). `ADJ-CROSS-BASIS` against the adjusted NSE daily
  data detects basis conflicts. No full 1m rebuild unless a contract requires it.
- Symbol identity: history is not joined across renames (TATAMOTORS -> TMPV, ZOMATO -> ETERNAL);
  a renamed symbol starts at its rename (an identity limitation, reported by `DIAG-LATE-START`).
- Survivorship: the universe is today's F&O list (a documented limitation).

#### Phases, revision 4 order

1. Phase 1: audit. DONE.
2. Phase 2a: NSE calendar. DONE (`nse_calendar.yaml`, `calendar.py`, audit wired, 98 tests).
3. Phase 2b: NSE EOD ingestion. `data_readiness/nse_eod.py`: fetch by calendar session, landing zips
   with sha256, staging table `nse_daily_raw(symbol, isin, series, session, open, high, low, close,
   last, prev_close, volume, turnover, trades, source_file, source_sha256)`. Range 2016-01-01 to
   today. Exit: every calendar session has a file; a missing file is a finding, never skipped.
4. Phase 2c: corporate-action layer. `data_readiness/corporate_actions.py`: feed snapshot, parser,
   factors, overrides, backward application. Tests on known events: RELIANCE 2024-10-28 (0.5), HAL
   2023-09-28 (0.5), BAJAJFINSV 2022-09-13 (split 5 to 1 with bonus 1:1 = 0.1), NMDC 2024-12-27 (1/3),
   a rights issue, and an unparseable subject that blocks.
5. Phase 2d: the adjusted staging dataset and the audit (official calendar). Exit: no unexplained
   `ADJ-JUMP`; completeness findings explained; owner sign-off.
6. Phase 3: snapshots (section 3), published from the NSE staging dataset, not the landing store.
7. Phase 4: preflight, contract and trials (unchanged).
8. Phase 5: the cron runs D26; a failed batch keeps the active version.
9. Phases 6 to 8: API, GUI, full tests (unchanged).
10. Phase 9: publish the first clean daily version. Phases 10 and 11: the IDEA-027 contract, then the rerun.
11. 1m track (D28) after Phase 9.

#### Open question for the owner

Demerger factor (10 events in the window: ABB 2019, GMRAIRPORT 2022, MOTHERSON 2022, NMDC 2022,
RELIANCE 2023, ITC 2025, SIEMENS 2025, TMPV 2025, HINDUNILVR 2025, VEDL 2026). The feed gives no value
for the demerged company. Recommended: price factor = the official ex-date open / the official close
before the ex-date. NSE runs a special pre-open price-discovery session on a demerger ex-date, so the
open is the exchange's own price for the remaining company; the factor is deterministic and comes from
official data only. Limitation: the factor also contains the overnight market move. Alternative: a
factor per event from the company's cost-of-acquisition announcement, entered by hand in the overrides
file with a source.

### Phase 2: Canonical data rules and calendar validated

Goal: the canonicalization rules and the calendar are proven before any snapshot is built.

- `data_readiness/canonical.py`:
  - D-row rule: on a session date that has a 05:30 IST row, that row is canonical. Other rows for
    the same symbol and date are dropped from the snapshot, and the drop is recorded.
  - A date with several rows but no 05:30 row is unresolved. Its session is quarantined: it is not
    published, and preflight then excludes it under CMP-MISSING or the D16 rules.
  - Each rule has a version and a dry-run report.
- Confirm the D-row rule against a second source for the 1,239 SBIN dates (`dataquality.cross_source`).
  Record the result in `signal_engine/research/data_versions/canonical-rules.md`.
- `signal_engine/research/reference/nse_calendar.yaml` lists holidays, special sessions with their
  time windows, and mock sessions.
  - Seed 2025 and 2026 from the seed functions in core `database/market_calendar_db.py`. Read them;
    do not import core into the run path.
  - Take 2016 to 2024 from NSE circulars, with a source URL for every special or mock session. Use
    WebSearch with primary NSE sources only.
  - Classify the 9 weekend dates. Do not mark 2026-09-13 until a source confirms it (D10).
  - The calendar version is the sha256 of the file. `data_readiness/calendar.py` validates the
    schema and fails fast.
- `signal_engine/research/reference/symbol_changes.yaml` holds only the known renames: from, to,
  effective date, source. It is used for disclosure (ID-RENAME), never for joining histories.
- Exit:
  - The second-source result is recorded.
  - The calendar validates.
  - The owner signs off the canonical rules and the calendar classification.
- ECC: `ecc:tdd-guide`, `ecc:python-reviewer`.

### Phase 3: Dataset versioning and immutable snapshots

Goal: section 3. This covers AC-04, AC-05 and AC-06.

- `data_readiness/snapshot.py`:
  - `publish(source_conn, calendar, rules, parent) -> Version`, which validates, canonicalizes,
    writes only the changed objects, then writes the index and the manifest;
  - `open_version(version_id)` with hash verification;
  - `diff(a, b)` by symbol, date and field.
- `data_readiness/versions.py`: the ledger, the git summary, and the status changes.
- CLI: `python -m signal_engine.backtest data-version {publish --dry-run, list, show, diff, verify}`.
- Loaders: `data.py` `from_historify` and `from_historify_daily` accept `source=Snapshot`. The
  live-store path stays for exploratory use. `record_run` accepts only a snapshot.
- Fingerprint: the run-scoped fingerprint is the hash of the object hashes, with
  `fingerprint_algo: snapshot-v1`. The old aggregate fingerprint stays, so old manifests still verify.
- No legacy bootstrap in this phase (D18).
- Remove the in-place `--apply` from `data_revision.py`, and move the rule into `canonical.py`
  (section 3.4).
- Measure:
  - the time and disk size of the first full publish;
  - the file count;
  - the read time for a 200-symbol 1m run;
  - the daily increment size.

  If the 1m read time is more than twice the current DuckDB load, change the partition grain before
  the exit.
- Exit:
  - A published fixture version verifies by root hash. Changing one byte of an object makes `verify` fail.
  - A test asserts that no run-path module opens the landing store (D20).
  - Loading the same canonical fixture from DuckDB and from the snapshot gives identical frames.
  - Objects are read-only on disk.
- ECC: `ecc:tdd-guide`, `ecc:python-reviewer`, `ecc:silent-failure-hunter`, the `fd-audit` skill.

#### Phase 3 status (2026-10-05): DONE

- Published `nse-daily-20261005-001`, fingerprint `c7ee36c2833cbbaa10eb70c90bfc74c244200d51933eca0b3d2db8cb7406505e`.
  `snapshot.verify`: 2,007 objects, 0 errors; published bars equal the staged bars; files read-only;
  a one-bit change is detected. Code: `data_readiness/snapshot.py` (publish, verify, open_version,
  resolve_version). Git record: `signal_engine/research/data_versions/`.
- Exclusions accepted by the owner: FORCEMOT (2023-10-26..2024-02-13, no NSE trading), CGPOWER
  (2019-12-26..2020-02-12, BZ only). Series rule v1 (EQ, else BE; never BL; no BZ).

#### Phase 4 decisions (owner, 2026-10-05)

1. Regime block: skipped for published-dataset runs, recorded as unavailable ("no published index
   dataset"); no Yahoo or other external source; never affects the verdict.
2. Reproduction: schema 4 runs reproduce only against their recorded version; schema 3 runs are
   historical only (`dataset_version: not_recorded`); never fall back to the live store.
3. Calendar: compatibility is checked for the requested range only. A calendar update that only
   adds later sessions does not invalidate a version; a correction inside the range is NOT_READY and
   needs a new published version.
4. Minimum history: every history requirement of the frozen version (warm-up, eligibility prior
   sessions, lookback/period/window parameters, indicator periods), with provenance.
5. Universe: Strategy universe (spec, else `data.NSE_FNO`) -> dataset availability -> requested ->
   data-defect exclusions -> eligibility exclusions -> eligible; every limitation recorded.

#### Phase 4 progress

- DONE (tests first): `data.from_published_daily` (no heuristic split re-adjustment; shared
  `_clean_daily` with the legacy loader), `snapshot.resolve_version`, scoped `open_version`,
  `data_readiness/contract.py`, `data_readiness/preflight.py` (+ `write_record`, `DataNotReadyError`).
  Tests: `test_published_loader.py`, `test_preflight.py`, fixtures `published_fixtures.py`.
- Real dry preflight of IDEA-027 (no engine, no run, trials.yaml unchanged): READY_WITH_WARNINGS;
  defect exclusions exactly CGPOWER + FORCEMOT; unavailable GVT&D, M&M, NIFTYFPI; 10 eligibility
  exclusions from the existing 500-session loader minimum; 196 eligible.
- DONE (2026-10-05): run-path integration.
  - `record_idea`: `prepare_data` (resolve one published version, contract, preflight) runs before
    the engine; NOT_READY and load-time rejections (`LoadRejected`: validity-affecting data, or a
    calendar that differs from the official one) leave a record under `<idea>/preflight/`, no run
    id, no folder, no trial. Validity is decided before the trial ledger: INVALID_DATA runs are
    sealed with `verdict = null`, `trial_counted = false`, and never registered.
  - `execute_backtest` reads bars only through `Resolution.open()` (verified version, eligible
    symbols); the baseline reload uses the same source; intraday is refused (`DataUnavailable`)
    until a 1m version is published; the intraday engine code is kept and tested with in-memory
    frame sources. Regime block skipped and noted. CLI `--dataset-version`, `--require-ready`.
  - Manifest/flat record schema 4 (dataset version, root hash, run fingerprint, preflight,
    symbol counts, exclusions, run status, trial flag); validator, registry and the read model
    accept {3, 4}; `reproduce` uses only the recorded version, schema 3 runs are `not_recorded`.
  - Tests: full `signal_engine` suite 5,159 passed (+ registry fix); `test/test_backtest_results_*`
    250 passed. 83 older tests were migrated to the resolution data path (assertions changed only
    for the approved behaviour: schema 4, INVALID_DATA instead of INCONCLUSIVE for data failures,
    dataset identity from the published version, regime block unavailable).
  - Acceptance: real IDEA-027 preflight READY_WITH_WARNINGS, defect exclusions exactly CGPOWER and
    FORCEMOT, no run, trials.yaml unchanged; `nse-daily-20261005-001` still verifies (2,007 objects).
- OPEN: code review of the run-path changes (the review agent hit a usage limit); M&M and GVT&D are
  in the F&O universe but not in the published version (publish a v002 that includes them); the
  existing 500-session loader minimum excludes 10 symbols that IDEA-027 (252 sessions) could use.

### Phase 4: Pre-run readiness

Goal: R9 and R10. This covers AC-01, AC-02, AC-03 and AC-10.

- `data_readiness/contract.py`: `resolve_contract(frozen)`, with provenance per field. It is sealed
  into the run as `strategy/data_contract.json`, with `fingerprints.data_contract` and the change
  class `DATA_CONTRACT_CHANGE`.
  - Fields: `frequency`, `required_fields`, `min_history_sessions`, `adjustment`, `universe`,
    `calendar`, `bar_timestamp_semantics`, `uses_latest_bar`, `session_exclusion`, `min_symbols`,
    `max_staleness_sessions`, `requires {point_in_time, total_return, security_continuity, fno_ban}`
    (all false by default), `blocking_overrides`.
  - An override can only make a rule stricter.
- `data_readiness/preflight.py`: `run_preflight(contract, version_id, symbols, period, policy) -> PreflightReport`.
  - It reads the snapshot only. It returns the state, the status per dimension, the exclusions, the
    counts (requested, loaded, eligible, excluded by reason), the coverage, the latest session, the
    freshness, and the versions of the rules, policy, calendar and contract.
- Placement in `record_run.record_idea`: after the freeze dry run (`record_run.py:997-1002`), and
  before `_execute_frozen` (1004) and `register_material` (1016).
  - NOT_READY writes `research/ideas/<idea>/preflight/<YYYY-MM-DD-HHMM>.json` and raises
    `DataNotReadyError`. No run id, no trial, no run folder.
  - Otherwise, the exclusions are passed to the loader, and `preflight.json` plus
    `evidence/data_exclusions.parquet` become run artifacts.
- CLI: `python -m signal_engine.backtest preflight <idea> [--version ID] [--until DATE] [--require-ready]`.
- Manifest schema 4 (additive): `dataset.{version_id, root_hash, status, fingerprint_algo,
  run_fingerprint, adjustment_mode, calendar_version, canonical_rules, coverage, latest_session}`,
  `preflight {state, sha256}`, `data_policy {version}`, `fingerprints.data_contract`,
  `symbol_counts`.
  - The read model keeps working for schemas 2 and 3.
- `reproduce.py` opens the run's version instead of the current store. A run without a version id
  (from before this system) reports `dataset_version: not_recorded`. It never falls back to the landing store.
- Run-time DQ (AC-11): `dq_run.py` and `validity.py` stay active with unchanged severities. When
  validity fails on data, `next_action` says "Fix the data, then rerun" and names the checks.
- Tests (first):
  - SBIN-incident snapshot: preflight blocks, the engine spy is never called, `trials.yaml` is
    unchanged, and no run folder exists.
  - Symbol-local negative volume: the symbol is excluded and the run proceeds.
  - Exclusions above the threshold block the run.
  - `session_exclusion: not_allowed` escalates a session defect to a symbol exclusion.
  - A `legacy` version is refused.
  - A tampered object blocks the run.
  - Warnings are persisted in the run.
  - A declared contract overrides a derived one.
  - `--require-ready` blocks a run with warnings.
- ECC: `ecc:tdd-guide`, `ecc:python-reviewer`, `ecc:code-reviewer` (`record_run` is the critical
  path), the `fd-audit` skill.

### Phase 5: Ingestion validation

Goal: R11. This covers AC-07.

- The backfill keeps writing to the landing store. After the backfill, the cron runs
  `data-version publish`:
  1. Read the changed partitions from the landing store.
  2. Canonicalize.
  3. Validate with the rule pack against the calendar.
  4. Compare with the parent version field by field. Classify each row as new, identical, a recent
     revision, an old revision, or adjustment-like (one ratio across many dates).
- Late data: a missing session stays PENDING for 2 sessions, then becomes MISSING.
- A batch with a dataset-level ERROR produces a `rejected` version record with the evidence. The
  latest `published` version does not change. A failed update never becomes the active dataset.
- Old revisions wait for `data-version approve` (D6).
- A publish report goes to `db/datasets/publish_runs/<ts>.json`. The cron exits non-zero when it
  rejects a batch.
- Tests (first):
  - a duplicate in the landing data;
  - malformed OHLC;
  - a missing session, then late arrival;
  - an old revision held for approval;
  - an adjustment-like change flagged;
  - a failed publish keeps the active version;
  - the same input published twice is a no-op.
- ECC: `ecc:tdd-guide`, `ecc:silent-failure-hunter` (today the backfill swallows chunk exceptions),
  `ecc:python-reviewer`, the `fd-audit` skill.

### Phase 6: API and read model

Goal: R15. This covers AC-09 (backend).

- The CLI writes `health.json` per version. The Flask service reads only JSON and parquet files. It
  never opens DuckDB.
- New GET endpoints, with the same auth and rate limit as today:
  - `/api/backtest-results/datasets`
  - `/datasets/<version_id>`
  - `/datasets/<version_id>/symbols/<symbol>`
  - `/<idea_id>/readiness`

  Run detail gains a `data` block.
- `version_id` and `symbol` must match strict patterns. Paths resolve under the ledger root only.
- Tests: extend `test/test_backtest_results_service.py` and `test/test_backtest_results_route.py`
  with these cases:
  - authentication is required;
  - an unknown version returns 404;
  - path traversal is rejected;
  - a schema 3 run still renders.
- ECC: `ecc:tdd-guide`, `ecc:python-reviewer`, `ecc:security-reviewer`.

### Phase 7: GUI visibility

Goal: R14. This covers AC-08 and AC-09 (frontend).

- `DataReadinessPanel` (read-only, shows the CLI command, D1):
  - the status for each of the eight dimensions;
  - blocking reasons in plain words;
  - the exclusions table;
  - the calendar version.
- `BacktestDataCard` on the run detail:
  - version, root hash and calendar version;
  - the period and the latest session;
  - requested, eligible and excluded symbols with reasons;
  - the adjustment mode;
  - the status per dimension;
  - warning badges that stay visible however good the performance is.

  The card has no score.
- `DataDetails` page `/backtest-results/datasets/:versionId`:
  - tabs: Coverage, Validity, Completeness, Identity, Calendar, Universe, Adjustment, Revisions, Freshness;
  - a drill-down per symbol;
  - the route registered in `blueprints/react_app.py`, in the same way as the existing routes.
- An INCONCLUSIVE run caused by data shows the failed checks, their severity, the affected symbols
  and dates, the reason the result cannot be trusted, and whether a rerun is recommended.
- Reuse `Card`, `Table`, `Badge`, `StatusBadge`, `ShortHash`, `InfoTip` and the glossary. Extend
  `DataQualityBlock`.
- Tests: READY, READY_WITH_WARNINGS and NOT_READY for both the panel and the card; the drill-down;
  and a schema 3 run.
- Build with `cd frontend && npm run build`.
- ECC: `ecc:tdd-guide`, `ecc:typescript-reviewer`, `ecc:react-reviewer`.

### Phase 8: Full test pass

- Run the whole Python suite and the frontend tests. Every acceptance criterion in section 9 must have
  a passing test.
- AC-12 regression: the existing strategy end-to-end tests pass unchanged. On a clean fixture, a run
  produces identical trades whether it is loaded from DuckDB or from a snapshot.
- The data-quality system is validated by its own fixtures and by the Phase 1 audit, not by an IDEA-027 rerun.
- ECC: `ecc:code-reviewer` over the full diff.

### Phase 9: Publish the first clean dataset version

1. `data-version publish --dry-run`. The owner reviews the health report: the status per dimension,
   the quarantined sessions, and the calendar classification.
2. Publish `historify-YYYYMMDD-001` with status `published`.
3. Run `data-version verify`.

### Phase 10: Validate the IDEA-027 data contract

- `preflight idea-027-52-week-high-proximity --version historify-YYYYMMDD-001`, without running the backtest.
- Expected warnings: survivorship, non-point-in-time membership, renames, split events, and
  unadjusted dividends.
- Under D16, any symbol with a session defect is excluded as a whole symbol.
- The owner reviews the exclusions and the warnings.
- If 2026-09-13 is still unclassified, preflight blocks (D10). Classify the date first.

### Phase 11: Rerun IDEA-027

Start this phase only after all of these hold:
- the data system has passed its own audit and tests (Phase 8);
- a valid immutable dataset version is published and verified (Phase 9);
- the IDEA-027 contract has passed preflight (Phase 10).

Then:
- Run it, reproduce it from its version id, and only then evaluate the strategy evidence.
- Runs v1 and v2 stay unchanged as historical evidence. Reconciling them is the separate task in
  section 3.3 (D18).

---

## 8. Trial accounting (D17)

`INVALID_DATA` is a **run status**, not a research verdict. The research verdicts stay EDGE,
MARGINAL, NO_EDGE and INCONCLUSIVE. A run whose data is invalid has:

```text
run_status    = INVALID_DATA
verdict       = null
trial_counted = false
```

The manifest gains `run_status` (`VALID` or `INVALID_DATA`), `invalid_reason` (the failed checks) and
`trial_counted`. The read model and the GUI show `INVALID_DATA` in place of a verdict, with the
reasons. They never show it as INCONCLUSIVE.

| Stage | What happens |
|---|---|
| Preflight rejection (NOT_READY) | Preflight runs before the engine and before `register_material` (`record_run.py:1016`). It raises `DataNotReadyError`. No run id, no run folder, no trial. The rejection is recorded only in `research/ideas/<idea>/preflight/`. |
| Load-time rejection | The run-time data check (`dq_run`) runs after the load and before the engine. If it finds a defect that preflight did not catch, `record_run` stops before the engine and before `register_material`. It writes a rejection record with `stage: load`. No valid research trial is registered. This is stricter than today, so no check is weakened (AC-11). |
| Data invalidity after the engine ran | The run folder is sealed and stays visible with `run_status = INVALID_DATA`, `verdict = null`, `trial_counted = false`. `register_material` moves from `record_run.py:1016` to after the validity checks, and is called only when `run_status = VALID`. So an invalid run never counts as a trial. |
| Rerun on corrected data | Trials are keyed on the rules, params, execution and cost fingerprints. The dataset is not part of the key (`trials.py:7-8`). The first valid run of a configuration registers one trial. Later reruns of it add none. |
| Existing IDEA-027 trials | The ledger is append-only and is not rewritten. Trials 1 and 2 stay as recorded. Correcting them belongs to the separate legacy task (D18). |

Tests:
- `trials.yaml` has identical bytes after a preflight rejection and after a load-time rejection.
- A post-engine data failure produces a run folder with `run_status = INVALID_DATA`, `verdict = null`,
  `trial_counted = false`, and no new trials entry.
- A valid run of the same configuration afterwards registers exactly one trial.
- The verdict function is never called with an INVALID_DATA run. A test asserts that the verdict set
  is still exactly EDGE, MARGINAL, NO_EDGE, INCONCLUSIVE.

---

## 9. Acceptance criteria mapping

| AC | Phase | Test |
|---|---|---|
| AC-01 a duplicate blocks before execution | 3, 4 | SBIN-incident snapshot test (engine spy not called, trials unchanged) |
| AC-02 a warning permits the run and is persisted | 4 | `test_preflight.py::test_warnings_persist_in_run` |
| AC-03 a declared contract is validated | 4 | `test_data_contract.py` |
| AC-04 every run records an immutable version | 3, 4 | manifest schema 4 tests; refusal of a non-snapshot run |
| AC-05 updates do not change run provenance | 3 | publish v2, reproduce a v1 run byte-identically |
| AC-06 revisions support dry run, audit, idempotence and a fingerprint change | 2, 3 | `canonical` dry-run tests; idempotent publish; new root hash |
| AC-07 ingestion detects bad data before publication | 5 | `test_publish_pipeline.py` |
| AC-08 the GUI shows coverage, duration, counts, state, adjustment, limitations and freshness | 7 | panel and card tests |
| AC-09 drill-down from a result to the dataset | 6, 7 | route tests, `DataDetails` test |
| AC-10 the SBIN incident is caught before execution | 1, 4 | rule fixture plus the AC-01 test |
| AC-11 run-time checks stay active | 4 | existing `dq_run` and `validity` tests, unchanged |
| AC-12 existing strategies are regression-tested | 8 | end-to-end suite plus the identical-trades test |

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| The first full publish of 165M rows is slow or large | Measured in Phase 3. It runs once. Daily publishes rewrite only the current-month objects. |
| Too many 1m files slow the loads | Measured in Phase 3. Change the partition grain before the exit. |
| DuckDB writer lock against core Historify | The publish opens the landing store read-only and writes only parquet objects. |
| The D15 thresholds block too often on the real store | The Phase 1 audit reports the exclusion counts. A change is a new policy version, approved by the owner and recorded per run. |
| Calendar research is incomplete | An unclassified date stays an ERROR. It is never guessed. |
| Daily lookback strategies lose many symbols to single-session defects (D16) | The audit counts this before the threshold is set. A contract may declare `session_exclusion: allowed` only with a written reason. |
| The manifest schema change breaks the UI | The read model keeps the schema 2 and 3 paths. A fixture test covers each schema. |

---

## 11. Layman summary

The backtester will stop reading the live price database directly. Every day, a checked and frozen
copy of the prices is published, with a version number. Only the parts that changed are stored again,
so this costs little disk space. Every test records which frozen copy it used, so it can be repeated
exactly years later, even after the data is corrected.

Before a test starts, a checker looks at the data in eight separate areas: bad bars, missing days,
renamed stocks, coverage, the trading calendar, the stock list, missing delisted companies, and price
adjustments.

- If the whole dataset is broken, the test does not start, and it is not counted as an attempt.
- If only one stock has bad data, that stock is left out, and the report says which stock and why.
- If too many stocks are left out, the test does not start.
- Known weaknesses, such as missing delisted companies or renamed stocks, are shown as warnings on
  every result.

IDEA-027 is rerun only at the very end, after the data system has proven itself on its own.
