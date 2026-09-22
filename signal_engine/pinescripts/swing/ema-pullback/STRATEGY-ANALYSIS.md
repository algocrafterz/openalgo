# EMA-20 Pullback Continuation - Strategy Analysis

**Source:** [stockkhoj.in](https://stockkhoj.in) - free NSE/BSE screener. Its "Swing" scan
category lists a one-line rule: *"pullback to 20 EMA during uptrend."* No entry trigger,
stop, target, or trend definition is given on the site - this document works out all of
that and turns the one-liner into a testable strategy.

**Status: TESTED and DISQUALIFIED, 2026-09-22.** Shipped defaults are a bull-market regime
bet (significantly profitable IS, significantly losing OOS). A risk-management combo
fixes the aggregate OOS number, but the full validation suite's concurrency stress test
shows the entire apparent edge depends on holding a median of 54 (max 138) simultaneous
positions - under any capital constraint a real account has (5-10 concurrent slots), the
strategy is flat-to-negative. **Not tradeable as designed.** See Verdict section at the
bottom. No `.pine` file exists and none may be needed - see "Why Python-only" below.

Adapter: `signal_engine/backtest/strategies/ema_pullback.py`
Registered as: `ema_pullback` in `signal_engine/backtest/__main__.py` (kind: `swing`)

---

## Why this setup, out of everything on stockkhoj.in

The site's scans fall into three groups: pure price/volume technicals (ORB, gap, relative
volume, EMA pullback, 52-week-high momentum), delivery-volume technicals (needs NSE
delivery% data OpenAlgo does not currently ingest), and fundamentals (CANSLIM, management
guidance tracking - needs quarterly filings data OpenAlgo does not ingest at all).

EMA pullback was chosen because it:

1. **Needs only OHLCV** - the one data type already flowing through Historify for every
   other backtest in this repo. Delivery-volume and CANSLIM candidates would need a new
   data source built first.
2. **Fills a real gap in the current book.** `signal_engine/pinescripts/swing/rsi-tp-mr/`
   is a mean-reversion swing (buy weakness, no SL, 2-7 day hold). This is the opposite bet
   - buy strength after a shallow dip, hold while the trend runs - so it is not a
   restatement of an edge already being tested.
3. **Is mechanically well-understood** (IBD "buy the first pullback", Minervini stage-2
   continuation), which matters because a plausible mechanism is easier to trust than a
   backtest number alone - see the `gap_rsi` writeup for what happens when a plausible
   *story* (1:10 payoff) turns out to be built on four lucky trades.

It is **not** claimed to be a proven edge. It is a well-known, cheaply-testable pattern
worth putting through the same validation suite as everything else
(`project_backtest_validation_suite` memory: 12-1 momentum as the gold-standard sanity
check, monthly-clustered t-stats, out-of-sample split, position-cap and concurrency
stress - the same failure modes that killed `gap_rsi`).

---

## Why Python-only (no PineScript)

RSI2 mean-reversion already proves a purely Python-defined swing setup can run through
`signal_engine` without a TradingView/PineScript front end. EMA pullback needs nothing a
Python screener can't compute directly off broker daily bars (Historify), so writing a
`.pine` file and wiring TradingView alerts would be pure overhead unless a future decision
is made to run it through the alert/Telegram pipeline instead of a standalone screener.

---

## The strategy, precisely

Long-only, CNC/delivery swing (matches RSI2's convention - India's T+1 cash settlement
makes swing shorting impractical, so short setups are out of scope here).

### 1. Uptrend filter (must hold on the last CLOSED bar)

```
Close > EMA(200)          long-term uptrend
EMA(50) > EMA(200)        medium trend confirms
[EMA(20) > EMA(50)]       optional stricter alignment (require_fast_above_mid, default off)
```

### 2. "Was extended" filter

At some point in the `lookback_bars` (default 10) sessions **before** the pullback bar,
`Close` must have cleared `EMA(20) * (1 + ext_pct%)` (default 3%). This requires the stock
to have shown real strength recently - without it, "touches the 20 EMA" is satisfied by
any stock chopping sideways along its average, which is a different (and much weaker)
setup than a pullback after a genuine move.

### 3. Pullback + reversal bar

```
Low  <= EMA20 * (1 + touch_band_pct%)         price came back to the average ...
Low  >= EMA50 * (1 - pullback_floor_pct%)     ... without breaking the medium trend
Close > Open                                  green candle
Close > EMA20                                 closed back above the average
(Close - Low) >= close_position_pct% * (High - Low)   closed in the upper half of the range
```

Defaults: `touch_band_pct=1.0`, `pullback_floor_pct=3.0`, `close_position_pct=50.0`.

### 4. Entry

Next session's **open**, if the pullback+reversal bar occurred on the prior closed bar.
(`entry_at_open` may only read `Open[i]` plus anything shifted from `i-1` or earlier - the
adapter follows the same `swing_base.SwingStrategy` contract as `gap_rsi`.)

### 5. Stop

`stop_mode="pullback_low"` (default): the pullback bar's low, minus a small buffer
(`sl_buffer_pct`, default 0.3%). `stop_mode="tighter_of_low_and_ema50"` uses
`min(pullback_low, EMA50)` for a tighter, more conservative stop - one of the first things
to sweep once real data is available.

### 6. Exit

- **Trend break:** close below `EMA50 * (1 - trend_exit_buffer_pct%)` (default 1%) - the
  continuation thesis has failed.
- **Breakeven trail:** once the trade has moved `trail_to_breakeven_r` (default 1.0) times
  its initial risk in profit, the stop ratchets to entry. Purely a downside-protection
  measure; it does not lock in any of the open profit beyond breakeven.
- **Fixed target:** off by default (`tp_r=0`, open-ended, ride the trend exit). Available
  as `tp_r=N` for anyone who wants to compare a fixed-R exit against the trend exit - the
  `gap_rsi` study found the open-ended exit clearly beat a fixed 10R target there, so the
  same comparison is worth running here rather than assumed.

### 7. Filters

`min_price=50` - keeps the percent-based bands meaningful (mirrors `gap_rsi`'s reasoning).

---

## Open questions this backtest needs to answer

These are the "unknowns stockkhoj.in leaves open" and each is a parameter in
`EmaPullbackParams`, not a hardcoded constant - the same approach `gap_rsi.py` uses for the
video's ambiguities:

| Question | Parameter | Why it might matter |
|---|---|---|
| How extended must price have been to count? | `ext_pct`, `lookback_bars` | Too loose and this is just "buy near the 20 EMA in any uptrend"; too tight and signal count collapses. |
| How close does the pullback need to come? | `touch_band_pct` | A pullback that never actually reaches the EMA is a shallower, more common, probably lower-quality setup. |
| Is EMA20 > EMA50 required? | `require_fast_above_mid` | Stricter alignment likely trades less often but with fewer false starts in a choppy market. |
| Tight stop (pullback low) vs. wider (also below EMA50)? | `stop_mode` | Same trade-off `gap_rsi` found: a tighter stop that gets hit on noise vs. a wider one that survives it - `gap_rsi`'s single biggest improvement was widening the stop. |
| Open-ended trend exit vs. fixed R target? | `tp_r` | Needs the same "cap the tail and see what survives" test that killed `gap_rsi`'s advertised 1:10. |
| Does breakeven trailing help or just clip winners early? | `trail_to_breakeven_r` | Needs testing with it on, off, and at different R thresholds. |
| Must the run-up have consolidated (a "base") before pulling back? | `require_base`, `base_lookback_bars`, `base_max_range_pct` | Folded in from `signal_engine/pinescripts/ideas/ideas.txt` idea #4 (2026-09-22): its "valid setup" example requires a tight base after the move; its two "skip" examples don't have one. Off by default - needs ablating against the base rule once real data is available. |
| Should an already-exhausted move be excluded? | `max_ext_pct` | Also from idea #4: its "too late" example is +37% run-up before the trigger. 0 = uncapped by default - needs ablating the same way. |

## Known risks / reasons this could fail the same way gap_rsi did

- **Whipsaw risk in a choppy uptrend.** Unlike `gap_rsi`'s crisis-cluster problem, this
  setup's failure mode is more likely death by a thousand cuts: a stock oscillating around
  its 20/50 EMA can trigger repeated entries that each get stopped for a small loss before
  the trend either resumes or actually breaks.
- **Overlap with market-wide regimes.** Like every long-only trend strategy, this is
  structurally a bet that the broad market is in an uptrend. It needs the same
  monthly-clustered t-stat and year-by-year breakdown `gap_rsi` used, not just an
  aggregate return, or a few strong bull years (2020-recovery, 2023-24) could carry the
  entire result the way four trades carried `gap_rsi`'s long side.
- **Survivorship bias**, same caveat as every study in this repo using today's F&O list
  against ten years of history (see `gap_rsi`'s Limitations section) - it will overstate
  the real number.
- **It may just be "buy dips in a bull market."** The validation suite's job is to check
  whether the EMA/reversal-candle machinery adds anything over a much simpler baseline
  (e.g., buy any 5%+ dip in an uptrend) - if it doesn't beat that baseline, the specific
  rules here aren't earning their complexity.

---

## What's needed before this can be backtested

1. 10-year split/bonus-adjusted daily OHLCV for the NSE F&O universe via Historify
   (`--source historify`, same path `gap_rsi`/`orb`/`momentum-rank` already use).
2. Run: `uv run --group analysis python -m signal_engine.backtest ema_pullback --full`
3. Apply the full validation suite before trusting any verdict: in-sample/out-of-sample
   split, monthly-clustered t-stat, per-trade cap sweep, concurrency/position-cap stress,
   and a comparison against a naive "buy any dip in an uptrend" baseline.

---

## Verdict (2026-09-22)

197-symbol NSE F&O universe, Historify split-adjusted daily bars, 2019-12-03 to
2026-09-13 (IS 1011 sessions / OOS 674, cut 2023-12-28), 24 bps round-trip cost.

**Shipped defaults: a real, statistically significant regime bet - not a robust edge.**

| window | n | win% | gross bps | net_R | t | t_naive |
|---|---|---|---|---|---|---|
| IS | 2844 | 15.5 | 262.53 | 0.568 | 4.28 | 6.09 |
| OOS | 2253 | 11.6 | 24.03 | -0.158 | **-5.49** | -2.85 |
| ALL | 5097 | 13.8 | 157.10 | 0.247 | 2.44 | 4.27 |

Unlike `liquidity_sweep` (killed by "not significant either way"), this one is
**significant in BOTH windows, with opposite signs** - not noise, a genuine split. IS
(2019-12 to 2023-12) is the post-COVID bull run this "buy strength after a shallow dip"
thesis is built for; OOS (2023-12 onward) is a different regime where it loses money with
high confidence. Win rate is low throughout (11-16%); the payoff is carried by rare huge
trend-riders (`MAX_HOLD` exits average +18R, only 1.8% of trades) - and that payoff
compressed in OOS (10.37 -> 5.89) at the same time win rate fell, which is why OOS is
unambiguously negative. This is exactly the risk the pre-backtest writeup above flagged:
*"It may just be 'buy dips in a bull market.'"* It was.

### Idea #4's filters (require_base, max_ext_pct): do not help

| variant | n_IS | bps_IS | n_OOS | bps_OOS | helps |
|---|---|---|---|---|---|
| base | 2844 | 262.53 | 2253 | 24.03 | - |
| require_base | 691 | 173.57 | 635 | -56.48 | neither |
| max_ext_pct=15 | 2800 | 259.69 | 2266 | 14.81 | neither |
| max_ext_pct=10 | 2701 | 262.38 | 2188 | 9.95 | neither |
| require_base + max_ext=15 | 691 | 173.57 | 635 | -56.48 | neither |

Requiring a tight base before the pullback cuts trade count by 76% and makes OOS *worse*
(+24 to -56 bps). Capping the extension does effectively nothing either way. **Idea #4's
mechanism does not transfer from "9/21 EMA cross" to "20 EMA pullback in an established
uptrend"** - plausible in hindsight: idea #4's base/extension-cap logic exists to filter a
single discrete cross EVENT down to the good ones, but this strategy's `was_extended`
filter already requires a prior move before it fires, so there is less low-quality signal
left for a base/extension filter to remove.

### What does help: three of the pre-backtest doc's other open questions

| variant | n_IS | bps_IS | n_OOS | bps_OOS | helps |
|---|---|---|---|---|---|
| require_fast_above_mid=True | 2714 | 265.58 | 2099 | 27.78 | BOTH |
| stop_mode=tighter_of_low_and_ema50 | 2110 | 465.44 | 1689 | 65.17 | BOTH |
| trail_to_breakeven_r=0 (no trail) | 2255 | 429.29 | 1825 | 77.84 | BOTH |
| tp_r=3 (fixed target) | 3777 | 55.32 | 3005 | -12.25 | neither |
| tp_r=5 (fixed target) | 3421 | 100.67 | 2746 | 0.66 | neither |

The wider stop (`gap_rsi`'s single biggest lesson, repeated here) and removing the
breakeven trail both help substantially in both windows - a trade that gets a wider berth
and is allowed to actually run pays off more than one that is stopped to breakeven early.
Fixed R targets hurt (open-ended trend exit is earning its complexity, same conclusion
`gap_rsi` reached).

Combining the three real single-axis wins (`require_fast_above_mid=True,
stop_mode="tighter_of_low_and_ema50", trail_to_breakeven_r=0`), checked net-of-cost:

| window | n | win% | net_R | t |
|---|---|---|---|---|
| IS | 1732 | 33.7 | 0.977 | 6.94 |
| OOS | 1314 | 25.8 | 0.142 | **-0.04** |
| ALL | 3046 | 30.3 | 0.617 | 6.29 |

OOS moves from confirmed-negative (t=-5.49) to statistically flat (t=-0.04, total_R now
**positive** at +186.7 vs -356.6 for the base config). This is NOT the same overfit
signature `liquidity_sweep`'s rescue attempt showed - there, IS win% (41) diverged sharply
from OOS win% (27) while OOS net_R stayed negative. Here IS (33.7%) and OOS (25.8%) win
rates move together, and OOS net_R crosses to positive. That said: **t=-0.04 is not a
confirmed edge, only the absence of a confirmed negative one.** The honest reading is "the
regime-bet problem is substantially reduced, not eliminated."

### Regime filter (added 2026-09-22): does not explain or fix the OOS loss

`use_regime` reuses `ib_extension.py`'s Nifty-50-vs-50-EMA gate (only take a long when
the index is not in a KNOWN downtrend). Off by default (no change to the numbers above).

| config | n_OOS | net_R OOS | t OOS |
|---|---|---|---|
| base | 2253 | -0.158 | -5.49 |
| +use_regime alone | 1767 | -0.158 | -3.88 |
| +use_regime, combined with the 3-filter risk combo above | 1051 | 0.144 | -0.18 |

Regime alone only filters out 22% of OOS trades and the survivors are **just as
negative** (net_R identical to base, t less extreme only because n dropped) - the NIFTY
index itself was not reliably in a downtrend during the OOS window, so an index-level
gate has little to reject. Adding it on top of the risk-management combo changes
nothing (t=-0.18 vs the combo's own t=-0.04 alone - within noise, fewer trades). **The
OOS losses are not explained by broad-market regime; they are driven by stock-specific
whipsaw**, which is exactly what the wider stop and no-early-breakeven fix already
addresses. Kept in the adapter (documented negative result, not removed) since a
different regime construction (breadth, sector-relative strength) remains untested.

### Full validation suite (2026-09-22): the concurrency test disqualifies it

Run on the risk-mgmt combo (`require_fast_above_mid=True, stop_mode=tighter_of_low_and_ema50,
trail_to_breakeven_r=0`), same methodology `gap_rsi` used.

**Year-by-year** (not one bad month - a multi-year fade):

| year | n | total_R | net_R |
|---|---|---|---|
| 2020 | 361 | +442.8 | 1.227 |
| 2021 | 452 | +399.7 | 0.884 |
| 2022 | 418 | +30.2 | 0.072 |
| 2023 | 481 | +821.2 | 1.707 |
| 2024 | 583 | +272.2 | 0.467 |
| 2025 | 418 | +20.5 | 0.049 |
| 2026 (partial) | 311 | **-109.6** | **-0.352** |

2024 (the first OOS year) was genuinely good; 2025 went flat; 2026-to-date is negative.
The OOS window isn't uniformly bad, it's decaying.

**Concurrency / position-cap stress - the decisive check.** Unconstrained, this strategy
wants a **median of 54 and a maximum of 138** simultaneous open positions (p90: 100).
Imposing a realistic slot cap, same as `gap_rsi`'s methodology:

| slots | n | net_R | t | total_R (7yr) |
|---|---|---|---|---|
| 3 | 260 | 0.015 | -0.26 | +3.8 |
| 5 | 432 | 0.029 | 0.18 | +12.6 |
| 10 | 824 | **-0.095** | 0.14 | **-78.0** |
| unlimited | 3046 | 0.617 | 6.29 | +1879.6 |

**The entire apparent edge is a scale artifact.** It exists only for an account that can
hold every signal on the market's busiest days at once. At a cap of 10 concurrent
positions - already generous for OpenAlgo's single-user, single-broker-session deployment
model - the strategy is net NEGATIVE over seven years. At 5 slots it is statistically
indistinguishable from zero (t=0.18, total +12.6R over seven years - nothing). This is the
same disqualifying failure mode `gap_rsi`'s long side hit, for the same underlying reason:
a shallow-pullback / breakaway-gap entry condition fires in market-wide clusters (many
stocks pull back or gap together on the same days), so an unconstrained backtest silently
assumes unlimited simultaneous capital that no real account has.

### Honest limits

- **The shipped-defaults IS/OOS split, and the risk-mgmt combo that appeared to fix
  it, are both moot until a slot cap is applied** - neither number in the sections above
  reflects what a real, capital-constrained account would actually experience.
- **This is still fundamentally a bet the broad market trends up**, just a better-managed
  one - regime filtering doesn't fix that because it isn't actually an index-timing
  problem, it's an execution/risk-management one.
- **The concurrency finding is the one that matters most for a go/no-go decision.** A
  strategy that is negative at 10 concurrent slots and flat at 5 is not tradeable on a
  realistic account size, independent of every other number in this document.
- **OOS t=-0.04 on the combo is inconclusive, not a green light.** More history, a
  monthly-clustered breakdown, and a concurrency/position-cap stress test (this repo's
  full validation suite) are needed before treating this as tradeable.
- **Idea #4's filters were a real experiment that failed**, and that is useful
  information in itself - not every "plausible-sounding Instagram refinement" transfers
  between strategies that superficially resemble each other.

## Layman summary

The strategy - buy a stock after it dips back to its 20-day average following a run-up,
sell if the trend breaks - made real money in backtesting from 2020 to late 2023, and lost
real money, with high confidence, from late 2023 onward. That split meant it was riding
the post-COVID bull market rather than finding a repeatable edge. The Instagram-idea
filters (idea #4) didn't fix that; boring risk management (a wider stop, no early
lock-to-breakeven) did - on paper. But the full validation pass found the real problem:
this strategy wants to hold dozens, sometimes over a hundred, positions at the same time,
because pullbacks across many stocks tend to happen together. No real trading account can
do that. Cap it at a number an actual account could realistically hold open at once - even
a generous one - and the strategy stops making money. The earlier "fixed" result only
looked good because the backtest quietly assumed unlimited capital. **Verdict: not
tradeable as designed**, regardless of which filters are turned on. If this is revisited,
the open question is whether adding a signal-ranking rule (trade only the N best-looking
setups on a crowded day, not all of them) can recover a positive result under a realistic
position cap - that has not been tested.
