# Strategy Onboarding Checklist (v4)

**Status: Initial Strategy Onboarding Draft - Ready (frozen 2026-10-02).** No new onboarding
requirements are added to this version. Further changes come only from implementation
findings: something the engine cannot enforce or produce as written. Implementation status is
tracked in `../docs/BACKTEST-ENGINE-READINESS.md`.

What the backtest coordinator needs from you before a strategy enters the pipeline, what the
platform fills in for you, what the engine must record, and what you get back. Gates and
thresholds are owned by `../docs/STRATEGY-ACCEPTANCE-CRITERIA.md`; the machine-checked spec
schema is `ideas/_template/spec.yaml`. This file says what to hand over and what evidence must
exist; it does not restate thresholds.

v4 follows three reviews: `STRATEGY-ONBOARDING-BACKTEST-EVALUATION-REVIEW.md` (evidence
package) and `STRATEGY-ONBOARDING-V2-REVIEW-FEEDBACK.md` (intake friction, MIS/CNC timing) and `STRATEGY-ONBOARDING-V3-REVIEW-FEEDBACK.md` (execution
and schema semantics, no new intake requirements).

Two principles that pull in opposite directions, and both hold:

> Never silently invent a strategy rule that could materially change the strategy's behavior.

> Do not require the strategy author to provide anything the platform can determine reliably
> and consistently.

And the evidence principle: a backtest produces an auditable evidence package, not merely a
performance report.

"Required" in this document means **resolved before the run**, not "the original source must
state it". Sources are usually incomplete (articles, videos, posts, screenshots). Every value
the source did not give is either derived, defaulted by the platform, or confirmed by you, and
every one of those is written to the assumption ledger (section C).

## The four contracts

    Strategy Specification  ->  Execution Contract  ->  Evidence Package  ->  Evaluation / Decision
    (can we implement it       (did we simulate it      (what actually        (is there a robust edge,
     without guessing?)         correctly?)              happened?)            is another iteration
                                                                               justified?)

## A. Intake: how a strategy gets in

### A1. Field classes

Every field has exactly one class.

| Class | Meaning | Who resolves it |
| --- | --- | --- |
| BLOCKER | Changes what the strategy does; cannot be safely guessed | You, or you confirm the coordinator's suggestion |
| DERIVABLE | Unambiguous from what you gave ("20-period EMA of closing price" is `EMA(20, close)`) | Coordinator, recorded |
| DEFAULTABLE | Execution infrastructure, not strategy logic | Platform default, recorded |
| DISCLOSURE | Affects how the result is interpreted, not whether it runs | You state it; it tightens validation, never blocks |
| OPTIONAL | Research metadata | You, if you have it |

### A2. Strategy semantics (BLOCKER unless noted)

- [ ] Entry: every condition with a number, and the price the order fills at
- [ ] Exit: every condition with a number
- [ ] Stop or risk logic: a definition, or the explicit word "none"
- [ ] Take profit: a definition, or "none"
- [ ] Direction: long only, short only, or both. Engine-validated: swing and positional must be long only; a swing or positional spec with shorts is a configuration error, not a run
- [ ] Style: intraday, swing or positional
- [ ] Timeframe: 1m, 5m, 15m, 1h or 1d
- [ ] Holding period: bars, or "same session"
- [ ] Indicators and parameters: name, period, source (DERIVABLE where the text implies it; blocks only when ambiguous, for example "EMA20" with no price given and no sensible default)
- [ ] Signal, order and fill timing, as three separate things. Signal evaluation: bar close, bar open, intrabar or tick. Order submission: for example at the signal close. Fill model: for example next bar open. "Candle closes above EMA20" resolves to evaluate at bar close, submit at signal close, fill at next bar open. BLOCKER only if it cannot be safely inferred; otherwise inferred, recorded, and confirmed only if High impact (see C2). The engine models market data, signal, order, fill, position, trade as distinct steps, which is what keeps look-ahead out
- [ ] Reference prices, only where ambiguous: what price stops, targets, trailing stops, risk and sizing are measured from (entry fill, signal close, signal high or low, previous close, indicator value)
- [ ] Strategy-specific filters and session windows (entry window, no-trade times, event days), or "none"
- [ ] Re-entry: yes or no, and how many per symbol per day. The execution contract can also represent the trigger (after stop, after target, after time exit, same-signal re-entry); these are not asked unless re-entry is on, and default to after stop only
- [ ] Position-sizing intent: risk per trade and capital per trade (these are different numbers)
- [ ] Portfolio mode: `single_position` or `multi_position` (A4)

Vague words ("strong momentum", "clean breakout", "buy after breakout") are not rules. The
coordinator returns the exact open questions, for example: what is a breakout, what price
confirms it, when is the order filled.

### A3. Provenance and disclosure

- [ ] Source: link, PDF, video, screenshot or your own words, saved unchanged in `ideas/_inbox/` (required provenance)
- [ ] Prior experimentation: how many variants you already tried (DISCLOSURE; feeds the trials ledger, under-reporting inflates the result)
- [ ] Chart-peeking: anything you saw on the chart that shaped the rules. This marks the idea "possible in-sample formulation bias" and raises the validation bar; it does not stop the backtest
- [ ] Thesis: why it should make money (OPTIONAL research metadata; not a blocker)
- [ ] Your kill criterion (OPTIONAL)
- [ ] Intended destination: Pine plus TradingView webhook, or Python on the strategy host, and any existing source to port (DISCLOSURE; backtest, paper and live use the same code)

### A4. Portfolio construction

`single_position`: maximum concurrent positions is 1, with an explicit scope:
`global` (one position across the whole universe: RELIANCE open means TCS cannot open) or
`per_symbol` (one position per symbol, many symbols at once). These are different portfolio
models, so the word `single_position` alone is not accepted; if the source does not say, the
scope is a High impact assumption. Ranking, sector and correlation fields do not apply to
`global` and are not asked. A `per_symbol` strategy that can hold many symbols at once is
really `multi_position` and states the fields below.

`multi_position` (a multi-stock strategy that can fire many signals at once) must state:
- [ ] Maximum concurrent positions and maximum trades per day
- [ ] Capital allocated per position and maximum total exposure
- [ ] Signal selection when signals exceed free slots: chronological until full, rank by a named score and take top N, or equal allocation. Deterministic, fixed before the run
- [ ] Conflict rules: symbol already open, opposite signal on an open symbol, capital reserved for open orders
- [ ] Sector or correlation limits, only if the strategy uses them

Without this a backtest silently assumes unlimited capital.

### A5. Strategy-specific execution behavior (only where the strategy depends on it)

- [ ] Order type for entry, stop and target if the source says (default: market entry, stop-market stop)
- [ ] Limit-order expiry and non-fill behavior if limit orders are used
- [ ] Gap-through-stop expectation if different from the default
- [ ] Anything that depends on halts, circuits or illiquid bars

### A6. What the platform supplies (you do not fill these)

| Item | Resolved from | Recorded in |
| --- | --- | --- |
| Exchange calendar, holidays, special sessions | Calendar version | manifest |
| Tick size and price bands | Market data, monthly tier | execution config |
| Circuit limits, halts, order rejection | Market data, broker config | execution config |
| Corporate actions (splits, bonuses, dividends, rights, mergers, symbol and ISIN changes) | Data engine policy | `data_quality.json` |
| Universe membership: which securities belong to the universe on each date (point-in-time) and its source | Platform | manifest, universe |
| Eligibility: which members are tradable for the strategy on each date (listed, not suspended, liquidity, price floor) | Platform policy; you add only strategy-specific limits | execution config, per-date eligibility in the run |
| Delisted, merged and suspended securities | Documented platform policy | `data_quality.json` |
| Broker fee schedule (brokerage, STT, exchange charges, GST, SEBI fee, stamp duty) | Broker, exchange, instrument, product, side, effective date | cost model |
| Default slippage | House default (4 bps per side, market orders) | cost model |
| Warm-up bars | Derived from the longest indicator | execution config |
| Dataset identity and version, timestamp normalization to IST | Platform | manifest |
| Monte Carlo seed, engine version, code commit | Platform | manifest |

You state universe, broker, expected capital and expected notional per trade, and a custom
slippage figure only if you have a reason.

Notes on cost and data (status 2026-10-07):

- Cost scenarios. The primary scenario is the deployment profile broker's statutory per-trade
  model (Flattrade) plus slippage. The primary scenario decides the verdict and every gate. The
  run record also reports `generic_discount_broker`, `flat_<n>bps` and `legacy_flat` as
  comparison lines. See `README.md` in this folder.
- Reference lines. A spec may declare the optional field `costs.reference_round_trip_bps`, a list
  of flat round-trip levels in bps. Each level adds one `flat_<n>bps` line. It never drives a gate.
- Daily dataset version. A daily (swing) idea must pass `--dataset-version nse-daily-20261005-002`
  to `record_run` and to preflight. Without it the newest published version is used, which is now
  the intraday one, and a daily idea then fails preflight as NOT_READY.
- No stop. A strategy with `stop: none` must declare `sizing.basis: fixed_notional` with
  `capital_per_trade` (or `portfolio.capital_per_position`). Otherwise the run is refused before
  execution, because a trade with no stop cannot be sized for costing.

### A7. Session, product and deployment timing

Four different things, never merged:

    Exchange session      resolved from exchange + instrument + date + calendar version
                          (holidays, special sessions and timing changes are the calendar's job)
    Strategy session      strategy-defined: entry window, for example 09:20 to 14:30
    Broker execution cutoff   deployment-profile-defined
    Force-flat cutoff     product, broker and deployment-defined (MIS only)

No universal session is hardcoded, and the exchange session is never redefined to end at
15:00. Closing-auction handling is a data rule (G0.6: value-area and level profiles use
continuous-session bars only), separate from every cutoff above.

The strategy spec, the execution configuration and the **deployment profile** are three
separate inputs. Broker constraints live only in the profile, so the backtest simulates the
selected profile; it does not take the most restrictive cutoff across brokers.

    deployment_profile:
      broker:   flattrade
      market:   NSE/BSE cash equity
      MIS:      force-flat by 15:00 IST. The backtest closes positions itself before that
                (standing time exit 14:45; latest exit bar closes by 14:55, per G6.9) and
                never relies on broker auto-square-off as the exit. The engine records
                the exit convention used.
      CNC:      delivery and carry-forward. Overnight holding allowed. No intraday
                force-flat rule and no MIS cutoff. Any broker-specific restriction comes
                from the profile, not from the strategy form.

A different broker is a different profile. Switching profile is a configuration change that
reruns the affected gates (G9.3), not an edit to the strategy.

The platform generates a **Backtest Scope** summary from the frozen spec, execution
configuration and profile (market, universe, style, direction, timeframe, period, product,
broker profile, force-flat, capital, portfolio mode and scope, slippage) and shows it at the
top of every run and report. It adds no intake requirements.

## B. Intake states

    Research Idea -> Draft -+- missing strategy semantics -> Needs Clarification -> back to Draft
                            |
                            +- resolvable assumptions ----> Assumption Review
                                                                  |
                                                                  v
                                                           Backtest Ready -> Freeze -> Backtest

| State | Meaning |
| --- | --- |
| Draft | An idea exists; one or more BLOCKER fields are undefined ("buy strong breakouts above resistance"). Valid research, not executable |
| Needs Clarification | The coordinator has listed the exact open questions |
| Assumption Review | All BLOCKERs resolved; defaults and inferences are in the ledger for you to look at |
| Backtest Ready | Every field resolved, some from platform defaults rather than the source |
| Backtest Blocked | A missing BLOCKER materially changes the strategy and cannot be inferred |

A source that is merely incomplete moves to Needs Clarification, never straight to reject.
Library classification (separate): complete strategy, incomplete strategy, research idea,
duplicate, obsolete, rejected. Only Backtest Ready strategies enter the queue.

### Intake steps

1. **Submit.** Source, description, entry, exit, stop and target if known, timeframe, market and style.
2. **Extract.** The coordinator sorts every field into known, derived, missing, ambiguous.
3. **Resolve only material ambiguity.** For each, it shows what it detected, what it suggests, the impact, and asks you to confirm. Example: detected "buy when candle closes above EMA20"; suggested "evaluate at bar close, fill at next bar open"; impact High; action: confirm.
4. **Apply platform defaults** (A6), each recorded.
5. **Freeze.** Spec hash, assumption ledger sealed, run id. Only then does a backtest run.

## C. Assumption ledger

Every value not stated by the source is a ledger entry, so nothing is silently invented and
nothing needs a form field.

    id: A-004
    field: signal_evaluation
    value: bar_close
    source: inferred            # user | derived | platform_default | market_data | broker_config | inferred
    confidence: medium
    materiality: high
    reason: source says "close above EMA" with no intrabar wording
    user_confirmed: false

### C1. Materiality

| Level | Examples | Handling |
| --- | --- | --- |
| Low | timestamp format, Monte Carlo seed, file layout | Platform resolves, recorded |
| Medium | default slippage, signal timing where ambiguous | Recorded, shown in Assumption Review |
| High | breakout definition, stop definition, entry trigger, direction, holding period, portfolio selection | Must be resolved and confirmed before freeze |

### C2. Rules
- An inferred value that is High impact needs `user_confirmed: true` before freeze.
- The ledger is stored as `assumptions.json` in the run artifact and summarized in the report, so a reader sees which numbers rest on defaults.
- A result that depends on an unconfirmed Medium assumption says so in its verdict notes.

## D. What the coordinator does

1. **Intake check.** Duplicate search against the registry, classification, state assignment (B).
2. **Spec validation and freeze.** Deterministic check, ledger sealed, strategy hash recorded. Any later change is a new version and a new trial.
3. **Integrity (G0).** Same logic across modes, causality tests, IST timestamps, coverage with gaps named, point-in-time universe, no silent exclusions, corporate actions, closing auction.
4. **Economics and significance (G1, G2).**
5. **Out-of-sample (G3).**
6. **Robustness and risk (G4, G5, G6).**
7. **Evidence package and verdict (E, F).**
8. **Disposition.** Improve (new version), shelve with `revisit_when`, or reject, with a diagnosis (G).
9. **Paper and live.** G7, G8, ramp, G9.

Gates run in order. Two kinds of failure are kept apart:

| Failure | Examples | Handling |
| --- | --- | --- |
| Validity | Look-ahead detected, invalid data, invalid configuration, malformed strategy, impossible execution assumption | Stop the run. The verdict is INCONCLUSIVE, not NO_EDGE |
| Performance | Negative expectancy, weak statistics, poor out-of-sample, cost sensitivity, weak robustness | First HARD fail stops promotion, but selected diagnostics (cost stress, parameter neighbourhood, regime and dispersion tables) still run so the report can say why |

Every variant tried is a trial and is counted, using the definition in
`STRATEGY-ACCEPTANCE-CRITERIA.md` (Definitions). An idea is not retested under a new name.

## E. Evidence package

The immutable run artifact is the source of truth. The Backtests page, CLI, notebooks and
exports read it; none of them is the canonical model.

    runs/<run-id>/
      manifest.json         reproducibility manifest
      assumptions.json      assumption ledger (C)
      data_quality.json     corporate-action policy and events, coverage, gaps, exclusions policy
      strategy/             spec.yaml, strategy hash, strategy version
      execution/            config, cost model, slippage model, point-in-time universe
      evidence/             signals, orders, trades (and positions if needed), exclusions
      analysis/             metrics, regime, robustness, sensitivity, monte-carlo, benchmark
      report/               report.json: verdict, gate scorecard, diagnosis

JSON for metadata and summaries; Parquet for the large signal, order, trade and position
tables.

### E1. Lineage
Every table carries the ids that rebuild the chain
`signal_id -> order_id -> fill_id -> position_id -> trade_id`. Independent tables with no
shared ids do not satisfy this.

### E2. Trade ledger (mandatory)
trade_id, run_id, strategy_version, symbol, side; signal_id, position_id, entry_order_id,
entry_fill_id, exit_order_id, exit_fill_id (so a trade rebuilds without timestamp joins);
signal_timestamp; entry_timestamp, entry_signal_price, entry_fill_price; exit_timestamp,
exit_fill_price, exit_reason; quantity;
initial_risk; gross_pnl, brokerage, statutory_charges, slippage, total_cost, net_pnl;
r_multiple; MAE, MFE; holding_period; market_regime.

### E3. Order and fill ledger (mandatory)
order_id, fill_id, trade_id, symbol, side, order_type; submitted_timestamp; requested and
executed price and quantity; execution_timestamp; status; rejection or cancellation reason.
Proves the stated non-fill, gap and halt rules were applied.

### E4. Signal ledger (mandatory)
signal_id, run_id, strategy_version, timestamp, symbol, direction, signal_state, indicator
values, per-condition results, decision (taken, or skipped and why: no free slot, filter,
already open).

### E5. Position ledger
Required only if the engine supports partial exits, scale in or out, multiple entries or
multiple lots. A one-entry, one-exit engine does not need it (P2).

### E6. Exclusions (mandatory)
Every excluded symbol, day or trade, with reason, period and the rule that caused it.

### E7. Manifest (mandatory)
run_id; strategy id, version, hash; engine version, code commit; dataset id and version,
data_as_of; calendar_version; universe version; deployment profile; backtest parameters;
execution, cost, slippage, sizing and portfolio models; warm-up bars; Monte Carlo seed,
settings and ruin definition; environment; run timestamp; references to the retained
artifacts. The manifest must hold enough immutable identifiers, versions, configuration and
artifact references to reproduce the run from the retained artifacts. A manifest alone is not
enough if the dataset and artifacts it points to are not kept and addressable.

## F. What you get back

### F1. Verdict and scorecard
- [ ] Verdict with the formal meaning below, the primary failed gate and the primary reason
- [ ] Gate scorecard G0 to G9: status, threshold, observed value, evidence. A profile, not one opaque score
- [ ] Flags: survivor-biased universe, in-sample formulation bias, unconfirmed Medium assumptions, missing evidence items (section J)

Verdicts are EDGE, MARGINAL, NO_EDGE and INCONCLUSIVE. Their deterministic definitions live in
`STRATEGY-ACCEPTANCE-CRITERIA.md` (Definitions). In short, MARGINAL is positive evidence with
one or more promotion requirements failed, and the report names which (cost stress, sample
size, parameter stability, dispersion). INCONCLUSIVE is a validity failure or too little
valid evidence, never a synonym for failed.

### F2. Performance
- [ ] Headline: trades, win rate, gross and net expectancy (R and bps), profit factor, Sharpe, max drawdown, breakeven cost
- [ ] Distribution: average winner and loser, largest winner and loser, median R, P10, P25, P75, P90, max consecutive wins and losses
- [ ] Equity and drawdown curves, monthly and yearly returns, underwater duration, time to recovery, drawdown by period
- [ ] Cost decomposition: gross to net P&L by component
- [ ] MAE and MFE distributions read against stop and target placement
- [ ] Exposure and turnover: average and maximum exposure, capital utilization, turnover, average holding period, maximum simultaneous positions

### F3. Out-of-sample
- [ ] In-sample, out-of-sample and walk-forward side by side: trades, net expectancy, return, drawdown, Sharpe or t-stat, profit factor

### F4. Robustness
- [ ] Cost stress at 1.0x, 1.5x, 2.0x, and breakeven cost
- [ ] Parameter neighbourhood for key parameters (lower, selected, upper); the aim is a plateau, not a peak
- [ ] Regime table. Baseline: trend vs range, high vs low volatility. Optional, where the strategy warrants it: India VIX terciles, sector-specific or index-trend regimes. Columns: trades, net P&L, expectancy, win rate, profit factor, drawdown, t-stat, holding period, exposure. A regime filter proposed from it must itself be validated out-of-sample
- [ ] Dispersion by symbol, year, long vs short; top-N and bottom-N contribution; percent of symbols profitable
- [ ] Monte Carlo drawdown distribution with bands, worst and expected drawdown, probability of ruin, time to recovery. The ruin definition (see Definitions in the acceptance criteria) and all simulation assumptions are stored with the result

### F5. Benchmarks (three questions, never merged)
- [ ] Market-relative: against a benchmark that fits the universe (NIFTY 50, NIFTY 500, sector index)
- [ ] Null model: controlled random-entry baseline. Keeps universe, session, number of opportunities, sizing, holding period, exit and stop mechanics, costs and exposure limits; randomizes only entry timing or selection, and preserves the opportunity set: if the strategy can only signal between 09:30 and 11:00, the null never enters at 14:30
- [ ] Edge: the strategy's own net expectancy and significance (G1, G2)

### F6. Data quality
A strong result can mislead if problem observations were quietly removed, so the report
shows this without anyone opening raw artifacts: coverage, missing bars, duplicate bars,
timestamp anomalies, corporate-action events applied, suspensions, unavailable symbols,
excluded observations and their count, and data-quality warnings. Source: `data_quality.json`
and the exclusions ledger.

## G. Diagnosis, evolution and stagnation

### G1. Failure diagnosis
A rejection is never just "rejected":

    Primary failure:    e.g. cost sensitivity
    Secondary failure:  e.g. poor out-of-sample performance
    Evidence:           IS and OOS expectancy, net at 1.0x, 1.5x, 2.0x cost
    Disposition:        reject | shelve | further research

It also says where the evidence points on the path: bad implementation, bad parameters, weak
strategy, promising but incomplete, robust.

### G2. Research state
Kept separate from the verdict, because a strategy can be shelved without being disproven:

    Promising | Needs More Evidence | Weak Evidence | Evidence Against Edge | Shelved | Rejected

### G3. Versions and trials
Each iteration records: version, parent, rule changes, parameter changes, code version,
dataset, trials count (per the trial definition in the acceptance criteria), results, gate status, robustness, decision. Comparing N to N+1 states
what changed, what improved, what deteriorated, whether out-of-sample, cost sensitivity and
robustness improved, and whether complexity grew.

### G4. Stagnation evidence
Recorded, not judged by feel: no net edge across reasonable parameter ranges; edge vanishes
after costs; improvement only by adding complexity; sharp sensitivity to small changes; poor
robustness across symbols, periods or regimes; dependence on few trades or one period;
repeated out-of-sample failure; repeated iterations with no gain. Several together justify
shelving or rejecting. Collecting this evidence is P2; automating the judgement is P3.

## H. Promotion gates

Stages only; every number lives in `STRATEGY-ACCEPTANCE-CRITERIA.md`.

| Stage | Entry | Exit |
| --- | --- | --- |
| Backtest | Backtest Ready, frozen | G0 to G6 acceptance criteria passed |
| Paper | Required backtest verdict and gates passed | G7 and G8 acceptance criteria passed |
| Live | Paper criteria passed | G9 monitoring and retirement rules apply |
| Retired | Per G9 | Recorded in the registry with the reason |

## I. Ground rules

- Broker data is the source of truth. Do not bend it to match TradingView; report the gap.
- All timestamps are IST, converted at the boundary where they enter the code.
- Full, point-in-time universe, never a subset chosen to make a result look better.
- No parameter is tuned on the hold-out. Looking at it consumes it.
- A strategy-logic change after freeze is a new version, a new trial and a STRATEGY-LOG.md entry in plain language.
- Run artifacts are immutable and never overwritten.
- P2 and P3 capabilities do not block basic research unless a particular strategy needs them.

## J. Engine readiness

A run may claim full evidence-package compliance (sections E and F) only when the required
engine capabilities are implemented and verified. Status, code locations, tests and gaps are
tracked in `../docs/BACKTEST-ENGINE-READINESS.md`, not here, so this contract stays stable
while the implementation moves. Until a P1 capability is verified the run record lists it as
missing, and the verdict cannot be EDGE.
