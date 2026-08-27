# Momentum-Rank Strategy Analysis

**File**: `momentum-rank.pine`
**Strategy name**: `momentum-rank`
**Type**: Positional, long only (CNC delivery), cross-sectional ranking
**Timeframe**: Daily chart only — enforced by `runtime.error`
**Status**: Candidate. Never traded. Paper first.

---

## Strategy Logic

Once every 21 sessions, rank a configured universe by **12-month return skipping the most
recent month** and hold the strongest N, equally weighted. There is no entry trigger, no
stop and no target — a name is sold when it stops being one of the strongest.

```
score  = close[21] / close[250] - 1        (evaluated on DAILY bars)
hold   = top N by score, equal weight
exit   = the name falls out of the top N at a rebalance
```

The 21-session skip is the classic 12-1 construction: the most recent month runs on
short-horizon reversal, which works against momentum.

---

## Where the edge comes from

Two separate things, and the second matters more than the first.

**1. Momentum.** Stocks that have risen tend to keep rising. Measured over 201 NSE F&O
names, 2016-2026, monthly rebalance, top 30, equal weight, against an **equal-weight basket
of the same universe** so market beta is not counted as skill:

| factor | alpha p.a. | t | IS / OOS |
|---|---|---|---|
| **12-1 momentum (250d, skip 21d)** | **+12.67%** | **3.12** | **+12.16 / +13.35** |
| trailing 250d return | +12.83% | 3.07 | +9.49 / +17.43 |
| trailing 120d return | +9.69% | 2.61 | +8.64 / +11.23 |
| position in 120d high-low range | -0.47% | -0.07 | +1.25 / -2.94 |
| range position / volatility | -7.54% | -2.22 | negative both |

Note the range-position row. An earlier pooled forward-return test showed the range factor
predicting, but as a *ranking* factor it is worthless — it saturates, with dozens of names
sitting at 97-100 and no longer separable. A raw return keeps ranking them. That
construction difference is the whole strategy.

**2. Cost tolerance.** Turnover is ~21% of the book per month, so charges are paid roughly
once a quarter per name instead of twice a day. At 150 bps round trip — seven times
realistic — alpha was still **+8.2%/yr**. For comparison, `orb.pine` breaks even at 11 bps.
The edge is not larger than ORB's in percentage-of-risk terms; it simply is not eaten.

---

## Robustness

Every axis was swept. None of it is load-bearing on a single parameter.

| axis | tested | result |
|---|---|---|
| lookback | 180 / 250 / 300 / 400d | 12.6-13.8% alpha, t ~3 (120d weaker at 6.6%) |
| rebalance | 21 / 42 / 63 / 126 sessions | 11-13% alpha |
| portfolio size | 10 / 20 / 30 / 50 / 80 | alpha falls with size, **Sharpe flat at ~1.48** |
| cost | 0 / 22 / 40 / 80 / 150 bps | +13.45 down to +8.20% |

The portfolio-size row is the one to read carefully: concentration buys return *and* risk in
equal measure. Top 10 gives +23% alpha at 29% volatility; top 30 gives +12.7% at 22.9%.
Sharpe barely moves. Smaller is not better, it is levered.

---

## Backtest metrics

### Per position (one row per holding, entry to exit, net of 22 bps)

| | tested: top 30 of 201 | shipped: top 8 of 40 |
|---|---|---|
| positions | 693 | 173 |
| win rate | 55.7% | 54.3% |
| average win | +45.2% | +61.5% |
| average loss | -10.6% | -9.5% |
| **win/loss ratio** | **4.27** | **6.51** |
| **profit factor** | **5.37** | **7.75** |
| expectancy | +20.5% | +29.1% |
| best / worst | +1050% / -67.2% | +1514% / -49.8% |
| median hold | 63 days | 62 days |
| mean hold | 136 days | 145 days |

**There is no R:R.** No stop, no target — the comparable number is the realised win/loss
ratio above. You win barely more than half the time, but winners run six times the size of
losers. That asymmetry is the mechanism: momentum lets a few enormous winners compound for
years while failures are cut at the next rebalance after a small loss.

### Holding period distribution (shipped config)

| bucket | share |
|---|---|
| under 1 month | 21% |
| 1-3 months | 41% |
| 3-6 months | 14% |
| 6-12 months | 13% |
| over 1 year | 11% |

### Portfolio level (shipped config, 8 of 40, 105 months)

```
CAGR                    36.9%
equal-weight benchmark  26.2%
alpha                  +10.7%/yr
Sharpe                   1.43
volatility              24.4%/yr
max drawdown            25.2%
best month             +19.8%
worst month            -23.4%
positive months           68%
months beating bench      58%
turnover                  21%/month  (~3 orders per rebalance)
```

---

## Honest limits — read before funding this

- **Alpha is lumpy.** Nine of ten years positive, but by year: +3.7, +8.2, +9.0, +2.6,
  **+31.9**, +4.2, **+53.5**, **+23.1**, -7.6, +3.8. Median year is about **+6%**; the mean
  is dragged up by 2021, 2023 and 2024. Expect single digits most years.
- **It does not protect you in a crash.** Feb-Mar 2020: book -29.4% against the universe's
  -27.6%. Momentum owns whatever was strongest, which is what gets sold first in a panic.
- **Survivorship bias is present and not fixable here.** The universe is today's F&O list
  walked backwards ten years; 156 of 201 names have full history. Names delisted or dropped
  from F&O since 2016 are absent. Reporting alpha against the same-universe benchmark cancels
  much of this, not all of it. A point-in-time universe would settle it.
- **The shipped 8-of-40 config is more concentrated than what was validated.** The robust
  evidence is 30-of-201 across 693 positions; 173 positions is a smaller sample, and the
  larger win/loss ratio partly reflects that concentration.
- **Not yet compiled on TradingView.**

---

## Implementation notes

- **One `request.security` per symbol**, returning momentum and price as a tuple. Fetching
  them separately would need 80 calls against Pine's limit of 40.
- **Daily charts only.** `rebalDays` is counted in `bar_index`, which advances per *chart*
  bar — on a 5-minute chart "21" would mean 21 five-minute bars, about 1.7 hours. The
  momentum itself is immune (evaluated inside `request.security(..., "D", ...)`), which is
  what made the bug invisible in the values. Guarded with `runtime.error`.
- **SL and TP in the alert are placeholders**, written 25% and 100% away. This system has no
  price stop, but `parser.py` requires the fields and the validator rejects an
  unrealistically tight stop.
- **`Entry:` is a reference price**, the rebalance bar's close. A daily alert fires after
  15:30, so a market order fills at the next open. Booking the whole test that way moves CAGR
  37.67% -> 37.91%, so the field is informational rather than wrong. It does feed position
  sizing (`qty = risk / |entry - sl|`), by about 0.2%.

---

## Verification against `portfolio.py`

The Pine's control flow was replayed in Python over the same 40 symbols and bars:

```
basket selection    105 of 105 rebalances IDENTICAL
signal dates        identical
per-period returns  max difference 0.0000% over 105 periods
book CAGR           37.67% both
```

Two residual differences, both benign: `portfolio.py` applies the min-price filter to its
benchmark and the replica does not (bench 26.19% vs 26.62%, alpha 11.48 vs 11.04 — the book
is identical, only the yardstick moves); and the fill assumption noted above.

`portfolio.py`'s validity mask requires the exit price to exist at selection time, which is
technically lookahead. On this universe it changes nothing (37.67% / 11.48% either way)
because all 40 names have continuous data. It stays a latent issue for any universe
containing delistings.
