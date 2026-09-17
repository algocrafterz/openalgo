# Index Options Premium Selling (Volatility Risk Premium) — Strategy Analysis

**Status: DOCUMENTED ONLY. Not implemented, not backtested, no `.pine` file, no
Python adapter.** signal_engine currently trades equities/index spot only; this
strategy requires option-chain execution (selling strangles/iron condors) that is
out of scope for the platform today. This document exists so the idea is not lost
and can be picked up if/when options execution is added, without re-doing the
research.

---

## The idea, in plain terms

Every other intraday/swing/positional strategy in this repo makes a **directional**
bet: will NIFTY/BANKNIFTY or a stock go up or down. This strategy makes a
**volatility** bet instead: it sells insurance against a big move, betting that the
market's own pricing of "how much it might move" (implied volatility) is
systematically higher than how much it actually ends up moving (realized
volatility). The seller collects a premium for taking that risk, similar to how an
insurance company prices in a margin above expected payouts.

Mechanically: sell a defined-risk structure (iron condor — sell a strangle, buy a
further-out strangle to cap the loss) on NIFTY/BANKNIFTY, sized for a range-bound
regime, closed before expiry or on a stop-loss/profit target.

## Why this edge should exist (not just "it works")

This is **not** folklore — it is one of the more rigorously documented anomalies in
finance:

- Coval & Shumway (2001) and Bakshi & Kapadia (2003) are peer-reviewed studies
  showing delta-hedged short-volatility positions earn a persistent premium in
  index options markets.
- The mechanism is compensation for tail/crash risk: option sellers are
  underwriting the risk of a sharp move, and the premium they collect is the market
  price of bearing that risk — economically similar to why insurance is priced
  above actuarially "fair" value.
- NIFTY/BANKNIFTY have historically spent a large majority of sessions inside a
  ~1% range, consistent with the premium existing on Indian indices too.

**The same research that proves the premium is real also proves its failure mode**:
losses are concentrated exactly in high-volatility regimes — the years it pays
steadily are punctuated by rare, sharp drawdowns that can erase months of gains in
a single session (Bakshi & Kapadia). This is a strategy that looks excellent right
up until it doesn't.

## Evidence honesty

| Claim | Strength |
|---|---|
| The volatility risk premium exists in index options generally | Strong — multiple peer-reviewed studies, replicated across markets |
| It exists specifically and tradeably on NIFTY/BANKNIFTY | **Not independently verified here** — no rigorous published NSE-specific backtest found. India-specific claims seen during research were retail forum/broker-blog anecdotes (Zerodha TradingQnA threads), not academic-grade evidence |
| Defined-risk (iron condor) sizing survives the tail-risk failure mode | Requires its own backtest against real historical NIFTY/BANKNIFTY option chains — not established here |

## What a real implementation would need (scoping notes for later)

- **Historical options-chain / IV data for NIFTY & BANKNIFTY** — not currently
  available in this platform (Historify stores OHLC bars, not option chains).
  Would need to be sourced before any backtest is possible.
- **Always defined-risk (iron condor), never a naked strangle** — the tail-risk
  failure mode above means an unhedged short strangle can lose far more than any
  single day's premium income; a long option purchased further out caps the loss to
  a known amount, which is what makes this compatible with signal_engine's existing
  portfolio-heat / max-loss risk framework (`risk.py`).
- **Explicit event-day exclusion** — RBI monetary policy days, Union Budget day,
  major election result days, and any other scheduled high-impact event should be
  hard-excluded from entry, since the premium's failure mode is exactly a large
  move on a day like this.
- **Regime awareness** — per the separate regime-detection research (India VIX
  level, ADX/Choppiness), this strategy should be SIZED DOWN or PAUSED when the
  regime read is "high volatility," not sized up — selling more premium into a
  vol spike is the opposite of what the evidence supports.

## Why "document only" rather than build now

The user's current focus is equities/spot; options-chain execution is a
different, larger scope (data sourcing, Greeks, margin/SPAN handling, a different
order-placement path) than any of the three strategies actually being backtested
this pass (IBS mean reversion, Turtle Soup fade, dual momentum). Revisit this
document if/when options execution becomes an active project, rather than starting
implementation from scratch.
