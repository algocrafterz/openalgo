# P2 plan: the two HARD gates G4.1 and G6.4

Status: PLAN ONLY, awaiting owner review. No code is written until this is approved.
Rule carried over: no gate is weakened or bypassed to make EDGE reachable. A gate with missing
evidence stays `not_evaluated` (which holds the verdict at MARGINAL), never `pass`.

## What exists today (verified in the tree)

- `evaluation.py` lists both gates as HARD with no evaluator (`_EVALUATORS` has neither).
  `next_action.py` already has rules for both.
- Evidence pattern to reuse: a stage in `record_run` produces a sealed evidence object
  (`truncation_check` -> `Execution.truncation` -> `_Inputs.truncation`), and the evaluator only reads it.
- `Backtest.sweep()` (harness.py) already varies one field over values; `stats.pbo_cscv` exists and
  waits for a parameter grid (G2.4).
- The engine does NOT round any price to a tick. Signal price, stop, target and fill prices are
  float values straight from indicators (types.py only narrows to float32). The only tick logic is
  live: `executor.py` rounds with `features.instruments.safe_tick`.
- Tick tables: `tick_for_price` (NSE tiers by price) and `safe_tick` (coarsest tier the stock could
  be in this month). The exchange picks the tier from the previous month's last close.

## G4.1 Parameter perturbation (HARD)

Rule (acceptance criteria): each parameter moved +-20%; at least 70% of neighbours stay net positive;
none loses more than 60% of the base expectancy; choose the plateau, not the peak.

### Evidence required
A sealed `evidence/perturbation.json` in the run folder:
- the base: params fingerprint, n_trades, net expectancy in bps and R at 1.0x cost
- one row per neighbour: parameter path, multiplier (0.8 or 1.2), applied value, params fingerprint,
  n_trades, net expectancy (bps, R), ratio to base, net-positive flag
- parameters skipped, each with a reason (non-numeric, boolean, time window, structural)
- the engine/cost/data fingerprints the neighbours ran under (must equal the base run's)

### Neighbour definition
- One-at-a-time: for each perturbable numeric parameter p, run p*0.8 and p*1.2, others at base.
  2 x P runs for P parameters (no cartesian product).
- Integer parameters: round to nearest integer; if the rounded value equals base, step by 1 so the
  neighbour is distinct. Floats keep full precision. Values are clamped to the parameter's valid range.
- Not perturbed: strings, booleans, enums, HH:MM times and session windows, and anything the strategy
  marks structural. Perturbing these is meaningless or changes the strategy, not its tuning.
- Open point: params are a nested dict today with no per-parameter metadata. Needs a small
  `param_meta` declaration on strategies (tunable, integer, min, max). Strategies without it are
  treated as P = 0 perturbable and the gate is `not_evaluated` with the reason, never a silent pass.

### Evaluator logic (`_g41`, reads `x.perturbation`)
- No evidence: `not_evaluated`.
- Base net expectancy <= 0: `fail` (nothing to be stable about); evidence names it.
- Otherwise value = share of neighbours with net expectancy > 0; gate passes only when
  share >= 0.70 AND every neighbour retains >= 40% of base expectancy (loses no more than 60%).
- Fewer than 4 neighbours (P < 2): `not_evaluated` (too few to call 70% meaningful), stated in evidence.
- Evidence string names the worst neighbour and the plateau ratio (median neighbour / base).
  A base that is much better than every neighbour is flagged as a peak.

### Data and cost dependencies
- Needs the engine re-run on the same frames, costs and window, so a `run_variant(params)` callback
  parallel to `execute`. The truncation stage already re-runs the engine, so the wiring pattern exists.
- Cost: 2P full-universe runs. Must be measured first on one real run before choosing defaults.
  Mitigations to decide after measuring: share prepared frames across variants where `prepare_key`
  is unchanged; cap neighbour count; run as an opt-in stage with a recorded "not run" state like
  `--no-truncation`.
- Eventlet constraint: this runs in the research CLI process, never the web worker.

### Trials interaction (OWNER DECISION)
Neighbour params differ in the params fingerprint, so as written they would register 2P new trials per
run and inflate the G2.5 deflation threshold. Recommendation: neighbours are diagnostics, not
selectable candidates, so they are recorded in the perturbation evidence and NOT registered as trials.
Any later decision to adopt a neighbour's values is a normal new run and counts as a trial. If you
prefer the conservative reading (every variant run counts), say so and the evaluator is unchanged;
only the registration call changes.

### Acceptance criteria
1. A strategy on a smooth plateau passes; one with a single sharp peak fails with the peak named.
2. Missing evidence, P < 2, or missing `param_meta` gives `not_evaluated`, never `pass`.
3. Neighbours reproduce deterministically (same fingerprints on rerun) and `reproduce` verifies them.
4. Neighbour runs use the base run's data, cost, calendar and universe fingerprints.
5. The sealed run folder stays byte-for-byte reproducible; the evidence file is part of the manifest.
6. The page shows the neighbour table and a "Show trades" style link is not needed (summary only).

### Tests (written first, watched failing)
- Neighbour generation: float, integer rounding collision, clamping, skipped types with reasons.
- Evaluator: plateau pass; 69% vs 70% boundary; 60% loss boundary (exactly 60% passes, above fails);
  negative base; P < 2; absent evidence; peak flag.
- Integration: record a synthetic strategy with a known sensitivity and assert the gate and evidence.
- Reproducibility: second run yields identical `perturbation.json`.
- Trials: assert the chosen policy (neighbours do not change the trial count).

## G6.4 Tick size (HARD before live)

Rule: every order price and stop trigger must be a multiple of the tick that applies to that stock this
month. The acceptance text also requires, before live, one small real order per tier (below Rs 250,
Rs 250 to 1,000, above Rs 1,000) accepted by the broker.

### What a backtest can and cannot prove
- A backtest can show whether the PRICES THE RUN ASSUMED lie on the exchange tick grid.
- It cannot prove the broker accepts them. That needs the real-order check, which is an operational
  attestation, not a computed value.
So the gate has two parts and passes only when both are present.

### Part A: modelled-price grid check (computed)
- Evidence: `evidence/tick_grid.json` (or a parquet) built at record time from the run's ledgers:
  for every signal stop and target and every fill price, the applicable tick for that symbol and month
  and whether the price is a multiple of it.
- Tick source: exact tier from the previous month's last close, computed from the run's own daily
  data (written at record time because `evaluate()` has no price data); `safe_tick` where the previous
  month is not in the data, labelled as the fallback.
- Result: counts on-grid and off-grid, by tier, with named worst offenders.

### Finding that needs an owner decision
The engine does not snap prices to the grid, so today the off-grid share will be high for stops and
targets derived from ATR-style indicators. Options:
1. (Recommended) Add an execution-resolution rule to the engine: stop, target and entry limit prices
   are rounded the way `executor.py` rounds them (`safe_tick`, direction-aware so a stop is never
   rounded to a better price). This is RESULT-CHANGING, so it needs its own change class, a version
   bump and a rerun of every recorded run; fills at market are unaffected.
2. Report only: keep the engine as is and report the off-grid share as information; the gate then
   cannot pass because the modelled prices are not what the executor would send.
I will not make option 1 silently; please choose.

### Part B: broker acceptance attestation (recorded, not computed)
- A small append-only `broker-tick-checks.yaml` per deployment profile: date, tier, symbol, order price,
  trigger price, broker order id, accepted yes/no, who recorded it. Three tiers required.
- Intake validates it like the other append-only ledgers (shrink check, schema errors fail the registry).
- No attestation: the gate is `not_evaluated` with the evidence "real-order check owed" (it stays so
  until you place and record the three orders).

### Evaluator logic (`_g64`)
- Part A evidence missing: `not_evaluated`.
- Part A has any off-grid executable price (stops and limit entries): `fail`, listing counts by tier.
- Part A clean and attestation missing or incomplete: `not_evaluated` (not `pass`).
- Part A clean and all three tiers attested accepted: `pass`.
- Rejected attestation: `fail`.

### Acceptance criteria
1. A synthetic run with off-grid stops fails; the same run with snapped prices passes Part A.
2. Tier boundaries are exact: 250 and 1,000 belong to the finer tier (matches `tick_for_price`).
3. Month-boundary handling uses the previous month's last close, with the fallback labelled.
4. The gate never reaches `pass` without the three-tier attestation.
5. Existing runs without the evidence still load and show the gate as not evaluated.

### Tests (written first)
- Grid check per tier and on band edges (250, 1000, 5000, 10000, 20000), float tolerance cases.
- Direction-aware rounding if option 1 is chosen: long stop rounds up in price distance, short stop down.
- Evaluator matrix: Part A missing, Part A failing, Part A clean without attestation, with partial
  attestation, with full attestation, with a rejection.
- Attestation file: schema, append-only shrink check, duplicate tier handling.
- Engine rounding change (option 1 only): golden-file comparison showing the exact result delta.

## Order of work after approval
1. Measure one real run's wall time and memory to size G4.1 (no code change).
2. G6.4 Part A evidence and evaluator (smallest, independent of the engine decision if option 2).
3. G4.1: `param_meta`, neighbour generation, variant runner, evidence, evaluator.
4. G6.4 Part B attestation ledger and intake checks.
5. UI: evidence blocks for both on the run page (read-model only).
6. Readiness rows and findings updated; independent review per step as before.

## Owner decisions (recorded 2026-10-04)
1. G4.1 neighbour runs are NOT trials. They are recorded as diagnostic robustness runs inside the
   perturbation evidence, so they stay auditable. Adopting a neighbour's values is a normal new run.
2. The engine models the executor's tick grid. Result-changing: new change class, new engine version,
   and affected historical runs are rerun, never silently altered. (The registry currently holds no
   recorded runs, so nothing needs rerunning today; the rule stands for any run recorded later.)
3. Perturbable parameters are declared explicitly per strategy (eligibility plus bounds or rules).
   Nothing is inferred from types. A strategy with no declaration gets `not_evaluated`.
4. G4.1 ships as an explicit robustness stage that must be run (not on every normal backtest).
   Not run means `not_evaluated`, never `pass`. Making it automatic for promotion candidates is decided
   after the real cost is measured.
5. G6.4 stays two-part and is not weakened: the modelled prices must be on the tick grid AND the three
   real broker-order records must exist. Without the broker records it stays `not_evaluated`.
6. The empty registry is not accepted as a final state. The 65 pre-existing idea deletions are not
   restored and not committed until you confirm them.
