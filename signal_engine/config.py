"""Configuration loader.

- config.yaml  -> user-configurable settings (non-sensitive)
- .env         -> secrets only (API keys, tokens, phone numbers)

All values are loaded from config.yaml with no in-code defaults.
Missing keys or sections cause a ConfigError at startup.
"""

import os
from dataclasses import dataclass
from typing import Dict, Tuple, Union

import yaml
from dotenv import dotenv_values

_DIR = os.path.dirname(__file__)
_YAML_PATH = os.path.join(_DIR, "config.yaml")
_ENV_PATH = os.path.join(_DIR, ".env")

VALID_SIZING_MODES = ("fixed_fractional", "pct_of_capital")


class ConfigError(Exception):
    """Raised when config.yaml is missing, incomplete, or invalid."""


def _load_yaml() -> dict:
    if not os.path.exists(_YAML_PATH):
        raise ConfigError(
            f"config.yaml not found at {_YAML_PATH}. "
            "Copy config.sample.yaml and fill in all values."
        )
    with open(_YAML_PATH) as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        raise ConfigError("config.yaml is empty or invalid")
    return data


def _load_env() -> dict:
    if not os.path.exists(_ENV_PATH):
        return {}
    return dotenv_values(_ENV_PATH)


def _require_section(yml: dict, section: str) -> dict:
    """Extract a required top-level section from the YAML config."""
    value = yml.get(section)
    if value is None:
        raise ConfigError(
            f"Missing required section '{section}' in config.yaml"
        )
    if not isinstance(value, dict):
        raise ConfigError(
            f"Section '{section}' in config.yaml must be a mapping, got {type(value).__name__}"
        )
    return value


def _require_key(section_data: dict, section_name: str, key: str):
    """Extract a required key from a config section. Raises on missing."""
    if key not in section_data:
        raise ConfigError(
            f"Missing required key '{section_name}.{key}' in config.yaml"
        )
    return section_data[key]


@dataclass(frozen=True)
class TelegramChannel:
    """A single Telegram channel to listen to."""
    name: str
    id: Union[int, str]  # numeric chat ID or @username


@dataclass(frozen=True)
class Settings:
    # Secrets from .env
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    openalgo_base_url: str
    openalgo_api_key: str

    # Telegram channels (from yaml)
    telegram_channels: Tuple[TelegramChannel, ...]

    # Position sizing (from yaml)
    sizing_mode: str
    risk_per_trade: float
    pct_of_capital: float
    max_position_size: float
    min_entry_price: float
    max_entry_price: float
    slippage_factor: float
    margin_multiplier: Dict[str, float]
    max_capital_utilization: float

    # Risk management (from yaml)
    daily_loss_limit: float
    weekly_loss_limit: float
    monthly_loss_limit: float
    max_portfolio_heat: float
    max_open_positions: int
    max_trades_per_day: int
    min_rr: float
    duplicate_window_seconds: int
    stale_signal_seconds: int
    min_sl_pct: float

    # Capital override (from yaml) — 0 means fetch from OpenAlgo API
    sandbox_capital: float

    # Position tracking (from yaml)
    poll_interval: int

    # Broker / Exchange (from yaml)
    exchange: str
    product: str
    order_type: str

    # Listener (from yaml)
    listener_max_retries: int
    listener_base_backoff: int

    # API (from yaml)
    api_timeout: float

    # Bracket orders (from yaml)
    bracket_enabled: bool
    bracket_sl_order_type: str
    bracket_max_sl_retries: int
    bracket_cancel_retry_count: int


def _build_settings() -> Settings:
    yml = _load_yaml()
    env = _load_env()

    # Required top-level sections
    telegram = yml.get("telegram", {})
    sizing = _require_section(yml, "sizing")
    risk = _require_section(yml, "risk")
    tracking = _require_section(yml, "tracking")
    broker = _require_section(yml, "broker")
    listener = _require_section(yml, "listener")
    api = _require_section(yml, "api")
    bracket = _require_section(yml, "bracket")

    # Validate sizing mode
    mode = _require_key(sizing, "sizing", "mode")
    if mode not in VALID_SIZING_MODES:
        raise ConfigError(
            f"Invalid sizing.mode '{mode}'. Must be one of: {', '.join(VALID_SIZING_MODES)}"
        )

    # Parse channel list
    raw_channels = telegram.get("channels", [])
    channels = []
    for ch in raw_channels:
        raw_id = ch.get("id", "")
        try:
            ch_id = int(raw_id)
        except (ValueError, TypeError):
            ch_id = str(raw_id)
        channels.append(TelegramChannel(name=ch.get("name", ""), id=ch_id))

    # Load and validate margin_multiplier
    raw_multiplier = _require_key(sizing, "sizing", "margin_multiplier")
    if not isinstance(raw_multiplier, dict):
        raise ConfigError(
            "sizing.margin_multiplier must be a mapping of product -> margin fraction"
        )
    margin_multiplier: Dict[str, float] = {}
    for product_key, rate in raw_multiplier.items():
        rate_f = float(rate)
        if rate_f <= 0 or rate_f > 1.0:
            raise ConfigError(
                f"sizing.margin_multiplier[{product_key!r}] must be > 0 and <= 1.0, got {rate_f}"
            )
        margin_multiplier[product_key] = rate_f

    max_capital_utilization = float(
        _require_key(sizing, "sizing", "max_capital_utilization")
    )

    return Settings(
        # Secrets from .env
        telegram_api_id=int(env.get("TELEGRAM_API_ID", 0)),
        telegram_api_hash=env.get("TELEGRAM_API_HASH", ""),
        telegram_phone=env.get("TELEGRAM_PHONE", ""),
        openalgo_base_url=env.get("OPENALGO_BASE_URL", "http://127.0.0.1:5000"),
        openalgo_api_key=env.get("OPENALGO_API_KEY", ""),

        # Telegram channels from yaml
        telegram_channels=tuple(channels),

        # Position sizing from yaml — all required
        sizing_mode=mode,
        risk_per_trade=float(_require_key(sizing, "sizing", "risk_per_trade")),
        pct_of_capital=float(_require_key(sizing, "sizing", "pct_of_capital")),
        max_position_size=float(_require_key(sizing, "sizing", "max_position_size")),
        min_entry_price=float(_require_key(sizing, "sizing", "min_entry_price")),
        max_entry_price=float(_require_key(sizing, "sizing", "max_entry_price")),
        slippage_factor=float(_require_key(sizing, "sizing", "slippage_factor")),
        margin_multiplier=margin_multiplier,
        max_capital_utilization=max_capital_utilization,

        # Risk management from yaml — all required
        daily_loss_limit=float(_require_key(risk, "risk", "daily_loss_limit")),
        weekly_loss_limit=float(_require_key(risk, "risk", "weekly_loss_limit")),
        monthly_loss_limit=float(_require_key(risk, "risk", "monthly_loss_limit")),
        max_portfolio_heat=float(_require_key(risk, "risk", "max_portfolio_heat")),
        max_open_positions=int(_require_key(risk, "risk", "max_open_positions")),
        max_trades_per_day=int(_require_key(risk, "risk", "max_trades_per_day")),
        min_rr=float(_require_key(risk, "risk", "min_rr")),
        duplicate_window_seconds=int(_require_key(risk, "risk", "duplicate_window_seconds")),
        stale_signal_seconds=int(_require_key(risk, "risk", "stale_signal_seconds")),
        min_sl_pct=float(_require_key(risk, "risk", "min_sl_pct")),

        # Capital override from yaml
        sandbox_capital=float(_require_key(sizing, "sizing", "sandbox_capital")),

        # Tracking from yaml
        poll_interval=int(_require_key(tracking, "tracking", "poll_interval")),

        # Broker from yaml — all required
        exchange=_require_key(broker, "broker", "exchange"),
        product=_require_key(broker, "broker", "product"),
        order_type=_require_key(broker, "broker", "order_type"),

        # Listener from yaml
        listener_max_retries=int(_require_key(listener, "listener", "max_retries")),
        listener_base_backoff=int(_require_key(listener, "listener", "base_backoff")),

        # API from yaml
        api_timeout=float(_require_key(api, "api", "timeout")),

        # Bracket orders from yaml
        bracket_enabled=bool(_require_key(bracket, "bracket", "enabled")),
        bracket_sl_order_type=str(_require_key(bracket, "bracket", "sl_order_type")),
        bracket_max_sl_retries=int(_require_key(bracket, "bracket", "max_sl_retries")),
        bracket_cancel_retry_count=int(_require_key(bracket, "bracket", "cancel_retry_count")),
    )


settings = _build_settings()
