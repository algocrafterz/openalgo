# signal_engine execution flow

Status: verified 2026-10-22 against the code on branch `review/signal-engine-audit-2026-10-22`.
"Audit fix" marks behavior changed on that branch (not deployed). See [architecture.md](architecture.md)
for components and [gaps.md](gaps.md) for open issues.

## 1. Daily process lifecycle (who starts what)

```mermaid
sequenceDiagram
    autonumber
    participant WTS as Windows Task Scheduler
    participant CTL as scripts/openalgoctl.sh
    participant SCH as scripts/openalgoscheduler.py
    participant APP as OpenAlgo app.py
    participant ENG as signal_engine.main
    WTS->>CTL: 08:50 openAlgoAutoStart (dailystart)
    CTL->>APP: uv run app.py
    CTL->>SCH: openalgoscheduler startup (broker login, verify, summary)
    CTL->>ENG: uv run python -m signal_engine.main
    loop supervisor (inside dailystart)
        CTL->>APP: health probe, restart and alert on crash
        CTL->>ENG: restart on crash, give up after N and alert
    end
    WTS->>CTL: every 5 min 09:00-16:00 openAlgoWatchdog (start, no-op if healthy)
    WTS->>WTS: every 10 min 09:05-16:00 openAlgoHeartbeatCheck (popup if heartbeat stale)
    ENG->>ENG: 14:45 TimeExitScheduler squares off MIS
    WTS->>CTL: 14:55 openAlgoSquareOff (failsafe close via openalgoscheduler squareoff)
    WTS->>CTL: 16:00 openAlgoAutoStop (stop)
```

Note: the two repeating tasks miss the whole day when the PC wakes after their first trigger
(verified 2026-10-19, 10-20, 10-22); the script fix sets `StartWhenAvailable=true` (audit fix,
needs re-registration). See [data-pipeline.md](data-pipeline.md) for the cron jobs.

## 2. Engine startup (`startup._run_engine`)

```mermaid
flowchart TD
    A["main.main()"] --> B["startup.start_engine"]
    B --> C["run_startup_health_checks()"]
    C -->|None| X1["exit: health check failed"]
    C --> D["runner_config_ok()"]
    D -->|False| X2["exit: runner config invalid"]
    D --> E["fetch_trading_mode() from OpenAlgo"]
    E --> F["apply_trade_mode(risk_engine)<br/>mode_guard.set_startup_phase<br/>db.set_trade_mode, set_log_mode"]
    F --> G["reconcile_open_positions(risk, tracker)"]
    G --> G1["positionbook vs risk counters<br/>restore TrackedPositions from trades.db<br/>recover stop ids from orderbook"]
    G1 --> H["notify_startup_summary"]
    H --> I["create_task(tracker.start)"]
    I --> J["create_task(TimeExitScheduler.start)"]
    J --> K["service.start(runner): fails closed, engine continues"]
    K --> L["_serve_until_shutdown: listener task + shutdown event"]
    L -->|listener gave up| M["degraded mode: halt entries,<br/>tracker and time exit keep running"]
    L -->|SIGINT or SIGTERM| N["cancel runner, tracker, scheduler<br/>_close_resources: HTTP client, trades.db, runner store, risk.db"]
```

Restart attribution (`_lookup_entry_legs` / `_lookup_entry_trade`) matches today's `trades.db`
entry rows to the broker's netted quantity and side. Audit fix: the side comparison now reads the
`Direction` enum correctly (`startup._direction_name`); before it, no row ever matched a side.

## 3. Signal intake to dispatch

```mermaid
flowchart TD
    T["Telegram message"] --> P1{"listener: channel enabled<br/>and phase matches mode?"}
    P1 -->|no| D0["DECLINED row (channel_phase), CRITICAL log"]
    P1 -->|yes| HM["main.handle_message"]
    HM --> N1["normalize + parse"]
    N1 -->|None| Z["ignored (debug log)"]
    N1 --> S1{"single_source: strategy owned by runner?"}
    S1 -->|python source| Z2["dropped, counted"]
    S1 --> S2{"Pine tag superseded on symbol?"}
    S2 -->|entry| D1["DECLINED (superseded)"]
    S2 -->|exit, no tracked position| D1
    S2 --> DS["dispatch_signal"]
    RUN["pipeline runner Fire -> DispatchSink"] --> DS
    DS --> F1{"entry older than stale_signal_seconds?"}
    F1 -->|yes| D2["DECLINED (stale)"]
    F1 --> V{"validator.validate"}
    V -->|invalid, ignored, duplicate| D3["DECLINED (validator)"]
    V -->|EXIT| EXH["_handle_exit (section 5)"]
    V -->|LONG or SHORT| ENH["_handle_entry (section 4)"]
```

## 4. Entry path (`main._handle_entry`)

```mermaid
flowchart TD
    A["_handle_entry"] --> B{"mode_guard halted?"}
    B -->|yes| R1["DECLINED halted + alert"]
    B --> C{"symbol rules: BE series, broker MIS reject list"}
    C -->|reject| R2["DECLINED symbol_rules"]
    C --> D{"trade policy: entries per direction per day"}
    D -->|reject| R3["DECLINED trade_policy"]
    D --> E{"risk gates: exposure, symbol, sector"}
    E -->|reject| R4["DECLINED risk_gates"]
    E --> F{"unresolved order on this symbol?"}
    F -->|yes| R5["held"]
    F --> G["fetch_available_capital, min capital floor"]
    G --> H["risk_engine.calculate_quantity (fixed fractional, slippage factor)"]
    H -->|0| R6["DECLINED sizing"]
    H --> I{"ANALYZE?"}
    I -->|yes| I1["_sandbox_can_fund"]
    I -->|no| I2["adjust_qty_for_margin (Margin API)"]
    I2 -->|qty 0| R7["DECLINED margin (audit fix: row was missing)"]
    I1 --> J["build_order, send_order (MARKET)"]
    I2 --> J
    J -->|TIMEOUT| K["_recover_ambiguous_entry_timeout (order book reads)"]
    J -->|SUCCESS| L["_establish_position"]
    K --> L
    L --> L1["risk_engine.record_trade"]
    L1 --> L2["_place_entry_bracket: SL-M stop (MIS)"]
    L2 --> L3["_fetch_entry_fill"]
    L3 --> L4{"bad-fill guard closes it?"}
    L4 -->|closed| L5["save entry + guard exit rows"]
    L4 -->|kept| L6["notify_entry_filled, tracker.register"]
    L6 --> S["db.save entry row"]
    J -->|rejected or error| S
```

Persistence note: the entry row is written last, after network calls; see [gaps.md](gaps.md) G7.

## 5. Exit path (`main._handle_exit` -> `_handle_exit_locked`)

```mermaid
flowchart TD
    A["_handle_exit: per (symbol, strategy) asyncio lock"] --> B{"time exit active?"}
    B -->|yes| Z1["skip"]
    B --> C{"tracked position? exit already pending?"}
    C -->|none| C1["_recover_position_from_broker"]
    C1 -->|none| Z2["no position, return"]
    C --> D{"tp_level is SL?"}
    D -->|yes| D1["_reconcile_sl_hit (book the broker stop)"]
    D --> E["_resolve_exit_qty (partial or full)"]
    E --> F["_cancel_sl_before_exit (stop must go before any exit)"]
    F --> F1{"stop filled during cancel and broker flat?"}
    F1 -->|yes| D1
    F --> G["_place_exit_order: MARKET, retries, reconcile on timeout"]
    G -->|SUCCESS| H["_book_exit_result: partial replaces runner stop, full calls book_close"]
    H -->|full| H1["_sweep_protective_orders"]
    G -->|failure| I["_handle_exit_order_failure: restore protection, alert"]
```

## 6. Position tracking poll (`PositionTracker.check_positions`, every `tracking.poll_interval`)

```mermaid
flowchart TD
    A["poll (skipped while time exit active)"] --> B["sweep_unresolved_orders"]
    B --> C["fetch_positionbook"]
    C -->|None| C1["outage counter, alert after repeated failures"]
    C --> D["_push_unrealised: risk drawdown"]
    D --> E["_check_mode_flip: halt entries on flip"]
    E --> F["per position: _check_one_position"]
    F --> F1["qty 0 after guards: _book_broker_close, EXIT row, risk.record_close"]
    F --> F2["fill never confirmed: order status, release orphan"]
    E --> G["_retry_missing_sl"]
    G --> H["no-progress gates: break-even stop or market exit"]
```

## 7. Time exit (14:45 IST, `TimeExitScheduler` -> `time_exit_all`)

```mermaid
flowchart TD
    A["TimeExitScheduler loop (5 s)"] --> B{"time reached, not fired today?"}
    B --> C["_fire (audit fix: exception logged, TIME EXIT ERROR alert, loop survives)"]
    C --> D["time_exit_all: MIS only, CNC kept"]
    D --> E["_square_off_strategies: cancel_all_orders, close_all_positions"]
    E --> F["_verify_positions_closed: fetch_net_position, up to 3 attempts"]
    F -->|"non-zero incl. -1 (audit fix)"| F1["retry cancel + close; after 3: TIME EXIT FAILED alert"]
    F -->|"None, unreadable (audit fix)"| F2["treated as closed (unchanged policy) + TIME EXIT UNVERIFIED alert"]
    F --> G["_confirm_orders_cancelled: order book, cancel by id, CRITICAL alert if still working"]
    G --> H["_book_time_exit_trades: per-leg P&L, TIME rows, risk.record_close"]
    H --> I["send_day_summary, reset day counters"]
```

## 8. Python runner cycle (`pipeline/`)

```mermaid
flowchart TD
    A["service.start: preflight history, warm symbols"] -->|fail| X["runner not started (CRITICAL), engine continues"]
    A --> B["RollingContext.warm: HistorifyLoader reads sessions up to day-1 from db/historify.duckdb"]
    B --> V{"newest session == calendar previous session<br/>and complete? (audit fix)"}
    V -->|no| VX["symbol excluded for the day<br/>CRITICAL log + runner_warmup_rejected alert"]
    V -->|yes| C
    C["every 5-min bar close: cycle.run_cycle"]
    C --> D["today's bars from OpenAlgo history API"]
    D --> E["strategy.evaluate(prior + today) in thread pool"]
    E --> F{"Fire?"}
    F -->|python source| G["DispatchSink: main.dispatch_signal"]
    F -->|shadow| H["RecordingSink: runner_state.db shadow_events"]
    C --> I["ExitDriver and trail driver for open positions"]
```

Audit fix (G1): `HistorifyLoader.load` drops bars on non-session days, then rejects a symbol whose
newest session is not the NSE calendar's previous session, or whose previous session has fewer than
324 continuous 1m bars or none from 15:10 (special sessions exempt). If the calendar cannot answer,
every symbol is rejected. Before the fix, a store missing yesterday made the strategy read the
session before it (2026-10-14, PFC).

## 9. Signal and position state

```mermaid
stateDiagram-v2
    [*] --> Received
    Received --> Declined: guard, validator, risk, sizing, margin
    Received --> OrderSent: entry
    OrderSent --> Unresolved: timeout, outcome unknown
    Unresolved --> Open: fill found later
    Unresolved --> Declined: no fill
    OrderSent --> Rejected: broker rejects
    OrderSent --> Open: SUCCESS, stop placed, tracker registered
    Open --> PartiallyClosed: TP partial, runner stop replaced
    PartiallyClosed --> Open
    Open --> Closed: stop hit, TP or EXIT, no-progress, time exit
    PartiallyClosed --> Closed
    Open --> Orphaned: fill never confirmed
    Closed --> [*]
    Declined --> [*]
    Rejected --> [*]
    Orphaned --> [*]
```

## 10. Restart and recovery

| Situation | Behavior | Where |
| --- | --- | --- |
| Engine restart mid-session | Counters from `risk.db`; positions restored from broker book plus today's `trades.db` entries; stop ids from the order book; missing stops re-placed next poll | `startup.reconcile_open_positions`, `tracker._retry_missing_sl` |
| Telegram listener dies | Degraded mode: entries halted, management continues | `startup._enter_degraded_mode` |
| OpenAlgo mode flips | Entries halted for the session | `tracker._check_mode_flip`, `mode_guard` |
| Positionbook outage | Poll skipped, alert after repeated failures | `tracker._note_positionbook_failure` |
| Order timeout | Order book reconcile, then held as unresolved and swept each poll | `order_reconcile.py`, `main.sweep_unresolved_orders` |
