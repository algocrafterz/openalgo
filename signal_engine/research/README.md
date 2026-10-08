# Research

Where strategy ideas live, from a raw link pasted in to a final verdict. The goal is that
no idea is done twice, forgotten, or dropped without a recorded reason, and that every
backtest result is reproducible and comparable with every other.

    research/
      REGISTRY.md          generated index: counts by status / category, one row per idea
      registry.json        same data, machine-readable
      site/index.html      generated static page, self-contained, safe to publish as-is
      build_registry.py    validates ideas and generates the three files above
      ideas/
        _inbox/            raw captures exactly as received (pasted text, PDFs, screenshots)
        _template/         copy this to start a new idea
        idea-NNN-<slug>/
          idea.md          frontmatter (status, verdict, links) plus the write-up
          runs/            one JSON per backtest run (see the schema below)

## Lifecycle

Note: new ideas use `stage` and `research_state`, not `status`. The table below is the older
`status` vocabulary. The current lifecycle and the schema 3 run artifact are defined in
`../docs/BACKTEST-LIFECYCLE-AND-RUN-SCHEMA.md`. Schema 4 adds the manifest keys listed in
`SCHEMA4_KEYS` in `../backtest/manifest.py`. The schema v2 tables in this file stay valid for
the `costs`, `baseline` and `position_caps` blocks.

| Status | Meaning |
| --- | --- |
| inbox | Captured, rules not yet pinned to numbers, nothing tested |
| evaluating | Being turned into testable rules or an adapter |
| backtested | At least one run recorded, no final decision yet |
| paper | Passed backtest, running on paper or forward data |
| live | Trading real capital |
| rejected | Tested and dropped; the reason is in `result` |
| shelved | Not dropped, but parked; `revisit_when` says what would reopen it |

`verdict` is the evidence, separate from status: `none`, `edge`, `marginal`, `no-edge`,
`inconclusive`. An idea can be `shelved` with verdict `marginal`.

Recorded runs use schema 3 or 4 (`../docs/BACKTEST-LIFECYCLE-AND-RUN-SCHEMA.md`). A schema 4
run reads one published dataset version and records the preflight result.

## Frontmatter classification

`strategy_category` (one of `taxonomy.yaml`; absent means Other) and `strategy_tags` (a list of
text) classify an idea. The older `category` and `tags` keys are retired: the builder rejects
them. Convert existing files with
`uv run python -m signal_engine.research.intake migrate-classification [--dry-run]`.
`horizon` is unchanged.

## Adding an idea

1. Save the raw source in `ideas/_inbox/` unchanged.
2. Copy `ideas/_template/` to `ideas/idea-NNN-<slug>/` using the next free number, and fill
   in `idea.md`. `revisit_when` is required in spirit: it is what stops an idea being
   forgotten or re-tested for nothing.
3. Run `uv run python -m signal_engine.research.build_registry`.
   `--check` validates without writing and fails on stale outputs, bad frontmatter, a
   duplicate id, or a link to a file that no longer exists.

## Where the analysis lives

One canonical home per fact. A strategy that has a folder in `../strategy_library/` keeps
its long analysis there (`STRATEGY-ANALYSIS.md` or `STRATEGY-LOG.md`); its `idea.md` only
links to it via `analysis_doc` and carries the one-line conclusion. An idea with no
strategy folder keeps its analysis inside its own `idea.md`.

## Run records (backtest results)

Results are data, not prose, so runs can be compared and audited. Save each run as
`ideas/<idea>/runs/<run_id>.json`; see `ideas/_template/runs/RUN-EXAMPLE.json.txt`. Required:
`run_id`, `date`, `code_version` (git sha), `data` (source, as_of, universe, bar_size),
`period`, `costs` (model, round_trip_bps) and `metrics` (trades, gross_bps, net_bps).
Optional: `split` (IS/OOS boundary), `win_rate`, `sharpe`, `max_dd_pct`, `t_stat`,
`breakeven_cost_bps`, `validation`, `notes`. The builder rejects a run missing any required
key, so a result cannot be recorded without saying which code, data and costs produced it.

### Schema v2: run history and tearsheet data

Every v2 key is optional, so v1 records stay valid; a malformed v2 key fails the build.
`uv run python -m signal_engine.backtest.record_run --idea IDEA-NNN` writes them all.

- **Runs are immutable.** A re-run after tuning creates a NEW file. Set `parent_run_id` to
  the run it descends from (it must be another run of the same idea) and describe the
  change in `change_note`. Never edit or overwrite an old run.
- `params`: the configuration that produced the run. `n_trials`: how many variants were
  searched to arrive at it, which sets `metrics.hurdle_t`. `promoted`: true once the run
  is chosen for forward testing.
- `cost_sensitivity`: net bps at flat round-trip costs. `in_sample` / `out_of_sample`:
  headline metrics for each side of `split`.
- `series`: `equity_r` and `drawdown_r` (lists of `[date, value]`, at most 500 points) and
  `monthly` (`month`, `n`, `total_r`, `net_r`). These stay in the run file and are left out
  of `registry.json`.
- Extra metrics: `profit_factor`, `expectancy_r`, `payoff`, `hurdle_t`, `max_dd_r`, `days`.
  A statistic that cannot be computed is omitted, not written as 0. A run with
  `trades == 0` stores 0.0 for gross and net bps; read those as n/a.

### Run records, schema v2

Runs are immutable and dated. Tuning an idea and re-running it writes a NEW file whose
`parent_run_id` names the run it grew from, so the results page can list runs by date and
compare any two. Never edit or overwrite a run file. Write them with
`uv run python -m signal_engine.backtest.record_run --idea IDEA-NNN [--parent RUN_ID]
[--note TEXT] [--trials N] [--oos-start YYYY-MM-DD]`, which builds the record through
`backtest/tearsheet.py`. All keys below are optional, so v1 records stay valid:

| Key | Meaning |
| --- | --- |
| `params` | The RunConfig and strategy parameters actually used, so two runs can be diffed |
| `parent_run_id` | Previous run of the same idea, or null; must exist and cannot be the run itself |
| `change_note` | One line on what changed since the parent |
| `n_trials` | How many variants were looked at to arrive at this one (defaults to 1, a lower bound) |
| `promoted` | True once the run has been chosen for forward testing |
| `cost_sensitivity` | `[{round_trip_bps, net_bps}]`: net result if real costs were worse than modelled |
| `in_sample`, `out_of_sample` | Core metrics on each side of `split.oos_start` |
| `series` | `equity_r` and `drawdown_r` as `[["YYYY-MM-DD", value]]`, and `monthly` rows `{month, n, total_r, net_r}` |
| `position_caps` | `[{max_positions, trades, taken_pct, gross_bps, net_bps, t_stat?, sharpe?}]`: what an account with only N open positions would have kept, first come first served. Every run is scored at 3, 5 and 10 slots by default (`--caps`) |
| `baseline` | Same-symbol random entry with the same holding period, direction and cost: `{method, gross_bps, net_bps, trade_net_bps, excess_net_bps, excess_t_stat?, trades, skipped}`. Computed for swing runs on all trades. Intraday runs compute it on a seeded sample of trades, loading only the symbols in that sample |
| `baseline_cross_sectional` | Intraday only, present when the strategy implements `null_entry` (`supports_selection_null`). Separates stock selection from the unconditional drift: for each session the strategy trades, the real engine replays the strategy's own stop, target and exit rules on every symbol with bars that session, at the same signal bar and direction, each costed on the statutory model: `{method, seed, null_pool, sessions, sessions_population, sampled, trades, population_trades, skipped, replays, replays_filled, replay_skips, pool_median_symbols, strategy_gross_bps, strategy_net_bps, null_gross_bps, null_net_bps, null_gross_t_stat, excess_gross_bps, excess_net_bps, excess_t_stat}`. The excess is the strategy trade minus the mean of its key's replays; `excess_t_stat` clusters by session. Whole sessions are sampled (seed 0) above 500,000 estimated replays. Report only: no gate, no spec field |

How to read `baseline_cross_sectional` (the code applies no thresholds):

| Case | Pattern | Reading |
| --- | --- | --- |
| 1 | `null_gross_bps` near 0 and `abs(null_gross_t_stat)` small | No unconditional drift in the strategy direction; any strategy result is not drift |
| 2 | `null_gross_bps` > 0 in the strategy direction with a large `null_gross_t_stat`, `excess_net_bps` near 0 and `abs(excess_t_stat)` small | Drift but no selection value: the names earn what any eligible name earns |
| 3 | `excess_net_bps` > 0 with a large `excess_t_stat` | Selection adds value beyond the drift |

### Costs and cost scenarios

The `costs` block still carries `model` and `round_trip_bps`. The `cost_model` block records
how a run was costed. The primary cost is the statutory per-trade model of the deployment
profile's broker (Flattrade, zero brokerage; `profiles/flattrade-nse-cash.yaml`, fee table in
`portfolio/broker_fees.py`). Each trade is priced at its own notional.
`cost_model.scenarios` lists the scenarios reported side by side:

| Scenario id | Meaning |
| --- | --- |
| `primary` | The profile broker's statutory model plus slippage. It drives the verdict and every gate |
| `generic_discount_broker` | The same model with the portfolio default brokerage (0.03%, capped at Rs 20 per order), priced per trade |
| `flat_<n>bps` | One reference line per level in the optional spec field `costs.reference_round_trip_bps` |
| `legacy_flat` | The frozen `execution.cost_bps_round_trip` |

Only `primary` is a gate input. The other scenarios are report-only. The scenario definitions
and `scenarios_sha256` are recorded. The cost fingerprint covers only the primary model
(settings, broker, rate table including the delivery DP charge, and the cost model version).

Round trip at Rs 1,00,000 per trade, with 8 bps slippage (two legs): MIS Flattrade is 11.55 bps
(3.55 statutory), MIS generic is 16.27 bps, CNC Flattrade is 32.61 bps (includes the DP charge)
and CNC generic is 30.25 bps.

Extra numeric `metrics`: `profit_factor`, `expectancy_r`, `payoff`, `hurdle_t` (the |t| a
result must clear at `n_trials`), `max_dd_r`, `days`, `peak_concurrent` and
`median_concurrent` (positions open when each trade opened, present when caps were scored).

`t_stat` and `sharpe` are scored on net bps, the unit of the headline `net_bps`, with each
trading day counted once. The headline is a per-trade mean, so the two can differ in sign when
a few heavy signal days carry the result; the results page flags that. A statistic that cannot be computed is
omitted, never written as 0. A run with `trades` 0 carries 0.0 in `gross_bps` and `net_bps`
only because the schema requires them; readers must treat those as n/a. Run the validation
suite (`backtest/validation.py`) before recording runs and say so in `validation`.

## Publishing

`site/index.html` is one file with inline CSS and JS and no external requests. Regenerate,
then publish that file wherever the page should live.

## Inbox items not yet triaged

- `ideas/_inbox/intraday-strategy.pdf` - not classified; could not be read in the session that
  built this registry. Open it and either create an idea or record why not.
