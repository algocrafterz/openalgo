"""The assumption ledger: every resolved value is an entry with a source and a confirmation.

The ledger is the source and the resolved configuration is a view of it. A value the author
stated is an entry (source `user`), so every field has provenance. A value the platform filled
in is an entry the author can review, and a High-materiality inference must be confirmed
before freeze.

The working ledger lives in `ideas/<idea>/assumptions.yaml`. Besides the entries it stores
only what cannot be derived: the open questions and an explicit `blocked_reason`. The intake
state itself is computed (see `state.py`), never stored.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from signal_engine.research.intake import execution, fields
from signal_engine.research.intake.profiles import ProfileError, load_profile
from signal_engine.research.intake.spec import validate_spec

LEDGER_FILE = "assumptions.yaml"
SOURCES = execution.SOURCES
CONFIDENCES = ("high", "medium", "low")

_ENTRY_KEYS = (
    "id",
    "field",
    "section",
    "value",
    "source",
    "confidence",
    "materiality",
    "reason",
    "user_confirmed",
)
_ID = re.compile(r"^A-(\d{3,})$")
#: Wording that really means "evaluated when the bar closes". "previous close" and "market close"
#: are nouns for a price or an event, so they do not match.
_CLOSE = re.compile(
    r"\b(?:bar|candle)\s+close\b|\bclos(?:es|ed|ing)\b|\bon\s+(?:the\s+)?close\b|"
    r"\b\d+\s*-?\s*(?:min|minute|m|h|hour|d|day)s?\s+close\b|\b(?:daily|hourly|weekly)\s+close\b",
    re.IGNORECASE,
)
#: A price "tick" is a unit of price, not a signal evaluated on every tick, so it does not match.
_INTRABAR = re.compile(r"\bintrabar\b|\bas\s+soon\s+as\b|\btouch(?:es|ed|ing)\b", re.IGNORECASE)
_NEXT_OPEN = re.compile(r"\bnext\s+(?:candle|bar)(?:'s)?\s+open\b|\bnext\s+open\b", re.IGNORECASE)
_ONE_POSITION = re.compile(
    r"\bone\s+(?:position|trade)\s+at\s+a\s+time\b|\bonly\s+one\s+(?:position|trade)\b(?!\s+per\b)|"
    r"\bsingle\s+position\s+(?:across|overall|in\s+total)\b",
    re.IGNORECASE,
)
#: (order_submission, fill_model) that follow from a signal evaluation point.
_TIMING_CHAIN = {
    "bar_close": ("signal_close", "next_bar_open"),
    "bar_open": ("next_bar_open", "next_bar_open"),
    "intrabar": ("signal_close", "limit_at_level"),
    "tick": ("signal_close", "limit_at_level"),
}


class LedgerError(Exception):
    """The ledger file is unreadable or invalid, or a confirmation names an unknown entry."""


@dataclass(frozen=True)
class Assumption:
    """One resolved value and the reason it holds."""

    id: str
    field: str
    section: str
    value: Any
    source: str
    confidence: str
    materiality: str
    reason: str
    user_confirmed: bool

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in _ENTRY_KEYS}


@dataclass(frozen=True)
class Ledger:
    """The entries plus the two things that cannot be derived from them."""

    entries: tuple[Assumption, ...]
    open_questions: tuple[str, ...] = ()
    blocked_reason: str | None = None
    next_id: int = 0

    def has(self, field: str) -> bool:
        return any(e.field == field for e in self.entries)

    def get(self, field: str) -> Assumption:
        for entry in self.entries:
            if entry.field == field:
                return entry
        raise KeyError(field)


@dataclass(frozen=True)
class _Row:
    section: str
    value: Any
    source: str
    confidence: str
    materiality: str
    reason: str


def _lookup(data: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _user_rows(spec: Mapping[str, Any]) -> dict[str, _Row]:
    rows: dict[str, _Row] = {}
    for f in fields.FIELDS:
        if f.section != fields.INTENT:
            continue
        found, value = _lookup(spec, f.name)
        if found:
            rows[f.name] = _Row(
                fields.INTENT,
                copy.deepcopy(value),
                "user",
                "high",
                f.materiality,
                "stated in spec.yaml",
            )
    return rows


def _timing_rows(spec: Mapping[str, Any], rows: dict[str, _Row]) -> None:
    text = " ".join(_texts(spec.get("entry")))
    intrabar, close = _INTRABAR.search(text), _CLOSE.search(text)
    if "timing.signal_evaluation" in rows:
        signal, implied = rows["timing.signal_evaluation"].value, True
    elif intrabar:
        signal, implied = "intrabar", False
        rows["timing.signal_evaluation"] = _Row(
            fields.INTENT,
            signal,
            "inferred",
            "medium",
            fields.HIGH,
            f'entry text says "{intrabar.group(0)}": evaluated inside the bar, which makes '
            "the fill price an assumption",
        )
    elif close:
        signal, implied = "bar_close", True
        rows["timing.signal_evaluation"] = _Row(
            fields.INTENT,
            signal,
            "inferred",
            "high",
            fields.MEDIUM,
            f'entry text says "{close.group(0)}": evaluated when the bar closes',
        )
    else:
        signal, implied = "bar_close", False
        rows["timing.signal_evaluation"] = _Row(
            fields.INTENT,
            signal,
            "inferred",
            "medium",
            fields.HIGH,
            "entry text does not say when the signal is evaluated; bar close assumed",
        )
    order, fill = _TIMING_CHAIN.get(signal, _TIMING_CHAIN["bar_close"])
    stated = rows["timing.signal_evaluation"].source == "user"
    chained = fields.MEDIUM if implied or stated else fields.HIGH
    if "timing.order_submission" not in rows:
        rows["timing.order_submission"] = _Row(
            fields.INTENT,
            order,
            "inferred",
            "high" if chained == fields.MEDIUM else "medium",
            chained,
            f"follows from evaluating at {signal}",
        )
    if "timing.fill_model" not in rows:
        next_open = _NEXT_OPEN.search(text)
        if next_open and signal == "bar_close":
            rows["timing.fill_model"] = _Row(
                fields.INTENT,
                fill,
                "inferred",
                "high",
                fields.MEDIUM,
                f'entry text says "{next_open.group(0)}"',
            )
        else:
            rows["timing.fill_model"] = _Row(
                fields.INTENT,
                fill,
                "inferred",
                "medium",
                chained,
                f"entry text does not state the fill; {fill} follows from evaluating at {signal}",
            )


def _scope_row(spec: Mapping[str, Any], rows: dict[str, _Row]) -> None:
    block = spec.get("portfolio")
    if not (isinstance(block, Mapping) and block.get("mode") == "single_position"):
        return
    if "portfolio.scope" in rows:
        return
    text = " ".join(
        t
        for key in ("entry", "exit", "filters", "stop", "take_profit")
        for t in _texts(spec.get(key))
    )
    hit = _ONE_POSITION.search(text)
    if hit:
        rows["portfolio.scope"] = _Row(
            fields.INTENT,
            "global",
            "inferred",
            "medium",
            fields.HIGH,
            f'rules say "{hit.group(0)}": one position across the whole universe',
        )
    else:
        rows["portfolio.scope"] = _Row(
            fields.INTENT,
            "per_symbol",
            "inferred",
            "low",
            fields.HIGH,
            "single_position without a scope; one position per symbol assumed",
        )


def _re_entry_row(spec: Mapping[str, Any], rows: dict[str, _Row]) -> None:
    block = spec.get("re_entry")
    if isinstance(block, Mapping) and block.get("allowed") is True and "trigger" not in block:
        rows["re_entry.trigger"] = _Row(
            fields.INTENT,
            "after_stop",
            "platform_default",
            "high",
            fields.get("re_entry.trigger").materiality,
            "default: re-enter after a stop only",
        )


def _execution_rows(
    spec: Mapping[str, Any],
    overrides: Mapping[str, Any],
    profiles_dir: Path | None,
    spec_is_clean: bool,
) -> dict[str, _Row]:
    try:
        resolved = execution.resolve_execution(
            spec, load_profile(str(spec.get("profile")), profiles_dir), overrides
        )
    except (ProfileError, execution.ExecutionError):
        if spec_is_clean:
            raise
        return {}
    return {
        item.field: _Row(
            item.section,
            item.value,
            item.source,
            "high",
            fields.get(item.field).materiality,
            item.reason,
        )
        for item in resolved.values
    }


def _build_rows(
    spec: Mapping[str, Any],
    overrides: Mapping[str, Any],
    profiles_dir: Path | None,
) -> dict[str, _Row]:
    problems = validate_spec(spec, profiles_dir)
    rows = _user_rows(spec)
    if spec.get("entry") is not None:
        _timing_rows(spec, rows)
    _scope_row(spec, rows)
    _re_entry_row(spec, rows)
    rows.update(_execution_rows(spec, overrides, profiles_dir, spec_is_clean=not problems))
    return rows


def _make_entry(entry_id: str, name: str, row: _Row, confirmed: bool) -> Assumption:
    return Assumption(
        entry_id,
        name,
        row.section,
        row.value,
        row.source,
        row.confidence,
        row.materiality,
        row.reason,
        confirmed,
    )


def sealed_next_id(folder: Path) -> int:
    """The lowest id that no sealed run snapshot has used.

    Each run seals its own ledger in `runs/<run_id>/assumptions.json`. A reference to a ledger id
    is always read against that snapshot, so an old run can never be reinterpreted. This floor
    also keeps numbering above every sealed id, so deleting the working `assumptions.yaml` cannot
    restart ids below one a run has cited. Raises LedgerError for an unreadable snapshot.
    """
    floor = 1
    for path in sorted((folder / "runs").glob("*/assumptions.json")):
        if path.parent.name.startswith("."):
            continue  # a half-written temp directory of a run that never committed
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            ids = [int(_ID.match(e["id"]).group(1)) for e in raw["entries"]]
            floor = max(floor, int(raw["next_id"]), max(ids, default=0) + 1)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise LedgerError(f"{path}: sealed ledger snapshot is unreadable ({exc})") from exc
    return floor


def resolve(
    spec: Any,
    overrides: Mapping[str, Any] | None = None,
    existing: Ledger | None = None,
    profiles_dir: Path | None = None,
    floor_next_id: int = 0,
) -> Ledger:
    """Build the ledger for a spec, merged with an existing one.

    Merging keeps an entry's id. Its confirmation is kept only while its value and source are
    unchanged, so a confirmation never carries over to a value the author did not see. Ids are
    never reused, and `floor_next_id` (see `sealed_next_id`) keeps new ids above sealed ones. Works on an incomplete spec: it records what it can and skips the rest.
    Raises ExecutionError for a bad override.
    """
    data = spec if isinstance(spec, Mapping) else {}
    accepted = execution.validate_overrides(overrides or {})
    rows = _build_rows(data, overrides or {}, profiles_dir)
    for name, (value, reason) in accepted.items():
        if name in rows:
            rows[name] = replace(rows[name], value=value, source="user", reason=reason)
    previous = {e.field: e for e in existing.entries} if existing else {}
    highest = max((int(_ID.match(e.id).group(1)) for e in previous.values()), default=0)
    next_id = max(existing.next_id if existing else 0, highest + 1, floor_next_id, 1)
    entries: list[Assumption] = []
    for f in fields.FIELDS:
        row = rows.get(f.name)
        if row is None:
            continue
        old = previous.get(f.name)
        if old is not None:
            entry_id = old.id
            keep = (
                old.user_confirmed
                and old.source == row.source
                and old.materiality == row.materiality
                and type(old.value) is type(row.value)
                and old.value == row.value
            )
        else:
            entry_id, next_id = f"A-{next_id:03d}", next_id + 1
            keep = False
        entries.append(_make_entry(entry_id, f.name, row, row.source == "user" or keep))
    return Ledger(
        tuple(entries),
        existing.open_questions if existing else (),
        existing.blocked_reason if existing else None,
        next_id,
    )


def confirm(ledger: Ledger, ids: Iterable[str]) -> Ledger:
    """A copy of the ledger with the named entries confirmed. Raises LedgerError for an unknown id."""
    wanted = set(ids)
    known = {e.id for e in ledger.entries}
    missing = sorted(wanted - known)
    if missing:
        raise LedgerError(f"unknown assumption id: {', '.join(missing)}")
    entries = tuple(
        replace(e, user_confirmed=True) if e.id in wanted else e for e in ledger.entries
    )
    return replace(ledger, entries=entries)


def _entry_from_raw(raw: Any, path: Path) -> Assumption:
    if not isinstance(raw, Mapping) or set(raw) != set(_ENTRY_KEYS):
        raise LedgerError(f"{path}: every entry needs exactly {', '.join(_ENTRY_KEYS)}")
    problems = []
    if not (isinstance(raw["id"], str) and _ID.match(raw["id"])):
        problems.append("id must look like A-004")
    if raw["section"] not in fields.SECTIONS:
        problems.append(f"section {raw['section']!r} is not one of {', '.join(fields.SECTIONS)}")
    if raw["source"] not in SOURCES:
        problems.append(f"source {raw['source']!r} is not one of {', '.join(SOURCES)}")
    if raw["confidence"] not in CONFIDENCES:
        problems.append(f"confidence {raw['confidence']!r} is not one of {', '.join(CONFIDENCES)}")
    if raw["materiality"] not in fields.MATERIALITIES:
        problems.append(f"materiality {raw['materiality']!r} is not valid")
    if not isinstance(raw["user_confirmed"], bool):
        problems.append("user_confirmed must be true or false")
    if not (isinstance(raw["reason"], str) and raw["reason"].strip()):
        problems.append("reason must be non-empty text")
    if problems:
        raise LedgerError(f"{path}: entry {raw.get('id')}: {'; '.join(problems)}")
    return Assumption(**{key: raw[key] for key in _ENTRY_KEYS})


def load(folder: Path) -> Ledger | None:
    """The folder's working ledger, or None when it has none. Raises LedgerError if invalid."""
    path = folder / LEDGER_FILE
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LedgerError(f"{path}: assumptions.yaml is unreadable ({exc})") from exc
    keys = {"next_id", "open_questions", "blocked_reason", "entries"}
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise LedgerError(f"{path}: expected exactly the keys {', '.join(sorted(keys))}")
    if not isinstance(raw["entries"], list):
        raise LedgerError(f"{path}: entries must be a list")
    entries = tuple(_entry_from_raw(item, path) for item in raw["entries"])
    for label, values in (("id", [e.id for e in entries]), ("field", [e.field for e in entries])):
        if len(values) != len(set(values)):
            raise LedgerError(f"{path}: duplicate {label} in entries")
    questions = raw["open_questions"]
    if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
        raise LedgerError(f"{path}: open_questions must be a list of text")
    if raw["blocked_reason"] is not None and not isinstance(raw["blocked_reason"], str):
        raise LedgerError(f"{path}: blocked_reason must be text or null")
    if not isinstance(raw["next_id"], int) or isinstance(raw["next_id"], bool):
        raise LedgerError(f"{path}: next_id must be an integer")
    return Ledger(entries, tuple(questions), raw["blocked_reason"], raw["next_id"])


def save(folder: Path, ledger: Ledger) -> Path:
    """Write the working ledger atomically."""
    path = folder / LEDGER_FILE
    payload = {
        "next_id": ledger.next_id,
        "open_questions": list(ledger.open_questions),
        "blocked_reason": ledger.blocked_reason,
        "entries": [e.as_dict() for e in ledger.entries],
    }
    folder.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, path)
    return path
