# RSI(2) Trend Pullback Strategy — Complete Implementation Guide

**Market:** Indian Equities (NSE) · **Timeframe:** Daily · **Holding:** 2–7 days · **Direction:** Long only

> **One-line summary:** Buy panic dips in strong uptrending stocks and exit on the quick bounce.

---

## Table of Contents

1. [Strategy Overview](#1-strategy-overview)
2. [Why This Strategy](#2-why-this-strategy)
3. [Entry & Exit Rules](#3-entry--exit-rules)
4. [Position Sizing & Risk](#4-position-sizing--risk-management)
5. [Daily Execution Workflow](#5-daily-execution-workflow)
6. [Chartink Scanner Setup](#6-chartink-scanner-setup)
7. [TradingView Pine Script](#7-tradingview-pine-script)
8. [Expected Performance](#8-expected-performance)
9. [Market Regime Guide](#9-market-regime-guide)
10. [Risks & Limitations](#10-risks--limitations)
11. [Optimisation Guidelines](#11-optimisation-guidelines)
12. [Trade Journal Template](#12-trade-journal-template)
13. [Frequently Asked Questions](#13-frequently-asked-questions)
14. [References & Further Reading](#14-references--further-reading)

---

## 1. Strategy Overview

This strategy exploits a recurring market behaviour: strong stocks pull back briefly due to panic selling, then revert upward quickly. It combines two orthogonal edges — trend following (200 SMA) ensures directional bias, whilst mean reversion (RSI(2)) captures short-term inefficiency.

| Parameter | Specification |
|-----------|---------------|
| Strategy type | Mean reversion inside a trend |
| Core signal | RSI(2) < 5 in uptrend |
| Entry timing | At market close (3:15–3:25 PM IST) |
| Exit trigger | Close > 5 SMA, or take-profit levels |
| Holding period | 2–7 days (typically 3–5) |
| Trades per stock per year | 10–25 |
| Time in market | 5–15% |
| Stop-loss | None (by design — see section 10) |
| Safety exit | Close < 200 SMA (trend broken) |

### Conceptual Foundation

Markets are not perfectly efficient in the short term. Short-term price moves are driven by emotional selling, news overreaction, and liquidity imbalance. These create temporary mispricing that RSI(2) captures.

**RSI(2)** is the 2-period Relative Strength Index — an extremely sensitive oscillator that captures 1–2 day panic moves. It was popularised by Larry Connors and has been proven to outperform longer RSI periods (RSI(14), RSI(7)) specifically for mean-reversion applications.

---

## 2. Why This Strategy

### 2.1 Academic & Practitioner Evidence

- **Larry Connors & Cesar Alvarez** — backtested across hundreds of thousands of trades in "Short Term Trading Strategies That Work" (2008) and "High Probability ETF Trading" (2009).
- **34-year independent backtest** — Python backtest on S&P 500 data from 1990–2024 confirmed the strategy still generates positive returns (Trade2Win forum, September 2024).
- **QuantifiedStrategies.com** — validated the R3 variant in 2024: profit factor 3.37, confirmed still working 12 years after publication.
- **Connors RSI backtests** — stocks with CRSI readings of 0–5 produced average 5-day returns of +2.15%.
- **StockManiacs.net** — documented Indian-market-specific RSI(2) application with Chartink scanner integration.

### 2.2 Why This Beats BTST for Indian Markets

The critical advantage over overnight (BTST) strategies is the holding period. Indian delivery trades carry a 0.224% round-trip cost (primarily STT at 0.1% each side). With BTST, this cost exceeds the ~0.03% overnight drift by 4–10×. With a 3–7 day swing capturing a 1.5–3% move, the cost represents only 7–15% of the expected gain.

| Approach | Expected move | Cost (STT+taxes) | Cost as % of move |
|----------|---------------|-------------------|-------------------|
| BTST (overnight) | +0.03% | 0.224% | **747%** — unviable |
| RSI(2) swing (3–7 days) | +1.5–3.0% | 0.224% | **7–15%** — viable |

### 2.3 Behavioural Edge

This strategy exploits three well-documented behavioural biases:

1. **Fear-based selling** — retail investors panic-sell on 1–2 day drops, creating temporary oversold conditions.
2. **Short-term inefficiency** — institutional rebalancing and algorithmic mean-reversion flows correct the mispricing within 3–5 days.
3. **Liquidity-driven mispricing** — sell-side pressure temporarily depresses prices below fair value; buying into this pressure captures the discount.

---

## 3. Entry & Exit Rules

### 3.1 Entry Conditions (ALL must be true)

| # | Condition | Purpose |
|---|-----------|---------|
| 1 | Close > 200 SMA | Uptrend confirmation — only buy in bull trends |
| 2 | Close > 50 SMA *(optional)* | Strong stock filter — avoids weak/sideways names |
| 3 | **RSI(2) < 5** | **Core signal** — short-term panic selling detected |
| 4 | Volume > 5,00,000 | Liquidity filter — ensures clean execution |
| 5 | Price > ₹100 | Avoids penny stocks with erratic behaviour |
| 6 | No earnings/results due within 3 days | Avoids event risk that overrides technical signals |

**Execution:** Enter at market close (3:15–3:25 PM IST) using CNC (Cash & Carry) product type. Do NOT chase intraday moves.

### 3.2 Exit Rules

| Exit Type | Condition | Action |
|-----------|-----------|--------|
| **Primary exit** | Close > 5 SMA | Sell at close — initial bounce captured |
| Take-profit 1 | +1.2% from entry | Exit 50% of position |
| Take-profit 2 | +2.5–3.0% from entry | Exit remaining position |
| Time exit | 10–12 days held | Force exit — mean reversion hasn't completed |
| **Safety exit** | Close < 200 SMA | **Exit immediately** — trend is broken |

### 3.3 Critical: No Traditional Stop-Loss

> Connors' research across hundreds of thousands of backtested trades found that adding stop-losses consistently degraded results. Mean reversion needs room to work — a stock that drops further after entry is now even MORE oversold and MORE likely to bounce. Stops cut winning trades early.

The **safety exit** (close < 200 SMA) serves as a structural trend-break stop instead. This protects against catastrophic losses (stock enters a genuine downtrend) whilst giving the mean-reversion signal room to work.

**If this makes you uncomfortable:** you can add a wide stop at –8% to –10% below entry. Connors' data shows this hurts long-run performance, but it may be necessary for psychological comfort — and a strategy you can actually follow is better than a theoretically optimal one you abandon.

---

## 4. Position Sizing & Risk Management

### 4.1 Allocation Per Trade

Allocate **15–20% of total trading capital** per position. Run across 3–5 stocks simultaneously for diversification. RSI(2) < 5 is a rare event per stock, so multiple stocks rarely trigger on the same day.

| Total Capital | Per Position (20%) | Max Concurrent | Max Exposure |
|---------------|-------------------|----------------|--------------|
| ₹5,00,000 | ₹1,00,000 | 3–5 stocks | 60–100% |
| ₹10,00,000 | ₹2,00,000 | 3–5 stocks | 60–100% |
| ₹25,00,000 | ₹5,00,000 | 3–5 stocks | 60–100% |

### 4.2 Cost Structure (NSE Delivery via Zerodha)

| Component | Rate | Per ₹1,00,000 trade |
|-----------|------|---------------------|
| STT (buy + sell) | 0.1% × 2 | ₹200 |
| Brokerage (Zerodha CNC) | 0% | ₹0 |
| Exchange charges | ~0.00345% × 2 | ₹6.90 |
| Stamp duty (buy side) | ~0.015% | ₹15 |
| GST + SEBI charges | ~0.002% | ₹2.24 |
| **Total round trip** | **~0.224%** | **~₹224** |

With an expected average gain of 1.5–3% per trade, the cost is **7–15% of the expected move** — viable.

### 4.3 Drawdown Expectations

- **Individual trade max loss:** –5% to –10% (no stop-loss, but safety exit at 200 SMA break)
- **Portfolio max drawdown:** –15% to –25% (diversified across 3–5 positions)
- **Recovery:** mean reversion drawdowns recover faster than trend-following drawdowns
- **Worst regime:** sustained bear market — strategy simply stops trading (no trades when close < 200 SMA)

### 4.4 Tax Implications

| Scenario | Classification | Tax Rate |
|----------|---------------|----------|
| Occasional BTST/swing trades (< 10/year) | Short-Term Capital Gains (STCG) | 20% flat |
| Frequent active trading (> 30 trades/year) | Speculative/Non-speculative business income | Slab rate (up to 30%) |
| F&O trades | Non-speculative business income | Slab rate |

Consult a CA for your specific situation. If trading frequently, maintain a proper trade journal (see section 12) for ITR filing.

---

## 5. Daily Execution Workflow

### 5.1 After-Market Scan (3:30–4:00 PM IST)

1. Run Chartink scanner (see section 6) or TradingView screener.
2. Review shortlisted stocks — typically 0–5 names per day.
3. For each candidate, verify:
   - No major earnings/results/AGM within 3 days
   - No extreme gap-down (> 3–4%) — this may indicate structural breakdown, not temporary panic
   - Clean pullback pattern — not a series of lower lows breaking through support
   - Sector is not in heavy downtrend (check sectoral index)
4. Rank by conviction: prefer Nifty 50/Nifty Next 50 names over smaller stocks.
5. Place CNC buy order at or near close if signal is confirmed.

### 5.2 Entry Checklist

- [ ] RSI(2) < 5 at today's close confirmed
- [ ] Stock is above 200 SMA
- [ ] Stock is above 50 SMA (preferred)
- [ ] Volume > 5 lakh shares today
- [ ] No earnings/results due within 3 days
- [ ] Position size defined (15–20% of capital)
- [ ] Not overexposed to same sector (max 2 stocks per sector)
- [ ] CNC order placed

### 5.3 Daily Monitoring (During Hold)

- [ ] Check: close > 5 SMA? → Primary exit signal
- [ ] Check: unrealised profit > +1.2%? → Partial exit (TP1)
- [ ] Check: unrealised profit > +2.5–3%? → Full exit (TP2)
- [ ] Check: close < 200 SMA? → **Immediate safety exit**
- [ ] Count days held → force exit at 10–12 days if no bounce

### 5.4 Exit Execution

- Exit at market close (3:15–3:25 PM), not intraday.
- If TP1 hit intraday, you may exit 50% — but prefer EOD confirmation.
- Log trade in journal immediately (see section 12).

---

## 6. Chartink Scanner Setup

### 6.1 Primary Scanner (RSI(2) < 5, Uptrend)

Go to [chartink.com/screener](https://chartink.com/screener) and enter:

```
( {cash} ( [0] 5 minute close > [0] 5 minute sma( close, 200 ) and
  [0] 5 minute close > 100 and
  [0] 5 minute volume > 500000 and
  latest rsi( close, 2 ) < 5 ) )
```

**Note:** Chartink uses its own query syntax. The above is a representative template — you may need to adjust based on Chartink's current interface. The key filters are:

- Close > SMA(200)
- RSI(2) < 5
- Volume > 500000
- Close > 100

### 6.2 Alternative: TradingView Screener

In TradingView, go to Screener → Stock → India → and set:

| Filter | Condition |
|--------|-----------|
| Exchange | NSE |
| RSI (2) | Below 5 |
| SMA (200) | Price above |
| Volume | Above 500K |
| Price | Above 100 |

Save this as a preset for daily scanning.

### 6.3 Recommended Watchlist

Maintain a fixed watchlist of 30–50 liquid NSE stocks to scan daily. Suggested starting universe:

**Nifty 50 names:** RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK, HINDUNILVR, ITC, SBIN, BHARTIARTL, KOTAKBANK, LT, AXISBANK, BAJFINANCE, MARUTI, ASIANPAINT, TITAN, ULTRACEMCO, WIPRO, HCLTECH, SUNPHARMA

**Nifty ETFs:** NIFTYBEES, BANKBEES, JUNIORBEES

**Why fixed watchlist:** Reduces noise from unfamiliar stocks, ensures you know the typical price behaviour and news calendar of your trading universe.

---

## 7. TradingView Pine Script

The accompanying Pine Script file (`connors_rsi2_swing_india.pine`) implements this strategy for backtesting on TradingView. Key features:

- Entry: RSI(2) < configurable threshold + price > 200 SMA
- Exit: Close > 5 SMA (canonical) or RSI(2) > configurable threshold
- Safety exit: max hold days
- Commission: 0.12% per side (approximates Indian STT + taxes)
- Stats table: win rate, profit factor, average trade, max drawdown

### How to Use

1. Open TradingView → Pine Editor → paste the script → Add to Chart.
2. Apply to **1D (daily) chart** of any NSE stock (start with NIFTYBEES).
3. Default settings: RSI(2) < 5 (canonical), exit on close > 5 SMA.
4. Check Strategy Tester tab for performance metrics.
5. Test across 4–5 different stocks to verify consistency.

### Interpreting Results

| Metric | Healthy Range | Red Flag |
|--------|--------------|----------|
| Win rate | > 60% | < 50% |
| Profit factor | > 1.5 | < 1.0 |
| Avg trade P/L | > ₹50 (per ₹1L position) | Negative |
| Max drawdown | < 25% of equity | > 35% |
| Total trades | 10–25 per year per stock | < 5 (insufficient data) |

---

## 8. Expected Performance

### 8.1 Typical Metrics

| Metric | Range |
|--------|-------|
| Win rate | 60–75% |
| Average win | +1.5% to +3.0% |
| Average loss | –2% to –5% (rare but larger) |
| Profit factor | 1.5–3.5 |
| Trades per stock per year | 10–25 |
| Holding period | 2–7 days |
| Time in market | 5–15% |
| CAGR (index backtest) | 2.7–8.5% (low due to low time-in-market) |

### 8.2 Strategy Character

This is a **high-probability, small-frequent-gain** system. The equity curve rises in small steps with occasional larger dips. Psychologically, this suits traders who prefer being right often (60–75% of trades) over traders who prefer large individual wins.

The opposite profile would be trend-following: 30–40% win rate, large occasional winners, many small losses. Both can be profitable — the question is which you can psychologically sustain.

### 8.3 Compounding the Edge

The strategy's real power comes from **capital efficiency.** Since time-in-market is only 5–15%, your capital is free for other use 85–95% of the time. You can:

- Run this strategy across 3–5 stocks simultaneously
- Keep idle capital in liquid funds / overnight funds (earning 6–7% annually)
- Combine with an ORB (Opening Range Breakout) intraday strategy for daily engagement
- The combined approach gives you both daily income (ORB) and occasional high-conviction swings (RSI(2))

---

## 9. Market Regime Guide

### 9.1 When to Trade

| Market Condition | Trade? | Expected Performance |
|-----------------|--------|---------------------|
| Strong bull trend (Nifty > 200 SMA, rising) | **Yes — actively** | Excellent. Frequent pullback opportunities, highest win rate. |
| Mild uptrend / early bull | **Yes** | Good. Fewer signals but clean setups. |
| Sideways / range-bound | **Yes, cautiously** | Moderate. Lower win rate, tighter TPs recommended. |
| Volatile correction within bull | **Yes — best single trades** | Extreme RSI(2) readings produce the largest bounces. |
| Bear market (Nifty < 200 SMA) | **No — strategy is inactive** | The 200 SMA filter automatically stops trading. |

### 9.2 When NOT to Trade

- Nifty is below its 200 SMA (macro downtrend)
- India VIX > 30 (extreme fear — gaps can be catastrophic)
- Budget day, RBI policy day, election results day (event risk overrides technicals)
- Stock has pending earnings/results within 3 days
- Stock shows continuous lower lows (structural breakdown, not pullback)
- You are holding 5+ positions already (over-concentration)

### 9.3 Combining With Market Breadth

For advanced practitioners, add a market breadth filter:

- **% of Nifty 50 stocks above their 200 SMA > 60%** → market is healthy, trade normally
- **% between 40–60%** → market is mixed, reduce position sizes to 10–15%
- **% below 40%** → market is weak, avoid new entries

This can be tracked via the Nifty 50 breadth indicator on TradingView or calculated from NSE data.

---

## 10. Risks & Limitations

### 10.1 No Stop-Loss Risk

Individual drawdowns can reach –5% to –10% before the safety exit triggers. This is the cost of the high win rate — the strategy accepts occasional larger losses to avoid cutting winners early. **Mitigation:** position sizing (15–20% max per trade) and diversification (3–5 stocks).

### 10.2 Gap Risk (India-Specific)

Indian markets are closed for 17.75 hours each night. Global events (US Fed decisions, geopolitical crises, commodity shocks) can cause gap-downs at the next open that bypass all exit levels. **Mitigation:** avoid holding through known event dates, diversify across sectors.

### 10.3 Survivorship Bias

The strategy works best on stocks that remain in an uptrend. Stocks that appear to pull back but are actually breaking down (entering a structural downtrend) will produce losses. **Mitigation:** the 200 SMA filter, the safety exit, and manual verification of the pullback quality (clean dip vs. breakdown).

### 10.4 Regime Dependency

Mean reversion strategies underperform in persistent downtrends and when correlation spikes (all stocks fall together). The 200 SMA filter handles this by automatically deactivating the strategy in bear markets — but the transition period (from bull to bear) can produce a cluster of losing trades before the filter kicks in.

### 10.5 Psychological Risk

The hardest part of this strategy is holding through a losing trade without a stop-loss. When a stock drops –3% after entry and RSI(2) is at 1, the correct action is to hold (or even add) — not panic-sell. If you cannot tolerate this, add a wide stop at –8% and accept the performance drag.

### 10.6 Short Delivery Risk (BTST/T+1 Sells)

If you sell on T+1 (the day after buying, before shares hit your demat), you face short-delivery risk. Since this strategy holds 2–7 days, shares will typically settle before you sell. If you exit on day 1, be aware of the BTST settlement risk and potential auction penalties.

---

## 11. Optimisation Guidelines

### 11.1 RSI Threshold

| RSI Level | Effect | Trades/Year (per stock) |
|-----------|--------|------------------------|
| < 2 | Ultra-selective, highest avg profit/trade | 3–8 |
| < 5 | **Canonical (recommended)** | 10–20 |
| < 10 | More trades, lower avg profit/trade | 20–40 |
| < 15 | Too many low-conviction signals | 40+ |

**Recommendation:** Start with < 5. If you want more action, move to < 10. Never go above < 15.

### 11.2 Exit Tuning

| Style | TP1 | TP2 | Exit SMA |
|-------|-----|-----|----------|
| Conservative | +1.0% | +2.0% | 5 SMA |
| Balanced (default) | +1.2% | +2.8% | 5 SMA |
| Aggressive | +1.5% | +3.5% | 5 SMA |
| Extended hold | — | — | RSI(2) > 70 exit |

The **RSI(2) > 70 exit** variant holds longer (5–10 days) and captures larger moves, but with more drawdown exposure. Test both in backtesting.

### 11.3 Robustness Testing

To verify you haven't curve-fitted:

1. **Cross-stock test:** results should be positive across NIFTYBEES, RELIANCE, TCS, HDFCBANK, and INFY. If only one stock works, it's noise.
2. **Parameter sensitivity:** shift RSI threshold from 5 to 3 and 8. Shift exit SMA from 5 to 3 and 7. If results collapse, the signal is fragile.
3. **Out-of-sample:** if you have 10 years of data, optimise on years 1–7 and test on years 8–10.
4. **Regime split:** check performance separately in bull years and flat/down years. The strategy should be profitable (even if less so) in flat markets and inactive (not losing) in bear markets.

---

## 12. Trade Journal Template

Maintain this log for every trade. Essential for performance review and ITR filing.

### Per-Trade Log

| Field | Example |
|-------|---------|
| Date (entry) | 2026-03-15 |
| Stock | HDFCBANK |
| Entry price | ₹1,542.30 |
| RSI(2) at entry | 3.8 |
| Position size | ₹2,00,000 (20% of capital) |
| Shares | 129 |
| Date (exit) | 2026-03-19 |
| Exit price | ₹1,578.50 |
| Exit reason | Close > 5 SMA |
| P/L (₹) | +₹4,670 |
| P/L (%) | +2.35% |
| Days held | 4 |
| Costs (STT+taxes) | ~₹896 |
| Net P/L | +₹3,774 |
| Notes | Clean pullback after sector rotation, bounce on above-avg volume |

### Monthly Summary

| Metric | Value |
|--------|-------|
| Total trades | |
| Winners / Losers | |
| Win rate | |
| Gross P/L | |
| Total costs | |
| Net P/L | |
| Avg hold period | |
| Max drawdown (single trade) | |
| Max drawdown (portfolio) | |

---

## 13. Frequently Asked Questions

**Q: Can I use this on Nifty/BankNifty futures instead of delivery?**
A: Yes — and it's cheaper (no STT on buy side for futures). The same RSI(2) logic works on NIFTY futures daily chart. The advantage is lower cost; the disadvantage is margin requirement (~₹1.5L) and overnight margin risk.

**Q: What if RSI(2) < 5 triggers on a Friday?**
A: Academic research shows the Friday-to-Monday overnight return is negative on average for Indian markets. You can skip Friday entries or accept the slight drag. The strategy document treats this as optional.

**Q: Can I combine this with intraday trading?**
A: Yes — this is ideal for combination with an ORB (Opening Range Breakout) intraday strategy. ORB handles your daily engagement on Nifty/BankNifty futures; RSI(2) swing handles your 3–7 day delivery positions. The two use different edges (momentum vs. mean reversion) and are naturally uncorrelated.

**Q: What about F&O strategies — selling puts on RSI(2) signals?**
A: Selling OTM puts on RSI(2) < 5 days captures theta decay plus directional recovery. This is a valid advanced approach but requires F&O margin (₹1.5–3L) and carries tail risk (gap-down through your strike). Only for experienced options traders.

**Q: Why not use RSI(14) instead of RSI(2)?**
A: RSI(14) captures multi-week trends and is too slow for 3–7 day mean reversion. RSI(2) captures 1–2 day panic selling specifically. Connors tested both and found RSI(2) substantially outperforms RSI(14) for short-term mean-reversion strategies.

**Q: How does this strategy handle stock splits, bonuses, and dividends?**
A: TradingView adjusts historical data for corporate actions automatically. In live trading, if a stock goes ex-dividend or ex-bonus while you're in a trade, your effective entry price adjusts accordingly. No special action needed.

**Q: Is there a screener that sends alerts when RSI(2) < 5?**
A: Yes — both Chartink (paid tier) and TradingView (Pro tier) support custom alerts. Set up the scanner from section 6 and enable email/push notifications for end-of-day signals.

---

## 14. References & Further Reading

### Books

- **Larry Connors & Cesar Alvarez** — *Short Term Trading Strategies That Work* (2008). The original RSI(2) research.
- **Larry Connors & Cesar Alvarez** — *High Probability ETF Trading* (2009). R3 strategy and cumulative RSI variations.
- **Larry Connors** — *Buy the Fear, Sell the Greed: 7 Behavioral Quant Strategies for Traders*. Updated perspectives on mean reversion.

### Academic Papers

- **Lou, Polk & Skouras (2019)** — "A Tug of War: Overnight Versus Intraday Expected Returns." *Journal of Financial Economics*, 134(2), 192–213. Documents that all momentum profits accrue overnight.
- **Knuteson (2022)** — "They Still Haven't Told You." *arXiv:2201.00223*. Comprehensive global analysis of overnight vs. intraday returns including India's SENSEX.
- **Boyarchenko, Larsen & Whelan (2023)** — "The Overnight Drift." *NY Fed Staff Report 917*. Explains overnight premium through dealer inventory management.

### Online Resources

- **QuantifiedStrategies.com** — Free backtests of Connors' strategies, updated regularly. See their Larry Connors strategy collection: `quantifiedstrategies.com/larry-connors/`
- **StockCharts.com RSI(2) Guide** — ChartSchool article with visual examples: `chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/rsi-2`
- **StockManiacs.net** — Indian-market-specific RSI(2) application: `stockmaniacs.net/rsi-2-strategy/`
- **Trade2Win Forum** — 34-year Python backtest with detailed results: `trade2win.com/threads/backtest-results-for-connors-rsi2-strategy.242688/`
- **TradingView Community** — Search for "Connors RSI" and "Gap Reversion Strategy by TradeAutomation" for open-source implementations.

### Tools

- **Chartink** — `chartink.com/screener` — Free NSE stock screener with custom technical filters.
- **TradingView** — `tradingview.com` — Pine Script backtesting, charting, and alerts.
- **Zerodha Varsity** — `zerodha.com/varsity` — Free modules on technical analysis, F&O, and taxation for Indian markets.
- **NSE India** — `nseindia.com` — Official market data, corporate actions calendar, and India VIX.

---

## Appendix A: Quick-Reference Decision Flowchart

```
START: Market close (3:30 PM scan)
  │
  ├─ Is Nifty above its 200 SMA?
  │   ├─ NO  → Do not trade. Wait.
  │   └─ YES → Continue
  │
  ├─ Run scanner: any stock with RSI(2) < 5?
  │   ├─ NO  → No trade today. Done.
  │   └─ YES → Review candidates
  │
  ├─ For each candidate:
  │   ├─ Stock above 200 SMA?         → YES: continue / NO: skip
  │   ├─ Volume > 5 lakh?             → YES: continue / NO: skip
  │   ├─ No earnings within 3 days?   → YES: continue / NO: skip
  │   ├─ Clean pullback (not breakdown)? → YES: continue / NO: skip
  │   └─ Already holding 5 positions? → YES: skip / NO: enter
  │
  ├─ ENTER: Buy at close, CNC, 15-20% of capital
  │
  └─ HOLD & MONITOR DAILY:
      ├─ Close > 5 SMA?     → EXIT (primary)
      ├─ Profit > +1.2%?    → EXIT 50% (TP1)
      ├─ Profit > +2.5-3%?  → EXIT remaining (TP2)
      ├─ Close < 200 SMA?   → EXIT immediately (safety)
      ├─ Held 10-12 days?   → EXIT (time limit)
      └─ None of above?     → HOLD. Do nothing.
```

---

## Appendix B: Companion Strategies

This document focuses on RSI(2) swing trading. Two companion strategies pair well:

1. **Opening Range Breakout (ORB)** — Intraday strategy on Nifty/BankNifty futures. Trades the breakout of the first 15/30-minute candle. Provides daily engagement and income whilst RSI(2) swing positions are idle.

2. **Relative Strength Momentum** — Weekly/monthly strategy. Buy stocks with highest 3–6 month relative strength on pullback to 20 EMA. Holds 1–4 weeks. Captures sector rotation trends.

Together, these three strategies cover intraday (ORB), short swing (RSI(2)), and medium swing (RS momentum) — three different timeframes, three different edges, natural diversification.

---

*Document version: 1.0 · Date: March 2026 · Not financial advice. Backtest any strategy thoroughly before deploying capital. Past performance does not guarantee future results.*
