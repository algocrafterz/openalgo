# Engine selection and comparison method

## Decision (evidence from a source walkthrough and a Stage A comparison)
- Keep the existing engine and extend its execution layer. LEAN needs .NET plus embedded Python, data converted to its file layout, symbol-property and factor files, and its India fee model is one hard-coded intraday rate set with no partial fills by default. NautilusTrader v2 is a release candidate with breaking changes per rc, needs a newer Python than the repo pins, has no next-bar-open mode and no NSE calendar. backtesting.py is single-instrument. vectorbt (Apache-2.0 with Commons Clause) is for screening only and is an optional dependency, because the clause conflicts with AGPL redistribution (legal review). backtrader, zipline and qstrader were read for ideas only.
- On the same signals the existing engine and backtesting.py agreed on 99.86% of 65k trades; the differences were target-gap fills (ours fills at the target, the conservative side) and truncated sessions. Agreement shows a shared fill convention, not agreement with broker fills; compare against sandbox or live fills before trusting slippage.
- Deferred by the owner: limit and stop-limit simulation, partial fills, a cash ledger, any framework migration. Revisit only when real strategy requirements need them.

## Method for comparing engines
- Write engine-neutral fixtures as tiny hand-built OHLCV frames in IST (next-open fill, same-bar stop and target, gap through the stop, missing bar, square-off per session, hand-computed costs on unequal legs, determinism by trade hash) and score each engine PASS / FAIL / UNSUPPORTED / NEEDS_WORKAROUND with the numbers.
- Feed both engines one shared signal table so differences are execution only, and say so in the report: it says nothing about the strategy rules.
- Install any copyleft candidate (AGPL, GPL) only in an isolated venv outside the repo, never in the repo environment or its dependency files.
- Load real data with `signal_engine.backtest.data.from_historify` (reads `historify.duckdb`, open it read-only); `from_openalgo` calls the live app over HTTP with an API key.
- Do not draw conclusions about strategy potential from a comparison run on simplified rules; establish execution and cost correctness on validated data first.

## Checking a data gap
- Compare each symbol's session set with `signal_engine/research/reference/nse_calendar.yaml` and with raw 1-minute rows in `market_data` before the loader; classify each missing date as non-trading day, missing in the store, removed by the loader (`drop_split_days`), or symbol-specific.
- Look at other symbols and the index on the same date: a date missing for most symbols with a partial universe stored is a download gap, not a closure.
- Missing sessions distort baskets silently because each symbol has its own session set; record the missing ranges per symbol in the dataset identity and the run artifact.
