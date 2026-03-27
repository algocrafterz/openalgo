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
   |-- check_exposure()     -> daily/weekly/monthly loss + portfolio heat + open positions + daily trades
   |-- fetch_available_capital() -> live capital from OpenAlgo funds API
   |-- calculate_quantity() -> risk-based sizing (1% fixed fractional)
        |
MarginAPI            [api_client.py]
   |-- fetch_margin()       -> actual broker margin required for the position (via /api/v1/margin)
   |-- adjust_qty_for_margin() -> scales qty down proportionally if margin > live capital
        |
ExecutionLayer       [executor.py]
   |-- send_order()         -> ENTRY order (MARKET)
   |-- send_bracket_legs()  -> SL-M order ONLY (no TP broker order — see Indian broker OCO constraint)
        |
OpenAlgo HTTP API    [local instance, auto-routes live/sandbox]
        |
Broker (live or sandbox)

[Background]:
PositionTracker      [tracker.py]
   |-- polls /api/v1/positionbook every poll_interval seconds (10s)
   |-- LTP monitoring: detects when ltp crosses TP level -> cancel SL -> MARKET exit
   |-- detects position close (qty=0), computes P&L delta, updates risk engine
   |-- sends Telegram notifications at each lifecycle event

TimeExitScheduler    [tracker.py]
   |-- fires once/day at configured IST time (15:00) to close all positions
   |-- cancel_all_orders + close_all_positions per strategy

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
  min_entry_price: 100           # Min stock price to trade (0 = no filter)
  max_entry_price: 800           # Max stock price to trade (0 = no filter)
  slippage_factor: 0.10          # Widen risk_per_share by 10% — entry (MARKET) + TP exit (MARKET) both slip
  sandbox_capital: 100000        # Override capital in analyze mode (0 = use OpenAlgo)
  use_day_start_capital: true    # Cache capital at first signal of day, use for all trades (equal risk per trade)

risk:
  daily_loss_limit: 0.04         # 4% of capital (allows 4 losers at 1% before lockout)
  weekly_loss_limit: 0.08        # 8% of capital (allows 2 bad days per week)
  monthly_loss_limit: 0.10
  max_portfolio_heat: 0.03       # 3% of capital max open risk (2 concurrent at 1% + buffer)
  max_open_positions: 2          # 2 concurrent slots (fits ~15K MIS margin budget)
  max_trades_per_day: 8
  min_rr: 1.0                    # Minimum reward:risk ratio
  duplicate_window_seconds: 60
  stale_signal_seconds: 60
  min_sl_pct: 0.005              # Reject SL tighter than 0.5% of entry (0 = disabled)
  max_positions_per_symbol: 1    # Max concurrent positions in same symbol
  max_positions_per_sector: 0    # Disabled — stock universe spans same sectors

tracking:
  poll_interval: 10              # 10s balances TP responsiveness vs API load

broker:
  exchange: NSE
  product: MIS
  order_type: MARKET

listener:
  max_retries: 5
  base_backoff: 2

api:
  timeout: 5.0
  margin_retries: 3              # Retry count for Margin API before skipping trade

bracket:
  enabled: true
  sl_order_type: SL-M            # SL or SL-M for stop-loss leg
  max_sl_retries: 5              # 5 retries with 0.5s delay = 2.5s window
  retry_delay: 0.5               # Seconds between retries
  tp_exit_retries: 3             # Retry count for tracker TP market exit
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
sizing_capital = day_start_capital  (cached on first signal of day via get_sizing_capital())
risk_amount    = sizing_capital * risk_per_trade          # e.g. 1% of ₹15,662 = ₹156
risk_per_share = abs(entry - sl) * (1 + slippage_factor)  # widened by 10% for MARKET slippage
raw_qty        = floor(risk_amount / risk_per_share)
```

**Percent of Capital**:
```
allocation = sizing_capital * pct_of_capital
raw_qty    = floor(allocation / entry)
```

**Margin adjustment (after risk sizing, uses live capital):**
```
actual_margin = fetch_margin(symbol, qty=raw_qty)   # Margin API: exact broker margin required
if actual_margin <= live_capital:
    quantity = raw_qty                               # fits — use full qty
else:
    quantity = floor(raw_qty * live_capital / actual_margin)  # scale down proportionally
    # MIS margin is linear with qty, so one API call suffices

# If quantity <= 0: return 0 (skip trade, never force qty=1)
```

[CONSTRAINT] Capital **always** fetched live from OpenAlgo funds API. Never hardcoded.
[CONSTRAINT] If quantity after margin adjustment is 0, trade is **skipped** entirely.
[CONSTRAINT] `day_start_capital` used for risk sizing (equal risk per trade). `live_capital` (availablecash from broker) used for margin check (actual available cash).

### 9.2 Exposure Checks (`check_exposure()`)

[IMPL] Called in this order in `main.py`. If any fails, skip the trade:

1. `can_trade_symbol(symbol)` -- symbol concentration (`max_positions_per_symbol`)
2. `can_trade_sector(symbol)` -- sector correlation (`max_positions_per_sector` via `sectors.yaml`)
3. `check_exposure()`:
   - Daily loss: `(daily_realised_loss + unrealised_drawdown) < capital * daily_loss_limit`
   - Weekly loss: `weekly_realised_loss < capital * weekly_loss_limit`
   - Monthly loss: `monthly_realised_loss < capital * monthly_loss_limit`
   - Portfolio heat: `current_heat < capital * max_portfolio_heat`
   - Open positions: `open_positions < max_open_positions`
   - Daily trades: `trades_today < max_trades_per_day`

### 9.3 Loss & Risk Tracking

**Portfolio Heat:**
- `add_heat(qty, risk_per_share)` / `remove_heat()` methods exist in `risk.py`
- Currently not called from `main.py` — heat stays at 0; `max_open_positions` provides equivalent protection

**Unrealised Drawdown:**
- `update_unrealised(unrealised_pnl)` called each tracker cycle
- Negative unrealised PnL combined with realised loss for daily limit check

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
| `fetch_realised_pnl()` | `/api/v1/funds` | `float` (m2mrealized) |
| `fetch_margin(symbol, exchange, action, qty, product)` | `/api/v1/margin` | `float` (total_margin_required) — raises `MarginAPIError` on failure |
| `cancel_order(order_id, strategy)` | `/api/v1/cancelorder` | `bool` |
| `cancel_all_orders(strategy)` | `/api/v1/cancelallorder` | `bool` |
| `close_all_positions(strategy)` | `/api/v1/closeposition` | `bool` |

**`fetch_margin()` error handling:**
- Retries on network/timeout errors (`margin_retries` from config, default 3)
- Raises `MarginAPIError` immediately on application errors (HTTP 4xx, status != success) — no retry
- Raises `MarginAPIError` after all retries exhausted on network errors
- Caller (`main.py`) catches `MarginAPIError` and skips the trade

All other functions return safe defaults on failure (0.0, None, False). Never raise exceptions.

### 10.2 Order Execution (`executor.py`)

**Entry Order (`build_order`, `send_order`):**
- LONG -> BUY, SHORT -> SELL
- price=0 for MARKET, price=entry for LIMIT
- Includes `trigger_price` in payload when > 0

**Bracket Orders (`build_sl_order`, `send_bracket_legs`):**
```
SL order:  action=opposite of entry, order_type=SL-M, trigger_price=sl (rounded to tick)
```
Only SL is placed as a broker order. Retries up to `max_sl_retries` (5) with `retry_delay` (0.5s) between attempts.
Prices are rounded to valid NSE tick size (0.05) to prevent broker rejection.

**Why no TP broker order (Indian broker OCO constraint):**
Indian brokers (mStock/Zerodha etc.) treat any SELL while an existing SL SELL is active as a new SHORT requiring full MIS margin, causing FUND LIMIT INSUFFICIENT. There is no BO/CO product type in OpenAlgo. Solution: SL-M at broker level for safety, TP exit driven by TradingView signals. **SL must ALWAYS be cancelled before placing ANY exit order** (partial or full), for both MIS and CNC products.

**TP execution (tracker-based):**
`tracker.py` polls `positionbook` every 10s, reads LTP, and when `ltp >= tp` (LONG) or `ltp <= tp` (SHORT): cancels SL order first, then places a MARKET exit with retries (`tp_exit_retries`).

**Null order ID handling:** `send_order()` checks for null `orderid` in API response — brokers can return HTTP 200 with `status=false` and `orderid=null` when an order is rejected at broker level.

**Design Decision (2026-03-12):** Signal engine is a dumb executor — it does not compute or override TP/SL levels. All trade logic (TP level selection, R:R calculation) stays in the PineScript strategy. Analysis of 196 trades showed TP1 (1R) at +69R significantly outperforms TP1.5 at -60R. See `pinescripts/intraday/orb/SIGNAL-PERFORMANCE-2026-Q1.md` for full simulation.

---

## 11. Position Tracker (`tracker.py`)

[IMPL] Background polling loop.

### `TrackedPosition`
```python
symbol, strategy, exchange, product, entry_price, quantity, sl, tp,
direction: Direction,     # LONG/SHORT for TP exit direction
entry_order_id, sl_order_id,
tp_triggered: bool        # guard against double-exit
```

### `PositionTracker`
- `register(position)` -- add to tracking dict (key: `symbol:strategy`)
- `check_positions()` -- **single `fetch_positionbook()` call** per cycle:
  - Builds symbol -> (qty, ltp) lookup from positionbook response
  - Per tracked position, two checks each cycle:
    1. **TP monitoring** (while qty > 0): if `ltp >= pos.tp` (LONG) or `ltp <= pos.tp` (SHORT) and not already triggered → call `_exit_at_tp()`
    2. **Close detection** (qty == 0): compute P&L delta from `fetch_realised_pnl()`, call `risk_engine.record_close()`, remove from tracking
  - If positionbook returns `None` (API error): skip entire cycle
- `_exit_at_tp(pos)`:
  - Cancel SL order first via `cancel_order()` (if fails, logs warning but continues)
  - Place MARKET exit order with retries (`tp_exit_retries` from config)
  - On success: `notify_tp_exit_placed`; on total failure: `notify_tp_exit_failed` (URGENT — position unprotected)
- `time_exit_all()` -- force-close all positions at EOD: `cancel_all_orders()` then `close_all_positions()` per strategy, send day summary
- `start()` / `stop()` -- asyncio background task lifecycle

### `TimeExitScheduler`
- Checks clock every 30s, fires `time_exit_all()` once/day at configured IST time
- Also fires as catch-up if past configured time by >5min (engine started late)
- Resets `_fired_today` flag at midnight IST

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
4.  risk_engine.check_exposure()
    -> False: log WARNING "Risk limit reached", skip
5.  risk_engine.can_trade_symbol(symbol)
    -> False: log WARNING "Symbol concentration limit", skip
6.  risk_engine.can_trade_sector(symbol)
    -> False: log WARNING "Sector concentration limit", skip
7.  fetch_available_capital() -> live_capital (float)
    -> 0: log ERROR "Cannot fetch capital", skip
8.  sizing_capital = risk_engine.get_sizing_capital(live_capital)
    (returns cached day-start capital if use_day_start_capital=true)
9.  risk_engine.calculate_quantity(signal, sizing_capital) -> raw_qty
    -> 0: log INFO "Position sizing returned 0", skip
10. fetch_margin(symbol, qty=raw_qty) -> actual_margin   [MarginAPI]
    -> MarginAPIError: log ERROR, skip trade
    -> if actual_margin > live_capital: scale qty = floor(raw_qty * live_capital / actual_margin)
    -> if scaled qty <= 0: skip trade
11. Log sizing detail (one line):
    "Sizing [SYMBOL]: capital=X risk=Y%=Z entry=E sl=S tp=T risk/sh=R reward/sh=W R:R=1:N qty=Q value=V total_risk=TR(P%)"
12. build_order(signal, quantity) -> Order
13. send_order(order) -> TradeResult
    -> SUCCESS:
       a. risk_engine.record_trade(symbol)
       b. Log capacity_status()
       c. If bracket.enabled: send_bracket_legs() -> (sl_result, None)
          [TP is never a broker order — handled by tracker LTP monitoring]
       d. tracker.register(TrackedPosition with direction + sl_order_id)
14. db.save(signal, order, result)   [always, for audit trail]
```

### Exit Pipeline (`_handle_exit`)

Triggered by TradingView EXIT/TP HIT signals (Direction.EXIT):

```
1.  Look up position in tracker by (symbol, strategy)
    -> Not found: fallback to broker API (engine restart recovery)
    -> Still not found: log WARNING, notify, skip
2.  _resolve_exit_qty(signal, pos) -> (exit_qty, is_full_exit)
    - Uses strategy_profiles.tp_levels config (e.g. TP1: 1.0 = 100%)
    - No tp_level or no profile -> full exit
3.  ALWAYS cancel SL order before exit (Indian broker constraint)
    [Broker treats any SELL while SL SELL is active as new SHORT -> FUND LIMIT INSUFFICIENT]
    -> cancel_order(sl_order_id) regardless of partial/full exit
4.  build_exit_order() -> MARKET SELL for exit_qty
5.  send_order(exit_order) -> TradeResult
    -> SUCCESS:
       a. Compute PnL from realised_pnl delta
       b. Full exit: unregister position, record_close in risk engine
       c. Partial exit: reduce tracked qty, clear sl_order_id (no SL re-placement)
       d. tracker.record_exit() for day summary counters
    -> FAILURE: notify_exit_failed
6.  db.save(signal, exit_order, result)
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
| Margin API error (transient) | Retry up to `margin_retries` times |
| Margin API error (app error) | Log ERROR, skip trade immediately (no retry) |
| SL placement fails | Log WARNING, retry up to `max_sl_retries` (5) with 0.5s delay |
| TP exit (tracker) fails | Log ERROR, `notify_tp_exit_failed` URGENT — position unprotected |
| SL cancel (before TP exit) fails | Log WARNING, proceed with market exit anyway |
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

[IMPL] Comprehensive test suite — **273 tests** (unit + integration):

| Test File | Coverage |
|-----------|----------|
| `test_risk.py` | Sizing, slippage, price filter, heat, unrealised drawdown, persistent counters, capacity, symbol/sector concentration |
| `test_executor.py` | Entry order, SL order, TP order builder, bracket legs (SL-only), send_order |
| `test_normalizer.py` | Unicode normalization, whitespace cleanup, edge cases |
| `test_api_client.py` | All API functions, positionbook, cancel order, fetch_margin, MarginAPIError |
| `test_parser.py` | Signal parsing, edge cases, optional fields |
| `test_validator.py` | Entry/SL/TP, R:R, duplicate, min_sl_pct |
| `test_tracker.py` | Register, batch positionbook, LTP TP monitoring, _exit_at_tp, time_exit_all |
| `test_config.py` | Fail-fast validation, all required keys, sectors.yaml loading |
| `test_main.py` | Full pipeline, margin adjustment, concentration risk, bracket order flow |
| `test_risk_store.py` | SQLite save/load, mode isolation, weekly/monthly aggregation |
| `test_db.py` | Table creation, save, column values, error handling |
| `test_telegram_integration.py` | Live Telegram connection (requires session) |
| `test_auto_login.py` (root) | Auto-login, TOTP, broker name, auth verification, startup/shutdown summary |

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

### Phase 6: Margin API + Application-Level OCO (2026-03-25)

- [x] **Indian broker OCO root cause fixed**: TP LIMIT order as 2nd SELL caused FUND LIMIT INSUFFICIENT (broker treats 2nd SELL as new short requiring full MIS margin). Removed TP broker order entirely.
- [x] **Application-level OCO**: SL-M at broker level + tracker LTP monitoring for TP. `_exit_at_tp()`: cancel SL → MARKET exit with retries.
- [x] **Margin API integration**: Replaced hardcoded `margin_multiplier` (wrong for mid-cap stocks, e.g. EMAMILTD actual 51% vs assumed 30%) with `fetch_margin()` calling `/api/v1/margin`. Exact margin per stock, no guesswork.
- [x] **Proportional qty scaling**: `adjusted_qty = floor(raw_qty * live_capital / actual_margin)`. Positions that were previously completely rejected can now take a smaller qty that fits 2nd slot.
- [x] **Removed dead config**: `margin_multiplier`, `max_capital_utilization`, `max_position_size`, `max_tp_retries`, `cancel_retry_count` all removed from config and risk engine.
- [x] **Retry improvements**: `max_sl_retries: 5` (was 3) with `retry_delay: 0.5s`; `tp_exit_retries: 3` for tracker market exit; `margin_retries: 3` for Margin API.
- [x] **slippage_factor 0.05 -> 0.10**: Both entry and TP exit are now MARKET orders (double slippage on round trip).
- [x] **poll_interval 30s -> 10s**: Faster TP detection for volatile ORB stocks.
- [x] **max_open_positions 5 -> 2**: Right-sized for ₹15K MIS margin budget (2 × ~₹7-11K margin).
- [x] **Telegram notifications for TP lifecycle**: `notify_tp_level_hit`, `notify_tp_exit_placed`, `notify_tp_exit_failed` (URGENT), `notify_sl_cancel_failed`.
- [x] **Day summary in tracker**: win/loss/PnL counters tracked in `PositionTracker`, sent via `notify_day_summary` at EOD or time exit.

### Phase 7: SL Cancel Before All Exits + RSI Single TP (2026-03-27)

- [x] **SL cancel before ALL exit orders**: Previously SL was only cancelled on full exits. Partial exits left SL active, causing broker to treat the exit SELL as a new SHORT (FUND LIMIT INSUFFICIENT). Now SL is always cancelled before any exit order (partial or full), for both MIS and CNC.
- [x] **RSI-TP-MR: TP1 = 100%**: Changed from 50% partial exit at TP1 to full 100% exit. Single TP for RSI mean reversion strategy.
- [x] **Simplified partial exit flow**: After partial exit, `sl_order_id` is cleared (no SL re-placement). Remaining position relies on next TP signal or safety EXIT from TradingView.

### Not Implemented (Future Considerations)

- [ ] **Chartink scanner integration:** stock selection via Chartink scans (see Section 21)
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

## 21. Chartink Scanner Integration (Future)

Stock selection via Chartink scans, feeding into the existing PineScript alert -> risk engine -> broker execution pipeline.

### Architecture

```
Chartink Scan (9:16 AM)          PineScript ORB (on all 50 symbols)
        |                                  |
        v                                  v webhook
  Today's candidates              Signal Engine receives alert
  stored in memory                        |
        |                                 |
        +-------- Is symbol --------------+
                  in candidates?
                       |
                  Yes  |  No -> ignore
                       v
                  RiskEngine -> Broker
```

PineScript handles strategy logic (entry/SL/TP). Chartink handles stock selection (volume, gap, liquidity). Signal engine handles risk + execution.

### Chartink Screener Endpoint

No official API. The screener web UI sends a POST internally:

```
POST https://chartink.com/screener/process
Content-Type: application/x-www-form-urlencoded

scan_clause=( {cash} ( latest close > 100 and ... ) )
```

Response: `{"data": [{"nsecode": "RELIANCE", "close": 1420.5, "volume": 8934521, ...}]}`

A session cookie from `GET https://chartink.com/screener` is required first (CSRF protection).

### Sample Scan Clauses

**ORB Candidates (gap + volume spike, liquid stocks):**
```
( {cash} (
  latest close > 100 and
  latest volume > 1.5 * latest sma( latest volume, 20 ) and
  abs( latest open - latest close 1 day ago ) / latest close 1 day ago < 0.025 and
  market cap > 10000
) )
```

**VWAP Pullback Candidates:**
```
( {cash} (
  latest close > 100 and
  latest close < latest vwap and
  latest close > latest vwap * 0.998 and
  latest volume > 1.2 * latest sma( latest volume, 20 )
) )
```

**High Relative Volume (pre-market movers):**
```
( {cash} (
  latest volume > 2 * latest sma( latest volume, 20 ) and
  latest close > 100 and
  latest close < 5000
) )
```

Chartink scan language reference: https://chartink.com/wiki/index.php/Scan_Language

### Integration Options

**Option A: Poll at 9:16 AM (recommended).** Fetch candidates once after market open, cache for the day. Filter incoming PineScript alerts against the candidate list in `main.py`.

**Option B: Chartink Telegram alerts (zero new code).** Chartink can send scan alerts to Telegram. The existing Telegram listener receives them. Requires a parser addition to handle "watchlist update" messages vs trade signals.

### Config (future: config.yaml)

```yaml
chartink:
  enabled: true
  scan_time: "09:16"
  orb_scan: |
    ( {cash} (
      latest close > 100 and
      latest volume > 1.5 * latest sma( latest volume, 20 ) and
      abs( latest open - latest close 1 day ago ) / latest close 1 day ago < 0.025 and
      market cap > 10000
    ) )
```

### Limitations

- Chartink has no official API; the POST endpoint is undocumented
- Rate limiting is unknown; keep requests to 1-2 per minute
- Scan results depend on Chartink's data feed freshness
- Session cookies may expire; re-fetch on 403/401 errors
- Chartink scan language differs from PineScript; not portable

---

*End of PRD*
