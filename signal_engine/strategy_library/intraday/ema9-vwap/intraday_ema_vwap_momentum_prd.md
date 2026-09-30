# PRD: Intraday EMA–VWAP Momentum Indicator & Trade Framework

## 1. Objective

Build a GoCharting/LipiScript-based indicator and trading framework for NSE intraday equities that detects momentum earlier than a conventional EMA crossover while reducing false signals caused by price oscillating around VWAP.

The system should use:

- VWAP — session reference
- Fast EMA — short-term price response
- 20 EMA — dynamic support/resistance
- ATR(14) — volatility normalization
- EMA–VWAP normalized displacement — momentum strength and expansion/contraction
- 5m price structure — execution and trade invalidation

The system must not assume that an EMA/VWAP crossover guarantees continuation.

---

## 2. Timeframes

### 15-minute

| Indicator | Setting | Purpose |
|---|---:|---|
| Fast EMA | 5 EMA | Early momentum |
| Slow EMA | 20 EMA | Dynamic support/resistance |
| VWAP | Session VWAP | Directional reference |
| ATR | 14 | Volatility normalization |
| Displacement | `(EMA5 - VWAP) / ATR14` | Momentum strength |

### 5-minute

| Indicator | Setting | Purpose |
|---|---:|---|
| Fast EMA | 9 EMA | Entry momentum |
| Slow EMA | 20 EMA | Dynamic support/resistance |
| VWAP | Session VWAP | Directional reference |
| ATR | 14 | Volatility normalization |
| Displacement | `(EMA9 - VWAP) / ATR14` | Momentum strength |

---

## 3. Core Indicator

Calculate:

```text
Displacement = (Fast EMA - VWAP) / ATR(14)
```

### Interpretation

```text
Positive → Fast EMA above VWAP
Negative → Fast EMA below VWAP
Zero     → Fast EMA = VWAP
```

Magnitude represents separation relative to current volatility.

Example:

```text
EMA9  = 102
VWAP  = 100
ATR14 = 4

Displacement = (102 - 100) / 4
             = +0.50
```

The indicator should be plotted in a separate pane.

---

## 4. Reference Levels

Display horizontal levels:

```text
+1.0
+0.5
 0.0
-0.5
-1.0
```

Interpretation:

| Displacement | Interpretation |
|---:|---|
| 0 to ±0.2 | VWAP/crossover zone |
| ±0.2 to ±0.5 | Momentum developing |
| > ±0.5 | Meaningful separation |
| > ±1.0 | Strong/possibly extended |
| Falling from positive | Bullish momentum contracting |
| Rising from negative toward zero | Bearish momentum contracting |

These thresholds are initial research parameters, not validated universal values.

---

## 5. Momentum State Detection

The system should distinguish between a crossover and a successful momentum expansion.

### State 1 — Neutral

```text
Displacement ≈ 0
```

Fast EMA and VWAP are close.

Potential VWAP chop.

### State 2 — Crossover

Long:

```text
Displacement crosses 0 → positive
```

Short:

```text
Displacement crosses 0 → negative
```

This is only a momentum attempt.

Do not automatically enter.

### State 3 — Expansion

Long:

```text
Displacement > previous Displacement
```

Short:

```text
Displacement < previous Displacement
```

Example:

```text
0.05
0.12
0.23
0.37
0.52
```

This indicates increasing EMA/VWAP separation.

### State 4 — Strong Momentum

Example:

```text
0.52
0.67
0.84
1.02
```

The move is expanding significantly relative to ATR.

### State 5 — Contraction

Example:

```text
1.02
0.91
0.76
0.61
0.45
```

Momentum is weakening even though the oscillator remains positive.

Do not automatically exit solely because of contraction.

### State 6 — Momentum Failure

Long:

```text
Displacement crosses back below 0
```

Short:

```text
Displacement crosses back above 0
```

This represents EMA/VWAP momentum failure.

Use price structure to determine whether the position should actually be exited.

---

## 6. Long Entry Logic

### 15-minute context

Require:

```text
EMA5 > VWAP
```

and preferably:

```text
Displacement > 0
Displacement increasing
Price > EMA20
```

The EMA5/VWAP crossover is the initial trigger.

Do not wait for EMA5 to cross EMA20.

### 5-minute execution

Look for:

```text
EMA9 crosses above VWAP
```

AND:

```text
Price > EMA20
```

AND:

```text
Displacement > 0
```

AND preferably:

```text
Current displacement > previous displacement
```

The preferred entry is after the crossover but before the move becomes excessively extended.

Example:

```text
0.00
0.08
0.17
0.29
0.42
```

This is preferable to waiting for:

```text
0.00
0.40
0.75
1.10
```

because the latter may already represent an extended move.

---

## 7. Short Entry Logic

Reverse the long conditions.

### 15-minute

```text
EMA5 < VWAP
Price < EMA20
Displacement < 0
Displacement decreasing
```

### 5-minute

```text
EMA9 crosses below VWAP
Price < EMA20
Displacement < 0
Displacement decreasing
```

---

## 8. Avoid / Filter Conditions

Avoid taking a crossover when:

### A. VWAP chop

```text
Displacement repeatedly:

+0.1
-0.1
+0.15
-0.08
```

This indicates price is oscillating around VWAP.

### B. EMA20 conflict

Example:

```text
EMA9 > VWAP
but
Price < EMA20
```

The fast momentum signal conflicts with the broader intraday structure.

Treat as lower-quality setup rather than automatically taking it.

### C. Displacement immediately contracts

Example:

```text
0.00
0.18
0.28
0.21
0.12
```

The crossover occurred but separation failed to develop.

### D. Excessive extension

Example:

```text
Displacement > +1.5
```

or:

```text
Displacement < -1.5
```

Do not initiate a new position solely because of an extreme displacement.

---

## 9. Stop-Loss Framework

The primary stop should be based on price structure rather than the oscillator.

### Long

Use:

```text
Recent 5m swing low - small buffer
```

### Short

Use:

```text
Recent 5m swing high + small buffer
```

ATR can be used as a secondary sanity check.

Initial parameter to test:

```text
ATR14 × 1.5 to 2.0
```

Do not force an ATR stop if the structural stop is clearly more meaningful.

---

## 10. Take-Profit Framework

Do not assume a fixed percentage target is optimal.

Measure results in R-multiples.

If:

```text
Entry = ₹100
SL    = ₹98
```

then:

```text
1R = ₹2
2R = ₹4
3R = ₹6
```

Initial configurations to backtest:

1. Fixed 1.5R
2. Fixed 2R
3. Fixed 3R
4. Partial at 1.5–2R + trailing remainder
5. No fixed TP + ATR/structure trailing

Because the strategy is intended to capture momentum expansions, the no-fixed-TP and partial-plus-trailing variants should be evaluated carefully.

---

## 11. Trade Management

Use the indicators for different purposes.

| Component | Function |
|---|---|
| EMA9/EMA5 ↔ VWAP | Momentum trigger |
| EMA20 | Dynamic support/resistance |
| Displacement | Momentum strength |
| Displacement slope | Expansion/contraction |
| ATR14 | Volatility |
| 5m swing structure | Stop/invalidation |
| Price action | Entry refinement |

Do not use all indicators as independent entry signals.

---

## 12. Displacement Rate of Change

Optional second oscillator:

```text
Displacement ROC =
Current Displacement - Previous Displacement
```

Interpretation:

```text
Positive ROC → separation expanding
Negative ROC → separation contracting
```

For a long:

```text
Displacement > 0
AND
Displacement ROC > 0
```

is a stronger momentum condition than displacement being merely positive.

For a short:

```text
Displacement < 0
AND
Displacement ROC < 0
```

is a stronger bearish momentum condition.

---

## 13. Visualization Requirements

### Main price chart

Display:

```text
VWAP
Fast EMA
20 EMA
```

### Lower oscillator

Display:

```text
EMA-VWAP / ATR
```

with horizontal levels:

```text
+1.0
+0.5
 0
-0.5
-1.0
```

Optional:

```text
Displacement ROC
```

### Visual states

The indicator should make these states visually obvious:

- Neutral/chop
- Bullish crossover
- Bullish expansion
- Bullish contraction
- Bearish crossover
- Bearish expansion
- Bearish contraction

---

## 14. Backtesting Requirements

The strategy must be tested on the actual intended NSE equity universe.

Do not optimize solely on one stock.

At minimum compare:

### Fast EMA variants

15m:

```text
5
8
9
```

5m:

```text
5
8
9
```

### Displacement thresholds

```text
0.25
0.50
0.75
1.00
1.25
```

### ATR periods

```text
10
14
21
```

### Exit methods

```text
1.5R
2R
3R
ATR trailing
Structure trailing
Partial + trailing
```

All combinations should be evaluated with realistic NSE transaction costs and slippage.

---

## 15. Metrics

Evaluate:

- Net R
- R/trade
- Win rate
- Profit factor
- Expectancy
- Maximum drawdown
- Maximum adverse excursion (MAE)
- Maximum favorable excursion (MFE)
- Average holding time
- Trades per day
- False crossover rate
- Percentage of crossover signals that reach +0.5/-0.5 displacement
- Return between crossover and +0.5/-0.5
- Return after +0.5/-0.5
- Performance by time of day
- Performance by volatility regime

The critical comparison is:

```text
EMA/VWAP crossover entry
vs
early displacement entry
vs
0.5 ATR displacement confirmation entry
```

---

## 16. Primary Research Question

The main hypothesis is:

> An EMA/VWAP crossover followed by expanding ATR-normalized separation can identify genuine momentum earlier than waiting for a conventional EMA/EMA crossover, while filtering some of the false signals caused by VWAP oscillation.

The backtest must determine whether this produces a statistically meaningful improvement in:

- Entry efficiency
- R/trade
- Expectancy
- Drawdown
- False-signal rate

without introducing excessive early-entry losses.

---

## 17. Initial Configuration

Use this as the baseline before optimization.

### 15m

```text
EMA = 5
EMA = 20
VWAP
ATR = 14

Displacement =
(EMA5 - VWAP) / ATR14
```

### 5m

```text
EMA = 9
EMA = 20
VWAP
ATR = 14

Displacement =
(EMA9 - VWAP) / ATR14
```

### Initial momentum interpretation

```text
0              = crossover
±0.2           = crossover/chop zone
±0.2–0.5       = developing momentum
±0.5+          = meaningful separation
±1.0+          = strong/possibly extended
```

These values are starting research parameters and must be validated through out-of-sample testing.

---

## 18. Key Design Principle

The framework should not ask:

> "Did EMA cross VWAP?"

It should ask:

> "Did EMA cross VWAP, is price structurally aligned, and is the EMA/VWAP separation expanding relative to normal volatility?"

That distinction is the core of the system.

---

## 19. Success Criteria

The implementation is considered promising only if out-of-sample testing demonstrates that the displacement-based framework provides measurable improvement over the simple EMA/VWAP crossover baseline after realistic costs.

No threshold such as `+0.5` should be considered optimal until validated across multiple NSE symbols, market regimes, and independent test periods.
