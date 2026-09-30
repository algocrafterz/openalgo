# Turtle Soup Failed-Breakout Fade — Strategy Analysis

**File**: `signal_engine/backtest/strategies/turtle_soup.py` (`TurtleSoup`, `TurtleSoupParams`)
**Strategy tag**: `TURTLESOUP`
**Type**: Intraday, both directions
**Status: Dead end. Do not trade.** Real gross edge, but too thin per-trade to
survive any realistic cost, and no filter tested rescues it.

---

## Strategy Logic

Source: Linda Raschke / Laurence Connors, *Street Smarts* (1995) — the mirror
image of a Turtle breakout: bet that a break FAILS rather than trading it for
continuation.

```
short  High[i] > PDH  AND  Close[i] < PDH     (failed break UP -> fade down)
long   Low[i]  < PDL  AND  Close[i] > PDL     (failed break DOWN -> fade up)
stop   beyond the bar's own false-break extreme, ATR-buffered
target fixed R-multiple (tp_r)
```

**Why this one, specifically**: this repo's own `key_level.py` study already
measured that breaking PDH/PDL/IBH/IBL for continuation follows through only
30.9% of the time vs a 33.3% random-walk baseline (t=-9.87, 49,677 events) —
breaking a level is slightly WORSE than random. Every continuation strategy
tried here (`orb`, `ema9`, `key_level`, `ib_extension`) ignored that and traded
the break anyway. This adapter fades the EXACT SAME PDH/PDL levels instead of
inventing new ones, directly testing whether the fade captures what the break
loses.

---

## Backtest metrics

212-symbol NSE F&O universe, Historify 5-minute bars, 2016-10-03 to 2026-09-13
(IS 1458 sessions / OOS 972, cut 2022-10-06), 16 bps round-trip cost.

**Shipped defaults**: decisively negative.

| window | n | win% | gross bps | net_R | t | max_dd_R |
|---|---|---|---|---|---|---|
| IS | 89,555 | 41.9 | 3.43 | -0.310 | -51.61 | 27,732.7 |
| OOS | 76,109 | 40.4 | 0.79 | -0.389 | -50.77 | 29,615.4 |
| ALL | 165,664 | 41.2 | 2.22 | -0.346 | -71.03 | 57,346.2 |

**0 of 212 symbols were profitable.** Median -0.343R. But gross-of-cost (0 bps)
was significantly POSITIVE: net_R +0.040, t=7.29 — a real directional edge, just
microscopic per trade, and the strategy fires 165,664 times. 8 bps of cost alone
already flips it deeply negative (t=-32.85).

## Ablation — searching for a rescue

Tested wider cooldowns, an ADX trend-day filter, a bigger R:R target, and a
combo of all three (per `harness.py`'s discipline: a variant only counts if it
helps BOTH IS and OOS on gross bps):

| variant | n_IS | bps_IS | n_OOS | bps_OOS | helps |
|---|---|---|---|---|---|
| base | 89,555 | 3.43 | 76,109 | 0.79 | – |
| high_cooldown_20 | 88,711 | 3.45 | 75,320 | 0.78 | IS only (trivial) |
| high_cooldown_40 | 88,711 | 3.45 | 75,320 | 0.78 | IS only (trivial) |
| adx_filter_20 | 18,452 | 2.23 | 16,894 | 0.35 | **neither — actively hurts** |
| wider_target_2.5r | 89,377 | 4.35 | 75,941 | 1.20 | BOTH (only real single-axis win) |
| wider_target_3r | 89,293 | 4.67 | 75,842 | 1.34 | BOTH |
| combo (cooldown_40 + adx_20 + tp_r=2.5) | 18,395 | 2.70 | 16,843 | 0.63 | neither |

The combo — despite cutting trade count by ~80% — is still deeply net-negative:

| window | n | gross bps | net_R | t |
|---|---|---|---|---|
| IS | 18,395 | 2.70 | -0.362 | -26.17 |
| OOS | 16,843 | 0.63 | -0.429 | -30.61 |
| ALL | 35,238 | 1.71 | -0.394 | -38.86 |

Even at 0 bps this combo is only +0.032 net_R (t=2.93) — a thin edge that 8 bps
of cost alone (t=-18.11) already destroys.

**Notably: an ADX trend-day filter (skip fading on strong-trend days) made
results WORSE, not better**, contradicting the naive expectation that fades work
better specifically in choppy markets.

---

## Honest limits

- **The core thesis is correct in direction but the effect size is too small.**
  Fading a failed PDH/PDL break genuinely beats trading it for continuation, and
  the gross edge is statistically real (t=7.29 at zero cost) — but it is a few
  basis points per trade against 16 bps of unavoidable NSE round-trip cost.
- **No lever tested changes this conclusion.** Cooldown widening: no effect.
  Trend-regime filtering: actively harmful. Bigger target: the one real
  improvement, and still nowhere close to viable.
- **Same failure mode as `nr_breakout.py` (NR7/NR4)**, a different regime-aware
  variant on the same PDH/PDL mechanism tested the same session — both confirm
  that simple level-based intraday price action on NSE 5-minute bars does not
  have an edge large enough to survive real trading costs, regardless of how the
  entries are filtered.

## Layman summary

Betting that a stock's brief break above/below yesterday's high or low will
fail and snap back is a real, statistically provable idea on this data — it
just happens constantly, in tiny amounts, and every single trade pays a real
cost to place. No combination of "wait longer between trades," "only fade on
choppy days," or "aim for a bigger reward" changed that arithmetic. This is a
confirmed dead end, not a tuning problem.
