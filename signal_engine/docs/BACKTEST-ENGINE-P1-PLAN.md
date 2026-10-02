# Backtest Engine P1 Plan: Steps 1 to 3

Implementation plan for the first three P1 build steps in `BACKTEST-ENGINE-READINESS.md`:
spec schema and validation, the assumption ledger with intake states, and the run artifact
with its manifest. The contract is `research/STRATEGY-ONBOARDING-CHECKLIST.md` (v4, frozen
2026-10-02). This document says how to build it. Status lives only in the readiness doc,
so this plan does not track progress.

Steps 4 to 6 (lineage ledgers, data and session layer, gate runner) are out of scope. Where
steps 1 to 3 must leave room for them, the plan says so.

Written 2026-10-02. The open questions were resolved the same day (see Decisions at the end). Nothing here is implemented yet.

## Starting point

Measured against the code on 2026-10-02:

| Area | Today | Gap |
| --- | --- | --- |
| Spec | `research/spec.py` checks one flat `spec.yaml` (18 required fields, vague-word regex, needs-a-number check, long-only for swing). Template at `research/ideas/_template/spec.yaml` | No separation of intent, execution configuration and deployment profile. No field classes, no provenance, no timing triple, no portfolio scope, timeframe is a loose regex rather than the checklist's 1m/5m/15m/1h/1d |
| Assumptions | None. `record_run` sets defaults in code (16/24 bps, 5m/1d bars, OOS fraction) and writes a few into `notes` as free text | No ledger, no materiality, no confirmation, no intake states |
| Run record | `record_run.py` writes one flat `runs/<run_id>.json` (schema v2, built by `tearsheet.build_run_record`), atomically and never overwritten. Readers: `build_registry._load_runs`, `evolution.py`, `export.py`, `services/backtest_results_service.py` | No run folder, no manifest, no strategy hash, no dataset identity, no retained trade table, no way to reproduce a run |
| Engine inputs | `RunConfig` already carries `mis_squareoff_min` (15:00) and `squareoff_margin_min`; loaders accept `end=` | Profile values are dataclass defaults, not read from a declared deployment profile |

The research registry was wiped on 2026-10-02 and no idea has a spec yet. That means the
spec schema can be replaced outright instead of kept backward compatible. The only spec in
the repo is the template.

## Design decisions

These decisions shape all three steps.

1. **The ledger is the source and the resolved configuration is a view of it.** Every
   resolved value (user-stated, derived, platform default, market data, broker config,
   inferred) is a ledger entry. The resolver builds the frozen configuration *only* from
   ledger entries, so "no value enters a run without a ledger entry" holds by construction
   and the test checks that structure rather than chasing a convention. Values the user
   stated are entries too (`source: user`), which also gives every field its provenance.
2. **Three input files, three owners.**
   - `ideas/<idea>/spec.yaml`: strategy intent, written by the author.
   - `ideas/<idea>/execution.yaml`: optional and usually absent. It holds overrides only, such as a custom slippage figure with a reason. The platform resolves everything else.
   - `research/profiles/<name>.yaml`: deployment profiles (first one: `flattrade-nse-cash`), selected by name from the spec. A profile is shared and never copied into an idea.
3. **The intake state is computed, never stored.** `intake_state(spec, ledger)` derives the
   state deterministically. The ledger file stores only what cannot be derived: open
   questions (Needs Clarification) and an explicit `blocked_reason` (Backtest Blocked).
   A stored state could drift from the files it describes. A computed one cannot.
4. **Two hashes, not one.** `strategy_hash` is SHA-256 of the canonical JSON of the *intent*
   only, and identifies the strategy version. `config_hash` covers intent, resolved
   execution configuration and profile, and identifies the exact run setup. A profile
   switch changes `config_hash` but not `strategy_hash`. That matches the checklist: a new
   profile reruns the affected gates without becoming a new strategy version. Step 6's
   trial counter uses both.
   **Amended 2026-10-02 by `BACKTESTS-UI-PLAN.md` E1:** `config_hash` is replaced by one
   fingerprint per component (rules, parameters, execution, cost, universe, profile,
   dataset, period, engine), so a run can be classified by what changed. `strategy_hash`
   is named `strategy_fingerprint` and is still rules plus parameters.
5. **The run folder sits beside the flat record and does not replace it.** The flat record
   stays at `runs/<run_id>.json` and becomes schema v3, gaining `artifact_dir`,
   `manifest_sha256`, `strategy_hash`, `config_hash` and `missing_capabilities`. The
   evidence package goes in `runs/<run_id>/`. Every current reader globs `runs/*.json`, so a
   subfolder is invisible to them and nothing downstream breaks. The flat record is the
   index, and the folder holds the evidence.
6. **The readiness doc is machine-read.** The P1 table in `BACKTEST-ENGINE-READINESS.md` is
   parsed at run time. Every row not marked `verified` lands in the manifest's
   `missing_capabilities`, and the verdict is capped. There is one source of truth and no
   duplicate Python constant.
7. **The dataset identity is a fingerprint plus a cutoff, not a copy.** Copying Historify
   for each run is unaffordable. The manifest records `data_as_of` (the loaders already
   accept `end=`) and a fingerprint: SHA-256 over sorted `(symbol, rows, first_ts, last_ts,
   sum(close))` up to that cutoff, computed in DuckDB. A backfill that only appends keeps
   the fingerprint stable. A correction of history changes it, and reproduction then fails
   loudly instead of silently giving different numbers.

## Step 1: spec schema and validation

### Spec v2 shape (intent only)

```yaml
schema_version: 2
name: ...
source: {kind: video | article | pdf | screenshot | own_words, ref: _inbox/<file>}
profile: flattrade-nse-cash          # deployment profile name
style: intraday                      # intraday | swing | positional
direction: long_only                 # long_only | short_only | both
timeframe: 5m                        # 1m | 5m | 15m | 1h | 1d
product: MIS                         # MIS | CNC (derivable from style; ledger if derived)
entry:  [conditions with numbers]
exit:   [conditions with numbers]
stop: [...] | none
take_profit: [...] | none
holding_period: 12 bars | same_session
indicators: [{name: EMA, period: 20, source: close}]
parameters: {name: value}
timing:                              # optional; inferred values go to the ledger
  signal_evaluation: bar_close       # bar_close | bar_open | intrabar | tick
  order_submission: signal_close
  fill_model: next_bar_open
reference_prices: {stop: entry_fill} # optional, only where ambiguous
filters: none
session_window: {entry: "09:20-14:30"} | none
re_entry: {allowed: false}           # trigger, max_per_symbol_day only when allowed
sizing: {risk_per_trade_pct: 1.0, capital_per_trade: 100000}
portfolio:
  mode: single_position              # single_position | multi_position
  scope: per_symbol                  # global | per_symbol (single_position only)
  # multi_position: max_concurrent, max_trades_per_day, capital_per_position,
  #   max_exposure, selection, conflict
order_types: optional (A5)
disclosure: {prior_variants: 0, chart_peeking: false, destination: python}
rationale: optional
kill_criterion: optional
```

### Files

| File | Purpose |
| --- | --- |
| `research/intake/__init__.py` | Package. `research/spec.py` is removed and its callers (`record_run`, `build_registry`) import from here |
| `research/intake/fields.py` | The field catalog: one frozen record per field with its section (intent, execution or profile), class (BLOCKER, DERIVABLE, DEFAULTABLE, DISCLOSURE or OPTIONAL), default materiality, allowed values and whether `none` is accepted. Validation, the ledger and intake states all read this one table |
| `research/intake/spec.py` | Load and validate the intent. It carries over the vague-word and needs-a-number checks from the current `spec.py` |
| `research/intake/profiles.py` + `research/profiles/flattrade-nse-cash.yaml` | Load and validate a deployment profile. MIS: force-flat 15:00, standing time exit 14:45, latest exit-bar close 14:55, plus the exit convention label. CNC: carry-forward allowed, no force-flat |
| `research/intake/execution.py` | Resolve the execution configuration: platform defaults (slippage 4 bps per side, cost model from the broker fee schedule, warm-up from the longest indicator period, bar size from timeframe), overrides from `execution.yaml`, and the profile mapped to `RunConfig` fields (`mis_squareoff_min`, `squareoff_margin_min`). Each value is returned as a ledger entry (step 2) |
| `research/ideas/_template/spec.yaml` | Rewritten to v2 |

### Validation rules

Validation is a pure function: it returns every problem, never only the first, and each
problem is tagged *validity* (malformed) or *intake* (missing or ambiguous).

- Swing or positional with any direction other than `long_only` is a configuration error, not an intake gap.
- `single_position` without `scope` is an intake gap, and the inferred scope is a High-materiality assumption.
- `global` scope with ranking, sector or correlation fields is a validity error, because those fields do not apply.
- `per_symbol` with any `multi_position` field is a validity error: the spec should declare `multi_position`.
- `multi_position` must carry max concurrent positions, trades per day, capital per position, max exposure, selection and conflict rules.
- `timing` fields that are absent are not an error. Step 2 infers them and records each one, and the inference is High materiality when the entry text does not imply it.
- `MIS` with `swing` or `positional`, or `CNC` with `intraday`, is a validity error.
- The profile must exist and must support the spec's product.
- `re_entry.allowed: false` with trigger fields present is a validity error.
- An indicator without a source is DERIVABLE when a sensible default exists (close). It is a BLOCKER only when it is ambiguous, for example VWAP anchoring.
- An MIS entry window may not end after the profile's standing time exit (14:45 for `flattrade-nse-cash`). This is a profile-level rule (`Profile.entry_window_problem`), not a strategy rule, and a violation is a validity error on `session_window.entry` (added 2026-10-02).

### Tests (written first)

`signal_engine/tests/test_intake_spec.py`, `test_intake_profiles.py` and
`test_intake_execution.py`:

- A complete v2 spec validates clean, and the template spec, filled in, validates clean.
- Each rule above has one failing fixture that asserts the exact problem text and its tag.
- Shorts in swing and in positional are rejected. Both directions are allowed for intraday.
- A missing BLOCKER yields an intake problem, and a malformed value yields a validity problem.
- A profile loads and maps to `RunConfig`, and an unknown profile name is rejected.
- A strategy-intent field placed in `execution.yaml` is rejected, so the sections cannot leak into each other.

## Step 2: assumption ledger and intake states

### Ledger entry

```yaml
id: A-004
field: timing.signal_evaluation
section: intent                  # intent | execution | profile
value: bar_close
source: inferred                 # user | derived | platform_default | market_data | broker_config | inferred
confidence: medium               # high | medium | low
materiality: high                # low | medium | high (default from fields.py, may be raised)
reason: source says "close above EMA" with no intrabar wording
user_confirmed: false
```

The working ledger is `ideas/<idea>/assumptions.yaml`, and it is editable until freeze. Ids
are stable: re-resolving keeps an existing entry's id and its confirmation unless the value
changes. A changed value resets `user_confirmed`, so a confirmation never carries over to
a value the user did not see.

### Files

| File | Purpose |
| --- | --- |
| `research/intake/ledger.py` | `Assumption` (frozen dataclass), load and save, `resolve(spec, execution_overrides, profile) -> Ledger`, and the merge with an existing ledger that keeps ids and confirmations |
| `research/intake/state.py` | `intake_state(spec, ledger) -> IntakeState` plus the reasons. The order of checks: Backtest Blocked (explicit `blocked_reason`), Draft or Needs Clarification (any BLOCKER unresolved; Needs Clarification when `open_questions` is non-empty), Assumption Review (any High entry with `source` not `user` and `user_confirmed: false`), and otherwise Backtest Ready |
| `research/intake/freeze.py` | `freeze(idea_folder) -> FrozenSpec`. It refuses unless the state is Backtest Ready. It returns the canonical intent, the resolved configuration (built only from the ledger), the profile, the sealed ledger, `strategy_hash`, `config_hash` and the unconfirmed Medium entries (for the verdict notes, rule C2) |
| `research/intake/__main__.py` | A small CLI. `check IDEA-n` prints the state, open questions and unconfirmed High entries. `confirm IDEA-n A-004 [A-005...]` confirms entries. `resolve IDEA-n` writes or refreshes `assumptions.yaml` |

`record_run.record_idea` replaces `spec.check_idea` with `freeze()`. A run of a strategy that
is not Backtest Ready is refused before anything executes. This is the same position the
spec gate holds today.

The current engine defaults that `record_run` sets in code (cost bps, bar size, OOS
fraction, embargo, slippage) move into the resolver as `platform_default` entries. The
constants stay where they are and the resolver reads them, so nothing is redefined.

### Tests (written first)

`signal_engine/tests/test_intake_ledger.py`, `test_intake_state.py` and `test_intake_freeze.py`:

- **The structural invariant.** Walk every leaf of the frozen resolved configuration and assert that each maps to exactly one ledger id. The test also adds a field to the configuration with no ledger entry and asserts that `freeze` raises.
- Each intake state is reached by a minimal fixture, and the transitions in section B hold (Draft to Needs Clarification to Draft, then Assumption Review to Backtest Ready).
- `freeze` refuses while any High inferred entry is unconfirmed, and it succeeds once that entry is confirmed.
- A changed value resets confirmation, and an unchanged re-resolve keeps ids and confirmations.
- `strategy_hash` is stable under key reordering and YAML formatting. It changes on any intent change and does not change on a profile switch. `config_hash` changes on a profile switch.
- Unconfirmed Medium entries are surfaced by `freeze` for the verdict notes.
- `record_idea` refuses a non-ready idea without executing (the execute stub is never called).

## Step 3: run artifact and manifest

### Layout produced in step 3

```
runs/<run_id>.json              flat record, schema v3 (index, read by the existing pages)
runs/<run_id>/
  manifest.json
  assumptions.json              sealed ledger
  strategy/spec.yaml            canonical intent as frozen
  strategy/strategy.json        strategy_hash, strategy_version, parent
  execution/config.json         resolved execution configuration + profile
  evidence/trades.parquet       the Trade list as the engine returned it
  report/report.json            the schema v3 record (same content as the flat file)
```

`data_quality.json`, `execution/universe.parquet`, `evidence/signals|orders|exclusions` and
`analysis/` are added by steps 4 and 5. Until then the manifest lists them under
`missing_capabilities` and does not omit them silently.

### Manifest fields (E7)

`run_id`; `strategy` (id, version, `strategy_hash`, `config_hash`); `engine_version` and
`code_commit` (`git_version`), plus `code_dirty` (bool) and the dirty paths; `dataset` (id
`historify`, interval, `data_as_of`, fingerprint, symbol count); `calendar_version` and
`universe_version` (null until step 5, and listed as missing); `deployment_profile` (name and
content hash); the backtest parameters; the execution, cost, slippage, sizing and portfolio
models; `warmup_bars`; Monte Carlo seed, settings and ruin definition (null until built);
the environment (Python version and key package versions from `uv.lock`); the run
timestamp in IST; and `artifacts` (relative path to SHA-256 for every retained file). The
manifest is written last, after every file it hashes.

### Files

| File | Purpose |
| --- | --- |
| `backtest/artifacts.py` | `RunFolder`: build the folder in a temporary sibling directory, then rename it into place atomically. It refuses if the target exists, and it marks files read-only after writing. The same never-overwrite rule as `write_run` |
| `backtest/manifest.py` | `build_manifest(...)`, `dataset_fingerprint(symbols, interval, as_of)` (DuckDB), and `verify_artifacts(folder)` (rehash every artifact against the manifest) |
| `backtest/reproduce.py` | `reproduce(run_folder) -> ReproductionReport`. It reloads the frozen intent, configuration and profile from the folder, checks the code commit and the dataset fingerprint, reruns with `end=data_as_of`, and compares the trades table row for row against `evidence/trades.parquet`. CLI: `python -m signal_engine.backtest.record_run --reproduce <run_id>` |
| `research/readiness.py` | Parse the P1 and P2 tables of `BACKTEST-ENGINE-READINESS.md` into rows. `missing_p1()` feeds the manifest |
| `backtest/record_run.py` | It writes the folder, then the flat v3 record that points to it. `execute_backtest` takes the frozen configuration instead of in-code defaults |
| `backtest/tearsheet.py` | Schema v3 fields. The verdict is capped while `missing_capabilities` is non-empty (Decision 1) |
| `research/build_registry.py` | It accepts schema v3, and its check verifies that `artifact_dir` exists and `manifest_sha256` matches |

### Reproducibility rules

- A run made from a dirty working tree is refused, unless `--allow-dirty` is given. That flag records `code_dirty: true`, and such a run is non-reproducible and ineligible for promotion: the gate runner (step 6) must refuse to promote it, and the verdict notes say so (Decision 2).
- `data_as_of` is the last session in the loaded data, and reproduction loads with `end=data_as_of`.
- A fingerprint mismatch or commit mismatch makes reproduction report "not reproducible: <reason>". It never reports a silent pass.

### Tests (written first)

`signal_engine/tests/test_run_artifacts.py`, `test_manifest.py` and `test_reproduce.py`, all on
synthetic frames and a temporary DuckDB, never the live Historify store:

- The folder has the stated layout, every artifact's hash matches the manifest, and the manifest is valid JSON with every E7 key present (with null plus a `missing_capabilities` entry where a value is not built yet).
- Writing a run id that exists raises, a crash mid-write leaves no partial folder, and artifacts are read-only.
- **Reproduction.** Record a run on synthetic data, then reproduce it from the folder alone. The trades are identical. Append bars after `data_as_of` and reproduction still passes. Alter a historical bar and reproduction fails with a fingerprint reason. Alter the code commit and it fails with a commit reason.
- `verify_artifacts` detects a tampered file.
- The readiness parser reads the current doc. Marking a row `verified` removes it from `missing_p1()`, and a malformed table raises.
- The flat v3 record is still read by `build_registry`, `evolution` and `backtest_results_service`, with existing reader tests run against a v3 fixture.
- While P1 capabilities are missing, an EDGE-qualifying synthetic result is capped and names the missing rows.

## Ledger id uniqueness (added 2026-10-02, built in step 3)

Ledger ids must never collide with ids that an older run already cites.
- Every run seals its own ledger snapshot (`assumptions.json`, including `next_id`), so a reference is always read as `(run_id, ledger_id)` against that snapshot, and an id can never be reinterpreted by a later ledger.
- The working ledger's `next_id` is seeded from the highest id across the sealed snapshots of the idea's existing runs when `assumptions.yaml` is missing or older. Deleting the working file therefore cannot restart numbering below an id a run has used.
- Tests: delete `assumptions.yaml` after a run and re-resolve, and assert new ids are above every id in the run's snapshot. A run's snapshot is unchanged by later edits to the working ledger.

## Build order and checkpoints

Each step finishes with its tests green, `uv run ruff check`, the full `signal_engine/tests`
suite passing, and the readiness doc rows moved to `built` with code and test locations.
Rows are marked `verified` only after review. Suggested commits are one per step.

| Order | Delivers | Readiness rows touched |
| --- | --- | --- |
| 1 | `research/intake/{fields,spec,profiles,execution}.py`, the profile YAML, the v2 template | Spec schema split; spec validation; direction validation; deployment profile (declared and mapped. Engine enforcement of CNC carry-forward is confirmed in step 5) |
| 2 | `research/intake/{ledger,state,freeze,__main__}.py`, `record_run` gated on freeze | Assumption ledger and intake states |
| 3 | `backtest/{artifacts,manifest,reproduce}.py`, `research/readiness.py`, schema v3 | Run manifest and run folder layout |

The UI plan (`BACKTESTS-UI-PLAN.md`) adds work to steps 2 and 3: strategy versions (E2)
with step 2, and component fingerprints, change classification and rerun (E1, E3, E5)
with step 3. Derived verdict, promotion and next action (E4) follow step 3.

Steps 1 and 2 touch only `research/` plus the gate call in `record_run`. Step 3 is the first
step to change what a run writes. None of the three changes `engine.py` or
`swing_engine.py`. That is step 4.

## What steps 4 to 6 inherit

- The resolved configuration and ledger ids are what the step 4 signal ledger cites for
  its indicator parameters and timing.
- `evidence/` and the manifest's `artifacts` map are where the step 4 Parquet ledgers land.
  No layout change is needed.
- The profile, already mapped to `RunConfig`, is what step 5's session model replaces with
  a proper exchange session, strategy session, broker cutoff and force-flat stack.
- `strategy_hash`, `config_hash` and the frozen ledger are the inputs the step 6 trial
  counter needs, under the acceptance-criteria trial definition.

## Decisions (confirmed by the user 2026-10-02)

1. **Verdict cap while P1 is incomplete.** EDGE is capped at MARGINAL, with the reason
   "engine readiness: <missing rows>". INCONCLUSIVE stays reserved for invalid or
   insufficient evidence, not incomplete engine capability.
2. **Dirty working tree.** Refused by default. `--allow-dirty` is for development runs only:
   the run records `code_dirty: true` and the dirty paths, is marked non-reproducible, and is
   ineligible for promotion. Step 3 tests: a dirty tree without the flag raises before
   anything executes; with the flag the manifest and flat record carry `code_dirty: true`
   and `promotion_eligible: false`.
3. **Older-idea exemption dropped.** `SPEC_REQUIRED_FROM_ID` is removed. Every idea needs a
   valid v2 spec before it is backtested, and the registry check demands one for every idea
   past the inbox.
4. **`research/spec.py` deleted in step 1.** One canonical specification format
   (`research/intake/`); no v1 path is kept. `test_research_spec.py` and `test_spec_gate.py`
   are rewritten against the v2 schema rather than kept for v1.
5. **Entry window against the profile (2026-10-02).** Built as a profile-level validity rule, not a strategy rule (see Step 1 validation rules).
6. **Ledger ids (2026-10-02).** Handled in step 3 by sealing a snapshot with each run and seeding `next_id` from it (section above).
7. **"Fully settled" means material assumptions are resolved.** Freeze needs every High-materiality inferred value confirmed. Medium and Low entries are recorded and surfaced, never required to be confirmed one by one.
