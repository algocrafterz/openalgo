# Signal Engine

Autonomous trading pipeline that listens to Telegram channels, parses structured signals, sizes positions, and executes orders via the OpenAlgo REST API. Runs alongside OpenAlgo as a separate process managed by `openalgoctl.sh`.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [End-to-End Pipeline](#end-to-end-pipeline)
3. [Module Reference](#module-reference)
4. [Signal Format](#signal-format)
5. [Position Sizing](#position-sizing)
6. [Risk Management](#risk-management)
7. [Bracket Orders & OCO](#bracket-orders--oco)
8. [Position Tracking](#position-tracking)
9. [Broker Auth & Auto-Login](#broker-auth--auto-login)
10. [Configuration Reference](#configuration-reference)
11. [Environment Variables](#environment-variables)
12. [Operating Modes](#operating-modes)
13. [Startup & Shutdown](#startup--shutdown)
14. [Testing](#testing)

---

## Architecture Overview

```
Telegram Channels
      │
      ▼
  listener.py          — async Telegram client (Telethon)
      │  raw text
      ▼
  normalizer.py        — strip emojis, alias keys, handle TP HIT / pipe formats
      │  canonical text
      ▼
  parser.py            — parse into Signal object
      │  Signal
      ▼
  validator.py         — SL/TP direction, R:R, duplicate, stale, blacklist, price filter
      │  valid Signal
      ▼
  main.py (_handle_entry / _handle_exit)
      │
      ├─ risk.py        — check_exposure(), calculate_quantity()
      ├─ api_client.py  — fetch_available_capital(), fetch_trading_mode(), fetch_margin()
      ├─ executor.py    — build_order(), send_order(), send_bracket_legs()
      ├─ tracker.py     — register position, poll SL fills, time exit
      ├─ notifier.py    — Telegram notifications
      └─ db.py          — persist trade to SQLite
```

All components are in `signal_engine/`. The entry point is `signal_engine/main.py` → `run()`.

---

## End-to-End Pipeline

### Entry Signal (`LONG` / `SHORT`)

```
1. listener     receives raw Telegram message
2. normalizer   cleans and canonicalizes text
3. parser       converts to Signal(strategy, direction, symbol, entry, sl, tp, ...)
4. validator    checks: SL direction, R:R ratio, duplicate window, staleness,
                        price filter, blacklist, min_sl_pct
5. main.py
   a. check exposure limits (open positions, trades today, loss limits, portfolio heat)
   b. check symbol/sector concentration
   c. fetch_available_capital() → live capital from OpenAlgo funds API
   d. get_sizing_capital() → day-start cached capital (if use_day_start_capital=true)
   e. calculate_quantity() → risk-based qty (fixed_fractional or pct_of_capital)
   f. adjust_qty_for_margin() → scale down if margin > live_capital [LIVE only]
   g. build_order() → Order object
   h. send_order() → POST /api/v1/placeorder → TradeResult
   i. send_bracket_legs() → SL-M order only (TP exit via signal, not broker)
   j. tracker.register() → track position for SL fill detection
   k. risk_engine.record_trade() → increment counters + portfolio heat
   l. notifier.notify_entry_placed() → Telegram
   m. db.save() → persist to SQLite
```

### Exit Signal (`EXIT` / TP HIT)

```
1. listener     receives TP HIT or EXIT signal
2. normalizer   converts "ORB TP1 HIT | SYMBOL" → canonical EXIT signal
3. main.py._handle_exit
   a. find tracked position for symbol+strategy
   b. determine exit_qty from strategy tp_levels config
      (e.g. TP1: 1.0 = full exit, TP1: 0.5 = partial)
   c. cancel existing SL order (MUST happen before exit order)
   d. build_exit_order() → SELL MARKET
   e. send_order() → POST /api/v1/placeorder
   f. fetch_realised_pnl() → calculate PnL delta
   g. full exit: tracker.unregister() + risk_engine.record_close()
      partial exit: reduce tracked qty, clear sl_order_id
   h. notifier.notify_exit_placed()
```

---

## Module Reference

| File | Responsibility |
|------|---------------|
| `main.py` | Pipeline orchestration, entry/exit routing, startup/shutdown |
| `listener.py` | Async Telegram channel listener (Telethon) |
| `normalizer.py` | Raw message preprocessing → canonical format |
| `parser.py` | Canonical text → `Signal` Pydantic model |
| `validator.py` | Signal validation rules (SL, R:R, duplicates, blacklist) |
| `risk.py` | Position sizing (`RiskEngine`), exposure limits |
| `risk_store.py` | SQLite persistence for risk counters (restart-safe) |
| `executor.py` | Order construction + OpenAlgo API calls |
| `tracker.py` | Position lifecycle: register, poll SL fills, time exit |
| `api_client.py` | All async OpenAlgo API calls (capital, margin, orders, etc.) |
| `notifier.py` | Telegram notification formatting and dispatch |
| `config.py` | Config loader with fail-fast validation (`Settings` dataclass) |
| `models.py` | Pydantic models: `Signal`, `Order`, `TradeResult`, `ValidationResult` |
| `strategies.py` | Strategy name constants |
| `db.py` | SQLite trade persistence |
| `logger_setup.py` | Loguru setup with daily rotation |

### Key Scripts (`signal_engine/scripts/`)

| File | Purpose |
|------|---------|
| `openalgoscheduler.py` | Startup orchestrator: auto-login, verify auth, start signal engine |
| `openalgoctl.sh` | Service controller: start/stop/restart/status, log rotation |
| `openalgoctl.ps1` | Windows wrapper: launches `openalgoctl.sh` in minimized cmd window |
| `createTaskOpenAlgoScheduler.ps1` | Windows Task Scheduler: 8:50 AM start, 3:30 PM stop (weekdays) |

---

## Signal Format

### Entry Signal

Sent to a Telegram channel the engine is watching:

```
STRATEGY DIRECTION
Symbol: SYMBOL
Entry: 250.50
SL: 246.00
TP: 262.00
Exchange: NSE        (optional — defaults to config broker.exchange)
Product: MIS         (optional — defaults to config broker.product)
Time: 09:20          (optional — informational only)
```

**Examples:**
```
ORB LONG
Symbol: WIPRO
Entry: 250.50
SL: 246.00
TP: 262.00
```

```
RSI-TP-MR SHORT
Symbol: INFY
Entry: 1520
SL: 1535
TP: 1498
```

### TP HIT / Exit Signal

```
ORB TP1 HIT | WIPRO
```
```
RSI-TP-MR TP1 HIT | INFY
```

The normalizer converts these into canonical `EXIT` signals automatically. The `TP1` level is matched against `strategy_profiles[strategy].tp_levels` to determine what fraction of the position to exit.

### Pipe Format (Alternative)

```
ORB LONG | WIPRO
Entry: 250.50
SL: 246.00
TP: 262.00
```

### Legacy Key Alias

`Target:` is aliased to `TP:` for backward compatibility.

---

## Position Sizing

### Mode: `fixed_fractional` (default)

```
qty = floor(capital × risk_per_trade / (|entry - sl| × (1 + slippage_factor)))
```

- Every trade risks the same INR amount (1% of capital by default)
- SL distance controls qty: tight SL → more shares, wide SL → fewer shares
- `slippage_factor: 0.10` widens the denominator by 10% (both entry and TP are MARKET orders)
- If `qty = 0` (stock too expensive for risk budget), trade is skipped

### Mode: `pct_of_capital`

```
qty = floor(capital × pct_of_capital / entry_price)
```

### Day-Start Capital Caching (`use_day_start_capital: true`)

The first capital fetch of each trading day is cached. All subsequent trades use this cached value, ensuring equal risk per trade regardless of intraday P&L swings.

### Margin Adjustment (Live Mode Only)

After risk-based sizing, the engine calls `/api/v1/margin` to get the actual broker margin required:

```
if actual_margin > live_capital:
    qty = floor(raw_qty × live_capital / actual_margin)
```

This prevents oversized orders when margin requirements exceed available capital (e.g., second concurrent MIS slot). Skipped in analyze mode — broker margin API is not available for sandbox.

### Example at ₹15,000 Capital

`risk_per_trade: 1%` → ₹150 risk per trade. MIS margin ~20%:

| Symbol | Entry | SL | SL dist | Qty | Notional | MIS Margin |
|--------|-------|----|---------|-----|----------|-----------|
| WIPRO | ₹250 | ₹246 | ₹4.00 | 37 | ₹9,250 | ~₹1,850 |
| INFY | ₹1,500 | ₹1,480 | ₹20.00 | 7 | ₹10,500 | ~₹2,100 |
| SBIN | ₹800 | ₹793 | ₹7.00 | 21 | ₹16,800 | ~₹3,360 |

At `max_open_positions: 2` with ₹15K capital: 2 concurrent MIS slots comfortably fit. Up to 8 total trades/day as slots recycle on close.

---

## Risk Management

All limits are enforced in `risk.py` before each order. Counters persist across restarts via `risk_store.py` (SQLite, keyed by `(mode, date)`).

| Limit | Config Key | Description |
|-------|-----------|-------------|
| Daily loss limit | `risk.daily_loss_limit` | Block new trades when realized + unrealized loss ≥ threshold |
| Weekly loss limit | `risk.weekly_loss_limit` | Accumulated over Mon–Sun |
| Monthly loss limit | `risk.monthly_loss_limit` | Accumulated over calendar month |
| Max open positions | `risk.max_open_positions` | Concurrent position slots (currently 2) |
| Max trades/day | `risk.max_trades_per_day` | Total order count cap (slots recycle) |
| Portfolio heat | `risk.max_portfolio_heat` | Sum of open risk ≤ threshold; blocks new trades |
| Price filter | `sizing.min/max_entry_price` | Skip stocks outside ₹150–₹800 range |
| Min SL % | `risk.min_sl_pct` | Reject SL tighter than 0.5% of entry |
| Min R:R | `risk.min_rr` | Skip signals with reward < 0.5× risk |
| Symbol concentration | `risk.max_positions_per_symbol` | Max 1 position per symbol |
| Duplicate window | `risk.duplicate_window_seconds` | Ignore identical signals within 60s |
| Stale signal | `risk.stale_signal_seconds` | Reject signals older than 60s |
| Blacklist | `blacklist.*` | Per-strategy and `_global` symbol exclusion |

---

## Bracket Orders & OCO

### Why SL-Only (Not SL+TP as Broker Orders)

Indian brokers treat any SELL while a SELL SL is active as a new SHORT position requiring full MIS margin → `FUND LIMIT INSUFFICIENT`. Placing both SL and TP simultaneously always results in one being rejected.

**Solution**: Place only the SL-M order at the broker. TP exit is driven by TradingView TP HIT signals via the `_handle_exit` pipeline.

### SL Order Flow

```
Entry filled → send_bracket_legs()
  → build_sl_order(): SELL SL-M, trigger_price = round_to_tick(sl, "down")
  → send_order() with up to bracket.max_sl_retries retries
  → sl_order_id stored in TrackedPosition
```

### SL Cancel Before Exit (Critical)

Before ANY exit order (full or partial), the SL must be cancelled:

```python
if pos.sl_order_id:
    await cancel_order(pos.sl_order_id, ...)
    pos.sl_order_id = ""
```

Skipping this causes the broker to interpret the exit SELL as a new SHORT.

### After Partial Exit

After a partial TP exit, `sl_order_id` is cleared and no new SL is placed. The remaining position relies on:
1. Next TP HIT signal for the remaining quantity
2. Safety `EXIT` signal if needed
3. Time exit at 15:00 IST

### CNC Positions

`bracket.cnc_sl_enabled: false` — NSE cancels CNC SL-M orders at EOD. CNC exits rely entirely on TradingView signals (no broker SL protection).

---

## Position Tracking

`tracker.py` polls open positions every `tracking.poll_interval` seconds (default: 10s).

### SL Fill Detection

```
poll cycle → fetch_positionbook() → for each TrackedPosition:
  if position qty == 0 in broker book → SL was triggered
    → record_close(pnl) + unregister + notify
```

### Time Exit (15:00 IST)

`TimeExitScheduler` fires at `time_exit.hour:minute` IST:

```
close_all_positions(strategy) → POST /api/v1/closeposition
cancel_all_orders(strategy)   → POST /api/v1/cancelallorder
```

All tracked positions are unregistered and risk counters updated.

---

## Broker Auth & Auto-Login

Managed by `scripts/openalgoscheduler.py`. Broker is auto-detected from `REDIRECT_URL` (e.g. `/flattrade/callback` → `flattrade`). `BROKER_NAME` env var overrides if set.

### TOTP Brokers (programmatic, unattended)

Brokers with `authenticate_with_totp` in their `auth_api.py` module:

| Broker | Auth method |
|--------|------------|
| **flattrade** | Headless OAuth: `POST /auth/session` → `POST /ftauth` → `POST /trade/apitoken` |
| **mstock** | Direct TOTP API |

**Flattrade flow in detail:**
```
1. POST https://authapi.flattrade.in/auth/session
   Headers: Origin/Referer = auth.flattrade.in
   → Response: SID (hex string)

2. POST https://authapi.flattrade.in/ftauth
   Body: {UserName, Password: SHA256(pwd), PAN_DOB: TOTP, APIKey, Sid}
   → Response: {emsg: "", RedirectURL: "...?code=XXXX"}

3. POST https://authapi.flattrade.in/trade/apitoken
   Body: {api_key, request_code: code, api_secret: SHA256(api_key+code+secret)}
   → Response: {stat: "Ok", token: "..."}
```

### OAuth Brokers (manual first-time login)

Zerodha and similar — no `authenticate_with_totp`. Scheduler reuses the stored DB token. Token must be refreshed daily via the OpenAlgo web UI.

### Startup Auth Flow

```
openalgoctl.sh startup
  └─ openalgoscheduler.py
       1. get_broker_name()           → from REDIRECT_URL or BROKER_NAME
       2. import broker.{name}.api.auth_api
       3a. has authenticate_with_totp?
           YES → generate TOTP → call authenticate_with_totp(password, totp)
                → upsert token in DB
           NO  → retrieve existing DB token
                → verify broker name matches
       4. verify_broker_auth(token)   → call broker funds API
       5. PASS → start signal engine
          FAIL → log error → sys.exit(1) → Telegram notification
```

---

## Configuration Reference

All settings in `signal_engine/config.yaml`. Fail-fast on any missing key.

### `telegram`
| Key | Description |
|-----|-------------|
| `channels` | List of `{name, id}` — Telegram channels to monitor |
| `notify_channel` | `{name, id}` — Separate channel for startup/shutdown/error notifications |

### `sizing`
| Key | Default | Description |
|-----|---------|-------------|
| `mode` | `fixed_fractional` | `fixed_fractional` or `pct_of_capital` |
| `risk_per_trade` | `0.01` | Fraction of capital to risk per trade (fixed_fractional) |
| `pct_of_capital` | `0.05` | Fraction of capital per position (pct_of_capital) |
| `min_entry_price` | `150` | Skip stocks below this price |
| `max_entry_price` | `800` | Skip stocks above this price |
| `slippage_factor` | `0.10` | Widens SL distance before sizing (accounts for MARKET entry + exit slippage) |
| `sandbox_capital` | `15000` | Capital to use in analyze mode — set to match live capital |
| `use_day_start_capital` | `true` | Cache first capital fetch of day for equal risk per trade |

### `risk`
| Key | Default | Description |
|-----|---------|-------------|
| `daily_loss_limit` | `0.04` | 4% daily loss lockout |
| `weekly_loss_limit` | `0.08` | 8% weekly loss lockout |
| `monthly_loss_limit` | `0.10` | 10% monthly loss lockout |
| `max_portfolio_heat` | `0.03` | 3% max open risk across all positions |
| `max_open_positions` | `5` | Concurrent position slots |
| `max_trades_per_day` | `8` | Total daily trade cap |
| `min_rr` | `0.5` | Minimum reward:risk ratio |
| `duplicate_window_seconds` | `60` | Deduplicate identical signals |
| `stale_signal_seconds` | `60` | Reject old signals |
| `min_sl_pct` | `0.005` | Reject SL < 0.5% of entry |
| `max_positions_per_symbol` | `1` | Max concurrent in same symbol |
| `max_positions_per_sector` | `0` | `0` = disabled |

### `blacklist`
```yaml
blacklist:
  _global:          # applies to all strategies
    - YESBANK
  ORB:              # strategy-specific
    - BHEL
```

### `time_exit`
| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Force-close all positions at configured time |
| `hour` | `15` | IST hour (24h) |
| `minute` | `0` | IST minute — 15:00 gives 10-min buffer before broker auto square-off |

### `tracking`
| Key | Default | Description |
|-----|---------|-------------|
| `poll_interval` | `10` | Seconds between position polls (SL fill detection) |

### `broker`
| Key | Default | Description |
|-----|---------|-------------|
| `exchange` | `NSE` | Default exchange |
| `product` | `MIS` | `MIS` or `CNC` |
| `order_type` | `MARKET` | `MARKET` or `LIMIT` |
| `allow_off_hours_testing` | `false` | When `true`: place orders as CNC in analyze mode outside market hours (pipeline testing only — never enable in live) |

### `api`
| Key | Default | Description |
|-----|---------|-------------|
| `timeout` | `5.0` | HTTP request timeout in seconds |
| `margin_retries` | `3` | Retry attempts for Margin API before skipping trade |

### `bracket`
| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Place SL-M order after entry fill |
| `cnc_sl_enabled` | `false` | CNC SL-M disabled (NSE cancels overnight) |
| `sl_order_type` | `SL-M` | `SL` or `SL-M` |
| `max_sl_retries` | `5` | Retry attempts for SL placement |
| `retry_delay` | `0.5` | Seconds between SL retries |
| `tp_exit_retries` | `3` | Retry attempts for TP MARKET exit |

### `strategy_profiles`
Per-strategy TP levels and product type. Strategies not listed exit 100% at any TP HIT.
```yaml
strategy_profiles:
  ORB:
    product: MIS
    tp_levels:
      TP1: 1.0          # Exit 100% at TP1
  RSI-TP-MR:
    product: MIS
    tp_levels:
      TP1: 1.0
```

---

## Environment Variables

Secrets go in `signal_engine/.env` (never in `config.yaml`).

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_API_ID` | Yes | Telegram API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | Yes | Telegram API hash from my.telegram.org |
| `TELEGRAM_PHONE` | Yes | Phone number for Telegram session |
| `OPENALGO_API_KEY` | Yes | OpenAlgo API key (`/apikey` page) |
| `OPENALGO_BASE_URL` | Yes | OpenAlgo server URL (e.g. `http://127.0.0.1:5000`) |
| `BROKER_API_KEY` | Yes | `CLIENT_ID:::API_KEY` (flattrade) or client code (mstock) |
| `BROKER_API_SECRET` | Yes | Broker API secret |
| `BROKER_PASSWORD` | Yes | Broker login password (TOTP brokers) |
| `BROKER_TOTP_SECRET` | Yes | Base32 TOTP seed from authenticator app |
| `REDIRECT_URL` | Yes | OpenAlgo OAuth URL — broker name auto-detected from path |
| `BROKER_NAME` | No | Override broker auto-detection |

---

## Operating Modes

### Live Mode

- Capital: fetched from broker funds API via `/api/v1/funds`
- Orders: routed to real broker
- Margin: validated via `/api/v1/margin` before each order
- SL: placed as SL-M broker order

### Analyze Mode (Sandbox)

- Capital: `sandbox_capital` from config (override) or OpenAlgo sandbox funds
- Orders: routed to OpenAlgo sandbox (virtual ₹1Cr capital by default)
- Margin: **skipped** — broker margin API unavailable for sandbox
- SL: placed in sandbox (no real broker order)
- `allow_off_hours_testing: true` → product overridden MIS→CNC to bypass sandbox after-hours restriction

**Important**: Set `sandbox_capital` to match live capital for realistic position sizing during testing.

---

## Startup & Shutdown

### Start

```bash
# Full startup (OpenAlgo + auto-login + signal engine)
./signal_engine/scripts/openalgoctl.sh run

# Windows
.\signal_engine\scripts\openalgoctl.ps1 start
```

### Stop

```bash
./signal_engine/scripts/openalgoctl.sh stop
```

### Windows Task Scheduler

`createTaskOpenAlgoScheduler.ps1` creates two scheduled tasks:
- **Start**: 8:50 AM weekdays → `openalgoctl.ps1 start`
- **Stop**: 3:30 PM weekdays → `openalgoctl.ps1 stop`

### Startup Sequence

```
openalgoctl.sh run
  1. Start OpenAlgo (app.py via uv) — wait for HTTP readiness
  2. openalgoscheduler.py startup
     a. Auto-login to broker (TOTP or OAuth reuse)
     b. Verify auth token via funds API
     c. Store token in OpenAlgo DB
  3. uv run python -m signal_engine.main
     a. Detect trading mode (live/analyze)
     b. Fetch startup capital
     c. Restore risk counters from DB
     d. Start TimeExitScheduler
     e. Start PositionTracker (background)
     f. Send Telegram startup notification
     g. Start Telegram listener (blocking)
```

### Graceful Shutdown

`openalgoctl.sh` traps `SIGINT`/`SIGTERM`/`EXIT`:
1. Sends Telegram shutdown notification (while OpenAlgo still running)
2. Kills signal engine process
3. Kills OpenAlgo process

---

## Testing

```bash
# All signal engine tests
PYTHONPATH=. uv run pytest signal_engine/tests/ -v

# Specific test file
PYTHONPATH=. uv run pytest signal_engine/tests/test_risk.py -v

# With coverage
PYTHONPATH=. uv run pytest signal_engine/tests/ --cov=signal_engine --cov-report=term-missing
```

**384 tests, ~99% coverage** on risk module.

| Test File | Coverage |
|-----------|---------|
| `test_risk.py` | RiskEngine sizing, exposure limits, portfolio heat |
| `test_risk_store.py` | SQLite persistence, restart recovery |
| `test_validator.py` | All validation rules (SL, R:R, duplicates, blacklist) |
| `test_parser.py` | Signal parsing (valid/invalid formats) |
| `test_normalizer.py` | Emoji strip, TP HIT, pipe format, legacy keys |
| `test_executor.py` | Order building, API responses, timeout handling |
| `test_tracker.py` | Position lifecycle, SL fill detection, time exit |
| `test_main.py` | Pipeline integration (entry/exit flow) |
| `test_api_client.py` | OpenAlgo API calls (capital, margin, orders) |
| `test_config.py` | Config loading, fail-fast validation |
| `test_db.py` | Trade persistence |
| `test_openalgoscheduler.py` | Broker detection, auto-login, broker switching |
| `test_telegram_integration.py` | Live Telegram connection (requires session, marked integration) |
