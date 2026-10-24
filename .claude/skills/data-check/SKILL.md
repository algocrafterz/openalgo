---
name: data-check
description: Read-only market-data health check - Historify coverage and freshness against the NSE calendar, ingestion and backfill status, data-quality rules, and a bar-by-bar comparison of the broker history API against Historify for given symbols and days (missing bars, OHLC and volume mismatches, timestamp basis). Use when asked to "check ingestion status", "is the data fresh", "compare OpenAlgo vs Historify", "missing bars for X", or before trusting a backtest or a runner warm-up.
---

# Market data check

One run: `/data-check [symbols|universe|fno] [YYYY-MM-DD[..YYYY-MM-DD]] [1m|5m|D]`.
Defaults: the 40-symbol paper universe, the previous NSE session, 1m.

**Read-only.** Never run `backfill` without `--status` or `--dry-run`, never write to
`db/historify.duckdb`, never publish a dataset version. A repair is a proposal: give the exact
command and wait for approval. Back up the store before any approved write.

## 1. Coverage and freshness

```bash
PYTHONPATH=. uv run python -m signal_engine.backtest.backfill --status --interval 1m
PYTHONPATH=. uv run python -m signal_engine.backtest.backfill --status --interval D
PYTHONPATH=. uv run python -m signal_engine.backtest.backfill --daily --dry-run     # what the 15:35 cron would fetch
PYTHONPATH=. uv run python -m signal_engine.backtest.dataquality --source historify --interval 1m --symbols <list>
```

Freshness means: the newest stored session of each symbol equals the NSE calendar's previous
session (today's after 15:30 IST), and that session is complete (375 one-minute or 75 five-minute
bars). A stale or incomplete symbol is excluded from the runner warm-up for the day.

## 2. Broker versus Historify comparison

- Historify: `duckdb.connect(str(data.HISTORIFY_DB), read_only=True, config=data._DUCKDB_CONFIG)`
  from `signal_engine.backtest.data`, table `market_data`. If the app holds the lock, report it;
  do not stop the app.
- Broker: OpenAlgo `/api/v1/history` at `OPENALGO_BASE_URL` with `OPENALGO_API_KEY` from
  `signal_engine/.env`, using the shared httpx client with an explicit timeout. OpenAlgo must be running.
- Convert both sides to IST at parse time. Epoch seconds are UTC.
- Compare per symbol and session: bar count, missing and extra bar times, open/high/low/close
  differences above one tick, volume differences, and the price-adjustment basis (a constant ratio
  over a whole span points to corporate-action adjustment, not bad data).

Known defects. Name a defect when it matches; do not report it as new:
- Angel 1m source: 30-day chunk-boundary bug, mixed vendor price basis, 361-bar closing-auction sessions.
- Flattrade 2.0.2.8 floors negative volume to 0 (rule C3 cannot flag it in new data).
- Closing Auction Session (from 2026-08-03): 15:15-15:35 bars can have zero volume.
- Muhurat and special sessions are excluded from trading history.

## 3. Output (chat; write a file only if asked)

```
Data check: <scope>, <interval>, <dates> (as of <HH:MM IST>)

Coverage   <n> symbols, newest session <date>, stale: <list or none>, incomplete: <list or none>
Quality    dataquality result: <pass/fail by rule>
Broker vs Historify
| Symbol | Session | Bars B/H | Missing | OHLC diffs | Volume diffs | Note |
Known defects matched  <list>
New findings           <list with evidence>
Proposed repairs       <exact command, needs approval>

In plain terms
<3-5 sentences>
```
