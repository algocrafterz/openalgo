# Daily Liquidity-Sweep Engulfing Continuation - Strategy Analysis

**Source:** `signal_engine/pinescripts/ideas/ideas.txt`, strategy:5 (Omar Agag's $500/day
prop-firm setup, Instagram reel). Original: 4H engulfing candle that sweeps the prior
candle's low marks a buy zone (lower half of the engulfing body); drop to 15m, wait for
a pullback into the zone with bullish market structure, enter with a stop below the zone,
target 2:1 R:R.

**Status: Tested. Dead end as shipped, and loosening it recreates a textbook overfit
rather than finding real edge.**

Adapter: `signal_engine/backtest/strategies/liquidity_sweep.py`
Registered as: `liquidity_sweep` in `signal_engine/backtest/__main__.py` (kind: `swing`)

---

## Why this setup

`turtle_soup.py` already proved the mechanism is real on NSE 5-minute bars: fading a
failed PDH/PDL break beats trading it for continuation (gross t=7.29 at zero cost), but
the edge is a few bps against a 16 bps round-trip cost - too thin to survive. This
strategy tests the same stop-hunt-and-reverse idea at a longer horizon (daily bars, held
for days), where transaction cost is a smaller fraction of the typical move and the edge
has more room to matter.

Historify does not carry clean 4-hour bars for the NSE universe, so the setup was recast
one timeframe down on both legs: the anchor is a DAILY bullish engulfing candle that also
undercuts the prior day's low (the sweep); the "15m pullback + structure" trigger becomes
a subsequent day that dips into the zone and closes above the rolling N-day high made
before it (the daily-bar proxy for "market structure realigns bullish"). Long-only,
CNC/delivery swing - matches `ema_pullback.py` and `rsi-tp-mr`'s convention.

## What the source left unspecified (see the adapter's docstring for the full list)

`min_engulf_body_pct` (how big the engulf must be), `zone_valid_days` (how long to watch
the zone), `structure_lookback` (what counts as a broken swing high), `stop_mode` (the
source names two different stop levels - "below the buy zone / below the sweep low" -
without picking one), and `tp_r` (kept at 2.0, the one number the source actually gave).

---

## Backtest

197-symbol NSE F&O universe, Historify split-adjusted daily bars, 2019-12-03 to
2026-09-13 (IS 1011 sessions / OOS 674, cut 2023-12-28), 24 bps round-trip cost.

**Shipped defaults:**

| window | n | win% | gross bps | net_R | t | t_naive |
|---|---|---|---|---|---|---|
| IS | 57 | 42.1 | 195.12 | 0.177 | 0.84 | 0.88 |
| OOS | 56 | 30.4 | -95.01 | -0.172 | -0.44 | -0.93 |
| ALL | 113 | 36.3 | 51.34 | 0.004 | 0.33 | 0.03 |

With 1 configuration searched, a result needs `|t| > 1.97` to beat chance. **Nothing here
clears it, in either window.** Worse than `turtle_soup`'s finding: even at 0 bps cost the
gross edge is not significant (net_R 0.064, t=0.75) - this rule shows no detectable
directional edge at all, gross or net, not just a thin one that costs eat. The signal is
also rare (113 trades across 197 symbols over ~7 years, roughly 1 trade per symbol every
12 years) - IS and OOS flip sign, which with n=56-57 per window is as consistent with
noise as with a real effect.

## Ablation - searching for a rescue

Six single-axis variants, each checked in both windows (`harness.py`'s discipline: only
"helps BOTH" counts):

| variant | n_IS | bps_IS | n_OOS | bps_OOS | helps |
|---|---|---|---|---|---|
| base | 57 | 195.12 | 56 | -95.01 | - |
| wider_zone_20d | 63 | 212.70 | 62 | -71.54 | BOTH (gross) |
| looser_structure_5d | 88 | 210.78 | 89 | -39.39 | BOTH (gross) |
| looser_engulf_40pct | 118 | 236.94 | 101 | 18.70 | BOTH (gross) |
| stop_sweep_low | 57 | 257.79 | 56 | -97.24 | IS only |
| tp_r_3 | 56 | 355.55 | 56 | -48.37 | BOTH (gross) |
| wider_zone_frac_0.75 | 145 | 272.00 | 137 | -4.04 | BOTH (gross) |

Four variants "help both" on **gross** bps, so the three biggest single-axis wins were
combined (`min_engulf_body_pct=40, zone_fraction=0.75, tp_r=3.0`) and checked on the
metric that actually matters - net-of-cost, out-of-sample:

| window | n | win% | gross bps | net_R | t |
|---|---|---|---|---|---|
| IS | 240 | 41.2 | 443.27 | 0.537 | 4.11 |
| OOS | 221 | 27.1 | 38.55 | -0.060 | 0.10 |
| ALL | 461 | 34.5 | 249.25 | 0.251 | 3.12 |

This is the textbook overfit signature the harness's IS/OOS split exists to catch: an
excellent-looking in-sample number (t=4.11, win% 41.2) that collapses to statistically
zero out-of-sample (t=0.10, win% 27.1, net_R **negative**). Each individual loosening
raised gross trade count and gross bps in both windows - but "more trades, more gross
return" is not the same as "the strategy has edge," and combining the individually
promising knobs did not produce a variant whose OOS net_R survives contact with real
cost. No stop_mode, target, or filter loosening tested rescues this.

---

## Honest limits

- **No real edge was found, gross or net.** Unlike `turtle_soup` (real gross edge,
  killed by cost) or `open_drive` (real edge, killed by adding more confluence), this
  strategy's shipped rule shows no statistically distinguishable effect at any cost
  level, including zero.
- **Small sample.** 113 trades from a 197-symbol, ~7-year panel is a genuinely rare
  signal; low power cuts both ways - this result does not prove the idea is impossible,
  only that this specific implementation, as tested, found nothing to trade.
- **Loosening filters reproduces overfitting, not discovery.** Every rescue attempt that
  looked good on gross bps failed net-of-cost out-of-sample. This is the same lesson
  `gap_rsi` and `nr_breakout` already recorded in this repo: a filter that "helps" only
  on an aggregate or in-sample number is not evidence.
- **The multi-day zone-persistence mechanic (anchor -> watch -> pullback -> structure
  break) has not been tested against a simpler baseline** (e.g., "buy any pullback after
  any down day that then breaks a local high") - it is possible the sweep/engulf
  specificity is adding nothing over a much plainer rule, but that comparison was not run
  given the base result was already non-significant.

## Layman summary

The idea was: a big reversal candle that fakes out a stop-hunt marks a buy zone; wait for
price to dip back into that zone and show signs of turning up again, then buy. This is
the same trick that works (barely) on 5-minute charts in this codebase, moved to daily
charts to see if it had more room to breathe. It didn't - across seven years and nearly
200 stocks, the rule as specified only fired 113 times, and those trades made money in
the training period and lost money in the untouched test period, which is the pattern
this repo's testing process specifically watches for as a sign of a coincidence rather
than a real edge. Loosening the rules to catch more trades made the in-sample number look
much better, but the untouched test period stayed flat or negative every time - a strong
signal that the "improvement" was fitting noise, not finding a real pattern. Verdict: not
worth trading as designed; not going to be rescued by tuning.
