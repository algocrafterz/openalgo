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

## 8. Open decisions for the implementer (answers recorded 2026-09-30)

- **History endpoint vs WebSocket-built bars:** history endpoint at bar close. Request today only
  (`start_date = end_date = today`, 5m). Prior-session bars come from Historify at warm-up. No WebSocket in v1.
- **`_replace_runner_sl`:** add an `override_sl` argument (not a bypass), threaded from
  `signal.context["ladder_sl"]`, so the tighten-only guard, SL placement and failure notice stay in use and
  the Ladder is the one stop rule. Built in Phase 5 (`main._ladder_sl_override`, `_place_runner_sl`).
- **Live universe:** an explicit list in `runner.universe` for the first shadow run, then widen once cycle
  time is measured. The Pine watchlist is a TradingView list and is not in this repo, so the shipped default
  is `fno_all` (harmless while `runner.enabled` is false). Backtests stay on the full F&O universe.
- **Exit plan default:** `tp1_only`.
- **Where last-processed-bar state lives:** its own `signal_engine/data/runner_state.db`
  (`pipeline/state.py`), keyed (mode, day, strategy, symbol). Signal-id dedupe also reads `trades.db`
  (`db.sig_id_exists`, DECLINED rows included), so nothing is duplicated there.
- **Package name:** `signal_engine/pipeline/` rather than `runner/`, because it is the shared path for
  backtest, replay, paper and live, not only the runner.

### Build status

| Phase | Scope | Status |
|---|---|---|
| 1 | `Strategy`/`Fire` protocol, `BreakoutStrategy`, `fire_to_signal`, `pretrade`, clock helpers; backtest BREAKOUT adapter re-pointed at `BreakoutStrategy` | done |
| 2 | `bars` (closed-bar rule, `ReplaySource`, `OpenAlgoHistorySource`, retry-until-present), `RollingContext`, P1/P2 parity | done |
| 3 | `state`, `cycle`, `runner`, `RecordingSink` (shadow), `runner` config section, single-source guard in `handle_message`, replay CLI | done |
| 4 | `dispatch_signal` extraction, `DispatchSink`, start-up wiring (`pipeline/service.py`), health checks | done |
| 5 | `ExitDriver`, `override_sl`, tracker `exit_owner`, `SimSink`, P3 trade parity | done |
| 6 | Fake-`api_client` replay of the real entry path, `ladder_30_35_35` persistence, process pool | later |

Start-up wiring is in (Phase 4): `startup._run_engine` checks the signal-source config first (refuses to start
on a mismatch, whether or not the runner is enabled), then starts one session loop per configured strategy
next to the tracker task, cancels it in the same `finally` and closes the state store in `_close_resources`.
`runner.enabled: false` (the shipped default) means no task is created. `service.start` fails closed: if the
history endpoint is unreachable or no symbol warms up it logs at critical level and does not start.

### Phase 4 and 5 decisions (2026-09-30)

- **`exit_owner` lives in `runner.strategies.<TAG>`**, not `strategy_profiles.<TAG>`: the runner block already
  holds each strategy's `signal_source` and `exit_plan`, and a second per-strategy location would be two
  places to keep in step. Values `tracker` (default in config.yaml, tracker exits unchanged) or `ladder` (the
  shared Ladder is the only exit rule; the tracker skips no-progress, loss-cut and profit-lock for that
  strategy, because the backtest never models them). `ladder` requires `signal_source: python`, enforced at
  start-up by `single_source.check_consistency`.
- **Ladder stop hit sends no order.** The broker SL-M owns that exit. If the tracker still holds the position
  `SAFETY_BARS` (2) bars later, the driver asks the broker: flat -> EXIT `tp_level="SL"` (reconcile the
  tracker only); still holding -> a plain full EXIT (market close); API error -> retry next bar. This differs
  from the original plan, which always sent the reconcile EXIT: `_reconcile_sl_hit` cancels the live stop and
  forgets the position, so it is only safe when the broker is already flat.
- **Fractions.** The Ladder's fill fractions are of the ORIGINAL position, `_resolve_exit_qty` reads
  `exit_qty_pct` as a fraction of the CURRENT quantity, so the driver sends `frac / remaining_before` (rounded
  to 9 places; exactly 1.0 for the last leg so floor() never leaves a share behind).
- **Lazy Ladder.** Created from the tracker on the first bar that closes after the position's entry time,
  anchored to the actual fill price (entry price, logged, if the fill is unknown). `tp1_only` can be rebuilt
  from tracker state alone after a restart; `ladder_30_35_35` needs `tp2`/`tp3` from the position context,
  which startup restore does not bring back, so such a position is logged once and left to the tracker.
- **Sink hooks.** `ExecutionSink` gained `on_bar` (before the driver steps a bar) and `after_bar` (after);
  both are no-ops for real-broker sinks. They exist so `SimSink`, the simulated broker, can fill entries at the
  next open, trigger stops before targets, and take the time exit after targets, in the engine's order.
- **After the entry window** the runner keeps cycling, but only for symbols the tracker still holds, until the
  time exit (`Runner(manage_until_min=...)`, config time_exit). Live stops one bar before the 14:45 time exit
  (the tracker takes over); the replay runs the time-exit bar so the close matches the engine.
- **Halt.** A halted cycle (mode guard) steps nothing, exits included: the tracker, the broker SL-M and the
  time exit still protect open positions, and sending orders in a mismatched mode is the worse risk.
- **Not handled:** exchange holidays (the session loop skips weekends only; a holiday cycle logs "bar late"
  per symbol until the deadline), and a Ladder rebuilt after a restart for a multi-leg plan (above).

### Parity results (2026-09-30, real Historify data)

`python -m signal_engine.pipeline.replay --parity --days 20 --limit 10` (10 most liquid symbols, last 20
sessions to 2026-09-29, P2 reference = 250-session history): 200 symbol-days, 78 fires. P1 200/200 identical,
P2 200/200 identical (0/78 mismatched fires). `warmup_sessions` stayed at 20; no tuning needed. Small sample:
rerun with a wider universe before relying on it for G7.2.

### P3 trade parity (2026-09-30)

`python -m signal_engine.pipeline.replay --trade-parity --days 5 --limit 6 --plan <plan>`: the real Runner,
cycle, RollingContext, ExitDriver and Ladder with `SimSink` as the broker, against `engine.simulate` with the
same `ExitPlan`, same strategy object, same bars (engine on 250-session history, replay on the 20-session
window). Real Historify data, 6 most liquid symbols, 5 sessions to 2026-09-29:

| Plan | Engine trades | Replay trades | Matched | Engine only | Replay only | Differing |
|---|---|---|---|---|---|---|
| `tp1_only` | 14 | 14 | 14 | 0 | 0 | 0 |
| `ladder_30_35_35` | 14 | 14 | 14 | 0 | 0 | 0 |

Synthetic-data tests (`tests/test_pipeline_trade_parity.py`) also match exactly for `tp1_only`, the three-leg
ladder with stop moves (2 and 3 leg exits present), an early time exit and a per-day trade cap. Tolerance:
prices 1e-4 x price (the engine casts bars to float32), `r_gross` 2e-3.

The first real-data run found one mismatch (BPCL 2026-09-29, an engine trade the replay never took). Cause: an
entry armed on the LAST bar of the entry window was never filled, because after the window the cycle only
processed symbols the tracker already held, and a not-yet-filled order is not held. Fixed with
`ExecutionSink.has_pending` (the cycle keeps processing a symbol with an unfilled order). Live is not exposed
to the same gap (a MARKET entry fills within seconds, well before the next cycle), which is why it needed
real data and a simulated broker to surface. Regression test: `TestPendingEntryAfterTheWindow`.

Known gaps in the comparison, not hidden: 14 trades is a small sample (widen with `--days 20 --limit 10`, about
40 minutes); the pipeline's pre-trade gates (minimum reward:risk, blacklist, price band) are not modelled by the
engine, so a trade the engine takes can be absent from the replay with a named skip reason (none occurred in
this sample; `format_diff` prints the skip reason for every engine-only trade); the live path's real order
placement, sizing and margin are not replayed (Phase 6, fake `api_client`).

### Findings that shape later phases

- `validator.validate` has no staleness check (`stale_signal_seconds` is read only by `listener.py`), so the
  pipeline carries its own (`pretrade.is_stale`).
- One `BreakoutStrategy.evaluate` on a 20-session window costs about 0.12 s, dominated by
  `features.volume.session_volume_factor` (a `stack` over the window). At 40 symbols a cycle is about 5 s
  sequential; the full 209-symbol universe would exceed the 25 s cycle deadline. Optimise only behind the
  P1/P2 parity tests.
- A restart mid-day does not backfill bars missed while the engine was down: such a bar would be stale
  under `risk.stale_signal_seconds` anyway.

### Shadow run preparation (rollout step 2, 2026-09-30)

**State.** `runner.enabled: true` with BREAKOUT still `signal_source: pine` / `exit_owner: tracker`. The runner
uses `RecordingSink`, which places nothing; Pine alerts trade exactly as before. It takes effect at the next
engine restart (config is read at start-up).

**What shadow mode records.** With a sink that has `records_shadow` (only `RecordingSink`), the cycle writes to
`signal_engine/data/runner_state.db`, table `shadow_events`: every signal it would have sent, every fire it
rejected (price band, pre-trade gate, entry window, halted), and every bar it could not evaluate (late, stale,
warm-up failed), each with its reason. In shadow mode the dedupe ignores trades.db (`seen_sig_id(...,
include_trades=False)`): otherwise a sig_id Pine already sent would look "seen" and the runner's matching signal
would never be recorded, making every match look like a miss.

**Report.** `uv run python -m signal_engine.pipeline.shadow_report --day YYYY-MM-DD` compares the shadow record
with Pine's BREAKOUT entry rows in trades.db (read-only, DECLINED rows included) by sig_id and prints matched,
direction-mismatch, runner-only and pine-only, with a reason for each pine-only row (outside the universe, the
runner's own skip reason, evaluated and did not fire, or did not process the bar). Informational only.

**Universe decision (40 symbols).** Pine's watchlist lives in TradingView, not in this repo. Built as: the
symbols Pine has signalled that sit inside the BREAKOUT entry price band (Rs 300-5000, 16 of 21), then filled to
40 with the most liquid in-band F&O names, ranked by 20-session median rupee turnover (5-minute close x volume,
summed per session, sessions to 2026-09-29, Historify). Out-of-band names cannot trade, so they were not
ranked. A pure top-40 by turnover would have covered only 12 of Pine's 21 signalled symbols. `GVT&D` was dropped
after the history endpoint returned HTTP 500 for it (`M&M` works) and replaced by the next name, `SUNPHARMA`.

**Bug found by the dry run: fixed.** Replay tests passed while every symbol failed against the real history
endpoint (`'Index' object has no attribute 'date'`). Live bars carry a fixed +05:30 offset; Historify warm-up bars
carry the Asia/Kolkata zone. `RollingContext._join` concatenated them into an object Index. It now converts
today's bars to the warm-up frame's timezone first. Regression test:
`test_live_shaped_today_frame_joins_warmup_bars_into_a_datetime_index`. The parity tests could not see this
because both sides of those tests came from Historify.

**Measured cycle time (real endpoint, 40 symbols, 24 cycles of 2026-09-30, one process, evaluation in a thread
pool):** median 13.5 s, typical 9-20 s, one outlier at 61.5 s (11:00 bar). This is worse than the 5 s estimate
(0.12 s x 40), because evaluation runs in threads under the GIL plus history fetches. Against the 25 s
`cycle_deadline_seconds` the typical cycle fits, but the outlier would be marked stale live (the replay clock does
not advance during processing, so the dry run reported 0 stale). Extrapolation: 209 symbols would take roughly
50-100 s per cycle and does not fit; widening needs a faster evaluate path (process pool, or incremental inputs
behind the parity tests) first. Watch `pipeline cycle ... in N.NNs` in the engine log tomorrow.

**Dry run comparison (full day 2026-09-30, real history bars, 40 symbols, against the real Pine rows in trades.db):**
matched 0, runner-only 32, pine-only 13. Of the 13 pine-only rows, 4 were outside the universe (all DECLINED by
Pine) and 9 were bars the runner evaluated without firing. So on this day the two sources did not agree on a
single signal. This is informational and is NOT tuned away (broker data is the source of truth, Pine is not the
reference), but it is a real open question to understand before step 3: the Python port may apply a different
score or confirmation filter than Pine, or the two may differ in how a bar's signal is stamped. One day is not
enough to conclude; compare several sessions and read the pine-only reasons.

### BREAKOUT-PY: paper trading beside Pine (2026-10-01)

**State.** The same BREAKOUT core paper-trades under its own tag, `BREAKOUT-PY`, while the Pine `BREAKOUT` runs
exactly as before. `runner.strategies.BREAKOUT-PY` is `signal_source: python`, `exit_plan: tp1_only`,
`exit_owner: ladder`, `paper_only: true`. It takes effect at the next engine restart.

**Why a separate tag.** One tag would share capital, slots, the duplicate window, trades.db rows and the P&L
ledger with Pine, so the two could not be compared and Pine's counters would be disturbed. With its own tag the
risk engine gives it its own day-start capital pool and slot counters in ANALYZE mode, the tracker keys its
position as (symbol, BREAKOUT-PY), and a same-symbol Pine position is left alone (per-strategy symbol limit).
Analyze caps are already `max_open_positions: 0` and `max_trades_per_day: 0` (unlimited, per strategy), so no
cap change is needed for a fourth sandbox strategy.

**Wiring.** `strategies.REGISTRY` (`channel_base` None, `tradeable` False: no Telegram channel of its own, so
lifecycle messages go to the admin notify channel and it appears in the consolidated day summary; False also keeps
it out of the live worst-case sizing floor), `blacklist` and `strategy_profiles` (same BHEL block and 0.20% stop
floor as BREAKOUT), `pipeline/service.STRATEGY_FACTORIES` (`BreakoutStrategy(name="BREAKOUT-PY")`). The tag flows
through `fire_to_signal`, `sig_id` and the cycle's own dedupe.

**sig_id.** Pine shape (`TCS-20260930-0955`) is kept for `BREAKOUT`; any other tag is prefixed
(`BREAKOUT-PY-TCS-20260930-0955`). Without this, Pine's row for the same symbol and bar made the runner's
matching signal look "already seen" (the runner's dedupe reads trades.db) and would have paired the wrong rows
in the sig_id-keyed P&L ledger.

**Paper-only is enforced in code, not just config.** `runner.strategies.<TAG>.paper_only` is a required key.
(1) `check_consistency` refuses to start a python-sourced strategy with `paper_only: false`: live promotion must
be a deliberate code change after the acceptance gates. (2) `service.start` does not launch a paper-only strategy
when the engine started in live mode (critical log naming it). (3) `DispatchSink` checks OpenAlgo's CURRENT mode
on EVERY submit, entries and exits, and refuses unless it is analyze, with a critical log; an unreadable mode or
an engine started in live mode also refuses (fail closed). So a mode flipped to live mid-session stops dispatch
on the next signal, in addition to the mode guard's halt.

**Pine BREAKOUT is not in `runner.strategies`.** It was listed as pine/tracker for the 2026-09-30 shadow run.
With BREAKOUT-PY running the same core, a second session would only double the per-cycle evaluation (9-20 s of
a 25 s deadline). A strategy absent from the runner block is simply not runner-managed: its alerts are not
dropped and the tracker owns its exits. The shadow comparison now reads BREAKOUT-PY's own events, which a
dispatching sink also writes (signal and skip, same store):
`uv run python -m signal_engine.pipeline.shadow_report --day YYYY-MM-DD --runner-strategy BREAKOUT-PY`.

**Known, not fixed.** OpenAlgo's sandbox positionbook nets by symbol and product; per-strategy attribution
lives in this engine (tracker, trades.db, `openposition` called with the strategy tag). The existing ORB and
BREAKOUT pair already share names this way. Watch for a same-symbol BREAKOUT and BREAKOUT-PY position in the
sandbox: a full exit of one sells its own quantity, and the broker-side position shows the net.

**Update 2026-10-01 (first paper day, what the netting actually did).** The shared sandbox position let a Pine TP1 alert
sell 32 of BREAKOUT-PY's TCS shares through the restart-fallback exit, let the exit driver mistake the other strategy's
shares for its own (and never run its stop-failed check for shorts, which read as "unknown"), and let entry-timeout recovery
adopt an earlier closed fill as a new entry. Fixed in `main.py` / `tracker.py` / `pipeline/service.py`:
`PositionTracker.fetch_own_position` subtracts other tracked strategies' quantity from the broker net everywhere the engine
asks "how many shares do I hold". Still true: the tracker's own close detection keys on the net per symbol, so a stop fill by
one strategy is not noticed until the symbol's net reaches zero when both hold it. The same day exposed that exit P&L was
booked from the account-wide realised delta and the alert's target price rather than the order's own fill; see
`docs/PRD.md` ("Exit P&L From the Order's Own Fill", "Daily Check: Strategy P&L / Performance Pages vs Broker Fills").

## 9. Definition of done

- Runner disabled by default; enabling it needs explicit config.
- All new code covered at 80% or more, existing suite green (2347 tests after Phase 5; 2053 before the pipeline).
- Parity replay test passes on at least 20 historical days.
- Single-source guard tested; no path dispatches from both sources.
- Docs updated: this file's decisions, `docs/PRD.md`, and a dated note in the strategy log if behaviour changes.
- Nothing committed unless the owner asks.

## 10. Important warning

BREAKOUT currently fails the economics and significance gates (net -0.48R per trade, t about -101). The runner
is infrastructure for the next strategy that clears gates G1 to G5; build it, but do not deploy BREAKOUT live
on this evidence.
