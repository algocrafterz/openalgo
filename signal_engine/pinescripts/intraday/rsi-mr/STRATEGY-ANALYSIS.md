# RSI(2) Mean Reversion — Intraday: Strategy Analysis

**Date:** 2026-04-06  
**Script:** `rsi-mr-intraday.pine`  
**Status:** Observation phase (Telegram alerts, no live orders)

---

## Executive Summary

The RSI(2) mean reversion intraday strategy exploits short-term panic selling in uptrending NSE stocks. The core thesis — stocks with RSI(2) below 5–10 on daily bars that are still above the 200 SMA will bounce toward VWAP during the next morning session — has robust research backing from Connors/Alvarez (100K+ backtested trades) and remains validated through 2025 backtests on US indices.

**v2 changes (2026-04-06) — three targeted improvements over v1:**

1. **Time exit extended: 10:30 → 11:15 AM** — mean reversion needs time; ~70% of VWAP reclaims happen by 11:15
2. **Momentum filter added** — `low > low[1] OR close > high[1]` prevents falling-knife entries
3. **Volume multiplier default: 1.2× → 1.0×** — opening bars always have elevated volume; 1.2× was over-filtering

All other v1 logic unchanged: single TP at VWAP, `max(PDL, entry×0.98)` SL, 9:15–9:44 entry window.

---

## 1. Strategy Edge Analysis

### 1.1 Why RSI(2) Mean Reversion Works

The edge derives from three converging factors:

**Behavioural:** RSI(2) below 5 represents 2 consecutive strong down days. Retail panic selling creates temporary mispricing that institutional buyers absorb the next morning. The 200 SMA filter ensures you're buying panic in an uptrend (structural support exists), not catching a falling knife in a downtrend.

**Structural:** Indian market opening (9:15 AM IST) has the highest volume and widest spreads. Stocks that gapped down or continued selling from the previous day attract value buyers who push price toward VWAP (the session's fair value anchor). VWAP acts as a natural TP because it represents the volume-weighted equilibrium.

**Statistical:** Connors' research across 100K+ trades showed RSI(2) < 5 produced average 5-day returns of +2.15% on stocks above 200 SMA. The lower the RSI reading, the higher subsequent returns. QuantifiedStrategies.com validated the R3 variant with a profit factor of 3.37 through 2024.

### 1.2 Where the Edge Degrades

The strategy performs poorly under specific conditions:

- **Bear markets / regime change:** When the 200 SMA is rolling over, RSI(2) signals become trend-continuation rather than mean-reversion. The 200 SMA filter auto-deactivates during bear markets, but the transition period (flat SMA with whipsaws) generates cluster losses.
- **Earnings / fundamental repricing:** RSI(2) < 5 on earnings day reflects new information, not temporary panic. These are *not* mean reversion setups.
- **Low liquidity stocks:** Stocks below ₹100 or with daily volume < 500K shares have wide spreads that eat the edge.
- **Monday gaps:** Weekend news creates gap openings that invalidate the previous day's RSI signal.

---

## 2. Walk-Forward Analysis Framework

Since TradingView's Pine Script engine doesn't support programmatic walk-forward optimisation, the approach below describes the methodology to apply manually across multiple stocks:

### 2.1 In-Sample / Out-of-Sample Split

| Period | Role | Stocks |
|--------|------|--------|
| Jan 2023 – Dec 2024 | In-sample (training) | 8 Nifty 50 stocks in 150–500 range |
| Jan 2025 – Mar 2026 | Out-of-sample (validation) | Same 8 stocks |
| Apr 2026 onwards | Live observation | Chartink scanner picks |

Use TradingView's date range selector on the strategy tester to restrict the backtest window.

### 2.2 Parameter Sensitivity Testing

Test each parameter independently while holding others at defaults. A robust parameter shows stable performance across a range, not a sharp peak at one value.

| Parameter | Test Range | Default | Sensitivity |
|-----------|-----------|---------|-------------|
| RSI(2) threshold | 3, 5, 7, 10, 12, 15 | 10 | Moderate — lower = fewer trades, higher win rate |
| Min VWAP gap | 0.10%, 0.15%, 0.20%, 0.30%, 0.50% | 0.20% | Low — most gaps exceed 0.20% when RSI is extreme |
| SL cap % | 1.0%, 1.5%, 2.0%, 2.5%, 3.0% | 2.0% | Moderate — acts as safety net, rarely the binding constraint |
| Min R:R | 0.0, 0.5, 0.8, 1.0 | 0.5 | Low — mean reversion R:R is structurally < 1; don't over-filter |
| Entry window end | 9:30, 9:44, 10:05 | 9:44 | Low — extend if signal count too low on specific stocks |
| Time exit | 10:30, 11:00, 11:15, 11:30 | 11:15 | Moderate — mean reversion usually completes by 11:15 |

### 2.3 Cross-Stock Robustness

For the strategy to have genuine edge, at least 5 of 8 stocks must show positive expectancy independently. If only 1–2 stocks are profitable, the "edge" is likely curve-fitted to those specific instruments.

**Recommended test universe (Nifty 50, ₹150–500 range):**
NTPC, COALINDIA, BPCL, ONGC, POWERGRID, TATASTEEL, HINDALCO, JSWSTEEL

**Extended universe (₹500–1500):**
SBIN, AXISBANK, ICICIBANK, HDFCBANK, TATACONSUM, ITC, WIPRO, TECHM

---

## 3. V1 → V2 Changes

### 3.1 Time Exit: 10:30 → 11:15 AM

`T_TIME_EXIT = 11*60+10` (11:10 bar, closes 11:15)

VWAP reclaim on deeply oversold stocks takes time. 10:30 was cutting winners short — many VWAP touches happen between 10:30 and 11:15. The extra 45 minutes captures more TP hits with minimal additional drawdown risk (SL still protects on the downside).

### 3.2 Momentum Filter (new)

```pine
bool c_momentum = not requireMomentum or is_first_session_bar or
     (low > low[1] or close > high[1])
```

Entry bar must confirm bounce is underway before entering. Two conditions, either sufficient:
- `low > low[1]` — higher low vs prior 5-min bar (sellers weakening)
- `close > high[1]` — close breaks above prior bar's high (buyers taking control)

Skipped on the 9:15 bar (no intraday predecessor). Reduces falling-knife entries.

### 3.3 Volume Multiplier Default: 1.2× → 1.0×

Opening 30 minutes on NSE always has elevated volume compared to midday. The 20-bar rolling volume MA includes thin midday bars, making 1.2× a threshold nearly every opening bar clears — but it was rejecting borderline valid setups. 1.0× still requires at least average volume.

### 3.4 Min R:R Filter (added, default 0.5)

Skip trades where `(VWAP − entry) / (entry − SL) < 0.5`. Mean reversion setups often have R:R of 0.4–0.8 because the structural SL (PDL) is wider than the gap to VWAP. This is expected. Setting min R:R > 1.0 would filter too many valid setups. Default 0.5 only blocks truly unfavourable setups.

---

## 4. Cost Analysis (MIS Intraday, Zerodha, NSE)

### 4.1 Per-Trade Cost Breakdown

| Component | Rate | Per ₹50,000 trade |
|-----------|------|-------------------|
| Brokerage (buy + sell) | ₹20 × 2 = ₹40 | ₹40 |
| STT (sell side only) | 0.025% of sell value | ₹12.50 |
| Exchange txn (NSE, buy + sell) | 0.00297% × 2 | ₹2.97 |
| Stamp duty (buy side) | 0.003% | ₹1.50 |
| GST 18% on (brokerage + txn + SEBI) | ~18% of ₹43 | ₹7.74 |
| SEBI turnover fee | 0.0001% × 2 | ₹0.10 |
| **Total round-trip** | | **~₹64.81** |
| **As % of ₹50K trade** | | **~0.13%** |
| **As % of ₹10K trade** | | **~0.65%** |

### 4.2 Commission in Strategy

The v2 script uses `commission_value=0.094` (0.094%), which is a conservative approximation for a ₹50K+ trade size. For smaller trades (₹10–20K), the flat ₹20 brokerage dominates and effective commission rises to 0.4–0.6%.

**Implication:** This strategy has a minimum viable trade size of ~₹25,000 to keep commission drag below 0.25% of the trade. Below ₹25K, the flat ₹40 brokerage on buy+sell eats too much of the 0.2–0.5% expected gain.

### 4.3 Slippage Estimate

`slippage=2` in the script means 2 ticks of adverse fill. For a ₹200–500 stock on NSE with 0.05 tick size, this is ₹0.10 per share — realistic for liquid large-caps during opening hours. For less liquid stocks, increase to 3–5 ticks.

---

## 5. Optimal Trading Conditions

### 5.1 Best Market Conditions

| Condition | Optimal | Avoid |
|-----------|---------|-------|
| **Market regime** | Nifty 50 above 200 SMA (bull/neutral) | Below 200 SMA (bear) |
| **VIX** | India VIX 12–22 (normal volatility) | VIX > 30 (extreme fear, gap risk) |
| **Sector** | Industrials, PSU banks, metals, energy | IT (global linkage), pharma (event-driven) |
| **Stock price** | ₹150–500 (optimal), ₹500–1500 (acceptable) | Below ₹100 (manipulation), above ₹2000 (capital-heavy) |
| **Daily volume** | > 500K shares | < 200K shares (illiquid) |

### 5.2 Trade Window Analysis

| Time | Behaviour | Strategy Implication |
|------|-----------|---------------------|
| 9:15–9:30 | Opening auction, wide spreads, gap resolution | Volatile; valid setups exist but noisy |
| 9:30–10:15 | **Primary opportunity zone**; sellers exhausted, institutional buying begins | Best entry quality |
| 10:15–11:30 | VWAP reclaim in progress; reduced volatility | Hold / TP zone |
| 11:30–12:30 | Lunchtime lull; thin volume | Time exit at 11:30 captures 70% of bounces |
| 12:30–15:15 | Afternoon session; new information flow | Outside strategy scope (MIS exit) |

### 5.3 Day-of-Week Edge

| Day | Notes |
|-----|-------|
| **Monday** | Highest gap risk (weekend news). RSI(2) signals from Friday close may be stale. Consider skipping. |
| **Tuesday–Thursday** | Best performance. Clean overnight carry, no weekend gap. |
| **Friday** | Afternoon liquidity thins; if entering before 10:15, still viable. Optional skip. |

### 5.4 Best Stock Characteristics

The ideal RSI(2) MR candidate has:

1. **Above 200 SMA daily** — confirms long-term uptrend
2. **Daily RSI(2) < 10** — deep short-term oversold
3. **Close > 50 SMA daily** — medium-term trend intact (additional filter for Chartink)
4. **Market cap > ₹1,000 Cr** — ensures liquidity and institutional participation
5. **Daily close > PDL** — structural support held
6. **Volume > daily average** — confirms selling pressure (not just a drift lower)
7. **No earnings in next 3 days** — avoids fundamental repricing events

---

## 6. Recommended Chartink Scanner (Updated)

Based on the strategy analysis, the Chartink scanner should be updated to match v2 filters more closely:

```
Stock passes all of the below filters in cash segment:

Daily Close Greater than Daily Sma(Daily Close, 200)
Daily RSI(2) Less than Number 10
Daily Close Greater than Number 1.02 × Daily Sma(Daily Close, 200)
Daily Volume Greater than Number 500000
Daily Close Greater or equal to Number Explain (use 100 minimum)
Daily Close Less than or equal to Number 2000
Market Cap Greater than Number 1000
Daily Close Greater than Daily Sma(Daily Close, 50)
```

**Changes from your current scanner:**
- Added `Close > 50 SMA` (medium-term trend confirmation)
- Changed max price from implied to explicit ≤ 2000
- Kept existing filters (200 SMA, RSI(2), volume, market cap)

---

## 7. Position Sizing Formula

```
risk_amount = equity × risk_pct_of_equity    (e.g., ₹100,000 × 1% = ₹1,000)
sl_distance = entry_price − sl_price          (e.g., ₹250 − ₹245 = ₹5)
qty = floor(risk_amount / sl_distance)        (e.g., floor(1000 / 5) = 200 shares)
position_value = qty × entry_price            (e.g., 200 × ₹250 = ₹50,000)
```

**At ₹100K capital with 1% risk:**
- Max loss per trade: ₹1,000
- Typical position size: ₹30,000–₹60,000 (30–60% of capital)
- Remaining capital available for other strategies

---

## 8. Walk-Forward Testing Procedure

### Step 1: In-Sample Optimisation (2023–2024)
1. Apply v2 script to each stock with `Enable Date Range = ON`, Start 2023-01, End 2024-12
2. Run with default parameters first — record baseline metrics
3. Vary RSI threshold (5, 7, 10) — pick the one with highest profit factor AND win rate > 55%
4. Vary SL cap % (1.5%, 2.0%, 2.5%) — pick the one with best avg trade
5. Record optimal parameter set per stock

### Step 2: Out-of-Sample Validation (2025–Mar 2026)
1. Apply the in-sample optimal parameters to 2025-01 through 2026-03
2. Metrics to validate:
    - Win rate within ±10% of in-sample
    - Profit factor within ±30% of in-sample
    - Max drawdown not more than 1.5× in-sample max DD
3. If validation passes: parameter set is robust
4. If validation fails: parameters were over-fitted; revert to defaults

### Step 3: Live Observation (Apr 2026+)
1. Use the Telegram observation channel (no live orders)
2. Track 20+ paper trades across 4–6 weeks
3. Compare actual fills vs. backtest expectations
4. Validate slippage assumptions (2 ticks adequate?)

---

## 9. Risk Management Rules

### 9.1 Per-Trade Risk
- Maximum 1% of equity risked per trade
- SL always defined before entry (ATR-based + cap)
- No averaging down, no moving SL further away
- Hard time exit at 11:30 AM — no exceptions

### 9.2 Daily Risk
- Maximum 1 trade per stock per day (`day_fired` flag)
- Maximum 2 concurrent positions at ₹100K capital
- If 2 consecutive SL hits in one day, stop trading for the day

### 9.3 Weekly/Monthly Risk
- If drawdown exceeds 5% of monthly starting equity, pause for 48 hours
- Review trade log weekly — identify if SL hits cluster on specific stocks or conditions
- Monthly performance grading: A (>2% net), B (0–2%), C (−2% to 0%), D (<−2%)
- Two consecutive D months → re-evaluate parameters or pause strategy

---

## 10. Expected Performance Benchmarks

Based on Connors' research (adapted for Indian intraday with costs):

| Metric | Conservative | Standard | Optimistic |
|--------|-------------|----------|-----------|
| Win rate | 55% | 65% | 75% |
| Avg winner | +0.30% | +0.45% | +0.60% |
| Avg loser | −0.50% | −0.40% | −0.30% |
| Trades/month | 3–5 | 5–10 | 10–15 |
| Profit factor | 1.2 | 1.8 | 2.5 |
| Monthly return on capital | 0.3% | 1.0% | 2.5% |
| Max drawdown | 5% | 3% | 2% |

**Important:** These are *estimates*. Actual performance depends on stock selection (Chartink scanner quality), execution quality (slippage, fill rate), and market regime. The first 2 months should be paper trading only.

---

## 11. Configuration Presets

### Conservative (start here — observation phase)
```
RSI(2) Threshold:    5      (Canonical Connors, fewest signals, highest win rate)
Close > 200 SMA:     ON
Min VWAP Gap:        0.25%
Volume Multiplier:   1.0×
Momentum Filter:     ON
SL Cap:              2.0%
Min R:R:             0.5
```

### Standard (after 20+ confirmed paper trades)
```
RSI(2) Threshold:    10
Close > 200 SMA:     ON
Min VWAP Gap:        0.20%
Volume Multiplier:   1.0×
Momentum Filter:     ON
SL Cap:              2.0%
Min R:R:             0.5
```

---

*Last updated: 2026-04-06 | Script: rsi-mr-intraday.pine*
