# Semi-Automated Intraday Volume Profile + Orderflow Strategy
## Technical Requirements for LLM / PineScript Implementation

### 1. Objective
Build a **semi-automated intraday trading system** for liquid NSE F&O stocks.

- **TradingView/PineScript:** automate objective market-structure, level, volume, trend, setup, scoring, alert and backtest logic.
- **GoCharting:** manually validate orderflow before entry.
- Do **not** fully automate discretionary absorption/orderflow interpretation initially.

### 2. Instrument Universe
Primary test/watchlist:
- TCS
- ICICIBANK
- SBIN
- HDFCBANK
- RELIANCE
- AXISBANK
- INFY
- KOTAKBANK
- BHARTIARTL
- HCLTECH

Use only sufficiently liquid F&O stocks. Universe must remain configurable.

### 3. Timeframes / Session
- Execution: **5-minute**
- Context: **15-minute**
- NSE cash session
- OR window: **09:15–09:30**
- IB window: **09:15–10:15**
- Entry windows: **09:15–10:00** and **13:00–14:45**
- Hard exit: **14:55**, no exceptions

### 4. Core Levels
Calculate/track:
- Previous-session VAH
- Previous-session POC
- Previous-session VAL
- Previous Day High (PDH)
- Previous Day Low (PDL)
- ORH / ORM / ORL
- IBH / IBM / IBL
- VWAP
- 9 EMA
- Optional developing day/week highs/lows

**Important:** Previous-session VAH/POC/VAL must match the intended volume-profile methodology. Validate whether Pine can reproduce the same profile precisely; do not assume TradingView/GoCharting profiles are identical.

### 5. Market Context
Bullish context:
- Close > VWAP
- Close > 9 EMA
- 9 EMA slope positive

Bearish context:
- Close < VWAP
- Close < 9 EMA
- 9 EMA slope negative

Neutral otherwise.

Trend alignment is a filter, not the standalone trigger.

### 6. Core Auction Model
Every candidate must occur at/near a meaningful level.

Primary auction states:

**Inside Value**
- VAL < price < VAH
- Expect rotation toward POC/opposite value edge.
- Avoid chasing random breakouts inside value.

**Above VAH**
- Classify as acceptance or rejection.

**Below VAL**
- Classify as acceptance or rejection.

### 7. Primary Setups

#### A. VAH Rejection / Failed Auction — Short
Objective starting definition:
- High > VAH
- Close < VAH
- Prefer strong bearish candle / volume expansion

Manual GoCharting confirmation:
- Aggressive buying fails to push price higher
- Absorption near/above VAH
- Negative response / seller imbalance
- Subsequent candle confirms

#### B. VAL Rejection / Failed Auction — Long
Objective starting definition:
- Low < VAL
- Close > VAL
- Prefer strong bullish candle / volume expansion

Manual confirmation:
- Aggressive selling fails to push price lower
- Absorption near/below VAL
- Positive response / buyer imbalance
- Subsequent candle confirms

#### C. VAH Breakout + Acceptance — Long
Initial objective definition:
- Close > VAH
- Volume expansion
- Prefer either:
  - 2 consecutive 5-min closes above VAH, OR
  - breakout candle closes above VAH and next candle does not materially lose VAH

Manual confirmation:
- Positive delta / buy imbalance
- Acceptance above VAH
- No meaningful absorption preventing continuation

#### D. VAL Breakdown + Acceptance — Short
Mirror of VAH breakout.

#### E. Breakout + Retest — Preferred continuation model
Long:
1. Break/close above VAH or another major resistance level with volume.
2. Pullback toward level.
3. Retest holds level.
4. Trigger candle breaks retest high with renewed volume.
5. Manual orderflow confirmation.

Short = exact inverse around VAL/support.

### 8. Level Confluence
Give higher priority when multiple levels overlap or are close:
- VAH + PDH
- VAL + PDL
- VAH + ORH
- VAL + ORL
- VAH + IBH
- VAL + IBL
- Similar multi-level clusters

Level interaction is more important than trading the stock itself.

### 9. Volume / RVOL
Do not rely on a fixed 20-bar 5-min volume average as the only volume measure.

Support:
1. Rolling RVOL:
   - Current 5-min volume / SMA(volume, configurable N)
2. Absolute 5-min volume threshold (e.g. 100k shares) as a configurable filter only.
3. Prefer time-of-day-adjusted volume later if feasible.

Example starting condition:
- RVOL >= 1.5
- Higher score for RVOL >= 2.0

Absolute volume is NOT proof of institutional activity.

### 10. Candle Quality
Use objective candle outcome, not volume alone.

Example:
- CLV = (Close - Low) / (High - Low)
- Long directional candle: CLV >= 0.65
- Short directional candle: CLV <= 0.35

Avoid treating high-volume indecision candles as strong breakout confirmation.

### 11. Orderflow — Manual GoCharting Layer
Keep these manual initially:
- Absorption
- Delta behavior
- Bid/ask imbalance
- Stacked imbalance
- POC migration
- Who initiated vs. who was absorbed
- Acceptance vs. rejection
- Candle outcome / next-candle confirmation

Long checklist:
- Important location?
- Sellers aggressive?
- Sellers absorbed?
- Negative delta fails to extend downside?
- Level reclaimed?
- POC/value shifting upward?
- Buy imbalance?
- Next candle confirms?

Short = inverse.

### 12. PineScript Automation Feasibility

#### High feasibility — automate fully
- Session/time windows
- Instrument filters
- ORH/ORL/ORM
- IBH/IBL/IBM
- PDH/PDL
- VWAP
- 9 EMA and slope
- Volume / RVOL
- Candle quality
- Level proximity/confluence
- Breakout
- Acceptance/rejection objective definitions
- Retest detection
- Setup state machine
- Setup score
- Alerts
- Position sizing
- SL/TP logic
- 14:55 hard exit
- Backtesting of objective rules

#### Medium feasibility — automate with validation
- Previous-session VAH/POC/VAL
- Exact volume-profile reconstruction
- Developing profile behavior
- Complex multi-level auction classification

Do not assume Pine profile values exactly match GoCharting until validated.

#### Low / discretionary — keep manual initially
- True absorption interpretation
- "Institutional activity" inference
- Contextual footprint reading
- Whether an imbalance is meaningful
- Final orderflow confirmation

TradingView/Pine may have footprint-data capabilities, but the first implementation should not attempt to replace the GoCharting discretionary layer.

### 13. Recommended Pine Architecture

**Module 1 — Market Structure**
- Key levels
- VWAP / EMA
- OR / IB
- Previous-session profile values

**Module 2 — Setup Engine**
- Auction state
- Breakout/rejection/retest
- Volume/RVOL
- Candle quality
- Confluence
- Setup score

**Module 3 — Backtest/Execution Engine**
- Entry
- Level-based SL
- Profit targets
- Position sizing
- 50% first profit booking
- Runner management
- 14:55 forced exit

Alerts should output a concise candidate such as:
`TCS LONG CANDIDATE | VAH breakout-retest | RVOL 2.1 | Score 8/10`

### 14. Setup Scoring — Initial Framework
Example configurable score:

- Major VAH/VAL/POC interaction: +2
- PDH/PDL: +1
- ORH/ORL: +1
- IBH/IBL: +1
- VWAP alignment: +1
- EMA9 alignment: +1
- RVOL >= 1.5: +1
- RVOL >= 2.0: +2
- Strong candle outcome: +1
- Breakout + retest: +2
- Multi-level confluence: +2

Suggested state:
- Score >= 7: high-priority candidate
- Score 5–6: watch
- <5: ignore

This scoring system is a starting specification and must be backtested/optimized without overfitting.

### 15. Entry / Risk / Exit
Entry requires:
1. Pine objective candidate.
2. GoCharting manual orderflow confirmation.
3. Level-based invalidation/SL.

Risk:
- **1% of capital per trade**
- Position size = risk amount / stop distance
- Respect quantity/margin constraints.

Profit management:
- Book **50% at next major liquidity/key level**
- Keep **50% as runner**
- Use subsequent structural/profile levels for runner management.

### 16. Final Decision Flow

`F&O liquid stock`
→ `valid trading window`
→ `important profile/structure level`
→ `auction state`
→ `volume/RVOL confirmation`
→ `price/candle confirmation`
→ `trend/VWAP/EMA alignment`
→ `breakout + retest OR rejection/failed auction`
→ `Pine candidate alert`
→ `manual GoCharting footprint confirmation`
→ `entry`
→ `level-based SL`
→ `50% first target + 50% runner`
→ `hard exit 14:55`

### 17. Implementation Principles
- Strategy is **setup-first**, not stock-first.
- Do not equate high volume with institutional activity.
- Do not enter on first breakout automatically; prefer acceptance/retest when appropriate.
- Profile/level location is the primary context.
- Orderflow is the final confirmation layer.
- Keep all thresholds configurable.
- Freeze exact definitions before backtesting to prevent rule drift.
- Backtest objective Pine rules separately from the discretionary GoCharting filter.
