# IBS (Internal Bar Strength) Mean Reversion — Strategy Analysis

**File**: `signal_engine/backtest/strategies/ibs_meanrev.py` (`IbsMeanRev`, `IbsMeanRevParams`)
**Strategy tag**: `IBSMR`
**Type**: Swing, long only (CNC delivery), single-symbol mean reversion
**Status: Marginal, not proven. Do not trade.** Meaningfully improved from clearly
losing to roughly break-even with one fix, but never cleared the bar for a real
edge, and a second fine-tuning pass found the ceiling.

---

## Strategy Logic

```
IBS   = (Close - Low) / (High - Low)         a 0-1 score of where a bar closed in
                                               its own daily range
entry = buy at tomorrow's open when YESTERDAY's IBS < 0.20 (a weak close)
exit  = sell at today's close when TODAY's IBS >= 0.80, or after max_hold_days
stop  = entry - sl_atr_mult x ATR(14)  (protective only, not the reversion signal)
```

Source: Pagonidis (2013), extended across asset classes (SPY, QQQ, gold, bitcoin)
by Alvarez Quant Trading / QuantifiedStrategies with the SAME parameters
unchanged — the strongest evidence for this candidate was that cross-market
robustness. Published evidence is mostly US ETFs/indices; single-stock NSE F&O
evidence was thin going in, which is exactly what this backtest tested.

---

## Where the edge comes from — and where it dies

Full 197-name NSE F&O universe, Historify daily bars, 2019-12-03 to 2026-09-13
(IS 1011 sessions / OOS 674, cut 2023-12-28), 24 bps round-trip cost.

**Shipped defaults**: net-NEGATIVE after cost.

| window | n | win% | gross bps | net_R | t | max_dd_R |
|---|---|---|---|---|---|---|
| IS | 19,903 | 55.3 | 12.00 | -0.021 | -1.48 | 724.7 |
| OOS | 17,133 | 54.3 | 0.65 | -0.046 | **-4.93** | 858.5 |
| ALL | 37,036 | 54.8 | 6.75 | -0.033 | -4.13 | 1233.9 |

Gross-of-cost (0 bps) was barely positive (net_R +0.009, t=0.90 — not
significant). Stop-losses were 14.2% of trades and the single biggest drag
(avg -1.04R) — a classic falling-knife problem: buying a "weak close" that is
really just a real downtrend, not overreaction.

**Fine-tune #1 (both windows improved)**: `use_trend_filter=True,
require_above_sma=True, trend_sma_len=100` (only buy a weak close if price is
STILL above its 100-day average — a pullback filter, not a bet the downtrend
reverses) + `sl_atr_mult=3.5` (wider stop than the 2.0 default). Per this repo's
`harness.py` discipline, a filter only counts if it helps BOTH windows on gross
bps — this one did: 12.00→29.59 IS, 0.65→13.38 OOS. SL-hit rate dropped from
14.2% to 2.7% of trades.

Net-of-cost after this fix — still not proven:

| window | n | win% | gross bps | net_R | t |
|---|---|---|---|---|---|
| IS | 11,124 | 55.7 | 29.59 | +0.003 | 0.50 |
| OOS | 9,081 | 55.5 | 13.38 | -0.015 | **-2.44** |
| ALL | 20,205 | 55.6 | 22.31 | -0.005 | -1.23 |

Cost sensitivity: sign flips between 10 bps (t=1.31, marginally positive) and 20
bps (t=-0.50) — sitting right on the realistic-cost knife edge, exactly like
`orb.pine`'s "breaks even at 11 bps" framing.

**Fine-tune #2 (2026-09-18, 15 further configs tried)**: searched longer trend
filters (150/200d SMA), higher price floors (100/200), tighter entry (IBS<0.15),
longer holds (8/10/12/15 days), wider stops (4.0/4.5 ATR), and combinations, all
built on top of fine-tune #1's base. **No configuration got OOS to a defensible
|t|>2 with the same sign as IS.** Every variant that pushed IS further into
significant-positive territory (best: t=2.59) left OOS still negative in every
single case (t range -1.78 to -2.44) — the sign never flipped, only the
magnitude shrank somewhat. Closest candidate, `trend_sma_len=100, sl_atr_mult=4.0,
max_hold_days=15`:

| window | n | net_R | t |
|---|---|---|---|
| IS | 9,146 | +0.022 | **2.53** |
| OOS | 7,861 | -0.007 | -1.89 |
| ALL | 17,007 | +0.009 | 0.70 |

---

## Honest limits — read before touching this again

- **This is a structural IS/OOS split, not noise from one bad variant.** Every
  lever tested (trend-filter length, price floor, entry threshold, hold length,
  stop width, and combinations) improves IS more than OOS, consistently. That
  pattern — not a single outlier config — is what makes "just try more params"
  unlikely to fix it.
- **Most plausible explanation: a regime shift, not a broken idea.** The
  2019-2023 window (mostly IS) behaves genuinely differently from 2024-2026
  (mostly OOS) for this signal on NSE single stocks. Published evidence for IBS
  is strongest on broad indices/ETFs; single-stock evidence was always the weak
  point, and this backtest is consistent with that weakness being real here.
- **Not fixable by more single-parameter search on this same split.** A
  different angle — sector/liquidity-tier restriction, or a rolling/walk-forward
  evaluation instead of one fixed IS/OOS cut — would be needed to tell whether
  this is genuinely dead or just needs a different lens. Not attempted here.
- **Mechanistically different from `gap_rsi.py`** (this repo's other failed
  mean-reversion attempt): gap_rsi bet on RSI-extreme-plus-gap and failed because
  the median trade stopped out on the entry bar itself. IBS's failure mode is
  different — it wins more than half the time and the gross edge is real, it
  just doesn't clear realistic cost with a statistically defensible OOS margin.

## Layman summary

Buying stocks right after a weak, low-closing day and selling them after a
strong bounce is a real, well-documented idea — and on this platform's own data
it went from a clear loser to something genuinely better once we added "only
buy the dip if the stock is still in a longer uptrend" and gave the stop more
room. But even the best version found sits almost exactly at break-even after
real trading costs, and pushing harder on the same knobs just made the
earlier half of the test period look better without ever fixing the more
recent half. That's a sign the market itself may have changed for this kind of
trade, not that the settings are wrong — not tradeable as-is.
