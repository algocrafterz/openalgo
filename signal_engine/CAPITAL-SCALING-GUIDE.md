# Capital Scaling Guide — Signal Engine

**Last updated:** 2026-03-25
**Sizing mode:** fixed_fractional (1% risk per trade)
**Product:** MIS (~3.3x leverage, margin_rate = 0.30)
**Slippage factor:** 0.05

---

## How MIS Margin Works (Simple Explanation)

When you buy a stock intraday (MIS), the broker doesn't block the full stock price. They block a **margin** — a fraction of the position value. This is leverage.

```
You have: ₹15,000
You buy: 50 shares of PFC at ₹400 = ₹20,000 position value
Broker blocks: ₹20,000 × 30% margin = ₹6,000 (not the full ₹20,000)
```

**What 0.30 margin means:** For every ₹100 of stock you hold, the broker locks ₹30 from your account. The remaining ₹70 is "leverage" — money the broker lends you for the day.

**Why it matters for our system:** With fixed_fractional sizing, the number of shares is determined by your risk budget (1% of capital / SL distance). A tighter SL = more shares = higher position value = more margin blocked. So **SL width controls how many concurrent trades you can take**, not stock price.

---

## Key Insight: Concurrent Positions Depend on SL Width, Not Capital

With fixed_fractional sizing, margin per trade **as a percentage of capital** is constant:

```
margin_per_trade = risk_per_trade × margin_rate / (SL% × (1 + slippage))

At 1% risk, 30% margin, 5% slippage:
= 0.01 × 0.30 / (SL% × 1.05)
= 0.003 / (SL% × 1.05)
```

| Typical SL% | Margin/Trade (% of capital) | Max Concurrent |
|-------------|---------------------------|----------------|
| 0.50% (min) | **57%** | **1** |
| 0.70% (typical ORB) | **41%** | **2** |
| 1.00% | **29%** | **3** |
| 1.50% | **19%** | **5** |

**This ratio is the same at ANY capital level.** Whether you have ₹15K or ₹1L, at 1% risk and 0.7% SL, each trade blocks ~41% of capital. Stock price doesn't matter — it cancels out in the formula.

**Verified from live data (2026-03-24):**
- PFC: 51 × ₹399.25 = ₹20,362 position → ₹6,109 margin (38% of ₹16K)
- JIOFIN: 98 × ₹228.55 = ₹22,398 position → ₹6,720 margin (42% of ₹16K)
- Total: 80% of capital → 3rd trade (BANKBARODA) rejected for insufficient funds ✓

---

## Current Config (₹15,000)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| risk_per_trade | 0.01 (1%) | ₹150 risk per trade |
| max_open_positions | 2 | 2 × ~41% margin = ~82% utilization at typical SL |
| max_capital_utilization | 0.85 | Prevents sending orders broker will reject |
| max_trades_per_day | 8 | 2 slots recycling through the day |
| min_entry_price | 100 | Avoid penny stocks |
| max_entry_price | 800 | Full mid-cap universe |
| daily_loss_limit | 0.04 | 4 consecutive losers before lockout |
| weekly_loss_limit | 0.08 | 2 bad days per week |
| max_portfolio_heat | 0.03 | 2 concurrent at 1% + buffer |

---

## What Scaling Actually Gives You

Since concurrent positions are ratio-limited (not capital-limited), scaling capital does NOT automatically increase concurrent trades. What it does:

1. **Higher absolute profit** — same 1R win = more rupees
2. **Wider stock universe** — higher max_entry_price → more signals
3. **Option to reduce risk_per_trade** — at ₹50K+, dropping to 0.8% risk allows 3 concurrent trades

### To Increase Concurrent Trades

| Lever | Effect | Trade-off |
|-------|--------|-----------|
| Reduce risk_per_trade (1% → 0.8%) | Margin drops from 41% to 33% → 3 trades | 20% less profit per trade |
| Wider SLs (min_sl_pct 0.5% → 1.0%) | Margin drops to 29% → 3 trades | Fewer qualifying signals |
| Lower margin_rate | Need different broker | Not in our control |

---

## Scaling Roadmap

### ₹25,000

```yaml
sizing:
  max_entry_price: 1000        # Include HDFCBANK, ICICIBANK range
risk:
  max_open_positions: 2        # Same ratio — still 2 concurrent at 1% risk
  max_trades_per_day: 10
  max_portfolio_heat: 0.03
```

- Risk/trade: ₹250 | Margin/trade: ~41% of capital
- **Same 2 concurrent**, but each trade earns more in absolute terms
- Opens mid-cap blue chip universe

### ₹50,000

```yaml
sizing:
  risk_per_trade: 0.008        # Reduce to 0.8% → enables 3 concurrent trades
  max_entry_price: 1500        # Higher-priced stocks
risk:
  max_open_positions: 3        # 0.8% risk → ~33% margin/trade → 3 fit
  max_trades_per_day: 12
  daily_loss_limit: 0.03       # Tighten: 0.8% × 4 losers = 3.2%
  max_portfolio_heat: 0.04     # 3 concurrent at 0.8% + buffer
```

- Risk/trade: ₹400 | Margin/trade: ~33% of capital
- **First level where 3 concurrent trades are realistic**
- Consider sector diversification at this level

### ₹75,000

```yaml
sizing:
  risk_per_trade: 0.008
  max_entry_price: 2000        # Full NSE mid/large cap universe
risk:
  max_open_positions: 3
  max_trades_per_day: 12
  weekly_loss_limit: 0.10      # Relax slightly — more capital absorbs variance
  max_portfolio_heat: 0.04
```

- Risk/trade: ₹600 | Margin/trade: ~33% of capital
- Most NSE stocks now tradeable
- Same 3 concurrent, higher absolute returns

### ₹1,00,000

```yaml
sizing:
  risk_per_trade: 0.007        # Further reduce → enables 4 concurrent
  max_entry_price: 2500        # RELIANCE, TCS, INFY eligible
risk:
  max_open_positions: 4        # 0.7% risk → ~29% margin/trade → 4 fit
  max_trades_per_day: 14
  max_portfolio_heat: 0.04     # 4 concurrent at 0.7% + buffer
  max_positions_per_sector: 2  # Force diversification across sectors
```

- Risk/trade: ₹700 | Margin/trade: ~29% of capital
- Full large-cap universe including RELIANCE/TCS
- 4 concurrent positions with sector diversification

---

## What NOT to Change When Scaling

- **min_entry_price:** Keep at ₹100. Penny stocks have wide spreads and unreliable fills.
- **margin_multiplier:** MIS 0.30 is actual broker rate (mStock, verified 2026-03-24). Don't change unless broker changes.
- **slippage_factor:** 0.05 is validated from Q1 data. Increase to 0.10 only if execution data shows higher slippage.
- **min_sl_pct:** Keep at 0.005. Protects against unrealistically tight SLs.

## When to Scale

Scale capital only after:
1. Minimum 50 live trades at current level
2. Win rate stable above 60%
3. No daily loss limit breaches in last 2 weeks
4. Comfortable with the drawdown at current risk level
