# PRD: Automated Trade Execution from TradingView Telegram Signals

> **Document Purpose:** Implementation reference and status tracker.
> **Reading convention:** `[IMPL]` = direct implementation directive. `[CONTEXT]` = background only. `[CONSTRAINT]` = hard rules.
> **Status:** MVP + Phase 2-5 completed. See Section 19 for status.

---

## 1. System Identity

**System Name:** `signal_engine`
**Runtime:** Python 3.12+
**Deployment:** Local machine (single user, retail trader)
**Broker Bridge:** OpenAlgo (local HTTP API) -- supports both live broker and sandbox (analyze) mode
**Package Manager:** `uv` (managed via root `pyproject.toml`)

---

## 2. Pipeline Architecture

```
TradingView PineScript Strategy
        |  (webhook POST)
Telegram Channel(s)  [one or more channels configured by name + id]
        |
TelegramListener     [listener.py]  -- stale guard, multi-channel, exponential backoff
        |
Normalizer           [normalizer.py] -- Unicode normalization, whitespace cleanup
        |
SignalParser         [parser.py]   -- raw text -> Signal object
        |
SignalValidator      [validator.py] -- entry/SL/target/direction/R:R/duplicate/min_sl_pct
        |
RiskEngine           [risk.py]
   |-- can_trade_symbol()   -> symbol concentration check
   |-- can_trade_sector()   -> sector correlation check
   |-- check_exposure()     -> daily/weekly/monthly loss + portfolio heat + capital utilization + open positions + daily trades
   |-- fetch_available_capital() -> live capital from OpenAlgo funds API
   |-- calculate_quantity() -> risk-based sizing + margin cap + budget cap
        |
ExecutionLayer       [executor.py]
   |-- send_order()         -> ENTRY order (MARKET or LIMIT)
   |-- send_bracket_legs()  -> SL-M order + TP LIMIT order (OCO pair)
        |
OpenAlgo HTTP API    [local instance, auto-routes live/sandbox]
        |
Broker (live or sandbox)

[Background]:
PositionTracker      [tracker.py]
   |-- polls /api/v1/openposition every poll_interval seconds
   |-- detects SL/TP closure, cancels remaining leg (OCO)
   |-- computes P&L delta, updates risk engine loss counters
   |-- releases committed margin on close

RiskStore            [risk_store.py]
   |-- SQLite persistence for risk counters keyed by (mode, date)
   |-- survives restarts, isolates live/sandbox counters
```

---

## 3. Technology Stack

[CONSTRAINT] Do not add dependencies beyond those listed.

| Purpose | Library | Notes |
|---------|---------|-------|
| Telegram client | `telethon` | MTProto, async |
| Data models | `pydantic` v2 | Strict validation |
| HTTP client | `httpx` | Async, for OpenAlgo API calls |
| Logging | `loguru` | Structured, file + console |
| Config (secrets) | `python-dotenv` | Load `.env` file (secrets only) |
| Config (settings) | `pyyaml` | Load `config.yaml` (user-tunable) |
| Persistence | `sqlite3` | stdlib, audit trail + risk counter store |
| Testing | `pytest` | Unit + integration tests |
| Testing (async) | `pytest-asyncio` | Async test support |

---

## 4. Project Structure

[IMPL] Fully isolated from OpenAlgo core -- zero modifications to existing files.

```
signal_engine/
    __init__.py              # Package init, version string
    main.py                  # Entry point, pipeline orchestration, graceful exit
    config.py                # Loads config.yaml + sectors.yaml + .env, Settings singleton
    config.yaml              # User-configurable settings (non-sensitive)
    sectors.yaml             # Sector-to-symbol mapping for correlation risk (separate file)
    listener.py              # Telegram client, multi-channel, stale guard
    normalizer.py            # Unicode normalization before parsing
    parser.py                # Parses raw message -> Signal object
    validator.py             # Validates Signal (entry/SL/TP/R:R/duplicate/min_sl_pct)
    risk.py                  # Position sizing, exposure checks, heat tracking, margin tracking
    risk_store.py            # SQLite persistence for risk counters (restart-safe)
    executor.py              # Builds + sends entry order, SL order, TP order
    api_client.py            # Async wrapper for OpenAlgo REST API (orders, funds, positions)
    tracker.py               # Position polling, OCO cancellation, P&L tracking, margin release
    models.py                # Pydantic models: Signal, Order, TradeResult, BracketLeg, etc.
    db.py                    # SQLite audit trail
    logger_setup.py          # Loguru configuration
    .env                     # Secrets (gitignored)
    .env.example             # Template for secrets (no values)
    PRD.md                   # This document
    scripts/
        __init__.py
        openalgoscheduler.py     # Startup/shutdown automation + Telegram notify
        openalgoctl.sh           # Unified service controller (start/run/stop/restart/status)
        openalgoctl.ps1          # Windows wrapper: boots WSL, calls openalgoctl.sh run
        createTaskOpenAlgoScheduler.ps1  # One-time Task Scheduler setup (start + stop)
    data/                    # Runtime data (gitignored)
        trades.db            # SQLite audit database
        telegram.session     # Telegram auth session
    logs/                    # All logs (gitignored)
        openalgoctl.log
        signal_engine_*.log
    tests/
        __init__.py
        conftest.py          # Shared fixtures (make_signal helper)
        test_parser.py
        test_validator.py
        test_risk.py
        test_risk_store.py
        test_executor.py
        test_api_client.py
        test_tracker.py
        test_db.py
        test_main.py
        test_config.py
        test_telegram_integration.py  # integration tests (require live session)
```

---

## 5. Configuration

### 5.1 Split Configuration Architecture

[IMPL] Configuration is split across three files:

- **`.env`** -- Secrets only (API keys, tokens, phone numbers). Gitignored.
- **`config.yaml`** -- All user-tunable settings (non-sensitive). Checked into git.
- **`sectors.yaml`** -- Sector-to-symbol mapping for correlation risk. Checked into git.

No hardcoded values in code files. All tunable values come from config. Missing required keys raise `ConfigError` at startup (fail-fast).

### 5.2 `.env` File Schema (Secrets Only)

```env
# Telegram (https://my.telegram.org/apps)
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=

# OpenAlgo
OPENALGO_BASE_URL=http://127.0.0.1:5000
OPENALGO_API_KEY=

# Auto-login (openalgoscheduler.py)
BROKER_NAME=mstock          # broker to auto-login (default: mstock)
BROKER_PASSWORD=            # broker login password
BROKER_TOTP_SECRET=         # base32 TOTP seed from authenticator setup
```

### 5.3 `config.yaml` Schema

```yaml
telegram:
  channels:
    - name: "orb_channel"
      id: -1003518225740

  # Optional: dedicated channel for system notifications (startup/shutdown)
  # If omitted, notifications go to all signal channels above.
  notify_channel:
    name: "admin_notify"
    id: -100XXXXXXXXXX

sizing:
  mode: fixed_fractional         # fixed_fractional | pct_of_capital
  risk_per_trade: 0.01           # 1% risk per trade (fixed_fractional)
  pct_of_capital: 0.05           # 5% of capital per trade (pct_of_capital)
  max_position_size: 0           # 0 = disabled (SL distance controls size)
  min_entry_price: 0             # Min stock price to trade (0 = no filter)
  max_entry_price: 0             # Max stock price to trade (0 = no filter)
  slippage_factor: 0.05          # Widen risk_per_share by 5% to account for MARKET order slippage
  sandbox_capital: 100000        # Override capital in analyze mode (0 = use OpenAlgo)
  margin_multiplier:
    MIS: 0.20                    # 5x intraday leverage (20% margin required)
    NRML: 0.25                   # 4x F&O leverage
    CNC: 1.0                     # No leverage for delivery
  max_capital_utilization: 0.80  # Never commit more than 80% of capital

risk:
  daily_loss_limit: 0.04         # 4% of capital (allows 4 losers at 1% before lockout)
  weekly_loss_limit: 0.08        # 8% of capital (allows 2 bad days per week)
  monthly_loss_limit: 0.10
  max_portfolio_heat: 0.05       # 5% of capital max open risk (aligned: 5 positions x 1%)
  max_open_positions: 5
  max_trades_per_day: 8
  min_rr: 1.0                    # Minimum reward:risk ratio
  duplicate_window_seconds: 60
  stale_signal_seconds: 60
  min_sl_pct: 0.005              # Reject SL tighter than 0.5% of entry (0 = disabled)
  max_positions_per_symbol: 1    # Max concurrent positions in same symbol
  max_positions_per_sector: 3    # Max concurrent positions in same sector (forces diversification)

tracking:
  poll_interval: 30

broker:
  exchange: NSE
  product: MIS
  order_type: MARKET

listener:
  max_retries: 5
  base_backoff: 2

api:
  timeout: 5.0

bracket:
  enabled: true
  sl_order_type: SL-M            # SL or SL-M for stop-loss leg
  max_sl_retries: 3
  cancel_retry_count: 2
```

### 5.4 `sectors.yaml` Schema

Separate file mapping sector names to lists of NSE symbols. Based on NSE sectoral index constituents. Optional — if file is missing, sector limits are not enforced.

```yaml
BANKING:
  - HDFCBANK
  - ICICIBANK
  # ... 20+ stocks
IT:
  - TCS
  - INFY
  # ... 8+ stocks
# ... 15+ sectors total
```

### 5.5 `config.py`

[IMPL] Uses frozen dataclass (not pydantic-settings). Exposes singleton `settings` imported by all modules. Sectors loaded from `sectors.yaml` by `_load_sectors()`. Missing `sectors.yaml` returns empty dict (no sector limits enforced). All required keys use `_require_key()` — `ConfigError` raised at startup if anything is missing.

**`notify_channel`:** Optional `TelegramChannel` for system notifications (startup, shutdown, errors). Parsed from `telegram.notify_channel` in config.yaml. If omitted (`None`), notifications fall back to all signal channels. Keeps operational alerts separate from trading signal channels.

---

## 6. Data Models (`models.py`)

[IMPL] Pydantic v2 models with enum-based status fields:

### Enums
```python
class Direction(str, Enum):        LONG, SHORT
class Action(str, Enum):           BUY, SELL
class ValidationStatus(str, Enum): VALID, INVALID, IGNORED
class OrderStatus(str, Enum):      SUCCESS, TIMEOUT, REJECTED, ERROR
```

### `Signal`
```python
class Signal(BaseModel):
    strategy: str
    direction: Direction
    symbol: str
    entry: float
    sl: float
    tp: float
    product: Optional[str]       # override from signal (else uses config default)
    time: Optional[str]
    raw_message: str
    received_at: datetime        # UTC, auto-set
```

### `Order`
```python
class Order(BaseModel):
    symbol: str
    exchange: str
    action: Action
    quantity: int
    price: float                 # 0 for MARKET
    trigger_price: float         # 0 unless SL order
    order_type: str              # MARKET, LIMIT, SL-M
    product: str
    strategy_tag: str
```

### `TradeResult`
```python
class TradeResult(BaseModel):
    order_id: str
    status: OrderStatus
    message: str
    timestamp: datetime
```

---

## 7. Signal Format

[CONTEXT] TradingView sends webhook to Telegram. Signals arrive as text messages.

### Canonical Signal Format
```
ORB LONG
Symbol: RELIANCE
Entry: 2485
SL: 2470
Target: 2510
Time: 09:35
```

### Parsing Rules (`parser.py`)
[IMPL] Parser:
- Extracts `strategy` and `direction` from the first line (`"ORB LONG"`)
- Parses `Symbol`, `Entry`, `SL`, `Target`/`TP`, `Time`, `Product` as key-value pairs (case-insensitive)
- Strips whitespace from all values
- Generic -- works for any strategy name
- Mandatory fields: `Symbol`, `Entry`, `SL`, `Target`/`TP`
- Optional: `Time`, `Product`
- Returns `None` if mandatory fields missing (never raises)

---

## 8. Signal Validation (`validator.py`)

[IMPL] Validate in order. Return `ValidationResult` for each check.

| Check | Rule | Result if Fails |
|-------|------|-----------------|
| Entry validity | `entry > 0` | `INVALID` |
| SL present | `sl > 0` | `INVALID` |
| Target present | `tp > 0` | `INVALID` |
| SL direction | LONG: `sl < entry`; SHORT: `sl > entry` | `INVALID` |
| Target direction | LONG: `tp > entry`; SHORT: `tp < entry` | `INVALID` |
| R:R ratio | `(tp - entry) / (entry - sl) >= min_rr` (1.0 = 1:1) | `IGNORED` |
| Min SL % | `abs(entry - sl) / entry >= min_sl_pct` (0.5% default, guards against noisy tight SLs) | `IGNORED` |
| Duplicate | Same symbol + direction + entry within `duplicate_window_seconds` | `IGNORED` |

---

## 9. Risk Engine (`risk.py`)

### 9.1 Position Sizing

[IMPL] Two sizing modes:

**Fixed Fractional** (default):
```
risk_amount    = capital * risk_per_trade
risk_per_share = abs(entry - sl) * (1 + slippage_factor)
risk_qty       = floor(risk_amount / risk_per_share)
```

**Percent of Capital**:
```
allocation = capital * pct_of_capital
risk_qty   = floor(allocation / entry)
```

**Common caps applied after risk sizing:**
```
# 1. Margin affordability cap (prevents exceeding available capital)
margin_rate  = margin_multiplier[product]    # e.g. 0.20 for MIS
max_qty      = floor((capital * max_capital_utilization - committed_margin) / (entry * margin_rate))
quantity     = min(risk_qty, max_qty)

# 2. Max position size cap (if enabled, i.e. > 0)
if max_position_size > 0:
    cap_qty  = floor(capital * max_position_size / entry)
    quantity = min(quantity, cap_qty)

# If quantity <= 0: return 0 (skip trade, never force qty=1)
```

[CONSTRAINT] Capital **always** fetched live from OpenAlgo funds API. Never hardcoded.
[CONSTRAINT] If `calculate_quantity()` returns 0, trade is **skipped** entirely — never override with 1.

### 9.2 Exposure Checks (`check_exposure()`)

[IMPL] Called in this order in `main.py`. If any fails, skip the trade:

1. `can_trade_symbol(symbol)` -- symbol concentration (`max_positions_per_symbol`)
2. `can_trade_sector(symbol)` -- sector correlation (`max_positions_per_sector` via `sectors.yaml`)
3. `check_exposure()`:
   - Daily loss: `(daily_realised_loss + unrealised_drawdown) < capital * daily_loss_limit`
   - Weekly loss: `weekly_realised_loss < capital * weekly_loss_limit`
   - Monthly loss: `monthly_realised_loss < capital * monthly_loss_limit`
   - Portfolio heat: `current_heat < capital * max_portfolio_heat`
   - Capital utilization: `committed_margin < capital * max_capital_utilization`
   - Open positions: `open_positions < max_open_positions`
   - Daily trades: `trades_today < max_trades_per_day`

### 9.3 Loss & Risk Tracking

**Portfolio Heat:**
- `add_heat(qty, entry, sl)` on new entry: adds `qty * abs(entry - sl)` to `_heat`
- `remove_heat(qty, entry, sl)` on close: subtracts from `_heat`
- Blocks new trades when `_heat >= capital * max_portfolio_heat`

**Unrealised Drawdown:**
- `update_unrealised(unrealised_pnl)` called each tracker cycle
- Negative unrealised PnL combined with realised loss for daily limit check

**Committed Margin:**
- `add_margin(qty, entry, product)` on new entry: adds `qty * entry * margin_rate`
- `remove_margin(qty, entry, product)` on close
- Blocks new trades when utilization >= `max_capital_utilization`

**Persistent Counters (`risk_store.py`):**
- `RiskStore` uses SQLite keyed by `(mode, trade_date)`
- Counters survive process restarts (restored by `_restore()` on init)
- `live` and `sandbox` modes stored independently
- Daily counters auto-reset (new date key)
- `weekly_loss()` / `monthly_loss()` aggregate across calendar period

**Record methods:**
- `record_trade(symbol)` -- increments `trades_today`, `open_positions`, symbol counter
- `record_close(pnl, symbol)` -- decrements `open_positions`, symbol counter; adds loss to counters if `pnl < 0`

---

## 10. OpenAlgo API Integration

### 10.1 API Client (`api_client.py`)

[IMPL] Async wrapper for all OpenAlgo REST API calls:

| Function | Endpoint | Returns |
|----------|----------|---------|
| `fetch_trading_mode()` | `/api/v1/analyzer` | `(mode_str, is_analyze: bool)` |
| `fetch_available_capital()` | `/api/v1/funds` | `float` (availablecash) |
| `fetch_positionbook()` | `/api/v1/positionbook` | `list[dict]` or `None` on error |
| `fetch_open_position(...)` | `/api/v1/openposition` | `int` (legacy, kept for compatibility) |
| `fetch_realised_pnl()` | `/api/v1/funds` | `float` (m2mrealized) |
| `cancel_order(order_id, strategy)` | `/api/v1/cancelorder` | `bool` |
| `fetch_order_status(order_id, strategy)` | `/api/v1/orderstatus` | `str` (status string) |

All functions return safe defaults on failure (0.0, None, False). Never raise exceptions.

### 10.2 Order Execution (`executor.py`)

**Entry Order (`build_order`, `send_order`):**
- LONG -> BUY, SHORT -> SELL
- price=0 for MARKET, price=entry for LIMIT
- Includes `trigger_price` in payload when > 0

**Bracket Orders (`build_sl_order`, `build_tp_order`, `send_bracket_legs`):**
```
SL order:  action=opposite of entry, order_type=SL-M, trigger_price=sl (rounded to tick)
TP order:  action=opposite of entry, order_type=LIMIT, price=tp (rounded to tick)
```
Both sent after successful entry. Retries up to `max_sl_retries` on failure.
Prices are rounded to valid NSE tick size (0.05) to prevent broker rejection of LIMIT orders.

**Null order ID handling:** `send_order()` checks for null `orderid` in API response — brokers can return HTTP 200 with `status=false` and `orderid=null` when an order is rejected at broker level.

**OCO (One-Cancels-Other):** When either SL or TP triggers (position closed), `tracker.py` cancels the other leg via `cancel_order()`.

**Design Decision (2026-03-12):** Signal engine is a dumb executor — it does not compute or override TP/SL levels. All trade logic (TP level selection, R:R calculation) stays in the PineScript strategy. Analysis of 196 trades showed TP1 (1R) at +69R significantly outperforms TP1.5 at -60R. Trailing SL to breakeven adds no value with TP1 strategy since the LIMIT order closes the trade completely. See `pinescripts/intraday/orb/SIGNAL-PERFORMANCE-2026-Q1.md` for full simulation.

---

## 11. Position Tracker (`tracker.py`)

[IMPL] Background polling loop.

### `TrackedPosition`
```python
symbol, strategy, exchange, product, entry_price, quantity, sl, tp,
entry_order_id, sl_order_id, tp_order_id
```

### `PositionTracker`
- `register(position)` -- add to tracking dict (key: `symbol:strategy`)
- `check_positions()` -- **single `fetch_positionbook()` call** per cycle (not N individual calls):
  - Builds symbol -> qty lookup from positionbook response
  - For each tracked position, check qty in lookup (absent or 0 = closed):
    - Detect which bracket leg triggered (SL vs TP) and cancel the other via `cancel_order()`
    - Compute P&L delta from `fetch_realised_pnl()`
    - Call `risk_engine.record_close(pnl_delta, symbol)`, `remove_heat()`, `remove_margin()`
    - Remove from tracking
  - If positionbook returns `None` (API error): skip entire cycle
  - If qty > 0: still open, skip
- `start()` / `stop()` -- asyncio background task lifecycle

---

## 12. Telegram Listener (`listener.py`)

[IMPL] Uses `telethon` async client:

- Connects with `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`
- Monitors **multiple channels** from `config.yaml` (numeric ID or `@username`)
- `NewMessage` handler per channel:
  - Stale guard: skip messages older than `stale_signal_seconds`
  - Logs full signal as single line: `" | ".join(non-empty lines)` -- no truncation
  - Passes `message.text` to pipeline callback
- Auto-reconnect: exponential backoff (`base_backoff * 2^(retry-1)`) up to `max_retries`
- Session persisted at `signal_engine/data/telegram.session`

---

## 13. Execution Flow (`main.py`)

[IMPL] Full sequential pipeline per incoming message:

```
1.  normalize(text) -> cleaned text
2.  parse(text) -> Signal | None
    -> None: log DEBUG "Unparseable message", skip
3.  validate(signal) -> ValidationResult
    -> INVALID/IGNORED: log INFO with reason, skip
4.  risk_engine.can_trade_symbol(symbol)
    -> False: log WARNING "Symbol concentration limit", skip
5.  risk_engine.can_trade_sector(symbol)
    -> False: log WARNING "Sector concentration limit", skip
6.  risk_engine.check_exposure()
    -> False: log WARNING "Risk limit reached", skip
7.  fetch_available_capital() -> float
    -> 0: log ERROR "Cannot fetch capital", skip
8.  risk_engine.calculate_quantity(signal, capital) -> int
    -> 0: log INFO "Position sizing returned 0", skip
9.  Log sizing detail (one line):
    "Sizing [SYMBOL]: capital=X risk=Y%=Z entry=E sl=S tp=T risk/sh=R reward/sh=W R:R=1:N qty=floor(Z/R)=Q value=V total_risk=TR(P%)"
10. build_order(signal, quantity) -> Order
11. send_order(order) -> TradeResult
    -> SUCCESS:
       a. risk_engine.record_trade(symbol)
       b. risk_engine.add_margin(quantity, entry, product)
       c. risk_engine.add_heat(quantity, entry, sl)
       d. Log capacity_status()
       e. If bracket.enabled: send_bracket_legs() -> (sl_result, tp_result)
       f. tracker.register(TrackedPosition with all order IDs)
12. db.save(signal, order, result)   [always, for audit trail]
```

### Startup Flow
```
1. setup_logger()
2. Log config (sizing mode, min R:R, poll interval, channels)
3. fetch_trading_mode() -> log LIVE or ANALYZE
4. Start tracker polling loop (background asyncio task)
5. Start Telegram listener
6. SIGINT/SIGTERM handlers -> graceful shutdown (stop tracker, cancel tasks)
```

---

## 14. Logging (`logger_setup.py`)

[IMPL] Loguru configuration:

- Console: `INFO` level, stderr
- File: `signal_engine/logs/signal_engine_{date}.log`, `DEBUG` level
- Rotation: 1 day, retention: 30 days
- Format: `{time:YYYY-MM-DD HH:mm:ss} | {level} | {module} | {message}`

[CONSTRAINT] No Unicode/emoji in log statements.

---

## 15. Database (`db.py`)

[IMPL] SQLite audit trail at `signal_engine/data/trades.db`.

Table `trades`:
```
id, strategy, direction, symbol, entry, sl, target, quantity,
order_id, status, message, signal_time, received_at, executed_at
```

Auto-creates table on first use. Saves every trade attempt (success and failure). Never raises.

**Risk counter store** (`db/risk.db`):
- Managed by `RiskStore` in `risk_store.py`
- Keyed by `(mode, trade_date)` -- live/sandbox counters isolated
- Schema: `mode TEXT, trade_date TEXT, trades_today INT, open_positions INT, daily_loss REAL, ...`

---

## 16. Error Handling Matrix

| Error | Action |
|-------|--------|
| Telegram disconnect | Reconnect with exponential backoff |
| Unparseable message | Log DEBUG, skip |
| Invalid signal | Log INFO with reason, skip |
| Duplicate / stale signal | Log INFO, skip |
| R:R below minimum | Log INFO (IGNORED), skip |
| SL too tight | Log INFO (INVALID), skip |
| Symbol/sector concentration | Log WARNING, skip |
| Risk limit / heat / utilization | Log WARNING, skip |
| Capital fetch fails | Log ERROR, skip |
| Position size = 0 | Log INFO, skip (never force qty=1) |
| OpenAlgo timeout | Log ERROR, do not retry |
| OpenAlgo rejection | Log WARNING with response body |
| SL/TP placement fails | Log WARNING, retry up to max_sl_retries |
| OCO cancel fails | Log WARNING, graceful continue |
| Position poll error | Log WARNING, skip cycle, retry next poll |
| Unexpected exception | Log CRITICAL with traceback |

---

## 17. Security

[CONSTRAINT] Secrets in `.env` only. Non-sensitive config in `config.yaml`.

- `.env`, `signal_engine/data/`, `signal_engine/logs/`, `*.session` all gitignored
- `.env.example` with empty values and comments only
- API key passed in request body, never logged

---

## 18. Testing

[IMPL] Comprehensive test suite — **329 tests** (325 unit + 4 integration):

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `test_risk.py` | 93 | Sizing, slippage, margin cap, price filter, heat, unrealised drawdown, persistent counters, capacity, symbol/sector concentration |
| `test_executor.py` | 30 | Entry order, SL order, TP order, bracket legs, OCO |
| `test_normalizer.py` | 27 | Unicode normalization, whitespace cleanup, edge cases |
| `test_api_client.py` | 25 | All API functions, positionbook, cancel order, order status |
| `test_parser.py` | 24 | Signal parsing, edge cases, optional fields |
| `test_validator.py` | 21 | Entry/SL/TP, R:R, duplicate, min_sl_pct |
| `test_tracker.py` | 21 | Register, batch positionbook, OCO cancellation, margin release |
| `test_config.py` | 20 | Fail-fast validation, all required keys, sectors.yaml loading |
| `test_main.py` | 13 | Full pipeline, concentration risk, bracket order flow |
| `test_risk_store.py` | 7 | SQLite save/load, mode isolation, weekly/monthly aggregation |
| `test_db.py` | 5 | Table creation, save, column values, error handling |
| `test_telegram_integration.py` | 4 | Live Telegram connection (requires session) |
| `test_auto_login.py` (root) | 39 | Auto-login, TOTP, broker name, auth verification, startup/shutdown summary, Telegram notify (notify_channel + fallback) |

Run tests:
```bash
PYTHONPATH=. uv run pytest signal_engine/tests/ -v

# Skip integration (no Telegram session required)
PYTHONPATH=. uv run pytest signal_engine/tests/ -v -m "not integration"
```

---

## 19. Implementation Status

### Original MVP Scope -- ALL COMPLETED

- [x] Telegram listener (multi-channel, name + id, stale guard)
- [x] Signal parser (generic key-value, any strategy name)
- [x] Signal validator (entry/SL/target/direction/R:R/duplicate)
- [x] Risk engine (fixed fractional + pct of capital sizing + exposure checks)
- [x] Live capital from OpenAlgo funds API (no hardcoded capital)
- [x] Dynamic position sizing (no fixed quantity)
- [x] OpenAlgo order placement
- [x] Live/analyze mode auto-detection
- [x] Position tracker with P&L monitoring
- [x] Loss tracking fed back to risk engine
- [x] SQLite audit trail
- [x] Loguru logging (console + rotating file)
- [x] Split config: `config.yaml` + `.env`
- [x] Fully isolated from OpenAlgo core

### Phase 2 Improvements -- ALL COMPLETED

- [x] **Bug fix:** `return 0` (not `return 1`) when position size is zero — never force unsafe trades
- [x] **Slippage buffer:** `slippage_factor` widens `risk_per_share` before sizing
- [x] **Portfolio heat tracking:** sum of open risk capped at `max_portfolio_heat`
- [x] **Unrealised drawdown:** combined with realised loss for daily limit check
- [x] **Persistent risk counters:** `RiskStore` SQLite, keyed by `(mode, date)`, restart-safe
- [x] **Weekly/monthly loss tracking:** via `RiskStore` aggregation
- [x] **Min SL %:** validator rejects SL tighter than `min_sl_pct` of entry
- [x] **Margin-aware position sizing:** caps quantity by available capital × leverage (`margin_multiplier`)
- [x] **Capital utilization tracking:** `committed_margin`, blocks trades at `max_capital_utilization`
- [x] **Simulated bracket orders:** SL-M + TP LIMIT legs after entry (no broker bracket API needed)
- [x] **OCO cancellation:** tracker cancels remaining leg when SL/TP triggers
- [x] **Symbol concentration limits:** `max_positions_per_symbol`
- [x] **Sector correlation risk:** `max_positions_per_sector` with `sectors.yaml` mapping
- [x] **`sectors.yaml`:** Comprehensive NSE sectoral index constituents (17 sectors, ~200 stocks)
- [x] **Full signal log:** no truncation, entire signal on one line in logs
- [x] **Detailed sizing log:** one-line calculation trace (capital, risk%, qty formula, R:R, value, total risk)
- [x] **Capacity status log:** after each trade showing heat/margin/position utilization
- [x] **Graceful exit:** SIGINT/SIGTERM handlers, clean asyncio shutdown
- [x] **Polling optimization:** single `fetch_positionbook()` call replaces N individual `fetch_open_position()` calls per cycle

### Phase 3: Config Tuning & Cleanup -- ALL COMPLETED

- [x] **Risk parameter tuning for intraday:** Aligned all limits for 5-position intraday setup
- [x] **Slippage buffer enabled:** `slippage_factor: 0.05` (5% of SL distance) for MARKET order fill protection
- [x] **Loss limits rebalanced:** Daily 4%, weekly 8% — scaled for 5 concurrent positions at 1% risk each
- [x] **Portfolio heat aligned:** `max_portfolio_heat: 0.05` matches 5 positions x 1% risk
- [x] **Position capacity increased:** `max_open_positions: 5`, `max_trades_per_day: 8` for better capital utilization
- [x] **Sector diversification enforced:** `max_positions_per_sector: 3` (forces spread across sectors)
- [x] **Min SL guard tightened:** `min_sl_pct: 0.005` (0.5%) rejects noise-level stop losses
- [x] **Sandbox capital realistic:** `sandbox_capital: 100000` for meaningful paper trading
- [x] **Dead code cleanup:** Removed unused `PriceType`/`BracketLeg` enums, stale imports, unused variable

### Phase 4: Live Trading Fixes (2026-03-12)

- [x] **Null orderid fix:** `place_order_service.py` checks `order_id is not None` before returning success
- [x] **Tick price rounding:** SL/TP prices rounded to NSE 0.05 tick size (prevents broker LIMIT order rejection)
- [x] **Null orderid detection in signal engine:** `send_order()` returns REJECTED when API returns success with null orderid
- [x] **Startup risk summary:** Logs restored risk state (positions, trades, losses, limits) on engine restart
- [x] **Duplicate log removal:** Removed redundant position sizing log from `risk.py` (kept detailed one in `main.py`)
- [x] **Price filter configured:** `min_entry_price: 100`, `max_entry_price: 800` for tradeable universe
- [x] **PineScript co-located:** Strategy source added to `signal_engine/pinescripts/intraday/orb/`
- [x] **TP strategy analysis:** Simulated 5 strategies across 196 trades — TP1 (1R) confirmed optimal (+69R vs -60R for TP1.5)

### Phase 5: Automated Startup & Auth Verification (2026-03-15)

- [x] **Scripts moved to `signal_engine/scripts/`**: All startup automation self-contained, no changes to core OpenAlgo
- [x] **Configurable broker name**: `BROKER_NAME` env var (default: `mstock`), strips/lowercases
- [x] **Auth token verification**: After auto-login, calls broker funds API to confirm token is live before starting signal engine
- [x] **Startup summary**: Logs detailed system state (available cash, utilized margin, realized/unrealized P&L, collateral, trading config, risk params, channels)
- [x] **Telegram startup notification**: Sends startup summary to `notify_channel` (or all signal channels if not configured)
- [x] **Telegram shutdown notification**: Sends shutdown summary (broker, reason, timestamp) before services stop — sent while app.py is still running so Telegram has network access
- [x] **PID file management**: `signal_engine/openalgo.pid` for reliable process tracking (replaces fragile `pgrep -f`)
- [x] **Service controller**: `openalgoctl.sh` with start/run/stop/restart/status; `start` is an alias for `run` (foreground blocking)
- [x] **Graceful shutdown trap**: `openalgoctl.sh run` catches SIGINT/SIGTERM/EXIT, sends shutdown notification, then kills processes
- [x] **Log rotation**: `openalgoctl.sh` rotates `openalgoctl.log` at 5MB
- [x] **Windows service window**: `openalgoctl.ps1 start` launches `openalgoctl.sh run` in a minimized cmd window; tracks that window's PID in `openalgo-service.pid` for reliable kill on stop/restart
- [x] **Scheduled stop**: `createTaskOpenAlgoScheduler.ps1` creates both start (8:50 AM) and stop (3:30 PM) weekday tasks; stop task calls `openalgoctl.ps1 stop` which sends shutdown notification via WSL before killing the service window
- [x] **WSL process management fix**: `openalgoctl.ps1` kills the Windows-side service window process tree (`taskkill /T /F`) not just WSL PIDs, preventing orphaned cmd windows on restart
- [x] **WSL UTF-16 fix**: Handles BOM in `wsl -l -q` output for reliable distro detection
- [x] **Separate notify channel**: `telegram.notify_channel` in config.yaml sends startup/shutdown notifications to a dedicated personal channel instead of all signal channels; falls back to signal channels if not configured

### Not Implemented (Future Considerations)

- [ ] **Multi-strategy performance tracking:** per-strategy P&L, win rate, avg R:R, drawdown (broker + OpenAlgo already provide trade analytics)
- [ ] **Web UI / dashboard:** real-time view of open positions, P&L, risk utilization
- [ ] **ATR-based stop/sizing:** dynamic SL based on volatility instead of fixed price
- [ ] **Alert system:** Telegram bot sending trade notifications back to trader
- [ ] **Symbol allowlist:** restrict trading to a pre-approved universe
- [ ] **Dynamic risk scaling:** reduce `risk_per_trade` during drawdown periods
- [ ] **Portfolio VAR:** value-at-risk across all open positions
- [ ] **Multi-broker support:** route different strategies to different brokers

---

## 20. Running the Signal Engine

### First-Time Setup
```bash
# 1. Configure secrets
cp signal_engine/.env.example signal_engine/.env
# Edit .env with your Telegram and OpenAlgo credentials

# 2. Configure settings
# Edit signal_engine/config.yaml -- channels, sizing, risk parameters
# Edit signal_engine/sectors.yaml -- add/remove stocks as needed

# 3. Authenticate Telegram (one-time, interactive)
PYTHONPATH=. uv run python -c "
import asyncio
from telethon import TelegramClient
from signal_engine.config import settings
import os
os.makedirs('signal_engine/data', exist_ok=True)
async def auth():
    c = TelegramClient('signal_engine/data/telegram', settings.telegram_api_id, settings.telegram_api_hash)
    await c.start(phone=settings.telegram_phone)
    me = await c.get_me()
    print(f'Authenticated as {me.first_name}')
    await c.disconnect()
asyncio.run(auth())
"
```

### Run
```bash
# Manual start (signal engine only)
PYTHONPATH=. uv run python -m signal_engine.main

# Automated start (OpenAlgo + auto-login + signal engine)
./signal_engine/scripts/openalgoctl.sh run

# Service controller
./signal_engine/scripts/openalgoctl.sh start|stop|restart|status

# Windows Task Scheduler (run in PowerShell as admin)
powershell -ExecutionPolicy Bypass -File signal_engine/scripts/createTaskOpenAlgoScheduler.ps1
```

### Test
```bash
# All tests
PYTHONPATH=. uv run pytest signal_engine/tests/ -v

# Unit only (no Telegram session needed)
PYTHONPATH=. uv run pytest signal_engine/tests/ -v -m "not integration"

# With coverage
PYTHONPATH=. uv run pytest signal_engine/tests/ --cov=signal_engine --cov-report=term-missing
```

---

*End of PRD*
