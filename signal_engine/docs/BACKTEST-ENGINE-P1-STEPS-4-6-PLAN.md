# Backtest Engine P1 Plan: Steps 4 to 6

Continues `BACKTEST-ENGINE-P1-PLAN.md` (steps 1 to 3, built). Contract: `BACKTEST-LIFECYCLE-AND-RUN-SCHEMA.md`
(wins on conflict) and `research/STRATEGY-ONBOARDING-CHECKLIST.md` (E, F, J). Status lives only in
`BACKTEST-ENGINE-READINESS.md`. Written 2026-10-03 from a read of the engine, data layer and gates.

Goal of the three steps: make every P1 readiness row real, so the readiness cap (EDGE held at MARGINAL,
promotion NOT_READY while rows are unverified) can be lifted by review, not by editing a document.

## Starting point (measured 2026-10-03)

| Area | Today |
| --- | --- |
| Identifiers | None. A trade is (symbol, entry_time, direction). No signal, order, fill or position id |
| Signal, order, fill | Signal is the closed bar `i` (`engine.py` entry call). The order is armed for bar `i+1` and filled at the next bar open. **Slippage is never added to the fill price**: it exists only as the flat `cost_bps` inside `r_net` |
| Skips | Every skip is a silent `continue` (max trades per day, open and cutoff windows, direction rules, risk and stop checks, min stop, pending order dropped at a new session or when a position is open). Post-hoc silent exclusions: position caps, symbols below `min_sessions`, split-day sessions, `_clean_session` row drops |
| Costs | Flat `cost_bps` in the engine. `costs.py` re-costs per trade with the statutory schedule but keeps one number: `portfolio/costs.py` `breakdown()` already separates brokerage, STT, exchange, GST, SEBI and stamp, and is discarded by `_statutory_bps` |
| Calendar | None. Sessions are the union of dates present in the data. Session minutes are constants |
| Data quality | `dataquality.audit_frame` exists as a report tool and is not called per run. `data_quality` in a run is the constant `NOT_RUN` |
| Corporate actions | Heuristic split-day exclusion (open versus previous close outside 0.75 to 1.33), silent. No action table |
| Universe | A hard-coded current F&O list or every symbol Historify holds. **No historical membership source exists anywhere in the repo.** Survivor-biased by the repo's own admission |
| Validity | `report.json` writes a constant `{ok: true, failures: []}`. `verdict.derive` already consumes the shape. No per-run check exists; no look-ahead (truncation) test exists as code |
| Trials | `n_trials` is `--trials` or the default 1. Nothing counts variants |

## Decisions

1. **Fills record the next-bar open, never an invented price.** Slippage is a cost component, not a fill price.
   The ledgers say so in a column comment and the report. Putting slippage into fill prices would change every
   result and is out of scope.
2. **Recorder, not rewrite.** `simulate` and `simulate_swing` gain an optional trailing `rec=None`. Each site is
   guarded by `if rec is not None`. Skips that happen on every bar are plain integer counters, aggregated per
   symbol, per day and per reason; per-signal rejections and dropped orders are rows. Bar indices and integer
   reason codes are stored in the loop and converted to timestamps once per symbol afterwards. The recorder is
   attached only to the single recorded run, never to sweeps, ablations or cost-sensitivity reruns.
   **Invariant (tested):** trades with and without a recorder are identical, and the overhead stays within 15%
   on the benchmark fixture.
3. **Deterministic lineage ids.** `signal_id = <symbol>|<signal bar ISO IST>|<dir>`, `order_id = O|<signal_id>`,
   `fill_id = F|<order_id>|<kind><n>`, `position_id = P|<entry fill_id>`. The trade row carries
   `signal_id, order_id, entry_fill_id, exit_fill_id, position_id`, so lineage is reconstructable from ids alone.
4. **Exclusion taxonomy** (`reason_code`, scope): `MAX_TRADES_DAY`, `OPEN_WINDOW`, `ENTRY_CUTOFF`,
   `DIRECTION_DISABLED`, `DIRECTION_ALREADY_TAKEN`, `RISK_INVALID`, `STOP_WRONG_SIDE`, `STOP_TOO_TIGHT`,
   `POSITION_OPEN`, `NO_NEXT_BAR`, `ORDER_DROPPED_SESSION_END`, `ORDER_DROPPED_POSITION_OPEN`,
   `POSITION_CAP`, `SYMBOL_MIN_SESSIONS`, `SPLIT_DAY`, `BAD_BAR`, `NOT_ELIGIBLE`, `WARMUP`. Unknown reasons are
   a test failure, not a silent bucket. **G0.5 (no silent exclusion) passes only when the ledgers reconcile
   exactly:** every signal is accepted or excluded, every order is filled or dropped with a code, every filled
   entry has a trade.
5. **Cost decomposition is modelled, and says so.** Per trade, the components brokerage, STT, exchange, GST,
   SEBI, stamp and slippage (bps of entry notional, at the deployed notional from `CostModel`) are columns on the
   trades table, and `modeled_cost_bps` is their sum. The headline `cost_bps` stays the engine's flat figure, so
   runs remain comparable with earlier results. A test pins that the components sum to `modeled_cost_bps`.
6. **Calendar is derived from market data, and labelled so.** No official holiday source exists in the repo.
   `calendar_version = historify-derived-<sha8 of the sorted session dates>`, `calendar_source =
   "derived_from_market_data"`. A session counts as a trading day when at least 80% of symbols that have data
   that month traded it. Short or irregular sessions are flagged, not removed. An official calendar can replace
   the source later without changing the manifest shape.
7. **Data quality runs per run, on the frames the run used.** `evidence`-adjacent file `data_quality.json`:
   `status` PASS or WARNING, `affects_validity`, per-symbol audit summary, counts of rows dropped by
   `_clean_session`, the split days excluded and the symbols dropped. A WARNING sets `affects_validity` when
   impossible bars, duplicate timestamps or out-of-hours bars exceed a stated threshold, or when session
   completeness falls below `MIN_SESSION_COMPLETENESS` for more than 2% of symbol-sessions.
8. **Corporate-action policy is written down, not implied.** Recorded in the manifest and `data_quality.json`:
   intraday uses the split-day exclusion heuristic (0.75 to 1.33); daily is back-adjusted for splits; dividends,
   bonuses and rights are NOT detected. The gate G0.8 reports this and does not pass.
9. **Universe: eligibility is data-driven, membership is not point-in-time, and the run says so.**
   Per-date eligibility = enough prior sessions (listing age), traded that date, volume above zero and price at or
   above a floor (all in the resolved execution config, each with a ledger entry). Ineligible symbol-sessions are
   `NOT_ELIGIBLE` exclusions. `execution/universe.parquet` records, per symbol, first and last bar date, sessions
   and the eligibility policy. The manifest records `membership_source = "current_symbol_master"` and
   `survivorship_bias = true`. **G4.7 survivorship therefore reports the bias (it does not pass).**
   The readiness row for point-in-time membership is marked `built` with this limitation and cannot honestly be
   marked `verified` until a historical F&O membership source is acquired: an owner decision, see Open decisions.
10. **Warm-up.** The derived `execution.warmup_bars` (longest indicator period) is applied: bars before warm-up
    in each symbol-session history are `WARMUP` exclusions and cannot produce signals.
11. **Validity checks run at record time** and fill `report.json` `validity.failures` (`{check, detail}`):
    `data_quality` (rule 7), `config` (the frozen intent and profile still validate, including the entry window),
    `lineage` (no fill outside its bar's high-low range, entry after signal, exit after entry, no overlapping
    positions per symbol, ids unique), `truncation` (look-ahead: re-run the engine on data cut at sampled bars for
    3 symbols and require the earlier signals and trades to be unchanged). Any failure makes the verdict
    INCONCLUSIVE and performance gates are not evaluated (rule already in `verdict.derive`).
12. **Trials follow the acceptance-criteria definition.** An append-only `trials.yaml` per idea records one entry
    for each distinct material configuration evaluated: the tuple (rules, params, execution, cost, universe)
    fingerprints. Changes that are NOT trials (dataset, data range, engine, deployment profile changes alone,
    re-runs) add nothing. `external_trials` (variants searched outside the system, with a reason) are added by
    `intake trials add IDEA-n N --reason`. `n_trials = distinct entries + external_trials`; the default of 1 is
    gone, and a run records which parts the number came from. G2.5 passes only when the ledger exists.
13. **The cap is lifted by review, not by code.** Steps 4 to 6 move the P1 rows to `built` with code and test
    locations. Rows become `verified` only after an independent review of that row, recorded in the readiness
    table. The verdict cap and promotion rule read that table (`research/readiness.py`) as today.

## Step 4: lineage ledgers and cost decomposition

New: `backtest/ledgers.py` (recorder, reason codes, id builders, reconciliation check). Changed: `engine.py`,
`swing_engine.py`, `harness.py` (recorder kwarg threaded to the engine), `types.py` (nothing breaking),
`concurrency.py` (position-cap exclusions returned, not silent), `costs.py` (component decomposition),
`trades_io.py` (new columns), `record_run.py` and `run_artifact.py` (ledgers carried in `Execution`, written to
`evidence/`), `evaluation.py` (evaluators below), `manifest.py` (artifacts list).

Evidence files (Parquet, IST timestamps, deterministic bytes):

| File | Columns |
| --- | --- |
| `evidence/signals.parquet` | signal_id, symbol, bar_time, direction, tag, signal_price, stop, target, risk, accepted, exclusion_id |
| `evidence/orders.parquet` | order_id, signal_id, symbol, submit_time, order_type, side, fill_bar_time, status (FILLED, DROPPED) , exclusion_id |
| `evidence/fills.parquet` | fill_id, order_id, position_id, symbol, fill_time, fill_price, side, kind (entry, exit), exit_reason, leg |
| `evidence/exclusions.parquet` | exclusion_id, scope, symbol, date, bar_time, reason_code, detail, count |
| `evidence/trades.parquet` | existing columns plus signal_id, order_id, entry_fill_id, exit_fill_id, position_id and the cost components (decision 5) |

Evaluators added (they exist only because the evidence now does): G0.5, G6.1 session and square-off,
G6.5 gap-through stops (report), G1.6 stop floor (stop-width table from signals). Others stay `not_evaluated`.

Tests: trades identical with and without a recorder for both engines (the existing strategy tests as fixtures);
overhead bound; every exclusion reason code is reachable by a fixture; reconciliation holds and is broken by a
deliberately dropped row; lineage walk from a trade id; components sum to `modeled_cost_bps`; Parquet round trip;
ledgers never attached to sweeps; manifest `artifacts` lists the new files and `verify_artifacts` covers them.

## Step 5: data and session layer

New: `backtest/session.py` (derived calendar, session model, `calendar_version`), `backtest/dq_run.py` (per-run
data-quality report using `dataquality.audit_frame`), `backtest/eligibility.py` (per-date eligibility and
`universe.parquet`). Changed: `data.py` (`_clean_session`, `split_days` and the min-sessions drop report counts
through an optional collector, behaviour unchanged), `harness.py` (eligibility and warm-up exclusions before the
engine), `run_artifact.py` and `manifest.py` (`calendar_version`, session fields, `corporate_action_policy`,
`data_quality.json`, `execution/universe.parquet`, `universe.version`, `membership_source`,
`survivorship_bias`), `evaluation.py` (G0.3, G0.4, G0.6 report, G0.8 report, G4.7 report, G6.2 report).

Manifest fields that were null and listed in `missing_capabilities` get real values: `calendar_version`,
`session.exchange` (NSE), `session.broker_cutoff`, `dataset.corporate_action_policy`, `universe.version`.
`session.force_flat` is filled for CNC as well (null reason recorded: no force-flat for delivery).

Tests: derived calendar on a synthetic month with a holiday and a short session; `calendar_version` changes when a
session date is added and not when bars are appended after `as_of`; collector counts equal the rows actually
dropped; behaviour of `from_historify` unchanged (existing tests); eligibility fixtures (listing age, no trade,
zero volume, price floor); `universe.parquet` schema; data-quality PASS and WARNING cases with `affects_validity`;
manifest has no nulls left that are not legitimately null; reproduction still passes.

## Step 6: gate runner

New: `backtest/validity.py` (the four validity checks of decision 11, returning `{ok, failures}`),
`backtest/truncation.py` (look-ahead check), `research/intake/trials.py` (append-only ledger, counting rule,
CLI `trials`), evaluators G0.2 (truncation), G0.3, G2.5 (from the ledger). Changed: `run_artifact.py` (real
`validity` and `data_quality` replace the constants; trial count from the ledger), `record_run.py` (`--trials`
becomes `--external-trials N --reason`, with a clear error for the old flag), `verdict.py` only if a case is
missing (the rules already exist).

Tests: each validity check fails on a crafted fixture and yields INCONCLUSIVE with performance gates not
evaluated; a deliberately look-ahead strategy fixture fails the truncation check and a causal one passes; the
trial ledger counts the right changes (material changes count, dataset and range changes and re-runs do not);
`n_trials` in the run equals the ledger; the ledger is append-only; G2.5 evaluator; the verdict for a run with
every implemented gate passing and every readiness row verified is EDGE (the cap lifts), and one unverified row
holds it at MARGINAL.

## Build order and checkpoints

| Order | Delivers | Readiness rows touched |
| --- | --- | --- |
| 4 | Ledgers, lineage ids, exclusions, cost decomposition, four evaluators | Trade/order/fill/signal/exclusion ledgers with lineage ids; per-component cost decomposition |
| 5 | Derived calendar and session model, data-quality report, corporate-action policy, eligibility and universe file, warm-up | Exchange calendar and session model; corporate-action policy and `data_quality.json`, warm-up, dataset identity and version; point-in-time universe (built with the stated limitation) |
| 6 | Validity checks, truncation, trial ledger, evaluators G0.2, G0.3, G2.5 | Validity versus performance handling; trial counting |

Each step: tests first, `uv run ruff`, the whole `signal_engine/tests` suite, the core `test/test_backtest_results_*`
files, an independent review, then one commit. Hot-loop changes in step 4 also run the existing strategy
backtest tests unchanged.

## Open decisions for the owner

1. **Historical F&O membership.** No source exists. Options: (a) accept the stated limitation and keep the row
   at `built` (survivorship reported on every run, never passed); (b) acquire historical NSE F&O lists (a data
   task) and add them as the membership source, after which G4.7 can be evaluated and the row verified. Steps
   4 to 6 proceed under (a).
2. **Calendar source.** The derived calendar is a stand-in. An official NSE holiday and special-session list can
   replace it later without a schema change.
