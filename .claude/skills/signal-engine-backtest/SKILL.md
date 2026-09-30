---
name: signal-engine-backtest
description: Map of OpenAlgo's signal_engine backtest system - how a strategy flows from adapter to engine to run record to research registry, and which invariants to preserve. Use before modifying anything under signal_engine/backtest/ or signal_engine/research/, when adding a backtest adapter, recording a run, evaluating a strategy against the acceptance criteria, or judging whether a strategy deserves more research.
---

# signal_engine backtest system

This skill is a map. It links to the owner of each fact and does not restate it. Read the
linked file for detail; the paths are relative to the repo root.

## Data flow

```
research/ideas/idea-NNN-*/idea.md      idea, status, verdict            (research/README.md)
        |
signal_engine/backtest/strategies/     one adapter per strategy; base.py (intraday), swing_base.py (swing)
        |  entry/exit rules, prepare(), prepare_key()
signal_engine/backtest/engine.py       intraday simulator, RunConfig in types.py
signal_engine/backtest/swing_engine.py multi-day simulator, SwingRunConfig
        |  list[Trade]                 (Trade / Ctx / Position in types.py)
signal_engine/backtest/harness.py      Backtest: IS/OOS split, ablation, sweep, cost_sensitivity, by_symbol
signal_engine/backtest/metrics.py      summary, clustered_t, hurdle_t
signal_engine/backtest/concurrency.py  position-cap view of a trade list
signal_engine/backtest/baseline.py     matched random-entry baseline (swing, and a sampled intraday version)
signal_engine/backtest/costs.py        per-trade statutory cost model at the deployed notional (G1.1, G1.3)
signal_engine/backtest/splits.py       purge and embargo at the IS/OOS cut (G3.5)
signal_engine/backtest/stats.py        Newey-West t, deflated Sharpe, MinTRL, PBO, walk-forward (G2.3 to G2.7, G3.3)
signal_engine/backtest/regime.py       versioned market-regime labels; evaluation.py scores them as X.2
signal_engine/backtest/evaluation.py   scores a run against the G0-G9 gates, no composite score
signal_engine/research/evolution.py    per-idea trajectory: improving, stagnating, regressing, exhausted
        |
signal_engine/backtest/tearsheet.py    build_run_record: the canonical run-record dict
signal_engine/backtest/record_run.py   CLI: runs an idea, writes research/ideas/<idea>/runs/<id>.json
        |
signal_engine/research/build_registry.py   validates records, writes REGISTRY.md, registry.json, site/
        |
services/backtest_results_service.py       read-only Backtests page (core code, do not edit)
```

Supporting code: `data.py` (Historify loaders), `dataquality.py`, `backfill.py`,
`validation.py` (four known-answer checks), `indicators.py`, `volume_profile.py`,
`portfolio.py`, `__main__.py` (`uv run --group analysis python -m signal_engine.backtest <name>`).

## Where the rules live

| Question | Read |
| --- | --- |
| Where does a new file go | `signal_engine/README.md` |
| Idea lifecycle, run-record schema, status and verdict values | `signal_engine/research/README.md` |
| Idea status and latest results | `signal_engine/research/REGISTRY.md` (generated, do not edit) |
| Pass/fail gates G0 to G9 and the India cost stack | `signal_engine/docs/STRATEGY-ACCEPTANCE-CRITERIA.md` |
| Benchmarking gaps and priorities | `signal_engine/docs/BACKTEST-BENCHMARKING-REVIEW.md` |
| Which ideas are real candidates | `signal_engine/docs/STRATEGY-LIBRARY-CLASSIFICATION.md` |
| Backlog being implemented | `signal_engine/backtest/backlog.md` |
| Live pipeline the backtest must match | `signal_engine/docs/PRD.md`, `signal_engine/docs/HOW-IT-WORKS.md` |
| Strategy Pine sources and analysis | `signal_engine/strategy_library/<horizon>/<name>/` |
| Pine to Python porting, strategy-from-video | skills `strategy-to-python`, `strategy-from-video`, `pinescript-strategy` |

## Invariants to preserve

- **Only signal_engine changes.** Never modify OpenAlgo core. The Backtests page, its blueprint and its service are core; treat them as read-only consumers of the run-record JSON.
- **Swing is long-only.** Indian cash equity cannot be shorted overnight; `SwingRunConfig` enforces `allow_longs=True, allow_shorts=False`. Two-sided strategies belong on the intraday engine.
- **Run records are immutable.** A change is a new version with `parent_run_id` and a change note, never an edit of an old file. Every variant tried counts toward `n_trials`; report the honest count.
- **Every timestamp is IST**, converted at the boundary where it enters (`signal_engine/timeutils.py`). Bar timestamps are open-time. See repo `CLAUDE.md`.
- **Backtest the full F&O universe**, never a deployment-constrained subset. Report the cap-limited view (`concurrency.py`) beside it, not instead of it.
- **One code path** for backtest, walk-forward, paper and live: rules live in `strategy_core`, adapters stay thin. See gate G0.1.
- **Broker data is the source of truth**; do not bend it to match TradingView.
- **No silent exclusion.** Every dropped symbol, day or trade is named with a reason (G0.5).
- **A strategy must pass the acceptance criteria** (G0 to G9, stop at the first HARD fail) before it goes live. Costs are India statutory charges at deployed notional plus slippage, not a flat guess.
- **X.2 (regime dispersion) is a heuristic, not a criteria-doc gate.** Its threshold was calibrated, not assumed: see `signal_engine/docs/REGIME-GATE-CALIBRATION.md`. Re-run `backtest/regime_gate_calibration.py` if the regime rule changes.
- **Judge verdicts by evidence, not one number.** Gross edge, net edge, t versus the trials-adjusted hurdle, in-sample versus out-of-sample, dispersion, and concurrency together.
- **Config style:** fail-fast, no in-code defaults for `signal_engine/config.yaml`; uv only; tests first, 80 percent coverage; no icons or emojis anywhere; do not commit unless asked.
- **A new strategy idea gets a folder in `research/ideas/` before any strategy code**, and after editing an idea run `uv run python -m signal_engine.research.build_registry`.

## Commands

```
PYTHONPATH=. uv run pytest signal_engine/tests/ -v
uv run --group analysis python -m signal_engine.backtest <strategy> [--full]
uv run --group analysis python -m signal_engine.backtest.record_run --help
uv run python -m signal_engine.research.build_registry
```

Heavy backtest or sweep work is best delegated to a forked agent that reports the distilled
conclusion, not raw output.
