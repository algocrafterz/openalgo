# HOD-FPB Strategy — Technical Requirements Document

**Version:** 2.1
**Date:** 31 March 2026
**Status:** SHELVED — Poor backtest results, not viable for live trading
**Author:** Strategy Development Team

---

## 1. Executive Summary

The HOD-FPB (High-of-Day Breakout + First Pullback) is an intraday equity strategy for Indian NSE markets. It detects HOD breakouts (Long) and LOD breakdowns (Short) with volume + VWAP confirmation, waits for a controlled pullback, and enters on pullback resumption.

**Implementation:** Single PineScript v6 strategy (`hod.pine`) with TradingView alerts routed to Telegram for signal monitoring. Both LONG and SHORT directions are combined in one script.

**Current Phase:** SHELVED — Strategy does not produce positive returns in TradingView backtests across the target stock universe.

---

## 2. System Architecture

```
TradingView (PineScript v6)
    |
    | alert() with JSON payload
    v
Telegram Bot API (sendMessage)
    |
    v
Telegram Channel (signal monitoring)
    |
    | (future: after monitoring phase)
    v
OpenAlgo Signal Engine (execution)
```

**Implementation file:** `signal_engine/pinescripts/intraday/hod-fpb/hod.pine`

---

## 3. Strategy Logic

### 3.1 State Machine

```
SCANNING --> HOD BREAKOUT / LOD BREAKDOWN --> PULLBACK --> ENTRY READY --> IN TRADE --> TP1/SL/TIME EXIT
```

### 3.2 LONG Setup (HOD Breakout + First Pullback)

1. **HOD Breakout Detection:**
   - Price makes new session high (`high >= sessionHigh`)
   - Volume confirmation: RVOL >= 1.5x (20-period SMA)
   - VWAP alignment: `close > VWAP` and VWAP slope positive (5-bar lookback)
   - Momentum: `close > close[1]`
   - Skip first 3 bars (opening volatility)

2. **Pullback Validation:**
   - 2-5 consecutive bars with lower highs
   - Pullback low stays above VWAP
   - Retracement: 0.3% - 1.5% from HOD (widened from original 0.8% for Indian stocks)
   - No aggressive selling: no pullback bar with volume > 2x average

3. **Entry Trigger:**
   - Price breaks above pullback high (`high > pullbackHigh`)
   - SL at pullback low minus 0.05% buffer
   - SL distance validated: 0.3% - 2.0% of entry price
   - TP1 at 1R (full exit)

### 3.3 SHORT Setup (LOD Breakdown + First Pullback)

Mirror of LONG:
- LOD breakdown: `low <= sessionLow`, RVOL >= 1.5x, `close < VWAP`, VWAP slope negative
- Pullback: 2-5 bars of higher lows, pullback high stays below VWAP
- Entry: break below pullback low
- SL: above pullback high plus buffer
- TP1 at 1R (full exit)

### 3.4 Exit Strategy

| Exit Type | Condition | Action |
|-----------|-----------|--------|
| TP1 | Price reaches entry +/- 1R | Full position exit (100%) |
| SL | Price hits stop loss | Full position exit |
| Time Exit | 15:00 IST | Close all positions |

**Design Decision:** TP1 = 100% exit at 1R. No trailing stop. Based on ORB Q1 2026 data showing TP1-only (+69R) vastly outperforms trail (+18R) in Indian intraday.

### 3.5 Risk Management

| Parameter | Default | Description |
|-----------|---------|-------------|
| Max Trades/Day | 1 | Single entry per session |
| Daily Loss Limit | -2R | Stop trading after -2R |
| Risk Per Trade | 0.5% | Of capital (reduced from 1% per Monte Carlo analysis) |
| Min SL % | 0.3% | Skip if SL too tight |
| Max SL % | 2.0% | Skip if SL too wide |
| Entry Cutoff | 14:00 IST | Later than ORB (11:00) since HOD breakouts are not time-bound |
| Time Exit | 15:00 IST | Buffer before broker auto square-off |

---

## 4. Implementation Details

### 4.1 Direction Control

Input toggle: `tradeDirection` = "Long Only" / "Short Only" / "Both"
- Both directions share the same risk limits
- Only one position at a time (`pyramiding=0`)
- Only one entry per session (`sessionEntryTaken` flag)

### 4.2 Chart Visualization

| Element | Long | Short |
|---------|------|-------|
| Entry line | Green (width=2) | Red (width=2) |
| Entry label | "LONG ENTRY: xxx" | "SHORT ENTRY: xxx" |
| SL line/label | Red dashed | Orange dashed |
| TP1 line/label | Lime dashed | Fuchsia dashed |
| TP2/TP3 | Lime/Fuchsia dotted (observation only) | Same |
| TP hit label | Green with checkmark | Green with checkmark |
| SL hit label | Red with cross | Red with cross |
| Breakout marker | Green triangle up | Red triangle down |
| Entry arrow | Green arrow up | Red arrow down |
| Background | Light green | Light red |
| Pullback zone | Green shaded box | Red shaded box |

Lines extend to current bar while trade is active.

### 4.3 Dashboard (12 rows)

| Row | Label | Content |
|-----|-------|---------|
| Header | HOD Strategy | Ticker symbol |
| HOD | Session high | Green |
| LOD | Session low | Red |
| VWAP | Value + slope arrow | Blue |
| RVOL | Relative volume | Green/Yellow/Red |
| State | Current state machine state | Color-coded by direction |
| Side | LONG / SHORT | Green / Red |
| Daily P&L | Cumulative R | Green / Red |
| Result | Last trade outcome | TP (green) / SL (red) / TIME (orange) |
| Trades | Count / Max | White |
| Mode | Long Only / Short Only / Both | White |
| Session | ACTIVE / DONE / NO ENTRY / CLOSED / LOSS LIMIT | Color-coded |

Font size: `size.small` for data, `size.normal` for header.

### 4.4 Telegram Alerts

Same JSON format as ORB strategy for consistency:

```json
{"chat_id": "<ID>", "text": "<message>", "parse_mode": "Markdown"}
```

Alert types:
- **Entry:** Direction, entry/target/SL prices, risk/reward, R:R ratio
- **TP1 Hit:** P&L amount and percentage
- **TP2/TP3 Hit:** Observation alerts (no execution)
- **SL Hit:** Loss amount and percentage
- **Time Exit:** P&L at close price

Webhook URL: `https://api.telegram.org/bot<TOKEN>/sendMessage`

### 4.5 Configurable Inputs

| Parameter | Default | Range | Group |
|-----------|---------|-------|-------|
| Trading Session | 0915-1530 | - | Session |
| Entry Cutoff | 14:00 | 9-15 | Session |
| Time Exit | 15:00 | 12-15 | Session |
| Skip First N Bars | 3 | 0-12 | Session |
| Trade Direction | Both | Long/Short/Both | Breakout |
| Volume MA Length | 20 | 5-100 | Breakout |
| Min RVOL | 1.5 | 1.0-5.0 | Breakout |
| VWAP Slope Lookback | 5 | 2-20 | Breakout |
| Require VWAP Slope | true | - | Breakout |
| Min Pullback Bars | 2 | 1-6 | Pullback |
| Max Pullback Bars | 5 | 2-10 | Pullback |
| Min Retracement % | 0.3 | 0.1-2.0 | Pullback |
| Max Retracement % | 1.5 | 0.3-3.0 | Pullback |
| Max Pullback Vol x | 2.0 | 1.0-5.0 | Pullback |
| Capital | 100000 | 10000+ | Risk |
| Risk Per Trade % | 1.0 | 0.25-3.0 | Risk |
| SL Buffer % | 0.05 | 0.01-0.5 | Risk |
| Min SL % | 0.3 | 0.1-2.0 | Risk |
| Max SL % | 2.0 | 0.5-5.0 | Risk |
| Max Trades/Day | 1 | 1-10 | Risk |
| Max Daily Loss (R) | -2.0 | -10 to -0.5 | Risk |
| TP1 Target (R) | 1.0 | 0.5-5.0 | Targets |

---

## 5. Stock Universe

### Tier 1 — Best Fit (10 stocks for monitoring)

TATAMOTORS, BAJFINANCE, SBIN, RELIANCE, ICICIBANK, HDFCBANK, LT, ADANIENT, AXISBANK, MARUTI

**Selection criteria:** High intraday volume, good ATR %, momentum-friendly, MIS-eligible F&O stocks.

### Avoid

BHEL, MANAPPURAM, YESBANK, GAIL, ITC, HINDUNILVR — low ATR, mean-reverting, wide spreads, or policy-driven.

---

## 6. Monitoring Phase (4 weeks)

### Setup
1. Add `hod.pine` to TradingView on all 10 stocks (5-min charts)
2. Set Telegram Chat ID in inputs
3. Create alert per chart: "Any alert() function call" with Telegram webhook

### Key Metrics to Track

| Metric | Promising | Concerning |
|--------|-----------|------------|
| TP1 hit rate | > 50% | < 45% |
| Time exits | < 30% of trades | > 50% of trades |
| Avg time exit P&L | > 0R | Consistently negative |
| Signal frequency | 3-5/week across 10 stocks | < 1/week |
| Signals after 12:00 | Some TP1 hits | Mostly SL or time exits |

### Decision Criteria
- **Go live:** TP1 hit rate > 50%, time exits < 30%, net positive R after 50+ signals
- **Tune parameters:** TP1 hit rate 45-50%, adjust pullback range or entry cutoff
- **Drop strategy:** TP1 hit rate < 42% or time exits > 50% consistently

---

## 7. Known Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Late-day entries leave insufficient time for 1R | HIGH | Entry cutoff at 14:00, time exit at 15:00 |
| Indian stocks mean-revert intraday | MEDIUM | VWAP + volume filters, pullback validation |
| Low signal frequency | MEDIUM | 10-stock universe, widened pullback range |
| Cost erosion (~0.19R/trade) requires >55% WR | MEDIUM | Monitor net R, not just WR |
| Correlation with ORB (same momentum stocks) | LOW | max_positions_per_symbol=1 in signal engine |

---

## 8. Future Integration (Post-Monitoring)

If monitoring phase is positive:
1. Add `HOD-FPB` to signal engine `strategy_profiles` in `config.yaml`
2. Add `HOD-FPB` blacklist section based on per-symbol monitoring data
3. Configure `tp_levels: {TP1: 1.0}` (full exit at 1R)
4. Route alerts through OpenAlgo TV webhook instead of direct Telegram
5. Signal engine handles entry, SL-M placement, TV-driven exits (same as ORB)

---

## 9. Shelved — Analysis (31 March 2026)

**Decision:** Strategy shelved after TradingView backtest results showed negative Total P&L across the target stock universe (Tier 1 F&O stocks, 5-min charts).

### Root Causes of Poor Performance

1. **Very low signal frequency** — The multi-filter pipeline (HOD breakout + RVOL + VWAP + pullback validation + regime filter + 1 trade/day limit) eliminates most setups. Typical output: 1-3 valid trades per month per stock, insufficient for compounding.

2. **Entry price mismatch** — `strategy.entry()` fills at next bar's open (market order), but TP/SL levels are calculated from `pullbackHigh` (assumed entry). If price gaps past the pullback high, actual R:R is worse than calculated.

3. **1R target with costs is marginal** — At 0.5% risk, 1R TP is ~0.5% of entry. Round-trip costs (0.06% commission × 2 + slippage) consume ~25-30% of each win. Effective win: ~0.7R vs -1R loss. Requires >60% win rate to break even — strategy does not deliver this.

4. **HOD is fundamentally weaker than ORB** — ORB uses a fixed level (opening range) that institutional flow anchors to. HOD is a moving target — every new high resets it. The "breakout" from a constantly updating level is statistically less meaningful.

5. **Monte Carlo pessimistic scenario was accurate** — The analysis report's pessimistic case (-8.4% CAGR, only 3.3% profitable runs) aligns with actual backtest results. The "base scenario" (19.5% CAGR) assumed theoretical entries without pullback filter attrition and transaction costs.

### Also Shelved: VWAP Mean Reversion

The intraday VWAP mean reversion strategy was also shelved. It targets range-bound days (low ADX, where breakout strategies fail), but the same cost structure and signal frequency issues apply to intraday mean reversion on Indian stocks.

### Recommendation

Focus development time on:
- **ORB strategy optimization** — already validated with live Q1 2026 data (+31% at 1% risk). D-grade symbol filter alone saves ~7.3R.
- **Swing strategies** (RSI-2, dividend growth) — longer holding periods amortize transaction costs better.

---

*End of Technical Requirements Document*
