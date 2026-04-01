# Dividend-Growth Swing Strategy Analysis

## Overview

**File**: `dividend-growth.pine`
**Strategy Name**: `swing-dividend-growth`
**Type**: Positional swing trading — Long only (CNC delivery, no shorting)
**Timeframe**: Daily
**Approach**: Buy low (value zone), sell high (sell zone) — mean reversion on dividend and growth stocks

---

## Strategy Logic

### Core Concept
Price position within its N-day range (0% = N-day low, 100% = N-day high):
- **BUY ZONE**: Price in bottom 30% of range → stock is cheap vs recent history
- **SELL ZONE**: Price in top 30% of range → stock is expensive vs recent history
- Multi-factor scoring (0-100) confirms signal quality before firing

### Signal Scoring Components

| Component | Max | BUY triggers | SELL triggers |
|---|---|---|---|
| Price Position | 25 | In value zone (<=30%): 18, Deep value (<=25%): 25 | In sell zone (>=70%): 18, Extreme (>=90%): 25 |
| RSI(14) | 25 | RSI<=35: 20, RSI<=45: 16, RSI<=55: 12 | RSI>=60: 16, RSI>=65: 18, RSI>=70: 20 |
| Trend | 25 | EMA cross: 10, Rising EMA: 8, Above SMA50: 7 | % above EMA: 4-10, % above SMA50: 2-10, 3+ consec up: 5 |
| Volume | 15 | 1.5x avg: 12, 1.3x: 9, 1.1x: 6 | 1.5x (any candle): 9, 2x: 12, 2.5x: 15 |
| Price Action | 10 | Bullish candle: 4, Hammer: 3, Higher close: 3 | Red candle: 4, Upper wick: 3, Failing highs: 3 |

### Auto-Detection by Stock Type

| Parameter | Dividend | Growth |
|---|---|---|
| Lookback | 90 days | 60 days |
| Strong BUY | score >= 50 | score >= 65 |
| Medium BUY | score >= 38 | score >= 50 |
| Strong SELL | score >= 55 | score >= 70 |
| Medium SELL | score >= 42 | score >= 55 |

---

## Signal Configuration

| Signal | Telegram Alert | Strategy Tester Entry/Exit |
|---|---|---|
| BUY STRONG | Yes | Entry (when flat) |
| BUY MEDIUM | Yes | Entry (when flat) |
| BUY WEAK | No | No |
| SELL STRONG | Yes (Take Profit) | Exit (only if profitable) |
| SELL MEDIUM | No | No |
| SELL WEAK | No | No |

**No Stop Loss**: CNC delivery — hold or accumulate on drops. Position never force-closed at a loss.

---

## Strategy Tester Configuration

```
initial_capital = 100,000
position_size   = 90% of equity
commission      = 0.1%
slippage        = 1 tick
```

**Exit logic** (profit-only, two mechanisms):
1. **Profit Target %** (default 8%): Close when gain ≥ target — faster profit booking
2. **STRONG SELL signal**: Close only if currently profitable (never exits at a loss)

**No pyramiding**: Single entry per cycle (`position_size == 0` guard). Unprofitable open positions are held until profitable — shown as "open trades" in tester, not closed losses.

### Why Losses Appeared Previously
- Missing `position_size == 0` guard → multiple entries (pyramiding) at different prices
- STRONG SELL closed all entries including higher-cost ones → individual loss trades in report
- Backtest end forced-close of underwater open positions

---

## Profit Target Optimization

| Target % | Avg Hold | Trades/Year/Stock | Best For |
|---|---|---|---|
| 5% | 2-4 weeks | 8-12 | Metals/banks — very fast cycling |
| **8% (default)** | **3-6 weeks** | **5-8** | **All stocks — balanced** |
| 10% | 4-8 weeks | 3-6 | Mid-volatility stocks |
| 12-15% | 6-12 weeks | 2-4 | Dividend PSUs, full range swings |

---

## Stock Universe

### Dividend Stocks (13) — 90-day lookback, 4-10 week cycles

```
ITC, CASTROLIND, HINDPETRO, NTPC, POWERGRID, COALINDIA, HINDZINC,
PETRONET, SJVN, GAIL, ONGC, NMDC, RECLTD
```

Characteristics: PSUs, utilities, regulated businesses — high dividend yield, government backing, safe to hold without SL.

### Growth Stocks (18) — 60-day lookback, 3-6 week cycles

```
MOTHERSON, SAIL, TATASTEEL, NATIONALUM, BANKBARODA, AARTIIND,
BEL, VEDL, DABUR, AMBUJACEM, IRCTC, INDHOTEL, MARICO, DLF,
HINDALCO, JSWSTEEL, HINDUNILVR, BAJFINANCE
```

Characteristics: Cyclicals, metals, PSU banks, FMCG — faster oscillators, higher volatility, quicker cycles.

### Removed Stocks

| Stock | Reason |
|---|---|
| ETERNAL (Zomato) | Pure momentum stock — no mean reversion, no dividend floor |
| BIOCON | Structural decline since 2022, biosimilars underperformance |
| INDUSINDBK | Sustained downtrend from MFI book stress, no PSU backing |

### Added Stocks

| Stock | List | Reason |
|---|---|---|
| ONGC | Dividend | PSU oil, ~5-6% dividend yield, government-backed, clean mean-reverter |
| NMDC | Dividend | PSU iron ore, ~5-7% dividend yield, government-backed |
| RECLTD | Dividend (moved from Growth) | PSU NBFC, high dividend, behaves like dividend stock |
| HINDUNILVR | Growth | FMCG bellwether, excellent mean-reverter, safe without SL |
| BAJFINANCE | Growth | Established NBFC, 25-35% range, recovers reliably from dips |

---

## Stock Ranking by Returns & Holding Period

### Tier 1 — Top 5: Fastest cycles, highest return potential (target 8%)

| Rank | Stock | Avg Cycle | Range | Return/Trade | Why |
|---|---|---|---|---|---|
| 1 | **HINDALCO** | 3-4 weeks | 35-45% | 8-14% | Aluminium + Novelis, quick mean reversion after commodity dips |
| 2 | **COALINDIA** | 4-6 weeks | 20-30% | 8-12% | PSU mining + high dividend (~9%), government earnings floor |
| 3 | **BANKBARODA** | 3-5 weeks | 30-40% | 8-12% | PSU bank, government-backed floor, fast recovery from dips |
| 4 | **NATIONALUM** | 3-5 weeks | 35-50% | 8-15% | PSU aluminium, fast cyclical oscillation, Navratna PSU |
| 5 | **SAIL** | 3-5 weeks | 35-50% | 8-14% | PSU steel, government backing prevents deep traps |

### Tier 2 — Good returns, moderate hold (5-8 weeks)

RECLTD, BEL, BAJFINANCE, NMDC, AMBUJACEM, GAIL, ONGC, IRCTC, VEDL, JSWSTEEL, TATASTEEL, HINDZINC

### Tier 3 — Steady but slower (8-14 weeks)

ITC, NTPC, POWERGRID, PETRONET, SJVN, DABUR, MARICO, HINDUNILVR, INDHOTEL, CASTROLIND

**Practical tip**: For faster consistent cycles, focus alerts on Tier 1 stocks and use `Profit Target = 8%`. For Tier 3 dividend stocks, raise target to 10-12% to capture full range swings.

---

## Dashboard Layout

| Table | Position | Purpose |
|---|---|---|
| Action Dashboard | Top-right | Current recommendation: STRONG BUY / ACCUMULATE / BOOK PROFIT / STRONG SELL / HOLD. Buy/sell scores, key indicators, target price levels. |
| Signal History | Bottom-right | Period-wise signal counts (1W to 12M): STRONG / MEDIUM / WEAK buys + SELLS |
| Debug Table | Bottom-left | OFF by default. Toggle via "Show Debug Table". Shows component scores (Pos/RSI/Trend/Vol/PA) for both buy and sell sides. |

---

## Chart Visualization

- **Green background**: Value zone (buy zone, bottom 30%)
- **Red background**: Sell zone (top 30%)
- **Green triangle up** below bar: Confirmed BUY signal
- **Red triangle down** above bar: Confirmed SELL signal
- **Circles**: Live bar — conditions currently favorable (not yet confirmed)

---

## Telegram Alert Format

**BUY (STRONG or MEDIUM)**:
```
Dividend ENTRY
BUY STRONG
Symbol: ITC
Entry: 432.50
Score: 67
Time: 14:32 | TF: D
View Chart
```

**SELL / Take Profit (STRONG only)**:
```
Dividend EXIT
SELL STRONG (TP)
Symbol: ITC
Exit: 468.20
Score: 58
Time: 10:15 | TF: D
View Chart
```
