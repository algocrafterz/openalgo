# Python signal runner: what it is, and what triggers a live trade

Written 2026-09-30. Read with `STRATEGY-ACCEPTANCE-CRITERIA.md` (the gates a strategy must pass
before real capital) and the memory rule that backtest, walk-forward, paper and live all run the
same strategy code.

## 1. Where things stand today

Status 2026-10-07: the bullets below describe 2026-09-30. Since then the runner in `pipeline/`
runs BREAKOUT-PY paper-only (`paper_only: true`, dispatched into analyze mode only) beside the Pine
BREAKOUT. Each strategy has a `signal_source` of `pine` or `python` in `config.yaml` (commit
`330564cbf`). The G7 gates (G7.1 to G7.5) still have no evaluator in `backtest/evaluation.py`.

- **The live trigger is still the Pine alert.** TradingView runs `breakout.pine`, its alert goes to
  Telegram, `signal_engine` reads it, and the risk checks, executor and OpenAlgo take it from there.
- **The Python port feeds backtests only.** `features/` and `strategy_core/` (inputs, BREAKOUT key-level
  engine, exit ladder) are complete and tested, but nothing in a live or paper session calls them.
- **Toggling OpenAlgo from analyze mode to live changes only where orders go** (sandbox or the real
  broker). It does not change where signals come from. If the toggle were flipped today, the trades
  would still be triggered by Pine alerts, exactly as now.
- **There is no live runner yet.** That missing piece is the "Python paper/live runner" below.

## 2. The Python runner

A long-running process that produces the signals itself, from broker data, using the same code as the
backtest, and feeds them into the existing pipeline.

```
Today:   TradingView Pine -> Telegram alert -> listener -> risk checks -> executor -> OpenAlgo
                                (about 12 s lag)

Runner:  broker live bars -> features + strategy_core -> risk checks -> executor -> OpenAlgo
                                      |
                              Ladder manages open positions each bar (same exit model as backtest)
```

Only the signal source changes. Risk checks, executor, stop placement, the entry-slippage guard,
reconciliation and notifications stay as they are.

Parts to build:

1. **Live bar feed.** Closed 5-minute bars from the broker feed, plus the 1-minute bars the value area needs.
2. **Signal step.** At each bar close run `build_breakout_inputs` and `compute_signals` on a rolling
   window and pass any new fire to the existing pipeline.
3. **Exit manager.** Step the shared `Ladder` on each bar from the actual fill. This replaces Pine's
   "TP1 HIT" and "SL HIT" alerts.
4. **Session control.** Start at 09:15, take entries only inside the strategy window, flatten by the
   time exit (14:45) which is before the 15:00 broker square-off.
5. **Bookkeeping.** Dedupe by signal ID, cooldown state, restart recovery, broker reconciliation.
6. **Replay check.** Re-run the day through the backtest and confirm the same signals (gate G7.2).

## 3. What the live trigger will be after deployment

For BREAKOUT, the trigger for a live trade is one event: **a 5-minute bar closes and
`compute_signals` returns a row for that bar.** Concretely:

- **When:** at the close of the bar, for example just after 10:00:00 for the 09:55 bar, plus a few
  seconds of processing. The bar's close is the signal reference price.
- **What qualifies:** one of the 14 setups (value-area, previous-day and initial-balance retests,
  rejections, breaks) with score at least 7, passing the HTF-close, headroom, chase, TP-reach and
  session-volume gates, inside the 09:45 to 11:45 window, and past the cooldown.
- **Entry:** a MARKET order sized by the risk engine, sent immediately, so it fills near the next bar's open.
- **Protection:** an SL-M stop at the core's stop level (level minus or plus 0.35 ATR, at least 0.5 ATR
  or 0.3% from entry), with a trigger rounded to a valid NSE tick (`executor.round_to_tick`). If the fill
  is worse than `bracket.max_entry_slippage_r` (0.5R) the position is closed at once.
- **Exits:** the shared exit model on each bar. TP1 only, or the 30/35/35 ladder with stop moves,
  whichever the acceptance process selects. Anything still open is closed at the 14:45 time exit.
- **Not needed any more:** the Pine trade alerts and the Telegram hop. Pine stays as a chart-only aid.

## 4. Cutover rules

1. **One signal source at a time.** Add an explicit setting (for example `signal_source: pine | python`)
   and refuse to start with both trade sources active. Two sources means duplicate orders.
2. **Disable Pine trade alerts only at cutover,** never before the runner has passed paper incubation.
3. **Paper first, then staged live.** Analyze mode for at least 20 sessions and 30 trades (G7.1), signal
   parity of at least 95% (G7.2), then 25% of intended size, 50%, 100% (G7.5).
4. **Live checks that remain open:** one small real order per tick tier to confirm Flattrade accepts the
   stop triggers (G6.4), and a tested kill switch (G8.1).
5. **Other strategies are unaffected.** ORB, EMA9 and the rest keep using Pine alerts until each is
   ported the same way.

## 5. What the runner achieves, and what it does not

**Achieves:** backtest and live become the same code path; real slippage, fill-timing and rejection data
before capital is at risk; independence from TradingView and its alert lag; and a reusable base, since
any future `strategy_core` module gets paper and live trading without new plumbing.

**Does not achieve:** it creates no edge. Analyze-mode fills are simulated, not real order-book fills.

## 6. Should BREAKOUT be deployed?

Not on current evidence. On the 209-stock, ten-year backtest it fails the economics and significance
gates (net expectancy -0.48R per trade, cost 0.46R per trade, t-statistic about -101), and none of the 13
exit designs changes that. The runner is infrastructure for the next strategy that clears gates G1 to G5,
so entry research comes first and the runner second.

## 7. In plain terms

A paper runner is a practice-trading robot: it watches the live market, decides with exactly the rules we
back-tested, and places pretend orders, so we learn what would really happen before money is at risk. Until
it exists, going live still means "TradingView tells the system when to trade". Building it makes Python the
boss instead, but it cannot make a losing strategy win.
