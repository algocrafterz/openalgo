# IDEA-034 v3 historical replay (2026-10-29)

Read-only replay of the FROZEN v3 rules on data that already exists. It validates the
implementation and tests robustness. It is **not** independent out-of-sample evidence: almost
every replayed session was examined while the strategy was developed (table below). Nothing here
changes the frozen rules, writes to the recorder's ledger or places an order.

## How it works

`replay.py` calls `analysis.idea034_paper.record_day(day, dry_run=True, ...)` for every session,
the same function the forward recorder runs, so the signal rules, the 15-minute strict
one-tick-through limit fill, stop, target, 14:45 exit, the 10-position capacity cap and the
Flattrade charges plus 4 bps slippage are the frozen code. Only the bar loaders are injected:

| Source | Signals from | Fills from |
| --- | --- | --- |
| `historify` | Historify raw 1m bars resampled to 5m (what the recorder reads) | Historify raw 1m |
| `published` | adjusted published dataset nse-intraday-5m-20261021-002 (the backtest's source) | Historify raw 1m |

The two sources are reported separately and never mixed. For `published`, the signal prices are
adjusted and the fill prices raw, so every signal carries the ratio of the two at the signal
minute; a signal more than two ticks apart is excluded and counted.

`verify.py` shows the replay is the recorder: on sample days, `record_day` with the replay's
year-sliced loader returns rows identical (every field, including the data hash) to `record_day`
with its own per-day Historify loader.

## Missing data

Missing data is never read as "no signal". Per symbol-session the wrapper records NOT_LISTED
(before the symbol's first bar), DATA_GAP (listed but absent or incomplete), HISTORY_GAP (one of
the 5 prior sessions short or missing; the frozen rule then reaches further back, and the signal
is kept but counted). Per signal it records FILL_GAP (fewer than 12 of 15 one-minute bars in the
limit window, or no bar from 14:40); those signals are excluded and counted, not treated as
unfilled. Whole sessions below the recorder's 90 percent coverage rule are the recorder's own
DATA_GAP.

Two runs per source:

- **strict**: the full frozen 206-symbol universe every day, exactly as the recorder would.
  Before about 2021 most sessions fail the 90 percent rule, mainly because 53 of today's 206
  symbols were not listed yet (Historify), or because the published dataset starts many symbols
  later (published).
- **listed** (`--listed-only`): each day the recorder is given only the universe symbols already
  listed that day. This is an input to the frozen code, not a rule change: a symbol with no
  history cannot signal. Listing dates come from the whole store, so a real outage stays a gap.

## Commands (on demand, no scheduler)

```
PYTHONPATH=. uv run python <this dir>/verify.py 2026-10-16 2026-10-01 2025-11-20
PYTHONPATH=. uv run python <this dir>/replay.py --source historify --start 2016-10-01 --end 2026-10-28 [--listed-only]
PYTHONPATH=. uv run python <this dir>/replay.py --source published --start 2017-01-01 --end 2026-10-19 --frames <frames.pkl> [--listed-only]
PYTHONPATH=. uv run python <this dir>/analyze.py [historify published historify_listed published_listed]
```

`frames.pkl` is built by `strategy_library/intraday/breaking-trade/experiments/2026-10-ib-reference-levels/inputs_frames.py`.
Historify must not be write-locked: the OpenAlgo app takes a write lock on `db/historify.duckdb`
while it ingests, and a read-only open then fails. The forward recorder has the same exposure.

## Evidence classification

| Window | Examined during development? | Use |
| --- | --- | --- |
| 2016-10 to 2016-12 (Historify only) | No IDEA-034 work read it | The only unexamined history; a few weeks of signals, too small to decide anything |
| 2017-01-02 to 2023-11-07 | Yes: v1/v2 in-sample, the limit-order study, both IB studies | Implementation and robustness only |
| 2023-11-08 to 2026-10-19 | Yes: v1/v2 out-of-sample, then the limit-order study chose v3's entry on it | Implementation and robustness only |
| Aug to Oct 2026 Historify | Yes: the recorder-vs-backtest parity check | Implementation only |
| 2026-10-20 to 2026-10-28 | Only the 2026-10-27 dry run (0 signals); before the freeze | Pre-freeze, a handful of signals |
| From 2026-10-29 | No | The only independent test (forward hold-out) |

## Results (2026-10-29 run; `analyze.py` reproduces the full tables)

bps of the Rs 1,00,000 notional; per signal counts an unfilled limit as 0; FILL_GAP and
basis-mismatch signals excluded; market = the same signals entered at the next 5m open.

| Run | Signals | Fill rate | Net / signal (t) | Net / fill | Market / signal | Max DD | Recorder status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| historify listed | 1,450 | 40.2% | +0.02 (0.38) | +0.05 | -6.02 | Rs 33,103 | KILL (drawdown beyond the 99th percentile) |
| published listed | 1,009 | 40.6% | +2.32 (1.50) | +5.72 | -2.83 | Rs 20,533 | PAUSED_DATA (13% gap sessions) |
| historify strict | 771 | 40.9% | -0.61 | -1.49 | -6.45 | Rs 20,876 | PAUSED_DATA |
| published strict | 411 | 39.7% | +3.31 | +8.36 | -6.38 | Rs 8,387 | PAUSED_DATA |

- The limit entry beats the market entry on identical signals in every run (by 5 to 10 bps).
  Adverse selection is confirmed: the market net of missed signals is above that of filled ones.
- On the 855 signals both sources share (2017-2026), results agree (+0.9 vs +1.2 bps per signal,
  99.4% same fill outcome). The gap comes from source-only signals: 574 seen only in Historify
  average -1.3 bps, 154 seen only in the published dataset +8.6 bps. The published reference
  (+2.9 bps) therefore leans on sessions the published quality rules keep and raw data does not
  match; the recorder will see the Historify behaviour.
- Historify listed, short +4.2 (t 1.6), long -4.6 (t -2.1); 2021 and 2022 are the worst years.
- The only never-examined window (2016-Q4, Historify) has 17 signals and 2 fills.
