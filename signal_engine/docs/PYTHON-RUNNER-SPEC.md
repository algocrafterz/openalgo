# Python signal runner: implementation spec

Self-contained brief for a fresh session. Read this file, `STRATEGY-ACCEPTANCE-CRITERIA.md` and
`PAPER-RUNNER-AND-LIVE-TRIGGER.md` (the concept), then build. Written 2026-09-30.

## 0. Ground rules (from the project owner; they apply to all work here)

- **One code path.** Backtest, walk-forward, paper (analyze) and live call the same strategy code:
  `signal_engine/features/` (inputs) and `signal_engine/strategy_core/` (`breakout.py`, `inputs.py`,
  `exits.py`). The runner contains no strategy rules.
- **Broker data only,** never TradingView numbers. Do not tune to match Pine.
- **All timestamps IST,** converted at the boundary where they enter the code. OpenAlgo's history
  endpoint returns UNIX epoch seconds (UTC); a missed conversion once silently disabled a strategy.
- **Tests first,** at least 80% coverage on new code. Run:
  `PYTHONPATH=. uv run pytest signal_engine/tests -q -p no:cacheprovider`, lint with `uv run ruff check`.
- **Always `uv`.** Logging via `logger` (loguru), errors with `logger.exception()`, no `print()`, no
  emojis or unicode in code, comments, logs, commits or docs.
- **Do not touch OpenAlgo core** (anything outside `signal_engine/`). Do not commit unless asked.
- **No silent exclusions.** Every skipped symbol, bar or signal is logged by name with a reason. Never wrap
  a batch in a catch-all (a whole batch of valid symbols was once dropped that way).
- **Fail fast on config:** new keys are read with `_require_key` in `config.py`, no in-code defaults.
- **Live-order safety:** nothing here may place a live order until the gates in
  `STRATEGY-ACCEPTANCE-CRITERIA.md` pass. The runner must work in analyze mode first.

## 1. Goal and non-goals

**Goal.** A long-running asyncio task inside the existing `signal_engine` process that, at every 5-minute bar
close, computes BREAKOUT signals in Python from broker data and feeds them into the existing pipeline, and
manages exits from the shared `Ladder`. The Pine alert path becomes optional per strategy.

**Non-goals (v1).** New strategies; changing risk sizing; the day-type counter-bias veto; same-day re-entry
cycles; a web UI; WebSocket tick handling (use closed bars); modifying `broker/*` or OpenAlgo core.

## 2. What already exists (reuse, do not rewrite)

| Need | Where | Notes |
|---|---|---|
| Inputs for a symbol | `strategy_core/inputs.py::build_breakout_inputs(df5m, df1m, tick)` | Causal; returns the frame `compute_signals` reads (`INPUT_COLUMNS`) |
| Signal logic | `strategy_core/breakout.py::compute_signals(frame, BreakoutParams)` | One row per fire, indexed by the signal bar; columns `code, direction, level, entry_ref, sl, risk, t1, tp1, tp1_5, tp2, tp3, score, conf, chase, headroom, tp_r, atr` |
| Exit model | `strategy_core/exits.py::{ExitPlan, Ladder}` | `Ladder(direction, fill, sl, targets, plan)`, `.step(o, h, low)`, `.sl_eff`, `.fills`, `.close_all(price, reason)` |
| Tick rules | `features/instruments.py::{tick_size, tick_for_price, safe_tick}`; `executor.round_to_tick` | Tiered NSE ticks; broker `tick_size` (symbol master) is stale above Rs 1,000 |
| Continuous session | `features/session.py::continuous_session` | Bars before 15:15; NSE Closing Auction runs 15:15-15:35 since 2026-08-03 |
| Pipeline entry | `main.py::handle_message(text)` -> `normalize` -> `parse` -> `validator.validate(signal)` -> `_handle_entry` / `_handle_exit` | The runner enters AFTER the parse step (see 4.1) |
| Signal object | `models.py::Signal` (pydantic) | `strategy, direction, symbol, entry, sl, tp, exchange, product, time, sig_id, tp_level, exit_qty_pct, context` |
| Orders and SL | `executor.py::{build_order, place_sl_order, round_to_tick}`; `main.py::_handle_entry`, `_place_entry_bracket`, `_auto_close_on_bad_fill` | Includes the `bracket.max_entry_slippage_r` guard |
| Exits today | `main.py::_handle_exit` (Pine "TP HIT" as `Signal(direction=EXIT, tp_level, exit_qty_pct)`), `_replace_runner_sl`, `structural_runner_sl` | See 4.4: this duplicates the Ladder's stop rule |
| Positions and time exit | `tracker.py::PositionTracker` (`start`, `time_exit_all`), `risk.py::RiskEngine`, `mode_guard.py` | Time exit is 14:45 (`config.yaml time_exit`) |
| Broker HTTP | `api_client.py` (shared client, `_post_json`, `fetch_trading_mode`, `fetch_orderbook`, ...) | Use the shared client with explicit timeouts |
| Bars over HTTP | OpenAlgo `POST /api/v1/history` (see `analysis/breakingtrade/validate.py::fetch_bars`) | Payload `apikey, symbol, exchange, interval, start_date, end_date`; response `data[].timestamp` in epoch seconds |
| Historical bars | `backtest/data.py::from_historify` (DuckDB, read-only, 1-minute source of truth) | Heavy file: use for warm-up and replay, not per-bar polling |
| Engine wiring | `startup.py::start_engine` / `_run_engine` (creates the listener and `tracker.start()` tasks) | Add the runner as one more task here |
| Strategy registry | `strategies.py::REGISTRY`, `config.yaml` `telegram.channels`, `strategy_profiles` | BREAKOUT is already registered |

## 3. Target flow

```
bar clock (every 5 min, IST) -> BarSource.fetch_closed_bars(symbols)      [broker /api/v1/history, forming bar dropped]
    -> RollingContext.update(symbol)  (warm-up window + today's bars; 1m bars for prior sessions' value area)
    -> build_breakout_inputs -> compute_signals -> take the row (if any) whose index == the just-closed bar
    -> fire_to_signal(row) -> Signal(strategy="BREAKOUT", ...)
    -> dispatch_signal(signal)   [extracted from handle_message: validate -> _handle_entry]
    -> existing risk gates, sizing, MARKET entry, SL-M (tick-safe), notifications, tracker registration
open positions: each bar -> ExitDriver steps Ladder -> emits exits through the existing exit path
```

## 4. Components to build

Suggested package `signal_engine/runner/` (small files, each under ~300 lines):

### 4.1 `dispatch_signal` (small refactor in `main.py`)
Extract the tail of `handle_message` (validate, `_decline` on failure, `contextualize`, dispatch to
`_handle_exit` / `_handle_entry`) into `async def dispatch_signal(signal: Signal) -> None`. `handle_message`
keeps `normalize` + `parse` and then calls it. Existing behaviour and tests must be unchanged. The runner
calls `dispatch_signal` directly with a real `Signal`; never fabricate Telegram text.

### 4.2 `feed.py`: `BarSource`
- Protocol: `async fetch_closed_bars(symbols, interval, day) -> dict[str, pd.DataFrame]`.
- `OpenAlgoHistorySource`: `POST /api/v1/history` through the shared client (explicit timeout, bounded
  concurrency), converts epoch seconds to tz-aware IST at once, sets `Open/High/Low/Close/Volume`, **drops
  the still-forming bar** (a bar is closed only when its open time + interval <= now), and raises a named
  error listing every symbol that returned nothing.
- `ReplaySource(historify)` for tests and the parity replay, returning the same shape.
- Retry with short backoff until the just-closed bar is present, up to a per-cycle deadline (config).

### 4.3 `context.py`: `RollingContext`
- Warm-up once at start-up (and lazily for a new symbol): about 20 prior sessions of 5-minute bars plus the
  previous 2 sessions of 1-minute bars per symbol, from Historify (read-only, batch by symbol, then close the
  connection; mind FD hygiene). ADR needs 14 sessions, volume factor 13 prior sessions.
- Each cycle: append today's closed bars, call `build_breakout_inputs(window5m, window1m, tick)` with the
  1-minute frame trimmed with `continuous_session`, then `compute_signals`. Deterministic: recomputing the
  whole window every bar is intended (it is what the backtest does); optimise later only with a parity test.
- Tick: `features.instruments.tick_size(symbol)` for the value-area row step (as the backtest does).

### 4.4 Exits: `driver.py`, and the duplication trap (decide before coding)
Today `_handle_exit` books partials and calls `_replace_runner_sl`, whose stop rule (`structural_runner_sl`
+ TP-minus-0.3R buffer) is a second, hand-written copy of what `strategy_core.exits.Ladder` does. Building
the runner on top of it would break the one-code-path rule. Recommended approach:
1. `ExitDriver` keeps one `Ladder` per open runner-managed position (created at the fill, anchored to the
   ACTUAL fill price and the signal's `tp1/tp2/tp3` and `sl`), stepped with each closed bar's O/H/L.
2. When the Ladder fills a target it emits `Signal(direction=EXIT, tp_level="TP1", exit_qty_pct=<fraction of
   the remaining position>, ...)` into `dispatch_signal`, which reuses the tested booking, retry, SL-cancel
   and notification code in `_handle_exit`.
3. The new stop comes from `Ladder.sl_eff`, not from `_replace_runner_sl`. Give `_replace_runner_sl` an
   optional `override_sl` (or bypass it for runner-managed positions) so there is exactly one stop rule.
   Add a test that the placed SL equals `Ladder.sl_eff` after each target.
4. Plan choice comes from config (`exit_plan: tp1_only | ladder_30_35_35`); backtests found no exit design with
   a positive edge, so default to `tp1_only` until the acceptance process selects otherwise.
5. Forced exits: the 14:45 `time_exit_all` remains the safety net; also feed it into the Ladder
   (`close_all`) so state stays consistent. A Ladder stop hit is normally executed by the broker SL-M
   already placed; the driver must reconcile with the broker position, not double-sell.

### 4.5 `signals.py`: `fire_to_signal`
Map one `compute_signals` row to `Signal`:
- `strategy="BREAKOUT"`, `direction` from `row.direction` (1 -> LONG, -1 -> SHORT), `symbol`, `exchange="NSE"`,
  `product` from config, `entry=row.entry_ref` (the bar close), `sl=row.sl`, `tp=row.tp1`.
- `time`: signal bar time formatted the way the validator expects; `received_at` naive IST.
- `sig_id="{SYMBOL}-{YYYYMMDD}-{HHMM of the signal bar}"` (same shape Pine used, e.g. `TCS-20260930-0955`).
- `context`: `trigger=row.code`, `score`, `conf`, `chase`, `headroom`, `atr`, plus the level prices that
  `main.structural_runner_sl` reads (`pdh, pdl, ibh, ibl, orh, orl, pvah, ppoc, pval`) so any remaining
  downstream code keeps working; `tp2`, `tp3` for the exit driver.
- Keep the mapping pure and unit-tested; no I/O.

### 4.6 `runner.py`: scheduler and lifecycle
- Bar clock aligned to IST 5-minute boundaries with a configurable `bar_delay_seconds` (about 3) and a hard
  per-cycle deadline; skip the cycle (log by name) if data is late beyond the validator's stale-signal window
  (`risk.stale_signal_seconds`).
- Runs only inside the strategy window (BREAKOUT: 09:45 to 11:45 for entries) but keeps stepping open
  positions until they close. Stops entry evaluation at the time exit.
- Skips a symbol for the day when ineligible (log by name): price outside `min_entry_price..max_entry_price`,
  broker MIS-blocked, ASM/GSM/T2T, no valid tick.
- Idempotent restart: before dispatching, check `trades.db` (and the tracker) for the `sig_id`; persist
  "last processed bar per symbol" so a restart mid-day cannot re-fire a bar or skip one.
- Respect `mode_guard.is_halted()` and `_entry_halted`; never dispatch while halted.

### 4.7 Config (`config.yaml` + `config.py`, fail-fast)
New section, for example:
```yaml
runner:
  enabled: false                 # off until the acceptance gates pass
  bar_interval_minutes: 5
  bar_delay_seconds: 3
  cycle_deadline_seconds: 25
  history_timeout_seconds: 10
  max_concurrent_requests: 8
  warmup_sessions: 20
  universe: fno_all              # or an explicit list
  strategies:
    BREAKOUT:
      signal_source: pine        # pine | python  (exactly one source per strategy)
      exit_plan: tp1_only        # tp1_only | ladder_30_35_35
```
`Settings` fields are loaded with `_require_key`; add the keys to test fixtures in `tests/test_config.py`.

### 4.8 Single-source guard (mandatory)
For each strategy exactly one of `pine` or `python`. In `handle_message`, drop entry AND exit alerts for a
strategy whose `signal_source == "python"` (log with the count), and in the runner refuse to dispatch for a
strategy set to `pine`. At start-up, log the active source per strategy and refuse to start if the config is
inconsistent. Two active sources means duplicate orders.

### 4.9 Start-up wiring
In `startup._run_engine`, when `runner.enabled`, create the runner task next to the tracker task and cancel it
in the same shutdown path. Reuse the existing health checks; add "runner warm-up finished" and "history reachable".

## 5. Correctness rules and known traps

- **Closed bars only.** The forming bar must never reach `compute_signals`.
- **Continuous session for level profiles.** Value area uses bars before 15:15; nothing may depend on bars
  from 15:15 onward. SL and SL-M orders do not exist in the closing auction.
- **3 PM square-off.** Flattrade squares off MIS at 15:00; the time exit is 14:45. Entries stop earlier.
- **Ticks.** Never build order prices by hand: use `executor.round_to_tick`. Do not use the symbol master tick
  above Rs 1,000. Live acceptance of stop triggers per tick tier is still to be verified with the broker.
- **Slippage guard.** Entries are MARKET; the fill can drift from the signal price. `_auto_close_on_bad_fill`
  handles fills worse than `bracket.max_entry_slippage_r`; keep it in the path.
- **Universe vs Pine.** Pine watched about 40 symbols; the backtest used about 209. Decide the live universe
  explicitly (config) and log it at start-up. The risk engine's slot caps still apply.
- **Cost of a cycle.** About 0.05 to 0.2 s per symbol to rebuild inputs on a 20-session window. Run the
  computation in a worker thread or process pool if the loop blocks (signal_engine is asyncio, not eventlet;
  OpenAlgo core is the eventlet process and must not be touched).
- **FD hygiene.** One shared HTTP client, one bounded executor, Historify connections closed after use.

## 6. Tests to write first

1. `fire_to_signal`: field mapping, `sig_id` format, context keys, LONG and SHORT.
2. `OpenAlgoHistorySource`: epoch-to-IST conversion, forming bar dropped, missing symbols named, timeout.
3. `RollingContext`: window build matches `build_breakout_inputs` on the same data (no drift).
4. **Parity replay:** for a set of historical days, feed Historify bars bar-by-bar through the runner's signal
   step and assert the fires equal `compute_signals` over the full day (this is gate G7.2 in code).
5. `dispatch_signal`: existing `handle_message` tests still pass; runner path reaches `_handle_entry`.
6. Single-source guard: pine-mode drops runner output, python-mode drops Pine alerts, inconsistent config
   refuses to start.
7. Idempotency: restart mid-day does not duplicate `sig_id`.
8. Exit driver: Ladder targets emit EXIT signals with correct `tp_level` and fractions; the placed SL equals
   `Ladder.sl_eff`; time exit closes the remainder once.
9. Late data: a cycle past the deadline is skipped and logged, not dispatched.
10. Soak in analyze mode against the live market for at least 20 sessions (manual, see section 7).

## 7. Rollout (do not skip steps)

1. Build and test with `ReplaySource` only; no network.
2. Run against live bars with `runner.enabled: true` but the strategy still `signal_source: pine` in a
   **shadow mode** that logs would-be signals without dispatching; compare with what Pine did (informational).
3. Flip the strategy to `python` in analyze mode; run 20 sessions and 30 trades (G7.1); check signal parity at
   least 95% (G7.2), slippage (G7.3) and tracking (G7.4).
4. Verify with a real small order per tick tier that Flattrade accepts the stop triggers (G6.4) and test the
   kill switch (G8.1).
5. Stage capital 25% -> 50% -> 100% (G7.5), at least 10 sessions per stage, with alert escalation on anomalies.
6. Only then disable Pine's trade alerts. Keep Pine on the chart for visual checks.

## 8. Open decisions for the implementer (record answers in this file)

- History endpoint vs WebSocket-built bars for the live feed (recommended for v1: history endpoint at bar close).
- Whether `_replace_runner_sl` is bypassed or takes an override (section 4.4).
- Live universe (about 40 like Pine, or the full F&O list).
- Exit plan default (`tp1_only` recommended until acceptance says otherwise).
- Where to persist last-processed-bar state (small SQLite under `signal_engine/data/`, or the existing trades DB).

## 9. Definition of done

- Runner disabled by default; enabling it needs explicit config.
- All new code covered at 80% or more, existing suite green (2053 tests at the time of writing).
- Parity replay test passes on at least 20 historical days.
- Single-source guard tested; no path dispatches from both sources.
- Docs updated: this file's decisions, `docs/PRD.md`, and a dated note in the strategy log if behaviour changes.
- Nothing committed unless the owner asks.

## 10. Important warning

BREAKOUT currently fails the economics and significance gates (net -0.48R per trade, t about -101). The runner
is infrastructure for the next strategy that clears gates G1 to G5; build it, but do not deploy BREAKOUT live
on this evidence.
