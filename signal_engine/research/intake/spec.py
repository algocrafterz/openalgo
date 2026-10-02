"""Spec v2: strategy intent only, validated without guessing.

`ideas/<idea>/spec.yaml` holds what the strategy does. Execution settings and broker rules are
not here (see `execution.py` and `profiles.py`). An incomplete strategy is clarified, never
completed by assumption: the checks are mechanical and cannot judge whether a rule is good,
only whether it is concrete enough to code.

Validation is a pure function returning every problem, never only the first. Each problem is
tagged:

  validity  the spec is malformed or self-contradictory (a configuration error)
  intake    something is missing or ambiguous, so the author must clarify it
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from signal_engine.research.intake import fields
from signal_engine.research.intake.profiles import ProfileError, load_profile

SCHEMA_VERSION = 2

#: Statuses at which the registry demands a spec. An inbox idea is a raw capture and a rejected
#: or shelved one is finished; the record gate is stricter. Replaced by `stage` in build order 2.
SPEC_ACTIVE_STATUSES = ("evaluating", "backtested", "paper", "live")

VALIDITY = "validity"
INTAKE = "intake"

VAGUE_WORDS = (
    "strong",
    "strongly",
    "weak",
    "good",
    "bad",
    "decent",
    "significant",
    "significantly",
    "sufficient",
    "sufficiently",
    "enough",
    "large",
    "small",
    "big",
    "clear",
    "clearly",
    "obvious",
    "obviously",
    "reasonable",
    "appropriate",
    "roughly",
    "approximately",
    "about",
    "around",
    "looks",
    "appears",
    "seems",
    "somewhat",
    "discretion",
    "discretionary",
    "subjective",
)
_VAGUE = re.compile(r"\b(" + "|".join(VAGUE_WORDS) + r")\b", re.IGNORECASE)
_DIGIT = re.compile(r"\d")
_SAME_SESSION = re.compile(r"\b(same[ _]session|eod)\b", re.IGNORECASE)
_RANGE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")

#: Indicators that read a price series and default to the close when no source is given.
SOURCE_DEFAULTS_TO_CLOSE = frozenset(
    {"EMA", "SMA", "WMA", "RSI", "MACD", "BBANDS", "BOLLINGER", "ROC", "MOMENTUM", "STDEV"}
)
#: Indicators defined on high, low and close (or volume), so no single source applies.
SOURCE_NOT_NEEDED = frozenset(
    {"ATR", "ADX", "SUPERTREND", "OBV", "STOCH", "DONCHIAN", "KELTNER", "CCI", "MFI"}
)
#: Indicators whose value depends on where they start, so an anchor is mandatory.
NEEDS_ANCHOR = frozenset({"VWAP", "ANCHORED_VWAP"})
#: Indicators that are meaningless without a lookback period; warm-up derives from it.
NEEDS_PERIOD = frozenset(
    {
        "EMA",
        "SMA",
        "WMA",
        "RSI",
        "ATR",
        "ADX",
        "ROC",
        "STDEV",
        "CCI",
        "MFI",
        "DONCHIAN",
        "KELTNER",
        "BBANDS",
        "BOLLINGER",
    }
)

_MULTI_ONLY = (
    "max_concurrent",
    "max_trades_per_day",
    "capital_per_position",
    "max_exposure",
    "selection",
    "conflict",
    "sector_limit",
    "correlation_limit",
)
_MULTI_REQUIRED = _MULTI_ONLY[:6]
_MULTI_COUNTS = ("max_concurrent", "max_trades_per_day")
_MULTI_ENUMS = ("selection", "conflict")


class SpecError(Exception):
    """A spec is missing, unreadable or incomplete. The message lists every problem."""


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a spec."""

    kind: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


class _Collector:
    """Accumulates problems for one validation pass."""

    def __init__(self) -> None:
        self.items: list[Problem] = []

    def validity(self, field: str, message: str) -> None:
        self.items.append(Problem(VALIDITY, field, message))

    def intake(self, field: str, message: str) -> None:
        self.items.append(Problem(INTAKE, field, message))

    def missing(self, field: str) -> None:
        self.intake(field, "missing, the author must state it")


def _texts(value: Any) -> list[str] | None:
    """A field as a list of non-blank strings, or None when its type is wrong."""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        items = value
    else:
        return None
    return [v.strip() for v in items if v.strip()]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_none_word(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "none"


def _check_enum(out: _Collector, name: str, value: Any) -> bool:
    allowed = fields.get(name).allowed
    if value in allowed:
        return True
    out.validity(name, f"{value!r} is not one of {', '.join(allowed)}")
    return False


def _check_rule(out: _Collector, name: str, data: Mapping[str, Any], *, number: bool) -> None:
    if name not in data:
        out.missing(name)
        return
    items = _texts(data[name])
    if items is None:
        out.validity(name, "must be text or a list of text")
        return
    if not items:
        out.intake(name, "is empty")
        return
    if len(items) == 1 and items[0].lower() == "none":
        if not fields.get(name).none_ok:
            out.intake(name, "cannot be 'none'")
        return
    found = sorted({m.lower() for item in items for m in _VAGUE.findall(item)})
    if found:
        out.intake(name, f"is ambiguous, replace vague wording with a rule: {', '.join(found)}")
    if number and not _DIGIT.search(" ".join(items)):
        out.intake(name, "needs at least one number or threshold")


def _check_holding_period(out: _Collector, data: Mapping[str, Any]) -> None:
    if "holding_period" not in data:
        out.missing("holding_period")
        return
    items = _texts(data["holding_period"])
    if items is None:
        out.validity("holding_period", "must be text")
        return
    joined = " ".join(items)
    if not (_DIGIT.search(joined) or _SAME_SESSION.search(joined)):
        out.intake("holding_period", "needs a limit such as '12 bars' or 'same session'")


def _check_text(out: _Collector, name: str, data: Mapping[str, Any], *, required: bool) -> None:
    if name not in data:
        if required:
            out.missing(name)
    elif not isinstance(data[name], str) or not data[name].strip():
        out.validity(name, "must be non-empty text")


def _check_source(out: _Collector, data: Mapping[str, Any]) -> None:
    if "source" not in data:
        out.missing("source")
        return
    block = data["source"]
    if not isinstance(block, Mapping):
        out.validity("source", "must be a mapping with kind and ref")
        return
    for key in block:
        if key not in ("kind", "ref"):
            out.validity(f"source.{key}", "unknown field")
    if "kind" not in block:
        out.missing("source.kind")
    else:
        _check_enum(out, "source.kind", block["kind"])
    if "ref" not in block:
        out.missing("source.ref")
    elif not isinstance(block["ref"], str) or not block["ref"].strip():
        out.validity("source.ref", "must be non-empty text")


def _check_identity(out: _Collector, data: Mapping[str, Any]) -> None:
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        out.validity("schema_version", f"must be {SCHEMA_VERSION}, got {version!r}")
    _check_text(out, "name", data, required=True)
    _check_text(out, "profile", data, required=True)
    _check_source(out, data)


def _check_classification(out: _Collector, data: Mapping[str, Any]) -> None:
    for name in ("style", "direction", "timeframe"):
        if name not in data:
            out.missing(name)
        else:
            _check_enum(out, name, data[name])
    if "product" in data:
        _check_enum(out, "product", data["product"])
    style, direction = data.get("style"), data.get("direction")
    if style in ("swing", "positional") and direction in fields.DIRECTIONS:
        if direction != "long_only":
            out.validity(
                "direction",
                f"a {style} strategy must be long_only: Indian cash equity cannot be shorted "
                "overnight",
            )
    product = data.get("product")
    if style in fields.STYLES and product in fields.PRODUCTS:
        intraday = style == "intraday"
        if (intraday and product == "CNC") or (not intraday and product == "MIS"):
            out.validity("product", f"{product} contradicts style {style}")


def _check_rules(out: _Collector, data: Mapping[str, Any]) -> None:
    _check_rule(out, "entry", data, number=True)
    _check_rule(out, "exit", data, number=True)
    _check_rule(out, "stop", data, number=True)
    _check_rule(out, "take_profit", data, number=False)
    _check_rule(out, "filters", data, number=False)
    _check_holding_period(out, data)


def _check_indicator(out: _Collector, entry: Any) -> None:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
        out.validity("indicators", "each indicator must be a mapping with a name")
        return
    name = entry["name"].strip().upper()
    for key in entry:
        if key not in ("name", "period", "source", "anchor"):
            out.validity("indicators", f"{name}: unknown key '{key}'")
    period = entry.get("period")
    if "period" in entry and not (_is_int(period) and period > 0):
        out.validity("indicators", f"{name}: period must be a positive integer")
    if "source" in entry:
        if entry["source"] not in fields.PRICE_SOURCES:
            out.validity("indicators", f"{name}: source {entry['source']!r} is not a price series")
    elif name in NEEDS_ANCHOR:
        if not entry.get("anchor"):
            out.intake("indicators", f"{name} needs an anchor, for example session")
    elif name not in SOURCE_DEFAULTS_TO_CLOSE | SOURCE_NOT_NEEDED:
        out.intake("indicators", f"{name}: source is ambiguous, state the price series it reads")
    if "period" not in entry and name in NEEDS_PERIOD:
        out.intake("indicators", f"{name} needs a period")
    if name in NEEDS_ANCHOR and "source" in entry and not entry.get("anchor"):
        out.intake("indicators", f"{name} needs an anchor, for example session")


def _check_indicators(out: _Collector, data: Mapping[str, Any]) -> None:
    if "indicators" not in data:
        out.missing("indicators")
        return
    value = data["indicators"]
    if _is_none_word(value):
        return
    if not isinstance(value, list):
        out.validity("indicators", "must be a list of indicators or 'none'")
        return
    for entry in value:
        _check_indicator(out, entry)


def _check_parameters(out: _Collector, data: Mapping[str, Any]) -> None:
    if "parameters" not in data:
        out.missing("parameters")
    elif _is_none_word(data["parameters"]):
        return
    elif not isinstance(data["parameters"], Mapping):
        out.validity("parameters", "must be a mapping of name to value, or 'none'")
    elif not data["parameters"]:
        out.intake("parameters", "is empty, list every tunable value or write 'none'")


def _check_timing(out: _Collector, data: Mapping[str, Any]) -> None:
    block = data.get("timing")
    if block is None:
        return
    if not isinstance(block, Mapping):
        out.validity("timing", "must be a mapping")
        return
    for key, value in block.items():
        name = f"timing.{key}"
        if key in ("signal_evaluation", "order_submission", "fill_model"):
            _check_enum(out, name, value)
        else:
            out.validity(name, "unknown field")


def _check_range(out: _Collector, name: str, value: Any) -> None:
    if not isinstance(value, str) or not _RANGE.match(value):
        out.validity(name, f"must be a time range like '09:20-14:30', got {value!r}")
    elif value[:5] >= value[6:]:
        out.validity(name, f"range must start before it ends, got {value!r}")


def _check_session_window(out: _Collector, data: Mapping[str, Any]) -> None:
    if "session_window" not in data:
        out.missing("session_window")
        return
    value = data["session_window"]
    if _is_none_word(value):
        return
    if not isinstance(value, Mapping):
        out.validity("session_window", "must be a mapping with entry, or 'none'")
        return
    for key, item in value.items():
        if key == "entry":
            _check_range(out, "session_window.entry", item)
        elif key == "no_trade":
            if not isinstance(item, list):
                out.validity("session_window.no_trade", "must be a list of time ranges")
                continue
            for one in item:
                _check_range(out, "session_window.no_trade", one)
        else:
            out.validity(f"session_window.{key}", "unknown field")


def _check_re_entry(out: _Collector, data: Mapping[str, Any]) -> None:
    if "re_entry" not in data:
        out.missing("re_entry")
        return
    block = data["re_entry"]
    if not isinstance(block, Mapping):
        out.validity("re_entry", "must be a mapping with allowed")
        return
    for key in block:
        if key not in ("allowed", "max_per_symbol_day", "trigger"):
            out.validity(f"re_entry.{key}", "unknown field")
    allowed = block.get("allowed")
    if "allowed" in block and not isinstance(allowed, bool):
        out.validity("re_entry.allowed", "must be true or false")
        return
    if "allowed" not in block:
        out.missing("re_entry.allowed")
        return
    if not allowed:
        for key in ("max_per_symbol_day", "trigger"):
            if key in block:
                out.validity(f"re_entry.{key}", "is set but re-entry is not allowed")
        return
    limit = block.get("max_per_symbol_day")
    if "max_per_symbol_day" not in block:
        out.missing("re_entry.max_per_symbol_day")
    elif not (_is_int(limit) and limit > 0):
        out.validity("re_entry.max_per_symbol_day", "must be a positive integer")
    if "trigger" in block:
        _check_enum(out, "re_entry.trigger", block["trigger"])


def _check_sizing(out: _Collector, data: Mapping[str, Any]) -> None:
    if "sizing" not in data:
        out.missing("sizing")
        return
    block = data["sizing"]
    if not isinstance(block, Mapping):
        out.validity("sizing", "must be a mapping")
        return
    for key in block:
        if key not in ("risk_per_trade_pct", "capital_per_trade"):
            out.validity(f"sizing.{key}", "unknown field")
    for key in ("risk_per_trade_pct", "capital_per_trade"):
        name = f"sizing.{key}"
        if key not in block:
            out.missing(name)
        elif not (_is_number(block[key]) and block[key] > 0):
            out.validity(name, "must be a positive number")


def _check_multi_position(out: _Collector, block: Mapping[str, Any]) -> None:
    if "scope" in block:
        out.validity("portfolio.scope", "applies only to single_position")
    for key in _MULTI_REQUIRED:
        name = f"portfolio.{key}"
        if key not in block:
            out.missing(name)
        elif key in _MULTI_ENUMS:
            _check_enum(out, name, block[key])
        elif key in _MULTI_COUNTS and not (_is_int(block[key]) and block[key] > 0):
            out.validity(name, "must be a positive integer")
        elif not (_is_number(block[key]) and block[key] > 0):
            out.validity(name, "must be a positive number")
    for key in ("sector_limit", "correlation_limit"):
        if key in block and not (_is_number(block[key]) and block[key] > 0):
            out.validity(f"portfolio.{key}", "must be a positive number")


def _check_single_position(out: _Collector, block: Mapping[str, Any]) -> None:
    scope = block.get("scope")
    if "scope" not in block:
        out.intake(
            "portfolio.scope",
            "missing, state global or per_symbol; an inferred scope is a High impact assumption",
        )
    elif not _check_enum(out, "portfolio.scope", scope):
        scope = None
    for key in _MULTI_ONLY:
        if key not in block:
            continue
        if scope == "global":
            out.validity(f"portfolio.{key}", "does not apply to a global single_position strategy")
        else:
            out.validity(
                f"portfolio.{key}", "is a multi_position field, declare mode multi_position"
            )


def _check_portfolio(out: _Collector, data: Mapping[str, Any]) -> None:
    if "portfolio" not in data:
        out.missing("portfolio")
        return
    block = data["portfolio"]
    if not isinstance(block, Mapping):
        out.validity("portfolio", "must be a mapping with mode")
        return
    for key in block:
        if key not in {"mode", "scope", *_MULTI_ONLY}:
            out.validity(f"portfolio.{key}", "unknown field")
    if "mode" not in block:
        out.missing("portfolio.mode")
    elif _check_enum(out, "portfolio.mode", block["mode"]):
        check = (
            _check_multi_position if block["mode"] == "multi_position" else _check_single_position
        )
        check(out, block)


def _check_mapping_enums(
    out: _Collector, data: Mapping[str, Any], name: str, allowed: Mapping[str, tuple[str, ...]]
) -> None:
    block = data.get(name)
    if block is None:
        return
    if not isinstance(block, Mapping):
        out.validity(name, "must be a mapping")
        return
    for key, value in block.items():
        if key not in allowed:
            out.validity(f"{name}.{key}", "unknown field")
        elif value not in allowed[key]:
            out.validity(f"{name}.{key}", f"{value!r} is not one of {', '.join(allowed[key])}")


def _check_disclosure(out: _Collector, data: Mapping[str, Any]) -> None:
    block = data.get("disclosure")
    if block is None:
        return
    if not isinstance(block, Mapping):
        out.validity("disclosure", "must be a mapping")
        return
    for key, value in block.items():
        name = f"disclosure.{key}"
        if key == "prior_variants":
            if not (_is_int(value) and value >= 0):
                out.validity(name, "must be a non-negative integer")
        elif key == "chart_peeking":
            if not isinstance(value, bool):
                out.validity(name, "must be true or false")
        elif key == "destination":
            _check_enum(out, name, value)
        else:
            out.validity(name, "unknown field")


def _check_optional(out: _Collector, data: Mapping[str, Any]) -> None:
    _check_timing(out, data)
    price_refs = dict.fromkeys(
        ("stop", "target", "trailing_stop", "sizing"), fields.REFERENCE_PRICES
    )
    _check_mapping_enums(out, data, "reference_prices", price_refs)
    order_types = {
        "entry": fields.ENTRY_ORDER_TYPES,
        "stop": fields.STOP_ORDER_TYPES,
        "target": fields.TARGET_ORDER_TYPES,
    }
    _check_mapping_enums(out, data, "order_types", order_types)
    _check_disclosure(out, data)
    for name in ("rationale", "kill_criterion"):
        _check_text(out, name, data, required=False)


def _derived_product(data: Mapping[str, Any]) -> str | None:
    if data.get("product") in fields.PRODUCTS:
        return data["product"]
    style = data.get("style")
    if style not in fields.STYLES:
        return None
    return "MIS" if style == "intraday" else "CNC"


def _entry_window(data: Mapping[str, Any]) -> str | None:
    """The entry window when it is a well-formed range that starts before it ends."""
    block = data.get("session_window")
    value = block.get("entry") if isinstance(block, Mapping) else None
    if isinstance(value, str) and _RANGE.match(value) and value[:5] < value[6:]:
        return value
    return None


def _check_profile(out: _Collector, data: Mapping[str, Any], profiles_dir: Path | None) -> None:
    name = data.get("profile")
    if not isinstance(name, str) or not name.strip():
        return
    try:
        profile = load_profile(name, profiles_dir)
    except ProfileError as exc:
        out.validity("profile", str(exc))
        return
    product = _derived_product(data)
    if product is not None and not profile.supports(product):
        out.validity("profile", f"profile '{name}' does not support product {product}")
        return
    window = _entry_window(data)
    if product is not None and window is not None:
        problem = profile.entry_window_problem(product, window)
        if problem:
            out.validity("session_window.entry", problem)


def validate_spec(data: Any, profiles_dir: Path | None = None) -> list[Problem]:
    """Every problem with a spec, or an empty list when it is complete and concrete."""
    if not isinstance(data, Mapping):
        return [Problem(VALIDITY, "spec", "spec is not a mapping")]
    out = _Collector()
    known = fields.top_level(fields.INTENT) | {"schema_version"}
    for key in data:
        if key not in known:
            out.validity(str(key), "unknown field")
    _check_identity(out, data)
    _check_classification(out, data)
    _check_rules(out, data)
    _check_indicators(out, data)
    _check_parameters(out, data)
    _check_session_window(out, data)
    _check_re_entry(out, data)
    _check_sizing(out, data)
    _check_portfolio(out, data)
    _check_optional(out, data)
    _check_profile(out, data, profiles_dir)
    return out.items


def load_spec(folder: Path) -> dict[str, Any] | None:
    """The folder's parsed spec.yaml, or None when it has none. Raises SpecError if unreadable."""
    path = folder / "spec.yaml"
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"{path}: spec.yaml is unreadable ({exc})") from exc


def _required_message(idea_id: str) -> str:
    return (
        f"{idea_id}: spec.yaml is required before this idea is backtested "
        "(copy research/ideas/_template/spec.yaml and complete every field)"
    )


def check_idea(idea_id: str, folder: Path) -> None:
    """The record gate: raise SpecError unless the idea has a valid spec. Every idea needs one."""
    data = load_spec(folder)
    if data is None:
        raise SpecError(_required_message(idea_id))
    problems = validate_spec(data)
    if problems:
        detail = "\n".join(f"- {p}" for p in problems)
        raise SpecError(f"{idea_id}: spec.yaml is incomplete:\n{detail}")


def registry_problems(idea_id: str, status: str, folder: Path) -> list[str]:
    """Spec problems for the registry check: a present spec must be valid, and an idea that is
    being worked on must have one."""
    try:
        data = load_spec(folder)
    except SpecError as exc:
        return [str(exc)]
    if data is None:
        return [_required_message(idea_id)] if status in SPEC_ACTIVE_STATUSES else []
    return [f"spec.yaml: {p}" for p in validate_spec(data)]
