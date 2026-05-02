# IB Extension Strategy v2 — Analysis & Implementation Reference

> Source of truth: `ib-extension-v6.pine`
> Timeframe: 30-min NSE equity (clock-aligned bars: 9:00, 9:30, 10:00 …)
> Last updated: 2026-05-02

---

## 1. Strategy Concept

The IB (Initial Balance) Extension strategy exploits directional momentum breakouts using two nested opening ranges as confirmation gates. A trade fires only when **both** ranges are broken in the same direction — weekly bias aligning with daily breakout. This double-confirmation eliminates a large class of false intraday breakouts that fool single-IB systems.

### Mental model

```
WEEKLY IB (Mon+Tue range)  → sets the week's directional bias
DAILY IB  (9:00–10:00 range) → sets today's intraday range

STRONG BUY  = price breaks above BOTH W-High and D-High
STRONG SELL = price breaks below BOTH W-Low  and D-Low
WATCH       = weekly broken but daily still inside (heads-up, not a trade)
```

---

## 2. IB Definitions

### 2.1 Daily IB

| Parameter | Value | Rationale |
|---|---|---|
| Session string | `0900-1014` | Clock-aligned 30-min bars on TradingView open at 9:00, 9:30, 10:00. Session membership is checked by bar OPEN time. "0900-1014" captures all three. "0915-1014" misses the 9:00 bar (the actual market open candle). |
| Bars captured | 9:00, 9:30, 10:00 | 3 bars = 90 min window approximating the 9:15–10:15 NSE IB |
| D-High | max(high of 3 bars) | Frozen after 10:00 bar closes |
| D-Low | min(low of 3 bars) | Frozen after 10:00 bar closes |
| D-Mid | (D-High + D-Low) / 2 | Reference level, always displayed |
| Range validity | 0.15% – 2.5% of midpoint | Rejects flat mornings and extreme event-day volatility |

**Bar alignment note:** NSE session 9:00–15:30 = 13 thirty-minute bars per day. This is a clock-aligned chart, not session-aligned. The strategy is designed and tested for 30-min clock-aligned charts only.

### 2.2 Weekly IB

| Parameter | Value | Rationale |
|---|---|---|
| Session | Mon + Tue full NSE session | Opening 2 days of the week set the week's institutional range |
| W-High | max(Mon+Tue highs) | Frozen at Tuesday close |
| W-Low | min(Mon+Tue lows) | Frozen at Tuesday close |
| W-Mid | (W-High + W-Low) / 2 | Reference level |
| Range validity | 0.30% – 4.0% of midpoint | Wider band than daily (2-day range is naturally larger) |
| Valid trading days | Wed, Thu, Fri only | Mon/Tue IB is still forming — no signals allowed |
| Holiday handling | If Monday is holiday, tracking starts Tuesday | Prevents missing IB week entirely |

---

## 3. Signal Conditions

```
finalStrongBuy  = wBOHigh  AND dBOHigh  AND volConfirmed AND longRegimeOK  AND guardsOK AND longRROK
                  AND rising-edge (not true on previous bar) AND barstate.isconfirmed

finalStrongSell = wBOLow   AND dBOLow   AND volConfirmed AND shortRegimeOK AND guardsOK AND shortRROK
                  AND rising-edge AND barstate.isconfirmed
```

**Rising-edge filter:** The signal fires only on the **first** bar the composite condition becomes true. Without this, the triangle marker would appear on every bar that stays above the IB levels (creating visual clutter and multiple strategy entries). `barstate.isconfirmed` prevents repainting on the live bar.

**WATCH signals:** Weekly IB broken but daily IB still inside (price between D-High and D-Low). Not a trade — a heads-up that the weekly gate is open and a daily breakout would trigger.

---

## 4. Signal Filters

### 4.1 Volume Filter

| Parameter | Default | Rationale |
|---|---|---|
| Enabled | ON | Rejects low-participation drifts above D-High |
| SMA Length | 65 bars | 65 × 30-min = 5 trading days (1 week). NSE has 13 bars/day, so 20 bars ≈ 1.5 days — too short and skewed by a single event morning. |
| Multiplier | 1.5× | Breakout bar must have 50% more volume than the 1-week average. Below 1.5× is normal-volume drift; above 1.5× indicates real institutional participation. 1.2× is too permissive; 2.0× over-filters genuine accumulation breakouts. |

**Why 1.5× works:** On NSE 30-min bars, genuine momentum breakouts consistently print 1.5–3× the baseline volume. Low-conviction drifts above D-High sit at 0.8–1.2×. The 1.5× threshold cleanly separates these zones. The 30-min bar completes fully before the signal fires, so volume is not partial — unlike 5-min charts where signal fires on incomplete bar volume.

### 4.2 R:R Filter

| Parameter | Default |
|---|---|
| Enabled | OFF |

Left off by default. The R:R calculation uses a fixed TP (IB extension) and the current close as entry. On late-entry bars where price is far above D-High, R:R appears poor even for setups that would have been valid. More useful as a display metric than a hard gate.

---

## 5. Guard Filters

### 5.1 Regime Filter

| Parameter | Default | Rationale |
|---|---|---|
| Enabled | ON | |
| Symbol | NSE:NIFTY | Nifty 50 — the broadest market regime proxy for NSE |
| EMA Length | 50 (Daily) | ~10 weeks. Widely watched by NSE institutions. Not too fast (20 EMA flips on every 2-week pullback) and not too slow (200 EMA leaves you out for months after recovery). **Do not go below 34.** |

**How it works:** Nifty 50 daily close vs 50-period daily EMA. When Nifty is above its EMA → bullish regime → only LONG signals allowed. Below EMA → bearish regime → only SHORT signals. Regime is evaluated on confirmed daily bars (`lookahead=barmerge.lookahead_off`).

**Why 50 EMA:** IB breakouts profit from trend continuation. In a bull regime (Nifty above 50 EMA), institutional buying programmes are active and breakouts follow through. Below it, mean-reversion dominates. The 50 EMA is widely watched — it becomes self-fulfilling.

### 5.2 Event Day Exclusion

| Parameter | Default |
|---|---|
| Enabled | ON |
| Event Dates | Empty (no effect until populated) |

Skips signals on known high-uncertainty calendar dates (Union Budget, RBI MPC, US Fed meetings, major earnings). IB breakouts on event days are unreliable — the morning range forms under pre-event positioning, not real directional intent. The filter has no effect until dates are added, so enabling it by default is safe.

**Dates to maintain:** RBI MPC bi-monthly (~6 dates/year), Union Budget (Feb 1), US Fed FOMC (~8 dates/year for high-impact stocks), quarterly earnings for actively traded symbols.

### 5.3 Circuit Guard

| Parameter | Default |
|---|---|
| Enabled | ON |
| Threshold | Gap > 4% from prev close |

Skips signal if today's price has gapped more than 4% from the previous close — a proxy for being within 1% of a 5% circuit limit. Post-gap IBs are compressed and unreliable: the stock is thin on one side of the book, and IB extension targets become erratic. Low signal cost on Nifty 50 stocks (rarely triggers); meaningful protection on mid-caps.

---

## 6. SL / TP Methodology

### 6.1 Two SL modes (configurable)

**Mode 1 — IB Structure (default)**

```
Long  SL = D-Low  − ATR(14) × 0.5
Short SL = D-High + ATR(14) × 0.5
```

Structural invalidation: if price re-enters the morning range, the breakout is false. The half-ATR buffer absorbs normal wick noise below D-Low. Best for early breakout entries close to D-High.

**Mode 2 — Entry-Relative ATR**

```
Long  SL = Entry − ATR(14) × multiplier
Short SL = Entry + ATR(14) × multiplier
```

Consistent risk per trade regardless of entry price. Addresses a key flaw of IB Structure mode: if entry is 50 points above D-High but SL is still at D-Low, the actual risk is large even though the R:R display looks acceptable. Recommended for late-entry setups. Typical multiplier: 1.0–2.0.

**When to switch:** Use Entry-Relative ATR when the debug panel shows SIGNAL fires but the trade ticket R:R looks poor (< 1.0) — this indicates a late entry where D-Low is far below entry.

### 6.2 TP levels

```
Long  TP1 = D-High + IB_Range × 1.0   (100% extension above D-High)
Long  TP2 = D-High + IB_Range × 2.0   (200% extension)

Short TP1 = D-Low  − IB_Range × 1.0
Short TP2 = D-Low  − IB_Range × 2.0
```

IB range extension is the standard methodology for IB breakout strategies. TP1 at 1× range is the conservative first target (achievable in most trending days). TP2 at 2× is the full extension for continuation days.

**Partial exit:** TP2 is informational for manual traders. The signal_engine Python module handles partial exits (TP1 = full exit in intraday mode; trail remainder in swing mode).

### 6.3 ATR parameters

| Parameter | Default | Rationale |
|---|---|---|
| ATR Length | 14 | ATR(14) on 30-min ≈ 7 hours (slightly over 1 trading day). Captures recent volatility without being too noisy (5-bar) or too slow (50-bar). |
| SL Buffer (IB Structure) | 0.5 | Half-ATR below D-Low. For INFY at ₹1500 with ATR ≈ 20, buffer = ₹10 below D-Low. Tight enough to be structural, wide enough to avoid noise wicks. |

---

## 7. Strategy Backtesting Parameters

```pine
process_orders_on_close = true   // fills at signal bar's close = matches trade ticket label
commission_value = 0.06%         // approximate NSE round-trip for retail broker
slippage = 2 ticks
initial_capital = ₹10,00,000
```

**How to read strategy report results:**
- Trust: win rate direction, relative comparison between settings, max drawdown character
- Do not trust: exact P&L figures (require sub-15 second execution after bar close), SL/TP exact fills (real markets gap through levels)
- Minimum sample: 30–50 trades per symbol before drawing conclusions. On 30-min NSE, one symbol may generate only 15–30 signals per year — use 3+ years of data.

---

## 8. Stock Universe Analysis

### 8.1 Criteria for this strategy

| Criterion | Ideal range | Why |
|---|---|---|
| Market cap | Nifty 50 / Nifty 100 | Deep order book, tight spreads on 30-min bars |
| Beta (vs Nifty) | 0.8 – 1.5 | Low beta = no follow-through; high beta = excess whipsaws |
| Daily avg volume | > 5M shares | Volume filter at 1.5× needs reliable baseline |
| Trending tendency | Hurst > 0.5 | Strategy profits from continuation, not mean-reversion |
| Nifty correlation | > 0.7 | Regime filter (Nifty 50 EMA) is meaningful only for correlated stocks |
| Circuit frequency | Low | Circuit guard removes these days — fewer is better |
| Sector | IT, Auto, Capital Goods, NBFC | Tend to trend; Banking/FMCG more mean-reverting |

### 8.2 Tier 1 — Best fit

| Symbol | Sector | Why it works |
|---|---|---|
| NSE:RELIANCE | Energy/Conglomerate | Largest NSE stock, 25%+ Nifty weight, sets market tone, excellent IB formation, deep book |
| NSE:INFY | IT | Tracks US tech + USD/INR, clean directional trends, strong liquidity, low circuit risk |
| NSE:TCS | IT | Similar to INFY, slightly lower beta but very consistent IB formation |
| NSE:HCLTECH | IT | Higher beta than TCS/WIPRO, better momentum properties for this strategy |
| NSE:ICICIBANK | Banking | Best private bank for breakouts — higher beta than HDFC, strong trending, growth-oriented |
| NSE:AXISBANK | Banking | Similar to ICICI, slightly more volatile, good momentum stock |
| NSE:BAJFINANCE | NBFC | High-beta NBFC, strong trending in bull markets, very responsive to regime filter |
| NSE:LT | Capital Goods | Capex cycle plays trend well, decent beta, reliable IB formation |
| NSE:MARUTI | Auto | Auto bellwether, clean sector trends, beta ~1.0, good IB respect |
| NSE:HDFCBANK | Banking | Highest liquidity on NSE, lower beta post-merger but anchors the portfolio |

### 8.3 Tier 2 — Viable with caveats

| Symbol | Caveat |
|---|---|
| NSE:SBIN | PSU bank — politically sensitive, event-prone, higher circuit risk on budget/rate days. Circuit guard helps but still noisier than private banks. |
| NSE:WIPRO | Lower beta IT (0.7–0.8), less intraday momentum. Decent for trend but generates fewer quality signals. |
| NSE:TATAMOTORS | High beta (1.3–1.5) with EV/JLR headline risk. Can work in strong trend phases but generates whipsaws in consolidation. |
| NSE:SUNPHARMA | Follows global pharma cycle, less correlated with Nifty. Regime filter less effective. |
| NSE:KOTAKBANK | Quality bank but lower liquidity than ICICI/HDFC. Volume filter may be harder to calibrate. |

### 8.4 Avoid

| Symbol | Reason |
|---|---|
| NSE:HINDUNILVR, NSE:NESTLEIND | Defensive FMCG, beta < 0.5, almost no intraday momentum |
| NSE:NTPC, NSE:POWERGRID | Utility/PSU, very low beta, range-bound by design |
| NSE:ADANIENT, NSE:ADANIPORTS | Volatile with manipulation/circuit risk. Volume filter parameters unreliable. |
| Mid-cap stocks | Wider bid-ask spreads invalidate the 0.06% commission assumption; volume baseline unstable |

### 8.5 Default scanner (current configuration in `ib-extension.pine`)

| Slot | Symbol | Sector | Tier |
|---|---|---|---|
| Symbol 1 | NSE:RELIANCE | Energy/Conglomerate | 1 |
| Symbol 2 | NSE:INFY | IT | 1 |
| Symbol 3 | NSE:HDFCBANK | Banking | 1 |
| Symbol 4 | NSE:TCS | IT | 1 |
| Symbol 5 | NSE:ICICIBANK | Banking | 1 |
| Symbol 6 | NSE:MARUTI | Auto | 1 |
| Symbol 7 | NSE:AXISBANK | Banking | 1 |
| Symbol 8 | NSE:LT | Capital Goods | 1 |
| Symbol 9 | NSE:BAJFINANCE | NBFC | 1 |
| Symbol 10 | NSE:HCLTECH | IT | 1 |

All 10 slots are Tier 1. SBIN (PSU bank, event-prone) and WIPRO (low beta IT) were replaced with MARUTI and HCLTECH respectively.

---

## 9. Suggested Improvements (not yet implemented)

### 9.1 VWAP confirmation filter (HIGH value)
Add optional gate: long only when `close > ta.vwap`, short only when `close < ta.vwap`. VWAP is the most-watched intraday level by NSE institutions. A breakout above D-High that occurs below VWAP is fighting institutional selling pressure. Above VWAP = breakout aligned with the day's average buyer. Built-in `ta.vwap` in Pine v6, single bool check.

### 9.2 Entry time cap — no signals after 14:00 IST (MEDIUM value)
IB breakout signals firing after 14:00 leave less than 90 minutes to 15:30 close for intraday mode. TP1 (1× IB range above D-High) often cannot be reached in this window. Adding an optional session end filter (`not f_inSession("0000-1400")`) prevents low-probability late-day entries.

### 9.3 Late-entry distance cap (MEDIUM value)
If `close − D-High > ATR × threshold`, skip the signal. Prevents chasing breakouts that have already extended significantly. Complements the Entry-Relative ATR SL mode — together they ensure both risk and entry quality are managed for late setups.

### 9.4 IB range quality ratio (LOW-MEDIUM value)
If `dailyIBRange / weeklyIBRange > 0.8`, the morning range consumed almost the entire week's range — W-H/W-L are no longer reliable as separate support/resistance levels. These days tend to produce choppy reversals after breakout. Block signal when this ratio is exceeded.

---

## 10. Known Limitations

1. **30-min chart only.** Session string `0900-1014` captures 3 bars on clock-aligned 30-min. On 15-min it captures 5 bars (different D-H/D-L), on daily it captures nothing. Always use 30-min.

2. **No partial exit modeling.** Strategy report shows full exit at TP1. The signal_engine handles partial exits and trailing stops in live trading. The backtest P&L is not directly comparable to live results.

3. **Static TP is mechanical.** TP1 = D-High + 1× range is a projection, not market-structure-based. On days where PDH (Previous Day High) or a major swing level sits between D-High and TP1, the actual resistance is lower than the projected target.

4. **Regime filter is one-dimensional.** Nifty 50 vs 50 EMA is a binary filter. It doesn't capture choppy markets (Nifty oscillating around the EMA) or sector divergence (IT trending while Banks reverse).

5. **Weekly IB requires a valid Mon+Tue.** If both Monday and Tuesday are NSE holidays, there is no Weekly IB for that week and no signals can fire (weeklyValidDay = false). This is correct behaviour — not a bug.

6. **Strategy report trust level:** Win rate and relative comparisons are trustworthy. Absolute P&L is aspirational — real fills at bar close require sub-15 second execution, and SL/TP fills assume no gap-through.
