# Backtests UI Plan

Implementation plan for `BACKTESTS-UI-TECHNICAL-REQUIREMENTS.md`: strategy lineage, run
comparison and dataset evolution (Requirement 1), and verdict, promotion and decision
support (Requirement 2). It sits on top of `BACKTEST-ENGINE-P1-PLAN.md`. Most of what the
page has to show is evidence the engine must first record, so this plan is mostly about
ordering. Status lives only in `BACKTEST-ENGINE-READINESS.md`.

Written 2026-10-02. Nothing here is implemented yet.

## Starting point

The page exists (`/backtest-results`, `frontend/src/pages/BacktestResults*.tsx`,
`blueprints/backtest_results.py`, `services/backtest_results_service.py`).

| Requirement | Today | Gap |
| --- | --- | --- |
| Strategy details (1.12) | `StrategyDefinition.tsx` renders the idea's current `spec.yaml` | It shows the *current* spec, not the spec frozen with the selected run. After any edit, an older run displays rules it was never run with |
| Strategy versions (1.2, 1.4, 1.10) | None. Runs link by `parent_run_id`; `research/evolution.py` and `build_registry.lineage` walk run chains | No version object, no fingerprint, no branches, no current or promoted version |
| Immutable artifact (1.3) | Flat `runs/<run_id>.json`, never overwritten | Covered by engine P1 step 3 (run folder and manifest) |
| Dataset identity (1.2, 1.7) | `data.as_of` and a universe label | Covered by engine P1 step 3 (fingerprint plus cutoff) |
| Change classification (1.5) | `_comparability` flags a few like-for-like differences as free text | No per-component fingerprints and no classification |
| Comparison (1.13 to 1.15) | `compare_runs`: metric deltas, parameter diff, R equity overlay, warnings | No strategy, scope, dataset, execution or cost diff. No overlap warning, no common or incremental period, no drawdown or monthly comparison |
| Reproducibility (1.8) | None | Engine P1 step 3 provides the data. The UI shows it |
| Backtest scope (1.9) | Scattered across `RunAssumptions.tsx` and `DataCoverage.tsx` | No single scope block generated from the frozen configuration |
| Verdict (2.2 to 2.4) | `VerdictBanner.tsx` shows `verdict` from `idea.md` frontmatter, which the researcher types by hand | It must be derived per run from the gate scorecard, with reasons |
| Gate evidence (2.7, 2.9) | `EvaluationTable.tsx` renders `evaluation.evaluate()`: gate rows with status, hard flag, threshold, value and evidence, plus `hard_failed` | The rows exist. There is no derived verdict, no stage grouping and no drill-down from verdict to trade evidence |
| Promotion (2.5, 2.6) | None | No stage-aware promotion block |
| Next action (2.8) | None | |
| Metrics (2.10) | Tearsheet metrics, cost model, position caps, regimes, robustness, monthly grid | Partly there. Exposure and turnover are engine P2 rows. Cost decomposition is engine P1 step 4 |
| Data quality (2.12) | `DataCoverage.tsx` (coverage only) | `data_quality.json` is engine P1 step 5 |
| Assumptions (2.13) | `RunAssumptions.tsx` (engine defaults as text) | The sealed assumption ledger from engine P1 step 2 |
| Glossary (2.11, 2.15) | None | |

The registry was wiped on 2026-10-02, so there are no old runs to migrate. The page must
still render a schema v2 record, showing "not captured" wherever v3 data is absent.

## Reconciling the requirements with the existing contracts

The requirements say to follow the existing evaluation framework and architecture where
they differ. These decisions apply that rule.

1. **Verdict vocabulary stays EDGE, MARGINAL, NO_EDGE, INCONCLUSIVE.** These are defined in
   `STRATEGY-ACCEPTANCE-CRITERIA.md`. The example taxonomy maps onto it. FAIL is NO_EDGE.
   MARGINAL and INCONCLUSIVE keep their names. PROMISING and ROBUST are not verdicts. They
   are the *promotion state* of an EDGE run: EDGE with later stages still to run reads as
   "ready for the next stage" (PROMISING), and EDGE with every gate for its stage passed
   reads as ROBUST for that stage. The glossary explains this mapping (decision U1 below).
2. **Verdict and research state stay separate.** The run verdict is derived and never typed.
   The idea's frontmatter `verdict` is renamed in meaning to the researcher's *research
   state* (checklist G2: Promising, Needs More Evidence, ..., Rejected). The banner shows
   both, labelled. That satisfies 2.2 without taking the researcher's disposition away.
3. **Dataset changes are detected, not prevented.** A fingerprint mismatch between two runs
   is shown as DATASET_CHANGE. Nothing rewrites an old run (engine P1 step 3).
4. **The verdict cap applies.** While P1 capabilities are missing, an EDGE result is capped at
   MARGINAL with the reason "engine readiness". A dirty-tree run is shown as not
   reproducible and not promotion-eligible (engine plan, Decisions 1 and 2).
5. **A data-quality WARNING that affects validity caps the verdict at INCONCLUSIVE.** This is
   the requirement's own rule (2.12), and it matches the checklist's validity-failure rule.

## Engine-side additions

These pieces go in `signal_engine/` and are not in the engine P1 plan as written. They are
small, and the UI's Phase 1 cannot work without them.

### E1. Component fingerprints (amends engine P1 step 3)

The engine plan proposed two hashes (`strategy_hash`, `config_hash`). Change classification
needs one fingerprint per component instead, so the manifest carries:

| Fingerprint | Covers | A change means |
| --- | --- | --- |
| `rules_fingerprint` | Intent minus parameters (entry, exit, stop, target, filters, sessions, timing, portfolio, sizing intent) | STRATEGY_CHANGE |
| `params_fingerprint` | `parameters` | PARAMETER_CHANGE |
| `strategy_fingerprint` | Both of the above | New strategy version |
| `execution_fingerprint` | Resolved execution configuration minus costs and universe | EXECUTION_CHANGE |
| `cost_fingerprint` | Cost model, fee schedule version, slippage | COST_CHANGE |
| `universe_fingerprint` | Universe definition and eligibility policy (the symbol list until step 5) | UNIVERSE_CHANGE |
| `profile_fingerprint` | Deployment profile content | DEPLOYMENT_PROFILE_CHANGE |
| `dataset_fingerprint` | Data fingerprint at `data_as_of` (engine plan, decision 7) | DATASET_CHANGE |
| `period` | `backtest_start`, `backtest_end` | DATA_RANGE_CHANGE |
| `engine` | `engine_version`, `code_commit` | ENGINE_CHANGE |

`config_hash` is dropped. The ten values are the run's identity. Each is SHA-256 over
canonical JSON, and they are computed in a new `backtest/fingerprints.py`.

### E2. Strategy versions

- `research/ideas/<idea>/versions.yaml` is an append-only list of `{version, strategy_fingerprint, parent, created_ist, note}`.
- At freeze, a known `strategy_fingerprint` reuses its version. An unknown one creates the next version. Its parent defaults to the idea's `current_version` and can be overridden with `--parent-version`. A branch label is optional (`--version v3b`). Otherwise the label is the next integer.
- Immutability is structural: changing a rule changes the fingerprint, which makes a new version. An existing entry is never rewritten, and a test asserts the file only grows.
- The idea's frontmatter gains `current_version` (set with `intake set-current IDEA-n v3b`) and `promoted_version` (set only when the idea's status moves to paper or live). The latest run is derived from the runs and never stored (1.11).
- The run's `strategy/strategy.json` records `{strategy_id, version, strategy_fingerprint, parent_version}`, and the frozen `spec.yaml` sits next to it. The Strategy Details panel reads this, not the idea's current spec, which fixes the stale-spec gap above.
- Files: `research/intake/versions.py`, called from `freeze()` (engine P1 step 2).

### E3. Change classification

`backtest/change_class.py` provides `classify(manifest_a, manifest_b) -> list[ChangeClass]`.
It returns every component whose fingerprint differs, in the order of the table in E1. An
empty diff returns `RESEARCH_RERUN`. The requirement's single "run type" is the first
entry, and the UI shows all of them. At record time, the run is classified against its
`--parent` run (or the previous run of the same version), and the result is stored in the
flat record as `change_classes`. Classification at compare time uses the same function for
any pair of runs.

### E4. Derived verdict, reasons, promotion and next action

This brings the verdict half of engine step 6 forward. The gate runner's trial counting
and its full validity-versus-performance handling stay in step 6.

- `backtest/verdict.py` provides `derive(evaluation, *, validity, data_quality, missing_capabilities, code_dirty) -> Verdict`, with `{verdict, primary_failed_gate, reasons: [{text, gate, status, evidence_ref}], caps_applied}`. Rules in order:
  1. Any validity failure, or a data-quality warning that affects validity, gives INCONCLUSIVE.
  2. Too little valid evidence (the G2 minimum-sample gates are not met) gives INCONCLUSIVE.
  3. Any HARD performance failure gives NO_EDGE.
  4. Positive evidence with a promotion requirement failed or not evaluated gives MARGINAL.
  5. Everything else gives EDGE.
  6. Last, the readiness cap is applied (EDGE becomes MARGINAL).

  The rule thresholds are the acceptance criteria's. `verdict.py` reads gate statuses and never re-derives a metric.
- `backtest/promotion.py` maps gates to stages, using the acceptance criteria's stage table: Backtest is G0 to G6, Paper is G7 to G8, Live is G9.
  - A gate's status is PASS, FAIL, NOT_RUN or NOT_APPLICABLE. It comes from the evaluation, where `not_evaluated` becomes NOT_RUN.
  - A stage's decision is READY FOR NEXT STAGE, NOT READY or BLOCKED. A later stage is BLOCKED while an earlier stage is not READY.
  - A dirty or non-reproducible run is NOT READY with that reason.
- `backtest/next_action.py` is a deterministic rule table from the primary failed or not-run gate to an action and its reason (for example, G3.x not run gives "Run OOS validation"). The rules are data, so they can be tested. The output is decision support. It never changes the verdict.
- `report/report.json` (engine P1 step 3) stores the verdict, reasons, promotion and next action. The flat record carries a copy for the list page.

### E5. Re-run with new data (1.16)

`record_run --rerun <run_id> [--end YYYY-MM-DD]` reruns the frozen version from that run's
folder against the current data. The strategy version is unchanged, the parent is that
run, and the result is classified by E3 (typically DATA_RANGE_CHANGE, or also
DATASET_CHANGE if history moved).

## UI work

The phases follow the requirements' phases (2.19). Each lists its engine prerequisites.

### Phase U1: foundation

The engine prerequisites are P1 steps 1 to 3 and E1 to E4.

| Item | Where |
| --- | --- |
| Decision block (verdict, promotion, why, next action) at the top, per 2.14 Level 1 and section 5 | `VerdictBanner.tsx` is reworked into `DecisionPanel.tsx`. It reads the run's derived verdict, not the frontmatter. The research state is shown beside it, labelled |
| Promotion block, stage-aware, with failed and not-run items expandable to their gate row | New `PromotionGate.tsx`, grouping `EvaluationTable` rows by stage |
| Strategy details from the frozen run spec, with version and fingerprint (short form, full on hover) | `StrategyDefinition.tsx` reads `strategy/` from the run artifact |
| Backtest Scope block (market, universe, timeframe, strategy session, period, execution, costs, profile, dataset). The exchange session, strategy session and broker cutoff are shown as separate lines | New `BacktestScope.tsx`. It merges and replaces the scope parts of `RunAssumptions.tsx` and `DataCoverage.tsx` |
| Reproducibility checklist from the manifest (each item captured or not), plus the last reproduction attempt if one exists | New `Reproducibility.tsx` |
| Resolved assumptions, grouped by source (user, derived, platform default, external, unresolved), with confirmation state | `RunAssumptions.tsx` reads `assumptions.json` |
| Run identity in the header: strategy, version, run id, change classes | Page header |

Service changes in `backtest_results_service.py`:
- `get_idea_results` returns each run's `strategy`, `scope`, `reproducibility`, `verdict`, `promotion`, `next_action` and `change_classes`, read from the run artifact.
- A new `GET /api/backtest-results/<idea_id>/runs/<run_id>` returns the full detail for one run. It reads the run folder, and its paths are validated against the idea's runs directory.

### Phase U2: results and diagnostics

Much of this exists already. The new parts depend on engine steps 4 and 5.

- Performance with before-cost and after-cost columns side by side (2.10). Cost decomposition by component arrives after engine step 4.
- Data-quality block with PASS or WARNING and its verdict impact (2.12), after engine step 5.
- Evidence drill-down: verdict to gate to metric to analysis to trades (2.9). Up to step 3 the trade level opens the retained `evidence/trades.parquet`, filtered by the gate's subset (for example the OOS split). After step 4 it links signal, order and fill ids.
- Metric tooltips (2.11) from the glossary module below.

### Phase U3: comparison

The engine prerequisites are step 3 (retained trades) and E1 and E3.

- `compare_runs` is extended with:
  - Change classes (E3).
  - A strategy diff from the two frozen specs, split into rules and parameters.
  - A scope diff, a dataset diff (fingerprint and `data_as_of`), an execution diff and a cost diff, all from the manifests.
  - Performance deltas (already there). They are never labelled better or worse.
- Overlap-aware comparison (1.14):
  - When the periods differ, the response carries a warning naming the extra span.
  - It also returns three views: full period, common period (both runs' metrics recomputed from `evidence/trades.parquet` restricted to the intersection) and incremental period (the newer run's trades outside the intersection).
  - The metrics are recomputed with the same `metrics.py` functions the tearsheet uses, never a second implementation.
- Curve comparison (1.15): equity, drawdown and monthly returns, with the common and incremental periods marked. Rolling performance and trade-distribution overlays go in the same component.
- `CompareView.tsx` is reworked into sections in this order: change classes, strategy, scope, dataset, execution, cost, performance, curves.

### Phase U4: research evolution

The engine prerequisite is E2.

- Version tree per idea: versions with their parents and branches, runs under each version, and each run's verdict. Current, latest and promoted are marked separately (1.11).
- A text or tree rendering is the first cut. The visual graph is in the nice-to-have list (2.17).
- `RunHistory.tsx` is grouped by version.

### Glossary (any phase, no prerequisites)

- `frontend/src/lib/backtestGlossary.ts` is one typed module holding the 2.15 terms, plus EDGE, NO_EDGE, MARGINAL, INCONCLUSIVE, research state and the PROMISING/ROBUST mapping from decision 1.
- Definitions follow the acceptance criteria's wording wherever both define a term.
- A help-button drawer (`GlossaryDrawer.tsx`) and an `InfoTip` used by metric labels. Nothing renders permanently on the page.

## Strategy classification (additive requirement, 2026-10-02)

Requirement: `STRATEGY-CATEGORIZATION-UI-REQUIREMENT.md`. It adds a lightweight discovery layer
(category, tags, filters, search, related strategies). It was not in the earlier plans: the
word "classification" in E3 means run change classes, which is unrelated.

**Where each field lives.** A logical Strategy is the idea. Nothing is duplicated.

| Field | Lives in | Why |
| --- | --- | --- |
| `strategy_category` | `idea.md` frontmatter. Allowed values are in `research/taxonomy.yaml` (Trend Following, Mean Reversion, Breakout, Momentum, Market Structure, Statistical / Quantitative, Grid, Arbitrage, Options, Other), so the list is extended by editing one file | Strategy level, mutable, never part of a run |
| `strategy_tags` | `idea.md` frontmatter, a list of free text. Duplicates are ignored regardless of case | Discovery only |
| `trading_style`, `direction`, `timeframe` | The frozen spec (`style`, `direction`, `timeframe`). Shown from the selected run's `strategy/` artifact | These change trading logic, so a change is a new version. They are already structured fields |
| `market` | The deployment profile's `market` (in the run's `execution/config.json`) | Already recorded. A new field would duplicate it |
| `research_state` | `idea.md` frontmatter (build order 4) | Researcher disposition, separate from classification |

**Rules.**
- Category and tags are never read by `freeze()`, never enter a fingerprint, the manifest or any run artifact. A test freezes an idea, edits its category and tags, freezes again and asserts the same fingerprints, the same version and no new `versions.yaml` entry (requirement sections 8 and 9).
- A run page shows category and tags from the idea's current metadata, labelled as strategy-level, and style, direction, timeframe and market from that run's frozen artifacts. Editing a tag therefore never changes what an old run displays about how it was run.
- No category-dependent execution, verdict or calculation. No `strategy_family` entity. Related strategies are found by shared category and tags.
- The legacy frontmatter `category` (for example `intraday-breakout`) is the library category the registry and the current page filter use. It mixes style and type. It is left unchanged, and `strategy_category` is added beside it. Whether to retire the legacy field is an open finding for the owner. (Resolved: the legacy `category` and `tags` are retired; the registry rejects them and `intake migrate-classification` converts them.)

**Build.**
- Engine side (build order 4, with the other `idea.md` metadata): `research/taxonomy.yaml`, validation in `build_registry` (category must be in the taxonomy, tags must be a list of text), the fields carried into `registry.json`, exposure in `backtest_results_service`, and `intake classify IDEA-n --category X --tag a --tag b`.
- UI: the classification line and chips in the run header (build order 5, U1). The strategy list filters (category, style, direction, market, timeframe, tags, research state, verdict), search by name, category and tag, and the related-strategies grouping (build order 7, with the version tree).

## Build order

Engine steps run first, because the UI reads what they write.

| Order | Work | Unlocks |
| --- | --- | --- |
| 1 | Engine P1 step 1 (spec v2, profiles) | |
| 2 | Engine P1 step 2 (ledger, intake states, freeze) + E2 (versions) | Strategy versions |
| 3 | Engine P1 step 3 (run folder, manifest) with E1 fingerprints, + E3 (classification) + E5 (rerun) | Immutable artifact, dataset identity, reproducibility |
| 4 | E4 (derived verdict, promotion, next action) + `idea.md` metadata: `stage` / `research_state` split, strategy classification (category, tags, taxonomy) | Decision support, classification data |
| 5 | UI U1 + glossary + classification line in the run header | Definition of Done items 1 to 7 and 11 to 18 |
| 6 | UI U3 | Definition of Done items 8 to 10 |
| 7 | UI U4 + strategy list filters, search and related strategies | Lineage, branches, discovery |
| 8 | UI U2 remainder, as engine steps 4 and 5 land | Cost decomposition, data quality, trade-level drill-down |

Each order closes with tests green before the next starts:
- Python: `uv run pytest`.
- Frontend: Vitest and Biome locally (the frontend tests also run in CI), then `npm run build` to check the page renders.

The frontend's built `dist/` is not committed from a feature branch. The CI job commits it on `main`.

## Tests

The tests are written first.

- **E1.** Each fingerprint changes when, and only when, its component changes. A parameter edit moves `params_fingerprint` and `strategy_fingerprint`, and nothing else.
- **E2.** A known fingerprint reuses its version, and an unknown one appends a new version with the right parent. A branch label is accepted. Any rewrite of `versions.yaml` is refused. A date-range-only rerun keeps the version (Definition of Done item 6).
- **E3.**
  - One test per change class.
  - Several changes at once return all of them in table order.
  - An identical rerun returns RESEARCH_RERUN.
  - A dataset correction with the same period returns DATASET_CHANGE (Definition of Done item 7).
- **E4.**
  - Each verdict rule has a fixture, with the reasons naming their gates.
  - The readiness cap and data-quality cap are applied and named in `caps_applied`.
  - Stage blocking cascades.
  - A dirty run is NOT READY.
  - Every next-action rule fires on its gate.
  - The verdict never comes from the frontmatter.
- **Service.**
  - The run-detail endpoint reads the run folder, and its path checks reject traversal.
  - A v2 record renders with "not captured" fields.
  - The compare response carries the overlap warning and common-period metrics that match a hand-computed fixture.
- **Frontend.**
  - The decision panel renders the verdict, reasons, promotion and next action.
  - The promotion items expand to their evidence.
  - Strategy details show the frozen spec, not the current one.
  - The glossary drawer opens and closes and is absent from the default render.
  - The compare view shows the overlap warning when the periods differ.
- **Classification.**
  - A category outside the taxonomy fails the registry check, and adding it to `taxonomy.yaml` makes it pass.
  - Tags are normalised and de-duplicated, and a non-list fails.
  - Editing category or tags leaves the strategy fingerprint, the version and `versions.yaml` unchanged, and an identical re-run is still RESEARCH_RERUN.
  - Filters combine (category and tag and research state), search matches name, category and tag, and a strategy with no category shows as Other.
  - A run page shows frozen style, direction, timeframe and market from the run, and category and tags from the idea.
- **Definition of Done check.** A test walks the 20 items in section 4 of the requirements against a fixture run and fails, naming the item, if any field it needs is missing from the API response.

## Decisions (confirmed by the user 2026-10-02)

- **U1. Verdict words.** `run_verdict` is one of EDGE, MARGINAL, NO_EDGE or INCONCLUSIVE, calculated and immutable for a completed run. PROMISING, ROBUST and similar words are promotion stages or research states, never verdicts.
- **U2. Research state.** The hand-set frontmatter `verdict` becomes `research_state`, the researcher's workflow state. Its values are defined in `BACKTEST-LIFECYCLE-AND-RUN-SCHEMA.md`. `run_verdict` is always calculated.
- **U3. Version labels.** v1, v2, ... are generated automatically. An optional human-readable branch label (v3a) is allowed. Users never manage the numbering.
- **U4. E4 is built early.** The initial verdict is based only on the gates and evidence already implemented:
  - A gate the engine cannot evaluate yet is NOT_RUN, never PASS, and is listed as a reason.
  - A promotion requirement that is NOT_RUN keeps the verdict at or below MARGINAL.
  - EDGE is therefore unreachable until the required gates exist, which is the honest result, and is consistent with the readiness cap.

- **U5. Strategy classification (2026-10-02).** Added by `STRATEGY-CATEGORIZATION-UI-REQUIREMENT.md` and mapped above. Category and tags live on the idea, outside every fingerprint. The legacy `category` field stays.
- **U6. Stage and research state (2026-10-02).** The `stage` / `research_state` frontmatter split is built in build order 4, with the promotion decision it depends on. `research_state` stays separate from the calculated run verdict.

Planning stops here. The canonical lifecycle and the minimum run schema are in
`BACKTEST-LIFECYCLE-AND-RUN-SCHEMA.md`. Remaining capabilities (portfolio analytics,
benchmarks, audit trail, Monte Carlo, sensitivity, walk-forward, data-quality diagnostics,
experiment management, exports) are added incrementally and do not block implementation.
