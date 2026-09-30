# Extreme-RSI Breakaway Gap ("1:10 Risk Reward") - backtest study

**Source:** [Trading Strategy with 1:10X Risk Reward](https://www.youtube.com/watch?v=uLTaXTyDxiI) -
Kirubakaran Rajendran, 2026-07-22. 24,650 views, 1,130 likes, 85 comments. Tamil, auto-captions only.

**Status: NOT SHIPPED.** No `.pine` file was written. The idea does not clear the cost line
in the form the video describes, and the one variant that looks better is no longer the
video's strategy.

---

## Verdict

**No tradeable edge as described.** Across 201 NSE F&O names and 462,453 bar-days
(2016-08-29 to 2026-08-27), the strategy exactly as specified produced **9,545 trades at
-11.22 bps gross per trade** (naive t = -5.43, monthly-clustered t = -4.15 IS / -1.93 OOS)
against a cost line of 8-22 bps. It loses before costs and loses more after them.

The result decomposes into two findings that point in opposite directions, and only one
of them is statistically solid:

| Side | n | Win % | Gross bps | Payoff | t (naive) | t (monthly) | Years positive |
|---|---|---|---|---|---|---|---|
| **Short** (overbought -> gap down) | 4,644 | 15.7 | **-66.01** | 2.64 | -14.62 | **-4.35** | **0 of 11** |
| **Long** (oversold -> gap up) | 5,174 | 20.2 | +38.19 | 4.23 | 1.14 | **0.50** | 9 of 11 |

- **The short side is robustly, significantly negative.** Every single year, 20% of months
  positive, and no parameter setting rescues it. This is the clearest result in the study.
- **The long side is positive but indistinguishable from zero.** Bootstrapping monthly
  means over 117 months gives a 95% CI of **[-0.205, +0.402] R** with P(mean > 0) = 67%.
  A coin flip with a positive lean.

The video's core *claim about payoff structure* holds - a 20% win rate with a 4.2:1 payoff is
exactly what it promises. It just isn't enough: 20% x 4.23 lands slightly under breakeven
once costs are paid.

---

## What the strategy actually is

The video is a Tanglish talk with no written rules, so this is reconstructed from the
transcript plus the author's own replies in the comments. Every gap the video leaves open
is an **input** in the adapter, not a hardcoded constant, so it could be tested rather
than argued about.

**Premise** (transcript): retail chases win rate, professionals chase payoff; a 30%-accurate
setup at 1:10 beats 70% at 1:1. The vehicle chosen to deliver that payoff is the
**breakaway gap out of an exhausted trend**:

| | Long | Short |
|---|---|---|
| Condition (prev close) | RSI in extreme **oversold** zone (< 10) | RSI in extreme **overbought** zone (> 90) |
| Trigger | today **gaps up** | today **gaps down** |
| Entry | today's open | today's open |
| Stop | previous close ("the gap filled, thesis dead") | previous close |
| Target | none - hold until close back through the 21 EMA | same |

The tiny risk unit is the whole trick: risk = the gap, ~1.5% of price, so a multi-week
trend ride divides into a large R multiple.

### The RSI ambiguity - and the answer

This is the centre of the strategy and the video is genuinely unclear. **The author gave
two contradictory answers in his own comments:**

- to `@AnandanK_0729`: *"First add the EMA to your chart, then open RSI settings and check
  the Source dropdown, EMA will appear there."* -> **RSI of the EMA**
- to `@sekarkanna1038`: *"The EMA option is under the Smoothing section ... that applies the
  EMA on the RSI line."* -> **EMA of the RSI**

**The data settles it decisively.** Over 462,453 bar-days:

| Construction | Long signals in 10 years |
|---|---|
| RSI(14) **of EMA(21)** | 3,423 |
| EMA(14) **of** RSI(14) | 30 |
| plain RSI(14) of close | 25 |

A plain or smoothed RSI essentially never reaches 10 - roughly two signals a year across
the entire F&O universe. Only RSI computed **on the EMA series** produces a usable
population, and only it can print the `1.67` reading shown on screen (the EMA is
near-monotone, so in a downtrend almost every delta is negative and the RSI pins near
zero). The reply to `@sekarkanna1038` is wrong; `@AnandanK_0729`'s answer is the strategy.

### Rules the video never specifies

| Gap | Asked in comments by | Resolved as |
|---|---|---|
| What size move counts as "a gap" | `@pushkardivedula162` | input `min_gap_pct`, default 1% |
| RSI length - 14 in the video, 21 in one slide, 10 in another | `@MVignesh-i2o`, `@srisiva1265` | input `rsi_len`, default 14 |
| How to filter the universe | ~12 commenters, **never answered** | rule-based: the full NSE F&O list |
| Whether an EMA cross is also required | `@abnirmal` | input `require_ema_cross`, default off |

One assumption had to be made that the video does not state, and it matters:

> **The EMA exit must arm before it can fire.** Taken literally, "hold until it closes below
> the 21 EMA" exits on the entry bar every single time - a stock oversold enough to read
> RSI < 10 is by construction trading *below* its 21 EMA. Implemented literally the strategy
> returns **-2.31 bps** in-sample. The arming rule (exit on the first close back through the
> EMA *after* price has closed beyond it) is the only reading that makes the rule a trend
> exit, and it is the generous one. All results above use it.

---

## Why it fails

### 1. The median trade is stopped out on the entry bar

Median holding period: **0 days**. The stop is the previous close, ~1.5% away, while the
entry is a gap-up open - and gaps partially fill intraday far more often than they run.
The exit mix over the full period:

| Exit | Share | Mean R |
|---|---|---|
| Stop | 74.5% | -1.13 |
| Stop, gapped through | 2.7% | -1.78 |
| EMA trend exit | 22.6% | +3.10 |

The structure works exactly as designed. It simply doesn't pay.

### 2. The entire long-side profit is four trades

Of 5,174 long trades, the **top 1% (51 trades) contribute 1,549R against a total gross of
997R** - i.e. the other 99% collectively lose money. The largest: HINDZINC +115R
(2024-04-01), GLENMARK +67R (2020-03-31, the COVID bottom), BDL +57R, TITAN +51R.

Cap any single trade - which is what *any* real position-sizing, trailing, or risk rule
does - and the edge evaporates:

| Cap on one trade | none | 50R | 20R | 10R | 5R |
|---|---|---|---|---|---|
| Gross bps | +38.2 | +36.2 | +22.1 | **-1.8** | **-32.2** |

**The strategy titled "1:10 Risk Reward" is worth nothing at 10R.** Testing a literal fixed
10R target confirms it: it *underperforms* the open-ended EMA exit in both windows
(10.16 IS / 42.82 OOS vs 28.48 / 57.18). The open-ended trend exit is what generates the
tail; the advertised 1:10 is precisely the level at which the edge disappears.

### 3. It needs unlimited capital

Signals arrive in market-wide clusters - 87 on 2020-04-07, 60 on 2026-04-01 - because a
crash-recovery morning gaps every oversold stock up at once. The strategy requires a
**median of 10 and a maximum of 114 concurrent positions**. Impose a realistic slot cap and
it inverts:

| Concurrent slots | 3 | 5 | 10 | unlimited |
|---|---|---|---|---|
| Gross bps | -30.8 | -15.3 | -3.8 | **+38.2** |
| Total | -661R | -633R | -609R | +342R |

The positive number exists only for an account that can hold every signal on the biggest
gap day of the decade. Under any capital constraint a retail trader actually faces, the
long side is **negative**. This alone disqualifies the strategy as published.

---

## What did survive

Two changes helped in **both** windows and are mechanically justified, not curve-fitted:

- **Stop at the previous day's low, not the previous close.** `cost_R = cost_pct x price / risk`,
  so widening the risk unit cuts cost in R *and* stops the same-bar stop-out that kills
  74% of trades. 87.4 bps IS / 109.2 bps OOS, versus 28.5 / 57.2.
- **Require a 2% gap, not any gap.** 51.7 IS / 115.9 OOS. Bigger gaps carry more conviction;
  the effect reverses above 5%, where the move is news rather than a setup.

Together ("mechanical" config, long only):

| Window | n | Win % | Gross bps | Payoff | t (naive) | t (monthly) |
|---|---|---|---|---|---|---|
| IS | 987 | 35.6 | 103.78 | 2.23 | 1.83 | 1.23 |
| OOS | 590 | 45.9 | 178.50 | 2.06 | 4.00 | 1.05 |
| ALL | 1,577 | 39.4 | **131.74** | 2.15 | 3.77 | **1.57** |

This is genuinely better: it survives a 10R cap (109.3 bps), 56% of symbols are profitable,
the top 1% share falls from 155% to 57%, it is positive in 8 of 11 years, and the
bootstrap gives P(mean > 0) = 95.3%.

**But three caveats, and they are decisive:**

1. **It is no longer the video's strategy.** Payoff drops from 4.2:1 to 2.15:1 and the win
   rate doubles to 39%. Widening the stop is exactly what destroys the "1:10" premise. This
   is an ordinary 1:2 mean-reversion swing, not the thing being taught.
2. **It still fails the concurrency test.** At 5 slots it returns +65R total over ten years
   against a 60.5R maximum drawdown - a return/drawdown ratio near 1.0. Not tradeable.
3. **Monthly-clustered t = 1.57.** Below 2. The 95% CI on the monthly mean, [-0.029, +0.469],
   still touches zero.

### And what did not

| Variant | Helps? |
|---|---|
| Stop at previous day's low | **BOTH** |
| 2% / 3% minimum gap | **BOTH** |
| Stricter RSI (< 5), RSI length 21 | **BOTH** (marginal) |
| Require EMA cross (`@abnirmal`) | **BOTH** (marginal, n drops 75%) |
| RSI length 10 | OOS only |
| EMA exit decided one day late | IS only |
| **Fixed 10R / 5R / 3R target** | **neither** |
| **Literal (unarmed) EMA exit** | **neither** |
| Any short-side setting tested | **neither** |

One diagnostic worth recording: loosening `os_level` from 5 to 50 - i.e. deleting the
"extreme oversold" premise almost entirely - moves gross bps only from 28.8 to 15.4, and
removing the RSI test altogether still leaves 13.3. **The extreme-RSI condition roughly
doubles the gross number but is not itself the source of anything**; most of what the long
side earns comes from "buy a stock that gapped up", which is a known and already-arbitraged
short-horizon momentum effect.

---

## Why the video looks convincing anyway

- The demo is a **single hand-picked trade** (BSE, ~31%, RSI 1.67). With a fat-tailed
  distribution whose top 1% carries the whole result, cherry-picking one winner is trivial
  and tells you nothing.
- The long side genuinely shines in **crisis-recovery windows** - 2020 (n=1,044, +79 bps)
  and 2023 (+121 bps). Anyone who studied charts from those periods would find the strategy
  compelling. 2018, a bear market, returned -38 bps.
- **The author never published a backtest.** Three commenters asked directly
  (`@JothimanikandanBaskaran`, `@arunfiddler`, `@ashwanthsampath1867`); none got an answer.
  `@ashwanthsampath1867` reported *"i did the backtest with ai, but the winning probability
  is showing only as 10%"* - which is much closer to what this study finds (15-20%) than to
  anything implied on screen.

---

## Limitations of this study

Stated so the result is not over-read:

- **Survivorship bias, in the strategy's favour.** The universe is *today's* F&O list applied
  to ten years of history. These are names that survived and grew into F&O eligibility. The
  real figures are somewhat worse than reported here, which strengthens a negative verdict
  and weakens the "mechanical" positive one.
- **Daily bars.** Where one bar spans both the stop and a favourable extreme, the stop is
  taken (the pessimistic and correct convention), but the true intraday sequence is unknown.
- **Entry is assumed filled at the exact open.** `@dontworrybehappy9655` raised precisely
  this: *"previous day close 100, today open 105, but we can't catch the exact open price."*
  The author's own refinement - `"better to wait till 9:16, ignore the 1min candle"` - cannot
  be tested on daily bars. Since risk = the gap, a few ticks of slippage is a large fraction
  of the risk unit, so real results would be **worse** than these.
- **Trend exit fills at the signal bar's close**, the Pine convention. Deciding one day late
  was tested (`ema_exit_delay=1`) and helped in-sample only.
- Costs modelled at 20 bps round trip. NSE reference: stock futures ~8 bps, cash delivery
  ~22 bps (STT dominates). Note the **short side cannot be held overnight in the cash
  segment at all** - it is futures-only, which the video does not mention.

---

## Reproduce

```bash
uv run --group analysis python -m signal_engine.backtest gap_rsi --full
```

Adapter: `signal_engine/backtest/strategies/gap_rsi.py`
Engine:  `signal_engine/backtest/swing_engine.py` (multi-day; the intraday engine forces an EOD exit)

---

## Bottom line

The payoff-over-accuracy *philosophy* the video argues for is sound and the setup is a real,
recognisable pattern. But as specified it loses money before costs, the short half is
significantly negative in every year tested, the long half is statistically indistinguishable
from zero, its apparent profit is four trades and vanishes under any position cap, and it
requires more simultaneous positions than a retail account can hold. The one improvement
that survives out-of-sample testing works by abandoning the 1:10 premise that gives the
video its title.

Not worth building. The reusable output is the swing backtest engine, which now exists.
