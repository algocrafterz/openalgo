# Backtest execution contract (engine 1.6, cost model 2)

Rules the simulator guarantees. Each is pinned by a test; keep both when changing the engine.

## Fill timing and exits
- A signal on a closed bar fills at the next bar's open. Stop and target are absolute levels from signal time.
- A bar that opens beyond the stop fills at the open (gap through), and is not ambiguous.
- A bar that reaches both stop and target (no gap through the stop) takes the STOP and sets `Trade.exit_ambiguous`. Never resolve toward the target. The run record's `execution_warnings` block reports counts and a labelled upper bound (mean R if every flagged trade had hit the target); the bound is inexact for ladder and dynamic-target trades.

## Missing-bar guard
- An order armed on bar s is due at stamp(s) + (1 + delay) steps. If the bar at the due index has another stamp, the order is dropped with `ENTRY_SKIPPED_MISSING_BAR` and `on_fill_skipped` is called. A hole while a position is open sets `Trade.data_gap` but exits are still evaluated on the next bar.
- Read the step from the data (`_session_step_ns`: shortest in-session step that repeats), never from the median step. The median is the time-exit bar length only; on sparse data it is longer than the real interval and the guard then drops good fills and takes fills across holes. Bars longer than 60 minutes break any `<= 60` filter.
- Test on sparse bars and on an interval above an hour, not only on liquid 5-minute data: a regression check on complete data cannot see this class of defect.
- The swing engine has no missing-bar guard (no cheap trading calendar there).

## Look-ahead guard
- `causal.GuardedCtx` wraps every symbol in `harness.Backtest`, so every normal run, sweep and record passes it and there is no off switch. Bounds per hook: entry, on_bar, custom_exit, on_exit up to the closed bar i; trail and dynamic target up to i-1; on_fill and on_fill_skipped up to i-1 plus Open[i]. Swing `entry_at_open`/`exit_at_open` may also read prepared non-OHLCV columns at i (adapters shift them in `prepare()`).
- `LookAheadError` derives from `RuntimeError`, not `ValueError`: strategy code that catches `ValueError` would otherwise turn a violation into 'no signal'.
- The guard does not cover `prepare()` leaks (centred windows, whole-series statistics); `truncation.py` does, including mid-session cuts. A recorded run with the truncation check skipped records `lookahead_check: not_run` and cannot be promoted (gate G0.2 not evaluated).
- Side paths that run strategy hooks without the guard (live caps, latency, perturbation, cross-sectional baseline, MFE/MAE report) are a known gap.

## Session coverage (stored history has gaps)
- Stored history can lack whole official sessions, and they differ per symbol. A missing FULL session (no bar that day) is a different defect from missing BARS inside a session the symbol has; the names, the record and the docs keep them apart (`session_coverage.MISSING_FULL_DEF`, `PARTIAL_DEF`). Partial-session numbers come from the data-quality audit (`dq_run` summary `session_completeness`), never recounted.
- `record_run._build_data_layer(coverage_policy=...)` compares each symbol with the official calendar (out-of-scope sessions such as Muhurat are not in it; `collector.split_days_dropped` is subtracted) and writes the optional record block `session_coverage`. Thresholds and the basket-size rule live in `research/data_policy.yaml` section `session_coverage` (no in-code defaults). It is a warning only: a run is never rejected for missing sessions, and no verdict or promotion gate reads the class. Classes by `missing_share` = missing full sessions / `expected_sessions_after_exclusions` (the symbol's own first-to-last window, minus out-of-scope and split-excluded sessions), limits inclusive: `COMPLETE` (nothing missing, no partial session), `MINOR_GAPS` (up to 1%), `MATERIAL_GAPS` (over 1% up to 5%), `SEVERE_GAPS` (over 5%). The run class is the worst symbol class; `universe` counts the symbols per class. `MATERIAL_GAPS` and `SEVERE_GAPS` runs carry a trader-language `disclosure` string that the UI shows verbatim.
- Column `session_after_gap` (bool, per bar) marks every bar of the first loaded session after a missing official session, computed last (after the session basis and the eligibility drop, so a session they remove counts as a gap too). Both engines skip NEW entries on such bars with ledger reason `SESSION_AFTER_GAP` (signal scope, with symbol and date). With the column absent nothing changes. `Backtest._ctx`, `perturbation._run_symbol` and `live_caps` carry it across `prepare`; a new path that calls `prepare_for` itself must do the same (`session_coverage.carry_column`).
- Session basis (`session_basis` setting, CLI `--session-basis`): `common` keeps only the sessions every symbol of the run has, `own` leaves frames as loaded; default `common` up to `common_basis_max_symbols`, `own` above, `own` for one symbol. `common` is part of the execution fingerprint (`own` is not, so older fingerprints stay equal). Symbols that list on different dates shrink the `common` period; the block warns when more than the severe share goes.
- Not reset at a gap: rolling indicators that span sessions (EMAs, multi-day ATR). The record states this in `continuity_note`. A swing position held across a missing day is not touched.
- `compare.py` flags two runs as not comparable when their session bases differ, or when the periods are equal but the effective session sets are not.

## Costs (cost model 2)
- Price each trade from its two legs: entry value = sized notional, exit value = notional x exit / entry; long buys at entry and sells at exit, a short is swapped. Apply the brokerage cap per leg by pricing each leg as its own order (`schedule.breakdown(buy, 0, orders=1)` plus `breakdown(0, sell, orders=1)`).
- `portfolio/costs.py` `Charge.amount` applies the per-order cap to the AVERAGE of the two legs when called with `orders=2`. It is shared with live-trade enrichment and the Portfolio Backtester, so do not fix it from a backtest change; report it.
- Slippage stays 2 x slippage_bps_per_side of the entry notional. A trade with a non-finite or non-positive entry or exit raises `CostModelUnavailable`; a probe that needs symmetric legs passes `symmetric_legs=True` explicitly.
- Known gap: a trade closed in several exit fills is priced as one sell order, so brokerage is too low when the cap binds.
- Statutory rates cross-checked against the Zerodha calculator: exchange 0.00307% of turnover, intraday STT 0.025% on the sell leg, stamp 0.003% on the buy leg (0.015% delivery), SEBI Rs 10 per crore, GST 18% on brokerage + exchange + SEBI.

## Changing the contract
- A new ledger reason code is appended at the end of `REASON_CODES` so every existing index is unchanged; the tests that pin the tuple counts grow with it by design.
- A new trade flag is a defaulted field at the end of `Trade` plus a column in `trades_io` (update the pinned column-list tests) and, for a run-level aggregate, an optional record block that old records still validate without (check `build_registry.py`).
- Fixtures and fakes that stand in for `Backtest` need the new methods the funnel calls (for example `lookahead_guard()`).
- Compare old and new engine on identical inputs (trades equal except the new flags), and run the full suite through `signal_engine/scripts/run_full_tests.sh --wait` after the last review fix, not only before it.
