"""Targeted edits of an idea's `idea.md` frontmatter.

`set_keys` changes only the lines of the keys it is given: an existing key is replaced where
it stands, an absent key is added at the end of the block, and everything else (other keys,
comments, the body, the line-ending style) is left byte for byte as it was. The block is never
re-serialised, so a hand-written file keeps its layout.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?=\r?\n|\Z)", re.DOTALL)
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WIDE = 10**9


class FrontmatterError(Exception):
    """idea.md is missing or has no frontmatter block, or a key name is not acceptable."""


def _split(folder: Path) -> tuple[Path, str, tuple[int, int]]:
    path = folder / "idea.md"
    if not path.exists():
        raise FrontmatterError(f"{path}: idea.md does not exist")
    raw = path.read_bytes().decode("utf-8")
    match = FRONTMATTER.match(raw)
    if match is None:
        raise FrontmatterError(f"{path}: no frontmatter block (--- ... ---) to update")
    return path, raw, (match.start(1), match.end(1))


def read(folder: Path) -> dict[str, Any]:
    """The idea's frontmatter as a mapping. Raises FrontmatterError when unreadable."""
    path, raw, (start, end) = _split(folder)
    try:
        data = yaml.safe_load(raw[start:end])
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"{path}: frontmatter is unreadable ({exc})") from exc
    if not isinstance(data, dict):
        raise FrontmatterError(f"{path}: frontmatter is not a mapping")
    return data


def _render(key: str, value: Any) -> str:
    """One `key: value` line, quoted by yaml so the value always reads back unchanged."""
    text = yaml.safe_dump(
        {key: value}, default_flow_style=True, allow_unicode=True, width=_WIDE, sort_keys=False
    )
    # Flow style wraps the pair in braces: "{key: value}\n". Peel them off for block style.
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    if "\n" in text:
        # yaml folded a multi-line string over several lines; JSON is valid YAML and one line.
        return f"{key}: {json.dumps(value, ensure_ascii=False)}"
    return text


def _is_continuation(line: str) -> bool:
    """A line that belongs to the preceding key: indented, or a zero-indent list item."""
    return line[:1] in (" ", "\t") or line.startswith("- ") or line.rstrip() == "-"


def _replace_key(lines: list[str], key: str, new_line: str) -> bool:
    """Replace the last occurrence of `key` with `new_line`; drop earlier duplicates.

    YAML takes the last of a repeated key, so that is the one that must change. Only top-level
    lines match (an indented line belongs to its parent key), and `key` must be the whole key
    name, quoted or followed by spaces before the colon.
    """
    pattern = re.compile(rf"""^["']?{re.escape(key)}["']?\s*:""")
    spans: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if pattern.match(line):
            end = i + 1
            while end < len(lines) and _is_continuation(lines[end]):
                end += 1
            spans.append((i, end))
    if not spans:
        return False
    for start, end in reversed(spans[:-1]):
        del lines[start:end]
    start, end = spans[-1]
    shift = sum(e - s for s, e in spans[:-1])
    lines[start - shift : end - shift] = [new_line]
    return True


def _write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory and os.replace; keep the file mode."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def set_keys(folder: Path, updates: dict[str, Any]) -> None:
    """Set the given frontmatter keys, changing no other line. Writes nothing on an error."""
    for key in updates:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise FrontmatterError(f"unacceptable frontmatter key {key!r}")
    path, raw, (start, end) = _split(folder)
    if not updates:
        return
    rendered = {key: _render(key, value) for key, value in updates.items()}
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw[start:end].split(nl)
    for key, new_line in rendered.items():
        if not _replace_key(lines, key, new_line):
            lines.append(new_line)
    _write_atomic(path, (raw[:start] + nl.join(lines) + raw[end:]).encode("utf-8"))


def _remove_key(lines: list[str], key: str) -> bool:
    """Delete every top-level occurrence of `key` with its continuation lines."""
    pattern = re.compile(rf"""^["']?{re.escape(key)}["']?\s*:""")
    spans: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if pattern.match(line):
            end = i + 1
            while end < len(lines) and _is_continuation(lines[end]):
                end += 1
            spans.append((i, end))
    for start, end in reversed(spans):
        del lines[start:end]
    return bool(spans)


def remove_keys(folder: Path, keys: list[str]) -> None:
    """Delete the given frontmatter keys, changing no other line. Absent keys are ignored and
    nothing is written when nothing changes."""
    for key in keys:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise FrontmatterError(f"unacceptable frontmatter key {key!r}")
    path, raw, (start, end) = _split(folder)
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw[start:end].split(nl)
    changed = False
    for key in keys:
        changed = _remove_key(lines, key) or changed
    if changed:
        _write_atomic(path, (raw[:start] + nl.join(lines) + raw[end:]).encode("utf-8"))
