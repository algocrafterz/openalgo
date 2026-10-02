"""Strategy versions: an append-only record of every distinct strategy an idea has had.

`ideas/<idea>/versions.yaml` is a block-style list of `{version, strategy_fingerprint, parent,
created_ist, note}`. A version is immutable: changing a rule or a parameter changes the
fingerprint, which makes a new version. An existing entry is never rewritten, because new
entries are appended to the file's text rather than the file being re-serialised.

Labels are v1, v2, ... generated automatically, with an optional branch label such as v3b.
The idea's frontmatter points at versions with `current_version` and `promoted_version`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from signal_engine.research.intake import frontmatter
from signal_engine.timeutils import IST

VERSIONS_FILE = "versions.yaml"
POINTER_KEYS = ("current_version", "promoted_version")

_LABEL = re.compile(r"^v([1-9]\d*)[a-z]?$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_KEYS = ("version", "strategy_fingerprint", "parent", "created_ist", "note")
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?=\r?\n|\Z)", re.DOTALL)


def require_aware(now: datetime | None) -> None:
    """Raise ValueError for a naive datetime: it would silently be read as local time."""
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("now must be timezone-aware")


class VersionError(Exception):
    """versions.yaml is invalid, or a version, parent, label or pointer is not acceptable."""


@dataclass(frozen=True)
class Version:
    version: str
    strategy_fingerprint: str
    parent: str | None
    created_ist: str
    note: str


@dataclass(frozen=True)
class VersionResult:
    """The version a strategy fingerprint resolved to."""

    version: str
    created: bool
    parent: str | None


def _check_row(path: Path | str, row: Any) -> Version:
    if not isinstance(row, dict):
        raise VersionError(f"{path}: every entry must be a mapping")
    for key in _KEYS:
        if key not in row:
            raise VersionError(f"{path}: entry {row.get('version')!r} is missing {key}")
    entry = Version(*(row[k] for k in _KEYS))
    if not (isinstance(entry.version, str) and _LABEL.fullmatch(entry.version)):
        raise VersionError(f"{path}: invalid version label {entry.version!r}")
    if not (
        isinstance(entry.strategy_fingerprint, str)
        and _FINGERPRINT.fullmatch(entry.strategy_fingerprint)
    ):
        raise VersionError(f"{path}: {entry.version} has an invalid strategy fingerprint")
    if entry.parent is not None and not (
        isinstance(entry.parent, str) and _LABEL.fullmatch(entry.parent)
    ):
        raise VersionError(f"{path}: {entry.version} has an invalid parent {entry.parent!r}")
    if not (isinstance(entry.created_ist, str) and isinstance(entry.note, str)):
        raise VersionError(f"{path}: {entry.version} needs text created_ist and note")
    return entry


def parse_versions(text: str, source: str = VERSIONS_FILE) -> list[Version]:
    """Parse and validate versions.yaml text. `source` names the file in error messages.

    Raises VersionError for invalid text. Callers that already hold the text (and have
    size-checked it) use this instead of `load_versions`, which would re-read the file.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise VersionError(f"{source}: versions.yaml is unreadable ({exc})") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise VersionError(f"{source}: versions.yaml must be a list")
    found: list[Version] = []
    for row in raw:
        entry = _check_row(source, row)
        if any(entry.version == v.version for v in found):
            raise VersionError(f"{source}: duplicate version {entry.version}")
        if any(entry.strategy_fingerprint == v.strategy_fingerprint for v in found):
            raise VersionError(f"{source}: duplicate fingerprint on {entry.version}")
        if entry.parent is not None and not any(entry.parent == v.version for v in found):
            raise VersionError(
                f"{source}: parent {entry.parent} of {entry.version} must be an earlier version"
            )
        found.append(entry)
    return found


def load_versions(folder: Path) -> list[Version]:
    """The idea's versions in file order. Raises VersionError for an invalid file."""
    path = folder / VERSIONS_FILE
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionError(f"{path}: versions.yaml is unreadable ({exc})") from exc
    return parse_versions(text, str(path))


def _next_label(existing: list[Version]) -> str:
    numbers = [int(_LABEL.match(v.version).group(1)) for v in existing]
    return f"v{max(numbers, default=0) + 1}"


def _is_block_style(text: str) -> bool:
    """True for an empty or comment-only file, or one whose first content line is a list item."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped.startswith("- ")
    return True


def _append(folder: Path, entry: Version) -> None:
    path = folder / VERSIONS_FILE
    existing = path.read_bytes().decode("utf-8") if path.exists() else ""
    if not _is_block_style(existing):
        raise VersionError(f"{path}: must be a block-style list to append to (one '- ' per entry)")
    row = {
        "version": entry.version,
        "strategy_fingerprint": entry.strategy_fingerprint,
        "parent": entry.parent,
        "created_ist": entry.created_ist,
        "note": entry.note,
    }
    block = yaml.safe_dump([row], sort_keys=False, allow_unicode=True)
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    folder.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(prefix + block)


def resolve_version(
    folder: Path,
    strategy_fingerprint: str,
    *,
    default_parent: str | None = None,
    parent: str | None = None,
    label: str | None = None,
    note: str = "",
    now: datetime | None = None,
    dry_run: bool = False,
) -> VersionResult:
    """Reuse the version of a known fingerprint, or append the next one.

    A new version's parent is `parent` if given, else `default_parent` (the idea's current
    version). A known fingerprint ignores `label` and `parent`. Raises VersionError before
    writing anything when the parent or label is not acceptable. With `dry_run` it performs
    every check and returns what would happen, but writes nothing.
    """
    require_aware(now)
    existing = load_versions(folder)
    for entry in existing:
        if entry.strategy_fingerprint == strategy_fingerprint:
            return VersionResult(entry.version, False, entry.parent)
    chosen_parent = parent if parent is not None else default_parent
    known = {v.version for v in existing}
    if chosen_parent is not None and chosen_parent not in known:
        raise VersionError(f"parent '{chosen_parent}' is not a registered version")
    if label is not None:
        if not _LABEL.fullmatch(label):
            raise VersionError(f"invalid label '{label}': use v<number> with an optional letter")
        if label in known:
            raise VersionError(f"version label '{label}' is already used")
    version = label or _next_label(existing)
    if not dry_run:
        stamp = (now or datetime.now(IST)).astimezone(IST).isoformat(timespec="seconds")
        _append(folder, Version(version, strategy_fingerprint, chosen_parent, stamp, note))
    return VersionResult(version, True, chosen_parent)


def _frontmatter_span(text: str) -> tuple[int, int] | None:
    match = _FRONTMATTER.match(text)
    return (match.start(1), match.end(1)) if match else None


def read_pointers(folder: Path) -> dict[str, Any]:
    """The idea's current_version and promoted_version, each None when unset."""
    empty = dict.fromkeys(POINTER_KEYS)
    path = folder / "idea.md"
    if not path.exists():
        return empty
    text = path.read_text(encoding="utf-8")
    span = _frontmatter_span(text)
    if span is None:
        return empty
    try:
        data = yaml.safe_load(text[span[0] : span[1]])
    except yaml.YAMLError as exc:
        raise VersionError(f"{path}: frontmatter is unreadable ({exc})") from exc
    if not isinstance(data, dict):
        return empty
    return {key: data.get(key) for key in POINTER_KEYS}


def set_pointer(folder: Path, key: str, version: str) -> None:
    """Point the idea's frontmatter `key` at a registered version, changing only that line."""
    if key not in POINTER_KEYS:
        raise VersionError(f"unknown pointer key '{key}', expected {', '.join(POINTER_KEYS)}")
    if version not in {v.version for v in load_versions(folder)}:
        raise VersionError(f"{version} is not a registered version of this idea")
    try:
        frontmatter.set_keys(folder, {key: version})
    except frontmatter.FrontmatterError as exc:
        raise VersionError(str(exc)) from exc
