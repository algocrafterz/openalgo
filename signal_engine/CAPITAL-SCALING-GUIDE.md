# Capital Scaling Guide — Signal Engine

**Last updated:** 2026-03-24
**Sizing mode:** fixed_fractional (1% risk per trade)
**Product:** MIS (5x leverage, margin_rate = 0.20)
**Slippage factor:** 0.05

---

## Key Insight: Fixed Fractional Makes Margin Nearly Constant

With fixed_fractional sizing, margin per trade is ~constant regardless of stock price:

```
qty   = risk_amount / (entry × SL% × (1 + slippage))
margin = qty × entry × margin_rate

The entry price cancels out:
margin ≈ capital × risk_pct / (SL% × 1.05) × 0.20
       = 15000 × 0.01 / (0.01 × 1.05) × 0.20
       ≈ ₹2,857 per trade (regardless of stock price)
```

**Lowering price filter does NOT improve margin utilization.** A ₹100 stock and a ₹800 stock both use ~₹2,800 margin per position.

---

## Current Config (₹15,000)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| risk_per_trade | 0.01 (1%) | ₹150 risk per trade |
| max_open_positions | 3 | 3 × ₹2,857 = ₹8,571 (57% of capital) |
| max_trades_per_day | 8 | 3 slots recycling through the day |
| min_entry_price | 100 | Avoid penny stocks |
| max_entry_price | 800 | Fits margin at any SL width |
| daily_loss_limit | 0.04 | 4 consecutive losers before lockout |
| weekly_loss_limit | 0.08 | 2 bad days per week |
| max_portfolio_heat | 0.04 | 3 concurrent at 1% + buffer |

**Margin headroom:** Could fit 5 concurrent positions, but 3 is quality-optimal (Q1 data: signals 1-2 have 90% WR vs 57% for later signals).

---

## Scaling Roadmap

### ₹25,000

```yaml
sizing:
  max_entry_price: 1000        # Include HDFCBANK, ICICIBANK range
risk:
  max_open_positions: 4        # ₹4,762 × 4 = ₹19,048 (76% utilization)
  max_trades_per_day: 10
  max_portfolio_heat: 0.05     # 4 concurrent at 1% + buffer
```

- Risk/trade: ₹250
- Margin/trade: ~₹4,762
- Opens mid-cap blue chip universe

### ₹50,000

```yaml
sizing:
  risk_per_trade: 0.008        # Reduce to 0.8% for capital preservation
  max_entry_price: 1500        # Higher-priced stocks
risk:
  max_open_positions: 5        # ₹7,619 × 5 = ₹38,095 (76% utilization)
  max_trades_per_day: 12
  daily_loss_limit: 0.03       # Tighten: 0.8% × 4 losers = 3.2%
  max_portfolio_heat: 0.05
```

- Risk/trade: ₹400
- Margin/trade: ~₹7,619
- Consider sector diversification at this level

### ₹75,000

```yaml
sizing:
  risk_per_trade: 0.008
  max_entry_price: 2000        # Full NSE mid/large cap universe
risk:
  max_open_positions: 5
  max_trades_per_day: 12
  weekly_loss_limit: 0.10      # Relax slightly — more capital absorbs variance
  max_portfolio_heat: 0.05
```

- Risk/trade: ₹600
- Margin/trade: ~₹11,429
- Most NSE stocks now tradeable

### ₹1,00,000

```yaml
sizing:
  risk_per_trade: 0.008
  max_entry_price: 2500        # RELIANCE, TCS, INFY eligible
risk:
  max_open_positions: 6        # ₹15,238 × 6 = ₹91,429 (91% utilization)
  max_trades_per_day: 14
  max_portfolio_heat: 0.06     # 6 concurrent at 0.8% + buffer
  max_positions_per_sector: 2  # Force diversification across sectors
```

- Risk/trade: ₹800
- Margin/trade: ~₹15,238
- Full large-cap universe including RELIANCE/TCS

---

## What NOT to Change When Scaling

- **risk_per_trade:** Keep at 1% until ₹50K, then consider 0.8%. Never go above 1%.
- **min_entry_price:** Keep at ₹100. Penny stocks have wide spreads and unreliable fills.
- **margin_multiplier:** MIS 0.20 is broker-standard. Don't change unless broker changes.
- **slippage_factor:** 0.05 is validated from Q1 data. Increase to 0.10 only if execution data shows higher slippage.
- **min_sl_pct:** Keep at 0.005. Protects against unrealistically tight SLs.

## When to Scale

Scale capital only after:
1. Minimum 50 live trades at current level
2. Win rate stable above 60%
3. No daily loss limit breaches in last 2 weeks
4. Comfortable with the drawdown at current risk level
