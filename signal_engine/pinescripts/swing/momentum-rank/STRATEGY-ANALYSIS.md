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

---

## 2026-09-15 — Fine-tuning against Historify (broker-verified) daily bars

**What changed:** `rebalDays` default moved from 21 to 30 sessions. Everything else
(lookback 250, skip 21, top_n 8, min_price 20) is unchanged.

### An important data-window correction first

`data.from_historify_daily()` — OpenAlgo's own broker-fed store, the source `portfolio.py`'s
header calls "the loader to prefer for anything reported" — only has daily bars back to
**2019-12-03** for this universe, not 2016. The original +12.7%/yr alpha, 36.9% CAGR numbers
above were produced against a longer (2016-2026) source. Re-running the identical
methodology on the shorter, broker-verified ~6.75-year window gives a **much weaker and
statistically insignificant out-of-sample result for the shipped config**: OOS alpha
+4.55%/yr, t = 0.71 (need t > ~3.7 to call it real after searching this many parameter
combinations). This is not a bug — it is the same "alpha is lumpy" honest limit already
documented above, just visible now because the 2016-2020 run-up that padded the old
in-sample window is mostly gone from this shorter store. **Treat the original 36.9%
CAGR / +10.7%/yr alpha headline as unverified against the platform's own broker data.**

### What was swept

Two grids, both `mom_skip` factor via `portfolio.py`, cost 22 bps, min price ₹20, 60/40
in-sample/out-of-sample split (cut 2023-12-28):

1. **197-name NSE F&O universe** (everything Historify has ≥500 daily sessions for) —
   300 configs: lookback {200,225,250,275,300}, skip {10,15,21,30}, top_n {6,8,10,12,15},
   rebal {21,30,42}. Marginal effects favored a bigger, more diversified basket (top_n
   12-15) and a slower rebalance (30d) — but that universe is **not what the shipped
   script can trade**: Pine's `request.security` call cap is 40, hard-coded as `u1..u40`.
2. **The actual 40-symbol Pine universe** — 180 configs, same axes minus top_n capped at
   12. This is the one that matters for what actually ships.

### Result on the deployable 40-name universe

| config | OOS alpha | OOS t | OOS max DD | all-period alpha | all-period t | all-period max DD |
|---|---|---|---|---|---|---|
| shipped: 250 / 21 / top8 / **21d** | +4.55%/yr | 0.71 | 16.8% | +13.53%/yr | 1.97 | 19.3% |
| **fine-tuned: 250 / 21 / top8 / 30d** | **+7.50%/yr** | 0.97 | **10.1%** | **+23.30%/yr** | 2.75 | **14.3%** |

Changing only the rebalance cadence (21 sessions -> 30, i.e. roughly monthly to roughly
six-weekly) improved every single number: more alpha in-sample, out-of-sample and overall,
lower drawdown in both windows, and a higher t-stat. It was also the most consistent
single-axis finding across BOTH grids (the 197-name sweep's rebal=30 group beat rebal=21
by a similar margin on alpha and, notably, drawdown: 19.3% average vs 28.0%). That
convergence across two independently-run universes is why it was adopted despite not
individually clearing the strict multi-trial significance bar (t=2.75 vs hurdle ~3.7) — a
single OOS window of only 22 rebalance periods (2024-2026) does not have the statistical
power to prove any config "real" on its own; this one is simply the most consistently
better in every direction tested, in both universes, on both cost-free and cost-loaded
runs.

**What did NOT move**: lookback, skip and top_n were re-swept on the 40-name universe and
none showed a clear, consistent improvement over the shipped 250/21/8 — the marginal
effects there were small and noisy (see raw grid CSVs). Concentration (top_n 6 vs 8 vs 10
vs 12) barely mattered within a 40-name pool; it mattered a lot in the larger 197-name
pool, which is the artifact of testing selectivity (top 8-of-40 = 20% vs top 12-of-197 =
6%) rather than a signal to chase without also growing the universe. **Growing the pine
universe beyond 40 names is not possible without moving this strategy off Pine's
`request.security` model** (e.g. a Python-native scheduled rebalance job pulling the F&O
list from OpenAlgo directly) - flagged as a future option, not done here.

### Honest status after this pass

Still **candidate, paper only, never traded**. The fine-tune is a risk-reduction (lower
drawdown, same or better alpha) supported by two independent grids agreeing on direction,
not a newly-proven edge — the original headline numbers cannot currently be reproduced
against the platform's own broker-verified data store because that store's depth (2019-12
onward) is shorter than the window they were computed on. Before this goes anywhere near
live capital: paper-trade the 30-day-rebalance version through at least a few real
rebalances, and treat the original 2016-2026 numbers as aspirational rather than
confirmed.

---

## 2026-09-15 (same day, follow-up) — Full-universe result adopted; Python-native Phase 1 shipped

The framing above restricted fine-tuning to Pine's deployable 40-symbol universe because
that was what the live `.pine` script could actually hold. **The user rejected that
framing**: backtests must use the full available F&O universe, not a subset chosen to fit
a deployment constraint — the constraint is a separate, explicitly-labeled finding, not a
reason to narrow the backtest itself. (Saved to memory —
`feedback_backtest_full_universe.md` — as a standing rule for every future strategy in
this project.)

**Re-reading the two grids with that correction: the full 197-name universe result is the
authoritative one**, not the 40-name-restricted one:

| config | OOS alpha | OOS t | OOS max DD | all-period alpha | all-period t | all-period max DD |
|---|---|---|---|---|---|---|
| shipped Pine (40-name): 250/21/top8/21d | +0.06%/yr | 0.25 | 32.7% | +12.26%/yr | 1.56 | 32.7% |
| **full-universe fine-tune: 300/21/top12/30d** | **+11.01%/yr** | 1.38 | **15.5%** | **+25.74%/yr** | **3.59** | **15.5%** |

Holding the top 12 of the full ~197-208 name universe (vs. top 8 of 40) roughly doubles
the Sharpe (1.44 -> 2.07) and halves the drawdown against the shipped config. This
requires a bigger, more diversified basket than Pine's 40-name cap can ever provide — so
closing this gap means moving the ranking step off Pine, not further tuning it in place.

**Decision: migrate to Python-native execution (Phase 1 only).** User chose this over (a)
keeping Pine and just picking a better 40, or (b) documenting the gap and doing nothing.
Phase 1 ships `strategies/examples/momentum_rank_strategy.py` +
`strategies/examples/momentum_rank_strategy.py` (single self-contained file, since
OpenAlgo's `/python` Strategy Host only accepts one uploaded `.py`; unit tests in
`strategies/examples/tests/test_momentum_rank_core.py`, including a numerical-parity
check against `portfolio.py`'s `build_factor("mom_skip", ...)`), running the fine-tuned
config (lookback 300, skip 21, top_n 12, rebal 30) against the full universe via
OpenAlgo's own `history` API, uploadable through the existing `/python` Strategy Host.

**Deliberately still manual execution, by design.** `signal_engine/main.py` resolves
strategy config via `strategy_profiles.get(tag, {})` — an unregistered strategy tag
silently falls through to global defaults rather than erroring, and `momentum-rank` is
registered nowhere. If a Python job posted machine-parseable "STRATEGY DIRECTION" alerts
into the live Telegram chat, the daemon would auto-execute them today, sized by whatever
the engine's global `sizing.mode` is against the strategy's fake 25%-wide placeholder
stop — not real equal-weight sizing. So the Phase 1 script sends **only a human-readable
digest** to its own, separate Telegram bot/chat; the trader still reads it and places CNC
orders manually, exactly as before, just now backed by the correct full-universe ranking.

**Phase 2 (not started, deliberately out of scope):** wiring this into automatic order
placement needs a new per-strategy sizing-mode override in `risk.py`/`config.py` (today
`RiskEngine.calculate_quantity` branches on one engine-global `sizing_mode`; no strategy
has ever gotten a per-strategy exception — confirmed by `grep` across `signal_engine/
*.py`), plus a `strategy_profiles.MOMENTUM-RANK` entry, plus a decision on whether to
keep synthesizing a fake wide SL/TP to satisfy the `Signal` schema or extend that schema
to make SL/TP genuinely optional for no-stop strategies. Revisit once the Python-computed
digests have been watched for a while.

The Pine script (`momentum-rank.pine`, `rebalDays=30` as of the earlier entry above)
remains as a secondary/legacy reference implementation, validated but permanently capped
at 40 names — not deprecated, just no longer the primary path for this strategy.

---

## 2026-09-16 17:40 — Weekly vs monthly rotation cadence, full universe

**Question:** does rebalancing weekly (for short-term moves) beat the already-adopted
~monthly cadence (for long-term moves), and does blending both timeframes help. Full
197-name F&O universe, Historify daily bars, `portfolio.py`, cost 22 bps, min price ₹20,
60/40 IS/OOS split (cut 2023-12-28) — same methodology as the two entries above, so these
numbers are directly comparable.

### Setup

Three factor families, each swept across rebalance cadence:

1. **12-1 factor** (lookback 300, skip 21 — the adopted long-term factor) at weekly (5d),
   biweekly (10d), ~3wk (15d), monthly (21d), the adopted 30d, and quarterly (63d).
2. **Pure short-term factor** (21d trailing return, and 63d-mom/skip-5) at weekly/biweekly/
   monthly — this is what "weekly rotation for short-term moves" means taken literally: a
   shorter lookback rebalanced faster, not just the long-term factor rebalanced faster.
3. **Blend ("both")** — 12-1 and 1-month-return factors combined via rank average
   (50/50 and 70/30 weight toward the long-term factor), rebalanced weekly and monthly.

### Result

| config | OOS alpha | OOS t | OOS max DD | ALL alpha | ALL t | ALL max DD | turnover/period |
|---|---|---|---|---|---|---|---|
| **12-1, adopted 30d** | +11.0%/yr | 1.38 | **15.5%** | **+25.7%/yr** | **3.59** | **15.5%** | 29.7% |
| 12-1, monthly (21d) | +7.8%/yr | 0.93 | 23.5% | +18.8%/yr | 2.50 | 23.5% | 24.2% |
| 12-1, weekly (5d) | +9.7%/yr | 1.15 | 32.7% | +18.7%/yr | 2.47 | 32.7% | 12.2% |
| 12-1, biweekly (10d) | +11.2%/yr | 1.20 | 26.1% | +19.9%/yr | 2.56 | 26.1% | 17.5% |
| pure 1m-momentum, weekly (5d) | +12.4%/yr | 1.32 | 24.8% | **+3.4%/yr** | 0.63 | 39.6% | 45.9% |
| pure 3m-mom/skip5, weekly (5d) | +8.8%/yr | 1.01 | 19.9% | +17.5%/yr | 2.31 | 19.9% | 27.0% |
| blend 50/50, weekly (5d) | +17.6%/yr | 1.68 | 31.4% | +24.1%/yr | 2.82 | 31.4% | 33.8% |
| **blend 70/30, weekly (5d)** | **+20.1%/yr** | **1.88** | 31.9% | +22.9%/yr | 2.73 | 31.9% | 28.9% |
| blend 50/50, monthly (21d) | +17.8%/yr | 1.65 | 28.5% | +20.9%/yr | 2.46 | 28.5% | 65.8% |

Full grid (18 configs x IS/OOS/ALL): `rotation_freq_results.csv` in that session's
scratchpad, not checked in — re-runnable via the sweep logic above against
`data.from_historify_daily()`.

### Answer

- **Weekly rotation of the long-term (12-1) factor alone does not help.** Same ALL-period
  alpha as monthly (~18.7-18.8%), worse drawdown (32.7% vs 23.5%), and both are clearly
  behind the already-adopted 30-day cadence (25.7% alpha, 15.5% drawdown, the only config
  here clearing the t > ~3.7 significance bar territory). Faster rotation just pays more
  turnover for a noisier version of the same signal — the 12-1 factor changes slowly by
  construction (it is a 300-day-back measurement), so sampling it every 5 days instead of
  every 21-30 mostly adds noise and cost, not information.
- **Weekly rotation using a genuinely short-term factor is weak or actively bad.** Plain
  1-month momentum rebalanced weekly loses money against the benchmark in-sample (-3.9%/yr)
  and only barely helps OOS — this reproduces the same short-horizon reversal effect that is
  the reason the 12-1 factor skips its most recent 21 days in the first place (see "Where
  the edge comes from" above). A 3-month/skip-5 factor rebalanced weekly is less bad
  (+17.5%/yr ALL) but still behind the adopted config on every axis.
- **"Both" (blending long-term and short-term momentum, weekly) is the one interesting
  result** — it has the best OOS alpha and OOS t-stat of everything tested here (blend
  70/30: +20.1%/yr, t=1.88), beating every pure-weekly and pure-monthly single-factor
  variant on the out-of-sample window specifically. But it comes with a meaningfully worse
  drawdown (~32% vs the adopted config's 15.5%) and no config here reaches the t > ~3.7
  hurdle this doc has used elsewhere to call a result "proven" — with only ~22-31 OOS
  rebalance periods, none of these differences are statistically distinguishable from noise
  yet.

**Recommendation:** keep the already-adopted 300/21/top12/**30d** monthly-ish cadence as
the primary config — it remains the best risk-adjusted result found across every sweep run
on this strategy (highest t-stat, lowest drawdown, lowest turnover cost). Weekly rotation on
its own is not supported by this data. The long+short blend rotated weekly is the only
weekly variant worth a second look, but it is a **new, untested idea** (not yet run through
the IS/OOS split as rigorously as the adopted config, not yet swept across blend weights or
lookbacks) and trades better OOS alpha for higher drawdown — flag as a future research item,
not a change to ship now.

**Status:** research only, no code changed. `momentum_rank_strategy.py` continues to run
the adopted 300/21/top12/30d config.

---

## 2026-09-16 (follow-up) — Fixed stale "checked once daily" text; digest clarity pass

**Bug found:** `momentum_rank_strategy.py`'s docstring, console log, and the Telegram digest
itself all claimed the strategy is "checked once daily." That was never true of the actual
deployment — `strategies/strategy_configs.json` schedules this script through the `/python`
host for a ~20-minute window on **Wednesdays only** (`schedule_days: ["wed"]`), i.e. weekly,
not daily. The script has no way to see its own host-level schedule, so the "daily" text was
just a leftover assumption from an earlier draft, never updated to match reality. Fixed in
both `strategies/examples/momentum_rank_strategy.py` (source of truth) and the live deployed
copy `strategies/scripts/momentum_rank_strategy_20260916144143.py` (gitignored — the file the
host actually executes), which had drifted out of sync with the source copy.

**Also fixed while rechecking the digest for trader clarity:** the digest always headlined
`ACTION REQUIRED: place CNC MARKET orders...` even on a rebalance where the top-N basket
didn't change (sells and buys both empty) — misleading, since there is nothing to trade that
period. It now reads `NO ACTION NEEDED: basket unchanged this rebalance` in that case.
Covered by two new tests in `test_momentum_rank_core.py`
(`test_no_buys_or_sells_does_not_claim_action_required`,
`test_buys_or_sells_still_flags_action_required`); all 23 tests in the suite pass.

Rest of the digest was reviewed and found already clear/actionable: explicit SELL/BUY/HOLD
lists with counts, per-slot equal-weight %, and an unambiguous "DIGEST ONLY - no order placed
automatically" disclaimer. The "Cadence: rebalances every 30 sessions" and "Next check: ...
weekly, not daily" lines now sit next to each other, made explicit that a *check* is not a
*rebalance* to avoid a trader reading "weekly" as the new rebalance frequency.

---

## 2026-09-16 (second follow-up) — End-to-end verification before treating this as a live
## trader recommendation

Walked the full live path — universe, filter criteria, ranking, and the sell/buy logic —
against what `portfolio.py` actually validated, since the digest now goes to a real trader
expecting these to be genuinely good CNC picks.

### Confirmed correct

- **Universe matches exactly.** `momentum_rank_strategy.py`'s hardcoded `UNIVERSE` (211
  names) diffed byte-for-byte against `signal_engine/backtest/data.py`'s `NSE_FNO` — zero
  names differ either direction. No drift between what was backtested and what is live.
- **New/thin-history names self-exclude correctly.** The live universe (211) is larger than
  the backtest's history-filtered universe (197, `min_sessions=500`) by design — recently
  listed F&O names (SWIGGY, TMPV, VMM, ATHERENERG, etc.) are included in `UNIVERSE` but
  `momentum_score()` returns `None` for any symbol without `LOOKBACK+SKIP` (321) sessions of
  real history, so they fall out of `usable` automatically. This reproduces the backtest's
  `min_sessions` filter at runtime instead of a static list — correct, not a gap.
- **Sell signals absolutely do fire during rebalance, independent of buys.** This is a
  rotation, not a buy-and-hold list: `diff_basket()` computes `sells = held - target` and
  `buys = target - held` independently. A name that falls out of the top-12 ranking is
  SOLD that rebalance even if no single new name displaces it 1-for-1 (e.g. two names drop
  out and two different names enter — 2 sells, 2 buys, not paired). The very first-ever
  rebalance is buy-only (`held=[]`), which is correct. With the partial-basket fix below,
  `len(held) == len(target) == TOP_N` on every subsequent rebalance, so in steady state
  `len(sells) == len(buys)` always — a pure sell-with-no-buy can no longer happen once the
  book is established, by construction, not by a special case.

### Bugs found and fixed (see `momentum_rank_strategy.py` diff)

1. **No data-corruption guard on fetched closes.** `fetch_universe_closes()` took
   `client.history()`'s `close` column on faith — a single non-positive tick (broker glitch,
   bad feed record) would have silently fed a nonsense ratio into `momentum_score()`,
   capable of ranking a garbage symbol into the top 12 and recommending it as a live BUY.
   **Fixed:** any symbol with a non-positive close anywhere in its fetched window is now
   dropped for that run (`fetch_errors` counter), not silently trusted.
2. **Live script could trade a smaller-than-tested basket.** `run_once()` checked that
   enough names had *score* history (`len(usable) >= TOP_N`) but never re-checked after the
   `MIN_PRICE` filter was also applied inside `rank_universe()`. `portfolio.py`'s
   `PortfolioBacktest.run()` requires the FULL `top_n` to survive every filter or it skips
   the period entirely (`if len(f) < cfg.top_n: continue`) — the live script had no
   equivalent, so it could have shipped a smaller, more-concentrated, partially-uninvested
   basket (at the same per-slot 8.3% weight, meaning real uninvested cash) that was never
   the portfolio validated by any backtest here. **Fixed:** `run_once()` now skips and
   retries next check if fewer than `TOP_N` names survive both filters, mirroring the
   backtest exactly.
3. **Stale "retry tomorrow" text**, same root cause as the earlier "checked once daily" fix
   — changed to "retry next check".
4. **Digest didn't surface data-quality problems to the trader.** If symbols failed to
   fetch or were dropped as corrupt, that only ever showed up in the console log, never in
   what the trader actually reads. **Fixed:** added a conditional "Data note: N/211 universe
   symbols excluded this run..." line, shown only when `fetch_errors > 0`.

### Known, NOT fixed — flag before sizing this up

- **Corporate-action (split/bonus) adjustment gap.** `signal_engine/backtest/data.py`'s
  `from_historify_daily()` explicitly split-adjusts every bar
  (`_split_adjust_daily()`) before any backtest number in this document was produced. The
  live script's `fetch_universe_closes()` calls `client.history()` — OpenAlgo's REST history
  endpoint, which is a broker-fed pass-through (`services/history_service.py` has no
  adjustment step, and no `broker/*/api/data.py` module in this repo applies one either).
  **If any live-fetched symbol undergoes a stock split or bonus inside its 321-session
  (~15-16 month) lookback window, its close series will NOT be back-adjusted, and
  `momentum_score()` will compute a spurious return off the discontinuity** (a 1:1 bonus
  halves the price overnight with no change in wealth — read naively, that looks like a
  -50% crash to the score). This is broker-agnostic — no broker integration in this repo
  currently normalizes corporate actions on the history endpoint. **Not fixed here**
  because a heuristic split-detector risks false positives (excluding a real, large
  momentum move) without a proper corporate-actions data source, and that is a bigger,
  separate piece of work than this pass. **Recommendation: before increasing size on this
  strategy, cross-check each rebalance's picks against a known corporate-actions calendar
  (e.g. NSE's corporate action announcements) for the trailing 16 months, or add a bulk
  adjustment step sourced the same way `_split_adjust_daily()` does.** This is the single
  highest-priority follow-up on this strategy.
- **No liquidity/volume filter beyond `min_price >= 20`.** Matches what was actually
  backtested (`portfolio.py` has no volume filter either), so this is not a live-only gap —
  but it means a technically-eligible, thinly-traded name could appear in the digest. Same
  limitation the backtest always had.
- **No ASM/GSM/trade-to-trade surveillance-stage awareness.** Nothing in OpenAlgo currently
  ingests NSE's surveillance-stage lists. A recommended BUY could be a stock currently under
  additional surveillance margin (does not block CNC delivery, but changes the effective
  cost). Not checked by this script or by the backtest.

### Status

Fixes above are shipped to both `strategies/examples/momentum_rank_strategy.py` (source) and
the live deployed copy `strategies/scripts/momentum_rank_strategy_20260916144143.py`. Test
suite: 27/27 passing. Strategy remains **candidate, paper-tracked** — these fixes improve the
integrity of what gets recommended, they do not constitute new evidence of an edge, and the
split-adjustment gap above should be resolved before this is traded with meaningful size.

---

## 2026-09-16 (third follow-up) — Per-symbol score in the digest; pre-settlement data guard

**1. Digest now shows each symbol's 12-1 momentum score, ranked best-first.** Buying all 12
names equal-weight needs meaningful capital; a trader who can only fund a subset had no way
to choose one over another before this change — every name looked identical in the digest.
`_build_digest()` now takes a `scores` dict (the live `usable` scores computed in
`run_once()`) and:

- Sorts SELL/BUY/HOLD and the "Full basket" section best-score-first (missing scores sort
  last, shown as `n/a` rather than silently omitted).
- Adds a one-line prompt: `"Capital-constrained? Buy top-down by score, not all TOP_N
  names."`
- The "Full basket" section is now a numbered, score-ranked list instead of a flat
  space-joined line, e.g. `1. RELIANCE  +61.0%` ... `4. TCS  +41.7%` — a trader funding only
  half the book can stop at the midpoint of this list rather than guess.

**Caveat carried into the digest implicitly, worth stating explicitly here:** the score is
the *ranking* signal, not a return forecast or a risk-adjusted priority order. Buying only
the top few by score is a real, different portfolio from "buy all 12 equal-weight" — it is
MORE concentrated than anything in the "Backtest metrics" section above, which tested equal
weight across the full `top_n`. Concentration was shown earlier in this document to buy
return and volatility roughly in equal measure (see "Robustness" section, portfolio-size
row) — partial-basket buying should expect a similar trade, not a free lunch.

**2. Pre-settlement data guard — the actual point of "ensure upcoming data is filled as day
progresses."** Traced the schedule end-to-end: `strategy_configs.json` had this strategy
running **14:45-15:05 IST**, which is *before* NSE cash market close (15:30 IST). Every
number in this document's "Backtest metrics" was computed from `from_historify_daily()`'s
SETTLED daily closes — the live script's `client.history()` call had no equivalent
guarantee. If the broker's daily-bar endpoint returns a live, still-forming candle for the
current session when queried intraday (behavior not verified per-broker; this repo has no
corporate-action-normalization layer either, see the split-adjustment gap above), the
ranking would be computed off a price that could still move before the real close — not the
"filled" data the backtest assumed.

**Fixed two ways:**
- **Code-level guard (the real fix, holds regardless of schedule):** `fetch_universe_closes()`
  now takes `now_ist` and calls `_today_bar_is_incomplete(bar_date, now_ist)` per symbol —
  true only when a symbol's last bar is dated *today* (IST) and now is before 15:30 IST + a
  10-minute settle buffer. A matching bar is trimmed off that symbol's series before scoring,
  so `momentum_score()` and `sessions_elapsed()` only ever see fully settled sessions, exactly
  matching what the backtest assumed — independent of whatever the host schedule is set to,
  now or later. Counted separately as `today_truncated`, NOT folded into `fetch_errors`: this
  is expected, correct behavior on a pre-close run, not a data-quality problem, and the digest
  says so via a distinct "Pre-settlement note" (only shown when `today_truncated > 0`) so it
  is never confused with the "Data note" that flags real fetch failures/corruption.
- **Schedule fix (removes the reason the guard fires every week):** moved
  `strategy_configs.json`'s window for this strategy from 14:45-15:05 to **15:45-16:15 IST**
  — 15 minutes past close, inside the code's own 10-minute settle buffer with room to spare,
  so a normal run now uses genuinely same-day settled data instead of silently falling back to
  yesterday's close every single week. `strategy_configs.json` is gitignored (user/deployment
  state, not source) — this was edited directly, not through git.

**Tests:** 10 new tests (`TestTodayBarIsIncomplete`, three new `TestFetchUniverseCloses`
cases, `TestBuildDigest` score/ranking/pre-settlement cases). Full suite: **37/37 passing**.

### Status

All fixes shipped to `strategies/examples/momentum_rank_strategy.py` (source),
`strategies/scripts/momentum_rank_strategy_20260916144143.py` (live deployed copy, kept in
sync), and `strategies/strategy_configs.json` (schedule). Strategy remains **candidate,
paper-tracked**. These are data-integrity and usability fixes to the recommendation pipeline,
not new evidence of an edge — the split-adjustment gap from the prior entry is still open and
remains the top item before sizing this up.

---

## 2026-09-16 (fourth follow-up) — Live dry-run confirmed; top-3/5 concentration tested

### Dry run against the live server

Ran `momentum_rank_strategy.py --dry-run` against this instance's actual running server and
broker session (Flattrade), using a scratch copy of the state file with an old
`last_rebalance_date` to force the 30-session gate open — dry-run never writes state, so the
real `log/strategies/momentum_rank_state.json` was untouched.

**Confirmed working end-to-end on real data:**
- The non-positive-close guard added in the previous pass fired for real: `SAGILITY` was
  excluded live with "non-positive close in history" — this is not a hypothetical test case,
  it caught an actual bad tick in this instance's broker feed on this run.
- Two symbols (`GVT&D`, `M&M`) failed with a Flattrade-side HTTP 500 ("jData is not valid json
  object") and one (`NIFTYFPI`) isn't a valid NSE equity symbol (index ticker miscategorized
  in the universe list) — all three correctly counted toward `fetch_errors` and surfaced as a
  "Data note: 4/211 universe symbols excluded" in the digest, not silently dropped.
- The score-ranked digest format renders correctly on live data (see below) — SELL/BUY/HOLD
  and the numbered "Full basket" list, capital-constrained buy-order prompt, all present.
- Because today's real held basket (from `momentum_rank_state.json`) still ranks inside the
  top 12 by live score, this run correctly produced `NO ACTION NEEDED` — a useful live
  confirmation that the "basket unchanged" branch fixed two passes ago works correctly, not
  just in the unit tests.

**Follow-up needed, not fixed here:** `NIFTYFPI` should be removed from `UNIVERSE` in both the
source and deployed copy — it is an index reference, not a tradeable NSE equity, so it will
permanently fail every fetch. Cosmetic (already correctly excluded, doesn't affect picks),
low priority.

### Concentrated portfolio (top 3 / top 5) vs the shipped top-12

Same adopted factor (12-1 momentum, lookback 300, skip 21) and same 30-day rebalance, full
197-name universe, only `top_n` varied — isolates the effect of buying fewer, higher-scored
names instead of the full rotation.

| top_n | OOS alpha | OOS t | OOS max DD | ALL alpha | ALL t | ALL max DD | ALL Sharpe |
|---|---|---|---|---|---|---|---|
| **top 3** | **-12.67%/yr** | -0.44 | 34.8% | **-6.32%/yr** | 0.04 | 47.6% | 0.63 |
| top 5 | -3.58%/yr | -0.04 | 29.2% | +14.24%/yr | 1.46 | 29.2% | 1.23 |
| top 8 | -0.40%/yr | 0.12 | 27.0% | +24.59%/yr | 2.55 | 27.0% | 1.71 |
| **top 12 (shipped)** | **+11.01%/yr** | **1.38** | **15.5%** | **+25.74%/yr** | **3.59** | 15.5% | **2.07** |
| top 20 | +9.05%/yr | 1.33 | **12.7%** | +15.31%/yr | 2.71 | **12.7%** | 1.79 |

**Result: going more concentrated than the shipped top-12 makes this WORSE, not better, on
every axis, not just risk.** Top-3 loses to the benchmark out-of-sample (-12.67%/yr alpha)
and roughly breaks even over the full period (-6.32%/yr, effectively noise, t~0). Top-5 is
still OOS-negative. Only from top-8 upward does the strategy show a real out-of-sample edge,
and top-12 is the single best point tested on every metric shown — best OOS alpha, best
t-stat, and (tied with top-20) the lowest drawdown.

**This is the opposite of the "Robustness" section's portfolio-size finding earlier in this
document** (top 10 beats top 30 on alpha, "concentration buys return and volatility in equal
measure"). That earlier result was measured on the longer, non-broker-verified 2016-2026
window with 21-day rebalance. This test uses the shorter, broker-verified 2019-2026 window
with the now-adopted 30-day rebalance — same tension already flagged in the 2026-09-15 entry
above (the original 2016-2020 run-up inflates results computed on the older window). Read
together: **do not extrapolate the old "smaller is more alpha" finding down to 3-5 names on
this dataset** — directly tested here, it fails.

**Why 3-5 names likely breaks down:** momentum names are individually volatile (see the
"Backtest metrics" table's win/loss ratio - about half of positions lose money, winners just
run much bigger). At 12 names, a handful of duds get diluted by the rest of the book. At 3-5,
a single bad pick is 20-33% of the entire book with no offsetting names that period - the
strategy's edge (a few huge winners compounding) needs enough names in rotation for those
winners to statistically show up every period, and enough names to dilute the losers when
they don't.

### Layman summary

**Don't cherry-pick just the top 3 or top 5 to save capital — on the numbers here, it made
the strategy worse, not better, in the recent test window.** With only 3 or 5 stocks, this
strategy actually lost to a simple "buy the whole basket" benchmark. It only starts clearly
beating the benchmark from about 8 stocks up, and 12 (what's already being recommended) was
the best-tested option, not just the safest. If capital genuinely doesn't stretch to all 12,
this data suggests going down to 8 is the lowest you should cut it to, not 3-5 — skipping
straight to the top 3-5 isn't supported by this backtest, it is actively contradicted by it.
(The digest no longer shows a per-symbol score at all as of the next entry below — this
result argues for buying the full basket, not for a better way to pick a subset of it.)

### Status

Research and live verification only — no code changes this pass beyond what the dry run
already exercised. Strategy remains **candidate, paper-tracked** at the shipped top-12
config; this pass provides evidence AGAINST going more concentrated, it does not change the
recommended config.

---

## 2026-09-16 (fifth follow-up) — Score removed from digest; Flattrade error hardening

### Digest: score removed, one symbol per line

The previous pass's concentration backtest (immediately above) showed buying fewer than the
full top-12 basket underperforms — so showing each symbol's score in the digest was actively
counterproductive: it invited exactly the cherry-picking that data argues against. Removed
entirely. `_build_digest()` no longer takes a `scores` parameter; `_score_pct`, `_ranked`, and
`_fmt_scored_list` are deleted.

SELL/BUY/HOLD and the "Full basket" section are now:
- **One symbol per line**, not space-joined on one line — the formatting the trader asked for.
- **Alphabetical, not score-ranked** — `rank_universe()` itself still returns `target` in
  score order internally (that is how the top-12 gets picked), but the DIGEST re-sorts
  alphabetically before printing, so no implicit "buy these first" ordering leaks through
  either. All 12 names are presented as equally mandatory.
- The digest's advisory line changed from "Capital-constrained? Buy top-down by score" to
  "Buy ALL names below, equal weight - this strategy was tested as a full basket. Buying only
  a few underperformed badly in backtest, see STRATEGY-ANALYSIS.md" — same spot, opposite
  message, now backed by the concentration-backtest evidence instead of contradicting it.

### Angel-for-live-data: investigated, NOT implemented — real architecture blocker

User's ask: since Historify's backfill used Angel data, should the live weekly digest also
fetch through Angel instead of Flattrade (motivated by two live fetch failures seen in the
dry run above). Traced this before writing any code and found a hard blocker:

- `database.token_db.get_token(symbol, exchange)` — used by every broker's `get_history()`,
  including `broker/angel/api/data.py` — has **no broker parameter**. `SymToken`
  (`database/symbol.py`) has no `broker` column either: it is ONE shared table holding
  whichever broker's master contract was most recently downloaded onto this instance.
- This instance's live trading broker is Flattrade, so `SymToken` currently holds Flattrade's
  tokens. Calling Angel's `get_history()` today would resolve each symbol through
  Flattrade's token, not Angel's own - wrong/garbage data, not actually "Angel data."
  Making it correct means re-downloading Angel's OWN master contract into that shared table,
  which **overwrites Flattrade's** and breaks live Flattrade order/symbol resolution until
  Flattrade re-authenticates.
- This is exactly the constraint `signal_engine/backtest/broker_login.py`'s own docstring
  already documents and works around for backfilling: it logs into a second broker directly
  (never through `auth_db`/`upsert_auth`) and keeps the token in-process only, specifically
  so `SymToken`/the live session are never touched. Backfilling is a manual, offline,
  occasional operation; the momentum-rank digest is a recurring LIVE check that has to
  coexist with live Flattrade trading every time it runs - the risk profile is not the same.

**Presented this to the user with four options** (fix the actual observed errors / temporarily
swap the master contract each run / stand up a second Angel-only OpenAlgo instance / leave
as-is). **Chosen: fix the actual observed errors, no broker switch** — lowest risk, and
targets what was actually broken rather than a hypothesized fix.

### What was actually wrong, and what was fixed

The 2026-09-16 dry run's 4 excluded symbols were three different things, not one problem:

| symbol | cause | fix |
|---|---|---|
| `SAGILITY` | non-positive close (bad tick) | already handled - the corruption guard from two passes ago |
| `NIFTYFPI` | not a real NSE-equity symbol in this instance's master at all | **removed from `UNIVERSE`** - permanent failure, retrying cannot help |
| `GVT&D`, `M&M` | Flattrade HTTP 500, "jData is not valid json object" | **retry added** - re-verified live: both fail IDENTICALLY on the second attempt, confirming this is a deterministic Flattrade-side bug, not a transient blip. Still correctly excluded and surfaced in the "Data note," now with confirmed evidence it is not a fluke worth chasing further here.

**`_fetch_symbol_history()`** (new) wraps the broker call with up to `_FETCH_ATTEMPTS = 2`
tries, 1.5s apart, distinguishing "genuinely no data" (empty response, not retried, not an
error - e.g. a brand-new listing) from "fetch failed" (retried, counted as `fetch_errors`
only if every attempt fails). `NIFTYFPI` was also removed only from this live script's
`UNIVERSE`, deliberately NOT from `signal_engine/backtest/data.py`'s `NSE_FNO` - that constant
is shared by other backtests, and whether Historify has usable NIFTYFPI history is a separate
question from whether this LIVE script's broker can trade it today. This is now a known,
intentional 1-symbol difference between the two lists (previously verified identical in an
earlier pass).

**Re-verified live after all fixes** (fresh dry run, same scratch-state technique as before):
universe now 210 (was 211), `NIFTYFPI` no longer attempted, `GVT&D`/`M&M` show "after 2
attempt(s)" in the log confirming the retry actually ran, `SAGILITY` still correctly excluded,
digest renders score-free and one-per-line, `NO ACTION NEEDED` still correctly returned since
the real held basket still ranks in the top 12.

**Tests:** 4 new (`TestFetchSymbolHistory` x3, `TestUniverse` x1), 2 rewritten for the new
digest format. **41/41 passing.**

### Status

Shipped to `strategies/examples/momentum_rank_strategy.py` and the synced deployed copy.
Angel-as-data-source remains an open, deliberately-declined option - revisit only via one of
the three alternatives above (fix targeted errors [done], temporary master-contract swap, or
a dedicated second instance), never a silent live switch. Strategy remains **candidate,
paper-tracked**, shipped top-12 config unchanged.

---

## 2026-09-17 — Real rebalance sent to Telegram; ₹1,00,000 paper-trading ledger built

### Rebalance #1 sent to the real channel

Reconstructed and sent rebalance #1's digest (2026-09-16, the actual first live rebalance -
`ADANIENSOL, ADANIPOWER, ATHERENERG, BHARATFORG, BHEL, CUMMINSIND, LAURUSLABS, NATIONALUM,
PAYTM, POWERINDIA, RADICO, SONACOMS`, buy-only, no prior holding) to the real
`positional-momentum-rank-live` Telegram channel, using the current (post-fix) digest format.

**Mid-send bug found and fixed:** the user spotted that every symbol appeared twice - once
under `BUY` and again under a separate `Full basket` section. Root cause: `BUY ∪ HOLD` is
always exactly equal to the target basket with zero overlap (by construction of
`diff_basket()`), so a third "Full basket" listing was pure duplication, not new information.
**Fixed:** removed the "Full basket" section entirely from `_build_digest()` - `SELL`/`BUY`/
`HOLD` alone now fully and non-redundantly specify the complete instruction. Two tests
updated, one new (`test_no_duplicate_full_basket_section`, asserts every symbol appears
exactly once in the whole digest). Resent the corrected message to the channel.

### Paper-trading ledger: ₹1,00,000, equal-weight, tracked automatically going forward

Built `strategies/examples/momentum_rank_paper_tracker.py` - a standalone ledger that records
what actually following the digest would have earned, without ever placing a real order.
Methodology mirrors the digest's own instructions exactly, not a fresh backtest: HOLD names
are left untouched (no re-weighting trade), only SELL/BUY names actually trade, and a new BUY
is sized off the *current* total book value / `TOP_N` (cash + mark-to-market of names staying
held), not off the original capital - so a new entry after some drift still targets a genuine
equal-weight slot of the book as it now stands.

**Initialized with real data.** 2026-09-17 (today) is exactly the "next session open" fill
day for the 2026-09-16 signal per this doc's own documented fill convention, so the initial
buy used REAL fetched opening prices, not a synthetic estimate.

**Immediate finding: `POWERINDIA` is completely unfunded at ₹1,00,000 capital.** Per-slot
allocation is ₹1,00,000 / 12 = ₹8,333.33; `POWERINDIA` traded at ₹30,435 that morning -
more than 3.6x the slot size, so it bought **zero shares**, not just an underweight position.
This is a real, structural constraint of running this strategy at this capital level with
this universe (which includes several high-priced names - `CUMMINSIND` ~₹4,940,
`RADICO` ~₹4,450 also only afford 1 share each), not a bug: equal-weight-by-value assumes
capital divides continuously, but real CNC orders are whole shares only. `_paper_initial_buy()`
/`_paper_apply_rebalance()` now print a `WARNING` whenever a buy allocation can't afford even
one share, so this is never silently invisible in a ledger that shows a clean ₹0.00 P&L line
for a position that was never actually established. **Flag for later:** at this capital, the
paper (and any real) portfolio is effectively ~11 stocks, not 12, until capital grows past
~₹30k/slot (~₹3.65L total) or `POWERINDIA` drops out of the ranking.

Result after the initial buy (2026-09-17 open prices): ₹100,000 capital, ₹20,133.46 left as
cash (large cash drag - a direct consequence of whole-share rounding across several
high-priced names plus the unfunded `POWERINDIA` slot), 12 positions opened (11 funded).

### Automatic tracking wired into the live script

The user asked for a concise paper P&L summary on every future rebalance, automatically -
not a separate manual step. Since the `/python` host accepts only one uploaded file, the
ledger math is **duplicated inline** into `momentum_rank_strategy.py` itself
(`_paper_initial_buy`/`_paper_apply_rebalance`/`_paper_summary`/`_paper_summary_lines`),
same reason `momentum_score`/`build_factor` are already duplicated rather than imported -
covered by a numerical-parity test against the standalone tracker
(`test_matches_standalone_paper_tracker_numerically`), so the two can never silently diverge.

**One deliberate difference from the standalone tracker's fill convention:** the inline
version fills at the rebalance signal day's CLOSE (`last_price`, already fetched during
ranking) rather than waiting for the next session's real open. This doc's own 2026-09-12
verification note already found that substitution changes the *backtest* CAGR by only
0.24 points (37.67% -> 37.91%) - a negligible difference accepted here in exchange for not
needing a second, later run just to capture "tomorrow's" price on a script that only checks
weekly. The standalone tracker keeps the more precise next-open convention for its own
manual `--report`/backfill use.

`run_once()` now: loads the paper ledger, computes fill prices for every symbol being traded
(falling back to a symbol's last recorded entry price - clearly logged - if a fresh price
failed to fetch this run, rather than crashing the rebalance over one stale price), applies
the rebalance, appends a concise block to the digest -

```
--------------------------------------------
PAPER PORTFOLIO (Rs 100,000 tracked since first rebalance):
  Value: Rs 100,000 (+0.00%) | Realized: Rs +0 | Unrealized: Rs +0
  Positions: 12 open, 0 closed (see momentum_rank_paper_tracker.py --report for per-symbol detail)
```

- and only persists the paper ledger update after a successful Telegram send, mirroring the
existing state-save safety pattern exactly (so a failed send never silently marks a paper
trade as booked).

**Tests:** 4 new (`TestPaperLedger`, including the parity check), all in
`test_momentum_rank_core.py`; `test_momentum_rank_paper_tracker.py` (13 tests) covers the
standalone module's own math independently. **55/55 passing** across the full suite.

### Status

Shipped to `strategies/examples/momentum_rank_strategy.py`, the synced deployed copy, and the
new `strategies/examples/momentum_rank_paper_tracker.py` (+ synced copy in `strategies/
scripts/`). Real ledger at `log/strategies/momentum_rank_paper_ledger.json` - not gitignored
by `log/strategies/.gitignore` (only `*.log` is), but deliberately never committed anyway:
it's live per-instance runtime state (current real holdings, real P&L), and committing it
would mean every future `git pull` on ANY OpenAlgo instance overwrites that instance's own
live paper-tracking with whatever this instance last committed. Same reasoning already
applied to `momentum_rank_state.json`. Strategy remains **candidate, paper-tracked**; this
pass adds the actual paper-tracking infrastructure the "paper-tracked"
label has been asserting since the very first entry in this document.

---

## 2026-09-17 (follow-up) — Weekly paper P&L check-in, decided and shipped

**Question:** the rebalance-triggered paper summary above only fires every ~30 sessions
(~6 weeks) - the user asked for regular position/P&L visibility and asked explicitly to
decide the cadence (daily/weekly/monthly).

**Decision: weekly.** Daily was rejected - this is a positional strategy with no intraday
exit, so nothing actionable happens between rebalances; a daily ping would be pure noise for
a book that cannot move on any daily decision. Monthly was rejected as too sparse - only 1-2
check-ins per 6-week rebalance cycle. Weekly matches the cadence this script already checks
at, gives ~4-5 data points per cycle, and is standard practice for monitoring a multi-week
swing book.

**Implementation, self-enforced rather than schedule-assumed.** The interval
(`PAPER_CHECKIN_INTERVAL_DAYS = 7`) is tracked by the script itself via a new
`last_checkin_date` field in the paper ledger, not just inferred from "whatever the host
schedule happens to run at" - the same lesson as the earlier "checked once daily" text bug
that didn't match the real (weekly) schedule. `run_once()`'s existing non-rebalance branch
(previously a bare `return`) now calls `_maybe_send_paper_checkin()`, which no-ops silently
if no paper position exists yet, no-ops (with a log line) if fewer than 7 days have passed
since the last check-in, and otherwise sends the full per-symbol position + P&L breakdown -
deliberately more detailed than the concise block appended to a rebalance digest, since a
check-in's entire purpose is that detail:

```
MOMENTUM-RANK PAPER CHECK-IN - 2026-09-17
============================================
Not a rebalance day (1/30 sessions since last rebalance) - this is a position/P&L snapshot only.

Capital: Rs 100,000  |  Value: Rs 100,568 (+0.57%)
Realized: Rs +0  |  Unrealized: Rs +568  |  Cash: Rs 20,133
Positions: 12 open, 0 closed
--------------------------------------------
  ADANIENSOL: qty 6 @ entry Rs 1,346.00 -> cur Rs 1,348.10  (+0.16%)
  ...
--------------------------------------------
This is a PAPER-TRACKING CHECK-IN ONLY - no order has been placed.
```

Sent once immediately to the real channel (2026-09-17, +0.57% one day after the initial buy -
too early to mean anything, this was to demonstrate/verify the feature works end-to-end, not
a performance claim). `last_checkin_date` now recorded, so the live weekly schedule will not
send again until 7 days have passed.

**Tests:** 6 new (`TestPaperCheckin`) covering the message format, the not-due-yet skip, the
no-positions-yet skip, the dry-run no-send-no-save path, and the failed-send-leaves-state-
unchanged path (mirrors the existing state/paper-ledger safety pattern). **61/61 passing.**

### Status

Shipped to `strategies/examples/momentum_rank_strategy.py` and the synced deployed copy.
Committed to git this session (see commit history) - the real ledger/state JSON files under
`log/strategies/` are deliberately excluded from that commit regardless (live per-instance
runtime state, not source - see the note two entries above).
