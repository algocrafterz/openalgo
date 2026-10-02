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

## P1

| Capability | Status | Code | Tests | Verified |
| --- | --- | --- | --- | --- |
| Spec schema split: strategy intent vs resolved execution configuration vs deployment profile; field class and per-field provenance | built 2026-10-02 (field class, spec v2, profile and execution resolution with a source per value; per-field provenance in the ledger is step 2) | `research/intake/{fields,spec,profiles,execution}.py`, `research/profiles/`, `research/ideas/_template/spec.yaml` | `tests/test_intake_spec.py`, `test_intake_profiles.py`, `test_intake_execution.py`, `test_spec_gate.py` | |
| Spec validation: entry, exit, direction, timeframe, holding period, stop, signal evaluation, order submission, fill model, indicator source, reference prices, portfolio scope | built 2026-10-02 (validity vs intake tagging; absent timing is inferred and recorded in step 2) | `research/intake/spec.py` (`validate_spec`) | `tests/test_intake_spec.py` | |
| Assumption ledger with materiality and confirmation; intake states | not started | | | |
| Point-in-time universe membership and per-date eligibility | not started | | | |
| Direction validation (no shorts in swing or positional) | built 2026-10-02 (spec-level; swing engine enforcement is existing behaviour) | `research/intake/spec.py` (`_check_classification`) | `tests/test_intake_spec.py::TestDirectionAndProduct` | |
| Exchange calendar with `calendar_version`; session model (exchange, strategy, broker cutoff, force-flat) | not started | | | |
| Deployment profile: MIS force-flat 15:00, CNC carry-forward | built 2026-10-02 (declared and mapped to `RunConfig`; engine enforcement of CNC carry-forward is confirmed in step 5) | `research/intake/profiles.py`, `research/profiles/flattrade-nse-cash.yaml`, `research/intake/execution.py` | `tests/test_intake_profiles.py`, `tests/test_intake_execution.py` | |
| Corporate-action policy and `data_quality.json`; warm-up derived from indicators; dataset identity and version | not started | | | |
| Trade, order and fill, signal, exclusion ledgers with lineage ids (signal, order, fill, position, trade) | not started | | | |
| Run manifest and run folder layout; immutable artifacts retained and addressable | not started | | | |
| Per-component cost decomposition per trade | not started | | | |
| Validity vs performance failure handling in the gate runner | not started | | | |
| Trial counting under the acceptance-criteria definition | not started | | | |

## P2

| Capability | Status | Code | Tests | Verified |
| --- | --- | --- | --- | --- |
| MAE and MFE per trade | not started | | | |
| Exposure and turnover | not started | | | |
| Regime by strategy table (baseline trend/range, high/low volatility; optional India VIX, sector, index trend) | not started | | | |
| Market benchmark; controlled random-entry null with opportunity-set preservation | not started | | | |
| Structured failure diagnosis; research states | not started | | | |
| Parameter neighbourhood; cost stress; symbol and year contribution | not started | | | |
| Monte Carlo with stored ruin definition | not started | | | |
| Data-quality report section | not started | | | |
| Backtest Scope summary generated from frozen config | not started | | | |
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
