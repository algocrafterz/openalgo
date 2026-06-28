# ORB Signal Performance — 2026 H1 (Jan 28 – Jun 24)

**Period:** 2026-01-28 to 2026-06-24 (5 months)
**Source:** Telegram channel `intraday-orb` export (`result.json`)
**Strategy:** ORB (Opening Range Breakout), 5-min chart, NSE equity MIS
**Engine:** Signal Engine v2 (OpenAlgo + TradingView alerts)
**Configuration at end of period:**
- Sizing: fixed_fractional, 1% risk/trade, slippage_factor=0.10
- Entry: MARKET order, SL: SL-M bracket
- TP: driven by TradingView TP HIT signals (MARKET exit)
- TP levels: TP1 (1R), TP1.5 (1.5R), TP2 (2R), TP3 (3R)
- Max open positions: 4 (reduced from 7 on 2026-05-12, data-driven)
- Max trades/day: 10
- No-progress gate: 90min / 20% (early gate disabled 2026-05-07)
- Loss-cut gate: -80% progress after 20min
- Time exit: 15:00 IST
- Capital: ~₹15K–₹35K (live, MIS 5x leverage)

**Methodology:** Per-exit counting (1 TP HIT or SL HIT = 1 unit). One entry can produce
multiple exits (TP1 + TP2). This is consistent with the Telegram export structure.
Cumulative R computed as sum of R-multiples (SL = -1.0R, TP at 1.5R = +1.5R, etc.).

---

## Overall Performance

| Metric | Value |
|---|---|
| Total exits tracked | **425** |
| Win rate | **72.0%** (306 TP / 119 SL) |
| Avg winning trade | +1.17R |
| Avg losing trade | -1.00R |
| Expected value / trade | **+0.562R** |
| Kelly fraction | 48% (trading 1% risk = ultra-conservative) |
| Cumulative R | **+238.7R** |
| Max win streak | 31 |
| Max loss streak | 6 |

---

## Monthly Performance

| Month | Trades | Wins | Losses | WR% | R/Month | R/Trade | Cumulative R |
|---|---|---|---|---|---|---|---|
| Jan 2026 | 3 | 0 | 3 | 0.0% | -3.0R | -1.00R | -3.0R |
| Feb 2026 | 139 | 100 | 39 | 71.9% | +79.8R | +0.57R | +76.8R |
| Mar 2026 | 103 | 68 | 35 | 66.0% | +53.8R | +0.52R | +130.5R |
| Apr 2026 | 101 | 88 | 13 | **87.1%** | +83.5R | +0.83R | +214.0R |
| May 2026 | 49 | 34 | 15 | 69.4% | +22.7R | +0.46R | +236.7R |
| Jun 2026 | 30 | 16 | 14 | 53.3% | +2.0R | +0.07R | +238.7R |

**Note:** April was the best month (+87.1% WR, +0.83R/trade). June is materially weaker
(53.3% WR, +0.07R/trade) — only 30 trades, likely choppy tape. Watch closely.

---

## Direction Analysis

| Direction | Trades | Wins | WR% | Avg Win R |
|---|---|---|---|---|
| LONG | 249 | 178 | **71.5%** | ~1.17R |
| SHORT | 176 | 128 | **72.7%** | ~1.17R |

Both directions perform nearly identically — strategy has no directional bias.

---

## TP Level Distribution

| Level | Count | % of All Trades |
|---|---|---|
| TP1 (1R) | 165 | 38.8% |
| TP1.5 (1.5R) | 108 | 25.4% |
| TP2 (2R) | 24 | 5.6% |
| TP3 (3R) | 9 | 2.1% |
| SL (-1R) | 119 | 28.0% |

TP1 + TP1.5 account for 64% of all outcomes. TP2/TP3 are rare but high-value events.

---

## Time-of-Day Analysis

| Hour (IST) | Trades | WR% |
|---|---|---|
| 09:xx | 5 | 80.0% |
| **10:xx** | **127** | **74.8%** |
| 11:xx | 119 | 70.6% |
| 12:xx | 75 | 73.3% |
| 13:xx | 41 | 63.4% |
| 14:xx | 30 | 70.0% |
| 15:xx | 28 | 75.0% |

Morning breakouts (10:xx) dominate volume. 13:xx shows the lowest WR — afternoon
signals are weaker (likely post-lunch low-volume noise).

---

## Symbol Performance (min 5 trades)

### A-Grade Symbols (WR ≥ 80%)

| Symbol | Trades | WR% | Net PnL% |
|---|---|---|---|
| EXIDEIND | 21 | **95%** | +26.2% |
| SAIL | 15 | **93%** | +24.2% |
| TMPV | 13 | **92%** | +10.2% |
| RECLTD | 17 | **88%** | +16.9% |
| ONGC | 7 | 86% | +9.0% |
| APOLLOTYRE | 19 | 84% | +11.0% |
| EMAMILTD | 18 | 83% | +13.2% |
| PNB | 6 | 83% | +3.2% |
| BPCL | 12 | 83% | +13.2% |
| HUDCO | 6 | 83% | +6.3% |

### B-Grade Symbols (WR 65–79%)

| Symbol | Trades | WR% | Net PnL% |
|---|---|---|---|
| JSWENERGY | 20 | 80% | +21.7% |
| NATIONALUM | 22 | 82% | +25.9% |
| SBIN | 10 | 80% | +0.9% |
| WIPRO | 12 | 67% | +4.0% |
| TATASTEEL | 13 | 69% | +6.0% |
| PFC | 22 | 64% | +6.2% |

### C/D-Grade Symbols (WR < 60%)

| Symbol | Trades | WR% | Net PnL% | Action |
|---|---|---|---|---|
| ADANIPOWER | 12 | 58% | +5.2% | Watch |
| BANKBARODA | 12 | 58% | +4.2% | Watch |
| JINDALSTEL | 9 | 56% | +1.4% | Watch |
| BALRAMCHIN | 8 | 50% | +1.8% | Watch |
| USHAMART | 9 | 44% | +1.0% | Consider soft-blacklist |
| HINDPETRO | 5 | 40% | -1.5% | Consider soft-blacklist |
| MANAPPURAM | 10 | 30% | -3.3% | **Add to soft-blacklist** |
| BHEL | 6 | **0%** | -6.0% | Already hard-blacklisted ✓ |

---

## June 2026 Daily Breakdown

| Date | Trades | W/L | WR% | Symbols |
|---|---|---|---|---|
| Jun 01 | 1 | 1/0 | 100% | APOLLOTYRE |
| Jun 02 | 1 | 0/1 | 0% | ADANIPOWER |
| Jun 03 | 3 | 0/3 | 0% | SBIN, TATASTEEL, MANAPPURAM |
| Jun 05 | 2 | 1/1 | 50% | BANKBARODA, VEDL |
| Jun 08 | 2 | 0/2 | 0% | ADANIPOWER, JSWENERGY |
| Jun 09 | 4 | 3/1 | 75% | NATIONALUM, BANKBARODA ×3 |
| Jun 10 | 3 | 2/1 | 67% | MANAPPURAM, PFC ×2 |
| Jun 11 | 1 | 0/1 | 0% | BANKBARODA |
| Jun 15 | 1 | 0/1 | 0% | PFC |
| Jun 17 | 1 | 1/0 | 100% | APOLLOTYRE |
| Jun 18 | 1 | 0/1 | 0% | RECLTD |
| Jun 19 | 2 | 2/0 | 100% | BPCL ×2 |
| Jun 22 | 1 | 1/0 | 100% | RECLTD |
| Jun 23 | 1 | 1/0 | 100% | NATIONALUM |
| Jun 24 | 6 | 4/2 | 67% | NATIONALUM, JSWENERGY, INDUSINDBK ×3, BPCL |

June shows high day-to-day volatility in WR. Multiple zero-WR days indicate choppy tape.
JSWENERGY and ADANIPOWER are both 0/2 in June — consider temporary soft-blacklist.

---

## Blacklist Recommendations

Based on H1 data:

| Symbol | H1 WR | H1 Trades | Recommendation |
|---|---|---|---|
| BHEL | 0% | 6 | Hard-blacklisted (no change) |
| MANAPPURAM | 30% | 10 | Add to `ORB.soft` (50% qty) |
| HINDPETRO | 40% | 5 | Watch (only 5 trades) |
| USHAMART | 44% | 9 | Add to `ORB.soft` (50% qty) |
| JSWENERGY | 0% in Jun | 2 Jun | Monitor — strong H1 overall (80% WR) |
| ADANIPOWER | 0% in Jun | 2 Jun | Monitor — 58% WR overall |

---

## Known Issues / Observations

1. **June 2026 performance drop**: WR fell from 87% (Apr) to 53% (Jun). Fewer trades
   (30 vs 101 in Apr) suggest reduced signal quality or tighter filters active. The
   choppy June tape (multiple 0/3 and 0/2 days) explains most of this.

2. **Capital floor hits on Jun 3 and Jun 5**: `live=248 < min=5,000` blocked new entries.
   A single large losing day depleted available margin, blocking subsequent signals.
   Consider raising capital allocation or lowering `min_capital_for_entry`.

3. **APOLLOTYRE broker-rejected on Jun 1**: Flattrade rejects APOLLOTYRE for MIS
   regardless of capital. Now in `broker_restrictions.flattrade.mis_rejected` — no
   further action needed.

---

## Config Evolution Since Q1

| Date | Change | Rationale |
|---|---|---|
| Mar 12 | Multi-TP levels added (TP1.5, TP2, TP3) | Capture extended runners |
| May 12 | max_open_positions: 7 → 4 | Signals 3+ WR only 57%, chop amplified losses |
| May 07 | Early no-progress gate disabled | Was exiting winners too early |
| Jun 01 | APOLLOTYRE → mis_rejected | Broker rejects it for MIS regardless |
| Jun 28 | daily/weekly/monthly loss limits = 1.0 | TESTING only — re-enable before live |

---

*Generated: 2026-06-28 | Source: result.json (890 Telegram messages, 425 exits parsed)*
