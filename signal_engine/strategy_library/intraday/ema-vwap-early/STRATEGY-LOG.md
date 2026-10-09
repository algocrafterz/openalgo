# EVWAP-EARLY-PY - Strategy Log

EMA/VWAP/ATR early displacement, trailing stop, no take-profit. Python runner, paper only.
Idea record: `research/ideas/idea-033-ema-vwap-early-trail/idea.md`. Rules:
`strategy_core/ema_vwap_early.py` (one code path for backtest, paper and live).

### 2026-10-09 15:30 - First paper session checked; missed-stop safety net added

- **What changed:** One technical fix, no rule change. On PFC the price reached the starting stop but
  the sandbox did not execute the broker stop order, so the position stayed open until the 14:45 time
  exit and lost about 1.5R instead of 1R. The engine now checks two bars after a stop touch: if the
  position is still open it closes it at market (or, if the broker is already flat, books the stop).
- **Entry:** Not affected.
- **Exit (SL):** Affected only when the broker stop failed to fill: the trade is now closed about 10
  minutes after the touch instead of being held to 14:45. A stop that fills normally is unchanged.
- **Exit (TP):** Not affected. There is still no profit target.
- **Consideration:** The paper sandbox triggers stop orders from sampled prices, so a brief spike to the
  stop can slip past it; a real exchange stop would have filled. The other 7 stopped trades filled within
  about 3 minutes. Nothing was tuned from the day's result.

Session facts (behaviour, not a verdict): 45 signals, 37 traded (8 declined by risk gates, 1 skipped
for a stop under 0.20%). Every entry happened before the crossover, every trigger was a real +/-0.50
crossing with EMA20 aligned, the trailing stop never loosened and only trailed after the crossover.
70% of trades reached the crossover; median entry advantage 0.17R; exits 26 trailing, 8 initial
stop, 3 time; median initial stop 2.0 ATR; 35 SWING / 2 FALLBACK_EXTREME. Realized -1.42R
(-Rs 1,843) before charges. Order latency after the bar close: median 11 s, max 40 s; BREAKOUT-PY
latency unchanged.

NOTE: `pipeline/trail_driver.py` `_missed_stop`: after an INITIAL_STOP touch with the position still
tracked SAFETY_BARS (2) bars later, it asks the broker (`service.make_broker_qty`): flat -> EXIT
tp_level "SL" (main reconciles), still held -> full market EXIT tp_level INITIAL_STOP_SAFETY, unknown
-> no order, retry next bar. Same rule as the Ladder ExitDriver. On the 5m timeframe the check runs in
the cycle of the bar that closes 10 minutes after the touch bar closed (bars are counted by time, so
a skipped cycle does not stretch it). At most one send per bar and MAX_SAFETY_ATTEMPTS (3) per
position. No duplicate close: `main._handle_exit_locked` cancels the SL-M first and, for this label
only, if the cancel fails it re-checks the broker: flat -> book the stop hit (no order); held or
unknown -> abort, the runner retries next bar; the 14:45 time exit (cancel all, then close) is the
backstop. Every other exit label keeps its existing behaviour. The diagnostics record is written when
the position actually closes: `exit_reason` INITIAL_STOP is the strategy event, `stop_touch_bar` the
bar fact, and the execution comes from trades.db (`report.stop_execution`: broker_executed,
safety_market_close, missed_until_time_exit) with minutes from the touch bar to the booked close.
Today: 7 broker_executed (median 3.2 min), 1 missed_until_time_exit (PFC, 210 min). Report fixes: realized R falls back to booked P&L / (qty x risk) when the
close row has no fill (PFC's time-exit row), `r_source` says which; slippage shows mean and
worse/better counts; `exit_mismatches` flags any trade whose driver exit reason disagrees with
trades.db (PFC: INITIAL_STOP vs TIME).

### 2026-10-09 00:30 - Frozen for the first observation run

- **What changed:** Nothing in the trading rules. The strategy is frozen as it stands for the first
  paper session on the same 40 stocks; only record-keeping was added (see NOTE items 13 to 16).
- **Entry:** Not affected.
- **Exit (SL):** Not affected.
- **Exit (TP):** Not affected. There is still no profit target.
- **Consideration:** This session is for watching, not tuning. Nothing about the thresholds, averages,
  stop distance, trailing, filters, stock list, timing or position size changes because of one day's
  profit or loss. Only a technical or operational bug justifies a change.

What to look at after the close (`uv run python -m signal_engine.analysis.evwap_early_report --day
2026-10-09`): MFE, MAE, realized R against the MFE that was available, time to MFE, exit reason and
entry timing first; then fill slippage, the displacement path into the signal, early excursion,
trailing versus initial-stop exits, repeated signals on one stock, ATR regime, and broker-confirmed
versus theoretical fills. Results are split SWING versus FALLBACK_EXTREME and never combined.

Where the paper trades show in the OpenAlgo UI: Order Book and Trade Book (strategy column and
filter), Strategy P&L (`/strategy-pnl`) and its Daily Performance view (`/strategy-pnl/daily`), under
the tag EVWAP-EARLY-PY. These screens key on the strategy tag the executor sends with every entry,
stop and exit order, so no change was needed; BREAKOUT-PY's paper orders already appear there the
same way (analyze mode).

### 2026-10-08 21:00 - First implementation, paper trading from 2026-10-09

- **What changed:** New strategy. It watches how far the 9-bar average sits from the day's average
  traded price (VWAP), measured in units of normal bar size (ATR). It trades when that gap starts to
  close from a large value, before the two lines cross. It runs on the same 40 stocks as BREAKOUT-PY,
  in the sandbox only.
- **Entry:** Long when the gap moves up through -0.50 (still below VWAP but rising); short when it
  moves down through +0.50. The price must also be on the trade's side of the 20-bar average. Signals
  are judged on a finished 5-minute bar between 09:20 and 14:15; the order goes in right after that
  bar closes.
- **Exit (SL):** Starting stop just beyond the most recent confirmed 5-minute swing (0.10 ATR
  buffer), held at the broker. After the lines cross, a trailing stop follows the best price at
  1.0 ATR (or the latest swing, whichever is closer) and only moves in the trade's favour. It tightens
  to 0.75 ATR when momentum fades and to 0.50 ATR if the gap crosses back. It is checked at each
  5-minute close; a touch sends a market exit.
- **Exit (TP):** None. There is no profit target; the trailing stop is the only way a winner exits,
  apart from the 14:45 time exit.
- **Consideration:** The same entry, with a different exit, lost money in the IDEA-032 backtest. This
  paper run checks that the code behaves like the discretionary chart method, not that it makes money.
  Expect many signals (about 60 a day across 40 stocks in a smoke replay) and stops that are often
  wide (median 2.3 ATR).

NOTE: translation assumptions and known deviations from the source.

0. Source: the LipiScript chart study `strategy_library/lipiscripts/intraday/ema-vwap-atr-early-displacement-v2.lipi` (the first version this port was
   written against was an untracked inbox file, since replaced by v2/v3; the early-entry rule is the
   same) and `research/ideas/_inbox/ema-vwap-atr-early-displacement.txt`.
1. Indicators follow the `.lipi`: EMA9 of close, session VWAP of hlc3, Wilder ATR14. Closing auction
   bars (15:15 onwards) are excluded from every indicator, per the repo data contract; the GoCharting
   chart includes them, so late-day values can differ slightly.
2. The first bar of a session cannot signal: its previous displacement belongs to the prior
   session's VWAP (same rule as the IDEA-032 adapter). The `.lipi` compares against yesterday's last bar.
3. Momentum confirmation (ROC > 0 for longs) is always true on the trigger bar itself, because a
   rising crossing of -0.50 implies a rising displacement. Baselines B and C are therefore identical.
   `min_displacement_roc` exists if a stronger ROC is wanted.
4. Chop filter: reject when the last 5 same-session bars cross zero more than once AND the mean
   absolute displacement is below 0.50. At T = 0.50 it practically never binds (the previous bar is
   already at least 0.50 away); it binds at smaller thresholds.
5. Extension filter (max 1.5) cannot bind on an early trigger (the trigger bar sits near -0.5/+0.5);
   it binds for the crossover baseline.
6. Watch markers and reversal diamonds from the `.lipi` are not entries. Reversals are computed with
   the `.lipi` state lock (one event per run) as diagnostics only.
7. Initial stop: latest same-session swing (2 bars each side, confirmed only after the 2 right
   bars), labelled `stop_basis SWING`. With no confirmed loss-side swing (29% of signals in a 20-session
   replay), `stop_basis FALLBACK_EXTREME`: the lowest low (long) / highest high (short) of the preceding
   6 completed same-session bars, excluding the signal bar (fewer bars right after the open), with the
   same 0.10 ATR buffer. Owner decision 2026-10-08: keep the fallback for the paper run and report the
   two populations separately; decide later from evidence whether it stays valid or becomes
   FILTERED_NO_STRUCTURE. Stop/ATR is recorded; stops wider than
   3 ATR are flagged `wide_stop`, never rejected. The validator still rejects stops under 0.20% of
   price (`min_sl_pct`), about 7% of signals in the smoke replay.
8. Trailing stop: hybrid (the closer of 1.0 ATR from the best price and the latest swing - 0.10 ATR),
   activated at the EMA/VWAP crossover. Before activation only the initial stop applies.
9. Touch rule: the stop computed at the close of bar k is in force during bar k+1. An exit is decided
   at a bar close when the bar touched the stop in force during it, or closed beyond the stop just
   computed. The level is not compared with the same bar's own high, because the order of high and
   low inside a 5-minute bar is unknown (look-ahead).
10. Execution: the broker SL-M stays at the initial stop (intrabar). Trailing exits are market orders
    sent after the bar closes (paper: tp_level TRAILING_STOP through main's existing exit path;
    backtest: `custom_exit_fill = next_open`). Moving the broker SL-M each bar is deferred.
11. No take-profit, represented as `tp = 0` ("none"). The validator accepts 0 only for strategies whose
    exit plan has no targets and rejects any other value for them; the bad-fill guard has no target
    to overshoot; the tracker's no-progress, loss-cut and profit-lock rules skip this strategy
    (exit_owner strategy); no TP order is placed. (The first draft used a 20R placeholder; replaced
    the same day, before any paper trade.)
12. No re-entry cap per symbol and direction (`max_entries_per_direction_per_day: null`); the risk
    engine still allows one open position per symbol per strategy.
13. Diagnostics: one JSON record per trade, whatever closed it (TRAILING_STOP, INITIAL_STOP,
    FORCE_EXIT, CLOSED_ELSEWHERE), in `logs/evwap-early-py_diagnostics_<day>.jsonl` and the engine log:
    entry context (signal reason/time, stop and entry-to-swing distance in ATR, stop basis, swing level
    and swing age in bars), MFE/MAE
    before and after the crossover, crossover price, entry advantage, minutes in trade. P&L stays in
    trades.db; `uv run python -m signal_engine.analysis.evwap_early_report --day YYYY-MM-DD` joins
    them on sig_id. Filtered triggers are logged with their reason code.
14. take_profit none is one rule with two encodings (`strategy_core.ema_vwap_early.TAKE_PROFIT`):
    `tp = 0` in the paper/live Signal (validator-enforced) and NaN in the backtest EntrySignal (the
    engine tests `high >= tp`, which 0 would fill at once). A test ties spec, config, adapter, paper
    strategy, trail driver and validator together.
15. Report sections (`analysis/evwap_early_report.py`): A early-entry quality, B continuation after
    the crossover (`post_cross_extension_r`, `early_entry_capture` = entry advantage / MFE after the
    crossover), C stop sanity (incl. SWING vs FALLBACK_EXTREME side by side), D by symbol, half-hour,
    exit reason and stop basis. Owner instruction: no tuning from the first sessions' P&L; this run
    checks behaviour against the discretionary method.
16. Raw research facts per trade (added 2026-10-08, diagnostics only, no decision reads them): the
    bar path from the fill bar to the exit (OHLC, EMA9, EMA20, VWAP, ATR, displacement, ROC, stop in
    force and after, running MFE/MAE in R); the displacement path into the signal (last 6 bars);
    displacement at the crossover, max, min and last bar; bars to MFE and MAE; ATR regime (ATR over its
    ~20-session median); price-to-VWAP / price-to-EMA20 / EMA9-to-VWAP in ATR; signal clustering
    (earlier signals and trades on the symbol that day, previous trade's direction, exit, MFE, minutes
    since its exit); whether the entry fill was broker-confirmed. MAE on an initial-stop bar is
    counted to the stop price (or the open on a gap), not the bar's extreme. Market context (NIFTY,
    VIX, breadth, sector) is not captured yet.

