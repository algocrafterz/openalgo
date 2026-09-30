---
name: openalgo-codebase-guide
description: Answer questions about the OpenAlgo platform and its signal_engine module, and teach a newcomer how the codebase fits together. Use when asked "how does X work", "where is Y implemented", "walk me through the signal pipeline", "what happens when a TradingView alert arrives", or when onboarding someone to the repo. Read-only explainer; for changing backtest code use signal-engine-backtest instead.
---

# OpenAlgo and signal_engine: codebase guide

This skill is a navigator. It says where each thing lives and which file owns the detail.
Open the owner file and quote from it; do not answer from memory, because code moves.

## How to answer a question

1. Find the area in the tables below.
2. Read the owner file (and `docs/INDEX.md` for platform docs, `signal_engine/README.md`
   for the signal_engine map).
3. Answer with `path:line` references, then one plain-language sentence of summary.
4. If the answer depends on a runtime fact (what is live today, current verdicts), read
   `signal_engine/research/REGISTRY.md` or the log, not this file.

## Platform (OpenAlgo core)

| Question | Owner |
| --- | --- |
| What products exist, invariants, runtime constraints (eventlet, ZMQ, SQLite) | `CLAUDE.md` |
| Where every doc lives | `docs/INDEX.md` |
| REST API, order flow | `blueprints/`, `services/`, `restx_api/`; docs in `docs/api/` |
| Broker plugins, symbol mapping | `broker/<name>/`; procedure in skill `broker-integration` |
| Market data streaming | `websocket_proxy/`, `broker/*/streaming/`; `docs/websocket-architecture.md` |
| Databases | `database/` (six DBs, each with an init function) |
| Frontend | `frontend/src/` (React 19); route registry for tools `frontend/src/lib/tools.ts` |

Core is maintained upstream. signal_engine work never edits it.

## signal_engine: live path

`listener.py` (Telegram) -> `parser.py` -> `validator.py` -> `risk.py` (sizing and limits,
state in `risk_store.py`) -> `executor.py` (orders via OpenAlgo API) -> `tracker.py`
(positions, SL and exits) -> `notifier.py`. Orchestration is `main.py`; all settings are in
`config.yaml`, loaded fail-fast by `config.py` with no in-code defaults. Design intent:
`docs/HOW-IT-WORKS.md` and `docs/PRD.md`.

## signal_engine: research and backtest path

Idea -> spec -> adapter -> engine -> run record -> evaluation -> registry -> verdict.

| Stage | Owner |
| --- | --- |
| Idea folder, lifecycle, status vs verdict | `signal_engine/research/README.md` |
| Minimum complete strategy definition (template and gate) | `research/ideas/_template/spec.yaml`, `research/spec.py` |
| Strategy adapters | `backtest/strategies/` (`base.py` intraday, `swing_base.py` swing, long-only) |
| Simulators | `backtest/engine.py` (intraday), `backtest/swing_engine.py` (swing) |
| Costs, metrics, baseline, stats | `backtest/costs.py`, `metrics.py`, `baseline.py`, `stats.py` |
| Market regimes | `backtest/regime.py`, `docs/REGIME-GATE-CALIBRATION.md` |
| Gate scoring (G0-G9, no single score) | `backtest/evaluation.py`, `docs/STRATEGY-ACCEPTANCE-CRITERIA.md` |
| Is the idea still improving or exhausted | `research/evolution.py` |
| Generated index of all ideas | `research/REGISTRY.md` (rebuild: `uv run python -m signal_engine.research.build_registry`) |
| Export and Backtests page | `backtest/export.py`, `frontend/src/` backtest results page |
| Review of the benchmarking and what is built | `docs/BACKTEST-BENCHMARKING-REVIEW.md` |

For modifying any of this, load skill `signal-engine-backtest` first.

## Facts worth stating correctly

- Swing and positional trading is long-only (Indian delivery market); intraday may go long
  and short. The swing engine enforces it; it is not left to run config.
- All analysis timestamps are IST, converted where data enters (`signal_engine/timeutils.py`).
- No strategy goes live before passing the acceptance criteria; a rejected verdict needs a
  recorded basis in its `idea.md`.
- Conventions: use `uv`, log with `get_logger`, no emojis, no `print()`.

## Commands

- Tests: `PYTHONPATH=. uv run pytest signal_engine/tests/ -v`
- Rebuild registry: `uv run python -m signal_engine.research.build_registry`
- Evolution verdicts: `uv run python -m signal_engine.research.evolution`
