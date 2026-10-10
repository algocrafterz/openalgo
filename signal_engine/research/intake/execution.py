"""Execution resolution: the platform's settings for a run, each with a source and a reason.

The spec says what the strategy does; this module decides how the platform simulates it.
Every resolved value is returned as a `ResolvedValue` carrying its source, so the assumption
ledger (build order 2) can be built from this list and nothing enters a run without an entry.

Platform defaults read the constants that already exist in `backtest/record_run.py`; they are
not redefined here. The only inputs an author can change are the overrides in
`ideas/<idea>/execution.yaml`, each with a stated reason.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from signal_engine.research.intake import fields
from signal_engine.research.intake.profiles import Profile

SOURCES = ("user", "derived", "platform_default", "market_data", "broker_config", "inferred")

#: Override keys an author may set in execution.yaml, mapped to their resolved field.
OVERRIDABLE = {
    "slippage_bps_per_side": "execution.slippage_bps_per_side",
    "cost_bps_round_trip": "execution.cost_bps_round_trip",
}

#: Resolved fields that map straight onto a RunConfig field of the same purpose.
_RUN_CONFIG_FIELDS = {
    "execution.cost_bps_round_trip": "cost_bps",
    "profile.mis_squareoff_min": "mis_squareoff_min",
    "profile.squareoff_margin_min": "squareoff_margin_min",
    "profile.time_exit_min": "time_exit_min",
    "profile.tick_grid": "tick_grid",
}

#: Per-date eligibility defaults (backtest/eligibility.py): listing age in sessions, session
#: volume strictly above, session close at or above, in rupees.
ELIGIBILITY_MIN_PRIOR_SESSIONS = 60
ELIGIBILITY_MIN_VOLUME = 1
ELIGIBILITY_MIN_PRICE = 10.0

_Row = tuple[str, Any, str, str]


class ExecutionError(Exception):
    """Execution settings cannot be resolved: a bad override or a profile that cannot serve."""


@dataclass(frozen=True)
class ResolvedValue:
    """One resolved setting: what it is, where it came from and why."""

    field: str
    section: str
    value: Any
    source: str
    reason: str


@dataclass(frozen=True)
class ResolvedExecution:
    """Every resolved setting for one run, plus the identity of the profile that shaped it."""

    values: tuple[ResolvedValue, ...]
    profile_name: str
    profile_sha256: str

    def get(self, name: str) -> ResolvedValue:
        for item in self.values:
            if item.field == name:
                return item
        raise KeyError(name)

    def as_dict(self) -> dict[str, Any]:
        return {item.field: item.value for item in self.values}

    def run_config_fields(self) -> dict[str, Any]:
        """Resolved values that apply to a RunConfig, keyed by its field names."""
        present = self.as_dict()
        return {rc: present[name] for name, rc in _RUN_CONFIG_FIELDS.items() if name in present}


def load_overrides(folder: Path) -> dict[str, Any] | None:
    """The idea's parsed execution.yaml, or None when it has none."""
    path = folder / "execution.yaml"
    if not path.exists():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ExecutionError(f"{path}: execution.yaml is unreadable ({exc})") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise ExecutionError(f"{path}: execution.yaml must be a mapping of overrides")
    return dict(loaded)


def validate_overrides(overrides: Mapping[str, Any]) -> dict[str, tuple[Any, str]]:
    intent = fields.top_level(fields.INTENT)
    accepted: dict[str, tuple[Any, str]] = {}
    for key, block in overrides.items():
        if key in intent:
            raise ExecutionError(
                f"'{key}' is strategy intent and belongs in spec.yaml, not execution.yaml"
            )
        if key not in OVERRIDABLE:
            raise ExecutionError(f"unknown override '{key}', allowed: {', '.join(OVERRIDABLE)}")
        value = block.get("value") if isinstance(block, Mapping) else None
        reason = block.get("reason") if isinstance(block, Mapping) else None
        if not (isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0):
            raise ExecutionError(f"override {key} needs a positive number as value")
        if not isinstance(reason, str) or not reason.strip():
            raise ExecutionError(f"override {key} needs a reason")
        accepted[OVERRIDABLE[key]] = (value, reason.strip())
    return accepted


_TIMEFRAME_SUFFIX = re.compile(r"_(\d+)([MH])$", re.IGNORECASE)
_BASE_BAR = re.compile(r"^(\d+)([mh])$")


def _minutes(count: str, unit: str) -> int:
    return int(count) * (60 if unit.upper() == "H" else 1)


def _warmup(spec: Mapping[str, Any]) -> tuple[int, str]:
    """The longest indicator span in base bars.

    An indicator named with a `_<n>M` or `_<n>H` suffix (EMA_15M) runs on bars of that length, so
    its period is converted to base bars: 20 fifteen-minute bars are 60 five-minute bars. A name
    with no suffix, or a spec whose timeframe is not a minute or hour bar, counts its period as
    given.
    """
    base = _BASE_BAR.match(spec.get("timeframe")) if isinstance(spec.get("timeframe"), str) else None
    base_minutes = _minutes(base.group(1), base.group(2)) if base else None
    best: tuple[int, str] | None = None
    indicators = spec.get("indicators")
    for entry in indicators if isinstance(indicators, list) else []:
        period = entry.get("period") if isinstance(entry, Mapping) else None
        if not isinstance(period, int) or isinstance(period, bool):
            continue
        name = str(entry.get("name"))
        suffix = _TIMEFRAME_SUFFIX.search(name)
        if suffix and base_minutes:
            tf = _minutes(suffix.group(1), suffix.group(2))
            bars = max(1, math.ceil(period * tf / base_minutes))
            reason = f"longest indicator span: {name}({period}) = {bars} {spec['timeframe']} bars"
        else:
            bars, reason = period, f"longest indicator period: {name}({period})"
        if best is None or bars > best[0]:
            best = (bars, reason)
    return best if best else (0, "no indicator with a period")


def _product(spec: Mapping[str, Any]) -> tuple[str, str, str]:
    style = spec.get("style")
    if style not in fields.STYLES:
        raise ExecutionError("cannot resolve execution settings without a valid style")
    if spec.get("product") in fields.PRODUCTS:
        return spec["product"], "user", "stated in the spec"
    product = "MIS" if style == "intraday" else "CNC"
    return product, "derived", f"{style} strategies trade {product}"


def _profile_rows(profile: Profile, product: str) -> list[_Row]:
    label = f"profile {profile.name} {product}"
    rows: list[_Row] = [
        (f"profile.{name}", value, "broker_config", label)
        for name, value in profile.run_config_fields(product).items()
    ]
    carry = profile.products[product].carry_forward
    rows.append(("profile.carry_forward", carry, "broker_config", label))
    return rows


_MIN_PRICE_REASON_RAW = "floor in rupees on the NSE raw (unadjusted) session close, excludes penny-priced sessions"

#: Why a daily swing run records the price floor but does not apply it (see eligibility.py).
MIN_PRICE_NOT_APPLIED = (
    "platform default, NOT APPLIED to this run: the daily panel is split-adjusted, so its prices "
    "are back-adjusted and not the traded price, and a nominal rupee floor would not be reliable. "
    "The eligibility filters that do apply are listing age, a bar on the session and non-zero "
    "volume"
)


def _platform_rows(spec: Mapping[str, Any], product: tuple[str, str, str]) -> list[_Row]:
    # Imported here, not at module level: record_run imports the intake package, so a top-level
    # import would make `import ...intake.ledger` fail when it is the first thing imported.
    from signal_engine.backtest import record_run

    intraday = spec.get("style") == "intraday"
    warmup, warmup_reason = _warmup(spec)
    cost = record_run.INTRADAY_COST_BPS if intraday else record_run.SWING_COST_BPS
    kind = "intraday" if intraday else "swing"
    return [
        ("execution.product", product[0], product[1], product[2]),
        ("execution.bar_size", spec.get("timeframe"), "derived", "same as the spec timeframe"),
        ("execution.warmup_bars", warmup, "derived", warmup_reason),
        (
            "execution.slippage_bps_per_side",
            record_run.SLIPPAGE_BPS_PER_SIDE,
            "platform_default",
            "house default for market orders",
        ),
        (
            "execution.cost_bps_round_trip",
            cost,
            "platform_default",
            f"{kind} round-trip cost default",
        ),
        (
            "execution.oos_fraction",
            record_run.OOS_FRACTION,
            "platform_default",
            "G3.1 holdout fraction",
        ),
        (
            "execution.embargo_sessions",
            record_run.EMBARGO_SESSIONS,
            "platform_default",
            "G3.5 embargo after the cut",
        ),
        (
            "execution.eligibility_min_prior_sessions",
            ELIGIBILITY_MIN_PRIOR_SESSIONS,
            "platform_default",
            "listing age: earlier traded sessions required before a symbol is eligible",
        ),
        (
            "execution.eligibility_min_volume",
            ELIGIBILITY_MIN_VOLUME,
            "platform_default",
            "session volume must be above this to count as traded",
        ),
        (
            "execution.eligibility_min_price",
            ELIGIBILITY_MIN_PRICE,
            "platform_default",
            _MIN_PRICE_REASON_RAW if intraday else MIN_PRICE_NOT_APPLIED,
        ),
    ]


def resolve_execution(
    spec: Mapping[str, Any], profile: Profile, overrides: Mapping[str, Any] | None = None
) -> ResolvedExecution:
    """Resolve every execution and profile setting for a validated spec.

    The spec must already pass `validate_spec`; this does not re-validate it. Raises
    ExecutionError for a bad override or when the profile cannot serve the product.
    """
    accepted = validate_overrides(overrides or {})
    product = _product(spec)
    if not profile.supports(product[0]):
        raise ExecutionError(f"profile '{profile.name}' does not support product {product[0]}")
    rows = _platform_rows(spec, product) + _profile_rows(profile, product[0])
    values = []
    for name, value, source, reason in rows:
        if name in accepted:
            value, reason = accepted[name]
            source = "user"
        values.append(ResolvedValue(name, name.split(".")[0], value, source, reason))
    return ResolvedExecution(tuple(values), profile.name, profile.sha256)
