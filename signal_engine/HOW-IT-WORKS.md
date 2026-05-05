# How the ORB Strategy Works — Plain-English Guide

Two things work together to place a live trade:

1. **PineScript on TradingView** — watches the chart, decides when to enter/exit, and sends an alert (a Telegram message).
2. **Signal Engine on the server** — reads the alert, sizes the trade, places the order with the broker, and tracks it.

Below: what each side does, step by step, in layman terms.

---

## Part A — The PineScript (`pinescripts/intraday/orb/orb.pine`)

This script runs on a 5-minute chart of one stock. Think of it as a watchman that only acts at four moments in the day.

### Step 1 — Build the "Opening Range" (09:15 → 09:30 IST)

For the first 15 minutes of the market, it just watches the price.
- **ORB High** = highest price during 09:15–09:30
- **ORB Low** = lowest price during 09:15–09:30
- **ORB Range** = High − Low
- **Mid** = (High + Low) / 2

These are the "fence" for the day. The strategy assumes price will pick a side and run.

### Step 2 — Wait for a Breakout (09:30 onwards, until ~11:00)

It watches every 5-minute candle and asks:

- **Is the candle closing ABOVE ORB High?** → "bullish breakout"
- **Is the candle closing BELOW ORB Low?** → "bearish breakout"

Before it acts on a breakout, it runs a few sanity checks (called *filters*):

| Filter | What it asks (in plain words) |
|---|---|
| **Volume filter** | Is volume on this candle higher than the recent average? (Real moves have volume.) |
| **Trend filter (EMA/VWAP/SuperTrend)** | Does the bigger trend agree with the breakout direction? |
| **HTF (higher timeframe) bias** | Is the daily/15-min chart pointing the same way? |
| **Index filter** | Is NIFTY moving in the same direction? |
| **Gap filter** | Did the stock open with a huge gap from yesterday? (Skip — too risky.) |
| **ORB-range filter** | Is the opening range neither too narrow nor too wide? |
| **NR (narrow-range) preference** | Was yesterday a narrow-range day? (NR days often produce clean breakouts.) |
| **Entry-cutoff hour** | Is the time before 11:00 IST? After 11:00, momentum dies — skip. |
| **Min-entry time** | Is it past 09:45? (First 15 min after ORB are gap-volatility chop — skip.) |
| **Session entry taken** | Have we already taken an entry today? (One trade per stock per day — skip.) |
| **Blacklist** | Is this stock on the "no-trade" list? (Underperformers from past data.) |

If all filters pass → **enter on the next candle's OPEN** (not the breakout candle, the one after — gives the breakout time to confirm).

### Step 3 — Decide SL and TP at Entry

For a **LONG** entry at price `E`:

- **SL (Stop Loss)** is computed using "Smart Adaptive" mode (`orb.pine:907`):
  - It looks at ATR (a volatility number) and the ORB Low.
  - SL = whichever is **further below entry**: `E − (ATR × multiplier)` or `ORBLow − 30% × ORBRange`.
  - Multiplier auto-adjusts to volatility (0.7× to 1.5× ATR).
  - There's also a wick buffer so a tiny dip below ORB Low doesn't kill the trade.

- **TPs (Targets)** are computed from R = risk distance = `|E − SL|` (`orb.pine:851`):
  - **TP1**  = E + 1.0R   ← *the only one that triggers a real exit*
  - **TP1.5** = E + 1.5R   ← shown on chart, used for partial-exit logic
  - **TP2**  = E + 2.0R   ← display only
  - **TP3**  = E + 3.0R   ← display only
  - For high-priced stocks (>₹1000) the R distance is shrunk slightly (0.6× to 0.8×) because absolute moves are smaller.

For a **SHORT** entry, the math mirrors (SL above entry, TPs below).

### Step 4 — Send the Entry Alert (Telegram)

It fires a webhook alert that looks like this:

```
🟢 ORB LONG | ASHOKLEY
Entry: 163.76 | Target: 168.83 | SL: 158.69
TP1.5: 171.37 | TP2: 173.9 | TP3: 178.97
Risk: 5.07 | Reward: 5.07 | R:R 1:1
09:50 IST
```

This goes to the **`intraday-orb` Telegram channel**.

### Step 5 — Watch for TP / SL / Time Exit

After entry the script watches every candle:

- **Price hits TP1?** → fires a `✅ ORB TP1 HIT` alert (asks engine to exit 50% of the position).
- **Price hits TP1.5/TP2/TP3?** → fires informational alerts (no real exit — the engine doesn't act on these by default; they're observation-only).
- **Price hits SL?** → fires a `❌ SL HIT` alert (so the engine can clean up its tracker — actual SL exit is done by the broker SL-M order).
- **Time exit (15:00 IST)?** → fires a time-exit alert.

That's the entire PineScript loop: build range → wait for breakout → filter → enter → alert TP/SL.

---

## Part B — The Signal Engine (`signal_engine/`)

The engine runs as a Python process on the server. It listens to the Telegram channel and turns alerts into real broker orders.

### Step 1 — Listen (`listener.py`)

Connects to Telegram, watches the `intraday-orb` channel, and grabs every new message.

### Step 2 — Parse (`parser.py`)

Reads the message text and extracts: strategy name (ORB), direction (LONG/SHORT/EXIT), symbol, entry, SL, TP, TP level (TP1/TP1.5/etc), exit-quantity-percent.

If the message doesn't look like a valid signal, it's ignored.

### Step 3 — Validate (`validator.py`)

Sanity checks on the parsed signal:
- Is the timestamp fresh (< 60s old)?
- Is the price within the configured min/max (₹150–₹800)?
- Is R:R ≥ 0.75?
- Is SL on the correct side of entry?
- Is the symbol allowed (not on the blacklist)?
- Is this a duplicate of a signal already processed in the last 60s?

If any check fails, the signal is dropped and logged.

### Step 4 — Risk Check (`risk.py`)

Before placing the order it asks:
- Are we already at max open positions (5)?
- Have we hit the daily loss limit?
- Have we hit the daily trade count limit (12)?
- Are we already holding a position in this symbol?
- Are we at the per-sector cap?

If any limit is breached → skip and notify Telegram.

### Step 5 — Size the Position (`risk.py` + `main.py:_handle_entry`)

This is where the rupee amount turns into a share count:

1. **Capital** is fetched from broker. The first fetch of the day is **cached** (`use_day_start_capital: true`) so every trade gets the same risk budget regardless of order in the day.
2. **Risk amount** = day-start capital × 1.5% (≈ ₹219 on ₹14,577 capital).
3. **Risk per share** = `|entry − SL|`, padded by a 10% slippage buffer.
4. **Quantity** = `floor(risk_amount / risk_per_share)`.
5. **Margin floor**: the engine then estimates if the broker will accept the order:
   - For NSE/BSE equity it uses a heuristic: `qty × entry × mis_margin_pct (0.20)`.
   - If the estimate is more than the live capital → **skip the trade** (don't scale down — a 1/3-size trade isn't worth the commission).

If the qty comes out to 0 (stock too expensive for the risk budget) → skip.

### Step 6 — Place the Entry Order (`executor.py` + `api_client.py`)

Sends a **MARKET BUY/SELL** order to OpenAlgo, which forwards it to the broker.

Why MARKET (not LIMIT)? Because breakouts run away — limit orders only fill 30–50% of the time and you miss the move.

### Step 7 — Place the Stop-Loss "Bracket Leg"

Right after the entry, the engine places a **separate SL-M (stop-market SELL)** order at the SL price. This sits in the broker's order book waiting to trigger.

**Why no broker-side TP order?** Indian brokers treat a second SELL as a **new short** and reject it ("FUND LIMIT INSUFFICIENT"). So TP exits are driven by a Telegram alert from PineScript instead.

### Step 8 — Track the Position (`tracker.py`)

Adds the position to an in-memory tracker and starts polling the broker every 5s:
- Did qty drop to 0? → trade closed (probably by SL hit). Compute PnL.
- Has the trade been open >90 min with <20% progress toward TP? → "no-progress exit" — close at market to free the slot.
- Is it 15:00 IST? → "time exit" — close all MIS positions before broker auto-square-off.

### Step 9 — Handle TP Alerts (`main.py:_handle_exit`)

When PineScript sends `TP1 HIT`:

1. Look up the position in the tracker.
2. **Cancel the SL-M order** (broker won't allow a SELL while SL is active).
3. Place a **MARKET SELL** for the partial qty (50% on TP1 by default).
4. **Re-place a new SL** for the remaining qty at `TP1 − 0.3R` (locks in some profit).
5. Wait for the next TP signal or the time exit.

### Step 10 — Handle SL Alerts

When PineScript sends `SL HIT`, the broker has already filled the SL-M order. The engine just:
1. Confirms with the broker.
2. Cleans up its tracker.
3. Records the loss.
4. Frees the position slot.

### Step 11 — Persist Everything (`db.py` → `data/trades.db`)

Every signal, order, and exit is stored in SQLite for audit and end-of-day reporting.

---

## End-to-end example (ASHOKLEY, today)

1. **09:15–09:30** — PineScript builds ORB on ASHOKLEY chart.
2. **09:50** — Price closes above ORB High with volume + trend OK.
3. **09:50 (next bar)** — PineScript fires `🟢 ORB LONG ASHOKLEY entry=163.76 SL=158.69 TP=168.83`.
4. **09:55:01** — Engine listener picks the message off Telegram.
5. **09:55:01** — Parsed → validated → risk check passes (1 of 5 slots).
6. **09:55:01** — Day-start capital cached at ₹14,577. Risk = ₹219. Risk/share = 5.07 + 10% buffer = 5.58. Qty = floor(219 / 5.58) = **39**.
7. **09:55:01** — MARKET BUY 39 ASHOKLEY → filled @ ₹163.43 (slippage −0.33).
8. **09:55:02** — SL-M SELL 39 @ trigger 158.69 placed and confirmed.
9. **09:55:02** — Position registered in tracker.
10. **11:25** — Tracker sees price drifted down to 162.53 with no progress for 90 min → fires no-progress exit.
11. **11:25:12** — Position closed @ 162.47. PnL = −₹37 (−0.2R).
12. Slot released. Engine waits for next signal.

That's the full loop.

---

## Quick reference

| Where | What |
|---|---|
| `orb.pine` | Decides entries, SL, TPs, sends alerts |
| `listener.py` | Reads Telegram |
| `parser.py` | Extracts fields from the message |
| `validator.py` | Sanity checks |
| `risk.py` | Position sizing + exposure limits |
| `main.py` | Orchestrates the whole pipeline |
| `executor.py` + `api_client.py` | Talks to OpenAlgo / broker |
| `tracker.py` | Polls broker, handles no-progress + time exits |
| `config.yaml` | All tunables (risk %, max positions, blacklist, etc.) |
| `data/trades.db` | Audit log |

---

## Known issues & follow-ups (2026-05-05)

These were surfaced from a forensic review of the multi-TP booking flow across 22 sessions (Apr 3 → May 4). The TP1-partial → TP1.5-close path is **logically correct** but only worked end-to-end in **5 of 16 candidate trades (31%)** in the historical sample. Tracking each fix below by priority so we can pick them up later.

### P0 — Concurrent TP alerts can place duplicate exit orders ⚠ correctness bug

**Symptom (2026-04-09 EXIDEIND)**: TradingView fired `TP1.5 HIT` and `TP1 HIT` in the same second on a fast move. Both Telethon tasks read `pos.quantity = 58` before either acquired the per-position `asyncio.Lock`. TP1.5 placed a 58-qty exit; TP1 then placed a 29-qty exit on a position that was already flat at the broker. Phantom 29-qty short SL was left in the order book.

```
09:55:07 EXIT: tp_level=TP1.5 exit_qty=58/58 full_exit=True
09:55:07 EXIT: tp_level=TP1   exit_qty=29/58 full_exit=False  ← read pos pre-unregister
09:55:07 EXIT order placed: qty=58 (TP1.5)
09:55:08 EXIT order placed: qty=29 (TP1)
09:55:08 SL moved to breakeven for EXIDEIND remaining 29 qty   ← phantom SL!
```

**Root cause**: the `_handle_exit` broker-fallback (`pos is None → fetch_open_position → reconstruct TrackedPosition`) cannot distinguish "position closed by sibling handler this second" from "engine restarted, tracker wiped". When a sibling TP handler unregisters mid-bar, the next-arriving handler falls back to the broker API, sees stale qty (broker has not yet processed our exit), and places a duplicate.

**Proposed fix**: gate the broker-fallback on a session-start timestamp on `PositionTracker`. The fallback only activates when `pos is None` AND the position was last seen registered before this session began. Same-session vanishing → "already closed by sibling, bail".

**Priority**: P0 (correctness — risk of phantom naked positions on fast moves).

---

### P1 — Trail SL at TP1 − 0.3R is too tight 📉 alpha loss

**Symptom**: 4 of 16 multi-TP candidates booked TP1 partial cleanly, but the `tp1_runner_sl_buffer = 0.3R` trail stop triggered before TP1.5 could fire. The runner exited at break-even or marginal profit instead of 1.5R.

| Date | Symbol | TP1 P&L | Runner outcome |
|---|---|---|---|
| 2026-04-16 | JSWENERGY | +₹69 | Trail SL hit 4 paise above trigger; runner break-even |
| 2026-04-21 | EMAMILTD | **−₹116** | Trail SL hit; partial leg net loss |
| 2026-04-28 | ONGC | +₹35 | Trail SL hit; runner zero |
| 2026-04-30 | NATIONALUM | +₹21 | Trail SL hit; runner +₹23 |

**Proposed fix**: widen `bracket.tp1_runner_sl_buffer` from 0.3 to 0.5 (= 50% of R). At avg SL=0.64% this gives the runner ~10–18 ticks of breathing room before the trail kicks in. Validate by replaying historical TP1 hits and counting trail-stop-outs at 0.3 vs 0.5.

Alternative: trail at **entry breakeven** instead of TP1-buffer. Locks in zero risk after TP1 but doesn't surrender any profit to the trail. Riskier on quick reversals.

**Priority**: P1 (alpha — recoverable lost profit, no correctness impact).

---

### P2 — Duplicate ExitQtyPct labels in `orb.pine` for partial vs full ⚠ minor

The PineScript currently sends `ExitQtyPct: 100` on both `TP1.5 HIT` and `TP2 HIT`. Either alert (whichever arrives first) closes the runner — the other becomes a no-op `EXIT ignored | no open position`. Confusing in logs but no correctness impact.

**Proposed fix**: only send the alert with `ExitQtyPct: 100` for the highest TP level the runner is configured to ride to. If we widen the trail later (P1), this becomes naturally consistent.

**Priority**: P2 (cleanliness).

---

### P3 — `no_progress` uses signal entry instead of fill price 📊 metric drift

**Symptom (2026-05-04 OIL)**: progress was logged as 19.6% (signal entry 481.65 → ltp 479.70) but actual fill-based progress was 13.0% (fill 480.90 → ltp 479.70). The log line read "progress=19.6% < 20% -> market-exit" — the trade was a hair under threshold using the signal-based metric and got correctly exited; using fill-price it would have been clearly under threshold (no marginality).

**Proposed fix**: switch progress-percent calculation in `tracker.check_positions` to use `pos.fill_price` when available, fall back to `pos.entry_price`. Keeps semantics aligned with what the trade actually risks.

**Priority**: P3 (telemetry accuracy, no behaviour change for clearly-progressed or clearly-stuck trades).

---

### Already shipped (2026-05-04)

- ✅ **Engine-restart tracker recovery** — `_handle_exit` fallback now reads original entry/SL/TP from `trades.db`; startup re-registers all open broker positions in the tracker. Eliminates the RBLBANK-style `new_sl=0.0` bug and the 7/16 historical "both legs missed" cases caused by restarts.
- ✅ **Pre-flight broker reject list** — `broker_restrictions.flattrade.mis_rejected` skips known-reject symbols before order placement. Saves slot + Telegram noise. List is populated as rejections are observed (see `FLATTRADE-RESTRICTIONS.md`).
- ✅ **Position concurrency** — `max_open_positions: 5 → 7`, `max_trades_per_day: 12 → 16`.

---

### Multi-TP success rate snapshot

| Outcome | Count (Apr 3 – May 4) | Mostly caused by |
|---|---|---|
| Clean two-leg (TP1 partial + TP1.5 close) | 5 | (the design path) |
| TP1 booked, runner trail-stopped before TP1.5 | 4 | P1 (trail too tight) |
| Both legs missed — "no open position" | 7 | Engine restart (now fixed) |
| Concurrent TP1 + TP1.5 race → phantom orders | 1 | P0 (correctness bug) |

Post-fix expectation (P0 + P1 + tracker recovery): success rate should move from 31% → 75–85%.
