# NR7/NR4 Narrow-Range Breakout — Strategy Analysis

**File**: `signal_engine/backtest/strategies/nr_breakout.py` (`NrBreakout`, `NrBreakoutParams`)
**Strategy tag**: `NRBREAKOUT`
**Type**: Intraday, both directions
**Status: Dead end. Do not trade.** The volatility-regime filter helps directionally
but the underlying edge it's filtering is too weak to matter.

---

## Strategy Logic

Source: Toby Crabel, *Day Trading with Short-Term Price Patterns*. Trade a
breakout of yesterday's high/low FOR CONTINUATION — but only when yesterday's
own session range was the narrowest of its trailing N sessions (N=7 is the
classic "NR7"; N=4, "NR4", is stricter/rarer). Premise: a volatility squeeze
precedes an expansion.

```
qualify  yesterday's range <= min(trailing nr_lookback days' ranges)
long     Close[i] > PDH  AND  Close[i-1] <= PDH  AND  yesterday qualified
short    Close[i] < PDL  AND  Close[i-1] >= PDL  AND  yesterday qualified
```

**Why this one, specifically**: with `require_nr=False` this collapses to
EXACTLY `key_level.py`'s PDH/PDL-break-for-continuation mechanism, already
measured in this repo as having no edge (30.9% follow-through vs 33.3% random,
t=-9.87). Testing `require_nr=True` vs `False` is a clean, direct A/B on
whether the narrow-range regime filter rescues a mechanism already known not to
work unconditionally — not a fresh, unrelated idea.

The only prior mention of NR7 in this repo was `orb.pine`'s `enableNRFilter`
input (added 2026-04-25, default off) — an unvalidated live toggle, checked
only via manual TradingView Strategy Tester. This is the first time NR7/NR4 has
been run through this repo's own rigorous Python backtest harness.

---

## Backtest metrics

212-symbol NSE F&O universe, Historify 5-minute bars, 2016-10-03 to 2026-09-13
(IS 1458 sessions / OOS 972, cut 2022-10-06), 16 bps round-trip cost.

**Shipped defaults (NR7, `require_nr=True`)**: decisively negative.

| window | n | win% | gross bps | net_R | t |
|---|---|---|---|---|---|
| IS | 17,212 | 40.5 | 0.16 | -0.328 | -25.81 |
| OOS | 13,888 | 40.4 | 1.06 | -0.372 | -24.30 |
| ALL | 31,100 | 40.4 | 0.57 | -0.348 | -35.26 |

Only 5 of 212 symbols profitable, median -0.346R. Gross-of-cost (0 bps) is only
barely positive (net_R +0.026, t=0.88 — not significant).

## Ablation — testing the actual thesis

| variant | n_IS | bps_IS | n_OOS | bps_OOS | helps |
|---|---|---|---|---|---|
| base (NR7) | 17,212 | 0.16 | 13,888 | 1.06 | – |
| require_nr_false (no filter) | 84,860 | -2.37 | 69,646 | 0.18 | **neither beats base** |
| nr4 (stricter) | 27,683 | -0.94 | 22,289 | 0.73 | neither beats base |
| wider_target_2r | 17,212 | 0.29 | 13,888 | 1.68 | BOTH beats base |
| combo (nr4 + wider) | 27,683 | -1.01 | 22,289 | 1.22 | OOS only |

**Reading this correctly**: `require_nr_false` failing to beat `base` in either
window means the NR7 filter (base) genuinely helps VERSUS no filter at all —
Crabel's underlying intuition has real, directionally-correct signal.
`require_nr_false`'s own net-of-cost numbers confirm this is even worse than
`key_level.py`'s original PDH/PDL-break finding: t=-68.31 ALL (154,506 trades,
every one of them an unconditional level break).

But NR4 (stricter selection) did NOT beat NR7 — slightly worse in both windows.
And even the best single lever, `wider_target_2r` (the only variant clearing
"helps BOTH"), is still only marginal gross-of-cost and — based on every other
net-of-cost table produced this session on this same signal family — nowhere
close to surviving 16 bps of real cost.

---

## Honest limits

- **The filter works in the direction the theory predicts, but the effect size
  is tiny.** NR7 beats no-filter; NR4 doesn't beat NR7. More selectivity is not
  automatically better.
- **The edge being filtered was never big enough to matter.** Even in its best
  configuration this is a fraction of a basis point to ~1-2 bps of gross edge
  against 16 bps of unavoidable cost — the same order-of-magnitude failure as
  `turtle_soup.py`, tested the same session on the same PDH/PDL mechanism.
- **Third confirmed dead end on this exact mechanism family** (PDH/PDL
  continuation break), after `key_level.py` (unconditional) and now both the
  fade (`turtle_soup.py`) and the volatility-regime-gated version
  (this file). Strong cumulative evidence that PDH/PDL-based intraday price
  action on NSE 5-minute bars, in any of the three framings tried, does not
  clear realistic Indian trading costs.

## Layman summary

Only trading a breakout after an unusually quiet, "coiled" day is a real idea —
and it genuinely does better than trading every breakout blindly, confirming
the theory. The problem is what it's improving FROM: even the best version of
this idea barely makes any money before costs, and just doesn't clear what it
actually costs to trade on the NSE. Being more selective (NR4 instead of NR7)
didn't help either — this is now the third way of trading "yesterday's high or
low" that's come up empty this session.
