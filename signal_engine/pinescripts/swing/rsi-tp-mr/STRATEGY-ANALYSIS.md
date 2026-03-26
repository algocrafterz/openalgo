# RSI(2) Trend Pullback Mean Reversion — Strategy Analysis & Development Notes

**Strategy tag:** `RSI-TP-MR`
**Status:** PineScript v2 complete, pending backtest validation (2026-03-26)
**Branch:** `feature/swing-rsi-trend-pullback-mean-reversion`
**Location:** `signal_engine/pinescripts/swing/rsi-tp-mr/`

---

## Strategy Summary

| Parameter | Value |
|---|---|
| Type | Mean reversion inside trend |
| Timeframe | Daily chart, swing (2-7 day hold) |
| Direction | Long only |
| Core signal | RSI(2) < 5, Close > 200 SMA |
| Entry | Pre-close window (3:15-3:25 PM IST), CNC delivery |
| Primary exit | LTP > 5 SMA (tracker continuous monitoring) |
| Exit | Close > 5 SMA (tracker LTP monitoring, continuous intraday) |
| Safety exit | Close < 200 SMA (trend broken) |
| Time exit | 7 trading days max hold |
| Stop-loss | max(200 SMA, entry * 0.92) — structural + emergency cap |
| Product type | CNC (Cash & Carry) — NOT MIS |
| Expected win rate | 60-75% |
| Expected profit factor | 1.5-3.5 |
| Trades/stock/year | 10-25 |

## How It Complements ORB

| Dimension | ORB | RSI(2) Swing |
|---|---|---|
| Edge | Momentum breakout | Mean reversion (panic dip -> bounce) |
| Timeframe | Intraday | 2-7 days |
| Product | MIS (margin) | CNC (delivery) |
| Capital usage | Active daily | 5-15% time-in-market |
| Market regime | Works in trending/volatile | Works in uptrend with pullbacks |
| Correlation | Directional | Counter-cyclical |
| Cost | ~0.19R/trade (MIS) | ~0.224% round-trip (delivery STT) |

Combined: ORB for daily income, RSI(2) for opportunistic swing gains. Naturally uncorrelated.

## Key Research Backing

- Larry Connors & Cesar Alvarez — "Short Term Trading Strategies That Work" (2008), 100K+ backtested trades
- 34-year independent Python backtest (S&P 500, 1990-2024) — still positive
- QuantifiedStrategies.com — R3 variant profit factor 3.37 (validated 2024)
- CRSI readings 0-5 produced avg 5-day returns of +2.15%

## PineScript v2 Status (rsi-tp-mr-v2.pine) — CURRENT

### Key Design Decisions (2026-03-26)

**TP: Close > 5 SMA — tracker continuous LTP monitoring (no partial TPs)**
- Connors' canonical exit, backtested 100K+ trades
- Mean reversion has defined endpoint (price reverts to mean)
- 5 SMA value sent as TP in entry alert → tracker monitors LTP vs TP continuously
- Exits as soon as LTP crosses 5 SMA — could be 10:30 AM next day (no waiting for EOD)
- PineScript EXIT alerts only for safety exits (trend break, max hold)
- Uses fixed TP from entry time (5 SMA barely moves over 2-7 day hold)

**SL: 200 SMA structural + configurable emergency cap (default -8%)**
- Primary SL = 200 SMA level (Connors' safety exit, barely impacts backtest)
- Emergency SL = entry * (1 - 8%) — caps worst case when 200 SMA is far
- Actual SL = max(sma200, emergency) — whichever is tighter/closer to entry
- Required for: (1) broker-side safety net, (2) signal engine position sizing denominator
- User can disable emergency SL for pure Connors (SL = 200 SMA only)

**Alert: ORB-identical pipeline (TradingView -> Telegram -> Signal Engine -> OpenAlgo -> Broker)**
- Alert format: `RSI-TP-MR LONG | SYMBOL` (pipe-delimited first line, normalizer splits into strategy + symbol)
- KV pairs: Entry, SL, TP, Product (parser extracts these)
- TP = 5 SMA value at entry time -- tracker monitors LTP vs this level continuously
- Human-readable extras: Risk, R:R, RSI(2), exit conditions (parser ignores non-KV lines)
- Safety exit alert: `RSI-TP-MR EXIT | SYMBOL` (trend break, max hold only)
- Normal exit (Close > 5 SMA) handled by tracker -- no PineScript EXIT alert needed
- Telegram JSON via webhook to Bot API `/sendMessage` (identical to ORB)
- Normalizer regex updated to support hyphenated strategy names (`[\w-]+`)

### What's implemented in v2
- Commission 0.12% per side, initial capital 500K INR
- All entry filters: RSI(2), 200 SMA trend, 50 SMA RS (toggleable), volume, min price
- **Pre-close entry window** (3:15-3:25 PM IST, configurable) -- evaluates on live intrabar data
- Primary exit: close > 5 SMA -- TP value sent in entry alert, tracker monitors continuously
- Safety exits: close < 200 SMA, max hold days -- fire EXIT alerts to signal engine
- SL order placed with broker: max(sma200, entry * (1 - emergency%))
- Grouped inputs with tooltips (6 groups, matching ORB style)
- Input validation with runtime.error()
- Stats table: trades, win rate, PF, avg trade, max DD, net profit, RSI(2)
- Position dashboard: entry, SL, exit SMA, days held, P&L, qty, risk/share
- ORB-identical alerts: pipe-delimited first line + KV pairs + human-readable extras
- Test alert functionality
- Date range filter for out-of-sample backtesting
- Calendar day approximation (bars * 7/5)
- `calc_on_every_tick=true` for intrabar entry evaluation during pre-close window
- `barstate.isconfirmed` on safety exit conditions (no repainting)
- Entry fires once per bar during window (guard prevents duplicate alerts)

### Removed from v1
- ATR volatility filter (not in Connors' research, added noise)
- TP1/TP2 partial exits (replaced with single 5 SMA exit)
- Bare JSON alerts (replaced with signal engine parser format)

## PineScript v1 (rsi-tp-mr.pine) — SUPERSEDED

Original prototype with partial TP exits and minimal alerts. Kept for reference.

## Signal Engine Integration — DONE (2026-03-26)

### Pipeline changes
- **Validator**: EXIT signals skip SL/TP/R:R/duplicate checks (early return after entry>0 + symbol check)
- **Executor**: `build_exit_order()` — MARKET SELL for closing existing LONG positions
- **Tracker**: `find_position()`, `unregister()` for EXIT-driven closes
- **Tracker**: `tp_monitoring=True` for all positions — LTP monitoring handles normal exit (TP = 5 SMA)
- **Tracker**: `time_exit_all()` only closes MIS positions — CNC survives overnight
- **Main**: `handle_message` dispatches EXIT -> `_handle_exit` vs LONG/SHORT -> `_handle_entry`
- **Main**: EXIT flow: tracker lookup -> fallback to broker API -> cancel SL -> MARKET SELL -> unregister
- **Main**: Product passthrough fix — `signal.product or settings.product` (was hardcoded `settings.product`)
- **Notifier**: EXIT-specific notifications (received, placed, no_position, failed)
- **304 tests** (26 new + 278 existing ORB regression safe)

### Exit Architecture (2026-03-26)
| Exit type | Handler | When |
|---|---|---|
| Normal (Close > 5 SMA) | Signal engine tracker LTP monitoring | Continuous during market hours |
| Safety (trend break) | PineScript EXIT alert -> signal engine | On confirmed daily bar close |
| Safety (max hold days) | PineScript EXIT alert -> signal engine | On confirmed daily bar close |
| Emergency SL | Broker-side SL-M order | Automatic via broker |

### Remaining
- Position persistence across engine restarts (fallback to broker API for now)
- Separate CNC capital pool from MIS (currently shares same capital fetch)

## Improvement Roadmap

### Phase 1: PineScript hardening -- DONE
- [x] Commission 0.12% per side for Indian markets
- [x] 5 SMA primary exit (canonical Connors)
- [x] Volume filter (min volume threshold)
- [x] Calendar day approximation (bars * 7/5)
- [x] ORB-identical alert format for signal engine pipeline
- [x] Strategy tag renamed from SWING to RSI-TP-MR
- [ ] Backtest across Nifty 50 universe on TradingView

### Phase 2: Quantitative validation
- Port to capital_sweep.py (vectorbt) for programmatic backtesting
- Test parameter sensitivity (RSI 3/5/8, exit SMA 3/5/7)
- Cross-stock robustness (min 5 liquid stocks must be profitable)
- Regime split (bull/flat/bear years separately)
- Indian-specific: account for STT, stamp duty, GST

### Phase 3: Signal engine integration -- DONE
- [x] Validator EXIT-aware (skip SL/TP/R:R checks for EXIT signals)
- [x] `build_exit_order()` — MARKET SELL for closing positions
- [x] `_handle_exit()` pipeline — cancel SL, exit, unregister
- [x] CNC product passthrough (signal.product flows to orders + tracker)
- [x] `tp_monitoring=True` for all positions (tracker LTP monitoring for normal exit)
- [x] `time_exit_all()` skips CNC positions (MIS only)
- [x] EXIT notifications (received, placed, no_position, failed)
- [x] Broker API fallback when tracker loses state (engine restart)
- [ ] Separate CNC capital pool from MIS

### Phase 4: Paper trading
- Run in analyzer mode alongside ORB for 2-4 weeks
- Track: actual fills vs backtest, slippage on CNC close orders
- Validate signal frequency matches backtest expectations

## Risk Profile

| Risk | Severity | Mitigation |
|---|---|---|
| No SL — individual trade -5% to -10% | High | Position sizing (15-20% max per trade), max 3-5 concurrent |
| Overnight gap risk (17.75hr market closure) | High | Avoid earnings, diversify sectors, Nifty 50 universe only |
| Bull-to-bear transition (cluster of losses) | Medium | 200 SMA filter auto-deactivates, but transition period hurts |
| Survivorship bias | Medium | Stick to Nifty 50/Next 50 (unlikely to delist) |
| Psychological (holding losers with no SL) | Medium | Wide -8% emergency stop if needed (performance drag) |

## Position Sizing

- Risk-based sizing using SL distance: `qty = floor(capital * risk_pct / abs(entry - sl))`
- SL = max(200 SMA, entry * 0.92) provides sizing denominator
- Same formula as ORB (signal engine's fixed_fractional mode)
- Max 3-5 concurrent positions
- Separate CNC capital pool from MIS

## Cost Analysis

| Component | Rate |
|---|---|
| STT (buy + sell) | 0.1% x 2 = 0.2% |
| Brokerage (Zerodha CNC) | 0% |
| Exchange + stamp + GST | ~0.024% |
| **Total round trip** | **~0.224%** |
| Expected gain per trade | 1.5-3.0% |
| **Cost as % of gain** | **7-15%** |

## Optimal Price Band Analysis

**Best suited: 150-350 INR stocks from Nifty 50**

| Factor | Low (<100) | Mid (150-350) | High (500-1500) | Premium (2000+) |
|---|---|---|---|---|
| Liquidity | Poor, wide spreads | Good | Excellent | Excellent |
| Mean reversion quality | Noisy, manipulation risk | Strong, institutional | Moderate | Slow, small % moves |
| CNC capital efficiency | Efficient but risky | Optimal | Needs 50K+ | Needs 1L+ |
| Signal reliability | D-grade stocks, avoid | A/B-grade, clean signals | Good but fewer signals | Good but slow |
| RSI(2) < 5 frequency | High (volatile) | Moderate (10-25/yr/stock) | Lower | Lowest |
| **Verdict** | **Avoid** | **Best** | **Good with capital** | **Needs large capital** |

**Why 150-350 INR is optimal:**
- Institutional participation ensures clean mean reversion (not manipulation)
- 5 SMA exit fires in 2-5 bars (fast bounce in this volatility range)
- At 10K capital: qty = 4-8 shares per trade, notional 1-2.5K (manageable)
- Tight bid-ask spreads relative to price (0.1-0.3%)
- Sufficient daily volume (>500K shares) for CNC fills

**Nifty 50 stocks in optimal range (approximate):** NTPC, COALINDIA, BPCL, ONGC, POWERGRID, TATASTEEL, HINDALCO, JSWSTEEL

## 10K INR Deployment Guide

### Capital Math
- Risk budget: 10,000 x 1% = 100 INR per trade
- Stock at 250, SL at 230 (8%): qty = floor(100/20) = 5 shares, value = 1,250
- Stock at 200, SL at 190 (5%): qty = floor(100/10) = 10 shares, value = 2,000
- Max 1 concurrent position at 10K (position = 12-20% of capital)

### Configuration for 10K
```yaml
risk_per_trade: 0.01        # 1% = 100 INR max loss per trade
max_open_positions: 1        # Only 1 concurrent at 10K
max_trades_per_day: 2        # Cap exposure
min_entry_price: 100         # Skip penny stocks
max_entry_price: 400         # Skip stocks that need >10K per position
```

### Pre-deployment Checklist
- [ ] PineScript v2 applied to 5-8 Nifty 50 stocks in 150-350 range
- [ ] TradingView alerts created: "Any alert() function call" with Telegram webhook
- [ ] Signal engine running with CNC config
- [ ] Telegram notify_channel receiving test alerts
- [ ] Broker CNC orders working (test with smallest qty)
- [ ] Chartink scanner bookmarked (RSI(2) < 5, Close > 200 SMA filter)

### Expected Performance at 10K
- Trades/month: 2-5 (conservative, low-price Nifty 50 only)
- Win rate: 60-75% (Connors baseline)
- Avg win: 1.5-3.0% of position value (20-60 INR per trade)
- Avg loss: 3-8% of position value (capped by SL)
- Monthly P&L: highly variable, expect +100 to +500 INR in good months
- **Purpose: validate strategy mechanics before scaling capital**

---

*Created: 2026-03-26 | Last updated: 2026-03-26 (pre-close entry, tracker TP monitoring, strategy notifications)*
