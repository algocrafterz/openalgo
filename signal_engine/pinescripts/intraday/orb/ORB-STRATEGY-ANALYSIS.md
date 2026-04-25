# ORB Strategy Analysis - Indian Stock Market (NSE/BSE)

## Strategy Overview

**Name:** `orb-strategy-luxy-tg` (Opening Range Breakout - Luxy variant with Telegram alerts)
**Version:** Pine Script v6
**Based on:** Mark Fisher's Opening Range Breakout, enhanced by OrenLuxy
**Target Market:** Indian Stocks (NSE/BSE), configurable for other markets
**File:** `orb-strategy-india.pine` (~3757 lines)

---

## Architecture Summary

### Core Components

| Component | Lines | Description |
|-----------|-------|-------------|
| Type Definitions | 50-74 | `ORBData` UDT with 23 fields |
| Input Parameters | 76-498 | ~100+ configurable inputs across 15 groups |
| State Variables | 430-634 | ~150 session-persistent variables |
| Alert Functions | 647-761 | Telegram JSON-formatted alerts (Entry, SL, TP, Time Exit) |
| SL/TP Calculations | 773-976 | 7 stop-loss modes, multi-TP targets with position sizing |
| HTF Bias | 978-1011 | Higher timeframe filter (Price vs MA, Candle Direction) |
| Trend Filters | 1048-1066 | 6 trend modes (VWAP, EMA, SuperTrend, combos) |
| Session Management | 1068-1586 | Multi-market session handling, replay mode detection |
| ORB Building | 1650-1745 | Independent multi-stage ORB construction (5/15/30/60 min) |
| Breakout Detection | 1774-2210 | Breakout + retest entry with volume/trend/HTF filtering |
| Smart Entry | 2212-2296 | Retest/consolidation entry with timeout |
| Retest Detection | 2321-2425 | Failed break detection, cycle tracking |
| TP/SL Management | 2476-2688 | Hit tracking, time exit, EOD close |
| Dashboard | 2929-3671 | Full real-time table with tooltips |
| Test Alerts | 3690-3757 | Telegram webhook test system |

### Multi-Stage ORB Architecture

```
Session Start
    |
    v
[ORB5 Building] --5 min--> [ORB5 Complete] --becomes active-->
[ORB15 Building] --15 min--> [ORB15 Complete] --supersedes ORB5-->
[ORB30 Building] --30 min--> [ORB30 Complete] --supersedes ORB15-->
[ORB60 Building] --60 min--> [ORB60 Complete] --supersedes ORB30-->
    |
    v
Breakout Detection on Active ORB
    |
    v
Entry -> TP/SL Management -> Time Exit / EOD Close
```

Priority: ORB60 > ORB30 > ORB15 > ORB5 (largest completed ORB becomes active)

---

## Entry Conditions

### Pre-conditions (must ALL pass before breakout detection)

| Condition | Description | Dashboard indicator |
|-----------|-------------|-------------------|
| **Gap filter** | Opening gap from previous close must be <= threshold | Gap: X% |
| **ORB range filter** | ORB width as % of price must be within min/max range | ORB Width: X% |
| **Minimum entry time** | Optional delay (e.g., after 9:45 AM) to let gap volatility settle | — |
| **Entry cutoff** | No new entries after 10:30 AM (configurable) | — |
| **One entry per session** | Only 1 trade per stock per day | — |

### Breakout Detection (must ALL pass simultaneously)

| Condition | Description |
|-----------|-------------|
| **Price cross** | Close crosses above ORB High + buffer (long) or below ORB Low - buffer (short) |
| **Volume** | Max volume of last 3 bars >= volume MA(50) * multiplier (e.g., 1.2x). Uses 3-bar window to capture pre-breakout volume surges. |
| **Directional filters (2-of-3 rule)** | At most 1 of the 3 filters below can oppose the trade direction. If 2 or more oppose, entry is **blocked**. |

### Directional Filter Logic (2-of-3 Rule)

Three filters assess whether the broader market agrees with the breakout direction:

| Filter | What it measures | Bullish signal | Bearish signal |
|--------|-----------------|---------------|---------------|
| **Trend** | Intraday direction (VWAP/EMA/SuperTrend) | Price above indicator | Price below indicator |
| **HTF Bias** | Daily timeframe trend | Higher TF bullish | Higher TF bearish |
| **Index** | NIFTY/BANKNIFTY direction | Index above VWAP/EMA | Index below VWAP/EMA |

**How it works:** For each trade direction, count how many filters oppose it. If 2+ oppose → blocked.

**Example scenarios for LONG entry:**

| Trend | HTF Bias | Index | Filters against | Result |
|-------|----------|-------|----------------|--------|
| Bullish | Bullish | Bullish | 0 | ENTRY |
| Bullish | Bullish | Bearish | 1 | ENTRY |
| Bullish | Bearish | Bullish | 1 | ENTRY |
| Bearish | Bullish | Bullish | 1 | ENTRY |
| Bullish | Bearish | Bearish | 2 | BLOCKED |
| Bearish | Bullish | Bearish | 2 | BLOCKED |
| Bearish | Bearish | Bullish | 2 | BLOCKED |
| Bearish | Bearish | Bearish | 3 | BLOCKED |

*(Same logic applies in reverse for SHORT entries)*

### Entry Execution Flow

1. Breakout detected + all filters pass → **pending entry** created
2. Optional: if retest entry enabled and volume is not strong, waits for pullback/consolidation
3. Entry executes on **next bar** after breakout (avoids same-bar repainting)
4. If cutoff time passes while entry is pending → pending entry **cancelled**

### Unified Entry Filters Dashboard

Single section shown in ALL scenarios (live, no-trade, trade-taken). Replaces the previously separate Smart Filters, Volume, Trend, HTF, Index, and ORB Status sections.

**Three display modes:**

| Mode | Header | When | ✅/❌ | Reason line |
|------|--------|------|-------|-------------|
| **Live** (mode 0) | `── Filters` | Before cutoff, no trade | Gap/Width only | No |
| **No Trade** (mode 1) | `── No Trade` | After cutoff, no trade | All filters | Yes |
| **Trade Taken** (mode 2) | `── Entry Filters` | Trade was taken | All filters (all ✅) | No |

**Filter rows:**

| Row | What it shows |
|-----|---------------|
| **Gap:** | ✅/❌ + gap % |
| **Width:** | ✅/❌ + ORB range % |
| **Cross:** | ✅/❌ + direction (Up/Down/None) |
| **Volume:** | ✅/❌ + ratio + quality (Strong/Good/Weak). Only shown if price crossed |
| **Trend:** | ✅/❌ + bias (Bullish/Bearish). Only shown if price crossed (mode > 0) |
| **HTF:** | ✅/❌ + bias (Bullish/Strong Bearish etc.). Only shown if price crossed (mode > 0) |
| **Index:** | ✅/❌ + bias (Bullish/Bearish). Only shown if price crossed (mode > 0) |
| **Cutoff:** | ❌ shown only if breakout after cutoff (mode 1 only) |
| **Reason:** | Single line explaining why no trade (mode 1 only) |

**"→" arrow for value drift:** When reviewing after hours, directional filters (Volume, Trend, HTF, Index) show attempt-time value + current value if changed: e.g., `✅ Bullish → Bearish`. This avoids confusion when the index was bullish at 10AM (when the cross happened) but bearish by 3PM (when you review the chart).

**Key design decisions:**
- All values captured AT breakout attempt time, not current bar
- ✅/❌ based on alignment with attempted direction at attempt time
- Arrow only appears when qualitative value changed (not minor ratio fluctuations)
- Position sizing section follows immediately after filters for trade-taken mode

---

## Exit Conditions

### Strategy Exit (actual position closure)

| Exit Type | When | Execution |
|-----------|------|-----------|
| **TP1 (1R)** | Price reaches 1x risk distance from entry | `strategy.exit` with limit order — **full position close** |
| **Stop Loss** | Price hits SL level | `strategy.exit` with stop order — **full position close** |
| **Time Exit (3:00 PM)** | Position still open at configured time | `strategy.close_all` — **full position close** |

Only ONE of these fires per trade — whichever is hit first.

### Observation Alerts (Telegram only, no position change)

| Event | When | Purpose |
|-------|------|---------|
| **TP1 hit** | Price reaches 1R target | Confirms execution target reached |
| **TP1.5 hit** | Price reaches 1.5R target | Data collection for future optimization |
| **TP2 hit** | Price reaches 2R target | Data collection for future optimization |
| **TP3 hit** | Price reaches 3R target | Data collection for future optimization |
| **Time Exit** | 3:00 PM if position still open | Signal engine handles actual exit independently |

### All Telegram Alerts

| Alert | Triggers execution? | Message function |
|-------|-------------------|-----------------|
| Entry | Yes — signal engine places order with TP1 | `buildEntryAlert` (TP1 hardcoded) |
| SL Hit | Yes — signal engine closes position | `buildSLAlert` |
| TP1 Hit | Observation only | `buildTPAlert(..., "TP1")` |
| TP1.5 Hit | Observation only | `buildTPAlert(..., "TP1.5")` |
| TP2 Hit | Observation only | `buildTPAlert(..., "TP2")` |
| TP3 Hit | Observation only | `buildTPAlert(..., "TP3")` |
| Time Exit | Observation only | `buildTimeExitAlert` |

### Key Design Decisions (as of 2026-03-20)

- **Execution**: `strategy.exit` always uses TP1 only (full position exit at 1R)
- **Observation**: All TP levels (TP1.5, TP2, TP3) enabled by default for chart display
- **Entry alert**: Hardcoded to TP1 — signal engine uses TP1 for order placement
- **Entry cutoff**: 11:00 AM (relaxed from 10:30 to capture late valid breakouts; 10:30 remains the sweet spot per Q1 data but 10:30–11:00 window added)
- **Time exit**: 3:00 PM — gives trades 4.5 hours from entry, exits before broker auto square-off
- **strategy.close_all retained**: For accurate backtest simulation in Strategy Tester

---

## Stop Loss Modes Analysis

| Mode | Best For | Mechanism |
|------|----------|-----------|
| **ATR** | Standard volatility-adjusted | `entry - ATR * multiplier * priceAdjust` |
| **ORB %** | Range-proportional | `orbLow - (orbRange * fraction)` |
| **Swing** | Support/resistance-based | `ta.lowest(low, swingBars)` |
| **Safer** | Maximum protection | Most conservative of ATR, Swing, ORB |
| **% Based** | Simple percentage | `entry * (1 - pct/100)` |
| **Smart Adaptive** | Volatility-aware | ATR-scaled with ORB fallback |
| **Scaled ATR** | Tiered volatility | ATR multiplier scales with ATR% of price |

All modes enforce a **minimum stop distance** to prevent unrealistically tight stops.

---

## Strengths (Pros)

### 1. Comprehensive Multi-Stage ORB
- 4 independent ORB timeframes (5/15/30/60 min) building simultaneously
- Progressive range widening captures stronger levels
- Automatic supersession (largest completed ORB takes priority)

### 2. Robust Filter Stack
- Volume confirmation prevents low-conviction entries
- "Both Against" blocking is smarter than single-filter blocking (reduces missed trades while maintaining safety)
- HTF bias adds institutional-level directional alignment
- Gap filter avoids tricky gap-day openings common in Indian markets

### 3. Smart Entry Mechanisms
- **Retest entry** avoids chasing breakouts, gets better fills
- **Consolidation entry** detects strength-holding patterns near ORB levels
- **Entry cutoff** prevents late-day entries with insufficient time for targets
- **Minimum entry time** lets opening volatility settle (critical for NSE at 9:15 AM)

### 4. Advanced Risk Management
- 7 stop-loss calculation modes
- Position sizing with risk-per-trade control
- Adaptive R:R based on market regime (trending vs ranging)
- Multi-TP partial exit preserves upside
- Time exit + EOD close ensures no overnight exposure

### 5. Production-Ready Features
- Telegram alerts with formatted JSON messages including R:R info
- Real-time dashboard with session statistics
- Failed breakout detection and labeling
- Replay mode detection for backtesting accuracy

### 6. Indian Market Adaptations
- IST timezone formatting
- INR currency default
- Session hours aligned with NSE (9:15-15:30)
- Commission set to 0.03% (typical for Indian discount brokers)

---

## Weaknesses (Cons)

### 1. Complexity Overhead
- **3757 lines** is very large for a Pine Script strategy
- ~150 state variables increase bug surface area
- Dashboard alone is ~750 lines (20% of codebase)
- Pine Script's execution model (bar-by-bar) makes debugging complex state transitions difficult

### 2. Single Entry Per Session
- `sessionEntryTaken` flag limits to ONE trade per session
- Misses valid second opportunities (e.g., false breakout up followed by genuine breakdown)
- No re-entry after SL hit (breakout flag resets, but sessionEntryTaken blocks new entry)

### 3. Target Calculation Limitations
- Targets use `math.min(ORB-width-based, Risk-based)` - always picks the **closer** target
- `riskAdjustment` (1.0/0.8/0.6 based on price level) reduces targets for higher-priced stocks without market-specific justification
- No consideration of key support/resistance levels, pivot points, or VWAP bands

### 4. No Trailing Stop
- Once SL is set, it remains static
- No breakeven move after TP1 hit
- No trailing mechanism to lock in gains on runners
- Extended TP mode (50/50 split) still uses original SL for the runner portion

### 5. Backtesting Limitations
- `strategy.entry` uses market orders at `open` of next bar - slippage in live trading will differ
- `calc_on_every_tick=false` means intra-bar SL/TP hits may not be accurately simulated
- Commission at 0.03% doesn't include STT, GST, stamp duty, SEBI charges (total Indian costs ~0.1-0.15%)

### 6. Indian Market Specific Gaps
- No handling of **pre-open session** (9:00-9:15 AM NSE) which sets opening price via auction
- No consideration of **circuit limits** (upper/lower circuit stocks can't be shorted or exited)
- No **F&O lot size** awareness for futures/options traders
- Short selling constraints in Indian cash market (BTST/STBT) not addressed

### 7. Volume Filter — Gap-Day Distortion (Mitigated)
- Volume at open (first 5-15 min) is typically 5-10x average in Indian markets
- **Fixed (2026-03-24):** Volume MA length increased from 20 to 50 bars
- With 20-bar MA, gap-day opening candles inflated the average, causing valid breakouts to fail the 1.2x check (showed "No breakout" despite price clearly breaking ORB level with all other filters passing)
- With 50-bar MA, includes previous session data as baseline, so 1.2x threshold is consistent across gap and non-gap days
- 1.2x multiplier retained as quality threshold

### 8. No Market Breadth / Index Confirmation
- Trades individual stocks without checking NIFTY/BANKNIFTY direction
- In Indian markets, 70%+ of stock movement correlates with index direction
- A Nifty filter would significantly reduce whipsaw entries

---

## Value Improvement Recommendations

### Priority 1: High Impact, Moderate Effort

#### 1.1 Trailing Stop After TP1 — ✅ DONE (2026-03-27)
Implemented as **runner SL buffer** rather than full ATR trail:
- After TP1 partial exit (50% of position), remaining runner SL is moved to `TP1 - 0.3R` (longs) / `TP1 + 0.3R` (shorts)
- This locks in ~0.35R profit on the runner even if stopped before TP1.5
- Signal engine: `config.yaml:227` — `tp1_runner_sl_buffer: 0.3`
- Chosen over ATR-trailing because it's simpler, avoids wick-back stop-outs (TMPV 2026-04-13 incident), and works without tick-level data.

#### 1.2 Multi-Entry Per Session — ❌ Open
Still blocked by `sessionEntryTaken` flag in `orb.pine:506`. Per-symbol re-entry after SL is not enabled. `max_trades_per_day: 12` caps portfolio-wide, not per-symbol.
**Next step:** Allow reverse-direction re-entry after failed breakout with a cooldown window.

#### 1.3 Index Correlation Filter (NIFTY/BANKNIFTY) — ✅ DONE (2026-03-12)
NSE:NIFTY direction integrated into the 2-of-3 filter stack (Trend + HTF + Index). See [orb.pine:323](orb.pine) and the "2-of-3 Rule" table in Entry Conditions above.

#### 1.4 Realistic Indian Market Costs — ✅ DONE (2026-03-12)
mStock pricing model applied — `commission_value=0.06`, `slippage=2`. See "Changes Implemented (2026-03-12)" section below for full cost breakdown.

### Priority 2: Medium Impact, Lower Effort

#### 2.1 Pre-Open Session Awareness
```
- Detect 9:00-9:15 AM IST (NSE pre-open auction)
- Use pre-open price as "expected open" for gap calculation
- Avoid placing ORB levels from auction-period candles
```

#### 2.2 VWAP-Anchored Targets
Instead of fixed R:R targets, blend with VWAP standard deviation bands:
```
- TP1 = min(1R target, VWAP + 1 SD)
- TP2 = min(2R target, VWAP + 2 SD)
- Provides market-structure-aware exits
```

#### 2.3 Time-Decay Exit
Add a time-based profit-taking rule:
```
After 2 hours in trade:
- If P&L < 0.5R: Exit (trade didn't develop)
- If P&L > 0.5R but < 1R: Tighten SL to 50% of original distance
```
**Rationale:** ORB moves tend to happen within 1-2 hours. Extended holding without progress suggests the trade thesis has weakened.

#### 2.4 ORB Width Classification
Dynamically adjust strategy parameters based on ORB width:
```
Narrow ORB (<0.5% of price): Expect wider move, use TP2 primary
Normal ORB (0.5-1.5%): Standard parameters
Wide ORB (>1.5%): Expect reversion, use TP1 only, tighter SL
```

### Priority 3: Nice to Have

#### 3.1 Session Statistics Export
Add alert messages for end-of-day summary:
```
{session_wins}W / {session_losses}L | Total: {session_total_rr}R | Win Rate: {win_rate}%
```
Useful for trade journal automation.

#### 3.2 Previous Day ORB Reference
Show previous day's ORB high/low as reference levels:
```
- Yesterday's ORB high/low often acts as support/resistance
- Can be used as additional confirmation
```

#### 3.3 Multi-Symbol Screener Alert
At ORB completion, send a "watchlist" alert listing all symbols where ORB range is within optimal parameters, before breakouts happen.

---

## Configuration Recommendations for Indian Markets

### Recommended ORB Configuration (2026-04-25)

These values reflect current production configuration and explicitly address the shortcomings observed in Q2 2026 (lower trade density, regime-flipped Q1 winners, premature no-progress exits).

#### PineScript inputs (`orb.pine`) — TradingView side

| Parameter | Recommended | Addresses |
|---|---|---|
| `enableORB15Signals` | `true` (only) | ORB5 too noisy on NSE (auction settlement), ORB30/60 too late |
| Session | `0915-1530:23456` IST | NSE regular hours, Mon–Fri |
| `enableMinEntryTime` / hh:mm | `true` / 09:45 | Skip first 30 min volatility |
| `entryCutoffHour` / minute | 11 / 00 | Q1 data: post-11:00 entries underperform; 10:45–11:00 still catches valid late breakouts |
| `timeExitHour` / minute | 15 / 00 | 10-min buffer before broker auto square-off at 15:10 |
| `stopMode` | `Smart Adaptive` or `Scaled ATR` | NSE volatility varies — fixed % too tight on banks, too loose on power |
| `enableVolumeFilter` / `volumeMaLength` / `volumeMultiplier` / `strongVolumeMultiplier` | `true` / 50 / 1.2 / 1.8 | MA(50) survives gap-day distortion; 1.2× = quality threshold; 1.8× skips retest wait |
| `enableTrendFilter` / `trendMode` | `true` / `VWAP` | Most-watched intraday institutional reference on NSE |
| `enableHTF` / `htfTF` / `htfMethod` / `htfEMA` | `true` / `D` / `Price vs MA` / 20 | Daily 20 EMA = the most-followed institutional bias line |
| `blockCounterTrend` | `true` | Counter-HTF entries showed disproportionate Q1 losses |
| `enableIndexFilter` / `indexSymbol` / `indexFilterMethod` | `true` / `NSE:NIFTY` / `Price vs VWAP` | 60–80% of stock movement correlates with NIFTY intraday |
| `enableGapFilter` / `maxGapPercent` | `true` / 2.5 | >2.5% gaps tend to fade; sweet spot ±0.5–2.0% (now caught by Scanner 3) |
| `enableORBRangeFilter` / `min` / `max` | `true` / 0.4 / 3.5 | Filters dead stocks and over-volatile ones |
| `tp1ExitQtyPct` / `tp1_5ExitQtyPct` | 50 / 100 | 72.3% of TP1 trades reach TP1.5 — partial exit captures both wins |
| `enableNRFilter` / `nrMode` / `nrLookback` | `true` / `Prefer` / 7 | **NEW** — Prefer mode is a no-op for trade execution but tracks NR-day state for A/B uplift measurement. Once 20-day data confirms uplift, flip to `Require`. |
| `riskPct` (Pine UI display) | 1.0 | Conservative for Indian volatility |

#### Signal engine (`signal_engine/config.yaml`) — execution side

| Section | Key | Recommended | Addresses shortcoming |
|---|---|---|---|
| sizing | `risk_per_trade` | 0.01 (1%) | Capital preservation |
| sizing | `slippage_factor` | 0.10 | Both entry + TP exit are MARKET orders; double slippage |
| sizing | `min_entry_price` / `max_entry_price` | 150 / 800 | Matches Chartink Scanner 1 universe |
| sizing | `use_day_start_capital` | true | Equal risk across all trades regardless of margin held |
| risk | `daily_loss_limit` | 0.04 | Stops after 4 full-risk losers in a day |
| risk | `weekly_loss_limit` / `monthly_loss_limit` | 0.08 / 0.10 | Bad-week / bad-month protection |
| risk | `max_open_positions` | 2 (₹15K MIS) → 3–4 (₹25K+) | Capital-tier dependent — see PRD |
| risk | `max_trades_per_day` | 12 | Caps portfolio-wide daily trades; recent days only hit 4 anyway |
| risk | `min_rr` | 1.0 | TP1 = 1R |
| risk | `min_sl_pct` | 0.005 (0.5%) | Filters noise; SL below this is inside bid-ask spread |
| risk | `max_positions_per_symbol` | 1 | One concurrent position per symbol |
| no_progress | `enabled` / `check_after_minutes` / `min_progress_pct` | true / 90 / 0.20 | **TUNED 2026-04-25** — 60/0.33 was cutting winners 1–2% short of TP1 |
| no_progress | `ab_test_disable` | false | Flip to true for a 10-day control comparison; default false in production |
| bracket | `tp1_runner_sl_buffer` | 0.3 | Locks ~0.35R profit on the runner (replaces full trailing stop) |
| blacklist | `ORB.hard` | (12 D-grade symbols) | Full block — see config.yaml |
| blacklist | `ORB.soft` + `soft_multiplier` | (4 regime-flipped) + 0.5 | **NEW 2026-04-25** — half-size keeps participation while limiting downside |
| time_exit | `hour` / `minute` | 15 / 0 | 10-min broker buffer |

#### How each shortcoming is addressed

| Shortcoming | Mitigation now in place |
|---|---|
| **Trade density dropped (4.7→2.2/day)** | (a) Setup Scanner widened to Nifty 500, (b) regime-flipped stocks moved hard→soft (still trade at 50% qty), (c) Pre-market Gap Scanner adds today's movers |
| **Premature no-progress exits at 31.x%** | Loosened to 90min / 20% — gives genuinely-stuck trades wider lane to resolve |
| **Q1 A-graders flipped negative in Q2 (regime change)** | Soft blacklist with 50% qty preserves optionality if regime rotates back |
| **Banks/IT sector weakness** | (a) Soft-blacklist for the affected symbols, (b) Sector-rotation overlay in Scanner 1 post-processing — manually deprioritise bottom-3 sectors by 5-day return |
| **Static watchlist misses today's movers** | Pre-market Gap Scanner at 9:10 AM — appends top 10 gap movers, removed at EOD |
| **No statistical edge boost beyond 67% baseline WR** | NR Filter (Prefer mode) — collects data on NR-day uplift; promote to Require once 20-day data confirms ≥3pp uplift |
| **Friday WR weak (55.2%)** | NOT addressed in code — operational discipline only: reduce position size 50% on Fridays via `test_qty_cap` toggle, or skip Friday signals manually |
| **Per-symbol re-entry blocked after SL** | NOT addressed in code — `sessionEntryTaken` flag still blocks. Operational workaround: monitor for failed-breakout reversals manually; do not implement until impact data justifies the complexity |

### Best Suited Instruments

| Instrument | Suitability | Notes |
|------------|-------------|-------|
| NIFTY 50 Index Futures | Excellent | High liquidity, tight spreads, clear ORB levels |
| BANKNIFTY Futures | Good | Higher volatility, use wider ORB (30 min) |
| Large-cap stocks (RELIANCE, TCS, HDFC) | Good | Sufficient liquidity, respects ORB levels |
| Mid-cap stocks | Fair | May have gaps in order book, wider slippage |
| Small-cap stocks | Poor | Low liquidity, prone to manipulation, wide spreads |

---

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| False breakouts on choppy days | HIGH | Gap filter + ORB range filter + volume confirmation |
| Late entries miss the move | MEDIUM | Retest entry mode, minimum entry time |
| SL too tight on volatile stocks | MEDIUM | Scaled ATR or Smart Adaptive stop mode |
| Single entry misses reversals | MEDIUM | Consider multi-entry enhancement |
| Backtesting overestimates PnL | HIGH | Update to realistic Indian market costs |
| Circuit limit stocks | LOW | Only trade high-liquidity instruments |

---

## Changes Implemented (2026-03-12)

### Phase 1: Realistic Commission Model (mStock Pricing)

**Source:** [mStock Pricing](https://www.mstock.com/pricing)

| Charge | Rate |
|--------|------|
| Brokerage | Rs 5 flat per order |
| STT | 0.025% (sell side only, intraday) |
| Exchange (NSE) | 0.00297% |
| SEBI | Rs 10 per crore |
| Stamp Duty | 0.003% (buy side) |
| GST | 18% on brokerage + exchange + SEBI |

**Effective round-trip cost: ~0.06%** for typical Rs 50K-5L intraday positions.

Changed: `commission_value=0.03` -> `0.06`, `slippage=1` -> `2`

**Impact on backtesting:** Net profit will decrease by approximately 0.03% per trade (correcting for previously understated costs). For a strategy doing 1 trade/day across 250 trading days, this is ~7.5% annual drag. Makes backtesting results more reliable and avoids overestimating profitability.

### Phase 2: NIFTY Index Direction Filter

Added configurable index filter (default: `NSE:NIFTY` with Price vs VWAP method).

**Blocking logic upgraded from "both against" (2 filters) to "2-of-3 against":**
- Filter 1: Intraday Trend (VWAP/EMA/SuperTrend)
- Filter 2: HTF Daily Bias
- Filter 3: NIFTY Index Direction (NEW)

Entry blocked when **2 or more** of these filters oppose the direction.

**Impact analysis:**

| Metric | Before (2 filters) | After (3 filters, block on 2+) |
|--------|--------------------|---------------------------------|
| Entry restriction | Blocks only when BOTH oppose | Blocks when ANY 2 of 3 oppose |
| False signal reduction | ~15-20% | ~25-35% estimated |
| Missed valid trades | Very few | Slightly more on mixed-signal days |
| Best improvement | N/A | Choppy/ranging days where NIFTY is directionless |

**Does it over-restrict?** No, because:
1. With 3 filters and a "2+ against" threshold, you still get entries when only 1 filter opposes
2. On strong trending days (all 3 aligned), zero restriction
3. On mixed days (1 against), still allows entry
4. Only blocks on genuinely conflicting signals (2+ against)
5. Previous "both against" with 2 filters was actually stricter per-filter (needed both to agree)

**Why NIFTY 50 (not sector-specific):**
- 60-80% intraday correlation with individual stocks regardless of sector
- User's stock list spans 8+ sectors - sector mapping would be complex and fragile
- One `request.security()` call vs 8+ for sector indices
- NIFTY data is highest quality on TradingView

---

## Signal Performance Summary (Q1 2026)

See [SIGNAL-PERFORMANCE-2026-Q1.md](SIGNAL-PERFORMANCE-2026-Q1.md) for full analysis.

**Key metrics (31 trading days, 196 trades):**
- Win Rate: 67.3% | Expectancy: +0.28%/trade | Cumulative PnL: +54.89%
- Shorts outperform: 72.4% WR vs Longs 63.3%
- Fridays underperform: 55.2% WR vs 70%+ other days
- Best performers (Grade A): NATIONALUM, APOLLOTYRE, PFC, CANBK, TMPV, SBIN, RECLTD, ASHOKLEY, PNB, ZYDUSLIFE, LICHSGFIN, EXIDEIND, HINDALCO
- Remove: BHEL (0% WR), MANAPPURAM (17% WR)

## Stock Selection Methodology

**Monthly review process:**
1. Run signal analysis on last 20 trading days
2. Grade each stock (A/B/C/D based on WR% and PnL)
3. Keep Grade A+B, watch Grade C, remove Grade D after 2 months
4. Add candidates from F&O stock list with: vol > 50L shares/day, ATR% 1.5-3.5%, trending sector

**Sector rotation awareness:**
- Risk-on rally: Metals, Banks, Power (high beta, strong ORB moves)
- Defensive rotation: FMCG, Pharma, IT (cleaner but smaller moves)
- Rate cut cycle: Banks, Realty, NBFCs
- Commodity boom: Metals, Oil & Gas

## Summary

The ORB strategy is a **well-engineered, feature-rich** implementation with strong foundations for Indian intraday trading. Its multi-stage ORB architecture, comprehensive filter stack, and smart entry mechanisms make it significantly more sophisticated than typical ORB scripts.

**Key strengths:** Multi-stage ORB, "2-of-3 against" filter logic (Trend + HTF + Index), retest entry, runner SL at TP1−0.3R (locks in profit), daily/weekly/monthly loss limits, Telegram integration.

**Completed since initial analysis:**
- ✅ Runner SL at TP1−0.3R (`tp1_runner_sl_buffer: 0.3`) — replaces full trailing stop
- ✅ Daily loss limit (`daily_loss_limit: 0.04` + weekly/monthly) — enforced in `risk.py`
- ✅ Index correlation filter (NIFTY via 2-of-3 rule)
- ✅ Realistic Indian market costs (mStock model, commission=0.06, slippage=2)
- ✅ Volume MA 50 (was 20) — fixes gap-day distortion
- ✅ `max_trades_per_day: 12` portfolio cap
- ✅ TP1/TP1.5/TP2/TP3 observation alerts for ongoing data collection

**Remaining improvements:**
1. **Friday position sizing / filter** — Fri 55.2% WR vs 70%+ other days; still manual discipline only
2. **Multi-entry per session (reversal re-entry)** — `sessionEntryTaken` still blocks per-symbol re-entry after SL
3. **Grade-based position sizing** (A=100%, B=75%, C=50%) — still manual discipline only

**Operational recommendations (manual discipline):**
- Grade-based position sizing: A=100%, B=75%, C=50%
- Tighter filters on Fridays (or reduced position size)
- Monthly stock review using Grade system

The strategy is best suited for **liquid large-cap stocks and index futures** on 5-minute charts with 15-30 minute ORB periods.

---

## Chartink Scanners

### What is Chartink (in plain English)

**Chartink** is a free Indian stock screening website — https://chartink.com — that lets you write SQL-like queries against live and historical NSE/BSE data and get a list of stocks matching your conditions. Think of it as "Google for stocks where I get to define the search."

It is **not** a community plugin or a TradingView product — it is its own standalone web tool, owned and operated by Chartink. The "community" aspect is that users publish their custom screens publicly at https://chartink.com/screeners — anyone can browse and copy them. The four scanners in this document are **custom queries we wrote for the ORB strategy**, not borrowed community screens. You use them by:

1. Sign up for a free Chartink account at https://chartink.com
2. Click "Screener" in the top nav → "Create Screener"
3. Paste one of the queries below into the text area
4. Click "Run Scan"
5. Chartink returns the matching stocks in seconds

You can save each query under your account, then run any saved scan in one click. Free tier is enough for everything in this document — paid tier mainly buys faster intra-bar updates and email alerts (we don't need either).

**Free tier caveat:** intraday data is delayed 15 minutes. So Scanner 2 and Scanner 3 are for *audit and discovery*, not real-time execution. Your actual trade signals come from TradingView (real-time) → Telegram → signal engine. The scanners feed your TradingView **watchlist** so TradingView is watching the right stocks in the first place.

### How to use the four scanners (workflow)

| When | Run | What you do with the result |
|---|---|---|
| 3:25 PM previous day | Scanner 4 (NR7) | Mark these symbols `[NR7]` in TradingView — they are tomorrow's premier candidates |
| 3:30–4:00 PM previous day | Scanner 1 (Setup, Nifty 500) | Build tomorrow's watchlist. Drop hard-blacklisted symbols. Tag soft-blacklisted with `[soft]`. Merge with NR7 stars. |
| 9:00–9:10 AM market day | Scanner 1 again | Catches overnight movers that were not on yesterday's list |
| 9:10 AM | Scanner 3 (Gap) | Top 10 by gap % go into TradingView for today only — append to watchlist, remove at end of day |
| 9:45 / 10:00 / 10:15 / 10:30 AM | Scanner 2 (Live Breakout) | Sanity check — if Chartink shows a stock breaking out that TradingView did not alert on, that stock is missing from your watchlist. Add it for next time. |
| After 11:00 AM | Stop scanning | Entry cutoff has passed; no new entries are valid |

You do not need to do all four every day. The minimum useful loop is **Scanner 1 (3:30 PM) + Scanner 3 (9:10 AM)**. Scanners 2 and 4 are bonuses — Scanner 2 helps you find missed stocks for next time, Scanner 4 finds the highest-conviction setups.

---

### The four scanners

1. **Setup Scanner** (Nifty 500) — night-before / pre-market watchlist build
2. **Live Breakout Scanner** — discovery during market hours
3. **Pre-market Gap Scanner** — 9:10 AM dynamic watchlist augmentation
4. **NR7 End-of-Day Pre-filter** — flags tomorrow's high-probability candidates (Crabel narrow-range)

### Scanner 1: Setup Scanner (Night Before / Pre-Market)

**When to run:** 3:30-4:00 PM after market close, or 9:00-9:10 AM pre-market.
**Purpose:** Find liquid, trending, volatile Nifty 500 stocks to add to TradingView watchlist and set ORB alerts on. Targets 15-30 stocks per day.

```
( {nifty500} (
  latest close > latest sma( close, 200 )
  and latest close > latest sma( close, 50 )
  and latest volume > 1500000
  and latest close > 150
  and latest close < 800
  and latest market cap > 10000
  and latest average true range ( 14 ) / latest close * 100 > 1
  and latest average true range ( 14 ) / latest close * 100 < 4
) )
or
( {nifty500} (
  latest close < latest sma( close, 200 )
  and latest close < latest sma( close, 50 )
  and latest volume > 1500000
  and latest close > 150
  and latest close < 800
  and latest market cap > 10000
  and latest average true range ( 14 ) / latest close * 100 > 1
  and latest average true range ( 14 ) / latest close * 100 < 4
) )
```

| Filter | Bullish group | Bearish group | Why |
|---|---|---|---|
| SMA(200) | Close > SMA(200) | Close < SMA(200) | Long-term trend aligned |
| SMA(50) | Close > SMA(50) | Close < SMA(50) | Short-term trend also aligned — do NOT use > for bearish |
| Volume | > 1,500,000 shares | same | Genuine daily liquidity for clean fills |
| Price band | 150 - 800 | same | Matches `min_entry_price` / `max_entry_price` in config.yaml |
| Market Cap | > 10,000 Cr | same | Institutional grade — avoids illiquid / manipulated stocks |
| ATR(14) % | 1% - 4% of price | same | Meaningful range regardless of price level. Maps to PineScript ORB range filter (0.4-3.5%). Absolute ATR > 5 was misleading: same number means 3% for ₹150 stock vs 0.6% for ₹800 stock |
| Universe | `{nifty500}` | same | Widened from Nifty 200 (2026-04-25) — adds liquid mid-caps in 150-800 band from sectors currently under-represented (auto ancillary, chemicals, specialty pharma). Vol + mcap filters still reject small caps |

**After scan:**
1. Drop symbols in `blacklist.ORB.hard` (BHEL, MANAPPURAM, GAIL, JIOFIN, TATAPOWER, BEL, PNBHOUSING, UNIONBANK, ADANIPOWER, USHAMART, HINDPETRO, SYNGENE) — full block.
2. Tag symbols in `blacklist.ORB.soft` (CANBK, FEDERALBNK, WIPRO, BANKBARODA) with `[soft]` prefix in TradingView watchlist as a visual reminder. The signal engine auto-scales these to 50% qty; no operator action needed.
3. **Sector rotation overlay (manual):** Chartink has no native sector filter. After the scan, glance at 5-day returns of sector indices (NIFTY BANK / METAL / AUTO / PHARMA / IT / FMCG / ENERGY / REALTY / PSU BANK / FIN SERVICE) and prioritise watchlist slots for symbols in the top 3 sectors. De-prioritise (don't drop) bottom 3.

**SMA(50) direction is critical:** Bearish group must use `less than` SMA(50) — not `greater than`. A stock below 200 SMA but above 50 SMA is in a transitioning/recovering phase and may bounce instead of breaking down.

### Scanner 2: Live Breakout / Breakdown Scanner (During Market Hours)

**When to run:** Every 15 minutes from 9:45 AM to 11:30 AM (after entry cutoff, stop scanning).
**Purpose:** Discovery — see which stocks are actively breaking ORH or ORL right now. Cross-check against TradingView alerts; if Chartink shows a breakout TradingView missed, that stock is not in your watchlist yet.

```
( {nifty200} (
  [0] 15 minute close crossed above [=1] 15 minute high
  and [0] 15 minute volume > [=1] 15 minute volume
  and [0] 15 minute close > 150
  and [0] 15 minute close < 800
  and [0] 15 minute close > latest sma( close, 200 )
  and [0] 15 minute close > latest sma( close, 50 )
) )
or
( {nifty200} (
  [0] 15 minute close crossed below [=1] 15 minute low
  and [0] 15 minute volume > [=1] 15 minute volume
  and [0] 15 minute close > 150
  and [0] 15 minute close < 800
  and [0] 15 minute close < latest sma( close, 200 )
  and [0] 15 minute close < latest sma( close, 50 )
) )
```

| Filter | Bullish (ORH break) | Bearish (ORL break) | Why |
|---|---|---|---|
| Breakout | `crossed above [=1] 15m high` | `crossed below [=1] 15m low` | `[=1]` = first 15-min candle (9:15-9:30 AM ORB). `crossed` fires on exact breakout candle only |
| Volume | Breakout candle > ORB candle | same | Confirms genuine breakout, not a fake |
| Price band | 150 - 800 | same | Matches config |
| Trend | Close > SMA(200) + SMA(50) | Close < SMA(200) + SMA(50) | Both SMAs required — consistent with setup scanner. SMA(200) alone misses transitional stocks between 50 and 200 SMA |
| Universe | `{nifty200}` | same | Matches setup scanner universe |

**Important:** `crossed above` / `crossed below` fires only on the **exact candle** of breakout. Each scan run shows only stocks that broke out during the most recent completed 15-min candle — not earlier ones. This is intentional.

**Free tier caveat:** Chartink free accounts have 15-min delayed data. This scanner is for **discovery and audit** only — your primary execution signals come from TradingView real-time alerts. By the time Chartink shows a breakout, the signal engine has already placed the order.

### Scanner 3: Pre-Market Gap Scanner

**When to run:** 09:10 AM IST (after NSE pre-open auction completes at 09:08).
**Purpose:** Augment the static watchlist with today's actual movers. Add the top ~10 stocks showing moderate directional gap and strong first-minute participation. Removes the static-list blind spot where today's big mover wasn't on yesterday's list.

```
( {nifty500} (
  [0] 1 minute open > [=1] 1 day close * 1.005
  and [0] 1 minute open < [=1] 1 day close * 1.02
  and [0] 1 minute volume > [=1] 1 day volume / 375 * 1.5
  and [0] 1 minute close > 150
  and [0] 1 minute close < 800
  and [=1] 1 day volume > 1500000
  and latest market cap > 10000
) )
or
( {nifty500} (
  [0] 1 minute open < [=1] 1 day close * 0.995
  and [0] 1 minute open > [=1] 1 day close * 0.98
  and [0] 1 minute volume > [=1] 1 day volume / 375 * 1.5
  and [0] 1 minute close > 150
  and [0] 1 minute close < 800
  and [=1] 1 day volume > 1500000
  and latest market cap > 10000
) )
```

| Filter | Bullish (gap up) | Bearish (gap down) | Why |
|---|---|---|---|
| Gap range | +0.5% to +2.0% | -0.5% to -2.0% | <0.5% is noise; >2.0% tends to fade. Sweet spot for ORB follow-through |
| First-minute volume | > 1.5× of prev-day-vol/375 | same | 375 = number of 1-min candles in NSE session. 1.5× the avg slice = strong participation |
| Liquidity floor | prev-day vol > 1.5M | same | Same liquidity gate as setup scanner |
| Price + mcap | 150-800, mcap > 10K Cr | same | Match config |
| Universe | `{nifty500}` | same | Match setup scanner |

**Expected yield:** 5–15 rows. **Post-processing:** sort by absolute gap %, take top 10, add to TradingView for today only, remove at EOD.

### Scanner 4: NR7 End-of-Day Pre-filter

**When to run:** 3:25 PM IST (just before close, after today's range is final).
**Purpose:** Find tomorrow's highest-probability ORB breakout candidates — stocks where TODAY's range is the narrowest of the last 7 days AND they pass the setup-scanner quality filters. Per Crabel's NR7 research, breakouts after a narrow-range day have ~70-75% follow-through (vs 55-60% baseline). Fires on roughly 14% of days per stock — these get priority slots in tomorrow's watchlist.

```
( {nifty500} (
  latest high - latest low < 1 day ago high - 1 day ago low
  and latest high - latest low < 2 days ago high - 2 days ago low
  and latest high - latest low < 3 days ago high - 3 days ago low
  and latest high - latest low < 4 days ago high - 4 days ago low
  and latest high - latest low < 5 days ago high - 5 days ago low
  and latest high - latest low < 6 days ago high - 6 days ago low
  and latest close > latest sma( close, 200 )
  and latest close > latest sma( close, 50 )
  and latest volume > 1500000
  and latest close > 150
  and latest close < 800
  and latest market cap > 10000
  and latest average true range ( 14 ) / latest close * 100 > 1
  and latest average true range ( 14 ) / latest close * 100 < 4
) )
or
( {nifty500} (
  latest high - latest low < 1 day ago high - 1 day ago low
  and latest high - latest low < 2 days ago high - 2 days ago low
  and latest high - latest low < 3 days ago high - 3 days ago low
  and latest high - latest low < 4 days ago high - 4 days ago low
  and latest high - latest low < 5 days ago high - 5 days ago low
  and latest high - latest low < 6 days ago high - 6 days ago low
  and latest close < latest sma( close, 200 )
  and latest close < latest sma( close, 50 )
  and latest volume > 1500000
  and latest close > 150
  and latest close < 800
  and latest market cap > 10000
  and latest average true range ( 14 ) / latest close * 100 > 1
  and latest average true range ( 14 ) / latest close * 100 < 4
) )
```

| Filter | Bullish | Bearish | Why |
|---|---|---|---|
| NR7 | today's range < each of last 6 days' ranges | same | Crabel's classic narrow-range-7. The 6 explicit AND conditions make today the narrowest of the last 7 trading days |
| Trend + Liquidity + Price + mcap + ATR | identical to Scanner 1 | same | Filter must compose with the existing quality gates — NR7 alone catches dead stocks too |
| Universe | `{nifty500}` | same | Match setup scanner |

**Expected yield:** 3–8 rows on most days, 0–2 on trendy markets (by design — NR7 is intentionally rare). **Post-processing:** these get a `[NR7]` prefix in the TradingView watchlist; they are the day's premier candidates. The PineScript `enableNRFilter` (Phase 3) provides the same gate at signal-generation time, so even if you forget to mark them in TradingView, the strategy itself can prefer-rank them.

**Note on Chartink syntax:** Chartink does not currently support a clean `min(N, …)` aggregator over historical bars. The 6-AND form is the verified working syntax (each `N days ago` reference is independently supported). If a future Chartink release adds `min(7, latest high - latest low)`, the queries can be shortened.

### Workflow Summary

| Time | Scanner | Action |
|---|---|---|
| 3:25 PM (prev day) | Scanner 4 (NR7) | Tag with `[NR7]` in tomorrow's TradingView watchlist — premier candidates |
| 3:30-4:00 PM | Scanner 1 (Setup, Nifty 500) | Build watchlist; drop hard-blacklist; tag soft-blacklist `[soft]`; merge with NR7 stars |
| 9:00-9:10 AM | Scanner 1 re-run | Catches overnight movers not on yesterday's list |
| 9:10 AM | Scanner 3 (Pre-market Gap) | Append top 10 gap movers; remove at EOD |
| 9:45 AM | Scanner 2 (Live Breakout) | First run after 2nd 15-min candle closes |
| 10:00, 10:15, 10:30 AM | Scanner 2 (Live Breakout) | Repeat each candle — spot missed symbols for future watchlist |
| After entry cutoff (11:00 AM) | — | Stop scanning, no new entries valid |
