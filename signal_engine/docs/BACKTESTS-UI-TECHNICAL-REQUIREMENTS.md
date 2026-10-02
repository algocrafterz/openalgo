# BACKTESTS-UI-TECHNICAL-REQUIREMENTS

## Purpose

This document defines the technical requirements for the Backtests experience.

The Backtests page must evolve from a simple historical-results viewer into a research interface that connects:

```text
Strategy
  ↓
Immutable Strategy Version
  ↓
Backtest Run
  ↓
Dataset / Execution Context
  ↓
Results
  ↓
Diagnostics / Evidence
  ↓
Verdict
  ↓
Promotion Gate
  ↓
Next Research Action
  ↓
Strategy Evolution / New Run
```

The two primary requirements covered by this document are:

1. **Strategy lineage, backtest-run comparison, and dataset evolution**
2. **Verdict, promotion readiness, decision support, and lightweight glossary**

The implementation should preserve the existing Backtests functionality where possible and extend it incrementally rather than redesigning unrelated engine behavior.

---

# Requirement 1 — Strategy Lineage, Backtest Run Comparison & Dataset Evolution

## 1.1 Objective

The system must allow the researcher to understand exactly what produced a backtest result and how that result relates to previous and future research runs.

The researcher must be able to distinguish:

- strategy changes
- parameter changes
- execution changes
- cost-model changes
- universe changes
- date-range changes
- dataset changes/corrections
- engine/code changes

These changes must not be conflated.

---

## 1.2 Core Domain Objects

### Strategy

A logical strategy identity.

Example:

```text
VWAP Mean Reversion
```

The strategy identity can have multiple immutable versions.

---

### Strategy Version

A frozen definition of a strategy at a particular point in research.

A strategy version contains the material strategy rules and parameters required to reproduce that strategy definition.

Example:

```text
VWAP Mean Reversion
v1
v2
v3
```

### Rule

Once a strategy version has been used by a completed backtest run, it must be immutable.

A material strategy-rule or parameter change must create a new strategy version.

---

### Backtest Run

A single execution of a specific strategy version against a specific research context.

A run must have a unique `run_id`.

A run references:

- strategy
- strategy version
- strategy fingerprint
- dataset identity
- dataset fingerprint
- backtest date range
- universe
- execution configuration
- deployment profile
- cost assumptions
- resolved assumptions
- engine version
- code commit/version
- resulting metrics
- diagnostics
- verdict
- promotion status
- immutable run artifact

One strategy version may have multiple backtest runs.

---

### Dataset Snapshot / Dataset Identity

The exact market-data state used by a run.

Dataset identity must not be represented only by:

```text
start_date + end_date
```

It should include enough information to detect a changed historical dataset.

At minimum the run should preserve:

```text
dataset_id
dataset_fingerprint
data_source
data_as_of / cutoff
adjustment / corporate-action policy
```

Exact fields should follow the existing data architecture.

---

# 1.3 Immutable Run Artifact

Every completed backtest must preserve an immutable research artifact.

Minimum information:

```text
run_id

strategy_id
strategy_version
strategy_fingerprint

dataset_id
dataset_fingerprint

execution_fingerprint
deployment_profile

backtest_start
backtest_end

universe
session

cost assumptions
execution assumptions
resolved assumptions

engine_version
code_commit

metrics
diagnostics
verdict
promotion_status
```

The historical Backtests UI must read from this immutable run state.

A later dataset refresh must not silently modify an existing historical run.

---

# 1.4 Strategy Version vs Backtest Run

The system must explicitly separate strategy versions from runs.

Example:

```text
Strategy: VWAP Mean Reversion

Version v2
  ├── BT-0042  2024-01-01 → 2026-06-30
  ├── BT-0048  2024-01-01 → 2026-09-30
  └── BT-0051  2024-01-01 → 2026-10-31
```

All three runs can use the same strategy version.

A new date range does NOT automatically create a new strategy version.

A material strategy change DOES create a new strategy version.

---

# 1.5 Run Change Classification

When a new run is created, the system should identify the relevant change from the compared run.

Supported classifications should include:

```text
STRATEGY_CHANGE
PARAMETER_CHANGE
DATA_RANGE_CHANGE
DATASET_CHANGE
EXECUTION_CHANGE
COST_CHANGE
UNIVERSE_CHANGE
DEPLOYMENT_PROFILE_CHANGE
ENGINE_CHANGE
RESEARCH_RERUN
```

Example:

```text
BT-0051

Compared with BT-0042:

Strategy: unchanged
Parameters: unchanged
Dataset: unchanged
Date range: extended
Execution: unchanged
Costs: unchanged

Run type:
DATA_RANGE_CHANGE
```

This classification should be visible in the UI.

---

# 1.6 Changing the Backtest Date Range

Changing the backtest date range creates a new run.

Example:

```text
Strategy v2

BT-001
2024-01-01 → 2025-12-31

BT-002
2024-01-01 → 2026-06-30

BT-003
2024-01-01 → 2026-09-30
```

The system must preserve all previous runs.

The new run should identify the reason:

```text
Run reason:
DATA_RANGE_CHANGE
```

---

# 1.7 Historical Dataset Changes

Historical data may be corrected, adjusted, backfilled, or reprocessed.

If the same strategy and date range are run against a materially different dataset state, the new run must reference a different dataset fingerprint.

Example:

```text
BT-0042
Dataset fingerprint: abc123

BT-0050
Same date range
Dataset fingerprint: def456
```

The original run must remain unchanged.

The UI should make the dataset change visible.

Example:

```text
Dataset changed since previous run.

Previous:
abc123

Current:
def456
```

The system must never silently rewrite historical backtest results because the underlying dataset changed.

---

# 1.8 Reproducibility

The system must be able to determine whether a historical run can be reproduced.

The UI should expose a compact reproducibility status.

Example:

```text
Reproducibility

[PASS] Strategy version captured
[PASS] Strategy fingerprint captured
[PASS] Dataset fingerprint captured
[PASS] Execution configuration captured
[PASS] Deployment profile captured
[PASS] Engine version captured
[PASS] Code commit captured
[PASS] Resolved assumptions captured
```

If required information is missing:

```text
Reproducibility: WARNING

Code commit is unavailable.

This run cannot be considered fully reproducible.
```

---

# 1.9 Backtest Scope

Every run must expose a compact Backtest Scope summary.

Example:

```text
Market
NSE Cash Equities

Period
2024-01-01 → 2026-09-30

Universe
NSE liquid-equity universe

Timeframe
5m

Strategy Session
09:15 → 15:00

Execution
Next-bar

Costs
Configured transaction-cost model

Deployment Profile
Flattrade MIS
```

Exchange session, strategy session, and broker/product cutoff must remain separate concepts internally.

A broker-specific operational cutoff must not be represented as the universal exchange close.

---

# 1.10 Strategy Lineage

The system should support a strategy evolution graph.

Example:

```text
VWAP Mean Reversion

v1
 ├── BT-001
 └── BT-002
       |
       v
v2
 ├── BT-003
 ├── BT-004
 └── BT-005
       |
       v
v3
 └── BT-006
```

Branches must be supported.

Example:

```text
v2
 ├── v3a — tighter entry
 └── v3b — wider entry
```

A strategy version should preserve its parent version where lineage exists.

---

# 1.11 Current vs Latest

Do not equate "latest run" with "current research version".

The system should distinguish:

```text
Latest Run
Current Research Version
Promoted Version
```

Example:

```text
Current research version: v3b
Latest run: BT-0087
Promoted version: v2
```

This prevents experimental branches from accidentally becoming the assumed active strategy.

---

# 1.12 Backtests UI — Strategy Details

The Backtests page must show the exact strategy definition associated with the selected run.

At minimum:

- strategy name
- strategy version
- strategy fingerprint
- trading style
- direction
- timeframe
- entry
- exit
- stop loss
- take profit
- position sizing
- filters
- parameters

The user must not need to navigate to another page to understand what generated the result.

---

# 1.13 Backtests UI — Comparison

The user must be able to select a previous/current run comparison.

At minimum show:

### Strategy diff

Which strategy rules or parameters changed.

### Scope diff

Which market, universe, timeframe, session, or date range changed.

### Dataset diff

Whether the dataset identity/fingerprint changed.

### Execution diff

Whether execution assumptions changed.

### Cost diff

Whether cost/slippage assumptions changed.

### Performance diff

Actual metric values and deltas.

Example:

| Metric | Previous | Current | Delta |
|---|---:|---:|---:|
| Net Return | 14.2% | 18.7% | +4.5 pp |
| Max Drawdown | -12.1% | -13.4% | -1.3 pp |
| Profit Factor | 1.21 | 1.29 | +0.08 |
| Trades | 2,184 | 2,641 | +457 |

Do not automatically label one run "better".

---

# 1.14 Overlap-Aware Comparison

When two runs have different date ranges, the UI must warn the user.

Example:

```text
Comparison warning

Previous:
2024-01-01 → 2026-06-30

Current:
2024-01-01 → 2026-09-30

The current run contains an additional 3 months of data.

This is not a pure strategy-version comparison.
```

Where feasible, support:

```text
Full-period comparison
Common-period comparison
Incremental-period analysis
```

This avoids attributing changes caused by newly added market data to a strategy modification.

---

# 1.15 Equity Curve Comparison

Support comparison of:

- equity curves
- drawdown curves
- monthly returns
- rolling performance
- trade distributions

For different date ranges, clearly identify the common period and incremental period.

---

# 1.16 Re-Run With New Data

The user must be able to re-run an existing strategy version against a newer dataset.

Example:

```text
Strategy Version: v2

Previous:
BT-0042
2024-01-01 → 2026-09-30

New:
BT-0051
2024-01-01 → 2026-10-31
```

Strategy version remains `v2`.

Run reason:

```text
DATA_RANGE_CHANGE
```

or another applicable run reason.

---

# Requirement 2 — Verdict, Promotion & Decision Support

## 2.1 Objective

The Backtests page must help the researcher interpret the results and understand whether the evidence is sufficient to move the strategy to the next research stage.

The page must answer:

```text
What happened?
Why did it happen?
How strong is the evidence?
What checks passed or failed?
Is it ready for the next stage?
What should be investigated next?
```

The page must not require the user to infer the decision entirely from raw metrics.

---

# 2.2 Verdict as a First-Class Result

Every completed evaluation should expose a research verdict.

The verdict must be derived from explicit evaluation criteria rather than manually typed.

Example taxonomy:

```text
INCONCLUSIVE
FAIL
MARGINAL
PROMISING
ROBUST
```

The exact taxonomy should remain aligned with the existing evaluation framework.

The important requirement is that every verdict has traceable reasons.

---

# 2.3 Verdict Meaning

## INCONCLUSIVE

The available evidence is insufficient to make a meaningful research decision.

Possible reasons:

- insufficient data
- insufficient trades
- data-quality problems
- unresolved required assumptions
- invalid execution semantics
- missing robustness evidence
- invalid comparison

---

## FAIL

The strategy does not satisfy one or more mandatory research criteria under the tested assumptions.

The UI must show exactly which mandatory criteria failed.

Example:

```text
FAIL

Failed:
[FAIL] Negative expectancy after costs
[FAIL] Maximum drawdown threshold exceeded

Passed:
[PASS] Data quality
[PASS] Minimum trade count
[PASS] Execution model
```

---

## MARGINAL

The strategy has some evidence of an edge, but the evidence is not yet strong enough for promotion.

Possible reasons:

- weak expectancy
- cost sensitivity
- parameter sensitivity
- insufficient OOS evidence
- weak robustness
- regime concentration

---

## PROMISING

The current evidence satisfies the defined research criteria for deeper validation but does not yet imply production readiness.

The next stage may be:

```text
Out-of-Sample
Robustness
Paper Trading
```

depending on the promotion pipeline.

---

## ROBUST

The strategy has satisfied the defined evidence and robustness gates for the relevant research stage.

This must NOT be represented as a guarantee of live profitability.

It means the strategy passed the platform's defined research criteria.

---

# 2.4 Verdict Must Include Reason

Never display only:

```text
VERDICT: MARGINAL
```

Instead:

```text
VERDICT
MARGINAL

Why?

• Positive expectancy before costs
• Positive expectancy after modeled costs
• Adequate trade count
• Parameter sensitivity remains high
• OOS evidence incomplete
• Cost sensitivity remains material
```

The user must be able to drill into the evidence supporting each reason.

---

# 2.5 Promotion Status

The UI must explicitly show whether the strategy can move to the next research stage.

Example:

```text
PROMOTION

Current stage
Backtest

Decision
NOT READY

Required checks
[PASS] Strategy definition resolved
[PASS] Data quality
[PASS] Minimum trade count
[PASS] Cost model
[PASS] Execution model
[FAIL] OOS validation
[FAIL] Robustness
[FAIL] Parameter stability

Next stage
Out-of-Sample Validation
```

When all relevant gates pass:

```text
PROMOTION

Current stage
Backtest

Decision
READY FOR NEXT STAGE

Passed
[PASS] Core performance
[PASS] Cost-adjusted expectancy
[PASS] Minimum trade count
[PASS] Data quality
[PASS] Execution validation
[PASS] Robustness

Next stage
Paper Trading
```

---

# 2.6 Stage-Aware Promotion

Promotion must be stage-aware.

Do not use one generic "GOOD/BAD" status.

Example:

```text
Stage
Backtest

Gate
Core performance

Status
PASS
```

and:

```text
Stage
Robustness

Gate
Parameter stability

Status
NOT RUN
```

and:

```text
Stage
Paper Trading

Status
BLOCKED
```

This makes the research process explicit.

---

# 2.7 Promotion Checklist

The Backtests page should show a compact checklist.

Example:

```text
Promotion Gate

Core Backtest
[PASS] Minimum trades
[PASS] Positive expectancy after costs
[PASS] Acceptable drawdown
[PASS] Valid execution assumptions

Robustness
[PASS] Parameter stability
[FAIL] Regime coverage
[FAIL] OOS validation

Data
[PASS] Data quality
[PASS] Corporate-action handling

Execution
[PASS] Slippage model
[PASS] Broker constraints

Overall
NOT READY
```

Each failed or incomplete item should be expandable to show the underlying evidence.

---

# 2.8 Next Research Action

The page should identify the most relevant next research action.

Example:

```text
Next Action

Run OOS validation.

Reason:
The strategy has positive in-sample expectancy but
OOS evidence has not yet been established.
```

Another example:

```text
Next Action

Investigate transaction-cost sensitivity.

Reason:
After-cost expectancy is close to the break-even threshold
and performance changes materially across cost assumptions.
```

Another:

```text
Next Action

Investigate parameter stability.

Reason:
Performance is concentrated in a narrow parameter region.
```

This is decision support.

It should explain the evidence and missing evidence without making the research decision for the user.

---

# 2.9 Evidence Drill-Down

Every verdict must be traceable.

Example:

```text
Verdict: MARGINAL

Evidence

├── Performance
│   ├── Positive expectancy [PASS]
│   ├── After-cost expectancy [PASS]
│   └── Drawdown threshold [PASS]
│
├── Robustness
│   ├── Parameter stability [FAIL]
│   ├── OOS [FAIL]
│   └── Regime coverage [PASS]
│
└── Execution
    ├── Slippage sensitivity [WARN]
    └── Broker constraints [PASS]
```

The user should be able to navigate:

```text
Verdict
  → Failed Gate
    → Metric
      → Analysis
        → Trade / run evidence
```

---

# 2.10 Performance Metrics

Primary metrics should include, where applicable:

- net return
- CAGR / annualized return
- maximum drawdown
- Sharpe
- Sortino
- number of trades
- win rate
- profit factor
- average trade
- median trade
- total costs
- return after costs
- exposure
- turnover

The UI should distinguish before-cost and after-cost results where applicable.

---

# 2.11 Metric Context

Important metrics should have lightweight explanations through:

- tooltips
- expandable help
- glossary drawer

Do not put long explanations into the main page.

Example:

```text
Profit Factor 1.32  [info]
```

Tooltip:

> Gross profit divided by gross loss. It describes the gross profitability of the trades before considering whether the result is statistically and operationally robust.

---

# 2.12 Data Quality

Show compact data-quality status.

Example:

```text
Data Quality
PASS

Coverage       99.8%
Missing bars   0.2%
Duplicates     0
Invalid OHLC   0
Corporate acts Applied
```

If data quality materially affects validity:

```text
DATA QUALITY
WARNING

17 sessions contain incomplete intraday data.

Impact:
Results for those sessions may be unreliable.

Verdict impact:
Evidence capped at INCONCLUSIVE.
```

---

# 2.13 Assumptions

The user must be able to inspect resolved assumptions.

Example:

```text
Resolved Assumptions

Execution timing
Next bar
Source: User-specified

Slippage
0.05%
Source: Platform default

Intraday cutoff
15:00 IST
Source: Deployment profile
```

Distinguish:

- user-specified
- derived
- platform default
- externally sourced
- unresolved

Material strategy decisions must never be silently invented.

---

# 2.14 UI Information Hierarchy

The primary page should prioritize information in this order.

### Level 1 — Decision

```text
Verdict
Promotion status
Why
Next action
```

### Level 2 — Evidence

```text
Performance
Robustness
Data quality
Execution validity
Promotion checks
```

### Level 3 — Context

```text
Strategy
Version
Parameters
Backtest scope
Dataset
Execution profile
```

### Level 4 — Research History

```text
Previous runs
Comparison
Lineage
Parameter evolution
Dataset evolution
```

### Level 5 — Technical Detail

```text
Trade list
Orders
Signals
Logs
Assumptions
Raw artifacts
```

---

# 2.15 Glossary

Provide a compact "Backtest Glossary" through a help icon, drawer, or modal.

Do not permanently display the glossary on the main page.

Minimum terms:

### Backtest
Simulated execution of a strategy against historical market data.

### Strategy Version
A frozen definition of strategy rules and parameters.

### Backtest Run
One execution of one strategy version against a specific dataset and execution context.

### Dataset
The market data used by the backtest.

### In-Sample
Data used during strategy development or parameter selection.

### Out-of-Sample
Data not used during strategy development or parameter selection.

### Walk-Forward
Repeatedly develop/tune on historical data and test on the following unseen period.

### Expectancy
Average amount the strategy makes or loses per trade under the tested assumptions.

### Profit Factor
Gross profit divided by gross loss.

### Maximum Drawdown
Largest peak-to-trough decline in equity.

### Sharpe Ratio
Risk-adjusted return measure relating return to variability.

### Slippage
Difference between assumed and actual execution price.

### Transaction Costs
Brokerage, exchange fees, taxes, and other modeled trading costs.

### Robustness
Evidence that results do not depend excessively on one narrow parameter or assumption set.

### Parameter Sensitivity
How much performance changes when strategy parameters are varied.

### Regime
A period with different market characteristics, such as trending, ranging, high-volatility, or low-volatility conditions.

### Overfitting
When a strategy becomes too tailored to historical data and fails to generalize to unseen data.

### Verdict
Research assessment generated from defined evaluation criteria and available evidence.

### Promotion Gate
Criteria that must be satisfied before a strategy can move to the next research stage.

---

# 2.16 Good-to-Have

Implement after the initial Backtests experience is stable:

- run filters
- search by strategy/version
- multi-run comparison
- parameter diff
- scope diff
- dataset diff
- run notes
- tags
- reproducibility status
- export evidence
- export comparison report
- saved comparison views
- common-period comparison
- incremental-period analysis

---

# 2.17 Nice-to-Have

Later research UX:

- visual strategy evolution graph
- experiment groups
- parameter evolution charts
- sensitivity visualization
- dataset evolution visualization
- regime-performance matrix
- automatic anomaly detection
- research timeline
- run clustering
- strategy-family comparison
- experiment branching visualization

These must not block the initial implementation.

---

# 2.18 Minimum Data Model

The conceptual model should support:

```text
Strategy
  │
  ├── StrategyVersion v1
  │      ├── BacktestRun BT-001
  │      └── BacktestRun BT-002
  │
  ├── StrategyVersion v2
  │      ├── BacktestRun BT-003
  │      ├── BacktestRun BT-004
  │      └── BacktestRun BT-005
  │
  └── StrategyVersion v3
         └── BacktestRun BT-006
```

Each run should reference:

```text
strategy_id
strategy_version
strategy_fingerprint

dataset_id
dataset_fingerprint

execution_fingerprint
deployment_profile

backtest_start
backtest_end

universe
session

engine_version
code_commit

resolved_assumptions

metrics
diagnostics

verdict
promotion_status

immutable_artifact
```

Exact storage/schema details should follow the existing OpenAlgo/signal-engine architecture where possible.

---

# 2.19 Initial Implementation Phases

## Phase 1 — Foundation

Implement:

- strategy details
- immutable strategy version
- backtest run identity
- dataset identity
- backtest scope
- parameters
- execution/deployment configuration
- immutable run artifact
- verdict
- promotion status
- verdict reasons
- promotion gate evidence

This is the minimum useful decision-support implementation.

---

## Phase 2 — Results and Diagnostics

Add:

- performance metrics
- equity curve
- drawdown
- trade statistics
- costs
- data quality
- robustness results
- promotion checklist

---

## Phase 3 — Comparison

Add:

- previous/current run
- parameter diff
- strategy diff
- scope diff
- dataset diff
- performance comparison
- equity comparison
- common-period comparison
- incremental-period analysis

---

## Phase 4 — Research Evolution

Add:

- strategy lineage
- branches
- experiment groups
- parameter evolution
- dataset evolution
- research timeline

---

# 2.20 Acceptance Criteria

A completed Backtest run must allow the researcher to answer the following without navigating to unrelated areas.

## Strategy

- What strategy was tested?
- Which strategy version?
- What exact parameters were used?
- What were the entry/exit/SL/TP rules?

## Scope

- What market?
- What universe?
- What timeframe?
- What date range?
- What strategy session?
- What execution model?
- What broker/deployment profile?
- What cost/slippage assumptions?

## Results

- What happened?
- What was the after-cost performance?
- What was the drawdown?
- How many trades occurred?
- What are the major performance characteristics?

## Evidence

- Is the data valid?
- Is there sufficient evidence?
- Is the strategy robust?
- Is OOS evidence available?
- Are results sensitive to parameters/costs?

## Decision

- What is the verdict?
- Why did the system reach that verdict?
- Which checks passed?
- Which checks failed?
- Is the run eligible for the next stage?
- What is the next research action?

## Evolution

- What changed from the previous run?
- Was the change strategy, parameter, data, date range, execution, cost, or universe?
- How did the results change?
- Which strategy version produced the run?
- What other strategy branches/runs exist?

## Reproducibility

- Can the exact run be reproduced?
- Which dataset was used?
- Which engine/code version was used?
- Which assumptions were resolved?

---

# 2.21 Product Principle

The Backtests page should follow:

```text
RESULT
  ↓
EVIDENCE
  ↓
VERDICT
  ↓
PROMOTION GATE
  ↓
NEXT RESEARCH ACTION
```

The page should reduce the amount of manual interpretation required from raw metrics while keeping every verdict traceable to explicit evidence and acceptance criteria.

The system should support the researcher's decision; it should not hide the evidence behind an unexplained "good/bad" label.

---

# 3. Engineering Constraints

## 3.1 Preserve Existing Functionality

Do not rewrite unrelated backtest engine functionality solely to support this UI.

Prefer additive changes:

- extend existing run artifacts
- extend existing Backtests read model/API
- preserve existing result compatibility
- introduce new fields incrementally
- migrate old runs where reliable information exists

---

## 3.2 Historical Integrity

Never mutate an existing completed run because:

- strategy definition changed
- strategy parameters changed
- new market data became available
- historical data was corrected
- broker configuration changed
- cost assumptions changed
- engine implementation changed

Create a new strategy version or new run as appropriate.

---

## 3.3 Change Classification Rule

Use this conceptual rule:

```text
Strategy rule / material parameter changed
    → New Strategy Version
    → New Backtest Run

Only date range changed
    → Same Strategy Version
    → New Backtest Run

Dataset state changed
    → Same Strategy Version
    → New Backtest Run
    → New Dataset Identity/Fingerprint

Execution/cost/deployment configuration changed
    → Same Strategy Version
    → New Backtest Run
    → New execution/config fingerprint

Research rerun with otherwise identical inputs
    → New Backtest Run
```

---

# 4. Definition of Done

The initial implementation is complete when:

1. A Backtest run displays the exact strategy version used.
2. The run displays its complete backtest scope.
3. The run identifies its dataset and dataset fingerprint.
4. The run is immutable.
5. Strategy versions are immutable once used.
6. A date-range extension creates a new run, not a new strategy version.
7. Dataset changes are detectable.
8. Previous/current runs can be compared.
9. Comparison distinguishes strategy changes from data/scope/execution changes.
10. Different date ranges generate an overlap warning.
11. The page displays the verdict.
12. The verdict has explicit reasons.
13. Promotion status is stage-aware.
14. Failed/incomplete promotion checks are visible.
15. The page provides a next research action.
16. Verdicts are traceable to underlying evidence.
17. Reproducibility status is visible.
18. A lightweight glossary is accessible without cluttering the primary UI.
19. Existing Backtests functionality remains usable.
20. The implementation does not require the user to infer the complete research decision from raw metrics alone.

---

# 5. Recommended Backtests Page

The final page should conceptually look like:

```text
┌───────────────────────────────────────────────────────────────┐
│ STRATEGY                                                      │
│ VWAP Mean Reversion · v2 · BT-0051                           │
│                                                               │
│ VERDICT: MARGINAL                                             │
│ Promotion: NOT READY                                         │
│                                                               │
│ Why?                                                         │
│ • Positive after-cost expectancy                              │
│ • Adequate trade count                                        │
│ • Parameter stability incomplete                              │
│ • OOS validation incomplete                                   │
│                                                               │
│ Next Action                                                   │
│ Run OOS validation and parameter-stability analysis.         │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│ STRATEGY DEFINITION                                           │
│ Entry · Exit · SL · TP · Position Size · Filters · Parameters│
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│ BACKTEST SCOPE                                                │
│ Market · Universe · Timeframe · Session · Period             │
│ Execution · Costs · Deployment Profile · Dataset             │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│ PERFORMANCE                                                   │
│ Return · Drawdown · Sharpe · PF · Trades · Expectancy        │
│ Equity Curve · Drawdown Curve                                 │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│ EVIDENCE & ROBUSTNESS                                         │
│ Data Quality · OOS · Parameter Stability · Regimes           │
│ Cost Sensitivity · Execution Validation                       │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│ PROMOTION GATE                                                │
│ [PASS] Passed                                                     │
│ [FAIL] Failed                                                     │
│ [WARN] Not Run                                                    │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│ COMPARE                                                       │
│ Previous Run · Parameter Diff · Scope Diff · Dataset Diff    │
│ Performance Diff · Common Period · Incremental Period        │
└───────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────┐
│ STRATEGY EVOLUTION                                            │
│ v1 → v2 → v3                                                  │
│ Branches · Runs · Verdicts                                    │
└───────────────────────────────────────────────────────────────┘

[i Backtest Glossary]
```

This is the target behavior and information hierarchy. Exact visual design is implementation-dependent.
