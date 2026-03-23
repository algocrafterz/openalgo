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
- **Entry cutoff**: 10:30 AM (optimal per Q1 2026 statistical analysis — ~100% win rate before, ~33% after)
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

#### 1.1 Trailing Stop After TP1
Add a trailing stop mechanism once TP1 is hit:
```
After TP1 hit:
- Move SL to breakeven (entry price)
- Trail stop at ATR * 1.5 below current high (for longs)
- Locks in gains while allowing runners to TP2/TP3
```
**Impact:** Significantly improves R:R on trending days. Currently, the 50/50 split still risks the runner portion at original SL.

#### 1.2 Multi-Entry Per Session
Allow configurable max entries per session (e.g., 2):
```
- After SL hit, reset sessionEntryTaken after cooldown period (e.g., 5 bars)
- Allow opposite direction entry after failed breakout
- Cap total session risk (e.g., max 2% account risk per day)
```
**Impact:** Captures reversal opportunities after failed breakouts - very common in Indian markets.

#### 1.3 Index Correlation Filter (NIFTY/BANKNIFTY)
Add NIFTY 50 direction as a filter:
```
- Request NIFTY 50 data via request.security()
- Check if NIFTY is above/below its VWAP or ORB
- Block counter-index entries (e.g., don't go long on a stock when NIFTY is breaking down)
```
**Impact:** Could reduce false signals by 20-30% based on Indian market correlation patterns.

#### 1.4 Realistic Indian Market Costs
Update commission model:
```
Current:  commission_value=0.03 (brokerage only)
Actual costs for intraday:
- Brokerage: 0.03% or flat Rs 20 (whichever lower)
- STT: 0.025% (on sell side)
- GST: 18% on brokerage
- Exchange fees: 0.00345%
- SEBI charges: 0.0001%
- Stamp duty: 0.003% (buy side)
Total: ~0.1% round trip
```
**Impact:** More accurate backtesting results, avoiding overestimated profitability.

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

### Optimal Settings for NSE Intraday

| Parameter | Recommended | Rationale |
|-----------|-------------|-----------|
| ORB Period | 15 min (primary), 30 min (secondary) | 5 min too noisy for NSE, 60 min too late |
| Session | 9:15-15:30 IST | NSE regular hours |
| Min Entry Time | 9:30 AM | Skip first 15 min auction settlement |
| Entry Cutoff | 14:00 | Allow 1.5 hours for targets to be reached |
| Time Exit | 15:15 | Close before market close |
| Stop Mode | Smart Adaptive or Scaled ATR | Indian stocks have variable volatility profiles |
| Volume Filter | Enable, 1.2x multiplier, 50-bar MA | 50-bar MA resists gap-day opening volume distortion |
| HTF Bias | Daily, Price vs EMA 20 | Aligns with institutional flow |
| Trend Filter | VWAP | Most relevant intraday indicator for Indian markets |
| Risk Per Trade | 1% of account | Conservative for volatile Indian stocks |
| Gap Filter | Enable, max 2% | Indian stocks gap heavily; >2% gaps tend to fill |
| ORB Range Filter | 0.3% - 2.0% | Filters out dead stocks and over-volatile ones |

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

**Key strengths:** Multi-stage ORB, "2-of-3 against" filter logic (Trend + HTF + Index), retest entry, adaptive R:R, Telegram integration.

**Remaining improvements:**
1. Trailing stop after TP1 (biggest single improvement for profitability)
2. Multi-entry capability per session
3. Daily loss limit (stop after 2 consecutive SL hits/day) - would save ~4% from data analysis

**Operational recommendations (manual discipline):**
- Grade-based position sizing: A=100%, B=75%, C=50%
- Stop taking signals after 2 consecutive losses in a day
- Tighter filters on Fridays (or reduced position size)
- Monthly stock review using Grade system

The strategy is best suited for **liquid large-cap stocks and index futures** on 5-minute charts with 15-30 minute ORB periods.
