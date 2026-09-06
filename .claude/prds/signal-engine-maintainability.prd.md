# Signal Engine Maintainability

## Problem

`signal_engine/` has accumulated implementation additions to the point where the maintainer cannot navigate it. The trading pipeline's decision logic is concentrated in four god-functions inside two oversized files, and the same exit sequence is re-implemented in four places. The cost of leaving this unsolved is that every change to exit behaviour requires locating and correctly editing up to four duplicated blocks, in live-money code, with no structural signal about which block governs a given situation.

## Evidence

Measured on branch `feature/optimize-signal-engine`, 2026-08-23. All figures are counted from the working tree, not estimated.

**Oversized units** (project standard in `CLAUDE.md`: files <800 lines, functions <50 lines)

| Unit | Size | Over standard by |
|---|---|---|
| `main.py` | 1,169 lines | 1.5x file cap |
| `tracker.py` | 904 lines | 1.1x file cap |
| `main._handle_exit_locked` | ~359 lines | 7x function cap |
| `main._handle_entry` | ~254 lines | 5x function cap |
| `tracker.check_positions` | ~237 lines | 5x function cap |
| `tracker._check_no_progress` | ~211 lines | 4x function cap |

**Duplication of the exit sequence** — cancel SL, build exit order, send, resolve fill, record close, notify — appears as four independent implementations:
- signal-driven exit (`main.py:415`)
- tracker close-detection (`main.py`/`tracker.py:385`)
- no-progress exit (`tracker.py:625`)
- time exit (`tracker.py:797`)

Corroborating call-site counts: 6 `record_close` sites, 3 `record_rejection` sites, 4 `build_exit_order` sites, 5 `notify_sl_failed` sites, 4 `cancel_order` sites in `main.py` plus 4 in `tracker.py`.

**Repeated boilerplate**
- 11 near-identical `httpx.AsyncClient` → POST → `raise_for_status` → `status != "success"` blocks in `api_client.py`, with no shared request helper.
- `_IST = timezone(timedelta(hours=5, minutes=30))` defined independently in 5 production modules (`db`, `notifier`, `risk`, `main`, `tracker`) and 7 further times across tests.

**Import-time global state**
- `risk_engine`, `tracker`, and `_exit_locks` are constructed at module import in `main.py`. Consequence: `test_main.py` is 1,878 lines, the largest file in the tree, dominated by monkeypatching.
- Three `tracker` methods perform a deferred `from signal_engine.config import settings` inside the function body — a circular-import workaround, not a deliberate lazy load.

**Existing safety net** — 466 tests pass in 40s; 85% overall coverage. Coverage is inverted against refactor risk:

| Module | Coverage |
|---|---|
| `main.py` | 52% |
| `notifier.py` | 46% |
| `scripts/openalgoscheduler.py` | 43% |
| `listener.py` | 13% |
| `smoke_test.py` | 0% |
| `tracker.py` | 83% |
| `risk.py` | 88% |
| `config.py` | 93% |

**Non-findings** (checked, no action needed): no unreferenced public functions in any module; `data/`, `logs/`, and `*.pid` are correctly gitignored; the fail-fast config loader is consistent across 31 `_require_key` call sites.

## Users

- **Primary**: the sole maintainer of the signal engine — the person who, on a live trading day, must locate and change exit or entry behaviour under time pressure and be confident the change took effect everywhere it needed to.
- **Not for**: end users of OpenAlgo, broker integrators, or consumers of the REST API. No externally observable behaviour of the trading engine is in scope for change.

## Hypothesis

We believe **a navigable signal engine — no oversized files or functions, and one canonical implementation of each repeated pipeline sequence** will **remove the search-and-cross-check cost of every change, and eliminate the class of bug where a fix lands in three of four duplicated blocks** for **the maintainer**.

We'll know we're right when **the full 466-test suite stays green through the entire effort with no test rewritten to accommodate changed behaviour, and no file in `signal_engine/` exceeds 800 lines or function exceeds 50 lines.**

## Success Metrics

| Metric | Target | How measured |
|---|---|---|
| **Release gate** — behaviour preserved | 466/466 tests pass; zero tests modified to accept new behaviour | `PYTHONPATH=. uv run pytest signal_engine/tests/ -v`; test diff reviewed per milestone |
| Files over the project cap | 0 | `wc -l` across `signal_engine/**/*.py` |
| Functions over the project cap | 0 in the trading pipeline | static scan of function spans |
| Exit-sequence implementations | 1 | count of independent cancel-SL → place-exit → record-close sequences |
| Coverage regression | overall stays >= 85% | `pytest --cov=signal_engine` |

Coverage *improvement* is explicitly not a success criterion — the maintainer chose the green suite as the gate. Coverage is tracked only to confirm it does not fall.

## Scope

**MVP** — the trading pipeline (`main.py`, `tracker.py`) is navigable: no file over 800 lines, no function over 50 lines, and each distinct pipeline stage is locatable without reading an unrelated 350-line function. Behaviour is unchanged and the suite is green.

**In scope (maintainer confirmed nothing is excluded)**
- Trading pipeline: `main.py`, `tracker.py`, `executor.py`, `risk.py`
- Support modules: `api_client.py`, `notifier.py`, `normalizer.py`, `parser.py`, `validator.py`, `db.py`, `risk_store.py`
- `config.py` / `config.yaml`
- `scripts/openalgoscheduler.py`
- `smoke_test.py`
- The test suite itself, including the 1,878-line `test_main.py`
- `pinescripts/` Python tooling and the strategy documentation set

Ordering is by leverage against the primary pain, not by this list.

**Out of scope**
- Any change to what the engine does — order semantics, risk rules, exit conditions, notification content, or config keys. Behaviour preservation is the constraint, not a goal to trade off.
- New features, new strategies, new brokers.
- Performance optimisation, except where it falls out of deduplication for free.
- `.pine` source files themselves — strategy logic is not a maintainability target of this effort.

## Delivery Milestones

<!-- Business outcomes, not engineering tasks. /plan turns each into a plan. -->
<!-- Status: pending | in-progress | complete -->

| # | Milestone | Outcome | Status | Result |
|---|---|---|---|---|
| 1 | Characterization safety net | Exit and entry branches pinned before any edit. | **complete** | 48 tests; `main.py` 52% → 71% pre-refactor |
| 2 | Navigable trading pipeline | Any pipeline stage findable without reading unrelated logic. | **complete for the engine** | 0 functions >50 lines in `main.py`, `tracker.py`, `startup.py`, `validator`, `parser`, `normalizer`, `executor`, `notifier`, `config`. File cap not met — see note |
| 3 | One exit sequence | Exit placement and recording in one place. | **partial** | Orphan release, P&L booking, day-counter reset, exit-order send unified. Cross-module unification blocked — see note |
| 4 | Composable pipeline | Engine constructible without import-time global state. | **partial** | `runtime.build_risk_engine()` added as composition root; the `tracker`/`risk_engine` singletons in `main.py` remain |
| 5 | Shared primitives | One definition per repeated mechanism. | **complete** | `IST` 5 copies → 1; httpx blocks 11 → 2; `RiskEngine` construction 3 → 1 |
| 6 | Proportionate test suite | Test files match the code they cover. | **complete** | 3 files (4,182 lines) → 13 files, none over 800 |
| 7 | Periphery cleanup | Support code meets the same standard. | **partial** | `config`, `smoke_test`, `listener`, `openalgoscheduler` done; `analyze_orb.py` deliberately skipped — see note |

### Status notes (2026-08-23)

**Test gate held throughout.** 514 tests green at every step; the 466 pre-existing tests
were never modified for behaviour, only relocated when their file was split.

**File cap not met for two files.** `main.py` is 1,114 lines and `tracker.py` 982 (cap 800).
Decomposition *adds* lines — signatures and docstrings — so splitting the god-functions moved
the number very little even after 260 lines went to `startup.py`. Closing the gap means moving
the entry and exit pipelines into their own modules, but both read the module-level `tracker`
and `risk_engine` singletons that ~380 `patch("signal_engine.main.X")` targets bind to. The
options are (a) thread an explicit dependency object through the pipeline functions — zero test
churn, but a `deps.` prefix on every external call, or (b) move the singletons to `runtime.py`
and rewrite those patch targets. Neither is worth doing silently on live-money code; navigability,
which the cap is a proxy for, is already achieved.

**Milestone 3 stopped at the module boundary.** Four exit paths remain, and they are genuinely
different: signal-driven exit places an order and books P&L immediately; broker-close detection
books P&L for an order it never placed; the no-progress exit places an order and lets close
detection book it; time exit closes in bulk and distributes P&L. What *was* duplicated has been
unified — orphan release (3 copies → 1), realised-P&L delta booking, day-counter reset (2 → 1),
exit-order build+send. Unifying further requires moving notification and P&L logic across the
`main`/`tracker` boundary, which breaks tests that assert on `notifier` calls while `tracker` is
mocked. That is the test-migration decision above.

**`analyze_orb.py` deliberately skipped.** Its 431-line `generate_report` has no tests. A golden-
output baseline was captured and three split attempts were made; all failed because report
sections share fourteen local variables (`m`, `d_grade`, `fri`, `multi_tp_pnl`, ...) that flow
forward between sections. Untangling that by hand risks silently corrupting the performance
report used for strategy decisions, for no live-trading benefit. The file was restored untouched.

**One latent defect found and documented, not fixed** — see "Known defects" in `signal_engine/PRD.md`.

## Open Questions

- [x] ~~Milestone 1 as prerequisite?~~ Accepted and complete — 48 characterization tests landed before any `main.py` edit.
- [ ] The four exit paths may differ in edge-case handling today (for example, whether a failed SL cancel aborts the exit). Behaviour preservation means those differences must be preserved through Milestone 3 rather than harmonised. Confirm — or is harmonising a difference acceptable when it is demonstrably a bug?
- [ ] `smoke_test.py` is at 0% coverage. Milestone 7 has no safety net at all. Acceptable, or should it be verified by running it against a live session instead?
- [ ] Should milestones land as separate commits on `feature/optimize-signal-engine`, or one branch per milestone?
- [ ] `PRD.md` (1,153 lines) is maintained alongside the engine and the maintainer's standing preference is to update it on every change. Does that apply to each milestone, or once at the end?

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Green suite is a weak gate for `main.py` at 52% coverage — a refactor breaks an untested branch and the failure surfaces only during live trading | High | High | Milestone 1 characterization tests before touching `main.py`; if declined, restrict Milestone 2 to mechanical extraction with no logic edits |
| The four exit paths encode real behavioural differences that look like duplication; unifying them silently changes exit semantics | Medium | High | Milestone 3 requires a documented behaviour diff of the four paths before any consolidation; differences are preserved unless explicitly approved |
| Removing import-time globals is a wide, cross-cutting change that touches nearly every test | Medium | Medium | Sequence Milestone 4 after 2 and 3, when the pipeline is already smaller; keep the old module-level names as thin aliases during transition |
| Refactor spans a live trading period and a regression costs real money | Medium | High | Land milestones between trading sessions; verify one live session after each milestone before starting the next |
| Scope is unbounded ("nothing is out of scope") and the effort stalls mid-restructure, leaving the code half-migrated and harder to navigate than before | Medium | Medium | Each milestone is independently shippable and leaves the tree consistent; stopping after any milestone is a valid end state |

---
*Status: DRAFT — requirements only. Implementation planning pending via /plan.*
