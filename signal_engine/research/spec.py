"""Strategy specification: the minimum an idea must state before it is backtested.

Each idea folder may hold a `spec.yaml` (template: `research/ideas/_template/spec.yaml`).
The principle is that an incomplete strategy is clarified, never completed by assumption:
an entry like "buy when momentum is strong" has no implementation, so the backtest engine
would have to invent one, and the result would describe a strategy nobody specified.

The checks here are deliberately mechanical. They cannot judge whether a rule is a good
one, only whether it is concrete enough to be coded without guessing:

  * every required field is present and non-empty;
  * rule fields contain no vague wording (see `VAGUE_WORDS`);
  * entry, exit and stop-loss each carry at least one number or threshold;
  * swing and positional strategies are long-only, matching the swing engine.

Ideas numbered below `SPEC_REQUIRED_FROM_ID` predate the gate and are exempt. A spec they
do carry is still validated.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

#: First idea number that must carry a complete spec. IDEA-026 is the last idea created
#: before the gate; every older idea is exempt so history is never rewritten.
SPEC_REQUIRED_FROM_ID = 27

#: Statuses at which the registry demands a spec for a gated idea. An inbox idea is a raw
#: capture and a rejected or shelved one is finished; the record gate is stricter.
SPEC_ACTIVE_STATUSES = ("evaluating", "backtested", "paper", "live")

REQUIRED_FIELDS = (
    "name",
    "market",
    "timeframe",
    "trading_style",
    "direction",
    "entry",
    "exit",
    "stop_loss",
    "take_profit",
    "position_sizing",
    "max_holding_period",
    "filters",
    "session_filters",
    "indicators",
    "parameters",
    "cost_assumptions",
    "constraints",
    "rationale",
)

TRADING_STYLES = ("intraday", "swing", "positional")
DIRECTIONS = ("long_only", "short_only", "both")

#: Fields that may be the literal text "none" (a deliberate, stated absence).
_MAY_BE_NONE = ("take_profit", "filters", "session_filters", "constraints")

#: Rule fields checked for vague wording.
_RULE_FIELDS = (
    "entry",
    "exit",
    "stop_loss",
    "take_profit",
    "filters",
    "session_filters",
    "position_sizing",
    "max_holding_period",
)

#: Rule fields whose content must include at least one number or threshold.
_NEEDS_NUMBER = ("entry", "exit", "stop_loss")

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
_TIMEFRAME = re.compile(
    r"^\d+\s*(m|min|mins|minute|minutes|h|hr|hour|hours|d|day|days|w|week|weeks)$",
    re.IGNORECASE,
)
_DIGIT = re.compile(r"\d")
_ID_NUMBER = re.compile(r"^IDEA-(\d{3})$")


class SpecError(Exception):
    """A spec is missing, unreadable or incomplete. The message lists every problem."""


def _texts(value: Any) -> list[str] | None:
    """A field as a list of non-blank strings, or None when its type is wrong."""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        items = value
    else:
        return None
    return [v.strip() for v in items if v.strip()]


def _is_none(items: list[str]) -> bool:
    return len(items) == 1 and items[0].lower() == "none"


def _check_field(field: str, value: Any) -> list[str]:
    if field == "parameters":
        if isinstance(value, str) and value.strip().lower() == "none":
            return []
        if not isinstance(value, dict) or not value:
            return ["'parameters' must be a non-empty mapping of name to value (or 'none')"]
        return []
    items = _texts(value)
    if items is None:
        return [f"'{field}' must be text or a list of text"]
    if not items:
        return [f"'{field}' is empty"]
    if _is_none(items):
        return [] if field in _MAY_BE_NONE else [f"'{field}' cannot be 'none'"]
    problems: list[str] = []
    if field in _RULE_FIELDS:
        found = sorted({m.lower() for item in items for m in _VAGUE.findall(item)})
        if found:
            problems.append(
                f"'{field}' is ambiguous, replace vague wording with a rule: {', '.join(found)}"
            )
    joined = " ".join(items)
    if field in _NEEDS_NUMBER and not _DIGIT.search(joined):
        problems.append(f"'{field}' needs at least one number or threshold")
    if field == "timeframe" and not _TIMEFRAME.match(joined):
        problems.append("'timeframe' must be a bar size such as 5m, 15min, 1h or 1d")
    if field == "max_holding_period" and not (
        _DIGIT.search(joined) or re.search(r"\b(session|eod)\b", joined, re.IGNORECASE)
    ):
        problems.append("'max_holding_period' needs a limit such as '12 bars' or 'same session'")
    if field == "cost_assumptions" and not _DIGIT.search(joined):
        problems.append("'cost_assumptions' needs a number, for example '16 bps round trip'")
    return problems


def validate_spec(data: Any) -> list[str]:
    """Every problem with a spec, or an empty list when it is complete and concrete."""
    if not isinstance(data, dict):
        return ["spec is not a mapping"]
    problems = [f"missing field '{f}'" for f in REQUIRED_FIELDS if f not in data]
    problems.extend(f"unknown field '{k}'" for k in data if k not in REQUIRED_FIELDS)
    for field in REQUIRED_FIELDS:
        if field in data:
            problems.extend(_check_field(field, data[field]))
    style, direction = data.get("trading_style"), data.get("direction")
    if "trading_style" in data and style not in TRADING_STYLES:
        problems.append(f"'trading_style' {style!r} not one of {', '.join(TRADING_STYLES)}")
    if "direction" in data and direction not in DIRECTIONS:
        problems.append(f"'direction' {direction!r} not one of {', '.join(DIRECTIONS)}")
    if style in ("swing", "positional") and direction in DIRECTIONS and direction != "long_only":
        problems.append(
            f"a {style} strategy must be direction long_only: Indian cash equity cannot be "
            "shorted overnight"
        )
    return problems


def load_spec(folder: Path) -> dict[str, Any] | None:
    """The folder's parsed spec.yaml, or None when it has none. Raises SpecError if unreadable."""
    path = folder / "spec.yaml"
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"{path}: spec.yaml is unreadable ({exc})") from exc


def spec_required(idea_id: str) -> bool:
    """True for ideas created after the gate (numbered `SPEC_REQUIRED_FROM_ID` or higher)."""
    match = _ID_NUMBER.match(idea_id)
    return bool(match) and int(match.group(1)) >= SPEC_REQUIRED_FROM_ID


def _required_message(idea_id: str) -> str:
    return (
        f"{idea_id}: spec.yaml is required before this idea is backtested "
        "(copy research/ideas/_template/spec.yaml and complete every field)"
    )


def check_idea(idea_id: str, folder: Path) -> None:
    """The record gate: raise SpecError unless the idea may be backtested."""
    data = load_spec(folder)
    if data is None:
        if spec_required(idea_id):
            raise SpecError(_required_message(idea_id))
        return
    problems = validate_spec(data)
    if problems:
        detail = "\n".join(f"- {p}" for p in problems)
        raise SpecError(f"{idea_id}: spec.yaml is incomplete:\n{detail}")


def registry_problems(idea_id: str, status: str, folder: Path) -> list[str]:
    """Spec problems for the registry check: a present spec must be valid, and a gated idea
    that is being worked on must have one."""
    try:
        data = load_spec(folder)
    except SpecError as exc:
        return [str(exc)]
    if data is None:
        active = status in SPEC_ACTIVE_STATUSES
        return [_required_message(idea_id)] if spec_required(idea_id) and active else []
    return [f"spec.yaml: {p}" for p in validate_spec(data)]
