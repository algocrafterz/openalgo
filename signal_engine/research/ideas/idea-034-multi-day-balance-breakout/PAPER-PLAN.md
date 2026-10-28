# IDEA-034 v3 - forward paper plan (frozen 2026-10-28)

v3 = the v2 primary rules with a limit entry at the broken balance edge (the limit-order study's
"L1 15m" variant). It is recorded forward by a separate process that places no orders: every
eligible signal is written to its own ledger, the limit fill and the exits are modelled on the
day's 1-minute bars, and the running engine, its strategies and their risk slots are untouched.

These rules and the acceptance criteria below were written before any hold-out session existed.
Nothing in this file may change during the test. A change is a new version with a new freeze
date, and its hold-out starts again from zero.

## Frozen rules

- **Universe:** the 206 symbols in `paper/universe.txt`, copied from the v1 run's
  `runs/2026-10-28-multi-day-balance-breakout-v1/execution/universe.parquet`.
- **Balance:** the last 5 completed sessions. Each session's 70 percent value area comes from 5m
  bars (bin 0.05 percent of the session mean close, sessions with fewer than 60 continuous bars
  skipped). Adjacent value areas overlap by at least 0.5 of the narrower one. Composite height
  H = max(VAH) - min(VAL) is at most 1.5 x daily ATR14. H / broken edge x 100 is at least 1.5.
- **Trigger:** a 30-minute period close (periods anchored at 09:15, closes 10:15 to 12:45) above
  the composite high while the previous 30-minute close was at or below it. A short mirrors this
  below the composite low. First breakout per stock per day only.
- **Entry:** a LIMIT order at the broken composite edge (buy at the composite high, sell at the
  composite low), live from the signal close for 15 minutes. It fills only if a 1m bar trades at
  least one tick THROUGH the limit; a touch does not fill. The fill is at the limit price (at the
  bar open when that open is already through the limit), with zero entry slippage. An unfilled
  order is no trade and counts as 0 per signal. If the fill bar also reaches the stop, the trade
  is stopped on that bar. A fill already at or beyond the stop (a bar that opens through both)
  exits flat at the fill price with the exit slippage charged (reason SL_GAP_ENTRY); the
  study booked that rare case at the stop price, a gain that could not happen.
- **Stop:** edge -/+ 0.25 x ATR14, a market exit with 4 bps slippage.
- **Target:** edge +/- H, a market exit with 4 bps slippage (the resting-target variant is not used).
- **Time exit:** 14:45, a market exit with 4 bps slippage.
- **Costs:** the Flattrade intraday statutory model (`portfolio.costs.india_intraday`, broker
  flattrade) plus 4 bps slippage on every market leg. A stress figure at 8 bps per market leg is
  reported.
- **Size:** Rs 1,00,000 notional per signal.
- **IB%:** recorded for every signal as information only (`features.ib_width.ib_pct_at` at the
  signal time). It is never a filter.
- **Hold-out:** only sessions from 2026-10-29 onward, the first session after the freeze commit.
  No earlier year may be cited as prospective evidence: every year before the freeze was already
  examined by v1, v2 or the limit-order study.
- **Capacity:** at most 10 modelled positions at once. A slot is held from the signal close: by a
  live limit until it fills or expires, then by the position until it exits. A signal that
  arrives with 10 slots held is recorded as CAPACITY_SKIP and counts as 0 per signal.
- **A session must exist to be traded:** a session needs at least 60 continuous 5m bars to enter
  the daily table, as in the backtest. No signal value depends on a later bar (tested), but the
  recorder runs after the close on complete data, not intraday.

## What is recorded

`signal_engine/data/idea034_paper.db` (gitignored, written only by the recorder):

- `days`: one row per session: OK or DATA_GAP, symbols with complete data, signal count, the
  code fingerprint and a hash of the day's bars, and whether `--force` replaced an earlier row.
- `signals`: one row per ELIGIBLE signal, filled or not: direction, signal time, edge, H, H%,
  ATR, limit, stop, target, FILLED / UNFILLED / CAPACITY_SKIP, fill and exit time and price,
  exit reason, gross, statutory cost, total cost, net, stress net, per-signal net (0 when not
  filled), the market-entry counterfactual for the same signal (gross, cost, net, exit reason)
  and IB% as information.

A session is DATA_GAP when fewer than 90 percent of the universe has 5m bars through 14:40. A day
already recorded is never overwritten without `--force`, which is logged and flagged in `days`.
A DATA_GAP day holds no result, so it is recorded again without `--force` once the data is
complete. The CLI refuses a future date.

## Acceptance criteria (fixed before the first hold-out session)

The backtest reference is the limit-order study's v2 replay under these rules: 1,319 signals,
fill rate 40.9 percent, +2.92 bps per signal, +7.12 bps per filled trade (`paper/reference.json`,
built by `paper/build_reference.py`; 20,000 seeded bootstrap paths per signal count).

| Signals | Per-signal mean, 5th pct | 95th pct | Drawdown 95th pct | Drawdown 99th pct |
|---|---|---|---|---|
| 50 | -11.40 bps | +19.24 | 734 bps (Rs 7,339) | 948 bps (Rs 9,478) |
| 100 | -7.51 | +14.29 | 1,057 (Rs 10,572) | 1,369 (Rs 13,685) |
| 200 | -4.65 | +11.00 | 1,463 (Rs 14,633) | 1,878 (Rs 18,780) |

Drawdown is of the cumulative per-signal net, in bps of one Rs 1,00,000 notional (1 bps = Rs 10).
The report reads the band for the largest grid count (25, 50, ... 400) at or below the signals
recorded.

- **Minimum sample before any verdict:** at least 40 sessions (the G7.1 clause for a strategy
  with fewer than one trade per session) AND at least 100 eligible signals AND at least 40 filled
  trades. At the backtest rate (about 11.5 signals a month, about 4.7 fills) that is roughly 9
  months.
- **PASS needs all of:**
  1. net per signal above 0 at the primary cost;
  2. net per filled trade above 0;
  3. fill rate between 30 and 55 percent (the backtest ran 36 to 49 percent a year);
  4. missed-trade impact: limit net per signal at least the market-entry net per signal on the
     same signals;
  5. maximum drawdown no worse than the backtest 95th percentile for that signal count;
  6. net per signal inside the backtest 5th to 95th percentile band (G7.4).
  The stress result at 8 bps per market leg is reported, not required.
- **KILL (stop early), checked once 50 signals exist:** cumulative per-signal net below the
  backtest 5th percentile path (signals x the band's 5th percentile mean), or drawdown beyond the
  99th percentile.
- **PAUSED_DATA:** DATA_GAP on more than 10 percent of sessions pauses the evaluation until the
  data is repaired and the days are re-recorded with `--force`.
- **Status line:** INSUFFICIENT_SAMPLE (fewer than 50 signals), ON_TRACK (50 or more, no kill, the
  minimum not reached), KILL, PAUSED_DATA, PASS or FAIL.
- **What a pass means:** a +3 bps per-signal edge cannot reach t >= 3 in any realistic paper
  period; even 9 months of data cannot prove it statistically. A PASS means only that the
  forward data has not contradicted the backtest. It permits a later, separately approved stage
  with sandbox limit orders, never live trading.
- **Signal parity (G7.2), monthly:** the recorder and the backtest adapter call the same core
  (`strategy_core/balance_breakout.py`; the adapter's output was checked identical before and
  after the move on 40 symbols, 2024 to 2026, five configurations). Each month, run
  `uv run python -m signal_engine.backtest balance_breakout --source historify --period 60d --full`
  and compare its signals with the ledger. Note: the recorded backtest used the published
  dataset (adjusted prices). On 24 sessions from August to October 2026 the recorder found 26 of
  that run's 31 signals plus 4 others (84 percent overlap), a data-source difference, not a code
  difference. Parity is judged on the same Historify bars.

## Monitoring

Run once per session after the evening Historify refresh:

    uv run python -m signal_engine.analysis.idea034_paper --day YYYY-MM-DD
    uv run python -m signal_engine.analysis.idea034_paper --report

Each run logs to `signal_engine/logs/idea034_paper_<day>.log`. The report lists any session since
2026-10-29 with no ledger entry (a missed run); record it with `--day` for that date.

No schedule is installed. Suggested crontab line for the owner to add (Monday to Friday 20:30 IST,
after the refresh; adjust to the refresh's own time):

    30 20 * * 1-5 cd /home/anand/github/openalgo && uv run python -m signal_engine.analysis.idea034_paper --day $(TZ=Asia/Kolkata date +\%F) >> signal_engine/logs/idea034_paper_cron.log 2>&1

The recorder refuses to run when the code, parameters, universe, criteria or reference differ
from `paper/FROZEN.json`. `--unfrozen-dry-run` computes any day and writes nothing.

## How to read the report

- **net per signal** is the headline: every eligible signal, with unfilled ones as 0. **per filled
  trade** is what the trades that happened earned.
- **missed-trade impact** compares with buying at market on the same signals. If the market
  version beats the limit, the cheaper entry is missing too many winners.
- **market net of filled / of missed**: the backtest saw the missed signals as the market's
  winners (+3.4 bps) and the filled ones as its losers (-12.1 bps). The limit wins only because
  each fill starts about 13 bps cheaper.
- **IB%** is shown in buckets for information only. It decides nothing.

## Worked example: TCS, Rs 1,00,000

TCS at about Rs 3,300: about 30 shares per signal.

- The limit order waits at the price where TCS broke out of its balance. About 4 times in 10 it
  comes back within 15 minutes and fills.
- A filled trade (backtest average): about +14.7 bps before costs, roughly Rs 147. Charges are
  about Rs 35 (STT Rs 25, exchange Rs 6, stamp Rs 3, GST and SEBI about Rs 1). The limit entry pays
  no slippage; the market exit pays about Rs 40. About **Rs 71 left per filled trade**.
- Per signal, including the 6 in 10 that never fill and earn nothing: about **Rs 29**.
- The same signals bought at market lose about Rs 21 each in the backtest.
- What the forward test asks: does that Rs 29 per signal, or anything above zero, still show up
  on days nobody has looked at yet?
