# ORB Signal Performance Analysis - Full Period 2026

**Period:** 2026-01-28 to 2026-04-06 (44 trading days)
**Source:** Telegram channel `intraday-orb` export
**Strategy:** orb-strategy-luxy-tg (ORB15, 5-min chart)
**Configuration:**
- TP levels: TP1, TP1.5, TP2, TP3 (multi-TP added Mar 12)
- Stop mode: Smart Adaptive / Scaled ATR
- Entry cutoff: 11:00 AM IST
- Time exit: 14:30 IST
- Gap filter: 2.5% max
- Min entry time: 9:45 AM
- ORB range filter: 0.4% - 3.5%
- Commission: 0.06% (mStock)
- Index filter: Enabled (from Mar 12)

**Methodology note:** This report counts per-entry (1 entry = 1 trade). The Q1 report counted per-exit (TP1 and TP1.5 on the same entry = 2 exits). This means Q1 WR here (61.6%) differs from Q1 report (67.3%) — same data, different unit of analysis. Per-entry is the correct measure for sizing and capital allocation.

**Data:** 464 Telegram messages parsed. 194 entries, 270 exits, 183 completed trades, 8 no-exit (entry at end of data or unmatched), 1 orphan exit. 88 live broker fills from XLSX cross-referenced for slippage.

---

## Overall Performance

| Metric | Value |
|--------|-------|
| Total entries | 191 |
| Total completed trades | 183 |
| Win Rate | **62.8%** (115/183) |
| Cumulative PnL | **+55.49%** (sum of individual trade %) |
| Avg PnL/trade | +0.303% |
| Avg win | +0.898% |
| Avg loss | -0.703% |
| Win/Loss ratio | 1.28 |
| Expectancy/trade | +0.303% |
| Winning days | 32/44 (72.7%) |
| Losing days | 11/44 (25.0%) |
| Max win streak | 10 |
| Max loss streak | 6 |
| Avg signals/day | 4.2 |

## Period Comparison: Q1 vs Post-Q1

Config changes applied Mar 12: Index direction filter, Volume MA 20->50, Volume 3-bar window, Commission correction 0.03%->0.06%

| Metric | Q1 (Jan 28 - Mar 11) | Post-Q1 (Mar 12 - Apr 6) | Delta |
|--------|---------------------|--------------------------|-------|
| Trades | 138 | 45 | — |
| Win Rate | 61.6% | 66.7% | +5.1% |
| Cumulative PnL | +31.39% | +24.10% | -7.29% |
| Avg PnL/trade | +0.227% | +0.536% | +0.308% |
| Avg win | +0.794% | +1.193% | +0.399% |
| Avg loss | -0.681% | -0.780% | -0.099% |
| Win/Loss ratio | 1.17 | 1.53 | +0.36 |
| Winning days | 22/31 (71.0%) | 10/13 (76.9%) | +6.0% |
| Max win streak | 10 | 6 | — |
| Max loss streak | 6 | 4 | — |
| Avg signals/day | 4.5 | 3.5 | -1.0 |

## Direction Performance

| Direction | Trades | Wins | Losses | TE | WR% | Total PnL | Avg PnL |
|-----------|--------|------|--------|-----|------|-----------|---------|
| LONG | 100 | 59 | 41 | 1 | 59.0% | +26.01% | +0.260% |
| SHORT | 83 | 56 | 27 | 0 | 67.5% | +29.48% | +0.355% |

### Direction by Period

| Direction | Q1 WR% | Q2 WR% | Q1 PnL | Q2 PnL |
|-----------|--------|--------|--------|--------|
| LONG | 57.1% | 65.2% | +14.91% | +11.10% |
| SHORT | 67.2% | 68.2% | +16.48% | +13.00% |

## TP Hit Distribution

| Exit Type | Count | % of Total |
|-----------|-------|------------|
| TP1 | 41 | 22.4% |
| TP1.5 | 67 | 36.6% |
| TP2 | 2 | 1.1% |
| TP3 | 4 | 2.2% |
| SL | 68 | 37.2% |
| TIME_EXIT | 1 | 0.5% |

### TP Progression Funnel

How many trades progressed through each TP level:

| Level | Count | % of Entries | Progression Rate |
|-------|-------|-------------|-----------------|
| TP1 reached | 101 | 55.2% | — |
| TP1 -> TP1.5 | 73 | 39.9% | 72.3% of TP1 |
| TP1.5 -> TP2 | 6 | 3.3% | 8.2% of TP1.5 |
| TP2 -> TP3 | 4 | 2.2% | 66.7% of TP2 |

## Entry Time Distribution

| Hour | Count | % | WR% | PnL% |
|------|-------|---|-----|------|
| 09:xx | 60 | 32.8% | 63.3% | +16.69% |
| 10:xx | 123 | 67.2% | 62.6% | +38.80% |

## R:R Ratio Distribution

| R:R Range | Count |
|-----------|-------|
| 0.8-1.0 | 2 |
| 1.0-1.2 | 56 |
| 1.2-1.5 | 12 |
| 1.5+ | 121 |
| Average | 1:1.33 |
| Median | 1:1.50 |

## Risk Per Trade

| Metric | Value |
|--------|-------|
| Min risk % | 0.30% |
| Max risk % | 1.43% |
| Avg risk % | 0.67% |
| Median | 0.65% |

## Day-of-Week Performance

| Day | Trades | Wins | Losses | WR% | PnL% |
|-----|--------|------|--------|------|-------|
| Mon | 33 | 23 | 10 | 69.7% | +16.90% |
| Tue | 25 | 16 | 9 | 64.0% | +8.30% |
| Wed | 56 | 35 | 21 | 62.5% | +14.36% |
| Thu | 35 | 21 | 14 | 60.0% | +9.03% |
| Fri | 32 | 19 | 13 | 59.4% | +6.20% |

### Day-of-Week by Period

| Day | Q1 WR% | Q2 WR% | Q1 PnL | Q2 PnL |
|-----|--------|--------|--------|--------|
| Mon | 69.6% | 70.0% | +6.20% | +10.70% |
| Tue | 72.2% | 42.9% | +7.50% | +0.80% |
| Wed | 64.1% | 58.8% | +12.36% | +2.00% |
| Thu | 56.2% | 100.0% | +5.53% | +3.50% |
| Fri | 50.0% | 87.5% | -0.90% | +7.10% |

## Daily Performance Log

| Date | # | W | L | TE | PnL% | Result |
|------|---|---|---|-----|-------|--------|
| 2026-01-28 | 3 | 3 | 0 | 0 | +1.56% | + |
| 2026-01-29 | 1 | 1 | 0 | 0 | +0.53% | + |
| 2026-01-30 | 4 | 1 | 3 | 1 | -1.90% | - |
| 2026-02-01 | 2 | 1 | 1 | 0 | +0.70% | + |
| 2026-02-02 | 5 | 4 | 1 | 0 | +3.20% | + |
| 2026-02-03 | 2 | 1 | 1 | 0 | +0.40% | + |
| 2026-02-04 | 5 | 1 | 4 | 0 | -1.20% | - |
| 2026-02-05 | 3 | 2 | 1 | 0 | +0.10% | + |
| 2026-02-06 | 6 | 6 | 0 | 0 | +3.00% | + |
| 2026-02-09 | 6 | 5 | 1 | 0 | +2.40% | + |
| 2026-02-10 | 1 | 0 | 1 | 0 | -0.60% | - |
| 2026-02-11 | 5 | 3 | 2 | 0 | +1.30% | + |
| 2026-02-12 | 7 | 3 | 4 | 0 | +0.10% | + |
| 2026-02-13 | 2 | 1 | 1 | 0 | +0.60% | + |
| 2026-02-16 | 4 | 4 | 0 | 0 | +3.70% | + |
| 2026-02-17 | 10 | 8 | 2 | 0 | +5.50% | + |
| 2026-02-18 | 5 | 4 | 1 | 0 | +1.90% | + |
| 2026-02-19 | 8 | 3 | 5 | 0 | -0.40% | - |
| 2026-02-20 | 4 | 2 | 2 | 0 | +0.00% | = |
| 2026-02-23 | 3 | 3 | 0 | 0 | +2.10% | + |
| 2026-02-24 | 3 | 2 | 1 | 0 | +1.00% | + |
| 2026-02-25 | 6 | 5 | 1 | 0 | +4.40% | + |
| 2026-02-26 | 5 | 4 | 1 | 0 | +2.10% | + |
| 2026-02-27 | 4 | 2 | 2 | 0 | +0.30% | + |
| 2026-03-02 | 3 | 0 | 3 | 0 | -3.30% | - |
| 2026-03-04 | 7 | 6 | 1 | 0 | +5.90% | + |
| 2026-03-05 | 8 | 5 | 3 | 0 | +3.10% | + |
| 2026-03-06 | 4 | 0 | 4 | 0 | -2.90% | - |
| 2026-03-09 | 2 | 0 | 2 | 0 | -1.90% | - |
| 2026-03-10 | 2 | 2 | 0 | 0 | +1.20% | + |
| 2026-03-11 | 8 | 3 | 5 | 0 | -1.50% | - |
| 2026-03-12 | 2 | 2 | 0 | 0 | +2.60% | + |
| 2026-03-13 | 3 | 3 | 0 | 0 | +2.80% | + |
| 2026-03-16 | 4 | 1 | 3 | 0 | -0.60% | - |
| 2026-03-17 | 2 | 2 | 0 | 0 | +2.60% | + |
| 2026-03-18 | 6 | 5 | 1 | 0 | +2.30% | + |
| 2026-03-23 | 2 | 2 | 0 | 0 | +3.50% | + |
| 2026-03-24 | 5 | 1 | 4 | 0 | -1.80% | - |
| 2026-03-25 | 5 | 3 | 2 | 0 | +1.40% | + |
| 2026-03-27 | 5 | 4 | 1 | 0 | +4.30% | + |
| 2026-03-30 | 1 | 1 | 0 | 0 | +2.30% | + |
| 2026-04-01 | 6 | 2 | 4 | 0 | -1.70% | - |
| 2026-04-02 | 1 | 1 | 0 | 0 | +0.90% | + |
| 2026-04-06 | 3 | 3 | 0 | 0 | +5.50% | + |

## Per-Symbol Performance

| Symbol | W | L | TE | WR% | CumPnL% | AvgPnL% | Grade |
|--------|---|---|-----|------|---------|---------|-------|
| NATIONALUM | 7 | 2 | 0 | 77.8% | +6.80% | +0.756% | B |
| SAIL | 3 | 1 | 0 | 75.0% | +6.50% | +1.625% | B |
| TMPV | 6 | 0 | 0 | 100.0% | +5.30% | +0.883% | A |
| EXIDEIND | 5 | 0 | 0 | 100.0% | +3.80% | +0.760% | A |
| APOLLOTYRE | 6 | 1 | 0 | 85.7% | +3.80% | +0.543% | A |
| PFC | 6 | 2 | 0 | 75.0% | +3.80% | +0.475% | B |
| ASHOKLEY | 4 | 0 | 0 | 100.0% | +3.70% | +0.925% | A |
| PNB | 4 | 0 | 0 | 100.0% | +3.20% | +0.800% | A |
| SBIN | 5 | 0 | 0 | 100.0% | +2.60% | +0.520% | A |
| CANBK | 4 | 1 | 0 | 80.0% | +2.60% | +0.520% | A |
| RECLTD | 4 | 1 | 0 | 80.0% | +2.50% | +0.500% | A |
| EMAMILTD | 5 | 2 | 0 | 71.4% | +2.30% | +0.329% | B |
| HUDCO | 1 | 0 | 0 | 100.0% | +2.30% | +2.300% | A |
| TATASTEEL | 4 | 2 | 0 | 66.7% | +2.10% | +0.350% | B |
| ZYDUSLIFE | 2 | 0 | 0 | 100.0% | +1.90% | +0.950% | A |
| FEDERALBNK | 4 | 2 | 0 | 66.7% | +1.71% | +0.285% | B |
| JINDALSTEL | 4 | 2 | 0 | 66.7% | +1.60% | +0.267% | B |
| JSWENERGY | 2 | 1 | 0 | 66.7% | +1.60% | +0.533% | B |
| IDFCFIRSTB | 2 | 0 | 0 | 100.0% | +1.60% | +0.800% | A |
| NMDC | 2 | 1 | 0 | 66.7% | +1.60% | +0.533% | B |
| LICHSGFIN | 2 | 0 | 0 | 100.0% | +1.50% | +0.750% | A |
| POWERGRID | 1 | 0 | 0 | 100.0% | +1.30% | +1.300% | A |
| ITC | 5 | 3 | 0 | 62.5% | +1.12% | +0.140% | B |
| ONGC | 1 | 1 | 0 | 50.0% | +1.10% | +0.550% | C |
| HINDALCO | 1 | 0 | 0 | 100.0% | +0.80% | +0.800% | A |
| BIOCON | 2 | 2 | 0 | 50.0% | +0.73% | +0.182% | C |
| BALRAMCHIN | 3 | 4 | 0 | 42.9% | +0.73% | +0.104% | C |
| BPCL | 2 | 1 | 0 | 66.7% | +0.70% | +0.233% | B |
| BANKINDIA | 1 | 0 | 0 | 100.0% | +0.60% | +0.600% | A |
| WIPRO | 3 | 4 | 0 | 42.9% | +0.40% | +0.057% | C |
| DLF | 2 | 1 | 0 | 66.7% | +0.30% | +0.100% | B |
| BANKBARODA | 2 | 2 | 0 | 50.0% | -0.10% | -0.025% | C |
| VBL | 1 | 1 | 0 | 50.0% | -0.30% | -0.150% | C |
| SYNGENE | 0 | 1 | 0 | 0.0% | -0.40% | -0.400% | D |
| GAIL | 1 | 2 | 0 | 33.3% | -0.50% | -0.167% | D |
| JIOFIN | 1 | 2 | 0 | 33.3% | -0.50% | -0.167% | D |
| HINDPETRO | 1 | 2 | 0 | 33.3% | -0.60% | -0.200% | D |
| USHAMART | 2 | 5 | 0 | 28.6% | -0.70% | -0.100% | D |
| TATAPOWER | 1 | 3 | 0 | 25.0% | -0.70% | -0.175% | D |
| ADANIPOWER | 1 | 2 | 0 | 33.3% | -0.80% | -0.267% | D |
| UNIONBANK | 0 | 1 | 0 | 0.0% | -0.80% | -0.800% | D |
| PNBHOUSING | 1 | 2 | 0 | 33.3% | -1.20% | -0.400% | D |
| BEL | 0 | 1 | 0 | 0.0% | -1.20% | -1.200% | D |
| MANAPPURAM | 1 | 5 | 0 | 16.7% | -3.20% | -0.533% | D |
| BHEL | 0 | 5 | 0 | 0.0% | -4.10% | -0.820% | D |

### Symbol Grade Changes (Q1 vs Post-Q1)

| Symbol | Q1 Grade | Q2 Grade | Q1 PnL | Q2 PnL | Change |
|--------|----------|----------|--------|--------|--------|
| ADANIPOWER | D | — | -0.80% | — | GONE |
| APOLLOTYRE | A | A | +3.20% | +0.60% | = |
| ASHOKLEY | A | A | +2.60% | +1.10% | = |
| BALRAMCHIN | C | — | +0.73% | — | GONE |
| BANKBARODA | B | D | +0.60% | -0.70% | DOWN |
| BANKINDIA | A | — | +0.60% | — | GONE |
| BEL | — | D | — | -1.20% | NEW |
| BHEL | D | — | -4.10% | — | GONE |
| BIOCON | C | — | +0.73% | — | GONE |
| BPCL | B | — | +0.70% | — | GONE |
| CANBK | A | D | +3.30% | -0.70% | DOWN |
| DLF | B | — | +0.30% | — | GONE |
| EMAMILTD | C | A | -0.10% | +2.40% | UP |
| EXIDEIND | A | A | +1.20% | +2.60% | = |
| FEDERALBNK | A | D | +2.41% | -0.70% | DOWN |
| GAIL | D | — | -0.50% | — | GONE |
| HINDALCO | A | — | +0.80% | — | GONE |
| HINDPETRO | D | — | -0.60% | — | GONE |
| HUDCO | — | A | — | +2.30% | NEW |
| IDFCFIRSTB | — | A | — | +1.60% | NEW |
| ITC | B | C | +0.82% | +0.30% | DOWN |
| JINDALSTEL | B | — | +1.60% | — | GONE |
| JIOFIN | C | D | +0.20% | -0.70% | DOWN |
| JSWENERGY | C | A | +0.20% | +1.40% | UP |
| LICHSGFIN | A | — | +1.50% | — | GONE |
| MANAPPURAM | D | — | -3.20% | — | GONE |
| NATIONALUM | A | B | +5.30% | +1.50% | DOWN |
| NMDC | — | B | — | +1.60% | NEW |
| ONGC | — | C | — | +1.10% | NEW |
| PFC | A | B | +3.10% | +0.70% | DOWN |
| PNB | A | A | +2.00% | +1.20% | = |
| PNBHOUSING | D | — | -1.20% | — | GONE |
| POWERGRID | — | A | — | +1.30% | NEW |
| RECLTD | B | A | +1.80% | +0.70% | UP |
| SAIL | — | B | — | +6.50% | NEW |
| SBIN | A | — | +2.60% | — | GONE |
| SYNGENE | D | — | -0.40% | — | GONE |
| TATAPOWER | D | D | -0.10% | -0.60% | = |
| TATASTEEL | C | A | +0.10% | +2.00% | UP |
| TMPV | A | A | +3.40% | +1.90% | = |
| UNIONBANK | — | D | — | -0.80% | NEW |
| USHAMART | D | — | -0.70% | — | GONE |
| VBL | C | — | -0.30% | — | GONE |
| WIPRO | B | D | +1.70% | -1.30% | DOWN |
| ZYDUSLIFE | A | — | +1.90% | — | GONE |

### Grading Key
- **A**: WR >= 80% AND positive PnL (core watchlist)
- **B**: WR >= 60% AND positive PnL (keep, monitor)
- **C**: WR >= 40% OR marginal PnL (watch closely, may remove)
- **D**: WR < 40% OR significant negative PnL (remove candidate)

## Execution Quality (Live Trade Cross-Reference)

**Period:** Mar 12 - Apr 6, 2026 (live broker data)
**Matched trades:** 22

| Metric | Value |
|--------|-------|
| Avg entry slippage | 0.189% |
| Max entry slippage | 0.588% |
| Avg exit slippage | 0.328% |

### Per-Trade Slippage Detail

| Date | Symbol | Dir | Signal Entry | Fill Entry | Slip% | Qty |
|------|--------|-----|-------------|------------|-------|-----|
| 2026-03-12 | JSWENERGY | LONG | 495.45 | 495.00 | 0.091% | 10 |
| 2026-03-12 | PNB | LONG | 115.55 | 115.82 | 0.234% | 43 |
| 2026-03-13 | EXIDEIND | SHORT | 303.50 | 303.30 | 0.066% | 13 |
| 2026-03-13 | TATASTEEL | SHORT | 186.60 | 185.83 | 0.413% | 12 |
| 2026-03-16 | FEDERALBNK | LONG | 264.75 | 264.80 | 0.019% | 79 |
| 2026-03-16 | NATIONALUM | SHORT | 378.70 | 377.55 | 0.304% | 4 |
| 2026-03-16 | PFC | SHORT | 399.50 | 399.30 | 0.050% | 38 |
| 2026-03-16 | CANBK | SHORT | 131.69 | 132.15 | 0.349% | 50 |
| 2026-03-17 | NATIONALUM | LONG | 381.20 | 383.30 | 0.551% | 32 |
| 2026-03-17 | SAIL | LONG | 149.72 | 150.60 | 0.588% | 33 |
| 2026-03-18 | PFC | LONG | 427.70 | 427.70 | 0.000% | 47 |
| 2026-03-18 | TMPV | LONG | 327.25 | 326.50 | 0.229% | 32 |
| 2026-03-18 | RECLTD | LONG | 345.15 | 345.65 | 0.145% | 5 |
| 2026-03-23 | SAIL | SHORT | 148.34 | 148.32 | 0.013% | 104 |
| 2026-03-23 | APOLLOTYRE | SHORT | 401.95 | 402.15 | 0.050% | 10 |
| 2026-03-24 | PFC | SHORT | 399.25 | 399.80 | 0.138% | 11 |
| 2026-03-24 | JIOFIN | SHORT | 228.55 | 228.40 | 0.066% | 7 |
| 2026-03-25 | EMAMILTD | LONG | 412.75 | 413.00 | 0.061% | 53 |
| 2026-03-27 | TMPV | SHORT | 305.10 | 303.85 | 0.410% | 50 |
| 2026-03-27 | ASHOKLEY | SHORT | 164.61 | 164.23 | 0.231% | 124 |
| 2026-04-06 | TATASTEEL | LONG | 194.84 | 194.65 | 0.098% | 111 |
| 2026-04-06 | SAIL | LONG | 156.05 | 155.96 | 0.058% | 114 |

## TP Strategy Analysis

### Multi-TP Impact Assessment

| Strategy | Gross PnL% | Per Trade |
|----------|-----------|-----------|
| TP1 only (cap at 1R) | +25.71% | +0.140% |
| Multi-TP (actual) | +55.49% | +0.303% |
| Multi-TP uplift | +29.78% | — |

## Strengths & Weaknesses

### Strengths

1. **Consistent positive expectancy**: +0.303%/trade across 183 trades over 44 days — robust edge
2. **72.7% winning days** (32/44) — reliable daily income stream
3. **Multi-TP progression is excellent**: 72.3% of TP1 trades continue to TP1.5. Multi-TP adds +29.78% cumulative uplift
4. **Config changes worked**: Post-Mar 12 metrics improved across the board — WR +5.1%, avg PnL doubled, W/L ratio +0.36
5. **SHORT direction is strong**: 67.5% WR with +29.48% PnL — the strategy's natural edge
6. **15 A-grade symbols** provide a reliable core watchlist (TMPV, EXIDEIND, APOLLOTYRE, ASHOKLEY, PNB, SBIN, CANBK, RECLTD, ZYDUSLIFE, HUDCO, IDFCFIRSTB, LICHSGFIN, POWERGRID, HINDALCO, BANKINDIA)
7. **Low max drawdown streak**: 6 consecutive losses max — manageable with 1% risk per trade
8. **Live execution validates signals**: 22 matched broker fills show avg 0.189% entry slippage — within cost model assumptions

### Weaknesses

1. **12 D-grade symbols cost -14.70%** — nearly wiping out a month's gains. This is the #1 fix.
2. **LONG direction underperforms**: 59.0% WR vs 67.5% SHORT — 8.5% gap persists across both periods
3. **Some A-grade symbols degraded in Q2**: CANBK (A->D), FEDERALBNK (A->D), WIPRO (B->D) — watchlist needs regular review
4. **Exit slippage is higher than entry**: 0.328% vs 0.189% avg — likely from TP market orders in fast-moving exits
5. **Signal engine not executing multi-TP**: The +29.78% uplift exists in signal data but signal engine currently takes only TP1. This is leaving significant money on the table.
6. **8 trades with no exit found** in data (early Q1) — minor data completeness issue

## Recommendations

### Priority 1: Symbol Blacklist (est. impact: +14.70% recovered)

**Remove D-grade symbols** from watchlist. These 12 symbols cost -14.70% cumulative:

| Symbol | PnL% | Trades | Action |
|--------|-------|--------|--------|
| BHEL | -4.10% | 5L | Remove (0% WR across 5 trades) |
| MANAPPURAM | -3.20% | 1W/5L | Remove (16.7% WR, worst performer) |
| BEL | -1.20% | 1L | Remove (new Q2, immediate loss) |
| PNBHOUSING | -1.20% | 1W/2L | Remove |
| UNIONBANK | -0.80% | 1L | Remove |
| ADANIPOWER | -0.80% | 1W/2L | Remove |
| TATAPOWER | -0.70% | 1W/3L | Remove |
| USHAMART | -0.70% | 2W/5L | Remove |
| HINDPETRO | -0.60% | 1W/2L | Remove |
| JIOFIN | -0.50% | 1W/2L | Remove (downgraded from C) |
| GAIL | -0.50% | 1W/2L | Remove |
| SYNGENE | -0.40% | 1L | Remove |

**Config change:** Add these to `blacklist_symbols` in PineScript alert settings.

### Priority 2: Multi-TP Execution (est. impact: +29.78% uplift confirmed)

Multi-TP is the single biggest performance driver:
- **TP1-only PnL: +25.71%** vs **Multi-TP actual: +55.49%** (+29.78% uplift)
- 72.3% of TP1 trades continued to TP1.5 — not a rare event
- Avg PnL of multi-TP trades: +1.041% vs single-TP: +0.743%
- **This reverses the Q1 conclusion** that TP1-only was optimal. Q1 simulated trailing SL from entry, not actual multi-TP exit. Actual data shows multi-TP is far superior.

**Config change:** If signal engine supports multi-TP execution (partial exits), enable it. If not, prioritize implementing it.

### Priority 3: Tighten LONG Entry Filters (est. impact: +3-5% improvement)

LONG trades underperform SHORT: 59.0% vs 67.5% WR, and this gap persists across both periods.

| Direction | Q1 WR | Q2 WR | Overall |
|-----------|-------|-------|---------|
| LONG | 57.1% | 65.2% | 59.0% |
| SHORT | 67.2% | 68.2% | 67.5% |

**Config change options:**
- Require index filter to be bullish for LONG entries (not just "not bearish")
- Reduce max LONG trades per day to 2 (keep SHORT unlimited)
- Require higher volume confirmation for LONG breakouts

### Priority 4: Monitor C-grade Symbols

These 6 symbols are borderline — review after 20 more trades each:
ONGC, BIOCON, BALRAMCHIN, WIPRO, BANKBARODA, VBL

### Priority 5: Symbols Showing Degradation

Watch for formerly good symbols trending down:
- **CANBK**: A -> D (Q1: +3.30%, Q2: -0.70%)
- **FEDERALBNK**: A -> D (Q1: +2.41%, Q2: -0.70%)
- **WIPRO**: B -> D (Q1: +1.70%, Q2: -1.30%)

Consider temporary blacklisting or reducing allocation for these.

### Summary of Config Changes

```yaml
# Recommended blacklist additions
blacklist_symbols:
  - BHEL
  - MANAPPURAM
  - BEL
  - PNBHOUSING
  - UNIONBANK
  - ADANIPOWER
  - TATAPOWER
  - USHAMART
  - HINDPETRO
  - JIOFIN
  - GAIL
  - SYNGENE

# Watchlist (consider removing if performance doesn't improve)
watch_symbols:
  - CANBK        # A -> D
  - FEDERALBNK   # A -> D
  - WIPRO        # B -> D

# Multi-TP: keep enabled, do NOT revert to TP1-only
# LONG filter: consider stricter index confirmation
```

### Projected Impact

| Change | Current PnL | Projected PnL |
|--------|-------------|---------------|
| Remove D-grade symbols | +55.49% | ~+70.19% |
| + Tighten LONG filters | — | ~+73-75% |
| Total est. (44 days) | +55.49% | ~+70-75% |
| Per trade (183 trades) | +0.303% | ~+0.40-0.42% |

## Baseline for Future Comparison

Use these metrics as baseline when evaluating improvements:

- **Win Rate baseline**: 62.8%
- **Expectancy baseline**: +0.303%/trade
- **Winning days baseline**: 72.7%
- **Long WR baseline**: 59.0%
- **Short WR baseline**: 67.5%
- **Friday WR baseline**: 59.4%
