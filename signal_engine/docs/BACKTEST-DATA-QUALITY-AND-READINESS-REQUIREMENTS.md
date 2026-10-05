# Backtest Data Quality, Readiness & Data Transparency

**Engineering Requirements — OpenAlgo / Signal Engine**  
**Status:** Proposed  
**Scope:** Historical data, preflight validation, daily backfill, dataset versioning, and GUI transparency

---

## 1. Purpose

Establish a dedicated data-quality and dataset-readiness layer so fundamental data problems are detected **before a strategy backtest executes**, while retaining run-time validation as a second line of defense.

The system must also make the exact data used by every backtest visible and auditable in OpenAlgo.

---

## 2. Problem Statement

A recent strategy backtest exposed a process gap: the historical daily dataset contained multiple observations for the same SBIN session date, including later records that were not always identical to the canonical daily bar. The run-time data-quality check detected the problem only after execution.

Target workflow:

```text
Data Store
    ↓
Data Validation
    ↓
Dataset Snapshot
    ↓
Strategy / Data Compatibility Check
    ↓
Backtest
    ↓
Run-time Validation
    ↓
Results
```

A failed preflight must prevent a backtest from starting when a mandatory data requirement is not satisfied.

Run-time validation remains mandatory as a second line of defense.

---

## 3. Goals

The system must:

- Inventory and characterize all available backtest data.
- Detect material data-quality problems before strategy execution.
- Define `READY`, `READY_WITH_WARNINGS`, and `NOT_READY` states.
- Allow each Strategy Version to declare a machine-readable data contract.
- Bind every backtest run to an immutable dataset snapshot/fingerprint.
- Validate every daily data update before it becomes research-ready.
- Prevent silent contamination by bad or revised data.
- Expose coverage, quality, provenance, adjustment mode, universe limitations, and freshness in the GUI.
- Preserve enough evidence to reproduce exactly what data a historical run used.

---

## 4. Non-Goals

- Do not redesign strategy evaluation, verdict, promotion, or signal logic.
- Do not weaken existing run-time data-quality checks.
- Do not silently repair questionable data.
- Do not create strategy-specific data cleaners.
- Do not hide survivorship or point-in-time-universe limitations.
- Do not block on warnings unless the rule is explicitly classified as blocking.

---

## 5. Core Data Concepts

| Concept | Requirement | Example |
|---|---|---|
| Dataset | Logical collection of market data at a defined frequency/adjustment mode. | Historify daily adjusted OHLCV |
| Dataset Revision | Deterministic transformation or source update creating a new fingerprint. | SBIN dedup revision |
| Dataset Snapshot | Immutable data state referenced by a backtest. | Historify daily snapshot X |
| Data Contract | Strategy-declared data requirements. | 1d OHLCV, 252 sessions |
| Preflight | Validation before execution. | Coverage + duplicate + OHLC checks |
| Run-time Validation | Validation during/after execution. | Per-run `data_quality.json` |
| Provenance | Source, ingestion, transformation and version history. | Historify → normalization → revision |

---

## 6. Initial Historical Data Audit

Audit the actual data store.

Aggregate percentages alone are insufficient. Every material issue must have symbol/date-level evidence.

The audit must cover:

- Datasets, frequencies, fields, exchanges/segments, and sources.
- Earliest/latest coverage and per-symbol coverage.
- Missing sessions and unexpected sessions.
- Duplicate dates.
- Duplicate timestamps.
- Multiple observations where one bar is expected.
- OHLC integrity and impossible values.
- Zero/negative prices.
- Abnormal/zero volume.
- Timezone and session normalization.
- Adjusted versus raw price semantics.
- Corporate-action/split handling.
- Listing-age conditions.
- Insufficient-history conditions.
- Symbol lifecycle/delisted handling.
- Current-universe versus point-in-time universe membership.
- Survivorship bias.
- Data provenance and ingestion/update history.
- Current dataset fingerprint/version.

The audit must report:

1. Global summary.
2. Per-symbol summary.
3. Per-date evidence for material issues.
4. Severity classification.
5. Whether each issue currently blocks backtesting.

---

## 7. Data Quality Severity Model

| Severity | Meaning | Examples |
|---|---|---|
| `ERROR` | Mandatory condition failed; dataset is `NOT_READY` for the affected use case. | Canonical daily duplicates, malformed OHLC, required history unavailable |
| `WARNING` | Dataset may be usable, but the limitation must be explicitly disclosed. | Survivorship bias, sparse history, non-point-in-time universe |
| `INFO` | Informational only. | Last update time, dataset size |

Quality rules must explicitly identify whether they are:

- Global blocking rules.
- Strategy/data-contract-specific blocking rules.
- Warning-only rules.
- Informational rules.

Do not introduce arbitrary thresholds without documenting their rationale.

---

## 8. Dataset Readiness State

### `READY`

All mandatory checks pass.

### `READY_WITH_WARNINGS`

All mandatory checks pass, but one or more documented non-blocking limitations remain.

### `NOT_READY`

At least one mandatory requirement fails.

A backtest **must not start** when the requested dataset is `NOT_READY`.

Readiness must be **strategy-aware**. A dataset may be `READY` for one strategy and `NOT_READY` for another because their data contracts differ.

---

## 9. Strategy Data Contract

Every frozen Strategy Version must declare or resolve a machine-readable data contract containing at least:

- Frequency / bar size.
- Required fields.
- Minimum history / warm-up.
- Adjustment mode.
- Required universe.
- Universe membership semantics.
- Session/calendar requirements.
- Minimum coverage where applicable.
- Mandatory versus warning-only data-quality conditions.

### IDEA-027 example

The contract should capture the strategy's actual requirements, including:

- Daily adjusted OHLCV.
- 252-session highest-close calculation.
- F&O universe.
- Required trading-session/calendar semantics.
- Explicit survivorship limitation.

The data layer must remain generic and must not contain IDEA-027-specific cleaning logic.

---

## 10. Pre-Backtest Data Readiness Gate

Before `record_run` starts strategy execution, the system must:

1. Resolve the exact dataset snapshot/fingerprint.
2. Resolve the Strategy Version and its data contract.
3. Validate dataset compatibility with the contract.
4. Run mandatory data-quality checks.
5. Produce a preflight report with pass/fail/warning evidence.
6. Block execution when readiness is `NOT_READY`.
7. Allow execution when `READY` or `READY_WITH_WARNINGS`, according to configured policy.
8. Persist the preflight result with the run metadata.

### Important

Preflight **does not replace** run-time data-quality validation.

Both are required:

```text
Preflight
    ↓
Prevent known bad runs
    ↓
Backtest
    ↓
Run-time validation
    ↓
Catch issues exposed during execution
```

---

## 11. Daily Backfill / Update Requirements

Required pipeline:

```text
Receive
  ↓
Normalize
  ↓
Deduplicate
  ↓
Validate
  ↓
Detect gaps / revisions
  ↓
Version
  ↓
Fingerprint
  ↓
Publish as research-ready
```

The daily update process must:

- Validate expected symbol/session coverage.
- Detect duplicate observations before publication.
- Detect revised historical observations.
- Distinguish late-arriving data from genuinely missing data.
- Normalize timestamps/sessions deterministically.
- Detect adjustment/corporate-action changes.
- Never silently replace a known-good dataset with a failed update.
- Assign a new dataset fingerprint/version to every accepted revision.
- Support dataset-to-dataset comparison by symbol, date, and field.

A bad daily update must not silently contaminate future backtests.

---

## 12. Data Revision / Cleaning Requirements

Corrective operations must be versioned and reversible where feasible.

**Never silently mutate historical data.**

Every data revision must support:

- Dry-run mode.
- Exact affected-row count and scope.
- Rule/version identification.
- Source dataset fingerprint.
- Affected symbols/dates.
- Before/after counts.
- Machine-readable manifest.
- Rollback artifact where feasible.
- Idempotent application.
- New resulting dataset fingerprint.

Applying the same revision twice must be a no-op.

Existing backtest runs must continue referencing their original dataset snapshot.

---

## 13. Backtest Dataset Snapshot

Every backtest run must persist:

- Dataset identifier.
- Dataset fingerprint.
- Dataset revision/version.
- Source/provider.
- Frequency.
- Adjustment mode.
- Coverage period.
- Universe/membership source and version.
- Preflight readiness result.
- Data-quality summary.
- Known warnings/limitations.
- Data transformations/revisions applied.
- Run-time data-quality result.

A historical run must remain reproducible even after later data updates.

Conceptually:

```text
Dataset v1
   ↓
Backtest Run A
   ↓
Immutable provenance

Dataset v2
   ↓
Future Backtest Run B
```

Run A must continue to reference Dataset v1 even after Dataset v2 becomes current.

---

# 14. OpenAlgo GUI Requirements

Backtest performance should **not** be presented without enough information to understand the data behind the result.

The GUI should expose a concise data summary and provide drill-down into the full dataset details.

---

## 14.1 Pre-Run Data Readiness Panel

Before the user starts a backtest, show:

- Exact dataset that will be used.
- Dataset version/fingerprint.
- Coverage period.
- Symbol count.
- Required versus available history.
- Readiness status.
- Blocking errors.
- Warnings.
- Universe definition.
- Survivorship-bias status.
- Point-in-time membership status.
- Adjustment mode.
- Last successful data update.
- Data freshness.
- Clear explanation when the run is `NOT_READY`.

Example:

```text
Dataset Readiness
────────────────────────────────────
Dataset:       Historify Daily
Version:       2026-10-04-abc123
Status:        READY_WITH_WARNINGS

Coverage:      2016-10-03 → 2026-10-04
Symbols:       197
Frequency:     Daily
Adjustment:    Split-adjusted

Warnings:
  ⚠ Universe membership is not point-in-time
  ⚠ Survivorship bias present

Errors:
  None

[View Data Details]     [Run Backtest]
```

If `NOT_READY`:

```text
Dataset Readiness
────────────────────────────────────
Status: NOT_READY

Blocking issue:
  2,479 duplicate SBIN daily observations
  affecting 1,239 sessions

[View Issue Details]

[Run Backtest]  ← disabled
```

---

## 14.2 Backtest Result Data Card

Every Backtest result should display a compact **Data** card containing:

- Dataset name.
- Dataset version.
- Dataset fingerprint.
- Source/provider.
- Frequency.
- Adjustment mode.
- Backtest data start/end dates.
- Latest available session.
- Requested symbol count.
- Loaded symbol count.
- Eligible symbol count.
- Excluded symbol count.
- Warm-up period.
- Data readiness state.
- Data-quality status.
- Major warning badges.

Example:

```text
Backtest Data
────────────────────────────────────
Dataset       Historify Daily
Version       2026-10-04-abc123
Fingerprint   abc123...

Period        Dec 2019 → Sep 2026
Symbols       197 loaded / 195 eligible
Frequency     Daily
Adjustment    Split-adjusted

Quality       READY_WITH_WARNINGS

Warnings
  ⚠ Survivorship bias
  ⚠ Universe is not point-in-time

[View Full Data Report]
```

Do not hide material quality issues behind a single numerical "data quality score".

Explicit issue counts and statuses are more important.

---

## 14.3 Data Details / Drill-Down

The user must be able to inspect:

### Coverage

- First session per symbol.
- Last session per symbol.
- Available session count.
- Missing-session count.
- Coverage gaps.

### Quality

- Duplicate-row count.
- Duplicate-date count.
- Invalid-bar count.
- Zero/abnormal-volume count.
- OHLC integrity failures.
- Affected symbols/dates.

### Eligibility

- Warm-up exclusions.
- Listing-age exclusions.
- Price exclusions.
- Volume exclusions.
- Insufficient-history exclusions.
- Other reason-coded exclusions.

### Dataset

- Source.
- Frequency.
- Adjustment mode.
- Calendar/session convention.
- Universe source.
- Universe version.
- Survivorship status.
- Corporate-action information.

### Revisions

- Dataset revision history.
- Revision timestamp.
- Revision rule.
- Affected symbols/dates.
- Before/after fingerprint.

### Freshness

- Last successful update.
- Expected latest session.
- Actual latest session.
- Missing latest-session data.
- Backfill/update status.

---

## 14.4 Result-to-Dataset Traceability

Every Backtest result must link to the exact dataset snapshot used.

The user should be able to navigate:

```text
Strategy
   ↓
Strategy Version
   ↓
Backtest Run
   ↓
Dataset Snapshot
   ↓
Data Health Report
   ↓
Symbol / Date Details
```

Warnings must remain visible even when the strategy performance looks strong.

If the verdict is `INCONCLUSIVE` because of data quality, the result must identify:

- Which checks failed.
- Severity.
- Affected symbols/dates.
- Why the result cannot be trusted.
- Whether a rerun is recommended after correction.

---

# 15. API / Read Model Requirements

Expose stable read-only dataset information for the GUI.

The frontend must **not reconstruct data-quality logic**.

Required read-model capabilities:

- Dataset metadata.
- Dataset fingerprint/version.
- Readiness state.
- Quality issue summary.
- Coverage summary.
- Symbol-level quality details.
- Revision history.
- Preflight result.
- Run-to-dataset relationship.
- Freshness/update status.

Data-quality computation belongs in the data/backtest layer.

The GUI consumes the resulting read model.

---

# 16. Acceptance Criteria

### AC-01

A mandatory duplicate-data error blocks a backtest before strategy execution.

### AC-02

A warning-only limitation permits execution and is persisted in the run artifact.

### AC-03

A Strategy Version can declare a data contract and preflight validates it.

### AC-04

Every run records an immutable dataset fingerprint/version.

### AC-05

Dataset updates create new versions without changing historical run provenance.

### AC-06

Data revisions support:

- Dry-run.
- Auditability.
- Idempotence.
- Fingerprint change.

### AC-07

Daily ingestion detects duplicate/malformed/missing data before publication.

### AC-08

The GUI shows:

- Coverage.
- Duration.
- Symbol counts.
- Quality state.
- Adjustment mode.
- Universe limitations.
- Freshness.

### AC-09

Users can drill from a Backtest result to the exact dataset and quality details.

### AC-10

The SBIN incident is detectable by preflight before strategy execution.

### AC-11

Existing run-time data-quality checks remain active.

### AC-12

Existing strategy backtests remain regression-tested.

---

# 17. Testing Requirements

Tests must include:

- Unit tests for every data-quality rule.
- Synthetic fixtures for duplicate bars.
- Malformed OHLC.
- Missing sessions.
- Sparse history.
- Revised historical bars.
- Timezone errors.
- Adjustment changes.
- Preflight blocking mandatory failures.
- Backfill failures not becoming the active dataset.
- Data revision dry-run/apply/idempotence/fingerprint behavior.
- Run reproducibility against immutable dataset snapshots.
- API/read-model tests.
- GUI tests for `READY`.
- GUI tests for `READY_WITH_WARNINGS`.
- GUI tests for `NOT_READY`.
- Regression tests for existing strategies.

---

# 18. Design Principles

1. Data quality is a first-class research dependency.
2. Preflight prevents avoidable invalid runs.
3. Run-time validation remains mandatory.
4. Never silently invent or repair data semantics.
5. Never silently mutate data used by an existing run.
6. Material data assumptions must be visible in dataset/run artifacts.
7. Data cleaning belongs in the data layer, not strategy logic.
8. Prefer deterministic, reproducible transformations.
9. Separate data-quality conclusions from strategy-performance conclusions.
10. A strong backtest result must never hide a weak or invalid dataset.

---

# 19. Recommended Implementation Phasing

### Phase 1 — Audit

Audit the current data store and produce the complete data inventory and issue report.

### Phase 2 — Data Quality Model

Define:

- Quality rules.
- Severity.
- Readiness states.
- Dataset fingerprints.
- Dataset revisions.

### Phase 3 — Preflight

Implement:

```text
Dataset
  ↓
Data Contract
  ↓
Preflight
  ↓
READY / WARNING / NOT_READY
  ↓
Backtest
```

### Phase 4 — Daily Backfill

Implement controlled ingestion and validation of daily close data.

### Phase 5 — GUI

Expose:

- Dataset Health.
- Data Readiness.
- Backtest Data Card.
- Full Data Details.
- Dataset revision history.

### Phase 6 — IDEA-027

Only after the dataset is validated:

1. Create/approve clean dataset revision.
2. Validate readiness.
3. Rerun IDEA-027.
4. Reproduce the run.
5. Evaluate strategy evidence.

---

# 20. Immediate Decision

Pause further IDEA-027 performance evaluation until the data audit and minimum readiness mechanism are established.

Preserve the existing invalid run as historical evidence.

Do not use the invalid run as the final strategy verdict.

---

# 21. Questions Claude Must Answer Before Implementation

Before modifying the system, Claude must answer:

1. What is the canonical daily observation when multiple records exist for the same symbol/session?
2. How are timestamps normalized?
3. How is the trading session determined?
4. Which data-quality checks already exist?
5. Which checks should be centralized in preflight?
6. Which material issues currently exist across the full historical store, not only SBIN?
7. Which issues should block all backtests?
8. Which issues should block only specific strategy data contracts?
9. How are adjusted/raw price semantics represented?
10. How are corporate actions detected and versioned?
11. How is point-in-time universe membership represented when unavailable?
12. How does daily backfill detect corrections to historical dates?
13. How are late-arriving observations handled?
14. What is the smallest implementation that provides reliable preflight and GUI transparency without creating a second data architecture?

---

# 22. SBIN Incident Reference

The IDEA-027 incident is the motivating example.

The historical dataset contained multiple SBIN daily observations for the same dates. Some later records differed from the canonical daily observation, and the existing loader could retain a fragment rather than the intended daily record.

The new data-quality system must detect this class of problem **before strategy execution**.

The solution must be generic and must not hard-code SBIN or IDEA-027.

The canonicalization rule for SBIN must be validated against the actual source/data contract before any destructive cleanup is applied.
