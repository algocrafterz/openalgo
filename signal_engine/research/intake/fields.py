"""The field catalog: one frozen record per spec, execution or profile field.

Validation, the assumption ledger and the intake states all read this one table, so a field's
class and default materiality are defined exactly once. Classes follow checklist A1:

  BLOCKER     changes what the strategy does and cannot be safely guessed
  DERIVABLE   unambiguous from what the author gave, or inferable and recorded
  DEFAULTABLE execution infrastructure, resolved by the platform and recorded
  DISCLOSURE  affects how a result is read, never whether it runs
  OPTIONAL    research metadata
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

INTENT = "intent"
EXECUTION = "execution"
PROFILE = "profile"
SECTIONS = (INTENT, EXECUTION, PROFILE)

BLOCKER = "BLOCKER"
DERIVABLE = "DERIVABLE"
DEFAULTABLE = "DEFAULTABLE"
DISCLOSURE = "DISCLOSURE"
OPTIONAL = "OPTIONAL"
CLASSES = (BLOCKER, DERIVABLE, DEFAULTABLE, DISCLOSURE, OPTIONAL)

LOW = "low"
MEDIUM = "medium"
HIGH = "high"
MATERIALITIES = (LOW, MEDIUM, HIGH)

STYLES = ("intraday", "swing", "positional")
DIRECTIONS = ("long_only", "short_only", "both")
TIMEFRAMES = ("1m", "5m", "15m", "1h", "1d")
PRODUCTS = ("MIS", "CNC")
SOURCE_KINDS = ("video", "article", "pdf", "screenshot", "own_words")
SIGNAL_EVALUATION = ("bar_close", "bar_open", "intrabar", "tick")
ORDER_SUBMISSION = ("signal_close", "next_bar_open")
FILL_MODELS = ("next_bar_open", "signal_close", "limit_at_level")
PORTFOLIO_MODES = ("single_position", "multi_position")
PORTFOLIO_SCOPES = ("global", "per_symbol")
SELECTIONS = ("chronological", "rank_by_score", "equal_allocation")
CONFLICTS = ("skip_if_symbol_open", "close_and_reverse", "ignore_opposite_signal")
RE_ENTRY_TRIGGERS = ("after_stop", "after_target", "after_time_exit", "same_signal")
SIZING_BASES = ("fixed_notional", "risk")
DESTINATIONS = ("python", "pine_tradingview")
#: The universe declarations a spec can make, and the only value each key may hold for it. The
#: current F&O list is today's membership applied to the past: never point-in-time, always biased.
UNIVERSE_DECLARATIONS: dict[str, dict[str, Any]] = {
    "current_fno": {"exchange": "NSE", "membership_basis": "current", "point_in_time": False,
                    "survivorship_bias": True},
}
PRICE_SOURCES = ("open", "high", "low", "close", "hl2", "hlc3", "ohlc4", "volume")
REFERENCE_PRICES = (
    "entry_fill",
    "signal_close",
    "signal_high",
    "signal_low",
    "previous_close",
    "indicator_value",
)
ENTRY_ORDER_TYPES = ("market", "limit")
STOP_ORDER_TYPES = ("stop_market", "stop_limit")
TARGET_ORDER_TYPES = ("limit", "market")


@dataclass(frozen=True)
class FieldDef:
    """One field: where it lives, how it is classed and what it may hold."""

    name: str
    section: str
    cls: str
    materiality: str
    allowed: tuple[str, ...] | None = None
    none_ok: bool = False
    condition: str = ""


def _f(name, section, cls, materiality, allowed=None, none_ok=False, condition=""):
    return FieldDef(name, section, cls, materiality, allowed, none_ok, condition)


_MULTI = "portfolio.mode == multi_position"
_RE = "re_entry.allowed"

FIELDS: tuple[FieldDef, ...] = (
    _f("name", INTENT, BLOCKER, LOW),
    _f("source.kind", INTENT, BLOCKER, LOW, SOURCE_KINDS),
    _f("source.ref", INTENT, BLOCKER, LOW),
    _f("profile", INTENT, BLOCKER, HIGH),
    _f("style", INTENT, BLOCKER, HIGH, STYLES),
    _f("direction", INTENT, BLOCKER, HIGH, DIRECTIONS),
    _f("timeframe", INTENT, BLOCKER, HIGH, TIMEFRAMES),
    _f("product", INTENT, DERIVABLE, MEDIUM, PRODUCTS),
    _f("entry", INTENT, BLOCKER, HIGH),
    _f("exit", INTENT, BLOCKER, HIGH),
    _f("stop", INTENT, BLOCKER, HIGH, none_ok=True),
    _f("take_profit", INTENT, BLOCKER, HIGH, none_ok=True),
    _f("holding_period", INTENT, BLOCKER, MEDIUM),
    _f("indicators", INTENT, BLOCKER, MEDIUM, none_ok=True),
    _f("parameters", INTENT, BLOCKER, MEDIUM, none_ok=True),
    _f("timing.signal_evaluation", INTENT, DERIVABLE, HIGH, SIGNAL_EVALUATION),
    _f("timing.order_submission", INTENT, DERIVABLE, HIGH, ORDER_SUBMISSION),
    _f("timing.fill_model", INTENT, DERIVABLE, HIGH, FILL_MODELS),
    _f("reference_prices", INTENT, OPTIONAL, MEDIUM, REFERENCE_PRICES),
    _f("filters", INTENT, BLOCKER, MEDIUM, none_ok=True),
    _f("session_window", INTENT, BLOCKER, MEDIUM, none_ok=True),
    _f("re_entry.allowed", INTENT, BLOCKER, MEDIUM),
    _f("re_entry.max_per_symbol_day", INTENT, BLOCKER, MEDIUM, condition=_RE),
    _f("re_entry.trigger", INTENT, DEFAULTABLE, LOW, RE_ENTRY_TRIGGERS, condition=_RE),
    _f("sizing.risk_per_trade_pct", INTENT, BLOCKER, HIGH),
    _f("sizing.capital_per_trade", INTENT, BLOCKER, HIGH),
    _f("sizing.basis", INTENT, OPTIONAL, HIGH, SIZING_BASES),
    _f("data.frequency", INTENT, OPTIONAL, HIGH, TIMEFRAMES),
    _f("data.field_families", INTENT, OPTIONAL, HIGH),
    _f("data.min_n_minutes", INTENT, OPTIONAL, HIGH),
    _f("data.complete_bins", INTENT, OPTIONAL, HIGH),
    _f("information_boundary", INTENT, OPTIONAL, MEDIUM),
    _f("portfolio.mode", INTENT, BLOCKER, HIGH, PORTFOLIO_MODES),
    _f("portfolio.scope", INTENT, DERIVABLE, HIGH, PORTFOLIO_SCOPES, condition="single_position"),
    _f("portfolio.max_concurrent", INTENT, BLOCKER, HIGH, condition=_MULTI),
    _f("portfolio.max_trades_per_day", INTENT, BLOCKER, HIGH, condition=_MULTI),
    _f("portfolio.capital_per_position", INTENT, BLOCKER, HIGH, condition=_MULTI),
    _f("portfolio.max_exposure", INTENT, BLOCKER, HIGH, condition=_MULTI),
    _f("portfolio.selection", INTENT, BLOCKER, HIGH, SELECTIONS, condition=_MULTI),
    _f("portfolio.conflict", INTENT, BLOCKER, HIGH, CONFLICTS, condition=_MULTI),
    _f("portfolio.sector_limit", INTENT, OPTIONAL, MEDIUM, condition=_MULTI),
    _f("portfolio.correlation_limit", INTENT, OPTIONAL, MEDIUM, condition=_MULTI),
    _f("order_types", INTENT, OPTIONAL, MEDIUM),
    _f("costs.reference_round_trip_bps", INTENT, DISCLOSURE, LOW),
    _f("disclosure.prior_variants", INTENT, DISCLOSURE, LOW),
    _f("disclosure.chart_peeking", INTENT, DISCLOSURE, LOW),
    _f("disclosure.destination", INTENT, DISCLOSURE, LOW, DESTINATIONS),
    _f("universe", INTENT, OPTIONAL, HIGH),
    _f("rationale", INTENT, OPTIONAL, LOW),
    _f("kill_criterion", INTENT, OPTIONAL, LOW),
    _f("execution.product", EXECUTION, DERIVABLE, MEDIUM, PRODUCTS),
    _f("execution.bar_size", EXECUTION, DERIVABLE, MEDIUM, TIMEFRAMES),
    _f("execution.warmup_bars", EXECUTION, DERIVABLE, MEDIUM),
    _f("execution.slippage_bps_per_side", EXECUTION, DEFAULTABLE, MEDIUM),
    _f("execution.cost_bps_round_trip", EXECUTION, DEFAULTABLE, MEDIUM),
    _f("execution.oos_fraction", EXECUTION, DEFAULTABLE, LOW),
    _f("execution.embargo_sessions", EXECUTION, DEFAULTABLE, LOW),
    _f("execution.eligibility_min_prior_sessions", EXECUTION, DEFAULTABLE, MEDIUM),
    _f("execution.eligibility_min_volume", EXECUTION, DEFAULTABLE, MEDIUM),
    _f("execution.eligibility_min_price", EXECUTION, DEFAULTABLE, MEDIUM),
    _f("profile.mis_squareoff_min", PROFILE, DEFAULTABLE, MEDIUM),
    _f("profile.squareoff_margin_min", PROFILE, DEFAULTABLE, MEDIUM),
    _f("profile.time_exit_min", PROFILE, DEFAULTABLE, MEDIUM),
    _f("profile.tick_grid", PROFILE, DEFAULTABLE, MEDIUM),
    _f("profile.carry_forward", PROFILE, DEFAULTABLE, MEDIUM),
)

_INDEX = {f.name: f for f in FIELDS}


def get(name: str) -> FieldDef:
    """The catalog entry for a dotted field name. Raises KeyError for an unknown field."""
    return _INDEX[name]


def top_level(section: str) -> frozenset[str]:
    """Top-level key names (the part before the first dot) of every field in a section."""
    return frozenset(f.name.split(".")[0] for f in FIELDS if f.section == section)
