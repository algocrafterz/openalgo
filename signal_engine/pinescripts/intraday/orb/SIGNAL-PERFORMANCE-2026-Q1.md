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

## Baseline for Future Comparison

Use these metrics as baseline when evaluating improvements:
- **Win Rate baseline**: 67.3%
- **Expectancy baseline**: +0.280%/trade
- **Winning days baseline**: 77.4%
- **Long WR baseline**: 63.3%
- **Short WR baseline**: 72.4%
- **Friday WR baseline**: 55.2%
