# Backtest Lifecycle and Minimum Run Schema

The final planning checkpoint, agreed 2026-10-02. It fixes two things the implementation
builds against:
- the canonical lifecycle (every state and who or what moves it);
- the exact minimum run artifact.

Everything else is in `BACKTEST-ENGINE-P1-PLAN.md` and `BACKTESTS-UI-PLAN.md`. A conflict
between those plans and this file is resolved in favour of this file. A change here is an
implementation finding, not a new requirement. Schema v2 stays documented in
`research/README.md`; this file defines schema 3.

## 1. Lifecycle

Five independent state dimensions. They are never merged into one field.

| Dimension | Belongs to | Values | Set by | Stored |
| --- | --- | --- | --- | --- |
| Intake state | idea (current spec and ledger) | DRAFT, NEEDS_CLARIFICATION, ASSUMPTION_REVIEW, BACKTEST_READY, BACKTEST_BLOCKED | Computed | Never stored; `intake_state()` |
| Stage | idea | inbox, backtest, paper, live, retired | Researcher, gated by evidence | `idea.md` frontmatter `stage` |
| Research state | idea | active, promising, needs_more_evidence, weak_evidence, evidence_against_edge, shelved, rejected | Researcher | `idea.md` frontmatter `research_state` |
| Run verdict | run | EDGE, MARGINAL, NO_EDGE, INCONCLUSIVE | Computed at record time | In the run; immutable |
| Promotion decision | run, per stage | READY_FOR_NEXT_STAGE, NOT_READY, BLOCKED | Computed at record time | In the run; immutable |

Strategy versions have no state. A version exists, is immutable, and is pointed at by
`current_version` and `promoted_version` in the idea's frontmatter. The latest run is
derived and never stored.

The existing frontmatter fields are replaced:
- `status` (inbox, evaluating, backtested, paper, live, rejected, shelved) is split into `stage` and `research_state`. Rejected and shelved are research states, not stages.
- `verdict` is removed. It is now the run verdict, which is calculated, or the research state, which is set by hand.

The registry was wiped, so nothing has to be migrated.

### 1.1 Intake state (computed per idea)

```
DRAFT --(coordinator lists questions)--> NEEDS_CLARIFICATION --(answers edit spec)--> DRAFT
DRAFT --(all BLOCKERs resolved)--> ASSUMPTION_REVIEW --(every High inferred entry confirmed)--> BACKTEST_READY
any --(blocked_reason set)--> BACKTEST_BLOCKED --(blocked_reason cleared)--> recomputed
```

The rules are checked in this order. The first one that matches decides the state.
1. `blocked_reason` is set: BACKTEST_BLOCKED.
2. Any BLOCKER is unresolved: NEEDS_CLARIFICATION if `open_questions` is non-empty, otherwise DRAFT.
3. Any High entry has `source` other than `user` and `user_confirmed: false`: ASSUMPTION_REVIEW.
4. Otherwise: BACKTEST_READY.

Freeze is allowed only in BACKTEST_READY. Freeze resolves or creates the strategy version.

### 1.2 Stage (per idea)

```
inbox -> backtest -> paper -> live -> retired
```

| Transition | Allowed when |
| --- | --- |
| inbox -> backtest | The idea has a `spec.yaml` (intake can then run) |
| backtest -> paper | `promoted_version` has a run that is (a) completed, (b) `reproducible: true`, (c) `code_dirty: false`, and (d) has `promotion.stages.backtest.decision == READY_FOR_NEXT_STAGE`. The transition records that run's id as `promoted_run_id` |
| paper -> live | G7 and G8 pass, per the acceptance criteria. This is outside this build; the transition is refused until the gates exist |
| live -> retired | Per G9. The reason is recorded |
| any -> previous stage | Always allowed; the reason is recorded |

The CLI enforces the transitions (`intake stage IDEA-n paper`), and the registry check
rejects a frontmatter stage that the evidence does not support.

### 1.3 Research state (per idea)

`research_state` is the researcher's judgement, set freely, with two rules:
- `shelved` requires `revisit_when`.
- `rejected` or `shelved` blocks any stage move forward until the state changes.

It never affects any run's verdict.

### 1.4 Run (per run)

A run is either written completely, or not written at all. The run folder is built in a
temporary directory and renamed into place atomically. There are no on-disk states such as
"running" or "failed".

| Outcome | What is written |
| --- | --- |
| The engine completes | The run, with its derived verdict |
| A validity failure is detected (look-ahead, invalid data or configuration, malformed spec past intake, impossible execution) | The run, with verdict INCONCLUSIVE and `validity.failures` listed. No performance gates are evaluated |
| A crash or an exception | Nothing. It is logged to `log/errors.jsonl`, and no run id is consumed |

Once written, nothing in a run changes. A later reproduction attempt is stored outside the
run folder, at `runs/<run_id>.reproductions/<timestamp_ist>.json`.

### 1.5 Run verdict derivation (initial implementation, decision U4)

The inputs are the evaluation rows (`pass`, `fail`, `report`, `not_evaluated`,
`not_applicable`), validity, data quality, `missing_capabilities` and `code_dirty`. The rules
are applied in order, and the first match decides:

1. A validity failure, or a data-quality warning marked `affects_validity`: INCONCLUSIVE.
2. The minimum-sample gates (G2) are `fail` or `not_evaluated`: INCONCLUSIVE.
3. Any `hard` gate is `fail`: NO_EDGE.
4. Any promotion-required gate for the Backtest stage is `fail` or `not_evaluated`: MARGINAL.
5. Otherwise: EDGE.
6. Caps, applied last and each recorded in `caps_applied`. If `missing_capabilities` is non-empty, EDGE is capped to MARGINAL.

Further rules:
- `not_evaluated` is shown as NOT_RUN and is never counted as a pass.
- `report` rows are informational and never decide the verdict.
- With the gates implemented today, rule 4 or the cap keeps every run at or below MARGINAL. That is the intended, honest result.

`code_dirty` does not change the verdict. It sets the promotion decision to NOT_READY with
the reason "dirty working tree".

### 1.6 Promotion decision (per run, per stage)

The stage-to-gate mapping is: Backtest is G0 to G6, Paper is G7 to G8, Live is G9.

- A stage is READY_FOR_NEXT_STAGE when every promotion-required gate in it is `pass`, and the run is clean and reproducible.
- A stage is NOT_READY when any of those gates is `fail` or NOT_RUN, or the run is dirty.
- Every stage after a stage that is not ready is BLOCKED.

The next action comes from the first NOT_RUN or `fail` gate of the earliest stage that is not
ready, looked up in the rule table in `backtest/next_action.py`.

### 1.7 Strategy classification is not a state

`strategy_category` and `strategy_tags` (see `STRATEGY-CATEGORIZATION-UI-REQUIREMENT.md` and the
classification section of `BACKTESTS-UI-PLAN.md`) are idea-level discovery metadata. They are not
a sixth lifecycle dimension: nothing is computed from them, they gate nothing, and they are never
copied into a run, a manifest or a fingerprint. Changing them creates no version and no run.

## 2. Minimum run artifact

The folder is the source of truth. The flat record is an index, and its content is copied
from the folder.

```
research/ideas/<idea>/runs/
  <run_id>.json                    flat record, schema 3
  <run_id>/
    manifest.json                  required
    assumptions.json               required
    strategy/spec.yaml             required (frozen intent, canonical)
    strategy/strategy.json         required
    execution/config.json          required (resolved execution config + deployment profile)
    evidence/trades.parquet        required (may have zero rows)
    report/report.json             required
```

`data_quality.json` and `evidence/{signals,orders,fills,exclusions}.parquet` are added by
engine steps 4 and 5, and `analysis/` is added later. Until then they are named in
`missing_capabilities`. A required file that is missing makes the run invalid, and the
registry check fails.

`run_id` has the format `YYYY-MM-DD-<slug>-v<n>`, which is today's format. Runs are
numbered per idea, starting at `v1`.

### 2.1 `manifest.json`

Every key is required. A value may be `null` only where the table marks it as nullable, and
every `null` has a matching `missing_capabilities` entry.

| Key | Type | Notes |
| --- | --- | --- |
| `schema` | int | 3 |
| `run_id` | str | |
| `created_ist` | str | ISO 8601 with +05:30 |
| `idea_id` | str | |
| `strategy` | object | `{version, parent_version, branch_label (nullable)}` |
| `fingerprints` | object | `{rules, params, strategy, execution, cost, universe, profile, dataset}`, each a sha256 hex string |
| `period` | object | `{start, end}`, ISO dates, IST sessions |
| `dataset` | object | `{id, source, interval, data_as_of, symbol_count, fingerprint, corporate_action_policy (nullable)}` |
| `universe` | object | `{definition, symbols_sha256, version (nullable)}` |
| `calendar_version` | str, nullable | null until step 5 |
| `deployment_profile` | object | `{name, product, sha256}` |
| `session` | object | `{exchange (nullable until step 5), strategy_window, broker_cutoff (nullable), force_flat (nullable)}` |
| `engine` | object | `{engine_version, code_commit, code_dirty, dirty_paths}` |
| `environment` | object | `{python, packages: {name: version}}` for the backtest-relevant packages from `uv.lock` |
| `random` | object | `{seed, monte_carlo (nullable)}` |
| `parent_run_id` | str, nullable | |
| `change_classes` | list[str] | Versus the parent run, or versus the previous run of the same version. `RESEARCH_RERUN` when nothing differs; `[]` for the first run of an idea |
| `missing_capabilities` | list[str] | Readiness P1 rows not `verified` at record time |
| `reproducible` | bool | `false` if `code_dirty` or any reproducibility item below is missing |
| `reproducibility` | object | `{strategy_version, strategy_fingerprint, dataset_fingerprint, execution_config, deployment_profile, engine_version, code_commit, resolved_assumptions}`, each a bool for whether it was captured |
| `artifacts` | object | `{relative_path: sha256}` for every other file in the folder |

### 2.2 `strategy/strategy.json`

`{strategy_id, name, version, parent_version, branch_label, strategy_fingerprint,
rules_fingerprint, params_fingerprint, style, direction, timeframe, product}`

### 2.3 `assumptions.json`

`{sealed_ist, entries: [{id, field, section, value, source, confidence, materiality, reason,
user_confirmed}]}`

- `source` is one of user, derived, platform_default, market_data, broker_config or inferred.
- The UI's "externally sourced" group is market_data plus broker_config.

### 2.4 `execution/config.json`

`{resolved: {<field>: {value, ledger_id}}, profile: {...full profile content...}}`

Every leaf of `resolved` carries a `ledger_id`. This is the structural invariant from engine
plan decision 1.

### 2.5 `evidence/trades.parquet`

One row per trade. These are the columns the engine produces today, which are enough for
common-period comparison and the drill-down:

`trade_id, symbol, side, entry_time, exit_time, entry_price, exit_price, quantity,
exit_reason, gross_r, net_r, gross_bps, net_bps, cost_bps`

All timestamps are tz-aware IST.

Engine step 4 adds the lineage ids and cost components as further columns. Adding columns
is allowed under schema 3. Removing or renaming one is a schema change.

### 2.6 `report/report.json`

| Key | Content |
| --- | --- |
| `schema` | 3 |
| `metrics` | The current headline block (`trades`, `gross_bps`, `net_bps`, `win_rate`, `sharpe`, `max_dd_pct`, `t_stat`, `profit_factor`, `expectancy_r`, ...) plus `net_return_pct`, `before_cost` and `after_cost` |
| `series` | `{equity_r, drawdown_r, monthly}` |
| `evaluation` | The output of `evaluation.evaluate()`, unchanged |
| `validity` | `{ok, failures: [{check, detail}]}` |
| `data_quality` | `{status: PASS \| WARNING \| NOT_RUN, affects_validity, items}`. NOT_RUN until step 5 |
| `verdict` | `{run_verdict, primary_failed_gate, reasons: [{text, gate, status, evidence_ref}], caps_applied: [str]}` |
| `promotion` | `{stages: {backtest, paper, live: {decision, gates: [{gate, status}], reasons}}, next_stage}` |
| `next_action` | `{action, reason, gate}` |
| `n_trials` | The trial count |
| `notes` | Free text |

Existing optional blocks (`position_caps`, `regimes`, `cost_model`, `split`, `baseline`) are
carried over unchanged when present.

### 2.7 Flat record `<run_id>.json` (index)

| Field | Content |
| --- | --- |
| `schema` | 3 |
| Identity | `run_id`, `date`, `idea_id`, `artifact_dir` (`<run_id>/`), `manifest_sha256` |
| Strategy | `strategy_version`, `strategy_fingerprint` |
| Lineage | `parent_run_id`, `change_classes`, `change_note` |
| Reproducibility | `reproducible`, `code_dirty`, `code_version` (the existing key, kept for the current readers) |
| Data and costs | `data`, `period`, `costs` (the existing v2 shapes, so `build_registry` and the service keep working) |
| Results | `metrics`, `series` |
| Decision | `run_verdict`, `promotion_decision` (the Backtest stage), `next_action` |
| Other | `n_trials`, `params` |

`build_registry` accepts schemas 2 and 3. For a schema 3 record it checks that
`artifact_dir` exists and that its `manifest.json` hashes to `manifest_sha256`.

### 2.8 Immutability rules

- A run folder and its flat record are never modified after the atomic rename. Files are
  marked read-only. `verify_artifacts` rehashes them against the manifest, and the registry
  check runs it.
- A completed run's `run_verdict` and `promotion` never change. A later engine change shows
  up as a new run classified ENGINE_CHANGE.
- `versions.yaml` is append-only.
