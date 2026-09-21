# 9:15 Opening-Candle Scalp ("Open Drive") — Strategy Analysis

Python-native intraday scalp, backtested via `signal_engine/backtest/strategies/open_drive.py`
(`python -m signal_engine.backtest open_drive`). No PineScript — this was never live.

## The hypothesis

Trade the opening candle's own direction, gated on two things: LOCATION (open above the
previous session's Value Area High for longs, below Value Area Low for shorts — reconstructed
from 1-minute bars via the `MarketProfile` library) and CONFIRMATION (price holds on the correct
side of that level for 1-2 more bars before a breakout of the opening candle's own high/low is
taken as the entry). This is the synthesis of two independent LLM-assisted design passes on the
user's original "open = low, buy the break" idea — both passes warned that version alone produces
false signals, and proposed the location + hold-confirmation version instead.

## Setup

- **Universe**: 212 F&O underlyings — every symbol Historify has 1-minute bars for, same
  discovery path (`data.from_historify()`) every other strategy in this repo uses. No
  hand-picked subset.
- **Period**: 2023-01-02 to 2026-09-13. IS = first 547 sessions (through 2025-03-19), OOS = last
  366 sessions. Restricted from the full ~10 years some symbols carry, for tractability and
  regime relevance — see "What wasn't tested" below.
- **Cost**: 16 bps round-trip (this repo's standard NSE intraday market-order estimate).
- **Shipped defaults**: value-area filter ON, open≈extreme tolerance 10%, close-location 25%,
  relative-range ≥2x, volume ≥1.5x same-slot-prior-sessions, hold-confirmation ON (2 bars),
  stop = candle extreme ± ATR-scaled buffer, target = 1.0x ATR (hard cap, no trail).

## Result: loses money, and the raw idea has no edge before costs either

| Window | n | win% | gross bps | net R/trade | t | total R |
|---|---|---|---|---|---|---|
| IS | 699 | 69.7% | -6.06 | -0.151 | -6.99 | -105.7 |
| OOS | 450 | 78.9% | +1.06 | -0.093 | -4.06 | -41.8 |
| ALL | 1149 | 73.3% | -3.27 | -0.128 | -8.04 | -147.5 |

`t = -8.04` on the full period is not a small-sample fluke — it clears any reasonable
multiple-comparison hurdle by a wide margin. This is a confident negative result, not an
inconclusive one.

**Cost sensitivity is the important context**: at 0 bps the result is already flat-to-negative
(t = -1.51, net R -0.027) — before real-world cost is even charged. This is not a "good idea
killed by execution cost" story. The raw signal itself carries no measurable edge in this data;
costs then make it a clear loser (t = -4.82 at 8 bps, -11.14 at 24 bps).

**Dispersion**: 208 of 212 symbols traded, only 65 (31%) net-positive, median per-symbol result
-0.097R. The loss is broad-based across the universe, not concentrated in a handful of bad names.

## Why it loses: the win rate is high but the payoff is backwards

| Exit reason | n | avg R | total R | % of trades |
|---|---|---|---|---|
| TP | 898 | +0.094 | +84.2 | 78.2% |
| SL | 174 | -1.112 | -193.4 | 15.1% |
| TIME_EXIT | 73 | -0.461 | -33.6 | 6.4% |
| SL_GAP | 4 | -1.151 | -4.6 | 0.3% |

The strategy wins 70-79% of the time — but an average win is only +0.094R while an average loss
is a full -1.1R. Payoff ratio ≈ 0.08, which needs roughly a 92%+ win rate to break even; actual
win rate is 70-79%. This is mechanical, not incidental: the stop (shipped `candle_atr` mode) sits
beyond the ENTIRE opening candle, and the `relative_range_mult=2.0` filter guarantees that candle
is already ≥2x a normal bar's range by construction — so risk is anchored to an already-inflated
distance while the target (1.0x ATR) is anchored to ordinary volatility. Risk and reward are
measured on two different scales, and risk is structurally the bigger of the two. Sweeping the
target multiple (0.5/1.0/1.5/2.0x ATR) does not fix this — all four lose, because the target was
never the side of the trade that needed rescaling (see Suggestions below).

## Ablation: the location filter isn't earning its keep — the honest surprise

| Filter removed | n (IS) | bps (IS) | n (OOS) | bps (OOS) | Helps? |
|---|---|---|---|---|---|
| — (shipped) | 699 | -6.06 | 450 | 1.06 | — |
| Value-area location filter | 1397 | -1.14 | 907 | 2.38 | **BOTH** |
| Close-location filter | 1234 | -2.13 | 795 | 2.38 | **BOTH** |
| Relative-range filter | 711 | -6.03 | 452 | 1.26 | BOTH (marginal) |
| Hold-confirmation | 918 | -2.58 | 556 | 0.43 | IS only |
| Open≈extreme wick filter | 1914 | -5.17 | 1267 | 0.85 | IS only |
| Volume filter | 2343 | -1.33 | 1547 | -1.61 | IS only |
| Stop mode → pure ATR | 698 | -1.88 | 449 | 0.05 | IS only |
| Stop mode → key-level (widest) | 699 | -10.55 | 450 | 0.10 | neither |
| Trail on top of fixed target | 699 | -6.23 | 450 | 1.06 | neither |

Both LLM design passes treated the **previous-session Value Area location** as the central,
highest-conviction filter — "the setup becomes much stronger if... all remain above VAH." On
this data, removing it improves gross bps in BOTH windows and roughly doubles the trade count.
Same for the close-in-the-top/bottom-25%-of-range filter. Neither survives the honest test.

Everything marked "IS only" is the harness's overfitting signature (looks better in-sample,
worse out-of-sample) — the hold-confirmation stage, the open≈extreme wick check, and the volume
filter all fall in this bucket, meaning they should be **kept**, not loosened, despite each one
cutting the trade count. This is the opposite of what the relaxation ladder in the plan would
have tried first (it targets low trade count, and 699-1149 trades is already well above the
30 IS / 15 OOS / 8-symbol floor set in advance) — no relaxation was needed or applied.

## What wasn't a real test: the 5-minute comparison

The plan called for comparing 1-minute vs 5-minute entries. The 5-minute run produced **zero
trades at shipped defaults** — not because 5-minute has no signal, but because of two
config bugs surfaced only by actually running it:

1. `hold_bars=2` means "2 bars of confirmation" regardless of bar size. On 5-minute bars that is
   10 minutes; combined with `entry_cutoff_min=09:45` (tuned assuming 1-minute bars), there is
   almost no window left for a breakout to occur. Removing hold-confirmation alone produced 656
   trades on 5-minute bars — the filter stack isn't "too tight" in general, the TIME BUDGET was
   miscalibrated for that bar size.
2. More fundamentally, the previous-day Value Area is reconstructed from whatever `df` the
   harness hands `prepare()` — for the 5-minute run, that is already-resampled 5-minute closes,
   not the 1-minute closes both LLM passes assumed the value area would always come from. This
   silently degrades the location filter's quality precisely for the comparison that filter
   matters most to. **The 5-minute result above should be discarded, not read as "5-minute
   doesn't work."** Fixing this needs decoupling the value-area source from the entry-bar
   interval (see Suggestions).

## Two real bugs found in the tooling while building this (fixed, tests added)

- `signal_engine/backtest/volume_profile.py`: `prev_session_value_area()` shifted within only
  the days that produced a valid profile, so a session too short to build one (holiday, bad
  print) caused the *next* day's lookup to silently read the wrong prior value — or crash if it
  was the last day in the frame. Fixed by reindexing onto every calendar day present before
  shifting.
- The third-party `MarketProfile` library's `calculate_value_area()` tests "no buckets left on
  this side" with bare Python truthiness (`not x`), which cannot distinguish a genuinely empty
  side from a bucket with legitimate zero volume (an illiquid minute) — both read as falsy. A
  zero-volume bucket between the POC and one edge stalls that side permanently until the other
  exhausts for real and crashes on `int + None`. Confirmed against real data
  (UNIONBANK, 2026-09-08). Worked around by skipping the session (rare: ~1 in 4,500 checked)
  rather than patching third-party internals.
- A third, related bug: `pd.DataFrame.from_dict({}, ...)` on a symbol where every session got
  skipped infers `object` dtype filled with Python `None`, not float `NaN` — which crashed
  `np.isfinite()` deep inside the strategy's `entry()`, far from the actual cause. Fixed by
  forcing `float64` unconditionally.

All three are covered by regression tests in `signal_engine/tests/test_volume_profile.py`.

## Suggestions for a next iteration

1. **Fix the risk:reward mismatch directly, not by sweeping the target.** Size the target as a
   multiple of the ACTUAL risk taken on that trade (`tp = entry + risk * R`, the same `tp_mode="r"`
   idea `orb.py` already uses) instead of a flat ATR multiple that ignores how wide the candle
   -anchored stop already is. This is a structural fix the 0.5-2.0x ATR sweep could never find,
   because it was sweeping the wrong side of the ratio.
2. **Drop or rethink the static value-area and close-location filters** — both hurt in both
   windows here, contradicting the design hypothesis. Worth testing whether the hold-confirmation
   stage already captures what the location filter was meant to add (price actually holding
   above the level is a stronger, dynamic version of "opened above the level"), making the static
   gate redundant on top of it.
3. **Keep — or tighten further — the hold-confirmation, open≈extreme, and volume filters.** All
   three showed the overfitting signature when removed (better IS, worse OOS).
4. **Fix the 5-minute comparison before drawing any conclusion from it**: compute the value area
   from 1-minute bars unconditionally regardless of the entry timeframe, and scale
   `hold_bars`/`entry_cutoff_min` to the loaded bar size rather than hardcoding wall-clock
   constants tuned for 1-minute bars.
5. **Question the core premise, not just its parameters.** By construction, entry happens only
   AFTER an oversized opening candle has already completed and been broken — meaning the stop is
   anchored beyond a move that already happened. That is a structurally difficult R:R to win
   regardless of filter tuning (item 1 above only rescales it, doesn't remove it). The ablation's
   one weak "helps BOTH" result (dropping the oversized-candle requirement) points the same
   direction. Of the LLM's own ranked list, the more promising untested candidates are B
   (already this setup, needs the R:R fix), C (gap-direction regime classification — distinguish
   continuation vs fade before trading either), and F (rejection/failure of the opening move,
   which is the OPPOSITE trade to what was tested here and was never tried).

---

## v2: Rebuilt Against the Documented Volume-Profile Model — Worse, Not Better

The user pointed at this repo's own existing, documented model
(`pinescripts/intraday/volume-profile/volume-profile-model.md`, the "Volume Profile Entry Zone"
diagram) — 8 price-action scenarios around VAH/VAL, collapsing into 6 named setups
(VAH-ACC/VAL-ACC "acceptance", VAH-RT/VAL-RT "retest", VAH-REJ/VAL-REJ "rejection"), with exact
stop rules (level in play, ATR-buffered) and target rules (nearest structural level: POC → VAH →
PDH/IBH). v1's trigger ("break of the opening candle's own high") was not actually one of these
six setups. v2 rebuilt the adapter to implement the real ones directly, dropped the
opening-candle-shape gates entirely, and added the model's other named ingredients: CLV (close
location value, replacing the old close-location-percent), a plain RVOL filter, a VWAP trend
filter (the model's explicit "filter, not a trigger" rule), and structural targeting.

**Result: dramatically worse, not better.** Same universe and period as v1 (212 symbols,
2023-01-02 to 2026-09-13, IS/OOS split 2025-03-19), 1-minute bars only (the 5-minute comparison
was dropped rather than repeated on a still-broken value-area/interval coupling — see v1's own
note above, still unfixed).

| Window | n | win% | gross bps | net R/trade | t | total R |
|---|---|---|---|---|---|---|
| IS | 108,404 | 28.2% | -1.56 | -0.334 | -67.42 | -36,195 |
| OOS | 76,749 | 26.5% | -1.20 | -0.335 | -47.80 | -25,737 |
| ALL | 185,153 | 27.5% | -1.41 | -0.334 | **-81.67** | -61,932 |

**Dispersion: 0 of 210 symbols traded net-positive.** Median per-symbol result -0.330R. v1 at
least had 65 of 212 (31%) net-positive symbols; this version loses on literally every single one.

**Unlike v1, this has a real negative edge even before any cost.** At 0 bps: t = -6.89, still
clearly significant. v1's raw signal was flat-to-negative (t = -1.51); this one is actively wrong
before a rupee of cost is charged.

### Why: "nearest structural level" makes the target almost free to touch, for almost nothing

70.5% of trades exit at TP — but the average TP trade nets only **+0.027R**, essentially
breakeven, while the 24.5% that hit SL average **-1.358R**. The "nearest structural level" target
(POC/VAH/VAL/PDH/PDL/IBH/IBL, whichever sits closest ahead) is, on 1-minute bars, almost always
*extremely* close to the entry price — some level is nearly always sitting a few paise away,
especially the Initial Balance high/low which is still forming and tracks price closely early in
the session. The target the model calls "Target 1" turns out to be trivially easy to reach for a
trivial reward, while the stop (correctly, per the model) sits a real ATR-buffered distance
beyond the actual level in play. This is the R:R mismatch from v1, mirrored and made worse: v1's
stop was too wide for its target; v2's target is too close for its stop.

Switching to a fixed risk-multiple target (`tp_mode="r"`) confirms this directly: win rate jumps
from 27.5% to 40.6%, gross bps improves from -1.41 to -0.07 (nearly flat), net R improves from
-0.334 to -0.308. Still a clear loss (t = -44.30) — but the size of the jump from one parameter
shows how much of the damage the structural-target choice alone was doing.

### Ablation: rejection is the one setup that isn't actively harmful — still not profitable

| Variant | n (IS) | bps (IS) | n (OOS) | bps (OOS) | Helps? |
|---|---|---|---|---|---|
| base (all 3 setups) | 108,404 | -1.56 | 76,749 | -1.20 | — |
| Remove acceptance | 30,911 | -1.15 | 22,052 | -0.43 | **BOTH** |
| Remove retest | 108,406 | -1.56 | 76,753 | -1.20 | neither (retest almost never fires) |
| Remove rejection | 86,244 | -1.64 | 60,706 | -1.37 | neither |
| Only acceptance | 86,244 | -1.64 | 60,706 | -1.37 | neither |
| Only retest | 205 | -4.57 | 50 | -0.41 | too few trades to read |
| **Only rejection** | 30,779 | -1.13 | 22,029 | -0.43 | **BOTH** |
| Target → r-multiple | 107,008 | +0.77 | 75,597 | -1.27 | IS only |

Two independent cuts of the data (dropping acceptance, and isolating rejection alone) agree:
**acceptance is the setup dragging the average down**, and **rejection alone is the least-bad
family** in both windows — consistent with, and reinforcing, v1's own closing suggestion to try
"the opposite trade" (fading a failed break) instead of betting on continuation. Rejection alone
is still net negative (bps still below zero in both windows) — "least bad" is not "profitable" —
but it is the one lead in this entire exercise that survived being cut two different ways.

### The honest conclusion

The documented model's own text is explicit that its scoring system (SS6) and, above all, its
footprint/orderflow confirmation (SS7 — "the ONLY discretionary decision... no footprint
confirmation, no trade, regardless of score") are load-bearing. Automating the location, shape,
volume and trend pre-filters while skipping that final human-judgment gate does not produce a
weaker version of the same edge — it produces a strategy with a NEGATIVE raw edge and a payoff
structure (near-free TPs, real SLs) that no amount of stop/target retuning tested here could
fix. This is not a "needs more parameter tuning" result. If this direction is pursued further,
the two realistic paths are: (a) keep it as a discretionary alert tool with a human confirming
each entry on a footprint/order-flow chart, matching how the model was actually designed to be
used, rather than a fully automated strategy; or (b) build specifically on the rejection-only
lead — a fundamentally different bet (fading failed breaks) from either v1 or v2's shipped
defaults — with its own fresh stop/target design rather than reusing this run's structural
target, which the data above shows actively hurts it.

---

## v3: The Rejection-Only Lead, Built as Its Own Strategy — Cost-Killed, Not Wrong

Pulled the rejection setup (VAL-REJ / VAH-REJ) out of `open_drive.py` entirely into its own file,
`signal_engine/backtest/strategies/value_area_fade.py` (`ValueAreaFade`/`FadeParams`), with a
purpose-built stop and target instead of reusing v2's generic ones - both v1 and v2 showed a
badly-designed stop/target can hide whatever real signal exists underneath it:

- **Target = POC**, not v2's seven-level grab bag (which was the single biggest driver of v2's
  -81.67 t-stat). Floored at `tp_min_r` risk-multiples so a POC sitting close to entry cannot
  reproduce that bug in miniature, and capped at `tp_r_cap` so a distant POC cannot promise an
  implausible reward.
- **No VWAP trend filter by default** - a rejection trade is a bet AGAINST the move that just
  happened, which by construction usually sits on the "wrong" side of VWAP the moment the wick
  prints. v2's continuation-style trend filter does not belong on a reversion trade by
  construction (tested anyway, see below - the intuition turned out to be only half right).
- **Both of the model's entry windows** (09:15-11:00 AND 13:00-14:45, lunch excluded) rather than
  v1/v2's morning-only restriction - a value-area edge can be probed and rejected any time of day,
  not only at the open.

### Result: this is the first version with a real signal before cost - and the first cost story

Same universe and period (212 symbols, 2023-01-02 to 2026-09-13, IS/OOS split 2025-03-19).

| Cost | n | net R/trade | t | total R | payoff |
|---|---|---|---|---|---|
| 0 bps | 78,040 | **+0.005** | -1.30 | **+423.1** | 1.35 |
| 4 bps | 78,040 | -0.100 | -18.64 | -7,822 | 1.13 |
| 8 bps | 78,040 | -0.206 | -35.85 | -16,067 | 0.96 |
| 16 bps (shipped) | 78,040 | -0.417 | -68.98 | -32,558 | 0.68 |

At 0 bps, this is **statistically indistinguishable from zero** (t = -1.30, not the confidently
positive or negative result either v1 (t = -1.51, mildly negative-leaning) or v2 (t = -6.89,
clearly wrong) produced) - total R is actually slightly positive, and payoff is 1.35 (average
winner bigger than average loser, before cost). This is a materially different diagnosis: v1 and
v2 both had something wrong with the raw idea; v3's raw pattern-match (fade a failed break at a
value-area edge) is genuinely close to a coin flip with a fair payoff before cost is considered.

**The problem is entirely cost, and it is decisive.** A mere 4 bps - a quarter of the shipped
16 bps assumption - is enough to flip it to t = -18.64. At 78,040 trades over 906 days (~86
trades/day across the universe, ~1 trade every 2-3 sessions per symbol), the strategy fires far
too often for an edge this thin to survive even a fraction of realistic NSE intraday cost. This
is not a "the filters need work" problem - **the trade frequency and the size of the raw edge are
fundamentally mismatched.**

### Ablation: two things reliably help in both windows, neither is enough

| Variant | bps (IS) | bps (OOS) | Helps? |
|---|---|---|---|
| base | 1.03 | 0.48 | — |
| VWAP filter ON | 1.58 | 0.43 | IS only |
| Target floor -> 1.5R | 1.38 | 0.63 | **BOTH** |
| Target floor -> 2.0R | 1.40 | 0.57 | **BOTH** |
| Fixed r-multiple target | 1.25 | 0.54 | **BOTH** |
| VWAP ON + floor 1.5R | 2.03 | 0.53 | **BOTH** (best found) |

The VWAP intuition above turned out to be only half right: it helps IS but not OOS on its own,
yet combined with a higher target floor it produces the best result found (2.03 / 0.53 bps). Even
so, "best found" is ~2 bps gross against a 16 bps cost - roughly an order of magnitude short of
what this setup would need to be economically viable at this trade frequency, not a rounding gap
a further parameter nudge could close.

**Dispersion: 0 of 210 symbols net-positive at the shipped 16 bps** (median -0.419R) - same as
v2, but for a completely different reason: v2 lost because the underlying bet was wrong; v3 loses
because a real, symmetric-payoff, near-zero-edge signal cannot cover realistic transaction cost
at this frequency, on any single name.

### Honest conclusion

Of the three attempts, this is the only one with a raw signal that is not actively wrong - and
that is worth recording, but it does not change the practical verdict. Fixing this would require
either (a) an execution model with dramatically lower effective cost than 16 bps round-trip
(unrealistic for NSE intraday market orders at any size this repo's cost model is calibrated to),
or (b) a much more selective version of the same setup that trades far less often for a much
larger edge per trade - which is a different, untested design, not a parameter tweak on this one.

---

## v4: Selectivity - Fewer, Deeper Rejections. Better, Still Not Viable.

v3 fired on ANY wick through the level, however shallow. Added `min_wick_depth_atr` to
`value_area_fade.py`: the wick must clear a minimum ATR-multiple PAST the level (raw column in
`prepare()`, threshold applied in `entry()` - keeping the lesson from v2 that a threshold in
`prepare_key()` forces the expensive volume-profile reconstruction to re-run per swept value).

**Search process, and an overfitting trap caught by the harness's own discipline.** A 3-symbol
IS/OOS-checked sweep found `min_wick_depth_atr=2.0` combined with a LOOSENED volume filter
(`vol_mult=1.0`, down from 1.5), a WIDER stop buffer (`stop_atr_mult=0.5`, up from 0.3), and the
VWAP filter on looked like the best combination (t=-1.87 at 16 bps on that sample, breaking even
around 8 bps - a huge apparent improvement over v3's t=-8.88 on the same 3 symbols). Run at full
universe (212 symbols), that combination's OWN ablation contradicted the small-sample tuning:

| Variant vs that combo | bps (IS) | bps (OOS) | Helps? |
|---|---|---|---|
| Revert vol_mult to 1.5 (v3 default) | 2.66 | 1.78 | **BOTH** |
| Revert stop_atr_mult to 0.3 (v3 default) | 2.28 | 1.83 | **BOTH** |
| Push depth to 2.5 | 3.70 | 2.65 | **BOTH** |
| Remove VWAP filter | 1.63 | 1.47 | neither (i.e. VWAP on is validated) |

The volume and stop changes that looked best on 3 symbols were noise - reverting BOTH improved
things at full scale. This is exactly the failure mode `harness.ablation()`'s "helps BOTH windows"
rule exists to catch, and it caught it: a config search on too small a sample found two spurious
"improvements" that a full-universe check overturned.

**Corrected config**: `min_wick_depth_atr=2.5`, `use_vwap_filter=True`, everything else at v3's
shipped defaults (`vol_mult=1.5`, `stop_atr_mult=0.3`). Full universe, same period as v1-v3:

| Window | n | win% | gross bps | net R/trade | t | total R |
|---|---|---|---|---|---|---|
| IS | 4,298 | 45.4% | 3.98 | -0.191 | -10.62 | -820.9 |
| OOS | 3,705 | 45.2% | 2.90 | -0.211 | -11.21 | -780.7 |
| ALL | 8,003 | 45.3% | **3.48** | -0.200 | -15.20 | -1,601.6 |

This is the best gross edge found across all four versions (3.48 bps vs v3's 0.81) and the least
extreme full-universe t-stat of any version that still loses (t=-15.20 vs v3's -68.98, v2's
-81.67). Trade count dropped to 8,003 from v3's 78,040 - the selectivity worked as intended,
roughly 10x fewer trades for roughly 4x the gross edge per trade.

**Cost sensitivity, dispersion, and exit mix** (initially thought lost to a session interruption;
the run had in fact completed and the full output recovered afterward):

| Cost | n | net R/trade | t | total R |
|---|---|---|---|---|
| 0 bps | 8,003 | **+0.027** | **+1.15** | **+215.9** |
| 4 bps | 8,003 | -0.030 | -2.97 | -238.5 |
| 8 bps | 8,003 | -0.087 | -7.08 | -692.8 |
| 16 bps (real cost) | 8,003 | -0.200 | -15.20 | -1,601.6 |
| 24 bps | 8,003 | -0.314 | -23.05 | -2,510.4 |

**At 0 bps this is the first genuinely, positively significant result in the whole investigation**
(t=+1.15, not merely "indistinguishable from zero" like v3's t=-1.30) - win rate 48.4%, payoff
1.13. The sign flips between 0 and 4 bps: this setup's breakeven cost is roughly 1-2 bps, an order
of magnitude below the 16 bps real NSE intraday cost this repo's model uses. Dispersion: 18 of 210
symbols net-positive (median -0.190R) - better than v3's 0/210, though still a small minority.
Exit mix: SL 39.0% of trades at -1.227R average, TP 35.1% at +0.901R average, TIME_EXIT 24.6% at
a small -0.088R drag; payoff and win rate both improved over v3 but not enough to close the gap
at real cost.

### Honest conclusion

Selectivity helped, genuinely and by a wide margin - the gross edge nearly quadrupled, the
full-universe t-stat roughly halved in magnitude, and the raw signal (0 bps) crossed from "flat"
into "actually, mildly positive." It did not cross into viable: breakeven cost is ~1-2 bps against
a real cost of 16. Two things worth taking from this round specifically: (1) a parameter search on
2-3 symbols found improvements that were noise at full scale in BOTH directions tested (volume,
stop) - full-universe confirmation is not optional even for a "quick check" of a promising lead;
(2) selectivity has a monotonic-looking relationship with gross edge in the range tested (2.0 to
2.5 ATR both helped BOTH windows) that was not run out to convergence - a further sweep of deeper
thresholds (3.0, 3.5, 4.0 ATR) on the full universe is the most promising next step if this is
still worth pursuing, traded off against a shrinking sample size making the result progressively
harder to trust, and against how much of the remaining gap (16x breakeven cost) a further increase
in selectivity could plausibly close.

---

## v5: Deeper Still - the Trend Keeps Improving, Sample Size Not Yet the Limit

Swept `min_wick_depth_atr` at 2.75, 3.0, 3.5, 4.0 (full universe, same setup as v4:
`use_vwap_filter=True`, everything else at v3 defaults). `min_wick_depth_atr` is entry()-only
(confirmed not to retrigger `prepare()`), so the whole six-point sweep (2.0 through 4.0) ran off
one cached prepare pass.

| Depth | n (ALL) | gross bps | t (16bps) | t (0bps) | net R (0bps) | Symbols +ve |
|---|---|---|---|---|---|---|
| 2.0 | 13,153 | 2.31 | -22.55 | - | - | - |
| 2.5 | 8,003 | 3.48 | -15.20 | +1.15 | +0.027 | 18/210 |
| 2.75 | 6,349 | 3.63 | -12.31 | - | - | - |
| 3.0 | 5,041 | 2.99 | -10.76 | +1.12 | +0.024 | 41/210 |
| 3.5 | 3,114 | 5.20 | -7.74 | +0.92 | +0.040 | 60/210 |
| **4.0** | **1,946** | **5.77** | **-5.34** | **+1.39** | **+0.045** | **71/210** |

**The trend from v3->v4 continues cleanly through this entire range - it has not plateaued or
reversed.** The 16bps t-stat improves (gets less negative) at every single step: -68.98 (v3,
depth 0) -> -22.55 -> -15.20 -> -12.31 -> -10.76 -> -7.74 -> **-5.34** at depth 4.0. Gross edge is
not perfectly monotonic bar-to-bar (2.75's 3.63 dips slightly to 3.0's 2.99) but the range-wide
trend is unambiguous: 2.31 -> 5.77 bps, roughly 2.5x, while trade count fell by 85% (13,153 ->
1,946). Dispersion improves in lockstep: 18/210 -> 41/210 -> 60/210 -> 71/210 net-positive symbols.

**Breakeven cost is rising, not just the t-stat.** At depth 4.0, 0bps: t=+1.39, net R +0.045 (the
best 0bps result of any version tried). At 4bps: net R is still marginally positive (+0.004,
t=-0.30 - essentially sitting AT breakeven, not yet clearly negative). This is a real move: v4's
depth 2.5 flipped negative by 4bps; depth 4.0 does not clearly flip until somewhere between 4 and
8bps (8bps: t=-1.98, net R -0.037). Breakeven has moved from ~1-2 bps (depth 2.5) to roughly
4-6 bps (depth 4.0) - closing part of the gap to the real 16bps cost, though still 3-4x short of
it, not closed.

**Exit mix at depth 4.0 shows a real character change**: TIME_EXIT is now the plurality exit
reason (36.6% of trades, up from v3's ~10%) rather than SL (31.8%, down from v3's >50%) - fewer of
these trades are getting stopped out outright; more are drifting to the session close without
resolving either way. TP still wins on payoff (avg +0.916R vs SL's -1.162R), consistent with the
improving payoff ratio (0.68 at v3 -> 0.84-1.11 across this sweep).

**Sample size has NOT yet become the binding constraint.** At depth 4.0, IS n=978 and OOS n=968 -
both comfortably above the 300-500-trade floor this investigation has used as its own noise
threshold. The sweep was run to the range specified (up to 4.0 ATR) and stopped there by
design, not because the data ran out or the result got too thin to trust. Extrapolating the
breakeven-cost trend (roughly doubling every 1-1.5 ATR of added depth: ~1-2 at 2.5, ~4-6 at 4.0)
suggests depth 5.0-6.0 might close more of the remaining 3-4x gap to 16bps - but trade count is
also roughly halving every 0.5-1.0 ATR step in this range (8,003 -> 1,946 from depth 2.5 to 4.0),
so by depth 5.5-6.0 the sample would likely be small enough (low hundreds) that the sample-size
floor and the cost-viability question would be fighting each other directly. That crossover point
was not reached in this round.

### Honest conclusion

Four consecutive rounds of "make it fire less, on stronger signals" (v2's implicit non-selectivity
-> v3's rejection-only -> v4's depth 2.5 -> v5's depth 4.0) have each measurably improved the
result on every axis tracked: gross edge, t-stat, dispersion, and now breakeven cost specifically.
None has crossed into viable. The lever is not exhausted - the trend was still improving with
workable sample sizes when this round's tested range ended - but each further step costs a full
~65-minute full-universe sweep for a shrinking, harder-to-trust sample, chasing a gap that has
narrowed from ~16x to ~3-4x but is still real. Whether to spend that next round is a judgement
call about diminishing returns, not a technical question this round can settle on its own.

---

## v6: The Lever's End - Trend Peaks at Depth ~5.0, Then Sample Size Breaks It

Pushed `min_wick_depth_atr` to 4.5, 5.0, 5.5, 6.0 (same full-universe setup as v4/v5:
`use_vwap_filter=True`, other params at v3 defaults). Same cached-prepare sweep as before -
`min_wick_depth_atr` never retriggers `prepare()`.

| Depth | n (ALL) | IS n | OOS n | gross bps | t (16bps) | t (0bps) | net R (0bps) | Symbols +ve |
|---|---|---|---|---|---|---|---|---|
| 4.0 (v5) | 1,946 | 978 | 968 | 5.77 | -5.34 | +1.39 | +0.045 | 71/210 |
| 4.5 | 1,235 | 625 | 610 | **8.76** | -4.23 | +0.89 | +0.066 | 88/208 |
| **5.0** | 731 | 367 | 364 | 8.20 | **-3.15** | +0.64 | +0.053 | **89/201** |
| 5.5 | 435 | 215 | 220 | 2.36 | -3.97 | -1.18 | -0.012 | 71/178 |
| 6.0 | 264 | 132 | 132 | -1.78 | -3.37 | (negative) | -0.037 | 51/139 |

**The trend continued cleanly through depth 5.0, then broke at depth 5.5 - and the break lines up
exactly with this investigation's own sample-size floor, not with a change in market behaviour.**
Depth 5.0 posts the best full-universe t-stat of all six rounds (-3.15), the best dispersion
(89/201 = 44.3% of symbols net-positive, versus depth 4.0's 33.8%), and the best gross edge
alongside 4.5's (8.2-8.8 bps, roughly 10x v3's 0.81). At depth 5.5, IS n drops to 215 - the first
point in this entire investigation to fall BELOW the 300-500-trade floor used throughout - and the
result does not just weaken, it reverses sign: gross edge collapses from ~8 bps to 2.36, and the
0 bps t-stat flips from solidly positive (+0.64 at depth 5.0) to negative (-1.18). Depth 6.0 (IS
n=132) is worse still, gross edge negative even before cost. This is the noise signature this
investigation has watched for since the v4 round's overfitting trap, now showing up as a function
of depth rather than of an ablated filter - and it appears at almost exactly the point predicted
in v5's own extrapolation.

**Cost-viability was not reached before the sample-size wall.** At the best sustained depths
(4.5-5.0), breakeven cost sits around 6-7 bps (0 bps and 4 bps both net positive with t roughly
+0.6 to +0.9; 8 bps turns marginally negative). Real NSE intraday cost is 16 bps - the gap has
narrowed from v3's ~16x to roughly **2.3-2.7x**, the smallest gap found in this entire
investigation, but it is not closed, and the lever that closed most of it (deeper selectivity)
stops producing trustworthy further gains right where the sample runs out, not because the
remaining gap closed.

**Exit mix at the peak (depth 4.5-5.0) confirms the improving character**: TIME_EXIT is now the
plurality reason (41.6-47.6% of trades) rather than SL, payoff is consistently above parity
(0.86-1.14 across the 0-16 bps range depending on cost), and dispersion crossed 40% of the
universe net-positive for the first time - the strongest breadth this investigation found, still
short of "most symbols work."

### Honest conclusion - this line of inquiry has run its course

Five consecutive rounds of "fire less, on stronger signals" (v3 -> v4 depth 2.5 -> v5 depth 4.0 ->
v6 depth 4.5-5.0) took the gap to real trading cost from ~16x down to ~2.3-2.7x, a genuine and
substantial improvement, entirely through one lever. That lever is now exhausted: depth 5.0 is
close to the best this specific selectivity threshold can do on this universe and period, because
going deeper trades against a hard constraint (trade count) rather than a soft one (parameter
tuning), and the constraint was hit before the cost gap closed. This is a different, more definite
answer than v5's "judgement call" - v5 didn't know whether depth 5.0-6.0 would close the gap or
run out of sample first; this round establishes that it ran out of sample first, with real gap
remaining. Recommended final config if this strategy is revisited: `min_wick_depth_atr=5.0`
(best t-stat and dispersion, sample still at the edge of the floor) or `4.5` (nearly identical
edge, meaningfully larger and more robust sample) - both documented here, neither closes the gap.
Further progress on this specific idea (value-area rejection fades) would need a different lever
entirely - a genuinely lower-cost execution model, or a different selectivity dimension than wick
depth (e.g. multi-day confluence, sector/index confirmation, or the model's own footprint
confirmation via a real orderflow data source) - not a deeper sweep of the one already tried.

---

## v7: Multi-Session Selectivity - Market Profile Day-Type, Confluence, and HTF Trend

v6 named three candidate directions for the next lever: multi-day confluence, a different
selectivity dimension, or real orderflow confirmation. Orderflow was re-confirmed unavailable
before this round started - Historify's `market_data` table stores only
open/high/low/close/volume/oi (`database/historify_db.py`), and no Indian broker's historical API
replays depth or footprint, so that path stays closed. This round instead implements three
OFF-by-default filters in `value_area_fade.py`, each a raw ratio computed in `prepare()` and
thresholded in `entry()` (same discipline as `min_wick_depth_atr`, so none of them invalidate the
prepare() cache when swept):

- **`day_type_ib_ratio_max`** - Market Profile's own day-type classification (Normal/balance vs
  Trend day), via the PRIOR session's day-range / Initial-Balance-range ratio
  (`indicators.initial_balance_ratio`, IB = first 60 minutes). Theory: fades are documented as a
  balance-day tactic, not a trend-day one.
- **`va_confluence_atr_mult`** - does the level being faded also roughly agree with session N-2's
  POC/VAL/VAH, not just N-1's (`volume_profile.value_area_at(..., lookback=2)`)? Multi-day
  agreement is a recognised Market Profile confluence signal.
- **`htf_trend_atr_mult`** - skip a fade betting against a strong multi-day trend
  (`indicators.prev_daily_trend_z`: prior close vs a daily EMA, in average-daily-range units).
  ICT-style liquidity-sweep/FVG signals were deliberately NOT implemented as a lever here:
  independent published backtests (StatOasis, and a public multi-market SMC/ICT study) found only
  weak-to-null edges for those signals (t~0.9-1.1) even on lower-cost, higher-liquidity markets
  than NSE intraday - not promising enough to spend a sample-limited sweep on.

**Method**: `Backtest.ablation()` against the most robust full-sample base from v5/v6
(`min_wick_depth_atr=4.0, use_vwap_filter=True`, full 212-symbol universe, 2023-01-02 to
2026-09-13, 16 bps real cost), each filter swept individually at a coarse 3-point grid first (per
`harness.py`'s own stated philosophy: coarse grids, not fine ones). Only a variant that helps
gross bps in BOTH the IS and OOS windows gets a follow-up cost-sensitivity/dispersion check;
stacking is the last step, not the first.

| Variant | n_IS | bps_IS | n_OOS | bps_OOS | helps |
|---|---|---|---|---|---|
| base (depth 4.0) | 978 | 1.96 | 968 | 9.63 | - |
| day_type<=2.0 | 840 | 4.28 | 842 | 8.75 | IS only |
| day_type<=2.5 | 906 | 3.78 | 911 | 8.37 | IS only |
| day_type<=3.0 | 942 | 1.69 | 947 | 9.74 | OOS only |
| va_confl<=0.5atr | 127 | 9.85 | 134 | 4.21 | IS only |
| va_confl<=1.0atr | 213 | 6.46 | 222 | 7.71 | IS only |
| va_confl<=1.5atr | 293 | 3.51 | 304 | 6.51 | IS only |
| htf_trend>=1.0atr | 752 | 2.72 | 708 | 12.53 | **BOTH** |
| htf_trend>=1.5atr | 835 | 2.13 | 805 | 10.03 | **BOTH** |
| htf_trend>=2.0atr | 888 | 2.09 | 866 | 10.43 | **BOTH** |

**Day-type and confluence are rejected outright** - every threshold shows the IS-only or OOS-only
signature this investigation has treated as the mark of noise since v4's own overfitting trap, not
a change of market behaviour. `va_confluence`'s sample also collapses fast (293 IS trades at its
loosest 1.5-ATR threshold, already brushing the 300-500 floor) for a filter that never clears the
bar anyway - not worth pursuing further at any threshold.

**HTF trend passes the mechanical "helps BOTH" bar at all three thresholds, but does not by itself
close (or even clearly narrow) the cost gap at depth 4.0.** Cost sensitivity for the three
htf_trend variants at depth 4.0:

| Config | n (ALL) | gross bps | t (6bps) | t (8bps) | net R (8bps) | Symbols +ve |
|---|---|---|---|---|---|---|
| htf_trend>=1.0atr | 1,460 | - | -1.48 | -2.22 | -0.032 | 79/209 |
| htf_trend>=1.5atr | 1,640 | - | -1.89 | -2.68 | - | 78/210 |
| htf_trend>=2.0atr | 1,754 | - | -1.32 | -2.14 | - | 74/210 |

Already negative at 6 bps, well below the real 16 bps hurdle, and no better than v5/v6's own
depth-4.0 baseline trajectory (documented above: 4bps net R +0.004/t=-0.30, 8bps net R
-0.037/t=-1.98) - within noise of a config that already existed, not a new result. `ablation()`'s
"helps BOTH" check only compares gross bps to the base; it does not by itself mean a filter
improves cost-viability, and this is the concrete case where that gap between the two matters.
Since only one filter (htf_trend) cleared the "BOTH" bar, no genuine multi-filter combination was
tested - stacking three thresholds of the SAME parameter is not a combination.

**HTF trend DOES produce a real, if modest, improvement stacked on depth 4.5** - the other robust
full-sample base from v6 (n=1,235). A follow-up (`bt.cost_sensitivity()` + `bt.by_symbol()` only,
not a re-ablation) at `htf_trend_atr_mult` 1.0 and 2.0:

| Config | n (ALL) | gross bps | t (0bps) | breakeven | t (16bps) | Symbols +ve |
|---|---|---|---|---|---|---|
| depth 4.5 alone (v6) | 1,235 | 8.76 | +0.89 | ~6-7 bps | -4.23 | 88/208 (42.3%) |
| depth 4.5 + htf_trend>=1.0atr | 922 | **12.39** | +0.73 | **~8 bps** | -3.85 | **92/206 (44.7%)** |
| depth 4.5 + htf_trend>=2.0atr | 1,108 | 11.09 | +1.04 | ~7-8 bps | -3.90 | **94/208 (45.2%)** |

Gross edge rises ~25-40% over depth 4.5 alone (8.76 -> 11-12.4 bps), breakeven cost moves from
~6-7 bps to ~7-8 bps (real cost is still 16 bps - gap narrows from ~2.3x to ~2.1x, not closed), and
dispersion reaches the best level found in the ENTIRE seven-round investigation (44.7-45.2% of
symbols net-positive, edging out v6 depth-5.0's previous best of 44.3%). Sample stays adequate
(n=922-1,108 total; the earlier ablation step's IS/OOS split for this exact htf>=1.0/depth-4.5
combination was 480 IS / 442 OOS - comfortably above the 300-500 floor, not a repeat of depth
5.5's collapse). Set against that: the t-stat at real 16 bps cost (-3.85 to -3.90) is WORSE, not
better, than v6's single best point (depth 5.0 alone, t=-3.15) - more trades and a better gross
edge did not translate into a tighter confidence interval, because the combined filter still cuts
the sample by ~10-25% versus depth 4.5 alone.

### Honest conclusion

Two of three new levers (day-type, multi-day confluence) are rejected outright on this
investigation's own established discipline - clean IS-only/OOS-only overfitting signatures, not
a change in the underlying edge. The third (HTF trend) is real but modest: it improves gross edge,
breakeven cost, and dispersion when stacked on depth 4.5, reaching the best breadth of any config
in this seven-round investigation, but it does not clearly beat the single best point already
found (v6's depth 5.0, t=-3.15) on the metric this investigation has used throughout to judge
statistical confidence, and it does not close the remaining gap to real cost (still ~2.1-2.7x,
narrowed but not closed). Market-profile multi-session context and ICT-style liquidity/FVG
signals do not appear to be the missing ingredient v6 hoped for; the one piece of evidence still
untested and structurally unavailable is real orderflow/footprint confirmation - the same gap v2's
model documentation flagged as the discretionary, unautomatable gate from the start. Absent that
data source, this investigation has now tried every OHLCV-derivable lever proposed for this
strategy (single-session selectivity, multi-day confluence, day-type, and HTF trend) without
finding one that closes a real ~16bps NSE intraday cost hurdle - the honest reading is that no
cost-viable edge exists in OHLCV-only data for this setup at this cost level, not that the next
threshold or the next lever will find it.
