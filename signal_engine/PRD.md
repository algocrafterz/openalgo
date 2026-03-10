# PRD: Automated Trade Execution from TradingView Telegram Signals

> **Document Purpose:** Implementation reference for Claude (or any LLM agent).
> **Reading convention:** Sections marked `[IMPL]` contain direct implementation directives. Sections marked `[CONTEXT]` provide background only. `[CONSTRAINT]` marks hard rules that must not be violated.
> **Status:** MVP implemented and tested. See Section 19 for implementation status.

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
Telegram Channel(s)  [one or more channels receive webhook messages as text]
        |
TelegramListener     [module: listener.py]
        |
SignalParser          [module: parser.py]
        |
SignalValidator       [module: validator.py]
        |
RiskEngine            [module: risk.py]
   |-- check_exposure() -> skip if limits breached
   |-- fetch_available_capital() -> live capital from OpenAlgo funds API
   |-- calculate_quantity() -> dynamic position sizing
        |
ExecutionLayer        [module: executor.py]
        |
OpenAlgo HTTP API     [local instance, auto-routes live/sandbox]
        |
Broker (mStocks or sandbox)

[Background]:
PositionTracker       [module: tracker.py]
   |-- polls /api/v1/openposition every N seconds
   |-- detects closures, computes P&L delta
   |-- updates risk engine loss counters
```

---

## 3. Technology Stack

[CONSTRAINT] Do not add dependencies beyond those listed. No over-engineering.

| Purpose         | Library         | Notes                              |
|----------------|-----------------|------------------------------------|
| Telegram client | `telethon`      | MTProto, async                     |
| Data models     | `pydantic` v2   | Strict validation                  |
| HTTP client     | `httpx`         | Async, for OpenAlgo API calls      |
| Logging         | `loguru`        | Structured, file + console         |
| Config (secrets)| `python-dotenv` | Load `.env` file (secrets only)    |
| Config (settings)| `pyyaml`       | Load `config.yaml` (user-tunable)  |
| Persistence     | `sqlite3`       | stdlib, audit trail for all trades |
| Testing         | `pytest`        | Unit + integration tests           |
| Testing (async) | `pytest-asyncio`| Async test support                 |

---

## 4. Project Structure

[IMPL] Fully isolated from OpenAlgo core -- zero modifications to existing files.

```
signal_engine/
    __init__.py              # Package init, version string
    main.py                  # Entry point, pipeline orchestration
    config.py                # Loads config.yaml + .env, exposes Settings singleton
    config.yaml              # User-configurable settings (non-sensitive)
    listener.py              # Telegram client, multi-channel message handler
    parser.py                # Parses raw message -> Signal object
    validator.py             # Validates Signal against trading rules
    risk.py                  # Position sizing, exposure checks, loss tracking
    executor.py              # Builds and sends order to OpenAlgo
    api_client.py            # Async wrapper for OpenAlgo REST API endpoints
    tracker.py               # Position polling, closure detection, P&L tracking
    models.py                # Pydantic models: Signal, Order, TradeResult, etc.
    db.py                    # SQLite audit trail
    logger_setup.py          # Loguru configuration
    .env                     # Secrets (gitignored)
    .env.example             # Template for secrets (no values)
    PRD.md                   # This document
    data/                    # Runtime data (gitignored)
        trades.db            # SQLite audit database
        telegram.session     # Telegram auth session
    logs/                    # Log files (gitignored)
        signal_engine_*.log
    tests/
        __init__.py
        test_parser.py       # 17 tests
        test_validator.py    # 14 tests
        test_risk.py         # 18 tests
        test_executor.py     # 11 tests
        test_api_client.py   # 13 tests
        test_tracker.py      #  8 tests
        test_db.py           #  5 tests
        test_main.py         #  7 tests
        test_telegram_integration.py  # 4 integration tests
```

---

## 5. Configuration

### 5.1 Split Configuration Architecture

[IMPL] Configuration is split into two files:

- **`.env`** -- Secrets only (API keys, tokens, phone numbers). Gitignored.
- **`config.yaml`** -- All user-tunable settings (non-sensitive). Checked into git.

No hardcoded values in code files. All tunable values come from config.

### 5.2 `.env` File Schema (Secrets Only)

```env
# Telegram (https://my.telegram.org/apps)
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=

# OpenAlgo
OPENALGO_BASE_URL=http://127.0.0.1:5000
OPENALGO_API_KEY=
```

### 5.3 `config.yaml` Schema (User-Tunable Settings)

```yaml
# Telegram -- multiple channels supported
telegram:
  channels:
    - name: "orb_channel"          # Human-readable label for logs
      id: -1003518225740           # Numeric chat ID or @username

# Position Sizing
# Modes: fixed_fractional | pct_of_capital
sizing:
  mode: fixed_fractional
  risk_per_trade: 0.01             # 1% risk per trade (fixed_fractional)
  pct_of_capital: 0.05             # 5% of capital per trade (pct_of_capital)
  max_position_size: 0.20          # Max 20% of capital in single position

# Risk Management
risk:
  daily_loss_limit: 0.03           # 3% of capital
  weekly_loss_limit: 0.06          # 6% of capital
  monthly_loss_limit: 0.10         # 10% of capital
  max_open_positions: 3
  max_trades_per_day: 5
  min_rr: 1.0                     # Minimum reward:risk ratio (1.0 = 1:1)
  duplicate_window_seconds: 60     # Ignore duplicate signals within this window
  stale_signal_seconds: 60         # Ignore signals older than this

# Position Tracking
tracking:
  poll_interval: 30                # Seconds between position checks

# Broker / Exchange
broker:
  exchange: NSE
  product: MIS                     # MIS or CNC
  order_type: MARKET               # MARKET or LIMIT

# Listener
listener:
  max_retries: 5                   # Max reconnection attempts
  base_backoff: 2                  # Base backoff in seconds (exponential)

# API
api:
  timeout: 5.0                     # HTTP request timeout in seconds
```

### 5.4 `config.py`

[IMPL] Uses frozen dataclass (not pydantic-settings) to load and merge both config sources. Exposes a singleton `settings` instance imported by all modules.

Key types:
- `TelegramChannel(name: str, id: int | str)` -- immutable dataclass per channel
- `Settings` -- frozen dataclass with all configuration fields
- `settings` -- module-level singleton built at import time

---

## 6. Data Models (`models.py`)

[IMPL] Pydantic v2 models with enum-based status fields:

### Enums
```python
class Direction(str, Enum):   LONG, SHORT
class Action(str, Enum):      BUY, SELL
class ValidationStatus(str, Enum):  VALID, INVALID, IGNORED
class OrderStatus(str, Enum): SUCCESS, TIMEOUT, REJECTED, ERROR
```

### `Signal`
```python
class Signal(BaseModel):
    strategy: str           # e.g. "ORB"
    direction: Direction    # LONG or SHORT
    symbol: str             # e.g. "RELIANCE"
    entry: float
    sl: float               # stop-loss price
    target: float
    time: Optional[str]     # as received, e.g. "09:35" (optional)
    raw_message: str        # original unparsed text
    received_at: datetime   # UTC timestamp (auto-set)
```

### `Order`
```python
class Order(BaseModel):
    symbol: str
    exchange: str
    action: Action          # BUY or SELL
    quantity: int
    price: float            # 0 for MARKET
    order_type: str         # MARKET or LIMIT
    product: str            # MIS or CNC
    strategy_tag: str       # for audit trail
```

### `TradeResult`
```python
class TradeResult(BaseModel):
    order_id: str
    status: OrderStatus     # SUCCESS, TIMEOUT, REJECTED, ERROR
    message: str
    timestamp: datetime     # UTC (auto-set)
```

### `ValidationResult`
```python
class ValidationResult(BaseModel):
    status: ValidationStatus  # VALID, INVALID, IGNORED
    reason: str = ""
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
[IMPL] Parser must:
- Extract `strategy` and `direction` from the first line (e.g. `"ORB LONG"`)
- Parse `Symbol`, `Entry`, `SL`, `Target`, `Time` as key-value pairs (case-insensitive keys)
- Strip whitespace from all values
- Be generic -- works for any strategy name, not just ORB
- Mandatory fields: `Symbol`, `Entry`, `SL`, `Target`
- Optional field: `Time`
- Return `None` if mandatory fields are missing (not raise exception)

---

## 8. Signal Validation (`validator.py`)

[IMPL] Validate in this order. Return `ValidationResult` for each check.

| Check | Rule | Result if Fails |
|-------|------|-----------------|
| Entry validity | `entry > 0` | `INVALID` |
| SL present | `sl > 0` | `INVALID` |
| Target present | `target > 0` | `INVALID` |
| SL direction | LONG: `sl < entry`; SHORT: `sl > entry` | `INVALID` |
| Target direction | LONG: `target > entry`; SHORT: `target < entry` | `INVALID` |
| R:R ratio | `reward / risk >= min_rr` (configurable, default 1.0) | `IGNORED` |
| Duplicate | Same symbol + direction + entry within `duplicate_window_seconds` | `IGNORED` |

Note: Symbol allowlist check from original PRD is **not implemented** (deferred).

---

## 9. Risk Engine (`risk.py`)

### 9.1 Position Sizing

[IMPL] Two sizing modes, selected via `config.yaml`:

**Fixed Fractional** (default):
```
risk_amount = capital * risk_per_trade
risk_per_share = abs(entry - sl)
quantity = floor(risk_amount / risk_per_share)
quantity = max(quantity, 1)
```

**Percent of Capital**:
```
allocation = capital * pct_of_capital
quantity = floor(allocation / entry)
quantity = max(quantity, 1)
```

**Max Position Size Cap** (both modes):
```
if max_position_size > 0:
    max_qty = floor(capital * max_position_size / entry)
    quantity = min(quantity, max_qty)
```

[CONSTRAINT] Capital is **always** fetched live from OpenAlgo funds API (`/api/v1/funds`). Never hardcoded. Returns broker capital in live mode, sandbox capital (100,000 INR) in analyze mode.

### 9.2 Exposure Checks

[IMPL] Before sending any order, check all of the following. If any fails, skip the trade:

| Check | Condition to PASS |
|-------|-------------------|
| Daily loss limit | `daily_realised_loss < capital * daily_loss_limit` |
| Weekly loss limit | `weekly_realised_loss < capital * weekly_loss_limit` |
| Monthly loss limit | `monthly_realised_loss < capital * monthly_loss_limit` |
| Max open positions | `open_positions < max_open_positions` |
| Max daily trades | `trades_today < max_trades_per_day` |

### 9.3 Loss Tracking

[IMPL] Loss tracking is done through:
- `record_trade()` -- increments `trades_today` and `open_positions` on successful order
- `record_close(pnl)` -- decrements `open_positions`, adds loss to daily/weekly/monthly counters (only for negative PnL)
- Daily counters auto-reset at midnight UTC
- Capital is tracked via `_last_known_capital` from the most recent funds API call

---

## 10. OpenAlgo API Integration

### 10.1 API Client (`api_client.py`)

[IMPL] Async wrapper for all OpenAlgo REST API calls:

| Function | Endpoint | Returns |
|----------|----------|---------|
| `fetch_trading_mode()` | `/api/v1/analyzer` | `(mode_str, is_analyze: bool)` |
| `fetch_available_capital()` | `/api/v1/funds` | `float` (availablecash) |
| `fetch_open_position(symbol, strategy, exchange, product)` | `/api/v1/openposition` | `int` (qty; 0=closed, -1=error) |
| `fetch_realised_pnl()` | `/api/v1/funds` | `float` (m2mrealized) |

All functions return safe defaults on failure (0.0 or -1). Never raise exceptions.

### 10.2 Order Execution (`executor.py`)

**Build Order:**
- Maps LONG to BUY, SHORT to SELL
- Sets price=0 for MARKET orders, price=entry for LIMIT
- Fills exchange/product from config defaults

**Send Order:**
```
POST {OPENALGO_BASE_URL}/api/v1/placeorder
Content-Type: application/json

{
  "apikey": "...",
  "strategy": "ORB",
  "symbol": "RELIANCE",
  "action": "BUY",
  "exchange": "NSE",
  "pricetype": "MARKET",
  "product": "MIS",
  "quantity": 10,
  "price": "0"
}
```

Response handling:
- HTTP 200 with success -> `TradeResult(status=SUCCESS)`
- HTTP timeout -> `TradeResult(status=TIMEOUT)`
- HTTP error -> `TradeResult(status=REJECTED)`
- Other exception -> `TradeResult(status=ERROR)`

[CONSTRAINT] Fire-and-forget. No retry logic.

---

## 11. Position Tracker (`tracker.py`)

[IMPL] Background polling loop that detects position closures and updates risk counters.

### `TrackedPosition` (dataclass)
```python
symbol, strategy, exchange, product, entry_price, quantity, sl, target
```

### `PositionTracker`
- `register(position)` -- add to tracking dict (key: `symbol:strategy`)
- `check_positions()` -- poll all tracked positions:
  - `fetch_open_position()` returns current quantity
  - If qty=0 (closed): compute P&L delta from `fetch_realised_pnl()`, call `risk_engine.record_close(pnl_delta)`, remove from tracking
  - If qty=-1 (API error): skip this cycle, retry next poll
  - If qty>0 (still open): leave as-is
- `start()` -- run polling loop every `poll_interval` seconds (background asyncio task)
- `stop()` -- graceful shutdown

### P&L Delta Calculation
```
new_realised_pnl = fetch_realised_pnl()  # day's total from funds API
pnl_delta = new_realised_pnl - last_realised_pnl
last_realised_pnl = new_realised_pnl
```

---

## 12. Telegram Listener (`listener.py`)

[IMPL] Use `telethon` async client:

- Connect using `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE` from `.env`
- Monitor **multiple channels** from `config.yaml` (support both `@username` and numeric ID)
- Register `NewMessage` event handler on all configured channels
- On new message:
  - Identify source channel by name (for logging)
  - Check stale guard: skip messages older than `stale_signal_seconds`
  - Pass `message.text` to pipeline callback
- Handle disconnection: auto-reconnect with exponential backoff (configurable max retries and base backoff)
- Session file persisted at `signal_engine/data/telegram.session` for reuse across restarts

---

## 13. Execution Flow (`main.py`)

[IMPL] Full sequential pipeline per incoming message:

```
1. message.text received from Telegram listener
2. parse(text) -> Signal | None
   -> None: log "unparseable message", skip
3. validate(signal) -> ValidationResult
   -> INVALID/IGNORED: log reason, skip
4. risk_engine.check_exposure() -> bool
   -> False: log "risk limit hit", skip
5. fetch_available_capital() -> float
   -> 0.0: log "cannot fetch capital", skip
6. risk_engine.calculate_quantity(signal, capital) -> int
7. build_order(signal, quantity) -> Order
8. send_order(order) -> TradeResult
9. IF SUCCESS:
   a. risk_engine.record_trade()
   b. tracker.register(position)
10. db.save(signal, order, result)   [always, for audit trail]
```

### Startup Flow
```
1. setup_logger()
2. Log config (sizing mode, min R:R, poll interval, channels)
3. fetch_trading_mode() -> log LIVE or ANALYZE
4. Start tracker polling loop (background asyncio task)
5. Start Telegram listener (blocking)
6. On shutdown: stop tracker, cancel task
```

---

## 14. Logging (`logger_setup.py`)

[IMPL] Loguru configuration:

- Console: `INFO` level, stderr
- File: `signal_engine/logs/signal_engine_{date}.log`, `DEBUG` level
- Rotation: 1 day
- Retention: 30 days
- Format: `{time:YYYY-MM-DD HH:mm:ss} | {level} | {module} | {message}`

[CONSTRAINT] No unicode/emoji in log statements.

---

## 15. Database (`db.py`)

[IMPL] SQLite audit trail at `signal_engine/data/trades.db`.

Table `trades`:
```
id, strategy, direction, symbol, entry, sl, target, quantity,
order_id, status, message, signal_time, received_at, executed_at
```

- Auto-creates table on first connection
- Saves every trade attempt (success and failure) for audit
- Catches all exceptions, logs errors, never raises

---

## 16. Error Handling Matrix

[IMPL] Handle every case:

| Error | Action |
|-------|--------|
| Telegram disconnect | Reconnect with exponential backoff (configurable) |
| Unparseable message | Log as DEBUG, skip silently |
| Invalid signal | Log as INFO with reason, skip |
| Duplicate signal | Log as INFO, skip |
| R:R below minimum | Log as INFO, skip |
| Risk limit breached | Log as WARNING, skip trade |
| Capital fetch fails | Log as ERROR, skip trade |
| OpenAlgo timeout | Log as ERROR, do not retry |
| OpenAlgo rejection | Log as ERROR with response body |
| Position poll error | Log as WARNING, skip cycle, retry next poll |
| Unexpected exception | Log as CRITICAL with traceback |

---

## 17. Security

[CONSTRAINT] Secrets in `.env` only. Non-sensitive config in `config.yaml`.

- `.env` must be in `.gitignore` (done)
- `signal_engine/data/` must be in `.gitignore` (done -- contains session, db)
- `signal_engine/logs/` must be in `.gitignore` (done)
- `signal_engine/*.session` must be in `.gitignore` (done)
- Provide `.env.example` with empty values and comments only
- No secrets in logs
- API key passed in request body, not logged

---

## 18. Testing

[IMPL] Comprehensive test suite with 101 tests:

| Test File | Count | Coverage |
|-----------|-------|----------|
| `test_parser.py` | 17 | Valid signals, invalid signals, missing fields, edge cases |
| `test_validator.py` | 14 | Entry/SL/target validation, R:R ratio, duplicate detection |
| `test_risk.py` | 18 | Fixed fractional, pct of capital, max position size, exposure checks, record trade/close, daily reset |
| `test_executor.py` | 11 | Order building (long/short, market/limit), HTTP mocking (success/timeout/error) |
| `test_api_client.py` | 13 | fetch capital, fetch position, fetch PnL, fetch trading mode (live/analyze/error) |
| `test_tracker.py` | 8 | Register positions, check positions (open/closed/profit/loss/error), stop flag |
| `test_db.py` | 5 | Table creation, save operations, column values, error handling |
| `test_main.py` | 7 | Pipeline flow (unparseable, invalid, ignored, risk limit, success, failed order, zero capital) |
| `test_telegram_integration.py` | 4 | Live Telegram connection, channel access, message reading (requires session) |
| **Total** | **97 unit + 4 integration** | |

Run tests:
```bash
PYTHONPATH=. uv run pytest signal_engine/tests/ -v
```

---

## 19. Implementation Status

### MVP Scope -- COMPLETED

- [x] Telegram listener (multi-channel support)
- [x] Signal parser (generic, key-value based)
- [x] Signal validator (entry/SL/target/direction/R:R/duplicate)
- [x] Risk engine (fixed fractional + pct of capital sizing + exposure checks)
- [x] Live capital from OpenAlgo funds API (no hardcoded capital)
- [x] Dynamic position sizing (no fixed quantity)
- [x] OpenAlgo order placement (fire-and-forget)
- [x] Live/analyze mode auto-detection
- [x] Position tracker with P&L monitoring (polls OpenAlgo position API)
- [x] Loss tracking fed back to risk engine (daily/weekly/monthly)
- [x] SQLite audit trail
- [x] Loguru logging (console + rotating file)
- [x] Split config: `config.yaml` (settings) + `.env` (secrets)
- [x] Fully isolated from OpenAlgo core (zero modifications to existing files)
- [x] 101 tests (97 unit + 4 integration)

### Beyond Original MVP (Implemented)

- [x] Multi-channel Telegram support (name + id per channel)
- [x] Position tracking after submission (was "out of scope" in original PRD)
- [x] Percent of capital sizing mode (in addition to fixed fractional)
- [x] Max position size cap
- [x] Configurable min R:R (default 1:1, not hardcoded 2.0)
- [x] API client module for all OpenAlgo interactions
- [x] Daily counter auto-reset at midnight UTC

### Not Implemented (Future)

- [ ] Symbol allowlist validation
- [ ] Web UI or dashboard
- [ ] Multi-broker support
- [ ] ATR-based stop/sizing
- [ ] Portfolio VAR
- [ ] Dynamic risk scaling
- [ ] Alert system (Telegram bot notifications back to trader)
- [ ] Persistent loss tracking across restarts (currently in-memory)

---

## 20. Running the Signal Engine

### First-Time Setup
```bash
# 1. Configure secrets
cd signal_engine
cp .env.example .env
# Edit .env with your Telegram and OpenAlgo credentials

# 2. Configure settings
# Edit config.yaml -- set telegram.channels, sizing, risk parameters

# 3. Authenticate Telegram (one-time, interactive)
cd /path/to/openalgo
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
PYTHONPATH=. uv run python -m signal_engine.main
```

### Verify
```bash
# Run all tests
PYTHONPATH=. uv run pytest signal_engine/tests/ -v

# Run only unit tests (skip integration)
PYTHONPATH=. uv run pytest signal_engine/tests/ -v -m "not integration"
```

---

*End of PRD*
