# Dual Momentum (Antonacci) — Strategy Analysis

**File**: `signal_engine/backtest/dual_momentum.py` (CLI), extends
`signal_engine/backtest/portfolio.py` (`PortfolioConfig.require_positive`)
**Type**: Positional, long only, cross-sectional portfolio (same construction as
`pinescripts/swing/momentum-rank/`)
**Status: Adds nothing on this universe/construction.** Not wrong — structurally
unable to bind on a top-decile-of-197 stock selection. `momentum-rank` remains
the sole proven strategy; this is not a replacement or an improvement on it.

---

## Strategy Logic

Gary Antonacci's dual momentum: the SAME 12-1 cross-sectional relative-momentum
ranking already validated for `momentum-rank`, plus one addition — a pick must
ALSO have positive ABSOLUTE momentum (its own trailing return > 0), not just
top-N relative rank. A name that is merely "the best of a falling universe" is
excluded, and that slot sits in CASH (0% that period) instead of being
backfilled by the next-best negative-momentum name.

Implementation: `PortfolioConfig.require_positive` (default `False`, preserving
`momentum-rank`'s exact existing behaviour). When `True`, `PortfolioBacktest.run()`
filters `picks = [s for s in picks if f[s] > 0]` and dilutes the book return by
`cash_frac = (top_n - len(picks)) / top_n`.

---

## Result: the filter never activated

Same methodology as `momentum-rank`'s adopted config (300-day lookback, 21-day
skip, top 12 of the full 197-name F&O universe, 30-session rebalance, 22 bps
cost), full 43 rebalances from 2019-12-03 to 2026-09-13:

**Dual momentum's results were byte-identical to plain relative momentum.**
0% cash held at every single one of 43 rebalances.

### Diagnostic: why

Printed the MINIMUM factor score among the top-12 picks at every rebalance:

```
43 rebalances total
lowest min_pick_score ever seen: 0.5173 on 2025-09-18
rebalances where at least one pick had negative momentum: 0
```

The weakest of the top-12 picks never had a trailing 300-day return below
**+51.7%** — not even through the Feb-Mar 2020 COVID crash (`momentum-rank`'s
own STRATEGY-ANALYSIS.md notes the book was down -29.4% during that crash, yet
still fully invested — consistent with this finding: the crash happened
*between* rebalances, and momentum is a slow, 300-day-lookback signal that
doesn't flip negative from one bad month).

---

## Why the construction doesn't bind here

Antonacci's classic Global Equities Momentum (GEM) applies the absolute filter
at the ASSET-CLASS level: "is the S&P 500's own trailing return positive? If
not, rotate the WHOLE book to bonds/cash." That is a binary, whole-portfolio
gate. What was tested here instead applies the filter PER-STOCK, within an
already-selected top-12-of-197 — i.e., the top ~6% of the universe by momentum.
For a name in that top decile to also fail the absolute-momentum test, the
ENTIRE broad universe would need to be simultaneously deeply negative — which,
on this ten-year NSE F&O window, never happened at any 30-day rebalance
sampling point.

**This is a structural mismatch, not a bug or a failed backtest.** The
per-stock construction and the asset-class construction are genuinely
different strategies that happen to share the name "dual momentum."

---

## Honest limits / follow-up

- **Not tested**: the actual GEM-style construction — gate the WHOLE
  momentum-rank book on NIFTY's (or the F&O universe's) own trailing 12-month
  return, rotating to cash/debt entirely when negative, rather than filtering
  individual stock picks. That is the version that would plausibly deliver the
  "similar returns, less than half the drawdown" result Antonacci's own 40-year
  US backtest reports. Flagged as a genuine follow-up, not attempted this pass.
- **No downside from adopting `require_positive=True` as shipped** — it is
  provably a no-op on this exact universe/period, so it carries zero regression
  risk if ever turned on. It simply has not been shown to help.
- **Regression-safe by construction**: `PortfolioConfig.require_positive`
  defaults to `False`, so `momentum-rank`'s own numbers and `validation.py`'s
  C+D momentum/reversal calibration check are provably unaffected by this
  change (unit-tested in `test_portfolio_dual_momentum.py`).

## Layman summary

The safety rule "don't buy a stock unless it's also winning on its own, not
just relatively" sounds like it should protect you in a crash — but checked
against ten years of real NSE data, it never once triggered, even during
COVID. Your top picks were always up more than 50% on the year by the time a
rebalance came around, so the rule never had anything to catch. This isn't a
wrong idea, it's applied at the wrong level — the real version of this safety
net checks whether the WHOLE MARKET is falling, not whether each individual
pick is, and that version hasn't been tested yet.
