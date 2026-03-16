"""
Backtest configuration loader.

Follows the same fail-fast pattern as signal_engine/config.py.
"""

from dataclasses import fields
from pathlib import Path

import yaml

from backtest.costs import IndianCosts
from backtest.strategies.orb import ORBConfig


class ConfigError(Exception):
    """Raised when backtest configuration is invalid or missing."""


def _require_key(data: dict, key: str, section: str = "root") -> object:
    """Get a required key from config dict, raise on missing."""
    if key not in data:
        raise ConfigError(f"Missing required key '{key}' in section '{section}'")
    return data[key]


def load_config(config_path: str | Path) -> dict:
    """
    Load backtest configuration from YAML file.

    Args:
        config_path: Path to config YAML file.

    Returns:
        Dict with keys: symbol, exchange, interval, start_date, end_date,
        initial_capital, position_size_pct, orb_config, costs, product.

    Raises:
        ConfigError: On missing file, section, or key.
    """
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ConfigError(f"Config file is empty: {path}")

    # Required top-level keys
    config = {
        "symbol": str(_require_key(raw, "symbol")),
        "exchange": str(_require_key(raw, "exchange")),
        "interval": str(_require_key(raw, "interval")),
        "start_date": str(_require_key(raw, "start_date")),
        "end_date": str(_require_key(raw, "end_date")),
        "initial_capital": float(_require_key(raw, "initial_capital")),
        "position_size_pct": float(raw.get("position_size_pct", 0.10)),
        "product": str(raw.get("product", "MIS")),
    }

    # ORB strategy config
    orb_raw = raw.get("orb", {})
    orb_kwargs = {}
    for f in fields(ORBConfig):
        if f.name in orb_raw:
            orb_kwargs[f.name] = orb_raw[f.name]
    config["orb_config"] = ORBConfig(**orb_kwargs)

    # Cost model overrides
    costs_raw = raw.get("costs", {})
    costs_kwargs = {}
    for f in fields(IndianCosts):
        if f.name in costs_raw:
            costs_kwargs[f.name] = costs_raw[f.name]
    config["costs"] = IndianCosts(**costs_kwargs)

    return config


def _normalize_symbols(raw_symbols: list) -> list[dict[str, str]]:
    """Normalize symbol list: strings become {symbol, exchange: NSE}."""
    result = []
    for item in raw_symbols:
        if isinstance(item, str):
            result.append({"symbol": item, "exchange": "NSE"})
        elif isinstance(item, dict):
            if "symbol" not in item:
                raise ConfigError(f"Symbol entry missing 'symbol' key: {item}")
            result.append({
                "symbol": str(item["symbol"]),
                "exchange": str(item.get("exchange", "NSE")),
            })
        else:
            raise ConfigError(f"Invalid symbol entry: {item}")
    return result


def load_batch_config(config_path: str | Path) -> dict:
    """
    Load batch backtest configuration with a list of symbols.

    Args:
        config_path: Path to batch config YAML file.

    Returns:
        Dict with keys: symbols (list of {symbol, exchange}), interval,
        start_date, end_date, initial_capital, position_size_pct,
        strategy, orb_config, costs, product.

    Raises:
        ConfigError: On missing file, section, or key.
    """
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ConfigError(f"Config file is empty: {path}")

    # Require symbols list
    raw_symbols = _require_key(raw, "symbols")
    if not raw_symbols:
        raise ConfigError("symbols list is empty")

    symbols = _normalize_symbols(raw_symbols)

    config = {
        "symbols": symbols,
        "interval": str(_require_key(raw, "interval")),
        "start_date": str(_require_key(raw, "start_date")),
        "end_date": str(_require_key(raw, "end_date")),
        "initial_capital": float(_require_key(raw, "initial_capital")),
        "position_size_pct": float(raw.get("position_size_pct", 0.10)),
        "product": str(raw.get("product", "MIS")),
        "strategy": str(raw.get("strategy", "orb")),
    }

    # ORB strategy config
    orb_raw = raw.get("orb", {})
    orb_kwargs = {}
    for f in fields(ORBConfig):
        if f.name in orb_raw:
            orb_kwargs[f.name] = orb_raw[f.name]
    config["orb_config"] = ORBConfig(**orb_kwargs)

    # Cost model overrides
    costs_raw = raw.get("costs", {})
    costs_kwargs = {}
    for f in fields(IndianCosts):
        if f.name in costs_raw:
            costs_kwargs[f.name] = costs_raw[f.name]
    config["costs"] = IndianCosts(**costs_kwargs)

    return config
