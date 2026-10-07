Status 2026-10-07: SUPERSEDED by the documents in `signal_engine/docs/` (start at `BACKTEST-ENGINE-READINESS.md`). Kept as history.

Below is a cleaned-up and structured version of your backlog. I separated codebase understanding, backtest infrastructure, strategy qualification, strategy lifecycle, data/export, and research hygiene so these become distinct workstreams rather than one large list.
AlgoTrade Backtest System — Development Backlog
1. Codebase Understanding & Documentation
   1.1 Create Claude Codebase Skill File
   Create a dedicated skill file that can be provided to Claude to help it understand and work with the AlgoTrade codebase.
   Scope
- Document the relevant OpenAlgo project structure.
- Document the signal engine module and its implementation.
- Explain how signals flow through the system.
- Explain the relationship between:
    - Strategy definition
    - Signal generation
    - Backtest engine
    - Trade execution simulation
    - Position management
    - Cost/slippage handling
    - Backtest results
    - Benchmarking/evaluation
- Identify important classes, modules, interfaces, and data structures.
- Document conventions and assumptions that Claude must preserve when modifying the codebase.
- Organize the information so Claude can reason about the architecture before making changes.
  Goal: Claude should be able to understand the existing architecture and make changes without repeatedly rediscovering how the codebase works.
2. Backtest Engine & Benchmarking
   2.1 Review Current Backtest Benchmarking Implementation
   Review the current benchmarking implementation for:
- Correctness
- Statistical validity
- Performance
- Computational efficiency
- Reproducibility
- Data quality
- Transaction-cost modelling
- Slippage assumptions
- Benchmark selection
- Edge measurement
- Risk-adjusted performance
- Overfitting protection
  Identify unnecessary computation and opportunities to optimize the backtest pipeline.
  Output:
- Current implementation assessment
- Problems/gaps
- Recommended improvements
- Performance optimizations
- Priority of each improvement
3. Strategy Evaluation Framework
   3.1 Evaluate Strategies Against the Backtest Checklist
   The backtest checklist should become an actual strategy evaluation framework, rather than simply a list of things to check.
   For every strategy candidate, the system should record:
- Which checklist items were evaluated
- Whether each requirement passed/failed
- Relevant measurements
- Evidence supporting the result
- Data/time period used
- Whether the result is statistically reliable
- Known weaknesses
- Remaining areas requiring investigation
  Strategy Quality Score
  Investigate whether a scoring system is useful.
  Rather than having one opaque "strategy score", consider a multi-dimensional evaluation profile, for example:
  Dimension	Example
  Edge	Positive expectancy after realistic costs
  Robustness	Performance across periods/instruments
  Risk	Drawdown, tail losses, risk-adjusted return
  Stability	Consistency across samples
  Cost sensitivity	Degradation under higher costs
  Regime robustness	Performance across market regimes
  Parameter sensitivity	Stability around selected parameters
  Statistical confidence	Sample size / t-stat / uncertainty
  Complexity	Number of assumptions and parameters


A composite score can be added later if useful, but the underlying dimensions should remain visible.
Important: The system should not reduce strategy evaluation to a single number that hides why a strategy passed or failed.
4. Strategy Evolution / Research Lifecycle
   4.1 Track Strategy Evolution
   The backtest system should help answer:
   Is this strategy improving, stagnating, or fundamentally unpromising?

Every meaningful strategy iteration should therefore be traceable.
Track:
- Strategy version
- Rule changes
- Parameter changes
- Dataset/time period
- Number of trades
- Gross edge
- Net edge
- Costs
- Risk metrics
- Robustness metrics
- Benchmark results
- Checklist status
- Improvements/regressions versus previous version
  Strategy Lifecycle
  Potential lifecycle:
  Idea
  ↓
  Candidate
  ↓
  Complete Strategy Specification
  ↓
  Initial Backtest
  ↓
  Validation
  ↓
  Optimization
  ↓
  Robustness Testing
  ↓
  Production Candidate

Strategies can also move to:
Rejected
Stagnant
Needs More Research
Promising
Production Candidate

The important distinction is that "Rejected" should mean there is sufficient evidence that further tuning is unlikely to produce a meaningful edge, rather than simply "the current parameters performed badly."
4.2 Determine When to Stop Fine-Tuning
The system should help determine whether further research is justified.
Examples of evidence that a strategy may be exhausted:
- No positive net edge across reasonable parameter ranges
- Edge disappears after realistic transaction costs
- Improvements only occur through increasingly complex rules
- Performance is highly sensitive to small parameter changes
- No robustness across instruments or time periods
- Performance depends on a small number of trades
- Edge exists only in one narrow historical period
- Out-of-sample performance consistently fails
- Additional iterations produce diminishing/no meaningful improvement
  The objective is to distinguish:
  Bad implementation
  ↓
  Bad parameters
  ↓
  Weak strategy
  ↓
  Promising but incomplete strategy
  ↓
  Robust strategy

This prevents wasting research time trying to optimize a strategy that does not contain a durable edge.
5. Backtest Result Data & Frontend
   5.1 Review OpenAlgo Backtests Page
   Current backtest run information is displayed on the Backtests page.
   Review whether the current frontend representation is optimal.
   Questions to investigate:
- Is the frontend reading structured JSON directly?
- Is JSON currently the canonical result representation?
- Are results being transformed unnecessarily before rendering?
- Can the same result data be consumed by:
    - UI
    - CLI
    - analysis scripts
    - notebooks
    - benchmarking tools
    - future reporting systems?
      Desired Architecture
      Prefer having a canonical machine-readable result format:
      Backtest Engine
      ↓
      Structured Backtest Result
      ↓
      JSON
      ┌────┼──────────────┐
      ↓    ↓              ↓
      UI   Analysis     Export

The UI should be a consumer of the backtest result rather than the backtest result being designed primarily around the UI.
5.2 Backtest Result Export
Consider adding a first-class Export Backtest Results capability.
Potential formats:
- JSON — canonical machine-readable format
- CSV — tabular analysis
- Parquet — potentially useful for large-scale research
- Human-readable report — optional
  JSON is particularly useful as the canonical interchange format because it can preserve hierarchical information such as:
  Run
  ├── Configuration
  ├── Strategy
  ├── Dataset
  ├── Metrics
  ├── Trades
  ├── Benchmark
  ├── Regime Analysis
  ├── Checklist
  └── Validation

This also allows future tooling to consume historical backtest results without depending on the OpenAlgo UI.
6. Standard Strategy Specification
   6.1 Create a Standard Strategy Template
   Create one simple strategy template that must be used when defining a strategy candidate.
   The purpose is to ensure that every strategy contains the minimum information required for implementation and backtesting.
   Required Information
   Strategy Name

Market
Timeframe
Trading Style
Position Direction

Entry Conditions
Exit Conditions
Stop Loss
Take Profit

Position Sizing
Maximum Holding Period

Filters
Market/Session Filters

Required Indicators
Required Parameters

Transaction-Cost Assumptions
Other Constraints

Strategy Rationale

The template should remain deliberately simple.
6.2 Reject Incomplete Strategy Specifications
A strategy should not enter the backtest pipeline if critical rules are missing or ambiguous.
For example:
Entry: Buy when momentum is strong

is insufficient.
Whereas:
Entry:
- 5-min close > VWAP
- EMA9 > EMA20
- Entry on next candle open

Stop Loss:
- 1 ATR below entry

Exit:
- Close below VWAP
- Maximum hold: 12 bars

is implementable.
Principle
Incomplete strategy
↓
Clarify / complete
↓
Backtest

not:
Incomplete strategy
↓
Make assumptions
↓
Backtest

This prevents the backtest engine from silently introducing assumptions that change the intended strategy.
7. Strategy Presentation & UI
   7.1 Separate Strategy Definition From Research Noise
   The strategy definition shown to users should contain only the information necessary to understand and reproduce the strategy.
   Separate:
   Strategy Definition

from:
Research Notes
Experiments
Ideas
Rejected Variations
Observations
Historical Discussion

This makes strategies easier to:
- Review
- Reproduce
- Backtest
- Compare
- Modify
- Fine-tune later
  The UI should show the current canonical strategy rules, while historical research remains available separately.
8. Research & Strategy Library Cleanup
   8.1 Aggressively Remove Non-Strategies From Candidate Pool
   Review the research and strategy_library folders.
   A candidate should remain in the strategy library only if it contains sufficiently defined rules to qualify as a strategy candidate.
   Classify items as:
   Complete Strategy
   Incomplete Strategy
   Research Idea
   Duplicate
   Obsolete
   Rejected

Ideas that contain no sufficiently defined:
- Entry
- Exit
- Position direction
- Risk/SL logic
- Holding-period logic
  should not be treated as backtest candidates.
  This prevents the candidate pool from becoming a collection of half-formed ideas that repeatedly consume research effort.
9. Position-Direction Rules
   9.1 Correct Swing vs Intraday Position Configuration
   The backtest engine must enforce the intended market constraints.
   Swing
   For Indian equities:
   {
   "allow_longs": true,
   "allow_shorts": false
   }

Intraday
{
"allow_longs": true,
"allow_shorts": true
}

Current example:
engine = swing

run = {
"allow_longs": true,
"allow_shorts": true,
"cost_bps": 24,
"max_hold_bars": 120,
"min_sl_pct": 0.005
}

needs to be reviewed because allow_shorts=true conflicts with the intended swing-trading constraint.
Migration / Impact Analysis
Before changing the configuration globally:
1. Identify affected historical backtest runs.
2. Determine which strategies generated short trades.
3. Re-run those strategies with short positions disabled.
4. Quantify the change in:
    - Trade count
    - P&L
    - Expectancy
    - Drawdown
    - Sharpe/t-stat or equivalent metrics
    - Strategy evaluation status
5. Correct stored results where appropriate.
6. Ensure future runs enforce the correct configuration automatically.
   Ideally, this should become an engine-level validation rather than relying on users to configure it correctly.
10. Market Regime Analysis
    10.1 Include Market Regime in Strategy Evaluation
    Market regime can materially affect strategy performance and should therefore become part of the backtest analysis.
    Possible regimes:
    Trending Up
    Trending Down
    Sideways / Range
    High Volatility
    Low Volatility

The exact classification methodology should be defined separately and remain consistent across backtests.
10.2 Measure Strategy Performance by Regime
For each strategy, calculate performance separately for each relevant regime:
Regime	Trades	Net R	Expectancy	Win Rate	Drawdown
Bull Trend					
Bear Trend					
Range					
High Volatility					
Low Volatility


This provides information that aggregate performance cannot.
For example, a strategy could have:
Overall:
Positive

But:
Trending market → Positive
Range market   → Negative
High volatility → Positive
Low volatility  → Negative

That leads to a potentially testable strategy-management hypothesis:
Enable strategy
when regime = X

Disable strategy
when regime = Y

However, regime filtering itself must be backtested and validated out-of-sample. Otherwise it risks becoming another layer of historical overfitting.
11. Suggested Overall Backtest Architecture
    These items fit together into a larger pipeline:
    Strategy Idea
    │
    ▼
    Strategy Template
    │
    Complete / Valid?
    ┌────┴────┐
    No         Yes
    │           │
    Reject         ▼
    Initial Backtest
    │
    ▼
    Backtest Checklist
    │
    ▼
    Benchmark Evaluation
    │
    ┌─────────────┼─────────────┐
    ▼             ▼             ▼
    Costs         Regimes       Robustness
    │             │             │
    └─────────────┼─────────────┘
    ▼
    Strategy Evaluation
    │
    ┌────────────┼────────────┐
    ▼            ▼            ▼
    Improve       Stagnant     Reject
    │            │
    ▼            │
    New Strategy       │
    Version          │
    │            │
    └──────┬─────┘
    ▼
    Strategy Evolution
    │
    ▼
    Production Candidate

The key design principle is that the system should answer three separate questions:
1. Is the strategy correctly specified and implementable?
2. Does it demonstrate a robust edge after realistic costs and validation?
3. Is further research likely to be productive, or has the strategy reached an evidence-based dead end?
   That separation will make the backtest system much more useful than simply producing P&L, Sharpe, win rate, and drawdown for each strategy.