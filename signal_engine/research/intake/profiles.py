"""Deployment profiles: broker and product rules, declared once and shared.

A profile names the broker constraints a backtest must simulate (MIS force-flat, CNC
carry-forward). It is selected by name from a spec and never copied into an idea. Switching
profile is a configuration change, not a new strategy.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROFILES_DIR = Path(__file__).resolve().parents[1] / "profiles"

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TIME = re.compile(r"^(\d{2}):(\d{2})$")
_TOP_KEYS = ("name", "broker", "market", "products")
_MIS_TIMES = (
    ("force_flat", "force_flat_min"),
    ("standing_time_exit", "standing_time_exit_min"),
    ("latest_exit_bar_close", "latest_exit_bar_close_min"),
)
_MIS_KEYS = tuple(k for k, _ in _MIS_TIMES) + ("exit_convention",)
_CNC_KEYS = ("carry_forward",)
PRODUCTS = ("MIS", "CNC")


class ProfileError(Exception):
    """A profile is missing, unreadable or invalid. The message lists every problem."""


@dataclass(frozen=True)
class ProductRules:
    """Rules for one product. Minutes are after midnight IST; None where the rule is absent."""

    force_flat_min: int | None = None
    standing_time_exit_min: int | None = None
    latest_exit_bar_close_min: int | None = None
    carry_forward: bool = False
    exit_convention: str | None = None


@dataclass(frozen=True, eq=False)
class Profile:
    """A validated deployment profile and the hash of its canonical content."""

    name: str
    broker: str
    market: str
    products: Mapping[str, ProductRules]
    sha256: str
    _content: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    def supports(self, product: str) -> bool:
        return product in self.products

    def as_dict(self) -> dict[str, Any]:
        """The profile content as loaded, for the run's execution config."""
        return deepcopy(self._content)

    def run_config_fields(self, product: str) -> dict[str, int]:
        """RunConfig fields this profile fixes for a product. CNC fixes none."""
        if product not in self.products:
            raise ProfileError(f"profile '{self.name}' does not support product {product}")
        rules = self.products[product]
        if rules.force_flat_min is None:
            return {}
        return {
            "mis_squareoff_min": rules.force_flat_min,
            "squareoff_margin_min": rules.force_flat_min - rules.latest_exit_bar_close_min,
            "time_exit_min": rules.standing_time_exit_min,
        }


def _minutes(value: Any, label: str, problems: list[str]) -> int | None:
    match = _TIME.match(value) if isinstance(value, str) else None
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        problems.append(f"{label} must be a time like '15:00', got {value!r}")
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _check_unknown(block: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> list[str]:
    return [f"{where}: unknown or misplaced key '{k}'" for k in block if k not in allowed]


def _parse_mis(block: Mapping[str, Any], problems: list[str]) -> ProductRules | None:
    if "carry_forward" in block:
        problems.append("MIS: carry_forward is not allowed, MIS is closed by force_flat")
    problems.extend(_check_unknown(block, _MIS_KEYS + ("carry_forward",), "MIS"))
    values: dict[str, int | None] = {}
    for key, attr in _MIS_TIMES:
        if key not in block:
            problems.append(f"MIS: missing {key}")
            values[attr] = None
        else:
            values[attr] = _minutes(block[key], f"MIS {key}", problems)
    convention = block.get("exit_convention")
    if not isinstance(convention, str) or not convention.strip():
        problems.append("MIS: missing exit_convention")
    flat, standing, latest = (values[a] for _, a in _MIS_TIMES)
    if None not in (flat, standing, latest) and not standing < latest <= flat:
        problems.append(
            "MIS: cutoffs out of order, need standing_time_exit < latest_exit_bar_close "
            "<= force_flat"
        )
    if None in (flat, standing, latest) or not isinstance(convention, str):
        return None
    return ProductRules(flat, standing, latest, False, convention.strip())


def _parse_cnc(block: Mapping[str, Any], problems: list[str]) -> ProductRules | None:
    for key in block:
        if key not in _CNC_KEYS:
            problems.append(f"CNC: '{key}' does not apply, CNC has no intraday cutoff")
    carry = block.get("carry_forward")
    if not isinstance(carry, bool):
        problems.append("CNC: carry_forward must be declared true or false")
        return None
    return ProductRules(carry_forward=carry)


def _parse_products(raw: Any, problems: list[str]) -> dict[str, ProductRules]:
    if not isinstance(raw, Mapping) or not raw:
        problems.append("products must be a non-empty mapping")
        return {}
    parsed: dict[str, ProductRules] = {}
    for product, block in raw.items():
        if product not in PRODUCTS:
            problems.append(
                f"products: unknown product '{product}', expected {', '.join(PRODUCTS)}"
            )
        elif not isinstance(block, Mapping):
            problems.append(f"{product}: must be a mapping")
        else:
            rules = (_parse_mis if product == "MIS" else _parse_cnc)(block, problems)
            if rules is not None:
                parsed[product] = rules
    return parsed


def _canonical_hash(content: Mapping[str, Any]) -> str:
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_profile(data: Any, expected_name: str) -> Profile:
    """Validate parsed profile content. Raises ProfileError listing every problem."""
    if not isinstance(data, Mapping):
        raise ProfileError(f"profile '{expected_name}' is not a mapping")
    problems = [f"missing key '{k}'" for k in _TOP_KEYS if k not in data]
    problems.extend(_check_unknown(data, _TOP_KEYS, "profile"))
    if "name" in data and data["name"] != expected_name:
        problems.append(f"name {data['name']!r} must match the file name '{expected_name}'")
    for key in ("broker", "market"):
        if key in data and not (isinstance(data[key], str) and data[key].strip()):
            problems.append(f"{key} must be non-empty text")
    products = _parse_products(data.get("products"), problems) if "products" in data else {}
    if problems:
        detail = "\n".join(f"- {p}" for p in problems)
        raise ProfileError(f"profile '{expected_name}' is invalid:\n{detail}")
    content = deepcopy(dict(data))
    return Profile(
        data["name"], data["broker"], data["market"], products, _canonical_hash(content), content
    )


def load_profile(name: str, profiles_dir: Path | None = None) -> Profile:
    """Load and validate the named profile. Raises ProfileError if absent or invalid."""
    folder = profiles_dir if profiles_dir is not None else PROFILES_DIR
    path = folder / f"{name}.yaml"
    if not isinstance(name, str) or not _NAME.fullmatch(name) or not path.is_file():
        raise ProfileError(f"profile '{name}' not found in {folder}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"{path}: profile is unreadable ({exc})") from exc
    return parse_profile(data, name)
