# Signal Engine — Reference

Autonomous trading pipeline: Telegram signals → OpenAlgo → Broker.
Runs alongside OpenAlgo as a separate process managed by `openalgoctl.sh`.

---

## Architecture

```
Telegram Channels
      │ raw text
      ▼
listener.py      — async Telethon client, stale guard, multi-channel
normalizer.py    — strip emojis, alias keys, handle TP HIT / pipe formats
parser.py        — text → Signal object
validator.py     — SL/TP direction, R:R, duplicate, stale, blacklist, price filter
      │ valid Signal
      ▼
main.py (_handle_entry / _handle_exit)
      ├─ risk.py        — check_exposure(), calculate_quantity()
      ├─ api_client.py  — fetch_available_capital(), fetch_margin(), fetch_positionbook()
      ├─ executor.py    — build_order(), send_order(), send_bracket_legs()
      ├─ tracker.py     — register position, poll SL fills, time exit, LTP monitoring
      ├─ notifier.py    — Telegram notifications
      └─ db.py          — persist trade audit to SQLite
```

---

## Pipeline

### Entry (LONG/SHORT)
1. `check_exposure()` — daily/weekly/monthly loss + portfolio heat + open positions
2. `fetch_available_capital()` → live capital; skip if below `min_capital_for_entry` floor
3. Day-start cached capital → `calculate_quantity()` → risk-based qty
4. `adjust_qty_for_margin()` [LIVE only]: NSE/BSE — binary reject if full-risk qty doesn't fit live capital (never scales down); derivatives — SpanCalc margin check, scales if needed
5. Apply `test_qty_cap` if set
6. `build_order()` + `send_order()` → `POST /api/v1/placeorder`
7. `send_bracket_legs()` → SL-M order only (see OCO constraint below)
8. `tracker.register()` → track for SL fill detection
9. `risk_engine.record_trade()` → persist counters
10. Notify + persist to DB

### Exit (TP HIT / EXIT signal)
1. Normalizer converts `"ORB TP1 HIT | SYMBOL"` → canonical EXIT signal
2. **Per-position asyncio.Lock acquired** — serializes concurrent TP signals for same position (TradingView fires all TP levels simultaneously at bar close; lock ensures second handler sees already-closed position)
3. Resolve exit qty from `signal.exit_qty_pct` → `tp_levels` config → default 100%
4. **Cancel SL order first** — broker treats any SELL while SL SELL is active as new SHORT
5. `build_exit_order()` → MARKET SELL
6. `send_order()` with `tp_exit_retries` retries
7. Partial exit: reduce tracked qty, clear `sl_order_id`, re-place SL at TP1 − 0.3R (runner SL)
   - `tp1_runner_sl_buffer: 0.3` — 30% of R below TP1. Gives price room to wick-back and test TP1 before continuing to TP1.5. Old value 0.1R was too tight (TMPV 2026-04-13: ₹0.80 gap triggered by normal TP1 test wick). 0.3R scales correctly across ₹150–₹800 filter (9–72 ticks depending on SL%).
   - Runner SL is the **only automated protection** when TP1.5 signal is delayed or missing.
   - `compute_next_tp()` derives next TP level (TP1→TP1.5, TP1.5→TP2) for notification
   - Telegram notification includes: booked qty, remaining qty, new SL, next TP price
   - **TP1.5 exit fires only on TradingView TP1.5 HIT alert** — engine has no autonomous LTP monitoring for TP exits. If the alert is delayed/missing, runner holds until runner SL fires or time exit at 15:00.
8. Full exit: `tracker.unregister()` + `risk_engine.record_close()`

---

## Module Map

| File | Responsibility |
|------|---------------|
| `main.py` | Pipeline orchestration, startup checks, entry/exit routing |
| `listener.py` | Async Telegram channel listener (Telethon) |
| `normalizer.py` | Raw message preprocessing → canonical format |
| `parser.py` | Canonical text → `Signal` model |
| `validator.py` | Signal validation (SL, R:R, duplicates, blacklist) |
| `risk.py` | Position sizing (`RiskEngine`), exposure limits, portfolio heat |
| `risk_store.py` | SQLite persistence for risk counters (restart-safe). Path: `RISK_DB_PATH` |
| `executor.py` | Order construction + OpenAlgo API calls |
| `tracker.py` | Position lifecycle: register, poll SL fills, time exit |
| `api_client.py` | All async OpenAlgo API calls |
| `notifier.py` | Telegram notification dispatch |
| `config.py` | Fail-fast config loader (`Settings` dataclass singleton) |
| `models.py` | `Signal`, `Order`, `TradeResult`, `ValidationResult` |
| `strategies.py` | Strategy name constants |
| `db.py` | SQLite trade audit trail. Path: `_DB_PATH` |
| `logger_setup.py` | Loguru daily rotation |
| `smoke_test.py` | Pre-session health checks + dry run |

### Scripts (`scripts/`)

| File | Purpose |
|------|---------|
| `openalgoscheduler.py` | Startup: auto-login (TOTP), verify auth, start signal engine |
| `openalgoctl.sh` | Service controller: start/run/stop/restart/status, log rotation |
| `openalgoctl.ps1` | Windows: launches `openalgoctl.sh` in minimized cmd |
| `createTaskOpenAlgoScheduler.ps1` | Windows Task Scheduler: 8:50 AM start, 3:30 PM stop |

---

## Signal Format

### Entry
```
STRATEGY DIRECTION
Symbol: SYMBOL
Entry: 250.50
SL: 246.00
TP: 262.00
Exchange: NSE        (optional — defaults to broker.exchange)
Product: MIS         (optional — defaults to broker.product)
```

Pipe format also supported: `ORB LONG | WIPRO` on first line.  
`Target:` is aliased to `TP:` for backward compatibility.

### TP HIT / Exit
```
ORB TP1 HIT | WIPRO
```
Normalizer converts this to canonical EXIT. `TP1` matched against `strategy_profiles.tp_levels`.

---

## Position Sizing

### fixed_fractional (default)
```
qty = floor(capital × risk_per_trade / (|entry - sl| × (1 + slippage_factor)))
```
`slippage_factor: 0.10` — widens the denominator by 10%, producing conservatively fewer shares.
If `qty = 0`, trade is skipped (stock too expensive for the risk budget).

**Actual risk per trade (Q2 2026 validated):**
The 10% slippage buffer means actual risk = `qty × sl_dist ≈ capital × risk_per_trade / 1.10`.
At 1% risk and ₹15K capital: actual risk ≈ ₹138 (0.91%), not ₹152 (1.0%) — the ~9% haircut
absorbs real fill slippage on MARKET entry and TP exit orders. This is by design.

Validated 2026-04-13: JSWENERGY 0.81%, EMAMILTD 0.89%, EXIDEIND 0.90%, TMPV 0.88%.

**Expected profit per trade (₹15K capital, avg stock ₹350, avg SL 1.5%):**
```
Full exit at R:R 1:1:     profit ≈ actual_risk ≈ ₹138  (not ₹150 — slippage buffer accounts for ~9%)
TP1 50% exit at R:R 0.7:  profit ≈ ₹138 × 0.7 × 0.5 ≈ ₹48  (partial, runner continues)
TP1 50% exit at R:R 0.5:  profit ≈ ₹138 × 0.5 × 0.5 ≈ ₹35  (partial, runner continues)
Blended (50-50 trail, TP1.5 hit 27.6% of trades): ≈₹61/winning trade
```
ORB signals typically have R:R 0.5–0.7 at TP1. Validated: TMPV ₹45, JSWENERGY ₹45 at TP1.
Full round-trip (TP1+runner) validated: TMPV ₹84.60, JSWENERGY ₹103.20.

### Slippage Factor vs Broker MPP

These serve different purposes and are NOT redundant:

- **MPP (Market Price Protection)** — Flattrade hard fill cap per leg: entry MARKET → LMT at LTP+1% (₹100–500 stocks). SL-M → SL-LMT at trigger+1%. Prevents catastrophic fills in illiquid spikes. Pure safety net.
- **`slippage_factor: 0.10`** — sizing buffer: widens denominator by 10% to account for entry filling above signal price on momentum breakout (typical: 0.3–0.8% above). Also covers TP exit slipping below TP price on MARKET order. Without this buffer, actual risk regularly runs 10–15% over 1%.

MPP caps the worst-case single fill. `slippage_factor` accounts for the typical gap that widens effective risk across both legs. Keep at 0.10 — validated Q2 2026 (actual risk 0.81–0.90% = ~9% haircut from the 10% buffer).

**`max_sl_pct_for_sizing` (default 0.0 = disabled):** Enabling this cap would inflate qty beyond
what the SL distance justifies, producing actual losses of 2–3× risk_per_trade when SL fires.
Keep at 0 to maintain strict ~0.91% risk per trade. Wide-SL days will have lower capital
utilisation — this is the mathematically correct consequence of consistent 1% risk management.

### pct_of_capital
```
qty = floor(capital × pct_of_capital / entry_price)
```

### Day-start capital (`use_day_start_capital: true`)
First capital fetch of each day is cached. All subsequent trades size off this value — equal risk per trade regardless of intraday P&L.

### Minimum capital floor (`min_capital_for_entry`)
Before sizing, live capital is checked against `sizing.min_capital_for_entry`. If below the floor, the entry is skipped cleanly — no order is sent to the broker. Prevents dwarf positions when open positions have consumed most of the margin.

### Margin check (Live only)
After sizing, `adjust_qty_for_margin()` checks whether the full-risk qty fits in live capital:

- **NSE/BSE equity** (SpanCalc API is derivatives-only): estimates margin as `qty × entry × mis_margin_pct`. **Binary reject** — if estimated margin > live capital, returns 0 (trade skipped). Never scales qty down; a scaled trade risks less than 1% and produces dwarf profits not worth commission cost.
- **Derivatives (NFO etc.)**: `fetch_margin()` → SpanCalc API for exact margin. If `actual_margin > live_capital`: `qty = floor(raw_qty × live_capital / actual_margin)`.

### Capital vs Concurrent Slots

**Q2 2026 observed margin per trade** (NSE MIS 20% on actual position values):
- Avg margin per trade: 6–8% of capital (wide-SL stocks like JSWENERGY: 4%, tight-SL: 9%)
- 5 concurrent slots = 30–40% margin utilisation — well within ₹15K+ available capital
- Original Q1 estimate of 31% margin per trade was based on avg_sl=0.64%; actual SLs vary 0.5–4%

`margin_per_trade ≈ qty × entry × 20%`  where `qty = floor(capital × 1% / (sl_distance × 1.10))`

**Sizing and profitability by capital level** (assumptions: avg stock ₹350, avg SL 1.5%, slippage_factor=0.10, 50-50 trail-to-TP1 strategy, ~3 trades/day, 70% win rate from Q1):

| Capital | Risk/trade | Actual risk (~91%) | Qty (avg) | Margin/trade (~9%) | Slots | TP1 partial profit | Blended $/win | Est. monthly net |
|---------|-----------|-------------------|-----------|-------------------|-------|--------------------|---------------|------------------|
| ₹15K    | ₹150      | ₹136              | ~25       | ₹788              | 5     | ₹44                | ₹61           | ₹3,200           |
| ₹25K    | ₹250      | ₹227              | ~43       | ₹1,330            | 5     | ₹74                | ₹102          | ₹5,400           |
| ₹35K    | ₹350      | ₹318              | ~60       | ₹1,860            | 5     | ₹103               | ₹142          | ₹7,500           |
| ₹50K    | ₹500      | ₹454              | ~86       | ₹2,660            | 5–6   | ₹148               | ₹204          | ₹10,800          |

Return % stays constant at ~21%/month — profit scales linearly with capital. The practical benefit of larger capital is fewer rejections from `min_capital_for_entry` floor and room to raise `max_open_positions` to 6–7 at ₹35K+.

### Multi-TP vs TP1-only: Strategy Decision

Q1 2026 net PnL comparison (196 trades, 31 days, ₹35K capital, Flattrade percentage fees):

| Strategy | Net PnL | Notes |
|---|---|---|
| TP1-only (100% exit) | +30.3% | Leaves 54 TP1.5 trades on the table |
| 50-50 trail to BE | +28.2% | BE stop-outs eat into gains |
| **50-50 trail to TP1** | **+42.7%** | **Strictly optimal** |

50-50 trail-to-TP1 dominates: identical outcome on SL trades and TP1-only trades, strictly better on the 27.6% of trades that reach TP1.5 (54 trades in Q1). Percentage fees (Flattrade) make partial exits free — no per-order cost penalty.

Note: earlier analysis showed "TP1-only +69R better" — that was under mStock's per-order brokerage model. Under percentage fees, 50-50 trail is always at least as good. **Current implementation is optimal.**

---

## Risk Management

All limits checked before each order. Counters persist across restarts via `risk_store.py`.  
On startup, `open_positions` is reconciled against actual broker positionbook.

| Limit | Config Key | Notes |
|-------|-----------|-------|
| Daily loss | `risk.daily_loss_limit` | Realised + unrealised loss |
| Weekly loss | `risk.weekly_loss_limit` | Mon–Sun |
| Monthly loss | `risk.monthly_loss_limit` | Calendar month |
| Max open positions | `risk.max_open_positions` | Slot-based (slots recycle) |
| Max trades/day | `risk.max_trades_per_day` | Total order count |
| Portfolio heat | `risk.max_portfolio_heat` | Sum of open risk % |
| Price filter | `sizing.min/max_entry_price` | Skip outside band |
| Min SL % | `risk.min_sl_pct` | Reject SL tighter than x% of entry |
| Min R:R | `risk.min_rr` | Skip signals below threshold |
| Symbol concentration | `risk.max_positions_per_symbol` | Per-symbol cap |
| Duplicate window | `risk.duplicate_window_seconds` | Dedup identical signals |
| Stale signal | `risk.stale_signal_seconds` | Reject old signals |
| Blacklist | `blacklist._global` / `blacklist.STRATEGY` | Per-strategy + global |

---

## Indian Broker OCO Constraint

**Problem:** Broker treats any SELL while a SL SELL is active as a new SHORT → `FUND LIMIT INSUFFICIENT`.

**Solution:** Only the SL-M order is placed at the broker. TP exit is driven by TradingView TP HIT signals → `_handle_exit`. No simultaneous SL + TP broker orders.

**Critical:** Before ANY exit order (full or partial) the SL must be cancelled first via `cancel_order()`.

### Flattrade MPP (Market Price Protection)
Flattrade blocks raw MARKET and SL-MKT order types via API.
- `MARKET` → `LMT` with price = LTP ± MPP% (slab: 2%/<100, 1%/100-500, 0.5%/>500)
- `SL-M` → `SL-LMT` with limit = trigger_price ± MPP% (keeps trigger untouched)
- Handled in `broker/flattrade/mapping/transform_data.py`

---

## Configuration (`config.yaml`)

All values required — `ConfigError` raised on any missing key.

### `telegram`
```yaml
telegram:
  channels:
    - name: "channel-label"
      id: -100XXXXXXXXX
  notify_channel:           # system alerts (startup/shutdown/errors)
    name: "admin"
    id: -100XXXXXXXXX
```

### `sizing`
| Key | Description |
|-----|-------------|
| `mode` | `fixed_fractional` or `pct_of_capital` |
| `risk_per_trade` | Fraction of capital to risk (fixed_fractional) |
| `pct_of_capital` | Fraction of capital per position (pct_of_capital) |
| `min_entry_price` | Skip stocks below this price |
| `max_entry_price` | Skip stocks above this price |
| `slippage_factor` | Widens SL distance before sizing |
| `max_sl_pct_for_sizing` | SL cap for qty calc (0=off). Wide-SL stock qty computed as if SL = entry × cap. Real SL order unchanged. |
| `sandbox_capital` | Capital override in analyze mode |
| `use_day_start_capital` | Cache first fetch of day for equal risk per trade |
| `test_qty_cap` | Max qty per order in `--test` mode (0 = disabled) |
| `min_capital_for_entry` | Skip new entries if live capital below this floor (INR) |

### `risk`
| Key | Description |
|-----|-------------|
| `daily/weekly/monthly_loss_limit` | Loss lockout thresholds (fraction of capital) |
| `max_portfolio_heat` | Max open risk fraction |
| `max_open_positions` | Concurrent slot cap |
| `max_trades_per_day` | Daily order cap |
| `min_rr` | Min reward:risk ratio |
| `duplicate_window_seconds` | Dedup window |
| `stale_signal_seconds` | Max signal age |
| `min_sl_pct` | Min SL distance (0 = disabled) |
| `max_positions_per_symbol` | Per-symbol cap (0 = disabled) |
| `max_positions_per_sector` | Per-sector cap (0 = disabled) |

### `bracket`
| Key | Description |
|-----|-------------|
| `enabled` | Place SL-M after entry fill |
| `cnc_sl_enabled` | false = skip bracket for CNC (NSE cancels overnight) |
| `sl_order_type` | `SL-M` (default) |
| `max_sl_retries` | Retry count for SL placement |
| `retry_delay` | Seconds between retries |
| `tp_exit_retries` | Retry count for TP MARKET exit |

### `strategy_profiles`
```yaml
strategy_profiles:
  ORB:
    product: MIS
    # tp_levels absent — ORB uses ExitQtyPct from signal directly
  RSI-TP-MR:
    product: MIS
    tp_levels:
      TP1: 1.0          # Exit 100% at TP1
```

### Other sections
```yaml
time_exit:
  enabled: true
  hour: 15            # IST 24h (15:00 = 10min buffer before broker square-off at 15:10)
  minute: 0

tracking:
  poll_interval: 10   # seconds between position polls

broker:
  exchange: NSE
  product: MIS                   # MIS or CNC
  order_type: MARKET
  allow_off_hours_testing: false  # MIS→CNC override in analyze mode (testing only)
  mis_margin_pct: 0.20           # Must match broker's actual NSE equity MIS margin %

api:
  timeout: 5.0
  margin_retries: 3
```

---

## Environment Variables (`signal_engine/.env`)

| Variable | Required | Notes |
|----------|----------|-------|
| `TELEGRAM_API_ID` | Yes | From my.telegram.org |
| `TELEGRAM_API_HASH` | Yes | From my.telegram.org |
| `TELEGRAM_PHONE` | Yes | Phone number for Telegram session |
| `OPENALGO_API_KEY` | Yes | From OpenAlgo `/apikey` page |
| `OPENALGO_BASE_URL` | Yes | Default: `http://127.0.0.1:5000` |
| `REDIRECT_URL` | Yes | Broker auto-detected from path (e.g. `/flattrade/callback`) |
| `BROKER_PASSWORD` | TOTP brokers | Plaintext, hashed before sending |
| `BROKER_TOTP_SECRET` | TOTP brokers | Base32 seed from authenticator app |
| `BROKER_API_KEY` | Yes | `CLIENT_ID:::API_KEY` (flattrade) or client ID (mstock) |
| `BROKER_API_SECRET` | Most brokers | Not required for mstock |
| `BROKER_NAME` | No | Overrides `REDIRECT_URL` auto-detection |

---

## Broker Setup

### Broker Detection Priority
1. `BROKER_NAME` env var (explicit)
2. First path segment of `REDIRECT_URL` (auto-detect)

### Flattrade (Headless TOTP)
```ini
REDIRECT_URL=http://127.0.0.1:5000/flattrade/callback
BROKER_API_KEY=FZ40074:::your_api_key    # CLIENT_ID:::API_KEY
BROKER_API_SECRET=your_api_secret
BROKER_PASSWORD=your_password
BROKER_TOTP_SECRET=BASE32SECRET
```
Auth flow: `POST /auth/session` → `POST /ftauth` (SHA256 pwd + TOTP) → `POST /trade/apitoken`

### mstock (TOTP)
```ini
REDIRECT_URL=http://127.0.0.1:5000/mstock/callback
BROKER_API_KEY=MA6718246                 # client code only
BROKER_PASSWORD=your_password
BROKER_TOTP_SECRET=BASE32SECRET
# BROKER_API_SECRET not required
```

### OAuth Brokers (e.g. Zerodha)
No programmatic login. Log in via OpenAlgo web UI first; scheduler reuses stored DB token.

### Switching Brokers
Update `REDIRECT_URL` + broker credentials in `.env`, then restart. TOTP brokers auto-replace token. OAuth brokers require a browser login first.

**Troubleshooting:**

| Error | Fix |
|-------|-----|
| `Cannot determine broker name` | Set `REDIRECT_URL` in `.env` |
| `ftauth HTTP 4xx` | Verify `BROKER_API_KEY=CLIENT_ID:::API_KEY` format |
| `Broker changed from X to Y` | TOTP: just restart. OAuth: browser login first |
| `stat=Not_Ok` / `emsg=...` | Wrong password or TOTP secret |

---

## Operating Modes

### Live
- Capital: broker funds API → `/api/v1/funds`
- Margin: validated via `/api/v1/margin` before each trade
- Orders: real broker

### Analyze (Sandbox)
- Capital: `sandbox_capital` from config
- Margin check: skipped
- Orders: OpenAlgo sandbox (virtual ₹1Cr capital)
- `allow_off_hours_testing: true` → MIS overridden to CNC for after-hours pipeline tests

---

## Startup & Shutdown

```bash
# Linux/WSL — full start (OpenAlgo + broker auth + signal engine)
./signal_engine/scripts/openalgoctl.sh run       # foreground
./signal_engine/scripts/openalgoctl.sh start     # background
./signal_engine/scripts/openalgoctl.sh stop
./signal_engine/scripts/openalgoctl.sh status

# Windows
.\signal_engine\scripts\openalgoctl.ps1 start

# Windows Task Scheduler (one-time setup)
.\signal_engine\scripts\createTaskOpenAlgoScheduler.ps1
# Creates: Start 8:50 AM weekdays, Stop 3:30 PM weekdays
```

### Startup Sequence
```
openalgoctl.sh run
  1. Start OpenAlgo (uv run app.py) — wait for HTTP readiness
  2. openalgoscheduler.py startup
     a. Auto-login to broker (TOTP or OAuth reuse)
     b. Verify token via broker funds API
  3. python -m signal_engine.main
     a. Run startup health checks (7 checks, ~5s) — abort if critical fail
     b. Reconcile open_positions against actual broker positions
     c. Restore risk counters from DB
     d. Start TimeExitScheduler + PositionTracker
     e. Send Telegram startup notification
     f. Start Telegram listener (blocking)
```

---

## Testing

```bash
# Full test suite (406 tests)
PYTHONPATH=. uv run pytest signal_engine/tests/ -v

# With coverage
PYTHONPATH=. uv run pytest signal_engine/tests/ --cov=signal_engine --cov-report=term-missing

# Pre-session health check (run before market open)
PYTHONPATH=. uv run python -m signal_engine.main --smoke-test

# Full dry run (smoke + synthetic order without placing)
PYTHONPATH=. uv run python -m signal_engine.main --dry-run

# Inject a test signal (live market, uses test_qty_cap)
PYTHONPATH=. uv run python -m signal_engine.main --test "ORB LONG
Symbol: SBIN
Entry: 800.00
SL: 793.00
TP: 815.00"

# From file
PYTHONPATH=. uv run python -m signal_engine.main --test --test-file signal_engine/tests/test_signal.txt
```

---

## Databases

| DB | Path | Table | Contents |
|----|------|-------|----------|
| Risk store | `signal_engine/data/risk.db` | `risk_counters` | Daily counters: trades, loss, open_positions, portfolio_heat — keyed (mode, date) |
| Trade audit | `signal_engine/data/trades.db` | `trades` | Every order: symbol, qty, entry/sl/tp, order_id, status, timestamps |

### Key SQL Commands

```bash
# Today's risk state
sqlite3 signal_engine/data/risk.db \
  "SELECT mode, trade_date, trades_today, daily_loss, open_positions, portfolio_heat FROM risk_counters WHERE trade_date=date('now');"

# Last 7 trading days
sqlite3 signal_engine/data/risk.db \
  "SELECT trade_date, trades_today, printf('%.2f',daily_loss) loss, open_positions, printf('%.2f',portfolio_heat) heat FROM risk_counters WHERE mode='live' ORDER BY trade_date DESC LIMIT 7;"

# Fix stale state after broker auto-squareoff
sqlite3 signal_engine/data/risk.db \
  "UPDATE risk_counters SET open_positions=0, portfolio_heat=0.0, daily_loss=0.0 WHERE mode='live' AND trade_date=date('now');"

# Today's trades
sqlite3 signal_engine/data/trades.db \
  "SELECT id, direction, symbol, quantity, order_id, status, substr(executed_at,12,8) time FROM trades WHERE executed_at >= date('now') ORDER BY executed_at;"

# Trades per day (last 14 days)
sqlite3 signal_engine/data/trades.db \
  "SELECT substr(executed_at,1,10) date, count(*) total, sum(status='SUCCESS') ok, sum(status='REJECTED') rej FROM trades WHERE executed_at >= date('now','-14 days') GROUP BY 1 ORDER BY 1 DESC;"
```
