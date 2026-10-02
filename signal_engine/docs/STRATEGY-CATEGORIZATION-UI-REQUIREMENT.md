# STRATEGY-CATEGORIZATION-UI-REQUIREMENT

## Purpose

Add a lightweight strategy categorization and discovery layer to the existing strategy and Backtests UI.

The goal is to make different strategies easy to identify, organize, search, and filter without coupling categorization to strategy versions, backtest runs, or calculated verdicts.

This is an additive requirement for the current build. Do not redesign the existing strategy/version/run architecture.

## 1. Core Requirement

Every logical strategy must have structured classification metadata independent of:

- strategy version
- backtest run
- calculated run verdict
- researcher workflow state

Classification belongs to the **Strategy** entity, not an individual Backtest Run.

## 2. Minimum Classification Fields

Add:

```text
strategy_category
trading_style
direction
market
timeframe
strategy_tags
```

Example:

```text
Category: Mean Reversion
Trading Style: Intraday
Direction: Long + Short
Market: NSE Equities
Timeframe: 5m
Tags: VWAP, Pullback, Volatility
```

Use existing strategy-spec fields where they already exist. Do not create duplicate representations.

## 3. Strategy Category

Initial supported categories:

```text
Trend Following
Mean Reversion
Breakout
Momentum
Market Structure
Statistical / Quantitative
Grid
Arbitrage
Options
Other
```

The category list must be extensible.

## 4. Tags

Allow multiple tags per strategy.

Examples:

```text
VWAP
EMA
Opening Range
Volatility
Pullback
Volume
Breakout
Intraday
Swing
```

Tags are for discovery/filtering only and must not affect strategy execution or backtest results.

## 5. Separation of Concepts

Keep these concepts separate:

```text
Strategy Category
    ↓
What kind of strategy is this?

Strategy
    ↓
Which logical strategy is this?

Strategy Version
    ↓
Which rule/parameter definition is this?

Backtest Run
    ↓
Which experiment was executed?

Run Verdict
    ↓
What did the evidence show?

Research State
    ↓
What is the researcher's current disposition?
```

`EDGE`, `MARGINAL`, `NO_EDGE`, and `INCONCLUSIVE` remain calculated run verdicts and must not be used as strategy categories.

## 6. UI Requirements

The Strategies and Backtests UI should support:

### Strategy identification

Display category and key classification metadata alongside the strategy name.

Example:

```text
VWAP Mean Reversion
Mean Reversion · Intraday · NSE Equities · 5m
```

### Filtering

Allow filtering by:

- strategy category
- trading style
- direction
- market
- timeframe
- tags
- research state
- verdict where applicable

### Search

Allow searching by:

- strategy name
- category
- tags

### Related strategy discovery

Users should be able to find related strategies through category and tags.

Example:

```text
Mean Reversion
├── VWAP Mean Reversion
├── RSI Mean Reversion
└── Bollinger Mean Reversion
```

Do not introduce a separate `strategy_family` entity unless the existing architecture requires it. Category + tags are sufficient initially.

## 7. Backtests Page

The selected Backtest Run should show strategy classification as contextual information.

Example:

```text
VWAP Mean Reversion · v3 · BT-0042

Category
Mean Reversion

Style
Intraday

Market
NSE Equities

Timeframe
5m

Direction
Long + Short
```

Classification does not replace the full strategy definition. The exact rules and parameters for the selected strategy version must remain accessible.

## 8. Versioning Rules

Changing classification metadata alone must NOT create a new strategy version if strategy rules and parameters are unchanged.

For example:

```text
v2
Category: Mean Reversion
```

Changing the category or adding a discovery tag does not create `v3`.

If the change represents a material change to trading logic or parameters, normal strategy-versioning rules apply.

## 9. Reproducibility

Classification metadata must not affect strategy execution or reproducibility unless the existing architecture intentionally includes such metadata in strategy identity.

Changing a tag or category must not invalidate an otherwise identical backtest.

## 10. Data Model Guidance

Conceptually:

```text
Strategy
├── name
├── strategy_category
├── trading_style
├── direction
├── market
├── timeframe
├── strategy_tags
├── research_state
│
└── StrategyVersion
      ├── version
      ├── strategy_definition
      └── parameters
            │
            └── BacktestRun
```

Reuse existing schema fields where possible.

## 11. Implementation Constraints

Keep this lightweight.

Do NOT introduce:

- complex taxonomy engines
- AI-based automatic classification
- separate strategy-family subsystems
- category-dependent execution behavior
- category-dependent verdict logic
- category-dependent backtest calculations

The initial implementation should provide structured metadata, filtering, search, and clear UI display.

## 12. Acceptance Criteria

The implementation is complete when:

1. Every strategy can have a primary category.
2. Classification is stored at the logical Strategy level.
3. Trading style, direction, market, and timeframe remain structured fields.
4. Strategies can have multiple tags.
5. Strategies can be filtered by category.
6. Strategies can be filtered by tags.
7. Strategies can be searched by name/category/tags.
8. Backtests display strategy classification context.
9. Classification changes do not create unnecessary strategy versions.
10. Classification metadata does not alter backtest execution.
11. Classification is separate from run verdict.
12. Classification is separate from researcher `research_state`.
13. The initial taxonomy can be extended later.
14. Existing strategy/version/run functionality remains unchanged.

## 13. Recommended Initial UI

```text
VWAP Mean Reversion
[Mean Reversion] [Intraday] [NSE Equities] [5m]

Tags:
VWAP · Pullback · Volatility
```

Filters:

```text
Category       [All ▼]
Trading Style  [All ▼]
Market         [All ▼]
Timeframe      [All ▼]
Tags           [All ▼]
Research State [All ▼]
```

Keep classification visible enough to aid discovery, but do not let it dominate the Backtests decision-support UI.

## 14. Definition of Done

A researcher can open the strategy list and immediately understand:

- what type of strategy each strategy is
- what market/style/timeframe it targets
- how to find related strategies
- how to filter the strategy inventory
- how classification differs from strategy version
- how classification differs from backtest verdict

The implementation remains lightweight and does not interfere with strategy execution, versioning, backtesting, or promotion logic.
