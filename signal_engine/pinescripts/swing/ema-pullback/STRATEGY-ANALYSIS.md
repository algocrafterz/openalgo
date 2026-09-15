# EMA-20 Pullback Continuation - Strategy Analysis

**Source:** [stockkhoj.in](https://stockkhoj.in) - free NSE/BSE screener. Its "Swing" scan
category lists a one-line rule: *"pullback to 20 EMA during uptrend."* No entry trigger,
stop, target, or trend definition is given on the site - this document works out all of
that and turns the one-liner into a testable strategy.

**Status: CANDIDATE. NOT BACKTESTED.** No `.pine` file exists and none may be needed - see
"Why Python-only" below. Blocked on a 10-year Historify daily pull for the F&O universe
(same data path already used for `gap_rsi` and `momentum-rank`). This document is the
pre-backtest design writeup; results get appended here once the run is done.

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

This document will be updated with a Verdict section, in the same format as
`gap-rsi/STRATEGY-ANALYSIS.md`, once that run happens.
