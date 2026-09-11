"""Configuration loader.

- config.yaml  -> user-configurable settings (non-sensitive)
- .env         -> secrets only (API keys, tokens, phone numbers)

All values are loaded from config.yaml with no in-code defaults.
Missing keys or sections cause a ConfigError at startup.
"""

import os
from dataclasses import dataclass
from typing import Union

import yaml
from dotenv import dotenv_values

_DIR = os.path.dirname(__file__)
_YAML_PATH = os.path.join(_DIR, "config.yaml")
_SECTORS_PATH = os.path.join(_DIR, "sectors.yaml")
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


def _load_sectors() -> dict[str, list[str]]:
    """Load sector-to-symbol mapping from sectors.yaml (optional file)."""
    if not os.path.exists(_SECTORS_PATH):
        return {}
    with open(_SECTORS_PATH) as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        return {}
    return {sector: list(symbols) for sector, symbols in data.items()}


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
    """A single Telegram channel to listen to.

    `enabled` is what decides whether the engine SUBSCRIBES to it. A disabled channel is
    still parsed and still carried in settings so startup can report it as deliberately off
    — the point is to make a paper-phase strategy a statement in the file rather than an
    absence from it. BREAKOUT spent its first fortnight in that gap: breakout.pine posted to
    intraday-breakout while telegram.channels listed only smidestn and intraday-orb, and
    nothing recorded that this was on purpose.
    """
    name: str
    id: int | str  # numeric chat ID or @username
    enabled: bool = True


@dataclass(frozen=True)
class Settings:
    # Secrets from .env
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    openalgo_base_url: str
    openalgo_api_key: str

    # Telegram channels (from yaml)
    telegram_channels: tuple[TelegramChannel, ...]
    #: Admin/system channel (startup, shutdown, risk halts, order lifecycle, day summary) -
    #: keyed "analyze"/"live", same split and same reasoning as breakingtrade_btst_channels
    #: below: system chatter from the paper phase must never sit in the same channel as a
    #: real-money alert. notifier.py picks the entry matching OpenAlgo's current mode.
    notify_channel: dict[str, TelegramChannel]
    #: BTST destinations for signal_engine/analysis/breakingtrade/alerts.py - keyed "analyze"/
    #: "live". Not part of telegram_channels because BTST is a manual daily decision with no
    #: engine-listener counterpart (see alerts.py's chat_id_for()). A phase missing from
    #: config.yaml is simply absent from this dict, not an error - alerts.py treats that as
    #: "record, don't deliver", same as any other unconfigured channel.
    breakingtrade_btst_channels: dict[str, TelegramChannel]
    #: Capital the BTST strategy is allocated, split equally across the day's names.
    #: Reporting only - see config.yaml's `btst:` block.
    btst_capital: float
    #: quiet | normal | verbose — how much routine traffic reaches notify_channel.
    #: Failure events ignore it entirely; see notifier.EVENT_LEVELS.
    notify_level: str

    # Position sizing (from yaml)
    sizing_mode: str
    risk_per_trade: float
    pct_of_capital: float
    min_entry_price: float
    max_entry_price: float
    slippage_factor: float
    max_sl_pct_for_sizing: float  # SL cap for qty calculation; 0 = disabled

    # Risk management (from yaml)
    daily_loss_limit: float
    weekly_loss_limit: float
    monthly_loss_limit: float
    max_open_positions: int
    max_trades_per_day: int
    min_rr: float
    duplicate_window_seconds: int
    stale_signal_seconds: int
    min_sl_pct: float

    # Correlation risk (from yaml)
    max_positions_per_symbol: int
    max_positions_per_sector: int
    sectors: dict[str, list[str]]

    # Capital override (from yaml) — 0 means fetch from OpenAlgo API
    sandbox_capital: float
    #: Per-mode risk-limit overrides, {"live": {...}, "analyze": {...}}. Empty when the
    #: config has no mode_profiles block, which leaves the base risk: values in force.
    mode_profiles: dict[str, dict]

    # Day-start capital caching (from yaml)
    use_day_start_capital: bool  # Cache capital at first signal, use for all trades

    # Test mode qty cap (from yaml) — 0 = disabled
    test_qty_cap: int

    # Minimum live capital required before placing any new entry (from yaml)
    # Prevents dwarf positions and broker rejections when capital is nearly depleted
    min_capital_for_entry: float

    # Position tracking (from yaml)
    poll_interval: int
    tracker_min_position_age_seconds: int
    tracker_guard2_timeout_minutes: int  # max minutes Guard 2 waits for ambiguous order status

    # Broker / Exchange (from yaml)
    exchange: str
    product: str
    order_type: str
    allow_off_hours_testing: bool
    mis_margin_pct: float  # NSE/BSE equity MIS margin % for live capital floor check

    # Broker pre-reject list (from yaml) — symbols the broker is known to reject for MIS.
    # Skipped before order placement to avoid wasted slot + Telegram rejection notice.
    # See FLATTRADE-RESTRICTIONS.md for source / maintenance procedure.
    broker_mis_rejected: frozenset

    # Listener (from yaml)
    listener_max_retries: int
    listener_base_backoff: int

    # API (from yaml)
    api_timeout: float
    margin_api_retries: int

    # Bracket orders (from yaml)
    bracket_enabled: bool
    bracket_cnc_sl_enabled: bool  # CNC SL-M cancelled at EOD by NSE; false = skip bracket for CNC
    bracket_sl_order_type: str
    bracket_max_sl_retries: int
    bracket_retry_delay: float
    bracket_tp_exit_retries: int
    tp1_runner_sl_buffer: float  # fraction of R to set below TP1 for runner SL after partial exit
    use_extended_runner_tiers: bool  # ratchet runner SL to the last TP level hit, not always TP1

    # Strategy profiles (from yaml) — per-strategy TP levels and product defaults
    # Keys: strategy tag (e.g. "ORB", "RSI-TP-MR")
    # Values: dict with "tp_levels" (e.g. {"TP1": 0.5, "TP2": 1.0}), "product" (e.g. "CNC"),
    # an optional "min_sl_pct" overriding the global stop-distance floor, and optional
    # "min_entry_price"/"max_entry_price" overriding the global price band (0 = no filter,
    # same convention as the global sizing.min_entry_price/max_entry_price).
    strategy_profiles: dict[str, dict]

    # Symbol blacklist (from yaml) — per-strategy + _global
    # Keys: strategy tag (e.g. "ORB", "RSI-TP-MR") or "_GLOBAL"
    # Values: frozenset of uppercase symbol names — HARD blocklist only.
    # Validator rejects signals for these symbols (IGNORED status, no order placed).
    blacklist: dict[str, frozenset]

    # Soft blacklist (from yaml) — per-strategy regime-sensitive list.
    # Risk engine reduces qty by soft_blacklist_multipliers[strategy] for these symbols.
    # Keys: strategy tag (uppercase). Values: frozenset of uppercase symbol names.
    soft_blacklist: dict[str, frozenset]

    # Soft blacklist qty multipliers per strategy (from yaml). Default 0.5 (50% qty).
    # Keys: strategy tag (uppercase). Values: float in (0, 1].
    soft_blacklist_multipliers: dict[str, float]

    # Time exit (from yaml) — close positions before broker auto square-off
    time_exit_enabled: bool
    time_exit_hour: int
    time_exit_minute: int

    # No-progress detection (from yaml) — move SL to break-even on stuck trades
    no_progress_enabled: bool
    no_progress_check_after_minutes: int
    no_progress_min_progress_pct: float
    no_progress_profit_lock_ratio: float  # 0.0 = strict break-even; 0.4 = lock 40% of unrealized profit
    no_progress_ab_test_disable: bool     # When true, the entire no-progress check is skipped
    # Early no-progress gate — independent of the main gate. Fires earlier with
    # a lower progress threshold to catch catastrophic stalls.
    no_progress_early_check_enabled: bool
    no_progress_early_check_after_minutes: int
    no_progress_early_min_progress_pct: float
    # Adaptive chop tightener — shortens early gate after N no-progress firings today.
    no_progress_chop_tightener_enabled: bool
    no_progress_chop_tightener_trigger_count: int
    no_progress_chop_tightener_early_check_after_minutes: int
    # Use broker fill_price for progress calc (more accurate). False = signal entry (legacy).
    no_progress_use_fill_price: bool
    # Loss-cut gate: exit when progress drops below a deep negative threshold.
    no_progress_loss_cut_enabled: bool
    no_progress_loss_cut_min_age_minutes: int
    no_progress_loss_cut_progress_threshold: float  # negative, e.g. -0.80


def _parse_no_progress(cfg: dict) -> dict:
    """Parse the no_progress config section into Settings keyword args."""
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "no_progress_enabled": bool(cfg.get("enabled", False)),
        "no_progress_check_after_minutes": int(cfg.get("check_after_minutes", 90)),
        "no_progress_min_progress_pct": float(cfg.get("min_progress_pct", 0.20)),
        "no_progress_profit_lock_ratio": float(cfg.get("profit_lock_ratio", 0.0)),
        "no_progress_ab_test_disable": bool(cfg.get("ab_test_disable", False)),
        "no_progress_early_check_enabled": bool(cfg.get("early_check_enabled", False)),
        "no_progress_early_check_after_minutes": int(cfg.get("early_check_after_minutes", 45)),
        "no_progress_early_min_progress_pct": float(cfg.get("early_min_progress_pct", 0.05)),
        "no_progress_chop_tightener_enabled": bool(cfg.get("chop_tightener_enabled", False)),
        "no_progress_chop_tightener_trigger_count": int(cfg.get("chop_tightener_trigger_count", 2)),
        "no_progress_chop_tightener_early_check_after_minutes": int(
            cfg.get("chop_tightener_early_check_after_minutes", 30)
        ),
        "no_progress_use_fill_price": bool(cfg.get("use_fill_price_for_progress", True)),
        "no_progress_loss_cut_enabled": bool(cfg.get("loss_cut_enabled", False)),
        "no_progress_loss_cut_min_age_minutes": int(cfg.get("loss_cut_min_age_minutes", 20)),
        "no_progress_loss_cut_progress_threshold": float(cfg.get("loss_cut_progress_threshold", -0.80)),
    }


def _symbol_set(raw) -> frozenset:
    """Normalise a YAML symbol list into an upper-cased frozenset."""
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(s.upper().strip() for s in raw if isinstance(s, str))


def _parse_blacklist(
    raw: dict,
) -> tuple[dict[str, frozenset], dict[str, frozenset], dict[str, float]]:
    """Parse the blacklist section into (hard, soft, soft_multipliers).

    Two accepted shapes per strategy key:

    1. Flat list (legacy / _global):
           ORB:
             - BHEL
             - GAIL
       Treated as hard blocklist; no soft entries.

    2. Tiered dict:
           ORB:
             hard: [BHEL, GAIL]
             soft: [CANBK, FEDERALBNK]
             soft_multiplier: 0.5

    Returns three dicts keyed by uppercase strategy tag:
      hard: dict[str, frozenset]   — fully-blocked symbols
      soft: dict[str, frozenset]   — qty-reduced symbols
      multipliers: dict[str, float] — per-strategy soft multiplier (default 0.5)
    """
    if not isinstance(raw, dict):
        raw = {}

    hard: dict[str, frozenset] = {}
    soft: dict[str, frozenset] = {}
    multipliers: dict[str, float] = {}

    for strategy_key, value in raw.items():
        key = strategy_key.upper()

        if isinstance(value, list):
            hard[key] = _symbol_set(value)
            continue
        if not isinstance(value, dict):
            continue

        multiplier = _soft_multiplier(strategy_key, value)
        if isinstance(value.get("hard", []), list):
            hard[key] = _symbol_set(value.get("hard", []))
        soft_symbols = _symbol_set(value.get("soft", []))
        if soft_symbols:
            soft[key] = soft_symbols
            multipliers[key] = multiplier

    return hard, soft, multipliers


def _soft_multiplier(strategy_key: str, value: dict) -> float:
    """Per-strategy qty multiplier for soft-blacklisted symbols. Must be in [0, 1]."""
    multiplier = float(value.get("soft_multiplier", 0.5))
    if not 0.0 <= multiplier <= 1.0:
        raise ConfigError(
            f"blacklist.{strategy_key}.soft_multiplier must be in [0, 1], got {multiplier}"
        )
    return multiplier


def _parse_broker_mis_rejected(raw: dict) -> frozenset:
    """Parse broker_restrictions.<broker>.mis_rejected into an uppercase frozenset.

    The active broker is taken from broker.product/exchange downstream — for now we
    union all configured broker entries so the filter is conservative even if the
    operator switches brokers without updating both sections.
    """
    if not isinstance(raw, dict):
        return frozenset()
    symbols: set[str] = set()
    for _broker_name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        rejected = cfg.get("mis_rejected", [])
        if not isinstance(rejected, list):
            continue
        for s in rejected:
            if isinstance(s, str) and s.strip():
                symbols.add(s.strip().upper())
    return frozenset(symbols)


#: Strings YAML users write meaning False. Anything unrecognised falls back to truthiness,
#: so a genuine bool from the YAML parser is used as-is.
_FALSEY = {"false", "no", "off", "0", ""}


def _channel_enabled(raw: dict) -> bool:
    """Whether the engine subscribes to this channel. Absent key means yes.

    Quoted YAML (`enabled: "false"`) parses as a non-empty string, which is truthy — the one
    way to write this key and get the exact opposite of what it says. Handled explicitly.
    """
    value = raw.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY
    return bool(value)


def _parse_channel(raw: dict) -> TelegramChannel:
    """Build a TelegramChannel, keeping the id numeric when it parses as one."""
    raw_id = raw.get("id", "")
    try:
        ch_id = int(raw_id)
    except (ValueError, TypeError):
        ch_id = str(raw_id)
    return TelegramChannel(
        name=raw.get("name", ""), id=ch_id, enabled=_channel_enabled(raw)
    )


#: Phase suffixes a strategy channel carries. Mirrors listener._PHASE_SUFFIXES; kept here
#: too so config can be validated without importing the listener (and telethon with it).
_PHASE_SUFFIXES = ("analyze", "live")


def _split_phase(name: str) -> "tuple[str, str] | None":
    """(base, phase) for a suffixed channel name, else None."""
    lowered = (name or "").strip().lower()
    for phase in _PHASE_SUFFIXES:
        if lowered.endswith(f"-{phase}"):
            return lowered[: -(len(phase) + 1)], phase
    return None


def validate_channels(channels) -> list:
    """Problems with the `telegram.channels` block, as human-readable strings.

    None of these used to be checked at all, and every one of them fails SILENTLY:

    - A duplicate NAME makes notifier._channel_for_strategy() and alerts.chat_id_for()
      resolve to whichever entry comes first in the file.
    - A duplicate ID double-subscribes the listener to one chat.
    - An `-analyze` channel with no `-live` twin (or the reverse) is a promotion waiting to
      fail: the day the mode flips, that strategy's signals are refused by the phase gate
      with no channel to move them to.
    - BOTH phases enabled for one strategy means whichever phase OpenAlgo happens to be in
      trades and the other's alerts are refused - almost always a half-finished promotion
      rather than an intent.

    Returns [] for a clean config. Reported at startup rather than raised: a config that
    still trades correctly should warn, not refuse to start.
    """
    problems = []

    seen_names = {}
    seen_ids = {}
    for ch in channels:
        key = (ch.name or "").strip().lower()
        if key in seen_names:
            problems.append(f"Duplicate channel name '{ch.name}' - only the first is ever used")
        seen_names[key] = ch
        if ch.id in seen_ids:
            problems.append(
                f"Duplicate channel id {ch.id} ('{seen_names[key].name}' and "
                f"'{seen_ids[ch.id].name}') - the listener would subscribe twice"
            )
        seen_ids[ch.id] = ch

    by_base: dict[str, dict[str, TelegramChannel]] = {}
    for ch in channels:
        split = _split_phase(ch.name)
        if split is None:
            continue  # unsuffixed channels are phase-agnostic by design
        base, phase = split
        by_base.setdefault(base, {})[phase] = ch

    for base, phases in sorted(by_base.items()):
        for phase in _PHASE_SUFFIXES:
            if phase not in phases:
                problems.append(
                    f"Channel '{base}-{phase}' is missing - '{base}' has only the "
                    f"'{'/'.join(sorted(phases))}' phase configured"
                )
        if all(ch.enabled for ch in phases.values()) and len(phases) == len(_PHASE_SUFFIXES):
            problems.append(
                f"'{base}' has BOTH phases enabled - only the phase matching OpenAlgo's "
                "current mode will trade, the other's signals are refused"
            )

    return problems


def enabled_channels(
    channels: "tuple[TelegramChannel, ...]",
) -> "tuple[TelegramChannel, ...]":
    """The subset the listener actually subscribes to."""
    return tuple(ch for ch in channels if ch.enabled)


def resolve_mode_profile(base: dict, profiles: dict, trade_mode: str) -> dict:
    """Base risk limits with the named mode's overrides layered on top.

    One config serving both modes meant the paper week either ran with live limits -- losing
    the OUTCOME of every signal those limits refused, which is most of what a paper week is
    for -- or someone loosened the live numbers by hand and had to remember to restore them.
    The second is the dangerous one.

    An unknown mode returns base unchanged rather than guessing: a typo in the profile name
    must not silently hand a live account the paper limits. Base is never mutated.
    """
    override = (profiles or {}).get((trade_mode or "").lower())
    if not isinstance(override, dict):
        return dict(base)
    return {**base, **override}


def _parse_mode_profiles(yml: dict) -> dict[str, dict]:
    """Optional `mode_profiles:` block. Absent means "no overrides", not an error."""
    raw = yml.get("mode_profiles", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(mode).lower(): dict(values)
        for mode, values in raw.items()
        if isinstance(values, dict)
    }


def _parse_strategy_profiles(yml: dict) -> dict[str, dict]:
    """Per-strategy TP levels and product. Optional section — empty if missing."""
    raw_profiles = yml.get("strategy_profiles", {})
    if not isinstance(raw_profiles, dict):
        return {}
    profiles: dict[str, dict] = {}
    for strategy_key, profile in raw_profiles.items():
        if not isinstance(profile, dict):
            continue
        tp_levels = profile.get("tp_levels", {})
        if isinstance(tp_levels, dict):
            tp_levels = {k.upper(): float(v) for k, v in tp_levels.items()}
        else:
            tp_levels = {}
        entry = {
            "tp_levels": tp_levels,
            "product": str(profile.get("product", "")),
        }
        # Optional per-strategy stop-distance floor. Absent means "use the global one", which
        # is not the same as 0.0 (0.0 disables the check for this strategy), so the key is
        # only added when it is actually present.
        if profile.get("min_sl_pct") is not None:
            entry["min_sl_pct"] = float(profile["min_sl_pct"])
        # Optional per-strategy price band, same "absent means inherit the global value"
        # convention as min_sl_pct above. A scanner-selected universe (BreakingTrade) has no
        # natural price band of its own the way a fixed-universe strategy's execution-cost
        # calibration does, so it needs to be able to opt OUT of the global band (0 = no
        # filter) rather than silently inherit whatever band was tuned for a different strategy.
        if profile.get("min_entry_price") is not None:
            entry["min_entry_price"] = float(profile["min_entry_price"])
        if profile.get("max_entry_price") is not None:
            entry["max_entry_price"] = float(profile["max_entry_price"])
        profiles[strategy_key.upper()] = entry
    return profiles


def _secret_fields(env: dict) -> dict:
    """Credentials — always from .env, never from config.yaml."""
    return {
        "telegram_api_id": int(env.get("TELEGRAM_API_ID", 0)),
        "telegram_api_hash": env.get("TELEGRAM_API_HASH", ""),
        "telegram_phone": env.get("TELEGRAM_PHONE", ""),
        "openalgo_base_url": env.get("OPENALGO_BASE_URL", "http://127.0.0.1:5000"),
        "openalgo_api_key": env.get("OPENALGO_API_KEY", ""),
    }


def _parse_phase_channels(raw: dict) -> dict[str, TelegramChannel]:
    """{"analyze": {...}, "live": {...}} -> {"analyze": TelegramChannel, "live": TelegramChannel},
    keeping only whichever phase is actually present - a phase not yet configured (a strategy
    not yet promoted to live) is simply absent from the result, not an error."""
    raw = raw or {}
    return {
        phase: _parse_channel(raw[phase])
        for phase in ("analyze", "live")
        if isinstance(raw.get(phase), dict)
    }


def _btst_fields(yml: dict) -> dict:
    """Optional `btst:` block. Absent means the documented default, not an error - this is a
    reporting knob, and a missing one must not stop the engine starting."""
    block = yml.get("btst") or {}
    return {"btst_capital": float(block.get("capital", 100000))}


def _telegram_fields(telegram: dict) -> dict:
    """Signal channels to listen on, plus the admin/system and BTST channel pairs."""
    return {
        "telegram_channels": tuple(_parse_channel(ch) for ch in telegram.get("channels", [])),
        "notify_channel": _parse_phase_channels(telegram.get("notify_channel")),
        "notify_level": str(telegram.get("notify_level", "normal")).strip().lower(),
        "breakingtrade_btst_channels": _parse_phase_channels(telegram.get("breakingtrade_btst_channels")),
    }


def _sizing_fields(sizing: dict, mode: str) -> dict:
    """Position sizing — how many shares a signal turns into."""
    return {
        "sizing_mode": mode,
        "risk_per_trade": float(_require_key(sizing, "sizing", "risk_per_trade")),
        "pct_of_capital": float(_require_key(sizing, "sizing", "pct_of_capital")),
        "min_entry_price": float(_require_key(sizing, "sizing", "min_entry_price")),
        "max_entry_price": float(_require_key(sizing, "sizing", "max_entry_price")),
        "slippage_factor": float(_require_key(sizing, "sizing", "slippage_factor")),
        "max_sl_pct_for_sizing": float(sizing.get("max_sl_pct_for_sizing", 0.0)),
        "sandbox_capital": float(_require_key(sizing, "sizing", "sandbox_capital")),
        "use_day_start_capital": bool(sizing.get("use_day_start_capital", False)),
        "test_qty_cap": int(sizing.get("test_qty_cap", 0)),
        "min_capital_for_entry": float(_require_key(sizing, "sizing", "min_capital_for_entry")),
    }


def _risk_fields(risk: dict) -> dict:
    """Loss limits, slot caps, signal quality floors, concentration limits."""
    return {
        "daily_loss_limit": float(_require_key(risk, "risk", "daily_loss_limit")),
        "weekly_loss_limit": float(_require_key(risk, "risk", "weekly_loss_limit")),
        "monthly_loss_limit": float(_require_key(risk, "risk", "monthly_loss_limit")),
        "max_open_positions": int(_require_key(risk, "risk", "max_open_positions")),
        "max_trades_per_day": int(_require_key(risk, "risk", "max_trades_per_day")),
        "min_rr": float(_require_key(risk, "risk", "min_rr")),
        "duplicate_window_seconds": int(_require_key(risk, "risk", "duplicate_window_seconds")),
        "stale_signal_seconds": int(_require_key(risk, "risk", "stale_signal_seconds")),
        "min_sl_pct": float(_require_key(risk, "risk", "min_sl_pct")),
        "max_positions_per_symbol": int(_require_key(risk, "risk", "max_positions_per_symbol")),
        "max_positions_per_sector": int(_require_key(risk, "risk", "max_positions_per_sector")),
        "sectors": _load_sectors(),
    }


def _tracking_fields(tracking: dict) -> dict:
    """Position polling cadence and the close-detection guard thresholds."""
    return {
        "poll_interval": int(_require_key(tracking, "tracking", "poll_interval")),
        "tracker_min_position_age_seconds": int(tracking.get("min_position_age_seconds", 30)),
        "tracker_guard2_timeout_minutes": int(tracking.get("guard2_timeout_minutes", 30)),
    }


def _broker_fields(broker: dict, yml: dict) -> dict:
    """Exchange/product defaults and the broker's known MIS reject list."""
    return {
        "exchange": _require_key(broker, "broker", "exchange"),
        "product": _require_key(broker, "broker", "product"),
        "order_type": _require_key(broker, "broker", "order_type"),
        "allow_off_hours_testing": bool(broker.get("allow_off_hours_testing", False)),
        "mis_margin_pct": float(_require_key(broker, "broker", "mis_margin_pct")),
        "broker_mis_rejected": _parse_broker_mis_rejected(yml.get("broker_restrictions", {})),
    }


def _bracket_fields(bracket: dict) -> dict:
    """SL bracket leg behaviour, retries, and the runner-SL buffer."""
    return {
        "bracket_enabled": bool(_require_key(bracket, "bracket", "enabled")),
        "bracket_cnc_sl_enabled": bool(bracket.get("cnc_sl_enabled", False)),
        "bracket_sl_order_type": str(_require_key(bracket, "bracket", "sl_order_type")),
        "bracket_max_sl_retries": int(_require_key(bracket, "bracket", "max_sl_retries")),
        "bracket_retry_delay": float(bracket.get("retry_delay", 0.5)),
        "bracket_tp_exit_retries": int(bracket.get("tp_exit_retries", 3)),
        "tp1_runner_sl_buffer": float(bracket.get("tp1_runner_sl_buffer", 0.3)),
        "use_extended_runner_tiers": bool(bracket.get("use_extended_runner_tiers", False)),
    }


def _time_exit_fields(yml: dict) -> dict:
    """Square-off schedule. Optional section — defaults to disabled if missing."""
    time_exit = yml.get("time_exit", {})
    if not isinstance(time_exit, dict):
        time_exit = {}
    return {
        "time_exit_enabled": bool(time_exit.get("enabled", False)),
        "time_exit_hour": int(time_exit.get("hour", 15)),
        "time_exit_minute": int(time_exit.get("minute", 0)),
    }


def _build_settings() -> Settings:
    """Assemble the Settings singleton, one builder per config.yaml section."""
    yml = _load_yaml()
    env = _load_env()

    telegram = yml.get("telegram", {})
    sizing = _require_section(yml, "sizing")
    risk = _require_section(yml, "risk")
    tracking = _require_section(yml, "tracking")
    broker = _require_section(yml, "broker")
    listener = _require_section(yml, "listener")
    api = _require_section(yml, "api")
    bracket = _require_section(yml, "bracket")

    mode = _require_key(sizing, "sizing", "mode")
    if mode not in VALID_SIZING_MODES:
        raise ConfigError(
            f"Invalid sizing.mode '{mode}'. Must be one of: {', '.join(VALID_SIZING_MODES)}"
        )

    # Two-tier blacklist: hard = full block (validator), soft = qty reduction (risk engine).
    blacklist, soft_blacklist, soft_blacklist_multipliers = _parse_blacklist(
        yml.get("blacklist", {})
    )

    return Settings(
        **_secret_fields(env),
        **_telegram_fields(telegram),
        **_btst_fields(yml),
        **_sizing_fields(sizing, mode),
        **_risk_fields(risk),
        **_tracking_fields(tracking),
        **_broker_fields(broker, yml),
        **_bracket_fields(bracket),
        **_time_exit_fields(yml),
        listener_max_retries=int(_require_key(listener, "listener", "max_retries")),
        listener_base_backoff=int(_require_key(listener, "listener", "base_backoff")),
        api_timeout=float(_require_key(api, "api", "timeout")),
        margin_api_retries=int(api.get("margin_retries", 3)),
        strategy_profiles=_parse_strategy_profiles(yml),
        mode_profiles=_parse_mode_profiles(yml),
        blacklist=blacklist,
        soft_blacklist=soft_blacklist,
        soft_blacklist_multipliers=soft_blacklist_multipliers,
        **_parse_no_progress(yml.get("no_progress", {})),
    )


settings = _build_settings()
