# EMA9 Intraday — Analysis

> Sources: `ema9-intraday.pine` (the script), `791187576-9-EMA-Trading-Strategy.pdf` (the article)
> Adapters: `backtest/strategies/ema9.py` (mirrors the Pine), `backtest/strategies/ema9_pdf.py` (mirrors the PDF)
> Last updated: 2026-08-30

---

## Verdict

**Do not trade. Reference only.** Both the shipped Pine script and every method in the
source PDF have negative gross expectancy — they lose money with costs set to zero.

| Artefact | Trades | Gross bps @ 0 cost | Verdict |
|---|---|---|---|
| `ema9-intraday.pine` (mode C, shipped defaults) | 6,577 | negative, t = -9.90 | Do not trade |
| PDF worked method | 1,674 | -2.13 | Do not trade |
| PDF 9/20 EMA crossover | 5,345 | -3.15 | Do not trade |
| PDF 9 EMA / 30 WMA | 9,140 | -1.23 | Do not trade |
| PDF 9 EMA x VWAP after PD break | 3,098 | -3.68 | Do not trade |
| PDF 9/21/55 stack | 1,936 | -4.23 | Do not trade |
| PDF 9/15 cross + engulfing | 902 | -5.48 | Do not trade |

Universe: 208 NSE F&O stocks, 5-minute bars, 2026-06-04 to 2026-08-26 (59 sessions),
35 in-sample / 24 out-of-sample.

---

## 1. What the PDF actually contains

The document is an 11-page HowToTrade.com explainer, not a specification. It describes
**five crossover variants** and then **one worked method**. Only the worked method carries
a complete rule set:

1. Mark the previous day's high and low as support/resistance.
2. A break of either begins a possible new trend.
3. Wait for price to retrace back to the 9 EMA.
4. The first **engulfing** candle back in the trend direction is the entry.
5. Stop beyond that engulfing candle.
6. Take profit when a **doji** prints after a significant move.

### Two defects in the source

- **Five of the six methods specify no stop and no target at all.** The PDF gives them an
  entry trigger and nothing else. Any risk model used to test them had to be supplied by
  the adapter, which means those five were never complete strategies. `ema9_pdf.py` uses a
  swing stop plus a fixed reward-to-risk, and sweeps `tp_r`; this is stated in the adapter's
  module docstring so the supplied half is never mistaken for the source's.
- **The worked example is spliced from another article.** Page 9 ends by saying the entry
  will be "on the first FVG that forms in the bullish direction" and "must fall within the
  10 AM to the 11 AM interval". Fair value gaps and a fixed 10-11 window appear nowhere
  else in the document and contradict the engulfing rule stated two paragraphs earlier.

Weight the document accordingly. It is assembled content, not a tested system.

---

## 2. Results

Every variant is negative **before costs**, on samples of 902 to 9,140 trades. Sweeping the
target across 1:1, 1:1.5, 1:2 and 1:3 does not lift any of them above zero, so there is
nothing to tune — the entries themselves have no edge.

The in-sample / out-of-sample split shows the usual decay: four variants sit near zero
gross in-sample and then fall apart out-of-sample.

| Variant | Gross bps IS | Gross bps OOS |
|---|---|---|
| Worked method | +0.18 | -5.39 |
| 9/21/55 stack | +0.38 | -10.30 |
| 9 EMA x VWAP | -0.03 | -8.46 |
| 9 EMA / 30 WMA | -0.16 | -2.75 |

### The 1-minute check

The PDF specifies a 1-minute chart. yfinance serves only 7 days of 1-minute history against
60 days of 5-minute, so this is a weak check, but it was run for fairness and does not
rescue anything: the worked method turns slightly positive gross (+4.23 bps on 86 trades,
t = 1.18) and the 9/15 variant likewise (+5.83, t = 1.39). Neither is significant, and both
are heavily negative after costs — a 1-minute stop is tighter still, so the cost-to-R ratio
gets **worse**, not better.

---

## 3. Why it fails, and where the failure is shared

The PDF's worked method is a previous-day-level breakout with a pullback entry. That is the
same family as `breakout.pine`'s key-level engine, and it fails for the same measured
reason — see `../orb/breakout.md`, 2026-08-30. Key-level breaks on the NSE F&O universe
follow through **less** often than a driftless random walk (30.9% observed against a 33.3%
baseline, t = -9.87 across 49,677 events).

The engulfing-candle entry does not help. Measured directly on those same break events,
engulfing bars follow through 29.3% of the time (n = 8,731), which is *below* the
unconditional 30.9%. The PDF's central confirmation rule selects a slightly worse subset of
breaks than taking them all.

---

## 4. Reproduce

```
uv run --group analysis python -m signal_engine.backtest ema9 --full
uv run --group analysis python -m signal_engine.backtest ema9_pdf --full
```

`ema9_pdf.py` is deliberately separate from `ema9.py`. The latter mirrors
`ema9-intraday.pine`, the production artefact; the former mirrors the PDF. Keeping them
apart means the two can disagree and the disagreement stays visible. Select a variant with
`Ema9PdfParams(variant=...)`: `pdf`, `c920`, `c930`, `cvwap`, `c92155`, `c915`.
