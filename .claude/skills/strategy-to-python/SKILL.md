---
name: strategy-to-python
description: Port a trading strategy from any source format (PineScript, LipiScript, a video/article description, a legacy or third-party script) into Python for backtesting and optional live deployment in OpenAlgo's signal_engine. Use whenever asked to "convert", "port", "rewrite in Python", or "migrate" a strategy, or when a strategy's live platform (e.g. Pine's request.security 40-symbol cap) is constraining what a validated backtest edge can actually trade. Chains from strategy-from-video (extract testable rules first) and pinescript-strategy (if the source is Pine); chains into fd-audit if the port adds a scheduled/long-running process.
---

# Porting a strategy to Python

The job is not "translate the syntax". It is "produce a Python implementation whose
signals are numerically provable to match the source logic, backtested on the full
available universe, with an honest, separately-labeled account of what the strategy is
worth versus what any given live platform can actually deliver."

Skipping the numeric-parity step is the most common way a port silently changes the
strategy while looking like a faithful translation — an off-by-one in a lookback window,
a `>` that should be `>=`, a skip applied to the wrong side of a ratio. All of these
compile, run, and produce a plausible-looking backtest that is quietly wrong.

---

## 1. Extract the logic before writing any Python

- **Source is a `.pine` file**: read it in full. Note every input default, every
  `request.security` call and its lookahead setting, the exact alert message format
  (`parser.py`'s "STRATEGY DIRECTION" contract if it's a signal_engine strategy), and any
  comment explaining *why* a value was chosen — that reasoning is often the only record
  of a parameter sweep already done, and re-deriving it from scratch wastes the sweep.
- **Source is a video/article/thread**: use the `strategy-from-video` skill first. Do not
  proceed until vague discretionary rules ("looks like a breakout") have been converted
  into testable ones (a specific range, a specific volume multiple).
- **Source is LipiScript**: use the `lipiscript` skill's documented deviations from Pine
  before assuming a construct means the same thing.
- **Source is a description with no code**: get the exact rule from the user before
  writing anything. Ambiguity resolved by guessing produces a strategy that was never
  actually specified.

Write down, in one place, before touching Python: the entry rule, the exit rule(s)
(stop, target, time exit, any strategy-specific exit), the universe it's meant to trade,
and the rebalance/signal frequency.

## 2. Classify the strategy shape — this picks the backtest engine

| Shape | Example | Engine |
|---|---|---|
| Per-symbol entry/exit, independent trades | ORB, breakout, EMA crossover | `signal_engine/backtest/harness.py`'s `Backtest` + `engine.py` (intraday) or `swing_engine.py` (swing), via a `Strategy` subclass — see `signal_engine/backtest/strategies/base.py` for the ABC contract (`prepare`, `entry`, optional `trail`/`custom_exit`/`on_bar`) and any file in that directory as a worked example |
| Cross-sectional ranking / rotation across many symbols | 12-1 momentum rank, sector rotation | `signal_engine/backtest/portfolio.py`'s `PortfolioBacktest` + `build_factor()` — this needs the whole universe visible on the same date, which a per-symbol loop cannot express |

Do not force a ranking strategy through the per-symbol harness or vice versa — the
mismatch either can't express the logic or silently drops the cross-sectional
comparison that IS the strategy.

## 3. Backtest on the FULL available universe — never a deployment-constrained subset

**This is a standing project rule, not a per-task choice** (see the `feedback-backtest
-full-universe` memory). Use `signal_engine/backtest/data.py`'s full universe —
`NSE_FNO` / `data.from_historify_daily()` / `data.from_historify()` — for every
backtest and parameter sweep, regardless of what any specific live platform can hold.

If the target live platform has a real capacity limit (Pine's `request.security` cap of
40 calls; a broker's per-instrument WebSocket cap; anything else), that is a **separate,
explicitly labeled finding** — never let it narrow the backtest's scope or quietly become
the headline number. Report both: "the strategy's real edge, full universe" and "what
this specific platform can currently deliver", and size the gap between them before
recommending a live config.

## 4. Write the port, then prove it matches the source numerically

- Reimplement the scoring/entry/exit formula as a small set of pure functions with no
  I/O — easy to unit test, easy to prove equivalent to the source.
- If the source already has a working reference (a `.pine` file, an existing
  `build_factor` factor, another team's script), write a test that runs BOTH
  implementations against the same synthetic input and asserts numeric equality
  (`pytest.approx`, tight relative tolerance). This is cheap insurance against
  transcription bugs and belongs in the test file permanently, not as a one-off check —
  see `strategies/examples/tests/test_momentum_rank_core.py`'s parity check against
  `portfolio.py`'s `build_factor("mom_skip", ...)` for the pattern.
- Run the actual backtest CLI and read the OOS row, not the IS row
  (`uv run --group analysis python -m signal_engine.backtest <name>` for a per-symbol
  strategy; a `PortfolioBacktest.report()`/`cost_curve()` script for a ranking one).
  Apply `metrics.hurdle_t(n_trials)` — count every configuration actually searched
  across every sweep, not just what's finally reported — before calling a result real.

## 5. Decide the live-deployment path explicitly — do not default to Pine out of habit

Two live paths exist in this codebase, with very different constraints:

| Path | Good for | Constraints |
|---|---|---|
| **TradingView Pine** (`signal_engine/pinescripts/`) | Per-symbol strategies, small ranking universes | `request.security()` capped at 40 calls per script — a hard ceiling on any cross-sectional strategy's universe size |
| **Python-native, `/python` Strategy Host** (`strategies/`) | Any universe size, anything Pine can't express | Needs its own scheduling logic (see `strategies/README.md`'s APScheduler/cron-trigger model), its own state persistence between runs (Pine's chart-local `var` state doesn't exist across process restarts), and its own market-data fetch (via the `openalgo` Python client's `history()`, matching `signal_engine/backtest/data.py:from_openalgo()`'s call shape) |

If the backtest-validated config can't fit the chosen platform's constraints (this is
common for ranking strategies against Pine's 40-symbol cap), say so and let the user pick
— don't silently degrade the config to fit, and don't silently start a platform migration
without asking. See `AskUserQuestion` with concrete options (keep the constrained
platform and re-tune for it vs. migrate vs. defer).

### If wiring into signal_engine's automatic execution (Telegram -> parser -> risk -> executor)

Check, don't assume, before connecting a new strategy's alerts to the live pipeline:

- **`signal_engine/main.py`'s `strategy_profiles.get(tag, {})` pattern**: an
  unregistered strategy tag does not error — it silently falls through to global
  defaults. If the new strategy's alerts share a Telegram chat the live daemon already
  polls, they WILL be parsed and acted on, sized however the engine's global
  `sizing.mode` currently works, the moment they're correctly formatted — even before
  you've decided that's what you want.
- **`signal_engine/risk.py`'s sizing model**: `_fixed_fractional` requires a real,
  meaningful stop-loss distance (`qty = risk / |entry - sl|`); `_pct_of_capital` doesn't
  need SL distance but the `Signal`/`parser.py` schema still requires SL/TP fields to be
  present. A strategy with no real stop (exits on a ranking change, not a price) needs
  either a synthesized placeholder SL/TP (the existing, documented pattern — see
  `momentum-rank.pine`'s `slPct`/`tpPct` inputs) or a genuine sizing-mode extension,
  which is real, first-of-its-kind engineering work — check `strategy_profiles`/
  `strategies.py`'s `StrategyMeta` registry for precedent before assuming one exists.
- **Default to a safe first phase**: if the sizing/registration question isn't resolved
  yet, ship the ranking/decision engine first and have it emit a **human-readable digest
  only** — deliberately NOT the strict machine-parseable alert format — to a separate
  chat the trader reads and acts on manually. This closes the "backtest vs. deployable"
  gap without accidentally turning a paper-only strategy into a live order-placer as a
  side effect of an infrastructure change. Flag full auto-execution as an explicit,
  separately-scoped next phase.

## 6. Document and test like every other strategy in this repo

- Dated entry in the strategy's `STRATEGY-ANALYSIS.md` (or create one) — what changed,
  what was tested, what didn't move, honest limits, status (candidate/paper vs. live).
- Update `signal_engine/PRD.md`'s strategy table/fine-tuning-plan entry.
- Unit tests for every pure function (score, rank/select, diff, any date/session-count
  logic) under a `tests/` directory next to the port, run via `uv run pytest`.
- Close the technical write-up with a layman-terms summary: how does a trader actually
  use this — manual or automatic, what order type, how is rotation/exit decided, what do
  the SL/TP fields in an alert actually mean if the strategy has no real stop.

## Worked example

The 2026-09-15 momentum-rank port (12-1 cross-sectional momentum, `.pine` -> Python)
exercises every step above:

- Source: `signal_engine/pinescripts/swing/momentum-rank/momentum-rank.pine`
- Full-universe backtest, reused engine: `signal_engine/backtest/portfolio.py`
  (`PortfolioBacktest`, `build_factor("mom_skip", ...)`)
- Port + parity test: pure ranking functions inline in
  `strategies/examples/momentum_rank_strategy.py` (single-file, because the
  `/python` host only accepts one uploaded `.py`), tested from
  `strategies/examples/tests/test_momentum_rank_core.py`
- Live runner, deployment-gap-aware, digest-only by design:
  `strategies/examples/momentum_rank_strategy.py`
- Full narrative, including the platform-constraint decision fork and the Phase 2
  auto-execution deferral: `signal_engine/pinescripts/swing/momentum-rank/
  STRATEGY-ANALYSIS.md`'s 2026-09-15 entries.
