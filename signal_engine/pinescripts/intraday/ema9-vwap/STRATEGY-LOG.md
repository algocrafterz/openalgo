# EMA9 x VWAP — Strategy Log

Plain 9 EMA / session VWAP crossover, NSE equity intraday, 5-minute.
Script: `ema9-vwap.pine` | Adapter: `signal_engine/backtest/strategies/ema9_vwap.py`

---

## How entry, stop-loss and target are computed

This section explains the mechanics in plain terms. It is reference material, kept up
to date whenever the logic changes; it is not a changelog entry itself.

**Entry — the trigger.** The script watches two lines: the 9 EMA (a fast average of the
last 9 candles) and VWAP (the average price the stock has traded at today, weighted by
volume — roughly "what most of today's money paid"). At the close of a 5-minute candle,
if the 9 EMA is below VWAP and then closes above it, that is a LONG. If it is above VWAP
and closes below it, that is a SHORT. Nothing else has to agree by default — no
candlestick pattern, no volume check, no trend filter. The signal only evaluates once a
candle has fully closed, so it cannot flicker mid-candle and cannot fire on a price that
later reverses within the same candle.

**Stop-loss — how far is "wrong."** The default stop style is Swing: look back over the
last 8 candles, find the lowest low (for a long) or highest high (for a short), and place
the stop a little beyond it — a small buffer equal to about a tenth of the stock's
typical 5-minute range, so ordinary noise does not tag it. In plain terms: "the most
recent turning point, plus a hair of room." Two alternative stop styles exist (a fixed
ATR distance, and a stop parked on the far side of VWAP) but neither measured better, so
Swing is the default. If that stop would work out to less than 0.20% of the share price,
the trade is skipped rather than sent with a stop too tight to survive — this mirrors a
check the live order system performs anyway, so a signal it would reject is never sent.

**Take-profit — where to book it.** A single multiple of the risk: whatever distance the
stop sits at, the target is twice that distance in the trade's favour (a "2R" target). If
the stop risks Rs 5, the target is Rs 10 away. There is no trailing stop and no partial
exit — one target, full position closed.

**Everything else that can end the trade.** The stop or target being hit closes it.
Independently, a hard cutoff at 14:45 IST flattens any open position regardless of price,
matching the platform-wide time exit. An optional exit (off by default) closes the trade
if the 9 EMA crosses back the other way, for anyone who prefers to leave early rather
than wait for the stop.

**Why these particular choices.** None of the stop style or target multiple was picked
because it measured best — the measurement work below shows nothing performs
meaningfully differently from anything else, because price moves about as far against a
crossover as it does in its favour. Given that, the simplest, most defensible version was
kept rather than tuning a knob until one combination looked good on this data.

---

### 2026-09-10 03:05 — ORB watchlist: anticipate vs wait, and where stops/targets belong

**What changed**

Nothing in the code. This is a measurement entry, run on the 45-stock ORB watchlist
rather than the wider F&O list, to answer two questions: should entries be taken early
as the lines converge, or only once they actually cross; and where the stop and target
belong. Defaults were already set the way the evidence points, so none were altered.

**Entry** — No change. Waiting for the actual crossover remains the default, and the
measurement now supports that directly rather than by assumption. Entering early does
genuinely get a better price — that part of the idea is correct and was confirmed —
but the size of the prize is the problem, explained below.

**Exit (SL)** — No change. The stop stays at the recent swing extreme, which works out
around 0.57% of the share price. That width turns out to matter more than where exactly
it sits, for cost reasons described below.

**Exit (TP)** — No change. Twice the risk remains the default; targets between 1.5x and
3x behave almost identically, so there is nothing to gain from moving it.

**Consideration** — Three findings, in order of importance.

First, and most decisive: after a crossover, price travels almost exactly as far against
the trade as it does in its favour. Measured with no stop and no target at all, the
typical best-case run is 2.11 units of daily range and the typical worst-case run is
2.04 — a ratio of 1.03, where 1.00 means no directional information whatsoever. That
holds at every level checked, not just the average. This is why no arrangement of stop
and target can rescue the idea: you are placing brackets around a symmetric outcome, so
every choice returns the same nothing, minus costs.

Second, on entering early. The better fill is real and was measured: entering as the
lines converge gets a price roughly 0.19 to 0.39 units of daily range better than
waiting. Converted into risk terms, that is worth about 0.06R at the tight setting and
0.13R at the loose one. But one round trip in brokerage and slippage costs about 0.17R.
**The entire prize from anticipating is smaller than the cost of a single trade.** On
top of that, between 19% and 33% of these early signals never go on to cross at all,
so anticipating also buys a batch of trades the patient rule would simply never take.
Net result on the watchlist: essentially identical to waiting. The better price is real;
it is just too small to matter, and it is bought with worse signals.

Third, on stop width. Costs are a fixed percentage of the trade, so the narrower the
stop, the larger those costs loom relative to what is being risked. A 0.25% stop hands
over 0.40R per trade before anything else happens; a 0.90% stop only 0.11R. This creates
a trap visible in the results: one tight-stop setting is the only one that looks
positive in both halves of the data, which is normally the test that matters — but it
looks positive only before costs. After costs it is markedly worse than the wider swing
stop. Passing the both-windows test is necessary, not sufficient; the cost line has to
be cleared too, and here it is not, by a wide margin.

A note on the watchlist itself: on these 45 stocks the plain crossover reads negative in
the first half of the data and positive in the second, which is the exact opposite of
what the same rule did on the broader F&O list. A genuine edge does not change sign when
you change the stock list or the date range. This one does, twice.

**NOTE:** Universe 45 ORB watchlist names, 5-min, 2026-06-04 to 2026-08-28, 36 IS / 25
OOS, 10 bps.

Plain cross, n=2578: -1.51 bps IS / +1.53 OOS (-0.30 full), net_R -0.183. On the F&O 208
the same config was +1.36 IS / -0.41 OOS. Sign flips across both universe and window.

Anticipate (band 0.15), n=2298: -1.02 IS / -0.33 OOS, net_R -0.181. Statistically
indistinguishable from the cross on net_R despite the better fills.

Approach-event decomposition, HOR=6 bars (30 min), measuring every "EMA inside band and
narrowing" event:

| band | approaches | went on to cross | false start | median fill adv | in R |
| --- | --- | --- | --- | --- | --- |
| 0.15 ATR | 4,431 | 80.9% | 19.1% | 0.186 ATR | 0.063 R |
| 0.25 ATR | 5,749 | 74.2% | 25.8% | 0.293 ATR | 0.099 R |
| 0.35 ATR | 6,658 | 67.5% | 32.5% | 0.390 ATR | 0.132 R |

Conversion: median ATR 0.195% of price, median stop 0.574% of price, so 1R = 2.95 ATR.
Cost drag at 10 bps = 0.10/0.574 = **0.174 R/trade**, which exceeds the best-case fill
advantage of 0.132 R.

MFE/MAE from a next-bar-open fill, held to the 14:45 exit, n=5,322 cross entries:

| percentile | MFE (ATR) | MAE (ATR) |
| --- | --- | --- |
| p25 | 0.92 | 0.88 |
| p50 | 2.11 | 2.04 |
| p75 | 3.83 | 3.72 |
| p90 | 6.02 | 5.76 |

Median ratio 1.03. Symmetric at every percentile — the structural reason no stop/target
grid can find an edge.

Stop x target grid, OOS gross bps: swing and vwap stops are numerically identical
(min(vwap, swing) almost always selects swing, so the "vwap stop" option is effectively
dead code in practice — left in place because it is the option that matches the premise,
and its equivalence is itself the finding). Best cell in the entire grid is
`atr x1.0, TP 3R` at +2.55 bps OOS / +0.37 IS — still four times below the 10 bps cost
line, and its net_R is -0.303 because the 0.257% stop drags 0.39R. Swing stop nets
-0.173 to -0.191 across all targets. tp_r 1.5/2.0/3.0 are within noise of each other.

Cost drag reference: stop 0.25% -> 0.400 R; 0.40% -> 0.250 R; 0.62% -> 0.161 R;
0.90% -> 0.111 R; 1.20% -> 0.083 R.

---

### 2026-09-10 02:35 — Chart stripped back; IB/PW/PM added to the key-level filter

**What changed**

Two things. First, the chart is now bare by default: the 9 EMA, VWAP, and the small
triangles marking a crossover, and nothing else. Everything that used to draw itself
automatically — the coloured shading between the two lines, the previous-day and
opening-range levels, the session background tints, the entry/stop/target lines, the
trade labels and the dashboard — is still available but switched off, so it does not
fight with Initial Balance and Volume Profile layers added from other indicators.

Second, the optional "only trade near a key level" filter now recognises more levels.
It previously knew the previous day's high, low and close, plus the opening-range
edges. It now also knows the Initial Balance high and low (first 60 minutes), the
previous week's high and low, and the previous month's high and low. The filter is
still off by default.

**Entry** — Only when the key-level filter is switched on, which it is not by default.
With it on, a crossover is now accepted near a wider set of levels than before, so it
will permit somewhat more trades than it did yesterday. With the filter off, entries
are completely unchanged: still the plain crossover.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected.

**Consideration** — A caution about the levels themselves, because widening the list
cuts both ways. Adding more levels makes the "near a key level" test easier to pass,
and a filter that passes almost everything is not filtering. Yesterday's measurement
already showed that requiring a key level made results worse in both halves of the
data, so this change makes an unhelpful filter slightly weaker rather than fixing it.
It is there so the idea can be looked at on a chart, not because the evidence supports
switching it on.

The bigger caution concerns entering early. Checking the actual TCS session this was
watched on, the whole day held exactly two crossovers — 10:55 and 12:30. The 12:30 one
matches the expectation of a long going into the 12:35 candle exactly. The 10:30/10:35
short does not exist: at 10:35 the 9 EMA was still meaningfully above VWAP and did not
reach it until 10:55. Getting the script to fire where the eye saw it requires
loosening the "approaching" setting to roughly double its default, and testing that
range shows the in-sample result improving steadily while the out-of-sample result
gets steadily worse. In plain terms: the earlier the entry, the better it looks on
days you have already seen and the worse it performs on days you have not. That is
worth sitting with, because it is the most seductive part of this whole idea.

Volume Profile levels (VAH/VAL/POC) are **not** included. They need a volume-profile
engine inside the script, which is a real piece of work rather than a few lines, and
was not built without asking first.

**NOTE:** Visual defaults changed only; no plot was deleted. New inputs `showFill`
and `showSkipped` split the EMA/VWAP shading and the rejected-cross markers out of
`showLines`/`showCross` so each can be silenced independently. `showLevels`,
`showKeyLevels`, `showZones`, `showLabels`, `showTable` all now default false.

Level set for `require_key_level` extended with `ibH`/`ibL` (rolling extremes over
`ibMinutes`, default 60, built the same way as the opening range), and `pwh`/`pwl`,
`pmh`/`pml` via `request.security(..., "W"/"M", high[1]/low[1],
lookahead=barmerge.lookahead_on)` — the confirmed-bar idiom, so no lookahead. Each
group is individually toggleable (`useIb`, `usePrevWeek`, `usePrevMonth`, all default
true *within* the filter, which itself is off).

Anticipation band sweep, `mode="anticipate"`, gross bps IS / OOS:

| band | bps IS | bps OOS |
| --- | --- | --- |
| 0.15 ATR | 0.64 | -1.47 |
| 0.25 ATR | 1.95 | -2.86 |
| 0.35 ATR | 2.33 | -2.86 |
| 0.50 ATR | 2.32 | -3.53 |

Monotonic in both directions and in opposite directions — the signature of a knob
that buys in-sample fit with out-of-sample performance.

TCS 2026-09-09 crossings, computed from 5-minute bars: 10:55 down (gap -0.07 from
+0.39), 12:30 up (gap +0.13 from -0.59). At 10:35 gap was +1.835 = 0.312 ATR
(session ATR ~5.9 on a ~2205 price). The backtest adapter is unchanged by this entry;
the Pine-side level additions are not mirrored there because the filter they feed was
already measured as unhelpful.

---

### 2026-09-10 01:59 — Built and measured. Verdict: do not trade.

**What changed**

A new strategy was written from an observation made while watching TCS on the
5-minute chart: that price "respects" the 9 EMA against VWAP, so the 9 EMA crossing
below VWAP is a short and crossing above it is a long. It was then measured before
being wired to money, and the measurement says the idea does not work. The script
exists as a paper-trading and measurement tool, not as a live strategy. Its defaults
are the plain crossover, and every proposed refinement ships switched off.

The short version of why: at the moment the two lines cross, price is usually
finishing a stretch rather than starting a run. Over 29,492 crossings the price went
the "right" way only about 44 times in 100 — worse than a coin toss. The eye
remembers the times it kept going, because those are the ones that look dramatic on
a chart. The times it immediately snapped back leave no memorable mark.

**Entry** — Yes, this defines a new one. A position opens when the 9 EMA crosses
session VWAP, on a closed 5-minute bar, with no candlestick confirmation required.
Two optional entry variations were tested and both ship OFF because they made results
worse: entering 1-2 candles early while the lines are still converging, and requiring
the cross to happen near a key level (previous day high/low/close, or the opening
range edges). Trading is restricted to the first 15 minutes onward, no new positions
after 14:00, and at most 3 per day.

**Exit (SL)** — Yes. The stop sits just beyond the recent 8-bar swing extreme, padded
by a tenth of the average daily range. Two alternatives are selectable: a fixed
volatility-based distance, or a stop parked on the far side of VWAP itself — that last
one matches the premise most closely, since if price stops respecting VWAP the reason
for being in the trade has gone. A stop closer than 0.20% of the share price is not
sent at all, because the engine would reject it anyway.

**Exit (TP)** — Yes. A single target at twice the risk, closing the whole position.
Targets of 1x, 1.5x, 2x and 3x the risk were all tested and all lost money; 2x lost the
least, which is not the same as making money. There is an optional exit when the 9 EMA
crosses back the other way, also off by default.

**Consideration** — The most important thing on this page: **this strategy lost money
in testing, before broker charges were even counted.** Across 208 stocks and roughly
three months of 5-minute data it produced 11,815 trades and earned essentially
nothing, then went slightly negative in the portion of the data that was not used for
tuning. Trading costs are around 10 basis points per round trip and the strategy earns
well under 1, so in practice every trade is a small donation to the broker.

TCS on its own looked mildly encouraging — but on only 51 trades, which is far too few
to tell skill from luck. Separating a genuine edge this small from noise takes roughly
1,600 trades. When the four other large IT names were added the picture went clearly
negative. TCS is not special; it was simply a small sample viewed with hindsight.

Two of the refinements — the early entry combined with a key-level filter, and the
long-term trend filter — looked like the *best* ideas on the first half of the data and
the *worst* on the second half. That pattern is the classic warning sign of a rule
tuned to noise, and it is worth recognising, because it is exactly how a losing
strategy gets talked into production.

Reversing the whole thing and fading the crossover was also tested, since the data says
the cross mean-reverts. The reversal is real but far too small to cover costs, so there
is no trade in that direction either.

If this is run forward, run it in sandbox or against a blacklist first, and compare the
signals it actually logs against the backtest above rather than against memory.

**NOTE:** Universe 208 NSE F&O names, 5-minute bars, 2026-06-04 to 2026-08-26, 59
sessions split 35 in-sample / 24 out-of-sample, costs 10 bps round trip, engine
`min_sl_pct` 0.002.

Base (plain cross, swing stop, TP 2R), n = 11,815: gross **+1.36 bps IS / -0.41 bps
OOS**, net_R -0.168, t = -16.76 full period. Cost sensitivity 6/8/10/12 bps: net_R
-0.097 / -0.133 / -0.168 / -0.204 — negative everywhere, no sign flip to find.

Model-free follow-through after the cross, no stop/target model, "does price travel
k x ATR in the cross direction before k x ATR against":

| k (ATR) | n | follow-through | t vs 50% |
| --- | --- | --- | --- |
| 0.5 | 29,492 | 44.54% | -18.76 |
| 1.0 | 29,179 | 48.31% | -5.78 |
| 1.5 | 28,287 | 49.10% | -3.01 |

Significantly below the 50% driftless baseline at every horizon, converging upward as
the horizon widens — the signature of short-horizon mean reversion. This is the core
finding; the trade-level results are downstream of it.

`ablation()`, only BOTH counts as evidence:

| variant | bps IS | bps OOS | helps |
| --- | --- | --- | --- |
| base | 1.36 | -0.41 | - |
| anticipate (1-2 bars early) | 0.64 | -1.47 | neither |
| near key level | 0.99 | -1.16 | neither |
| anticipate + key level | 3.14 | -4.04 | IS only |
| 200 EMA trend filter | 2.04 | -4.02 | IS only |
| stop beyond VWAP | 1.29 | -0.40 | OOS only |
| ATR stop | -0.62 | 0.36 | OOS only |
| exit on recross | 0.13 | -0.33 | OOS only |
| TP 1R | -0.38 | -0.70 | neither |
| TP 3R | 1.88 | -0.44 | IS only |

Nothing scores BOTH. `tp_r` sweep OOS: 1.0/-0.70, 1.5/-0.61, 2.0/-0.41, 3.0/-0.44 bps.

Single-name and sector detail: TCS n=51, gross +7.14 bps but t=-0.60, IS +13.87 vs
OOS -2.48. IT basket (TCS, INFY, HCLTECH, WIPRO, TECHM) n=242, OOS -8.97 bps, t=-3.15;
every one of the five is negative in total_R.

Fade (`invert=True`), same costs: ATR stop 0.5x with 0.75R/1R/1.5R targets gives
-1.62 / -0.44 / +0.65 bps full period; swing stop at 1R gives +0.18 bps. Best case
+0.65 bps against a 10 bps cost line, and net_R between -0.26 and -0.46 throughout.

Exit mix at defaults: TIME_EXIT 45.0%, SL 39.6%, TP 14.6%, SL_GAP 0.7%. Fewer than one
trade in seven reaches its target, and nearly half are still open at the time exit —
consistent with an entry that does not initiate movement.

Reproduce:
```
uv run --group analysis python -m signal_engine.backtest ema9_vwap --full
```
