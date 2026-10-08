# Backtest Engine Readiness

Implementation status of the capabilities that `research/STRATEGY-ONBOARDING-CHECKLIST.md`
(sections E and F) requires. The checklist is the stable contract; this file is the moving
status and is the only place that records what is built.

A run may claim full evidence-package compliance only when the required capabilities are
implemented and verified. Until then the run record lists what is missing, and the verdict
cannot be EDGE while any P1 item is absent.

Baseline: keyword search of `signal_engine/backtest/` on 2026-10-02 found no MAE/MFE, run
manifest, Monte Carlo or point-in-time universe code. Costs, slippage, walk-forward, regime,
portfolio turnover and benchmark helpers and a random-entry baseline exist in some form and
must be verified before being relied on. Status values: not started, in progress, built,
verified. Update the row, code location, test and date when status changes.

Build plan for the first three P1 steps (spec schema, assumption ledger, run manifest):
`BACKTEST-ENGINE-P1-PLAN.md`. Backtests page requirements and their plan:
`BACKTESTS-UI-TECHNICAL-REQUIREMENTS.md`, `BACKTESTS-UI-PLAN.md`. Canonical lifecycle and
minimum run schema (wins on conflict): `BACKTEST-LIFECYCLE-AND-RUN-SCHEMA.md`.

## Build order progress

The order is in `BACKTESTS-UI-PLAN.md`, "Build order". One step per conversation, each ending
with tests green, ruff clean and a commit.

| Order | Work | Status | Commit |
| --- | --- | --- | --- |
| 1 | Engine P1 step 1: spec v2, profiles, execution resolution | done 2026-10-02 | `27626054d` |
| 2 | Engine P1 step 2: ledger, intake states, freeze, versions (E2) | done 2026-10-02 | `32eb6f00a`, plus `b5f40c0fd` (entry window against the profile; classification planned) |
| 3 | Engine P1 step 3: run folder, manifest, component fingerprints (E1), change classes (E3), rerun (E5), ledger id fix | done 2026-10-02 | `c7b348a51` |
| 4 | E4: derived verdict, promotion, next action, plus `stage` / `research_state` split and strategy classification metadata | done 2026-10-02 | `9d1f3fd43`, plus `668f7b20c` (G2.7 diagnostic) |
| 5 | UI U1, glossary and the classification line (backend read-model, decision panel, promotion block, scope, reproducibility, assumptions, frozen strategy, glossary) | done 2026-10-03 | `15afdb590` |
| 6 | UI U3 (comparison): engine (`backtest/compare.py`), service wiring, compare view | done 2026-10-03 | `df01546ab` |
| 7 | UI U4: version tree, run history grouped by version, strategy list filters, search, related strategies | done 2026-10-03 | `ef3bfd8fc` |
| 8 | UI U2 remainder: validity, data quality, trials, ledger summary and exclusions, cost decomposition, trade-level drill-down with lineage, gate-to-trades links | done 2026-10-04 | see `git log` (feat(backtests-ui): evidence blocks and trade-level drill-down) |
| 9 | Data quality: published verified NSE daily dataset versions, NSE corporate-action layer, run schema 4, official NSE calendar (`7c0665d3b`, `c7b4d5827`), intraday 5m dataset (`32a6a1dd6`) | done 2026-10-05 to 2026-10-06 | see commits |
| 10 | ENGINE_VERSION 1.1: float64 decisions, per-symbol single-pass recorded runs (`1890abb16`); intraday price floor on the NSE raw close, eligibility policy version 2 (`faf00cb02`); statutory broker cost with cost scenarios (`531ae59ee`) | done 2026-10-07 | `1890abb16`, `faf00cb02`, `531ae59ee` |

Engine steps 4 to 6 (plan: `BACKTEST-ENGINE-P1-STEPS-4-6-PLAN.md`), agreed order before the UI build order 8:

| Step | Work | Status | Commit |
| --- | --- | --- | --- |
| 4 | Lineage ledgers, exclusions, cost decomposition, evaluators G0.5, G6.1, G6.5, G1.6 | done 2026-10-03 (recorder overhead measured 1.08 to 1.28 times on a noisy host, hard test bound 1.5; recorder memory is bounded per symbol) | `eba020a86` |
| 5 | Derived calendar and session model, per-run data quality, corporate-action policy, eligibility and universe file, warm-up | done 2026-10-04 | `cca57958e` |
| 6 | Validity checks incl. truncation, trial ledger, evaluators G0.2, G2.5 | done 2026-10-04 | `aa77381ba` |

Findings carried forward from implementation (raised, not designed around):
- Resolved 2026-10-02: the entry window is now validated against the profile's standing time exit (`Profile.entry_window_problem`).
- The frontmatter `status` still drives the registry's spec requirement. The split into `stage` and `research_state`, and the stage transitions, need the promotion decision, so they are scheduled with build order 4 (confirmed by the owner).
- Resolved 2026-10-02 (step 3): each run seals its ledger snapshot (`assumptions.json`) and the working ledger's `next_id` is floored by `ledger.sealed_next_id`, so deleting `assumptions.yaml` cannot restart ids below a sealed one.
- Resolved 2026-10-02 (build order 4): the verdict is calculated per run (`backtest/verdict.py`), with EDGE capped at MARGINAL while P1 readiness rows are unverified. Because the cap would otherwise let a capped run be promoted, promotion is also NOT_READY while readiness rows are missing (a conservative reading of lifecycle 1.6, flagged for the owner).
- Resolved 2026-10-03 (owner decision): minimum-sample gates are counted explicitly. `G2.1` (trade count, with the 30-trade subgroup floor) is mandatory and decides the verdict; `G2.7` (track length) is labelled REPORT in the acceptance criteria, so it is a diagnostic: shown as a reason, never required for the verdict, promotion or the next action (`promotion.DIAGNOSTIC_GATES`). An INCONCLUSIVE verdict from the sample rule now names its gate.
- Owner decision 2026-10-03: promotion requires engine readiness (kept as built).
- Owner decision 2026-10-03: the Backtests page is an approved, narrow exception to the no-core-changes rule: `services/backtest_results_service.py`, `blueprints/backtest_results.py`, `frontend/src` and their tests, limited to read-model, API and presentation. Strategy execution and evaluation logic stays in `signal_engine/`. Build orders 6 to 8 may use it.
- Frontmatter `status` and `verdict` are removed as hand-written keys. The registry emits them as DERIVED fields (status from stage, research state and runs; verdict from the latest run's calculated verdict) so the existing Backtests page, REGISTRY.md and site keep working unchanged. Build order 5 now reads `stage`, `research_state`, `run_verdict` and classification directly (see the exception above). The list page filters, search and related strategies are build order 7.
- The dirty-tree check counts only result-affecting paths (Python outside tests, config.yaml, uv.lock, pyproject.toml, research profiles). Docs, generated registry files, run outputs and frontend files do not block a run.
- Strategy classification (`STRATEGY-CATEGORIZATION-UI-REQUIREMENT.md`) is planned in `BACKTESTS-UI-PLAN.md` and built in build order 4 (data) and 5 and 7 (UI). The legacy frontmatter `category` is left unchanged; retiring it is an open decision.
- Profile-sourced values (`profile.*`) are Medium materiality, not High, because the author selects the profile by name. A High default would force a confirmation on every idea.
- The service module `services/backtest_results_service.py` imported the deleted `signal_engine.research.spec` from build order 1 until build order 5: the repo-wide search then covered only `signal_engine/`, and the test runs covered only `signal_engine/tests`. Fixed 2026-10-03; the core `test/test_backtest_results_*` files are part of the checks from now on.
- The legacy frontmatter `tags` field (read by the Backtests service) and the new `strategy_tags` overlap in purpose. Both are kept; retire the legacy `tags` and the legacy `category` together once the new filters exist (build order 7).
- After engine steps 4 to 6, `EDGE` is still unreachable in a real run, for a reason separate from the readiness rows: 16 gates have no evaluator and keep the verdict at MARGINAL because promotion-required gates that are not evaluated hold it there. They are G0.1 (same logic in every mode), G0.7 (test quality), G2.4 (probability of backtest overfitting), G4.1 (parameter perturbation, HARD), G4.2 (latency), G4.3 (bar resolution), G4.8 (event days), G5.1 (Monte Carlo), G5.3 (sizing), G5.4 (capacity), G5.5 (margin), G6.3 (circuits), G6.4 (tick size, HARD), G6.6, G6.7 and G6.8 (operational). Each needs evidence the engine does not yet produce: the P2 backlog (parameter neighbourhood, Monte Carlo, capacity, event calendar) plus manual or operational evidence. Marking readiness rows verified lifts the cap; it does not make EDGE reachable.
- The legacy-field retirement (`category`, `tags`) is done: the registry rejects them with a hint to run `intake migrate-classification`.
- Trade drill-down read-model limits (build order 8): `get_run_trades` refuses a trades file above 64 MB or 300,000 rows and the lineage lookup refuses oversized ledgers, because the web process is a single eventlet worker and a native Parquet decode would freeze it (measured: about 0.6 s cold and 10 ms warm for 300k rows). New runs write ledgers with row-group statistics so a lineage lookup reads only the matching row groups; runs recorded before that decode whole files. The in-sample and out-of-sample subsets are by entry date against `oos_start` and do NOT apply the purge and embargo exclusions the tearsheet applies (the page says so).
- `signal_engine/tests/test_tracker_close_detection.py::TestGuard2Timeout::test_guard2_waits_when_young_and_status_unknown` is a wall-clock timing test that failed once when the machine was busy and passes alone; it is unrelated to this work.
- Build order 6 design: the comparison is computed in `signal_engine/backtest/compare.py` (diffs, change classes, common and incremental recomputation with the tearsheet's own `headline_metrics`, per-view curves) and the service only calls it. `signal_engine/backtest/__init__.py` is now lazy so importing a lean backtest module no longer loads the harness. The first compare call in the web process still loads scipy and adds roughly 85 MB once; owner decision 2026-10-03: keep it in the web process for now. Add a separate-process design only if profiling shows the worker's memory footprint becoming a problem, concurrent comparisons causing memory pressure, or comparison work materially affecting request latency.
- Type-check note: in `frontend/`, `npx tsc --noEmit -p tsconfig.json` only follows project references and checks nothing. Use `npx tsc --noEmit -p tsconfig.app.json`. Pre-existing, unrelated frontend test failures: `src/config/navigation.test.ts` expects 10 nav items (config has 12); StrategyBuilder tests time out under full-suite load but pass alone.
- Comparison curves for the common and incremental views are computed by the backend (`curves_common`, `curves_incremental`), re-based to zero at the window start, so the UI does no date filtering of its own. The common and incremental spans are lists of disjoint ranges, because one run can extend the other on both sides.

## P1

| Capability | Status | Code | Tests | Verified |
| --- | --- | --- | --- | --- |
| Spec schema split: strategy intent vs resolved execution configuration vs deployment profile; field class and per-field provenance | built 2026-10-02 (field class, spec v2, profile and execution resolution with a source per value; per-field provenance in the ledger is step 2) | `research/intake/{fields,spec,profiles,execution}.py`, `research/profiles/`, `research/ideas/_template/spec.yaml` | `tests/test_intake_spec.py`, `test_intake_profiles.py`, `test_intake_execution.py`, `test_spec_gate.py` | |
| Spec validation: entry, exit, direction, timeframe, holding period, stop, signal evaluation, order submission, fill model, indicator source, reference prices, portfolio scope | built 2026-10-02 (validity vs intake tagging; absent timing is inferred and recorded in step 2) | `research/intake/spec.py` (`validate_spec`) | `tests/test_intake_spec.py` | |
| Assumption ledger with materiality and confirmation; intake states | built 2026-10-02 (ledger with merge that keeps ids and resets confirmation on a changed value; computed intake state; freeze with the structural ledger-id invariant; intake CLI; `record_run` gated on freeze) | `research/intake/{ledger,state,freeze,cli}.py`, `research/intake/__main__.py`, `backtest/record_run.py` (freeze call) | `tests/test_intake_ledger.py`, `test_intake_state.py`, `test_intake_freeze.py`, `test_intake_cli.py`, `test_spec_gate.py::TestFreezeGate` | |
| Strategy versions: append-only `versions.yaml`, automatic and branch labels, current and promoted pointers, rules, parameters and strategy fingerprints | built 2026-10-02 (UI plan E2; component fingerprints other than rules, parameters and strategy are step 3) | `research/intake/{versions,fingerprints}.py`, `research/build_registry.py` (`_validate_version_pointers`) | `tests/test_intake_versions.py`, `test_intake_fingerprints.py`, `test_intake_freeze.py::TestVersionRegistration`, `test_spec_gate.py::TestRegistryVersionPointers` | |
| Point-in-time universe membership and per-date eligibility | built 2026-10-04 with a stated limitation: per-date eligibility is data-driven (listing age, traded, volume, price floor) and ineligible symbol-sessions are NOT_ELIGIBLE exclusions; membership is NOT point-in-time (`membership_source = current_symbol_master`, `survivorship_bias = true` on every run; G4.7 reports it and never passes). Cannot honestly be marked verified until a historical F&O membership source is acquired | `backtest/eligibility.py`, `backtest/record_run.py`, `research/intake/{fields,execution}.py` | `tests/test_backtest_eligibility.py`, `test_data_session_integration.py` | |
| Direction validation (no shorts in swing or positional) | built 2026-10-02 (spec-level; swing engine enforcement is existing behaviour) | `research/intake/spec.py` (`_check_classification`) | `tests/test_intake_spec.py::TestDirectionAndProduct` | |
| Exchange calendar with `calendar_version`; session model (exchange, strategy, broker cutoff, force-flat) | built 2026-10-04; superseded 2026-10-06 for published-dataset runs, which use the official NSE calendar (`research/reference/nse_calendar.yaml`, commit `c7b4d5827`). The derived calendar (80% of active symbols per month, short sessions flagged; `calendar_source = derived_from_market_data`) remains only for runs without a published dataset | `backtest/session.py`, `run_artifact.py`, `reproduce.py` (`calendar_unchanged`) | `tests/test_backtest_session.py`, `test_data_session_integration.py` | |
| Deployment profile: MIS force-flat 15:00, CNC carry-forward | built 2026-10-02 (declared and mapped to `RunConfig`; engine enforcement of CNC carry-forward is confirmed in step 5) | `research/intake/profiles.py`, `research/profiles/flattrade-nse-cash.yaml`, `research/intake/execution.py` | `tests/test_intake_profiles.py`, `tests/test_intake_execution.py` | |
| Corporate-action policy and `data_quality.json`; warm-up derived from indicators; dataset identity and version | built 2026-10-04; updated 2026-10-05 to 2026-10-07: recorded runs read only published, verified dataset versions (`nse-daily-20261005-002`, `nse-intraday-5m-20261006-001`) and record schema 4 (preflight, symbol counts, exclusions). The NSE corporate-action layer is `backtest/data_readiness/corporate_actions.py` with `research/reference/corporate_action_overrides.yaml`. Original 2026-10-04 text follows (per-run `data_quality.json` with exact drop counts; policy stated in the manifest: intraday split-day heuristic, dividends, bonuses and rights NOT detected; warm-up applied as WARMUP exclusions; the audit samples at most 60 symbols and records which; this describes the pre-published-dataset policy) | `backtest/{data_collect,dq_run,data}.py`, `engine.py`, `swing_engine.py`, `run_artifact.py` | `tests/test_data_collect.py`, `test_dq_run.py`, `test_data_session_integration.py` | |
| Trade, order and fill, signal, exclusion ledgers with lineage ids (signal, order, fill, position, trade) | built 2026-10-03 (recorder attached to the headline run only; deterministic ids; every engine skip is a counted exclusion with a code; `evidence/{signals,orders,fills,exclusions}.parquet`; reconciliation; evaluators G0.5, G6.1, G6.5, G1.6; fills record the next-bar open and never an invented price: slippage is a cost component) | `backtest/ledgers.py`, `engine.py`, `swing_engine.py`, `harness.py`, `trades_io.py`, `run_artifact.py`, `record_run.py`, `evaluation.py` | `tests/test_backtest_ledgers.py`, `test_evidence_ledgers.py` | |
| Run manifest and run folder layout; immutable artifacts retained and addressable | built 2026-10-02 (7-file run folder committed atomically and read-only, manifest with the 8 component fingerprints and reproducibility flags, schema 3 flat record, dirty-tree refusal and `--allow-dirty`, change classes, `--rerun`, `--reproduce`, registry check of folder and manifest hash; derived verdict, promotion and next action are written as null until build order 4) | `backtest/{artifacts,manifest,trades_io,fingerprints,change_class,run_artifact,reproduce}.py`, `backtest/record_run.py`, `research/readiness.py`, `research/intake/freeze.py` (`from_run_folder`), `research/build_registry.py` (schema 3 checks) | `tests/test_run_artifacts.py`, `test_manifest.py`, `test_trades_io.py`, `test_backtest_fingerprints.py`, `test_change_class.py`, `test_run_artifact_write.py`, `test_reproduce.py`, `test_rerun.py`, `test_readiness.py`, `test_registry_v3.py` | |
| Per-component cost decomposition per trade | built 2026-10-03 (brokerage, STT, exchange, GST, SEBI, stamp and slippage in bps of entry notional per trade, summing to `modeled_cost_bps`; the headline `cost_bps` stays the engine's flat figure). Updated 2026-10-07 (`531ae59ee`): the primary cost is now the deployment profile broker's statutory per-trade model, and each record reports `primary`, `generic_discount_broker`, `flat_<n>bps` and `legacy_flat` scenarios under `cost_model.scenarios`; only `primary` drives gates) | `backtest/costs.py` (`decompose_trades`), `backtest/trades_io.py` | `tests/test_cost_components.py`, `test_trades_io.py` | |
| Validity vs performance failure handling in the gate runner | built 2026-10-04 (real `validity` block at record time: data quality, config, lineage and a look-ahead truncation check with session-boundary and mid-session cuts; a failure makes the verdict INCONCLUSIVE and performance gates are not evaluated; `checks_run` shows what actually ran, a skipped check is never counted as passed; the truncation covers single-symbol, frame-derived inputs only, not regime or cross-sectional inputs) | `backtest/validity.py`, `backtest/truncation.py`, `backtest/run_artifact.py`, `backtest/record_run.py` | `tests/test_validity.py`, `test_truncation.py`, `test_validity_trials_integration.py` | |
| Trial counting under the acceptance-criteria definition | built 2026-10-04 (append-only `trials.yaml` per idea; one material trial per distinct (rules, params, execution, cost) tuple, written right after a successful execution; dataset, range, engine, profile and universe-size changes and re-runs are not trials; `--external-trials N --reason` and `intake trials add` for variants searched elsewhere; the default of 1 is gone; the registry flags a ledger that shrank) | `research/intake/trials.py`, `research/intake/cli.py`, `backtest/record_run.py`, `research/build_registry.py` | `tests/test_intake_trials.py`, `test_intake_trials_cli.py`, `test_validity_trials_integration.py` | |

## P2

| Capability | Status | Code | Tests | Verified |
| --- | --- | --- | --- | --- |
| MAE and MFE per trade | not started (2026-10-08: only a standalone exploratory diagnostic exists, from IDEA-032. It measures every executable signal of an early-displacement strategy, not the recorded trades, and is not called by `record_run`, the run record, the evaluation or the Backtests page. The engine `Trade` carries no excursion fields.) | `backtest/mfe_mae.py`, `backtest/mfe_mae_report.py` (diagnostic only) | `tests/test_mfe_mae.py` | |
| Exposure and turnover | not started | | | |
| Regime by strategy table (baseline trend/range, high/low volatility; optional India VIX, sector, index trend) | not started | | | |
| Market benchmark; controlled random-entry null with opportunity-set preservation | built (cross-sectional selection null for intraday runs only: engine replay of the strategy's stop and target rules on every symbol eligible in the same session, per-replay statutory cost, report only, opt-in through `Strategy.null_entry`; swing runs and the market benchmark are not started; 2026-10-08: no real strategy implements `null_entry` yet, so no recorded run has produced the block and it is checked on synthetic data only) | `backtest/baseline_cross_sectional.py`, `backtest/strategies/base.py` | `tests/test_baseline_cross_sectional.py`, `tests/test_record_run.py` | |
| Structured failure diagnosis; research states | built (next action and failure diagnosis from the gate table; research states in the lifecycle; verify the diagnosis wording before marking verified) | `backtest/next_action.py`, `research/intake/lifecycle.py` | | |
| Parameter neighbourhood; cost stress; symbol and year contribution | not started | | | |
| Monte Carlo with stored ruin definition | not started | | | |
| Data-quality report section | built (Backtests page block) | `frontend/src/components/backtest-results/DataQualityBlock.tsx`, `backtest/dq_run.py` | | |
| Backtest Scope summary generated from frozen config | built (Backtests page block) | `frontend/src/components/backtest-results/BacktestScope.tsx` | `BacktestScope.test.tsx` | |
| Position ledger (only if partial exits or scaling exist); sector and correlation exposure | not started | | | |
| Stagnation evidence collection | not started | | | |
| Re-entry trigger context in the execution contract | not started | | | |

## P3

| Capability | Status | Code | Tests | Verified |
| --- | --- | --- | --- | --- |
| Capacity and participation analysis | not started | | | |
| Contribution concentration; advanced Monte Carlo diagnostics | not started | | | |
| Automated stagnation detection; advanced portfolio analytics | not started | | | |

## Tests the P0/P1 work must include

- No material assumption is silently introduced (every non-source value has a ledger entry).
- Strategy semantics and execution configuration are distinct; deployment profile drives broker and product constraints.
- Signal, order, fill, position and trade lineage is reconstructable from ids alone.
- Validity failures yield INCONCLUSIVE, not NO_EDGE.
- A run is reproducible from the retained immutable artifacts.
- Trials and strategy versions are auditable.
