#!/usr/bin/env python3
"""
Batch data download for backtesting.

Downloads 1m OHLCV data for all symbols in a batch config file
using the Historify job system. This reuses the same rate-limited,
retry-capable, incremental download pipeline that the Historify web UI uses.

Rate limiting (mstock broker):
  - Per-API-call: 350ms minimum between broker calls (~3 req/sec)
    enforced by history_service._enforce_rate_limit()
  - Per-symbol: 1-3s random delay between symbols (configurable via
    HISTORIFY_DELAY_MIN / HISTORIFY_DELAY_MAX env vars)
  - Per-request: 1000 candle limit, chunked automatically
    (1m data = 2 days/chunk = ~150 requests per symbol for 14 months)

Usage:
    uv run python backtest/download_data.py
    uv run python backtest/download_data.py --config backtest/strategies/orb/config.yaml
    uv run python backtest/download_data.py --config backtest/strategies/orb/config.yaml --incremental
    uv run python backtest/download_data.py --check
    uv run python backtest/download_data.py --dry-run
"""

import argparse
import sys
import time
from pathlib import Path

# Add project root to path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from database.historify_db import get_data_catalog as _db_get_data_catalog


def get_data_catalog() -> list[dict]:
    """Get data catalog from Historify DuckDB."""
    return _db_get_data_catalog()


def check_data_availability(
    symbols: list[dict[str, str]], interval: str
) -> tuple[list[dict], list[dict]]:
    """
    Check which symbols have data available in Historify.

    For computed intervals (5m, 15m, 30m, 1h), checks for 1m source data.

    Args:
        symbols: List of {symbol, exchange} dicts.
        interval: Target interval (e.g., "5m", "1m", "D").

    Returns:
        Tuple of (available, missing) symbol lists.
    """
    # Computed intervals derive from 1m data
    check_interval = "1m" if interval in ("5m", "15m", "30m", "1h") else interval

    catalog = get_data_catalog()
    catalog_set = {
        (entry["symbol"], entry["exchange"], entry["interval"])
        for entry in catalog
    }

    available = []
    missing = []
    for sym in symbols:
        key = (sym["symbol"], sym["exchange"], check_interval)
        if key in catalog_set:
            available.append(sym)
        else:
            missing.append(sym)

    return available, missing


def get_missing_symbols(
    symbols: list[dict[str, str]], interval: str
) -> list[dict[str, str]]:
    """
    Get symbols that need data download.

    Args:
        symbols: List of {symbol, exchange} dicts.
        interval: Target interval.

    Returns:
        List of symbols missing from Historify.
    """
    _, missing = check_data_availability(symbols, interval)
    return missing


def estimate_download_time(
    num_symbols: int, start_date: str, end_date: str,
) -> float:
    """
    Estimate download time in minutes.

    Based on: 1m data = ~375 candles/day, 1000 candle limit per request
    = ~2 trading days per chunk. With 250 trading days/year and ~2s per chunk
    + 2s inter-symbol delay.

    Args:
        num_symbols: Number of symbols to download.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).

    Returns:
        Estimated download time in minutes.
    """
    from datetime import datetime

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    calendar_days = (end - start).days

    # ~250 trading days per 365 calendar days
    trading_days = int(calendar_days * (250 / 365))

    # ~2 trading days per chunk (1000 candle limit at 375 candles/day)
    chunks_per_symbol = max(1, trading_days // 2)

    # Time per chunk: ~0.4s API call + overhead
    chunk_time_s = 0.5

    # Inter-symbol delay: ~2s average
    symbol_delay_s = 2.0

    total_seconds = num_symbols * (
        chunks_per_symbol * chunk_time_s + symbol_delay_s
    )
    return round(total_seconds / 60, 1)


def _ensure_env_loaded():
    """Ensure .env is loaded for database and broker access."""
    from dotenv import load_dotenv
    load_dotenv()


def _load_download_config(config_path: str, pool: str | None = None) -> dict:
    """Load config from either data.yaml or strategy config.yaml."""
    import yaml

    # Peek at file to detect format
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    # data.yaml has symbols as dict of pools; strategy config has symbols as list
    if isinstance(raw.get("symbols"), dict):
        from backtest.config import load_data_config
        return load_data_config(config_path, pool=pool)

    # Strategy config format
    from backtest.config import load_batch_config
    return load_batch_config(config_path)


def download_batch(
    config_path: str,
    incremental: bool = False,
    dry_run: bool = False,
    pool: str | None = None,
) -> dict:
    """
    Download 1m data for all symbols in config using the Historify job system.

    Supports both data.yaml (pool-based) and strategy config.yaml formats.

    This reuses the same download pipeline as the Historify web UI:
    - Per-call rate limiting (350ms, ~3 req/sec) via history_service
    - Per-symbol delay (1-3s random) via historify job processor
    - Automatic chunking (1000 candle limit per request)
    - Incremental download support (only fetch new data)
    - DuckDB upsert (safe to re-run)

    Args:
        config_path: Path to config YAML (data.yaml or strategy config).
        incremental: Only fetch data after last available timestamp.
        dry_run: If True, only report what would be downloaded.
        pool: Optional pool name (only for data.yaml format).

    Returns:
        Dict with download results: {total, downloaded, skipped, failed, job_id}.
    """
    config = _load_download_config(config_path, pool=pool)
    symbols = config["symbols"]
    start_date = config["start_date"]
    end_date = config["end_date"]
    interval = config["interval"]

    # Determine storage interval (5m -> 1m)
    storage_interval = "1m" if interval in ("5m", "15m", "30m", "1h") else interval

    # Check what's already available
    available, missing = check_data_availability(symbols, interval)

    result = {
        "total": len(symbols),
        "already_available": len(available),
        "to_download": len(missing) if not incremental else len(symbols),
        "downloaded": 0,
        "failed": 0,
        "job_id": None,
    }

    if dry_run:
        print(f"Date range: {start_date} to {end_date}")
        print(f"Interval: {storage_interval}")
        print(f"Total symbols: {len(symbols)}")
        print(f"Already available: {len(available)}")
        print(f"Need download: {len(missing)}")
        if missing:
            est_min = estimate_download_time(len(missing), start_date, end_date)
            print(f"Estimated time: {est_min:.0f} min ({est_min/60:.1f} hrs)")
            print("Missing symbols:")
            for sym in missing:
                print(f"  {sym['exchange']}:{sym['symbol']}")
        if incremental:
            est_min = estimate_download_time(len(symbols), start_date, end_date)
            print(f"Incremental refresh: all {len(symbols)} symbols")
            print(f"Estimated time: {est_min:.0f} min ({est_min/60:.1f} hrs)")
        return result

    # Determine which symbols to download
    download_list = symbols if incremental else missing
    if not download_list:
        print("All symbols already have data. Use --incremental to refresh.")
        return result

    result["to_download"] = len(download_list)

    # Ensure env vars are loaded for DB/broker access
    _ensure_env_loaded()
    print("Initializing...")

    from database.auth_db import get_first_available_api_key
    from services.historify_service import create_and_start_job

    api_key = get_first_available_api_key()
    if not api_key:
        print("No API key found. Please log in to OpenAlgo and generate an API key first.")
        return result

    print(f"Creating download job for {len(download_list)} symbols "
          f"({storage_interval}, {start_date} to {end_date})...")
    print(f"Rate limits: ~3 req/sec per-call, 1-3s delay between symbols")

    # Use the Historify job system - same pipeline as the web UI
    success, response, status_code = create_and_start_job(
        job_type="custom",
        symbols=download_list,
        interval=storage_interval,
        start_date=start_date,
        end_date=end_date,
        api_key=api_key,
        incremental=incremental,
    )

    if not success:
        print(f"Failed to create job: {response.get('message', 'Unknown error')}")
        return result

    job_id = response.get("job_id")
    result["job_id"] = job_id
    print(f"Job started: {job_id}")
    print(f"Downloading {len(download_list)} symbols... (this may take a while)")

    # Poll for job completion
    from database.historify_db import get_download_job, get_job_items

    poll_interval = 5  # seconds
    last_completed = 0

    while True:
        time.sleep(poll_interval)

        job = get_download_job(job_id)
        if not job:
            print("Job not found. Exiting.")
            break

        status = job.get("status", "unknown")
        completed = job.get("completed_symbols", 0)
        failed = job.get("failed_symbols", 0)
        total = job.get("total_symbols", len(download_list))

        # Print progress when it changes
        if completed != last_completed:
            print(f"  Progress: {completed + failed}/{total} "
                  f"({completed} OK, {failed} failed)", flush=True)
            last_completed = completed

        if status in ("completed", "failed", "cancelled"):
            break

    # Final summary
    job = get_download_job(job_id)
    items = get_job_items(job_id)

    result["downloaded"] = job.get("completed_symbols", 0)
    result["failed"] = job.get("failed_symbols", 0)

    print(f"\nJob {job_id} {job.get('status', 'unknown')}:")
    print(f"  Downloaded: {result['downloaded']}")
    print(f"  Failed: {result['failed']}")
    print(f"  Already available: {result['already_available']}")

    # Show failed items if any
    if items:
        failed_items = [i for i in items if i.get("status") == "error"]
        if failed_items:
            print(f"\nFailed symbols:")
            for item in failed_items:
                print(f"  {item.get('exchange', '?')}:{item.get('symbol', '?')} "
                      f"- {item.get('error_message', 'Unknown error')}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Download data for batch backtesting")
    parser.add_argument("--config", default="backtest/data.yaml",
                        help="Data config YAML path (default: backtest/data.yaml)")
    parser.add_argument("--pool", default=None,
                        help="Download only a specific symbol pool (e.g., orb)")
    parser.add_argument("--incremental", action="store_true",
                        help="Re-download all symbols (update existing data)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only show what would be downloaded")
    parser.add_argument("--check", action="store_true",
                        help="Only check data availability")
    args = parser.parse_args()

    if args.check:
        config = _load_download_config(args.config, pool=args.pool)
        available, missing = check_data_availability(
            config["symbols"], config["interval"]
        )
        print(f"Available: {len(available)}/{len(config['symbols'])}")
        if missing:
            print("Missing:")
            for sym in missing:
                print(f"  {sym['exchange']}:{sym['symbol']}")
        else:
            print("All symbols have data.")
        return

    download_batch(
        args.config, incremental=args.incremental,
        dry_run=args.dry_run, pool=args.pool,
    )


if __name__ == "__main__":
    main()
