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
| `baseline` | Same-symbol random entry with the same holding period, direction and cost: `{method, gross_bps, net_bps, trade_net_bps, excess_net_bps, excess_t_stat?, trades, skipped}`. Computed for swing runs; intraday runs skip it (reloading the 5-minute bars costs minutes) |

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
