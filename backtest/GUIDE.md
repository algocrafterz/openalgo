# Backtest Guide

## Quick Start

```bash
# 1. Download 1m data for all symbols
uv run python backtest/download_data.py

# 2. Run batch backtest (costs + slippage applied automatically)
uv run python backtest/runner.py

# 3. Check for overfitting (walk-forward analysis)
uv run python backtest/runner.py --walk-forward

# 4. View results
cat backtest/results/orb/latest/evaluation.txt
```

## Directory Structure

```
backtest/
  config.py              # Config loader (single + batch + data)
  data.yaml              # Shared data download config (symbol pools, date range)
  costs.py               # Indian market transaction costs
  data_loader.py         # DuckDB OHLCV loader
  download_data.py       # Batch data download CLI
  evaluate.py            # Post-run evaluation + insights
  benchmark.py           # Buy-and-hold comparison
  sensitivity.py         # Parameter sensitivity analysis
  walkforward.py         # Walk-forward / train-test split
  report.py              # Tearsheet + CSV generation
  runner.py              # Multi-symbol batch runner
  results/               # Output (gitignored)
    orb/                 # Per-strategy
      20260316_213928/   # Timestamped runs
        summary.csv
        trades.csv
        tearsheet.html
        evaluation.txt
        walkforward.txt  # (if --walk-forward used)
      latest -> 20260316_213928  # Symlink to latest run
  strategies/
    __init__.py          # Strategy registry
    base.py              # Abstract Strategy base
    orb/                 # ORB strategy (self-contained)
      __init__.py
      strategy.py        # ORB engine (translated from PineScript)
      config.yaml        # Batch config (25 symbols, strategy params)
      run.py             # Single-symbol runner
      capital_sweep.py   # Capital level sweep
      PINESCRIPT_PARITY.md  # PineScript vs Python comparison
```

## Data Download

Data is stored in `db/historify.duckdb`. The download uses the Historify job system
(same pipeline as the web UI) with broker API rate limits (~3 req/sec).

Two config formats are supported:
- `backtest/data.yaml` (default) - shared pool-based config, strategy-agnostic
- `backtest/strategies/*/config.yaml` - strategy-specific, auto-detected

```bash
# Check which symbols need data
uv run python backtest/download_data.py --check

# Dry run (show what would download, with time estimate)
uv run python backtest/download_data.py --dry-run

# Download missing symbols (reads backtest/data.yaml by default)
uv run python backtest/download_data.py

# Download only a specific pool
uv run python backtest/download_data.py --pool orb

# Refresh all (fetch new candles for existing symbols)
uv run python backtest/download_data.py --incremental
```

Re-running download is safe - DuckDB uses upsert (no duplicates).

Broker API typically provides ~4 years of 1m data (back to Jan 2022).
For the 25 ORB symbols, this is ~4.3M candles covering bear (2022),
recovery (2023), and bull (2024-2026) market regimes.

### Data Config Format (data.yaml)

```yaml
start_date: "2022-01-01"
end_date: "2026-03-01"
interval: 1m

symbols:
  orb:                    # Pool name
    exchange: NSE
    list:
      - SBIN
      - PNB
  momentum:               # Another pool (symbols deduplicated across pools)
    exchange: NSE
    list:
      - RELIANCE

indices:
  - symbol: NIFTY 50
    exchange: NSE
```

### Checking Downloaded Data

```bash
# Quick summary via CLI (works even when OpenAlgo is running)
uv run python backtest/download_data.py --check

# Detailed per-symbol coverage
uv run python -c "
import duckdb, datetime
con = duckdb.connect('db/historify.duckdb', read_only=True)
rows = con.sql('''
    SELECT symbol, COUNT(*) as candles,
           MIN(timestamp) as min_ts, MAX(timestamp) as max_ts
    FROM market_data WHERE interval = '1m'
    GROUP BY symbol ORDER BY symbol
''').fetchall()
for r in rows:
    fr = datetime.datetime.fromtimestamp(r[2])
    to = datetime.datetime.fromtimestamp(r[3])
    print(f'{r[0]:<15} {r[1]:>8,} candles  {fr:%Y-%m-%d} to {to:%Y-%m-%d}')
"
```

Note: DuckDB allows only one process at a time. If OpenAlgo is running,
use `read_only=True` or stop the app first. GUI tools like DBeaver will
fail with OutOfMemoryError if the app holds the write lock.

## Running Backtests

### Batch (all symbols in config)

```bash
# Default: reads backtest/strategies/orb/config.yaml
uv run python backtest/runner.py

# Custom config
uv run python backtest/runner.py --config backtest/strategies/orb/config.yaml

# Custom output dir
uv run python backtest/runner.py --output-dir my_results
```

Results saved to: `backtest/results/{strategy}/{timestamp}/`

### Single Symbol

```bash
uv run python backtest/strategies/orb/run.py --symbol SBIN --start 2025-01-01 --end 2026-03-01
```

### Capital Sweep

```bash
uv run python backtest/strategies/orb/capital_sweep.py --symbol SBIN
```

## Results Output

Each run creates a timestamped directory:

```
backtest/results/orb/20260316_213928/
  summary.csv      # Per-symbol stats (gross PnL, net PnL, costs, profit factor)
  trades.csv       # Every trade with gross_pnl, net_pnl, cost, cost_r columns
  tearsheet.html   # QuantStats HTML report
  evaluation.txt   # Strategy health, cost impact, risk profile, edge analysis
```

A `latest` symlink always points to the most recent run for convenience.

### Evaluation Report

Each run auto-generates `evaluation.txt` with cost-aware insights:

```bash
# Auto-generated after each batch run, or run standalone:
uv run python backtest/evaluate.py backtest/results/orb/latest
```

The evaluation includes:
- **Strategy Health**: Net expectancy, profit factor, payoff ratio, edge ratio
- **Cost Impact**: Gross expectancy, avg cost/trade in R, cost erosion %
- **Risk Profile**: Max drawdown (in R), streaks, R-Sharpe, tail ratio
- **Edge Analysis**: Long vs short, exit type breakdown, symbol tiers, day-of-week
- **Insights**: Tagged `[STRENGTH]`, `[WARNING]`, `[ACTION]` messages

## Transaction Costs & Slippage

All trades automatically deduct realistic Indian market costs:

- **Brokerage**: 0.03% or Rs 20 flat (whichever is lower)
- **STT**: 0.025% on sell side (intraday MIS)
- **Exchange charges**: 0.00345%
- **SEBI fee**: 0.0001%
- **GST**: 18% on brokerage + exchange
- **Stamp duty**: 0.003% on buy side (intraday)
- **Slippage**: Configurable via `slippage_pct` (default: 0.05%)

Override in config YAML:

```yaml
slippage_pct: 0.001      # 0.1% slippage per leg
costs:
  flat_brokerage: 20.0   # Override default cost params
```

Trade log columns: `gross_pnl`, `net_pnl`, `cost`, `gross_r_multiple`, `net_r_multiple`, `cost_r`.
The `pnl` and `r_multiple` columns are aliases for net values (backward compatible).

## Validating the Edge

### 1. Walk-Forward Analysis (overfitting check)

Splits the data into train/test periods and compares performance:

```bash
# Default 70/30 split
uv run python backtest/runner.py --walk-forward

# Custom split ratio
uv run python backtest/runner.py --walk-forward --train-pct 0.6
```

Output: IS vs OOS expectancy comparison, OOS/IS ratio, overfitting flag.
- **Ratio > 0.5**: Edge appears robust
- **Ratio < 0.5**: Overfitting likely - edge doesn't generalize

### 2. Parameter Sensitivity (fragility check)

Sweep a parameter across values to see if the edge holds:

```python
from backtest.sensitivity import sensitivity_sweep, stability_score

# Define a runner function for each parameter value
def run_with_tp(tp_val):
    config["orb_config"] = ORBConfig(tp_multiplier=tp_val, ...)
    batch = run_batch(config)
    trades = batch.combined_trade_log()
    health = compute_strategy_health(trades)
    return {"expectancy_r": health["expectancy_r"], "total_trades": health["total_trades"]}

# Sweep
df = sensitivity_sweep("tp_multiplier", [0.8, 1.0, 1.2, 1.5], run_with_tp)
score = stability_score(df["expectancy_r"].tolist())
print(score["message"])  # "Stable (CV=0.15)" or "Fragile (CV=1.8)"
```

- **CV < 0.5**: Parameter is stable (edge holds across values)
- **CV > 0.5**: Parameter is fragile (edge depends on exact value = overfitting risk)

### 3. Benchmark Comparison (alpha check)

Compare strategy returns against buy-and-hold:

```python
from backtest.benchmark import compute_buy_and_hold, compare_strategy_vs_benchmark

bh = compute_buy_and_hold(nifty_ohlcv_df)
result = compare_strategy_vs_benchmark(
    strategy_return_pct=25.0,
    benchmark_return_pct=bh["total_return_pct"],
    strategy_name="ORB",
    benchmark_name="NIFTY 50",
)
print(result["message"])  # "ORB outperforms NIFTY 50 by +10.0pp"
```

## Realistic Backtest Benchmarks

Before trading a strategy live, these are the minimum thresholds
a backtest should meet. Values below these indicate no tradeable edge.

| Metric | Minimum | Good | Excellent |
|--------|---------|------|-----------|
| Net Expectancy | > +0.10 R | > +0.20 R | > +0.35 R |
| Gross Expectancy | > +0.15 R | > +0.30 R | > +0.50 R |
| Profit Factor | > 1.2 | > 1.5 | > 2.0 |
| Win Rate (at 1:1 payoff) | > 55% | > 60% | > 65% |
| Win Rate (at 2:1 payoff) | > 40% | > 45% | > 50% |
| Walk-Forward OOS/IS | > 0.5 | > 0.7 | > 0.9 |
| Sensitivity CV | < 0.5 | < 0.3 | < 0.15 |
| Max Drawdown | < 30 R | < 20 R | < 10 R |
| Sample Size | > 200 trades | > 500 trades | > 1000 trades |
| Data Period | > 3 years | > 4 years | > 5 years |
| Cost Erosion | < 50% of gross | < 30% | < 20% |

**Indian market cost reality**: Intraday MIS round-trip costs ~0.15-0.30R
per trade (brokerage + STT + exchange + GST + slippage). Any strategy with
gross expectancy below +0.30R will struggle to be net-profitable.

## ORB Strategy Observations (March 2026)

### 4-Year Backtest Results (2022-01 to 2026-02, 25 symbols)

- 1067 trades, gross expectancy: -0.002R (essentially random)
- Net expectancy: -0.303R (costs of 0.298R/trade destroy any edge)
- 0 out of 25 symbols net-profitable
- Walk-forward: IS -0.306R, OOS -0.299R (consistent losses both periods)
- 55% of trades exit on TIME at -0.343R (entries without follow-through)

### Known Backtest vs Live Gap

The Python backtest currently runs **without NIFTY index data** for the
index direction filter. In live PineScript trading, this filter blocks
counter-index entries via `request.security()`. This is a significant
gap - the index filter is one of the strongest filters in the strategy.

**Impact**: The backtest takes ~20-30% more trades than live PineScript
would, most of which are losers (counter-index breakouts that fail).

**Status**: Fix pending - requires loading NIFTY 50 data from DuckDB and
passing it to the strategy via `index_data` parameter in `runner.py`.

### Interpretation

The Q1 2026 live PineScript results (+69R gross over 31 days) likely
benefited from: (1) a favorable momentum regime, (2) the index filter
blocking bad trades, and (3) a small sample size (31 days). The 4-year
backtest with 1067 trades is more statistically reliable but is missing
the index filter, so the true answer lies between these two results.

**Before adjusting live trading**: Fix the index filter gap in the
backtest, re-run, and compare. If gross expectancy is still below +0.15R
with the index filter enabled, the strategy does not have a tradeable edge.

## Recommended Workflow

```
1. Download data          uv run python backtest/download_data.py
2. Run backtest           uv run python backtest/runner.py
3. Check evaluation       cat backtest/results/orb/latest/evaluation.txt
4. Walk-forward check     uv run python backtest/runner.py --walk-forward
5. If edge survives OOS   -> tune parameters, expand universe
6. If edge dies in OOS    -> reduce parameters, simplify strategy
```

Key questions to answer before trading live:
- Is net expectancy positive after costs? (evaluation.txt COST IMPACT section)
- Does the edge hold out-of-sample? (walk-forward OOS/IS ratio > 0.5)
- Is the edge robust to parameter changes? (sensitivity CV < 0.5)
- Does it beat buy-and-hold? (benchmark alpha > 0)

## Switching Strategies

There is no `--strategy` flag. Each config file declares its own `strategy:` key.
To switch strategies, point `--config` at a different config file:

```bash
# Run ORB strategy
uv run python backtest/runner.py --config backtest/strategies/orb/config.yaml

# Run a custom strategy
uv run python backtest/runner.py --config backtest/strategies/my_strategy/config.yaml
```

The `strategy:` field in the YAML must match a registered key in `backtest/strategies/__init__.py`.

## Adding a New Strategy

1. Create folder: `backtest/strategies/my_strategy/`
2. Implement `strategy.py` extending `Strategy` base class
3. Add `config.yaml` with symbols and parameters
4. Register in `backtest/strategies/__init__.py`:
   ```python
   STRATEGY_REGISTRY["my_strategy"] = (MyStrategy, "Description")
   ```
5. Run: `uv run python backtest/runner.py --config backtest/strategies/my_strategy/config.yaml`

## Config Format (Batch)

```yaml
symbols:
  - SBIN               # Shorthand (defaults to NSE)
  - symbol: RELIANCE   # Full format
    exchange: NSE

interval: 5m           # 1m, 5m, 15m, 30m, 1h, D
start_date: "2022-01-04"
end_date: "2026-03-01"
initial_capital: 100000
position_size_pct: 0.10
product: MIS
strategy: orb           # Must match registry key
slippage_pct: 0.0005    # 0.05% per leg (default)

orb:                    # Strategy-specific parameters
  orb_minutes: 15
  tp_multiplier: 1.0
  stop_mode: ATR
  # ... see ORBConfig in strategy.py for all options

costs:                  # Optional cost overrides
  flat_brokerage: 20.0
```

## Intervals

- `1m` and `D` are stored directly in Historify DuckDB
- `5m`, `15m`, `30m`, `1h` are computed from 1m data on-the-fly
- Download script always fetches 1m data for computed intervals
