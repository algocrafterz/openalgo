# ORB Signal Performance Analysis - Q1 2026

**Period:** Jan 28 - Mar 11, 2026 (31 trading days)
**Source:** Telegram channel `orb_channel` export
**Strategy:** orb-strategy-luxy-tg (ORB15, 5-min chart)
**Configuration at time of analysis:**
- TP levels enabled: TP1, TP1.5 (TP2, TP3 NOT enabled)
- Stop mode: Smart Adaptive / Scaled ATR
- Entry cutoff: 11:00 AM IST
- Time exit: 14:30 IST
- Gap filter: 2.5% max
- Min entry time: 9:45 AM
- ORB range filter: 0.4% - 3.5%
- Commission: 0.03% (corrected to 0.06% on 2026-03-12)
- Index filter: NOT enabled (added on 2026-03-12)

---

## Overall Performance

| Metric | Value |
|--------|-------|
| Total entries | 145 |
| Total exits | 196 |
| Win Rate | **67.3%** (132/196) |
| Cumulative PnL | **+54.89%** (sum of individual trade %) |
| Avg PnL/trade | +0.280% |
| Avg win | +0.725% |
| Avg loss | -0.660% |
| Win/Loss ratio | 1.10 |
| Expectancy/trade | +0.280% |
| Winning days | 24/31 (77.4%) |
| Losing days | 7/31 (22.6%) |
| Max win streak | 20 |
| Max loss streak | 6 |
| Avg signals/day | 4.7 |

## Direction Performance

| Direction | Trades | Wins | Losses | TE | WR% | Total PnL | Avg PnL |
|-----------|--------|------|--------|-----|------|-----------|---------|
| LONG | 109 | 69 | 39 | 1 | 63.3% | +28.81% | +0.264% |
| SHORT | 87 | 63 | 24 | 0 | 72.4% | +26.08% | +0.300% |

## TP Hit Distribution

| Exit Type | Count | % of Total |
|-----------|-------|------------|
| TP1 | 78 | 39.8% |
| TP1.5 | 54 | 27.6% |
| SL | 63 | 32.1% |
| TIME_EXIT | 1 | 0.5% |

**Note:** TP2 and TP3 were NOT enabled during this period. Unknown how many trades would have reached TP2/TP3.

## Entry Time Distribution

| Hour | Count | % |
|------|-------|---|
| 09:xx | 47 | 32.4% |
| 10:xx | 98 | 67.6% |

## R:R Ratio Distribution

| R:R Range | Count |
|-----------|-------|
| 0.8-1.0 | 21 |
| 1.0-1.2 | 11 |
| 1.2-1.5 | 113 |
| Average | 1:1.40 |
| Median | 1:1.50 |

## Risk Per Trade

| Metric | Value |
|--------|-------|
| Min risk % | 0.30% |
| Max risk % | 1.43% |
| Avg risk % | 0.64% |
| Median | 0.62% |

## Day-of-Week Performance

| Day | Trades | Wins | Losses | WR% | PnL% |
|-----|--------|------|--------|------|-------|
| Mon | 30 | 22 | 8 | 73.3% | +8.80% |
| Tue | 29 | 22 | 7 | 75.9% | +11.20% |
| Wed | 58 | 40 | 18 | 69.0% | +19.86% |
| Thu | 47 | 31 | 16 | 66.0% | +12.73% |
| **Fri** | **30** | **16** | **13** | **55.2%** | **+1.60%** |

## Daily Performance Log

| Date | # | W | L | TE | PnL% | Result |
|------|---|---|---|-----|-------|--------|
| 2026-01-28 | 3 | 3 | 0 | 0 | +1.56% | + |
| 2026-01-29 | 1 | 1 | 0 | 0 | +0.53% | + |
| 2026-01-30 | 4 | 0 | 3 | 1 | -1.90% | - |
| 2026-02-01 | 2 | 1 | 1 | 0 | +0.70% | + |
| 2026-02-02 | 5 | 4 | 1 | 0 | +3.20% | + |
| 2026-02-03 | 2 | 1 | 1 | 0 | +0.40% | + |
| 2026-02-04 | 5 | 1 | 4 | 0 | -1.20% | - |
| 2026-02-05 | 3 | 2 | 1 | 0 | +0.10% | + |
| 2026-02-06 | 6 | 6 | 0 | 0 | +3.00% | + |
| 2026-02-09 | 6 | 5 | 1 | 0 | +2.40% | + |
| 2026-02-10 | 1 | 0 | 1 | 0 | -0.60% | - |
| 2026-02-11 | 8 | 5 | 3 | 0 | +1.50% | + |
| 2026-02-12 | 10 | 6 | 4 | 0 | +2.10% | + |
| 2026-02-13 | 3 | 2 | 1 | 0 | +1.30% | + |
| 2026-02-16 | 8 | 8 | 0 | 0 | +6.00% | + |
| 2026-02-17 | 18 | 16 | 2 | 0 | +9.80% | + |
| 2026-02-18 | 8 | 5 | 3 | 0 | +1.50% | + |
| 2026-02-19 | 11 | 6 | 5 | 0 | +0.90% | + |
| 2026-02-20 | 6 | 3 | 3 | 0 | +0.20% | + |
| 2026-02-23 | 6 | 5 | 1 | 0 | +2.40% | + |
| 2026-02-24 | 5 | 3 | 2 | 0 | +1.00% | + |
| 2026-02-25 | 10 | 9 | 1 | 0 | +7.20% | + |
| 2026-02-26 | 9 | 7 | 2 | 0 | +3.30% | + |
| 2026-02-27 | 7 | 5 | 2 | 0 | +1.90% | + |
| 2026-03-02 | 3 | 0 | 3 | 0 | -3.30% | - |
| 2026-03-04 | 13 | 12 | 1 | 0 | +10.40% | + |
| 2026-03-05 | 13 | 9 | 4 | 0 | +5.80% | + |
| 2026-03-06 | 4 | 0 | 4 | 0 | -2.90% | - |
| 2026-03-09 | 2 | 0 | 2 | 0 | -1.90% | - |
| 2026-03-10 | 3 | 2 | 1 | 0 | +0.60% | + |
| 2026-03-11 | 11 | 5 | 6 | 0 | -1.10% | - |

## Per-Symbol Performance

| Symbol | W | L | TE | WR% | CumPnL% | AvgPnL% | Grade |
|--------|---|---|-----|------|---------|---------|-------|
| NATIONALUM | 9 | 1 | 0 | 90.0% | +8.70% | +0.87% | A |
| APOLLOTYRE | 9 | 1 | 0 | 90.0% | +4.90% | +0.49% | A |
| PFC | 6 | 1 | 0 | 85.7% | +4.50% | +0.64% | A |
| CANBK | 6 | 1 | 0 | 85.7% | +4.10% | +0.59% | A |
| TMPV | 5 | 0 | 0 | 100.0% | +3.90% | +0.78% | A |
| FEDERALBNK | 5 | 1 | 1 | 71.4% | +3.71% | +0.53% | B |
| WIPRO | 6 | 2 | 0 | 75.0% | +3.50% | +0.44% | B |
| RECLTD | 5 | 1 | 0 | 83.3% | +3.20% | +0.53% | A |
| SBIN | 8 | 1 | 0 | 88.9% | +3.10% | +0.34% | A |
| ZYDUSLIFE | 4 | 0 | 0 | 100.0% | +3.10% | +0.77% | A |
| ASHOKLEY | 5 | 1 | 0 | 83.3% | +3.00% | +0.50% | A |
| LICHSGFIN | 4 | 0 | 0 | 100.0% | +2.50% | +0.62% | A |
| BALRAMCHIN | 5 | 4 | 0 | 55.6% | +2.43% | +0.27% | C |
| PNB | 4 | 1 | 0 | 80.0% | +2.00% | +0.40% | A |
| EXIDEIND | 3 | 0 | 0 | 100.0% | +1.70% | +0.57% | A |
| BIOCON | 3 | 2 | 0 | 60.0% | +1.63% | +0.33% | B |
| JINDALSTEL | 5 | 4 | 0 | 55.6% | +1.40% | +0.16% | C |
| HINDALCO | 2 | 0 | 0 | 100.0% | +1.40% | +0.70% | A |
| ITC | 5 | 2 | 0 | 71.4% | +1.32% | +0.19% | B |
| BANKBARODA | 3 | 1 | 0 | 75.0% | +1.20% | +0.30% | B |
| USHAMART | 4 | 5 | 0 | 44.4% | +1.00% | +0.11% | C |
| JSWENERGY | 2 | 1 | 0 | 66.7% | +0.80% | +0.27% | B |
| EMAMILTD | 4 | 2 | 0 | 66.7% | +0.70% | +0.12% | B |
| TATASTEEL | 3 | 2 | 0 | 60.0% | +0.70% | +0.14% | B |
| BPCL | 3 | 2 | 0 | 60.0% | +0.40% | +0.08% | B |
| HINDPETRO | 2 | 2 | 0 | 50.0% | +0.20% | +0.05% | C |
| DLF | 2 | 2 | 0 | 50.0% | -0.10% | -0.02% | C |
| TATAPOWER | 1 | 2 | 0 | 33.3% | -0.10% | -0.03% | D |
| VBL | 1 | 1 | 0 | 50.0% | -0.30% | -0.15% | C |
| ADANIPOWER | 2 | 2 | 0 | 50.0% | -0.30% | -0.08% | C |
| JIOFIN | 1 | 2 | 0 | 33.3% | -0.40% | -0.13% | D |
| GAIL | 1 | 2 | 0 | 33.3% | -0.50% | -0.17% | D |
| MANAPPURAM | 1 | 5 | 0 | 16.7% | -3.20% | -0.53% | D |
| BHEL | 0 | 5 | 0 | 0.0% | -4.10% | -0.82% | D |

### Grading Key
- **A**: WR >= 80% AND positive PnL (core watchlist)
- **B**: WR >= 60% AND positive PnL (keep, monitor)
- **C**: WR >= 40% OR marginal PnL (watch closely, may remove)
- **D**: WR < 40% OR significant negative PnL (remove candidate)

---

## Changes Applied on 2026-03-12

1. **Commission corrected**: 0.03% -> 0.06% (mStock actual costs)
2. **Slippage corrected**: 1 -> 2 ticks
3. **NIFTY Index Direction Filter added**: Blocks entries when 2+ of 3 filters (Trend, HTF, Index) oppose direction

## TP Strategy Analysis (2026-03-12)

Simulated net PnL across 196 trades under different TP strategies using actual Q1 exit distribution (TP1=78, TP1.5=54, SL=63, TE=1):

| Strategy | Net PnL | Per Trade | Win Rate |
|----------|---------|-----------|----------|
| **TP1 only (1R)** | **+69.0R** | **+0.352R** | **67.3%** |
| 50-50 split + trail SL to BE | +43.5R | +0.222R | — |
| TP1.5 + trail SL to BE at TP1 | +18.0R | +0.092R | 27.6% |
| 50-50 split, no trail | +4.5R | +0.023R | — |
| TP1.5 only, no trail | -60.0R | -0.306R | 27.6% |

### Conclusions

1. **TP1 (1R) is the optimal target** — highest net PnL by a wide margin. High win rate (67.3%) with 1:1 R:R outperforms all alternatives.
2. **TP1.5 alone without trailing SL loses money** (-60R). The 78 trades that hit TP1 but not TP1.5 would become full losses.
3. **Trailing SL to breakeven adds no value with TP1 strategy** — once the TP1 LIMIT fills, the trade is done. No remaining position to trail.
4. **Trailing SL only makes sense with higher TP targets or partial exits** — revisit if strategy evolves to split exits.
5. **PineScript already sends TP1 in alerts** — signal engine uses signal.tp as-is, no TP computation needed in the executor.

### Design Decision

Signal engine is a **dumb executor** — it does not compute or override TP/SL levels. All trade logic (TP level selection, R:R calculation) stays in the PineScript strategy. Signal engine handles: parsing, validation, sizing, order placement, and position tracking only.

## Capital Simulation — TP1 Only (2026-03-13)

Based on Q1 data: 196 trades over 31 trading days (~1.5 months), TP1 only (+69R gross).

**Assumptions:**
- Sizing: fixed_fractional, risk_per_trade = 1% of capital
- Avg SL distance: 0.64% of entry price (from Q1 data)
- Avg position value per trade: ~1.56x capital (= 1% risk / 0.64% SL distance)
- Commission: 0.06% round trip (mStock actual costs)
- Slippage: ~2 ticks round trip (MARKET orders) = ~0.10R per trade
- Product: MIS (5x leverage, 20% margin)
- Max open positions: 5 (from config)

### Cost Breakdown (per trade)

| Cost Component | As % of Position | As R-multiple |
|----------------|-----------------|---------------|
| Commission (0.06% RT) | 0.06% | 0.094R |
| Slippage (~2 ticks RT) | ~0.065% | ~0.10R |
| **Total per trade** | **~0.125%** | **~0.19R** |

### Net Return Calculation

| Component | R-multiples | At 1% risk/trade |
|-----------|-------------|-------------------|
| Gross P&L (132W - 63L) | +69.0R | +69.0% |
| Commission (196 trades) | -18.4R | -18.4% |
| Slippage (196 trades) | -19.6R | -19.6% |
| **Net P&L** | **+31.0R** | **+31.0%** |

### Capital vs Absolute Returns (TP1 Only, Net of Costs)

| Capital (INR) | Risk/Trade | Net P&L (31R) | Monthly (~20R) | Max DD (6R) | Margin/Trade (20%) |
|--------------:|-----------:|--------------:|---------------:|------------:|--------------------:|
| 5,000 | 50 | 1,550 | ~1,000 | -300 | ~1,560 |
| 10,000 | 100 | 3,100 | ~2,000 | -600 | ~3,125 |
| 25,000 | 250 | 7,750 | ~5,000 | -1,500 | ~7,813 |
| 50,000 | 500 | 15,500 | ~10,000 | -3,000 | ~15,625 |
| 75,000 | 750 | 23,250 | ~15,000 | -4,500 | ~23,438 |
| **100,000** | **1,000** | **31,000** | **~20,000** | **-6,000** | **~31,250** |
| 200,000 | 2,000 | 62,000 | ~40,000 | -12,000 | ~62,500 |
| 300,000 | 3,000 | 93,000 | ~60,000 | -18,000 | ~93,750 |
| 500,000 | 5,000 | 155,000 | ~100,000 | -30,000 | ~156,250 |

### Approx Net Rate of Return

| Period | Net Return % | Notes |
|--------|-------------|-------|
| Q1 (31 trading days) | **~31%** | 196 trades, 67.3% WR |
| Per month (~21 trading days) | **~20%** | ~130 trades/month |
| Per week (~5 trading days) | **~5%** | ~32 trades/week |
| Per trade (avg) | **+0.16R** | Net after all costs |

### Key Observations

1. **Net return % is constant regardless of capital** — fixed_fractional sizing scales linearly. Capital only changes absolute INR.
2. **Practical minimum: 50,000 INR** — below this, lot sizes on 100-800 INR stocks become too small for meaningful execution.
3. **Sweet spot: 100,000 INR** — gives 31K net over Q1, 1000 INR risk/trade, comfortable lot sizes across the stock universe.
4. **Max drawdown estimate: 6R (6%)** — worst streak was 6 consecutive losses. Daily loss limit (4%) would lock out after 4 losses in a single day.
5. **Margin constraint: at 5 max positions, margin needed = 5 x ~31% of capital = ~156%** — but MIS 5x leverage means actual margin = ~31% of capital per position. With max_capital_utilization at 80%, this limits effective concurrent positions.
6. **Slippage is the dominant cost** (~0.10R/trade) — consider LIMIT orders for entry to reduce this by ~50%.

## Baseline for Future Comparison

Use these metrics as baseline when evaluating improvements:
- **Win Rate baseline**: 67.3%
- **Expectancy baseline**: +0.280%/trade
- **Winning days baseline**: 77.4%
- **Long WR baseline**: 63.3%
- **Short WR baseline**: 72.4%
- **Friday WR baseline**: 55.2%

## Slippage & Cost Optimization Analysis (2026-03-13)

### Cost Structure Breakdown

| Cost Component | Per Trade (R) | Total (196 trades) | % of Gross |
|---------------|--------------|--------------------|-----------:|
| Commission (0.06% RT, mStock) | 0.094R | 18.4R | 26.7% |
| Slippage (MARKET, ~2 ticks RT) | 0.10R | 19.6R | 28.4% |
| **Total costs** | **0.19R** | **38.0R** | **55.1%** |
| **Net retained** | **0.16R** | **31.0R** | **44.9%** |

### Decision: Keep MARKET Orders for Entry

**LIMIT orders are NOT suitable for ORB breakout entries.** Reasons:

1. **Momentum strategy** — price moves away from entry on successful breakouts
2. **Telegram relay latency** — 2-7 seconds from PineScript alert to exchange, price already past entry
3. **Adverse selection** — LIMIT fills mainly on failed breakouts (worst trades), misses the best ones
4. **Estimated LIMIT fill rate: 30-50%** — losing half the trades destroys the edge
5. **Adding tick buffer to LIMIT** just converts random slippage to fixed — no net gain

### Higher-Impact Optimizations (ranked)

| Priority | Action | Est. Impact | Status |
|----------|--------|-------------|--------|
| 1 | **Remove D-grade symbols** (BHEL -4.1%, MANAPPURAM -3.2%) | +7.3R saved | Pending |
| 2 | **Cache capital fetch** (30s TTL, avoid HTTP per signal) | -0.1 to -0.3s latency | Pending |
| 3 | **Increase slippage_factor** 0.05 -> 0.10 (honest risk accounting) | Better sizing accuracy | Pending |
| 4 | **Direct TradingView webhook** (bypass Telegram) | -2 to -4s latency, ~30-50% slippage reduction | Deferred (high effort, loses audit trail) |

### Key Insight

**Signal quality > slippage reduction.** Removing 2 D-grade symbols (BHEL, MANAPPURAM) recovers ~7.3R — nearly equivalent to eliminating ALL slippage. The watchlist filter is the highest-ROI improvement.

### Latency Chain (signal to exchange)

| Step | Latency |
|------|---------|
| PineScript alert fires (bar close) | — |
| TradingView -> Telegram | ~1-3s |
| Telegram API -> Telethon client | ~0.5-2s |
| Signal engine pipeline (parse/validate/size) | ~0.2-0.5s |
| HTTP POST to OpenAlgo API | ~0.1-0.3s |
| OpenAlgo -> broker (mStock) | ~0.1-0.5s |
| Broker -> exchange | ~0.05-0.2s |
| **Total** | **~2-7s** |

## Capital Allocation & Equal Risk Design (2026-03-20)

### Problem: Tapering Risk

With `fixed_fractional` sizing using live available capital, each trade fetches current capital from the broker API. As positions open and margin is blocked, available capital shrinks — causing later trades to get smaller position sizes and lower risk:

| Trade# | Available Capital | Risk (1%) | Relative Risk |
|--------|------------------|-----------|:---:|
| 1 | ₹15,000 | ₹150 | 100% |
| 2 | ₹10,364 | ₹103 | 69% |
| 3 | ₹5,742 | ₹57 | 38% |

Trade 1 carries 3x the risk of trade 3. Day's P&L is dominated by which trades fire first — **sequence luck, not signal quality**.

### Solution: Day-Start Capital Caching

`use_day_start_capital: true` in config.yaml:

1. First signal of the day fetches capital from broker API (e.g., ₹15,000)
2. This value is cached as `_day_start_capital`
3. All subsequent trades use the cached value for position sizing
4. Resets automatically on new trading day

Every trade risks exactly 1% of ₹15,000 = ₹150, regardless of order or concurrent positions.

### Margin Constraint: 3 Concurrent Positions

With ₹15K capital and MIS (5x / 20% margin), each ₹380 stock trade needs ~₹4,600 margin:
- 3 trades: ~₹13,800 margin (fits in ₹15K)
- 5 trades: ~₹23,000 margin (exceeds ₹15K, broker rejects)

**Decision**: `max_open_positions: 3` — ensures margin fits. Slots recycle when positions close (TP/SL hit), so total daily trades can exceed 3 within the `max_trades_per_day: 8` safety cap.

### Signal Quality Supports 3 Concurrent Slots

From Mar 12-18 Telegram data (5-day sample):
- Signals 1-2: **90% WR** (9W/1L) — strongest ORB breakouts
- Signals 3+: **57% WR** (4W/3L) — weaker, later entries

Taking fewer concurrent positions naturally filters for the best signals.

### Configuration (2026-03-20)

```yaml
sizing:
  mode: fixed_fractional
  risk_per_trade: 0.01           # 1% risk, equal for all trades
  use_day_start_capital: true    # Cache capital at first signal of day

risk:
  max_open_positions: 3          # 3 concurrent slots (margin fits ₹15K MIS)
  max_trades_per_day: 8          # Safety cap (slots recycle within this)
  max_portfolio_heat: 0.04       # 3 concurrent at 1% + buffer
  daily_loss_limit: 0.04         # 4% daily loss lockout
```

### Scaling

The design works at any capital level — only `max_open_positions` may need adjustment:

| Capital | Risk/Trade (1%) | Margin/Trade (avg) | Max Concurrent | Adjust? |
|--------:|----------------:|-------------------:|:-:|:-:|
| ₹15K | ₹150 | ~₹4,600 | 3 | Current |
| ₹25K | ₹250 | ~₹7,800 | 3 | Could go to 4 |
| ₹50K | ₹500 | ~₹15,600 | 3 | Could go to 5 |
| ₹1L | ₹1,000 | ~₹31,200 | 3 | Could go to 5 |
