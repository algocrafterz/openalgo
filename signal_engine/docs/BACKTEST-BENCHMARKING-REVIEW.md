# Backtest Benchmarking Review (backlog item 2.1)

Date: 2026-09-30. Scope: the current benchmarking implementation under
`signal_engine/backtest/`, judged against `docs/STRATEGY-ACCEPTANCE-CRITERIA.md`. Every
claim cites the file where it was checked. Priorities: P1 fix first, P2 next, P3 later.

## Current implementation in one paragraph

An adapter feeds `engine.simulate` (intraday) or `swing_engine.simulate_swing` (swing),
producing `Trade` rows. `harness.Backtest` slices them into in-sample and out-of-sample
windows and offers ablation, sweep, cost sensitivity and per-symbol views.
`tearsheet.build_run_record` turns the trades into the canonical run record with metrics,
in-sample/out-of-sample results, cost sensitivity, position-cap views and (swing only) a
random-entry baseline. `record_run.py` writes it and `research/build_registry.py`
validates it. The strong points are a day-clustered t-statistic, a trials-adjusted hurdle,
immutable records with parent links, a known-answer validation suite (`validation.py`)
and a concurrency check that already caught one false edge (IDEA-004).

## Findings by axis

| Axis | Status | Evidence | Gap | Priority |
| --- | --- | --- | --- | --- |
| Correctness | Good | `validation.py` runs four checks with known answers; engine tests in `tests/test_backtest_engine.py` | Direction was unchecked: `SwingRunConfig` defaulted `allow_shorts=True` (`swing_engine.py`) although the Indian swing book cannot short. Now enforced in `__post_init__` (P1) | Done |
| Statistical validity | Partial | `metrics.clustered_t` (line 45) clusters by day. `hurdle_t` (line 59) uses Sidak at `alpha=0.05`, and gives about 1.97 at one trial | G2.2 requires at least 2.5 at 10 or fewer variants; the record's hurdle is weaker than the criteria for low trial counts. No Newey-West adjustment for autocorrelated daily P&L (G2.6), no Deflated Sharpe (G2.3), no PBO/CSCV (G2.4), no Minimum Track Record Length (G2.7). A repo-wide search for deflated, pbo, newey, walk-forward and cscv in `backtest/` finds nothing | P1 |
| Overfitting protection | Partial | `harness.py` docstring: IS chooses, OOS confirms once; `ablation()` marks BOTH / IS only / OOS only; `n_trials` recorded in `tearsheet.py` | No walk-forward (G3.3), no symbol hold-out (G3.4), no parameter-perturbation grid (G4.1). Hold-out size is inconsistent: `record_run.py:53` `OOS_FRACTION` was 0.25 at review time and is now 0.30 (G3.1); `harness.py:32` `is_fraction=0.60` (40 percent OOS) stays and is inside the 30 to 40 percent range, though the two constants are still separate. Split is by trade `day` (`harness._filter`), so a multi-day swing trade entered before the cut can be scored in-sample while its outcome falls after it; no embargo (G3.5) | P1 |
| Transaction-cost modelling | Partial | `record_run.py:50-51` flat `INTRADAY_COST_BPS = 16.0`, `SWING_COST_BPS = 24.0`; run record stores `"model": "flat-round-trip-bps"` (line 264). `types.india_intraday_bps()` (line 147) derives 16 bps from `portfolio.costs.india_intraday` at Rs 1 lakh | G1.1 wants statutory charges at the deployed notional plus slippage, per trade. Flat bps ignores stop-distance (R) so G1.2 to G1.5 (net in R, cost per trade in R) are not measured from the record. The swing figure of 24 bps is a constant, not derived from the CNC schedule | P1 |
| Slippage assumptions | Partial | 4 bps per side in `india_intraday_bps` (line 147); `harness.cost_sensitivity` and `tearsheet._cost_sensitivity` (line 176) show sensitivity | Slippage is a single constant, not stressed as G1.3 specifies (double slippage only, statutory fixed) and not scaled to liquidity or gap opens | P2 |
| Benchmark selection | Partial | `baseline.random_entry_baseline` (matched random entries) is computed for swing only: `record_run.py` lines 242-249 skip it for intraday "because reloading 10 years of 5-minute bars costs minutes per idea" | Intraday runs, which are most ideas (ORB, EMA9, IB and others), carry no random-entry benchmark. No buy-and-hold or index benchmark in the record | P1 |
| Edge measurement | Good | Gross bps and net bps in every record; `summary()` reports `net_R`, `t`, `t_naive`, payoff | Edge is measured per trade; nothing reports it per regime (backlog 10) or on daily aggregated P&L with dependence adjustment (G2.6) | P2 |
| Risk-adjusted performance | Partial | Drawdown, equity and monthly series in the tearsheet; concurrency profile in `concurrency.py` | Sharpe and drawdown computed from trade sequence, not a capital-based daily equity curve at the deployed size; no tail-loss (CVaR) or Ulcer measure | P2 |
| Reproducibility | Good | Immutable records with `git_version`, parent id, seed in the baseline (`seed=0`), `params` and `run` config stored | `git_version` is marked dirty when the working tree has uncommitted changes, so a re-run made from an uncommitted tree cannot be reproduced later. Data as-of date is stored but the Historify snapshot itself is not hashed | P2 |
| Data quality | Partial | `dataquality.py`, `backfill.py`, split-day drop in `data.py` | G0.4 coverage report and G0.5 named exclusions are not written into the record; survivorship (G4.7) is not reported | P2 |
| Performance | See below | | | |

## Unnecessary computation and optimizations

1. **Cost sensitivity re-simulates the whole book per cost level.**
   `harness.cost_sensitivity` (line 127) calls `self.trades(...)` once per bps value, and
   `trades()` runs the engine over every symbol each time (lines 71-91). Cost enters each
   trade only as `r_net = r_gross - cost_frac * entry / risk` (see `swing_engine.py`
   `close_out`), so net results at any cost can be recomputed from one set of trades with
   a vector operation. This removes N-1 of N engine passes per report. `tearsheet.py`'s
   `_cost_sensitivity` already works this way from the stored aggregates. Priority P1 (time).
2. **The intraday random baseline is skipped for cost of reloading.** The baseline needs
   the price panel, not a new simulation. Compute it from the frames already held by
   `Backtest`, or load it once per run and reuse across ideas, instead of dropping it.
   Priority P1.
3. **`release_raw=True` blocks re-preparation.** It saves memory but forbids varying
   strategy parameters (`harness.py` comment and the RuntimeError at line 82), so a
   perturbation grid (G4.1) needs a second, non-released mode or a parameter-keyed
   prepare cache with an on-disk raw store. Priority P2.
4. **Parameter sweeps and ablations run sequentially per symbol.** `trades()` loops over
   symbols in a single process; a per-symbol process pool would parallelize sweeps, but
   respect the memory ceiling that motivated `release_raw`. Priority P3.
5. **Trade lists are not stored.** The record keeps aggregates only, so direction counts,
   symbol concentration and regime cuts cannot be recomputed later without re-running the
   idea. A compressed trades sidecar (P2 in the implementation plan) turns each future
   analysis from a full re-run into a file read. Priority P2.

## Recommended improvements, in order

| Priority | Change | Why |
| --- | --- | --- |
| P1 | Use one hold-out constant in `record_run` and `harness` (record_run is now 0.30, harness 0.40, both inside G3.1's 30 to 40 percent), and drop trades that straddle the cut with a one-session embargo | Removes the 30/40 duplication and closes leakage (G3.1, G3.5) |
| P1 | Report both the trials-adjusted `hurdle_t` and the G2.2 floor of 2.5, and pass only above the larger | 1.97 at one trial is below the acceptance bar |
| P1 | Cost-model upgrade: per-trade statutory charges at the deployed notional plus slippage, with net in R (`net_R`) and cost-per-trade in R | Aligns with G1.1 to G1.5; flat bps hides small-stop trades |
| P1 | Intraday random-entry baseline, using the optimization in item 2 above | Benchmark exists for swing only |
| P1 | Cost sensitivity by vector recompute (item 1 above) | Cuts run time for every report |
| P2 | Newey-West t on daily P&L, Deflated Sharpe and Minimum Track Record Length in `metrics.py` | G2.3, G2.6, G2.7 |
| P2 | Parameter perturbation grid at plus or minus 20 percent | G4.1 |
| P2 | Direction split, dispersion, yearly and volatility-tercile tables in the record | G4.4 to G4.6; supplied by the evaluation and export phases |
| P2 | Record data coverage, named exclusions and survivorship note per run | G0.4, G0.5, G4.7 |
| P3 | Walk-forward windows, symbol hold-out, PBO/CSCV | G3.3, G3.4, G2.4; heavier and only worth building once the P1 items land |

## Implementation status (2026-10-01)

Each row of the two lists above, with what was built. Code paths are under `backtest/`.
Nothing here regenerates a stored run record: records are immutable, so the new blocks
appear only on runs made after this change.

| Item | Status | What exists | What is left |
| --- | --- | --- | --- |
| P1 one hold-out constant, embargo | Done | `types.HOLDOUT_OOS_FRACTION = 0.30` is read by `record_run`, `Backtest` (interactive default moved from 40 to 30 percent out of sample) and `PortfolioConfig` (60/40 to 70/30). `splits.purge_and_embargo` purges in-sample trades that exit after the cut and skips one session; `record_run.EMBARGO_SESSIONS = 1`; counts are written to `split`; gate G3.5 | The interactive `Backtest.confirm` windows still split by entry day with no embargo |
| P1 G2.2 bar is the larger of criteria level and hurdle | Done | `evaluation._g22` passes only above `max(t_threshold, hurdle_t)` and cites both | The headline `hurdle_t` in the record is unchanged (1.97 at one trial) |
| P1 cost model at deployed notional | Partial | `costs.CostModel`, `recost`, `cost_profile`: per-trade statutory schedule at risk-sized notional (capital, risk and MIS leverage from `config.yaml`) plus slippage; optional `cost_model` record block; gates G1.1 and G1.3 (slippage doubled, HARD) | The engines and the headline still charge the flat bps so records stay comparable; slippage is not scaled to liquidity or gap opens |
| P1 intraday random-entry baseline | Done | `baseline.random_entry_baseline_intraday`: same symbol, hold in bars, direction and session; seeded sample of at most 2000 trades; only the sample's symbols are loaded; record says `sampled` and `population_trades` | Smoke-tested on six symbols; not yet run on a full-universe intraday record |
| P1 cost sensitivity from one trade set | Done | `metrics.with_flat_cost`; `Backtest.cost_sensitivity` simulates once and restates net per level (test proves equality with a re-simulation) | None |
| P2 Newey-West t, DSR, MinTRL | Done | `stats.newey_west_t`, `deflated_sharpe`, `min_track_record_length`; gates G2.6 (report), G2.3 (pass at 0.90), G2.7 (report) | DSR assumes the trial Sharpe variance is the no-edge sampling variance, because only the trial count is recorded; say so when quoting it |
| P2 parameter perturbation grid | Deferred | G4.1 stays not_evaluated | Needs a mode that keeps raw frames (`release_raw=False`) or a parameter-keyed prepare cache |
| P2 direction, dispersion, yearly and regime tables | Done | G4.4 to G4.6, `regimes` block, X.2 (a calibrated heuristic, `REGIME-GATE-CALIBRATION.md`) | None |
| P2 coverage, named exclusions, survivorship per run | Deferred | G0.4, G0.5, G4.7 stay not_evaluated | Needs the data layer to return what it dropped |
| P3 walk-forward | Done | `stats.walk_forward`, gate G3.3: at least 4 windows of 6 months, 60 percent positive; parameters held fixed | Rolling stability only, not a re-fit per window |
| P3 PBO / CSCV | Partial | `stats.pbo_cscv` is implemented and tested | G2.4 needs P&L for every variant on a common grid; stays not_evaluated |
| P3 symbol hold-out (G3.4) | Done | `evaluation.symbol_half` splits symbols by a SHA-1 hash, stable across runs; both halves must be net positive and the weaker at least 40 percent of the stronger; needs 10 symbols and 100 trades per half | Parameters are fixed in a record, so this tests generalisation across names, not a re-fit |
| Regime activation hypothesis (X.3) | Done, report only | `evaluation._regime_activation` lists regimes to enable and disable per dimension from the record's regime block | Never a rule: the filtered strategy must be recorded as its own candidate and clear the criteria out-of-sample |
| Optimization 1 cost sensitivity | Done | see above | |
| Optimization 2 intraday baseline | Done | see above | |
| Optimization 3 `release_raw` blocks the grid | Deferred | | Same as the perturbation grid |
| Optimization 4 per-symbol process pool | Deferred | | P3 |
| Optimization 5 trade sidecar | Done | `export.py` | |
| Risk measures (CVaR, Ulcer), reproducibility hash | Deferred | | P2 |

## Caveats

- The G6 gates in the acceptance criteria assume intraday MIS trading; swing ideas need a
  gate profile with G6 marked not applicable.
- The record schema is immutable, so every new block above must be optional in
  `build_registry` validation to keep v1 and v2 records valid.
- This review reads code and the acceptance criteria; it did not re-run any backtest.
