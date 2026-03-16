# Backtest Guide

## Quick Start

```bash
# 1. Download 1m data for all symbols in config
uv run python backtest/download_data.py

# 2. Run batch backtest
uv run python backtest/runner.py

# 3. View results
ls backtest/results/orb/latest/
```

## Directory Structure

```
backtest/
  config.py              # Config loader (single + batch)
  config.yaml            # Single-symbol config template
  costs.py               # Indian market transaction costs
  data_loader.py         # DuckDB OHLCV loader
  download_data.py       # Batch data download CLI
  evaluate.py            # Post-run evaluation + insights
  report.py              # Tearsheet + CSV generation
  runner.py              # Multi-symbol batch runner
  results/               # Output (gitignored)
    orb/                 # Per-strategy
      20260316_143022/   # Timestamped runs
        summary.csv
        trades.csv
        tearsheet.html
      latest -> 20260316_143022  # Symlink to latest run
  strategies/
    __init__.py          # Strategy registry
    base.py              # Abstract Strategy base
    orb/                 # ORB strategy (self-contained)
      __init__.py
      strategy.py        # ORB engine (translated from PineScript)
      config.yaml        # Batch config (25 symbols)
      run.py             # Single-symbol runner
      capital_sweep.py   # Capital level sweep
      PINESCRIPT_PARITY.md  # PineScript vs Python comparison
```

## Data Download

Downloads 1m OHLCV data from Historify (DuckDB) using the broker API.
Rate-limited: ~3 req/sec per-call, 1-3s delay between symbols.

```bash
# Check which symbols need data
uv run python backtest/download_data.py --check

# Dry run (show what would download)
uv run python backtest/download_data.py --dry-run

# Download missing symbols
uv run python backtest/download_data.py

# Refresh all (only fetch new candles)
uv run python backtest/download_data.py --incremental

# Custom config
uv run python backtest/download_data.py --config backtest/strategies/orb/config.yaml
```

## Running Backtests

### Batch (all symbols in config)

```bash
# Default: ORB strategy, 25 symbols
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
backtest/results/orb/20260316_143022/
  summary.csv      # Per-symbol stats (trades, win rate, PnL, profit factor)
  trades.csv       # Every trade (entry/exit time, price, direction, R-multiple)
  tearsheet.html   # QuantStats HTML report (requires: uv add quantstats)
  evaluation.txt   # Strategy health, risk profile, edge analysis, insights
```

A `latest` symlink always points to the most recent run for convenience.

### Evaluation Report

Each run also generates `evaluation.txt` with actionable insights:

```bash
# Auto-generated after each batch run, or run standalone:
uv run python backtest/evaluate.py backtest/results/orb/latest
```

The evaluation includes:
- **Strategy Health**: Expectancy, profit factor, payoff ratio, edge ratio
- **Risk Profile**: Max drawdown (in R), streaks, R-Sharpe, tail ratio
- **Edge Analysis**: Long vs short, exit type breakdown, symbol tiers, day-of-week
- **Insights**: Tagged `[STRENGTH]`, `[WARNING]`, `[ACTION]` messages

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
start_date: "2025-01-01"
end_date: "2026-03-01"
initial_capital: 100000
position_size_pct: 0.10
product: MIS
strategy: orb           # Must match registry key

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
