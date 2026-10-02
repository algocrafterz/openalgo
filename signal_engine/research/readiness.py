"""Parse the backtest engine readiness doc into capability rows.

The run manifest uses this to list engine capabilities that are not yet verified
(`missing_capabilities`). Only the tier tables under the P1, P2 and P3 headings are read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "docs" / "BACKTEST-ENGINE-READINESS.md"
TIERS = ("P1", "P2", "P3")
STATUSES = ("not started", "in progress", "built", "verified")
HEADER = ("capability", "status", "code", "tests", "verified")

_TIER_RE = re.compile(r"^##\s+(P[123])\b")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_STATUS_RE = re.compile("^(" + "|".join(STATUSES) + r")\b", re.IGNORECASE)
_PIPE_RE = re.compile(r"(?<!\\)\|")


class ReadinessError(Exception):
    """Raised when the readiness document is malformed."""


@dataclass(frozen=True)
class Row:
    """One capability row of a tier table.

    Attributes:
        tier: "P1", "P2" or "P3".
        capability: Capability text.
        status: Normalised status keyword.
        detail: Remainder of the status cell after the keyword (date, notes).
        code: Code cell.
        tests: Tests cell.
        verified: Verified cell.
    """

    tier: str
    capability: str
    status: str
    detail: str
    code: str
    tests: str
    verified: str


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [c.strip() for c in _PIPE_RE.split(inner)]


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def _parse_row(tier: str, cells: list[str], where: str) -> Row:
    if len(cells) != len(HEADER):
        raise ReadinessError(f"{where}: expected {len(HEADER)} columns, found {len(cells)}")
    capability, status_cell, code, tests, verified = cells
    if not capability:
        raise ReadinessError(f"{where}: empty capability")
    match = _STATUS_RE.match(status_cell)
    if not match:
        raise ReadinessError(
            f"{where}: status {status_cell!r} must start with one of {', '.join(STATUSES)}"
        )
    detail = status_cell[match.end() :].strip()
    return Row(tier, capability, match.group(1).lower(), detail, code, tests, verified)


def parse_text(text: str, source: str = "<text>") -> list[Row]:
    """Parse readiness markdown text.

    Args:
        text: Full markdown text.
        source: Name used in error messages.

    Returns:
        Rows of the P1, P2 and P3 tables in document order.

    Raises:
        ReadinessError: On a malformed table, or a missing P1 table.
    """
    rows: list[Row] = []
    seen: dict[str, set[str]] = {}
    tier: str | None = None
    tier_line = 0
    header_seen = False
    tables: set[str] = set()

    def close() -> None:
        if tier is not None and not header_seen:
            raise ReadinessError(f"{source} line {tier_line}: section {tier} has no table")

    for lineno, line in enumerate(text.splitlines(), start=1):
        tier_match = _TIER_RE.match(line)
        if tier_match or _HEADING_RE.match(line):
            close()
            tier = tier_match.group(1) if tier_match else None
            tier_line, header_seen = lineno, False
            if tier:
                seen.setdefault(tier, set())
            continue
        if tier is None or not line.strip().startswith("|"):
            continue
        where = f"{source} line {lineno} (section {tier})"
        cells = _cells(line)
        if not header_seen:
            if tuple(c.lower() for c in cells) != HEADER:
                raise ReadinessError(f"{where}: unexpected table header")
            header_seen = True
            tables.add(tier)
            continue
        if _is_separator(cells):
            continue
        row = _parse_row(tier, cells, where)
        if row.capability in seen[tier]:
            raise ReadinessError(f"{where}: duplicate capability {row.capability!r}")
        seen[tier].add(row.capability)
        rows.append(row)
    close()
    if "P1" not in tables:
        raise ReadinessError(f"{source}: missing P1 table")
    return rows


def parse(path: Path | str | None = None) -> list[Row]:
    """Parse the readiness doc.

    Args:
        path: Markdown file; defaults to the shipped readiness doc.

    Returns:
        Rows of the P1, P2 and P3 tables in document order.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    return parse_text(target.read_text(encoding="utf-8"), source=str(target))


def missing(tier: str = "P1", path: Path | str | None = None) -> list[str]:
    """List capabilities of a tier whose status is not verified.

    Args:
        tier: "P1", "P2" or "P3".
        path: Markdown file; defaults to the shipped readiness doc.

    Returns:
        Capability texts in document order.
    """
    return [r.capability for r in parse(path) if r.tier == tier and r.status != "verified"]


def missing_p1(path: Path | str | None = None) -> list[str]:
    """Return the unverified P1 capabilities."""
    return missing("P1", path)
