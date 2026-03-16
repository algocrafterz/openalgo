# ORB Strategy — Final Backtest Report

**Date**: 2026-03-16
**Verdict**: No tradeable edge. Strategy retired.
**Results dir**: `backtest/results/orb/20260316_230119/`

## Executive Summary

The Opening Range Breakout (ORB) strategy was backtested across 25 NSE
mid-cap symbols over 4 years (Jan 2022 — Mar 2026) with exact PineScript
parity and realistic Indian market transaction costs. The strategy has no
tradeable edge. Gross expectancy is +0.021R/trade — marginally positive
but 15x too small to overcome transaction costs of 0.302R/trade.

## Backtest Configuration

| Parameter | Value |
|-----------|-------|
| Period | 2022-01-04 to 2026-03-01 (~4 years) |
| Symbols | 25 NSE mid-caps |
| Data | 1m OHLCV from DuckDB, resampled to 5m |
| Index filter | NIFTY (NSE_INDEX), VWAP method, 34,512 bars |
| Initial capital | Rs 1,00,000 per symbol |
| Position size | 10% of capital per trade |
| Slippage | 0.05% per leg |
| Costs | Full Indian MIS costs (brokerage, STT, exchange, GST, stamp) |
| PineScript parity | 25/25 features verified (see PINESCRIPT_PARITY.md) |

### Strategy Parameters (PineScript defaults)

- ORB window: 15 minutes
- Breakout buffer: 0.5%
- Stop loss: ATR(14) * 2.0 with price/vol adjustment
- Target: TP1 at 1.0R (conservative: min of ORB-width and risk-based)
- Entry window: 09:45 — 11:00
- Time exit: 14:30
- Filters: Volume (1.2x MA20), VWAP trend, Daily EMA(20), NIFTY index, gap (2.5%), ORB range (0.4-3.5%)
- One entry per session, no retest entry

## Key Metrics

| Metric | Value | Required for Edge |
|--------|-------|-------------------|
| Total trades | 1,067 | > 200 (sufficient sample) |
| **Gross expectancy** | **+0.021 R/trade** | > +0.35 R |
| **Net expectancy** | **-0.281 R/trade** | > +0.10 R |
| **Avg cost/trade** | **0.302 R** | — |
| Win rate | 47.6% | > 55% at 1:1 payoff |
| Profit factor | 0.54 | > 1.2 |
| Payoff ratio | 0.59 | > 1.0 |
| Avg winner | +0.683 R | — |
| Avg loser | -1.157 R | — |
| Max drawdown | -300.59 R | < 30 R |
| Worst losing streak | 10 | — |
| R-Sharpe | -0.28 | > 0.0 |
| Tail ratio | 0.67 | > 1.0 |
| Positive days | 34.1% | > 50% |

## Why There Is No Edge

### 1. Breakouts don't follow through (structural)

55% of trades (586/1,067) exit on TIME at 14:30 with avg -0.306R. The
stock breaks the ORB range, entry triggers, but price drifts sideways or
reverses — never reaching TP or SL. These time exits are the single
largest source of losses.

Only 23% of trades hit TP (+0.915R avg) and 22% hit SL (-1.461R avg).
The strategy needs directional follow-through after breakout, but Indian
mid-caps mean-revert instead.

### 2. Transaction costs destroy the gross edge

| Layer | Cost |
|-------|------|
| Gross expectancy | +0.021 R/trade |
| Brokerage (0.03% or Rs 20 cap) | ~0.06 R |
| STT (0.025% sell side) | ~0.05 R |
| Exchange + SEBI + GST | ~0.04 R |
| Stamp duty | ~0.01 R |
| Slippage (0.05% × 2 legs) | ~0.14 R |
| **Total cost** | **~0.302 R/trade** |
| **Net expectancy** | **-0.281 R/trade** |

Cost erosion is 1,438% of gross edge. You'd need gross expectancy of
+0.35R+ just to break even after costs. The current +0.021R is 17x
too small.

### 3. No symbol survives

| Tier | Symbols | Count |
|------|---------|-------|
| Green (avg R > +0.15) | — | 0 |
| Yellow (-0.1 to +0.15) | RECLTD | 1 |
| Red (avg R < -0.1) | All others | 24 |

Zero symbols are net-profitable. Even RECLTD (best at -0.05R) is
marginally negative. The problem is universal, not symbol-specific.

### 4. Both directions lose

- Long: 632 trades, avg -0.263 R/trade
- Short: 435 trades, avg -0.308 R/trade

Shorts are slightly worse, but longs are also negative. Long-only
doesn't fix it.

### 5. All days of the week lose

| Day | Avg R |
|-----|-------|
| Monday | -0.245 |
| Tuesday | -0.257 |
| Wednesday | -0.303 |
| Thursday | -0.359 |
| Friday | -0.259 |

No day-of-week filter can help when all days are negative.

## Per-Symbol Results

| Symbol | Trades | Win Rate | Net PnL | Gross PnL | Costs | Avg R | PF |
|--------|--------|----------|---------|-----------|-------|-------|----|
| RECLTD | 58 | 56.9% | +4.45 | +39.32 | 34.87 | -0.05 | 1.07 |
| PNB | 21 | 47.6% | -1.33 | +3.02 | 4.35 | -0.18 | 0.84 |
| NMDC | 11 | 36.4% | -2.82 | -1.50 | 1.32 | -0.45 | 0.32 |
| IDFCFIRSTB | 28 | 35.7% | -5.08 | -0.99 | 4.09 | -0.47 | 0.42 |
| FEDERALBNK | 39 | 53.8% | -5.75 | +7.46 | 13.21 | -0.26 | 0.76 |
| TATASTEEL | 14 | 35.7% | -5.77 | -1.26 | 4.51 | -0.54 | 0.42 |
| NATIONALUM | 42 | 54.8% | -6.25 | +7.65 | 13.90 | -0.16 | 0.81 |
| IRFC | 35 | 37.1% | -6.35 | +0.21 | 6.56 | -0.36 | 0.55 |
| SAIL | 46 | 56.5% | -6.59 | +3.87 | 10.46 | -0.16 | 0.65 |
| CANBK | 28 | 53.6% | -6.73 | -0.92 | 5.81 | -0.14 | 0.48 |
| ASHOKLEY | 46 | 47.8% | -7.37 | +2.92 | 10.29 | -0.22 | 0.67 |
| BANKINDIA | 52 | 50.0% | -12.65 | -1.95 | 10.70 | -0.22 | 0.54 |
| WIPRO | 36 | 50.0% | -14.31 | +3.29 | 17.60 | -0.19 | 0.52 |
| BPCL | 47 | 55.3% | -17.12 | +6.01 | 23.13 | -0.24 | 0.65 |
| TMPV | 42 | 54.8% | -19.27 | +34.15 | 53.42 | -0.25 | 0.79 |
| PFC | 45 | 48.9% | -19.66 | +4.64 | 24.30 | -0.25 | 0.63 |
| UNIONBANK | 55 | 38.2% | -24.01 | -12.07 | 11.94 | -0.45 | 0.28 |
| APOLLOTYRE | 62 | 54.8% | -28.54 | +23.25 | 51.79 | -0.14 | 0.72 |
| ITC | 40 | 42.5% | -29.90 | +1.60 | 31.50 | -0.33 | 0.39 |
| LICHSGFIN | 49 | 55.1% | -29.57 | +18.75 | 48.32 | -0.15 | 0.68 |
| BANKBARODA | 55 | 43.6% | -32.70 | -8.93 | 23.77 | -0.38 | 0.42 |
| JSWENERGY | 57 | 56.1% | -35.67 | +13.10 | 48.77 | -0.13 | 0.68 |
| BIOCON | 44 | 31.8% | -61.53 | -31.90 | 29.63 | -0.59 | 0.26 |
| EXIDEIND | 63 | 38.1% | -61.17 | -23.35 | 37.82 | -0.53 | 0.36 |
| EMAMILTD | 52 | 34.6% | -100.61 | -44.45 | 56.16 | -0.44 | 0.43 |

**Portfolio total**: -536.30 net PnL, +41.92 gross PnL, 578.22 costs

## Baseline Thresholds (Indian Intraday MIS)

These are minimum thresholds for a strategy to be considered tradeable
on Indian equities with MIS product type:

| Metric | Minimum | This Strategy | Pass? |
|--------|---------|---------------|-------|
| Net expectancy | > +0.10 R | -0.281 R | FAIL |
| Gross expectancy | > +0.35 R | +0.021 R | FAIL |
| Profit factor | > 1.2 | 0.54 | FAIL |
| Win rate (at ~1:1) | > 55% | 47.6% | FAIL |
| Payoff ratio | > 1.0 | 0.59 | FAIL |
| Max drawdown | < 30 R | -300.59 R | FAIL |
| R-Sharpe | > 0.0 | -0.28 | FAIL |
| TIME exit % | < 25% | 54.9% | FAIL |
| Net-profitable symbols | > 30% | 0% (0/25) | FAIL |

**Result: 0/9 thresholds met.**

## Q1 2026 Live vs 4-Year Backtest

| Metric | Q1 2026 Live (PineScript) | 4-Year Backtest (Python) |
|--------|---------------------------|--------------------------|
| Period | 31 trading days | ~1,000 trading days |
| Trades | ~200 | 1,067 |
| Gross expectancy | ~+0.35 R | +0.021 R |
| Sample reliability | Low (small sample) | High (large sample) |

The Q1 2026 live results were a favorable streak in a small sample.
The 4-year backtest with 5x the trades is statistically definitive.

## Backtest Pipeline Integrity

This report is based on a fully validated backtest pipeline:

- **PineScript parity**: 25/25 features match (PINESCRIPT_PARITY.md)
- **ATR**: Wilder's smoothing (EWM), not SMA
- **Index filter**: NIFTY VWAP method active (34,512 bars loaded)
- **SL calculation**: price_adjust (long) / vol_adjust (short) matching PineScript
- **TP calculation**: Conservative min(ORB-width, risk-based) matching PineScript
- **Costs**: Full Indian MIS cost model with slippage
- **Data**: 4 years of 1m OHLCV, ~4.3M candles across 25 symbols + NIFTY index

## Recommendation

**Archive this strategy. Do not trade it live.**

The ORB breakout approach is structurally mismatched with Indian mid-cap
stocks, which mean-revert after the opening range rather than trending.
The backtest infrastructure (costs, walk-forward, evaluation, parity
verification) is production-quality and should be reused for testing
alternative strategies.
