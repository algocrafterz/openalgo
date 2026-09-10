# Signal Engine — End-to-End Review (2026-09-11)

Reviewed as of `1da05cd95` on `feature/optimize-signal-engine-v2.0.2.1`, covering the
2026-09-06 -> 2026-09-11 arc: per-strategy risk isolation, live pooling, the ANALYZE/LIVE
channel split, the BreakingTrade watchlist second outcome, and per-strategy EOD summaries.

Scope: `signal_engine/` end to end — config, risk, sizing, execution, tracker, notifier,
listener, startup/reconciliation, databases, logs, scripts, and the BreakingTrade poller.

> **STATUS 2026-09-11 (post-fix).** Every P0 and P1 item below is FIXED, plus M1, M3 and M8.
> The suite is now **1172 tests passing**; coverage 78% overall with `listener.py` 25% -> 62%,
> `startup.py` 40% -> 52%, `risk.py` 88% -> 92%, and a new `mode_guard.py` at 98%. Each item
> carries its own status line. P2 (bar the three done) and P3 remain open — see the plan.
>
> (The four `test_telegram_integration.py` tests fail when the live engine is running: it
> holds `data/telegram.session` and Telethon's SQLite open returns "database is locked". They
> pass in isolation. That is the hazard `alerts.py`'s docstring already warns about, and it
> is now listed as **L7**.)

**Baseline health (pre-fix).** 1095 tests passed in 57s. Coverage 77% overall (`main.py` 98%,
`tracker.py` 87%, `risk.py` 88%, `validator.py` 97%). Ruff reports 194 findings, all
cosmetic. The recent per-strategy plumbing (`strategy=` threaded through every
`record_trade`/`record_close`/`record_rejection` call site) is complete and correct — no
missed call sites. `strategy_cards.py`'s content-hash pinning and `notifier.py`'s
`_send_and_pin_day_summary` are well-built and correctly fail-open.

The findings below are what the tests do not cover.

---

## P0 — Blockers before any live session

### C1. The documented two-gate safety rule is only half-implemented

**FIXED.** `listener._channel_phase()` reads the `-analyze`/`-live` suffix and `_phase_mismatch()` refuses any message whose phase disagrees with OpenAlgo's current mode — checked PER MESSAGE, so a mid-session flip is caught too. Refusals log CRITICAL, write a DECLINED row (`stage="channel_phase"`) and alert once per channel. Unsuffixed channels (`smidestn`) stay phase-agnostic. `test_listener_phase_gate.py`, 9 tests. `config.yaml`'s claim now matches the code.

`config.yaml`'s `channels:` block states:

> a channel only ever trades when BOTH (1) this entry's `enabled: true` AND (2) OpenAlgo is
> actually in that phase's mode.

Gate (2) does not exist in code. `listener._split_by_enabled()` filters on `ch.enabled`
alone, `listener._connect()` never consults OpenAlgo's mode, and the only
`fetch_trading_mode()` call on the entry path (`main._resolve_entry_quantity`, line 969)
uses the answer solely to decide whether to skip the margin check. Nothing anywhere
correlates a channel's `-analyze`/`-live` suffix with the mode the engine is running in.

Today four `-analyze` channels are `enabled: true` (orb, breakout, breakingtrade,
breakingtrade-watchlist). The moment OpenAlgo is flipped to LIVE — which is exactly what
promoting BREAKOUT requires — **all four begin placing real orders**, pooled into 2 slots on
~Rs 35k. The paper strategies would go live silently, with no error and no alert.

Fix: derive the phase in `_make_handler` (per message, using the same 60s-TTL cache
`notifier._current_phase()` already has) and refuse any message whose channel suffix does not
match, logging loudly and writing a DECLINED row. Per-message rather than per-subscribe, so a
mid-session flip is caught. Channels with no suffix (`smidestn`) keep current behaviour.

### C2. Mode is resolved once at startup; a mid-session flip split-brains the engine

**FIXED.** New `signal_engine/mode_guard.py` owns the process-wide cached phase (replacing `notifier`'s duplicate copy, which the listener gate would have made a third). `startup._run_engine()` arms it with `set_startup_phase()`; the tracker's poll loop calls `_check_mode_flip()` each cycle; a flip halts new entries, logs CRITICAL and alerts once. `main._entry_halted()` enforces the halt at the top of the entry pipeline — before the symbol and risk gates. Exits are never halted: a halt stops NEW risk, it must not strand an open position. `test_mode_guard.py` (10) + `test_entry_halt_gate.py` (4) + `test_tracker_mode_and_unrealised.py`.

`startup._run_engine()` calls `fetch_trading_mode()` exactly once and fans it out to
`apply_trade_mode()`, `db.set_trade_mode()`, and `logger_setup.set_mode()`. Nothing
re-checks. Meanwhile `notifier._current_phase()` and `alerts._current_phase()` both re-check
every 60 seconds.

If OpenAlgo's mode flips while the engine runs, the state diverges:

| Component | After a flip to LIVE |
| --- | --- |
| OpenAlgo order routing | real broker |
| `risk_engine` limits | still `analyze` profile: `max_open_positions: 0` (unlimited), all three loss limits `1.0` (off) |
| `risk_engine` counter isolation | still per-strategy, not pooled |
| `trades.db.trade_mode` | still `"analyze"` |
| `risk.db` counter rows | still keyed `mode="analyze"` |
| log file | still `signal_engine_analyze_*.log` |
| Telegram destination | flips to `-live` within 60s |

That is real money traded with every risk limit disabled and the audit trail labelled paper.

Fix: re-check the mode on the tracker's poll loop; on a change, halt new entries and send a
CRITICAL alert. Restarting cleanly under the new mode is the correct recovery — attempting to
migrate counters mid-session is not worth the complexity. C1's per-message gate is the
cheaper half of this and should land first.

### H1. Loss limits are silently off for the first signal after every restart

**FIXED.** New `RiskEngine._limit_capital()` falls back to the persisted `day_start_capital` when `last_known_capital` is 0, and a fresher in-session figure still wins. `test_risk_limit_capital_fallback.py`, 6 tests.

`risk.check_exposure()` gates all three loss checks on `capital = state.last_known_capital`
being `> 0`. That field is stamped only inside `calculate_quantity()` and is **never
persisted**. On a fresh process it is `0.0` for every strategy.

`main._handle_entry()` runs the gates before resolving capital:

```
_entry_passes_risk_gates(signal)     # line 841 -> check_exposure()
_resolve_entry_capital(signal)       # line 845
risk_engine.get_sizing_capital(...)  # line 851
risk_engine.calculate_quantity(...)  # line 959 -> stamps last_known_capital
```

So after a restart following a day that already hit the 4% daily limit, the next signal is
not blocked. `daily_realised_loss` *is* restored from `risk.db` correctly — it just is not
compared against anything.

Fix: in `check_exposure()`/`exposure_block_reason()`, fall back to `state.day_start_capital`
(already persisted in `risk.db`) when `last_known_capital <= 0`, and only skip the check when
both are zero. One-line change, and it makes the persisted counters actually load-bearing.

### H5. `sl_order_id` is lost on restart, reintroducing the broker OCO rejection

**FIXED.** New `api_client.fetch_orderbook()` and `startup._match_open_sl_orders()` recover the live stop id per symbol (working statuses only, SL/SL-M order types only, latest wins) and `_restore_tracker_positions()` attaches it. A symbol with no working stop is logged loudly rather than silently blank. `test_startup_sl_recovery.py` (9) + `test_api_client_orderbook.py` (4).

`startup._restore_tracker_positions()` registers restored positions with `sl_order_id=""`
(the comment acknowledges it as unknown). `main._cancel_sl_before_exit()` returns early when
`sl_order_id` is falsy. So after any restart, the next TP or EXIT signal places a SELL while
the broker's SL-M is still live — the exact condition documented as causing
`FUND LIMIT INSUFFICIENT` on Indian brokers.

Restarts are routine here: the supervisor restarts on crash, the ~03:00 IST broker token
rollover forces one, and the 2026-09-08 flood-wait incident produced 16 in 24 minutes.

Fix: fetch the orderbook during reconciliation and match open SL-M orders to restored
positions by symbol+side. Failing that, call `cancel_all_orders(strategy)` for the symbol
before the exit when `sl_order_id` is unknown, rather than no-oping.

---

## P1 — Fix before promoting any strategy to live

### H2. Weekly and monthly loss limits count gross losses, not net drawdown

**FIXED.** `_StrategyState` gains signed `daily/weekly/monthly_net_pnl`; the weekly and monthly gates measure net drawdown while the daily gate stays gross (the "four stops" reading is deliberate and now documented as such in `config.yaml`). `risk.db` gains a `daily_net_pnl` column via an idempotent in-place migration, backfilled from each row's own `-daily_loss` — the conservative reading, since it can only make a limit fire earlier. `test_risk_net_drawdown.py`, 7 tests.

`risk.record_close()` accumulates `abs(pnl)` only on the `pnl < 0` branch; wins never offset.
`RiskStore.weekly_loss()`/`monthly_loss()` sum that same gross column.

At Rs 35k with `risk_per_trade: 0.01` (~Rs 350 risk/trade after the slippage buffer):

| Limit | Config | Rupees | Losing trades to lock out |
| --- | --- | --- | --- |
| daily | 0.04 | 1,400 | 4 |
| weekly | 0.08 | 2,800 | 8 |
| monthly | 0.15 | 5,250 | 15 |

The daily reading matches intent ("4% daily = four full stops"). The weekly and monthly ones
do not: at `max_trades_per_day: 10` and a ~50% loss rate, 8 gross losses arrive in about two
sessions and 15 in about four — **the engine would lock out for the rest of the week, and
then the month, on a net-profitable account.** A month with 60 wins and 40 losses is halted
in week one.

Fix: track net realised P&L per period (or peak-to-trough drawdown, which is what a "loss
limit" normally means) and compare that. The daily limit can stay gross if the "four stops"
reading is deliberate, but weekly/monthly must net out — and if they stay gross, they need to
be re-derived as trade counts, not as percentages of capital.

### H3. Duplicate suppression ignores the strategy tag

**FIXED.** `sig_key` now leads with `signal.strategy.upper()`. `test_validator_dedupe_strategy.py`, 5 tests.

`validator._check_duplicate()` keys on `(symbol, direction, tp_level or entry)` — no
strategy. For EXIT signals `entry` is the synthesized `0.0`, so the key reduces to
`(SYMBOL, "EXIT", "TP1")`.

BREAKINGTRADE and BREAKINGTRADE-WATCHLIST are *designed* to hold the same symbol at the same
time with the same TP math. Two "TP1 HIT" alerts within `duplicate_window_seconds: 60` and
the second is dropped as a duplicate — **that position never exits** and rides to the 14:45
time exit. ORB and BREAKOUT share the same NSE universe and the same 5-min bars, so the same
collision is available to them.

Fix: add `signal.strategy` to `sig_key`. The parser always populates it from the alert
header, so this is a one-line change.

### H4. A global symbol cap confounds the confirmed-vs-watchlist experiment

**FIXED.** `_positions_by_symbol`/`_positions_by_sector` are keyed `(counter_key, name)` where `counter_key` is `_key(strategy)` — so both caps pool in LIVE and isolate in ANALYZE, matching every other counter. `can_trade_symbol`/`can_trade_sector` take a `strategy` argument. `test_risk_symbol_cap_isolation.py`, 7 tests.

`risk.can_trade_symbol()` is global across strategies by design (correlation risk), and
`max_positions_per_symbol: 1`. But `__main__._emit_trade_signals()` now emits both outcomes
for the same symbol and direction, and the watchlist fires first (no confirming-close wait).

On the poll where the scan hits, only the watchlist plan exists (`plan_trade()` returns None
until `entry_trigger()` sees a confirming close), so the watchlist takes the symbol slot. By
the time the confirmed plan is built on a later poll, the slot is gone and the signal is
declined with "symbol concentration limit" unless the watchlist position has already closed. The head-to-head the whole two-outcome design exists to produce is
biased toward the watchlist, and BREAKINGTRADE will show near-zero trades — which reads as
"the confirmation filter is too strict" rather than "the cap blocked it." (The declines are
recorded via `save_declined`, so it is recoverable offline — but the live comparison table
will be wrong.)

Fix: make `max_positions_per_symbol` per-strategy when `isolates_per_strategy` is True,
matching the isolation philosophy already applied to slots and capital. Keep it global in
LIVE, where correlated real exposure is the genuine concern.

### H6. Telegram FloodWaitError is not honoured; the engine dies and takes the tracker with it

**FIXED**, all three causes. (1) `_keepalive()` now reads `client.is_connected()` instead of calling `get_me()` — no API call, so it can no longer trigger the throttle it exists to detect. (2) `FloodWaitError` is caught explicitly and slept for its own stated `seconds` + jitter (capped at 900s), against a separate bounded budget so a flood wait never consumes the reconnect budget meant for genuine failures; `flood_sleep_threshold` is raised to 120s so only long bans surface at all. (3) `startup._serve_until_shutdown()` no longer ends the session when the listener gives up — it enters DEGRADED MODE: tracker, stop-losses, no-progress gates and the 14:45 time exit keep running, new entries are halted via `mode_guard`, and the operator is alerted. `test_listener_flood_wait.py` (4) + `test_startup_degraded_mode.py` (2).

Confirmed production incident, `logs/errors_2026-09-08.jsonl`, 11:15:50 -> 11:39:53 IST
(96 error records):

- 64x `A wait of N seconds is required (caused by GetUsersRequest)`
- 16x `A wait of N seconds is required (caused by InvokeWithLayerRequest(...))`
- 16x `Max retries exceeded, listener shutting down`

Three compounding causes:

1. `listener._keepalive()` calls `client.get_me()` every 90 seconds. That is the
   `GetUsersRequest` behind 64 of the 80 flood waits — the keepalive is what triggered
   the throttle it exists to detect.
2. `start_listener()`'s backoff is `base_backoff * 2**(n-1)` = 2,4,8,16,32s. Telethon's
   `FloodWaitError` carries the required wait (349s in the first record) and it is ignored,
   so every retry lands inside the ban window and extends it.
3. After `listener_max_retries: 5` the function *returns*. `_serve_until_shutdown()` treats
   that as completion, so `_run_engine()`'s `finally` stops the tracker and the time-exit
   scheduler. **The whole engine dies, not just the listener** — open positions lose SL
   monitoring, close detection, no-progress gates, and the 14:45 square-off.

Across those 24 minutes of NSE trading hours the stack cycled through 16 listener deaths and
supervisor restarts, each one tearing down the tracker and time-exit scheduler with it.

Fix: catch `FloodWaitError` explicitly and sleep `e.seconds + jitter`; raise Telethon's
`flood_sleep_threshold` so short waits are absorbed by the library; drop the keepalive to a
passive check (Telethon already tracks connection state — `client.is_connected()` costs no
API call); and on terminal listener failure, keep the tracker and time-exit scheduler running
in a degraded "manage existing positions, accept no new signals" mode, with a CRITICAL alert,
rather than exiting.

### M1. The unrealised-drawdown gate has no production caller

**FIXED (wired, not deleted).** `tracker._push_unrealised()` runs each poll off the same positionbook snapshot, sums each strategy's mark-to-market LOSS (a position in profit reports 0.0 — the limit ADDS this to realised losses, so a winner must not credit against it) and zeroes any strategy whose positions have all closed. 6 tests in `test_tracker_mode_and_unrealised.py`.

`RiskEngine.update_unrealised()` is called only from `tests/test_risk_counters.py`. In
production `state.unrealised_loss` is permanently `0.0`, so the `combined_daily =
daily_realised_loss + unrealised_loss` check in `check_exposure()` is just the realised
check. PRD.md and the project memory both describe this as an active feature.

Fix: either wire it (the tracker already computes per-position unrealised P&L on each poll —
sum it per strategy and call `update_unrealised`), or delete it and correct the docs. Leaving
it inert is the worst of the three.

---

## P2 — Correctness and observability

**M2. Daily rollover is checked at the wrong point.** `_maybe_reset_daily()` runs only inside
`check_exposure()`, but `get_sizing_capital()` is called first in `_handle_entry` and again
from `smoke_test`. Across a midnight-IST rollover on a long-running process, the first signal
of the new day reads the previous day's `day_start_capital`. Call `_maybe_reset_daily()` at
the top of `get_sizing_capital()` and `calculate_quantity()` too.

**M3. LIVE mode restores counter rows it can never read.** **FIXED** — `_restore()` maps
every stored key through `_key()` before loading it. `_restore()` loads every key
`RiskStore.strategies_for()` returns, but `_key()` maps them all to `PORTFOLIO`. Real
strategy-tagged rows written in live mode before the 2026-09-10 pooling change are loaded
into `_by_strategy`, never touched by `_state()`, yet summed by `total_open_positions()` and
`total_last_known_capital()` and printed by `log_startup_summary()` as phantom strategies.
Filter `_restore()` through `self._key()`.

**M4. Reconciliation filters the positionbook by the global product only.** It uses
`settings.product` and its broker code, ignoring `strategy_profiles.<TAG>.product`. A
strategy overridden to CNC would have its still-open position excluded from
`open_broker_symbols`, then found in `by_symbol` and booked as a reconciled exit — closing a
live position in the ledger while it is still open at the broker. Latent today (every profile
is MIS); it becomes live the day one is not.

**M5. Restart attribution guesses the strategy.** `_lookup_entry_trade()` tries `"ORB"`
first, then iterates `settings.strategy_profiles` in dict order. A symbol traded by two
strategies today is attributed to whichever is checked first. Match on the broker position's
quantity/side and prefer the most recent entry row.

**M6. Stale counters for idle strategies are never corrected.** The ANALYZE reconciliation
branch only iterates `known_strategies` derived from `locally_open` and the broker book. A
strategy left with a non-zero `open_positions` in `risk.db` but no local open rows keeps that
counter forever. Iterate every key in `_by_strategy` as well.

**M7. The consolidated day summary reports a capital figure no account holds.** In ANALYZE,
`total_last_known_capital()` sums four independent Rs 1L sandbox pools into Rs 4L, and the
`Net (X%)` line divides by it. The per-strategy summaries are correct; the consolidated
header is not. Either drop the capital/% line from the consolidated message or label it
explicitly as a sum of independent pools.

**M8. Stale signals vanish silently.** **FIXED** — now WARNING plus a DECLINED row. `listener._is_stale()` logs at DEBUG and writes no
DECLINED row. `stale_signal_seconds: 60` combined with the 09-08 outage means signals were
dropped with no audit trail. Log at WARNING and record a DECLINED row so "what did we miss"
is answerable.

**M9. Unknown mode routes safety alerts to the paper channel.** **PARTLY FIXED** —
`mode_guard.current_phase()` now returns the LAST KNOWN phase on an unreachable OpenAlgo
instead of always `"analyze"`, so a blip no longer reroutes live alerts. Broadcasting to both
phases when nothing is known yet is still open. Previously `notifier._current_phase()`
returned `"analyze"` unconditionally. During live trading, an SL-FAILED alert
then lands in the paper channel. When the mode is genuinely unknown, broadcast to every
configured phase — the same thing `openalgoscheduler.send_telegram_notification()` already
does for broker-login notices.

**M10. Adding a strategy means editing six hand-maintained maps.** `config.yaml`
`telegram.channels`, `strategy_profiles`, `blacklist`, plus `notifier._STRATEGY_CHANNEL_BASE`,
`strategy_cards.CARDS`, and `alerts._CHANNEL_NAME_BY_GROUP`. EMA9 and EMA9VWAP have the first
three and none of the last three — no channel, no card, no per-strategy EOD summary. There is
also no startup validation that channel names are unique, that ids are unique, or that every
strategy base has both `-analyze` and `-live` entries. Add a single strategy registry plus a
startup check that fails loudly on a gap.

**M11. Coverage was 77%, concentrated where the P0s lived** — now 78% overall, with
`listener.py` 25% -> 62%, `startup.py` 40% -> 52%, `risk.py` 88% -> 92%, `mode_guard.py` 98%.
Every fix above shipped with the test that would have caught it (+77 tests). The residual gap
is dominated by `backtest/` and the `pinescripts/*/trade-analysis` scripts, not the trading
path; `startup.py`'s CLI/lifecycle helpers are the remaining core gap. Original detail:
`listener.py` 25% (the phase gate and flood-wait handling both live in untested code),
`startup.py` 40% (the LIVE pooled reconciliation branch at 273-282 is uncovered),
`api_client.py` 68% (the margin path 341-375 is uncovered). Every fix in P0/P1 should arrive
with the test that would have caught it.

**M12. `alerts._warned_missing_config` is one flag for all alert kinds.** A missing BTST
channel suppresses the warning for a later missing watchlist channel. Key it by
`(group, phase)`.

---

## P3 — Performance, resources, hygiene

**L1. Synchronous SQLite on the asyncio loop.** Every `db.py` call opens a connection,
re-runs `CREATE TABLE`, `PRAGMA table_info`, and possibly `ALTER TABLE`, then closes — all
blocking, all on the event loop. The BreakingTrade poller is a separate process that also
touches `trades.db` (`flip_watch.py:52`), so a write-lock contention can block the loop for
up to `timeout=10`, stalling the position poll and SL placement. Hold one module-level
connection, run the schema check once at import, and move writes off the loop with
`asyncio.to_thread`.

**L2. `RiskStore` holds an un-closed connection** with no `check_same_thread=False` and no
`close()`. Fine while single-threaded and process-scoped, but it will surface the moment
anything touches it from a thread.

**L3. No HTTP connection reuse.** `api_client` constructs a fresh `httpx.AsyncClient` per
call. At `poll_interval: 5` that is roughly 700 TCP+TLS handshakes an hour against a
localhost OpenAlgo — cheap, but free to fix with one module-level client.

**L4. No retention policy on the analysis data.** `data/breakingtrade_profile` is 125 MB and
`breakingtrade.db` 26 MB, growing daily. Logs are rotated and retained properly (30d files,
90d errors); these are not.

**L5. 194 ruff findings**, all cosmetic (import ordering, PEP-585 annotations, three empty
f-strings in `risk.py:503,505` and `tracker.py:1089`). `ruff check --fix` clears 138.

**L6. Icons against repo convention.** `notifier._trade_line` uses `▲`/`▼`/`─`, and loguru
serialises its level icons (`❌`, `☠️`) into `errors_*.jsonl`. CLAUDE.md forbids icons in
source, logs, and Telegram text.

---

## Trader's read

**T1. The margin scaler quietly breaks the 1% risk contract downward.**
`adjust_qty_for_margin()` scales qty to fit live capital, so whenever slot 2 is taken the
actual rupee risk is below 1%. R-multiples stay comparable (they are per-share), but the
rupee P&L and every loss-limit counter no longer correspond to "N full stops" — which is
exactly the mental model H2's limits are written in. Log the realised risk-per-trade
alongside the R so the drift is visible.

**T2. `min_sl_pct: 0.002` is not fundable at Rs 35k.** As `config.yaml` itself derives,
notional = `risk_per_trade / (sl_pct x (1 + slippage))` and entry price cancels. At a 0.20%
stop that is 4.55x notional; at 20% MIS margin a single position needs ~91% of a Rs 35k
account. `max_open_positions: 2` is therefore aspirational at the tight end of BREAKOUT's
stop distribution — slot 2 exists mostly as a margin-scaled stub. Either accept one real slot
at this capital, or add an explicit notional/leverage cap so the undersizing is a decision
rather than a side effect.

**T3. Late entries can never reach the main no-progress gate.** `check_after_minutes: 90`
against a 14:45 time exit means anything entered after 13:15 is force-closed by the clock
first. Correct behaviour, worth knowing when reading the exit-type mix.

**T4. Expect the watchlist's R-distribution to be fatter on both tails.**
`plan_trade_watchlist()` enters at the scan-hit price with the *same* IB-derived stop as the
confirmed plan, so risk-per-share is smaller — bigger size, tighter effective stop, more
stop-outs and larger winners. Ranking the comparison table by avg R rather than net rupees is
the right call; do not compare the two on rupees.

**T5. `record_close` never nets wins, so a profitable day can still halt trading.** Same
mechanism as H2, but worth stating in trading terms: four stop-outs on a day that finished
+2R still trips the daily limit. If that is intended (loss-streak discipline), say so
explicitly in `config.yaml`; if not, it is a bug.

---

## Remediation plan

Sequenced so each phase is independently shippable and testable. Every item lands with the
test that would have caught it (M11).

### Phase 0 — before the next live session (blockers)

| # | Item | Files | Effort |
| --- | --- | --- | --- |
| 1 | C1 phase gate: reject a message whose channel suffix does not match the current mode | `listener.py` (+ new `test_listener_phase_gate.py`) | S |
| 2 | H1 loss-limit capital fallback to `day_start_capital` | `risk.py` | XS |
| 3 | H5 recover `sl_order_id` from the orderbook on restart | `startup.py`, `api_client.py` | M |
| 4 | C2 periodic mode re-check; halt entries + CRITICAL alert on a flip | `tracker.py`, `startup.py`, `notifier.py` | M |

Ship 1 and 2 first — together they close the "paper strategies go live silently" hole and
re-arm the loss limits, and they are the two smallest changes on this list.

### Phase 1 — before promoting any strategy

| # | Item | Files | Effort |
| --- | --- | --- | --- |
| 5 | H3 add `strategy` to the dedupe key | `validator.py` | XS |
| 6 | H4 per-strategy symbol cap in ANALYZE, global in LIVE | `risk.py`, `config.yaml` | S |
| 7 | H6 honour `FloodWaitError`; passive keepalive; degraded-mode survival | `listener.py`, `startup.py` | M |
| 8 | H2 net-drawdown weekly/monthly limits (decide daily's semantics explicitly) | `risk.py`, `risk_store.py`, `config.yaml` | M |
| 9 | M1 wire `update_unrealised` from the tracker poll, or delete it and fix the docs | `tracker.py`, `risk.py`, `PRD.md` | S |

Item 8 needs a decision first: is the daily limit "four stop-outs" (gross, as written) or "4%
drawdown" (net)? The weekly and monthly ones are only defensible as net.

### Phase 2 — correctness and observability

10. M2 rollover check in `get_sizing_capital`/`calculate_quantity`
11. M3 filter `_restore()` through `_key()`
12. M4 per-strategy product in the reconciliation filter
13. M5/M6 better restart attribution; correct idle strategies' counters
14. M7 drop or relabel the consolidated capital line
15. M8 stale signals at WARNING plus a DECLINED row
16. M9 broadcast to all phases when the mode is unknown
17. M10 single strategy registry plus startup validation (unique names/ids, both phases
    present, every enabled strategy has profile + card + channel)
18. M11 raise `listener.py` and `startup.py` coverage above 80%
19. M12 key the alerts warning by `(group, phase)`

### Phase 3 — performance and hygiene

20. L1 pooled DB connection, one-time schema check, writes off the event loop
21. L2/L3 `RiskStore` lifecycle; shared `httpx.AsyncClient`
22. L4 retention/VACUUM for `breakingtrade.db` and `breakingtrade_profile`
23. L5 `ruff check --fix` plus a CI lint gate
24. L6 strip icons from `notifier._trade_line`; drop loguru level icons from the JSON sink
25. T1 log realised risk-per-trade alongside R; T2 decide on an explicit notional cap

### Suggested config changes (no code)

- Re-derive `weekly_loss_limit` / `monthly_loss_limit` once H2's semantics are settled. As
  gross-loss counters they are far too tight for a 10-trade day.
- Add an `EMA9` / `EMA9VWAP` decision to `config.yaml`: either give them `-analyze` channels,
  cards, and `_STRATEGY_CHANNEL_BASE` entries, or state in the file that they are
  backtest-only and unreachable by the listener on purpose (the same reasoning the file
  already applies to absent-versus-disabled channels).


---

## Post-fix addendum

**L7. The Telegram integration tests fight the live engine for the session file.**
`test_telegram_integration.py` opens `data/telegram.session` directly while
`python -m signal_engine.main` holds it, and Telethon's SQLite session returns
`database is locked`. The four tests pass in isolation and fail in a full run whenever the
engine is up — which reads as a regression every time and is not one. This is precisely the
hazard `alerts.py`'s own docstring warns about ("Two processes sharing one Telethon session
file is a good way to corrupt it"). Point the tests at a copy of the session file, or skip
them when the engine's pid file is present.

## What changed, by file

| File | Change |
| --- | --- |
| `mode_guard.py` (new) | Process-wide phase cache; startup-phase arming; flip detection; sticky halt |
| `listener.py` | Per-message phase gate (C1); FloodWaitError handling and passive keepalive (H6); stale signals recorded (M8) |
| `risk.py` | `_limit_capital` fallback (H1); net weekly/monthly counters (H2); per-strategy concentration caps (H4); `_restore` keyed through `_key` (M3) |
| `risk_store.py` | `daily_net_pnl` column, idempotent migration, backfill from `-daily_loss`, net aggregates (H2) |
| `startup.py` | Arms the mode guard (C2); SL order-id recovery (H5); degraded mode instead of session end (H6) |
| `tracker.py` | `_push_unrealised` (M1); `_check_mode_flip` on every poll (C2) |
| `main.py` | `_entry_halted` gate (C2); concentration calls pass strategy (H4) |
| `validator.py` | Strategy in the dedupe key (H3) |
| `notifier.py` | Delegates phase to `mode_guard`; last-known-phase fallback (M9) |
| `api_client.py` | `fetch_orderbook()` (H5) |
| `config.yaml` | Documentation corrected to match the code on all four points |

## Still open

P2 items M2, M4, M5, M6, M7, M10, M12 and all of P3 (L1-L7) and T1-T5. None of them are
promotion blockers; M4 (per-strategy product in the reconciliation filter) becomes one the
day any `strategy_profiles.<TAG>.product` is set to something other than `MIS`.
