# Dividend Strategy Analysis

## Overview

**File**: `dividend.pine`
**Type**: Positional swing trading system for Indian dividend/growth stocks
**Timeframe**: Daily (positional, multi-week holds)
**Alerts**: Telegram via TradingView webhook (STRONG signals only)

## Strategy Logic

### Core Concept
Mean-reversion on dividend-paying and growth stocks using a **multi-factor scoring system (0-100)**:
- **BUY** when price is in the **value zone** (bottom 30% of N-day range)
- **SELL** when price is in the **sell zone** (top 30% of N-day range)

### Auto-Detection System
Script auto-detects stock type from hardcoded symbol lists:

| Parameter | Dividend | Growth |
|-----------|----------|--------|
| Lookback | 90 days | 60 days |
| Signal Gap | User config (default 10) | User config (default 10) |
| Strong Buy Score | 50 | 65 |
| Medium Buy Score | 38 | 50 |
| Strong Sell Score | 55 | 70 |
| Medium Sell Score | 42 | 55 |

**Dividend stocks** (10): ITC, CASTROLIND, HINDPETRO, NTPC, POWERGRID, COALINDIA, HINDZINC, PETRONET, SJVN, GAIL
**Growth stocks** (20): MOTHERSON, SAIL, TATASTEEL, NATIONALUM, BANKBARODA, ETERNAL, RECLTD, AARTIIND, BIOCON, BEL, VEDL, DABUR, AMBUJACEM, IRCTC, INDHOTEL, MARICO, DLF, HINDALCO, INDUSINDBK, JSWSTEEL

### Scoring System (BUY)

| Component | Max Points | Key Triggers |
|-----------|-----------|--------------|
| Price Position | 25 | Deep value (<=25%): 25, Value zone (<=30%): 18 |
| RSI(14) | 25 | RSI <= 25: 25, RSI <= 35: 20, RSI <= 45: 16 |
| Trend (EMA/SMA) | 25 | EMA cross: 10, Rising EMA: 8, Above SMA50: 7 |
| Volume | 15 | 2.5x avg: 15, 1.5x: 12, 1.3x: 9 |
| Price Action | 10 | Bullish candle: 4, Higher close: 3, Hammer: 3 |
| **Total** | **100** | |

**Thresholds**: STRONG >= 50 (dividend) / 65 (growth), MEDIUM >= 38 / 50

### Scoring System (SELL)

| Component | Max Points | Key Triggers |
|-----------|-----------|--------------|
| Price Position | 25 | Extreme sell (>=90%): 25, Sell zone (>=70%): 18 |
| RSI(14) | 25 | RSI >= 80: 25, RSI >= 70: 20, RSI >= 60: 16 |
| Trend Exhaustion | 25 | Overextended above EMA: 10, Above SMA50 by 15%: 10, Consecutive up: 5 |
| Volume | 15 | High volume at highs: 15 (any candle color) |
| Price Action | 10 | Red candle: 4, Upper wick rejection: 3, Failing highs: 3 |
| **Total** | **100** | |

**Thresholds**: STRONG >= 55 (dividend) / 70 (growth), MEDIUM >= 42 / 55

### Signal Flow
1. Calculate 5 scoring components
2. Check zone condition (value zone for buy, sell zone for sell)
3. Check signal gap (minimum N days between signals)
4. Determine strength (STRONG/MEDIUM/WEAK)
5. Fire alert if STRONG + webhook enabled

### Telegram Alert Format
- **BUY**: Symbol, entry price, score, strength, chart link
- **SELL**: Symbol, exit price, score, strength, chart link
- Only STRONG signals trigger Telegram alerts

## Dashboard

### Action Dashboard (bottom-right)
Large-font actionable panel with 10 rows:
- **Row 0**: Symbol, stock type, current price, position %
- **Row 1**: ACTION RECOMMENDATION (STRONG BUY / ACCUMULATE / HOLD / BOOK PROFIT / STRONG SELL) in huge font
- **Row 2-3**: Buy/Sell score with strength rating and zone status
- **Row 4**: Key indicators (RSI, Volume ratio, EMA status, SMA50 status)
- **Row 5**: N-day range with % from low/high
- **Row 6-7**: Target price levels (buy below X, sell above Y)
- **Row 8**: Signal history (1M and 3M buy/sell counts)
- **Row 9**: Threshold reference

### Debug Table (top-right)
- Shows real-time scoring breakdown for both BUY and SELL
- Bar state, zone status, individual component scores
- Helps diagnose why signals may not fire

## Chart Visualization

- **Green background**: Value zone (buy zone)
- **Red background**: Sell zone
- **Green triangle up** (below bar): Confirmed BUY signal
- **Red triangle down** (above bar): Confirmed SELL signal
- **Circle markers**: Live bar conditions (not yet confirmed)
- **Zone boundary lines**: Buy/sell zone price levels
- **EMAs**: Fast EMA(12), Slow EMA(26), Trend SMA(50)

## Investment Approach

This is NOT a traditional dividend investment strategy (buy-and-hold for yield). It is a **swing trading strategy** that:

1. **Targets dividend-quality stocks** for their mean-reverting behavior
2. **Buys dips** in value zone using multi-factor confirmation
3. **Sells rallies** in overvalued zone when exhaustion signals appear
4. **Holds positionally** (weeks to months, not day trading)
5. **Complements dividend income** with capital gains from swing trading

### Why Dividend Stocks for Swing Trading?
- PSUs and utilities are strongly mean-reverting (government ownership, regulated earnings)
- High institutional holding creates natural support/resistance zones
- Dividend dates create predictable price patterns (run-up before ex-date, dip after)
- Lower volatility than growth stocks = more predictable ranges

## Key Improvements Made (v2)

### Sell Signal Rebalance
- **Before**: Sell scoring required bearish confirmation (red candles, declining EMAs) which contradicts being at range highs
- **After**: Sell scoring detects overextension (distance above MAs, consecutive green bars, high volume at highs regardless of candle color)

### Visual Differentiation
- BUY signals: Green labels and markers
- SELL signals: Red/orange labels and markers (previously same green as buy)

### Enhanced Debug Table
- Shows both BUY and SELL score breakdowns
- Individual component scores visible for tuning

## Usage Notes

- Apply to **daily timeframe** on TradingView
- Add stocks to the appropriate symbol list (dividend_symbols or growth_symbols)
- Set up TradingView alert with "Any alert() function call" condition
- Configure Telegram chat ID in settings
- Adjust signal gap based on trading style (5-10 aggressive, 15-20 moderate, 30+ conservative)
- Monitor debug table to understand scoring behavior on live charts
