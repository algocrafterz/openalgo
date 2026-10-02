"""Strategy classification: the category taxonomy and the tag rules.

Classification is discovery metadata on the idea. Nothing is computed from it, it gates
nothing, and it must never reach `freeze()`, a fingerprint, a version or a run. This module
therefore only reads `idea.md` frontmatter and `spec.yaml`, and writes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from signal_engine.research.intake.profiles import ProfileError, load_profile

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "taxonomy.yaml"
DEFAULT_CATEGORY = "Other"

_CACHE: dict[Path, tuple[int, int, tuple[str, ...]]] = {}


class TaxonomyError(Exception):
    """taxonomy.yaml is unreadable or invalid, or a category or tag value is not acceptable."""


def _parse(path: Path) -> tuple[str, ...]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TaxonomyError(f"{path}: taxonomy is unreadable ({exc})") from exc
    if not isinstance(raw, dict):
        raise TaxonomyError(f"{path}: taxonomy must be a mapping with a 'categories' list")
    cats = raw.get("categories")
    if cats is None:
        raise TaxonomyError(f"{path}: taxonomy needs a 'categories' list")
    if not isinstance(cats, list):
        raise TaxonomyError(f"{path}: 'categories' must be a list")
    if not cats:
        raise TaxonomyError(f"{path}: 'categories' must be a non-empty list")
    if not all(isinstance(c, str) and c.strip() for c in cats):
        raise TaxonomyError(f"{path}: every category must be non-empty text")
    names = tuple(c.strip() for c in cats)
    if len({n.casefold() for n in names}) != len(names):
        raise TaxonomyError(f"{path}: categories must be unique (case-insensitively)")
    if DEFAULT_CATEGORY not in names:
        raise TaxonomyError(f"{path}: categories must include '{DEFAULT_CATEGORY}'")
    return names


def load_categories(path: Path | None = None) -> tuple[str, ...]:
    """The allowed categories, cached per path and refreshed when the file changes."""
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        stat = target.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _CACHE.pop(target, None)
        return _parse(target)  # raises TaxonomyError naming the unreadable file
    cached = _CACHE.get(target)
    if cached is not None and cached[:2] == stamp:
        return cached[2]
    names = _parse(target)
    _CACHE[target] = (*stamp, names)
    return names


def taxonomy_path(root: Path) -> Path:
    """`root`/research/taxonomy.yaml when it exists, else the shipped taxonomy."""
    own = Path(root) / "research" / "taxonomy.yaml"
    return own if own.exists() else DEFAULT_PATH


def normalise_tags(tags: Any) -> list[str]:
    """Strip each tag and drop case-insensitive duplicates, keeping the first spelling and order."""
    if not isinstance(tags, list):
        raise TaxonomyError("strategy_tags must be a list of text")
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            raise TaxonomyError("every strategy_tags item must be non-empty text")
        clean = tag.strip()
        key = clean.casefold()
        if key not in seen:
            seen.add(key)
            out.append(clean)
    return out


def parse_tags(argument: str) -> list[str]:
    """A comma-separated command-line value as a normalised tag list."""
    return normalise_tags([part for part in argument.split(",") if part.strip()])


def classification_problems(idea: dict[str, Any], categories: tuple[str, ...]) -> list[str]:
    """Problems with an idea's `strategy_category` and `strategy_tags`."""
    problems: list[str] = []
    if "strategy_category" in idea and idea["strategy_category"] not in categories:
        problems.append(
            f"strategy_category '{idea['strategy_category']}' not one of {', '.join(categories)}"
        )
    if "strategy_tags" in idea:
        try:
            normalise_tags(idea["strategy_tags"])
        except TaxonomyError as exc:
            problems.append(str(exc))
    return problems


def category_of(idea: dict[str, Any]) -> str:
    return idea.get("strategy_category") or DEFAULT_CATEGORY


def tags_of(idea: dict[str, Any]) -> list[str]:
    return normalise_tags(idea.get("strategy_tags") or [])


def _market(spec_data: dict[str, Any]) -> str | None:
    name = spec_data.get("profile")
    if not isinstance(name, str):
        return None
    try:
        return load_profile(name).market
    except ProfileError:
        return None


def classification_block(
    idea: dict[str, Any], categories: tuple[str, ...], spec_data: dict[str, Any] | None
) -> dict[str, Any]:
    """The idea's classification: its own category and tags, plus style, direction, timeframe
    and market read from the current valid spec and its deployment profile (None if absent)."""
    data = spec_data if isinstance(spec_data, dict) else {}
    return {
        "strategy_category": category_of(idea),
        "strategy_tags": tags_of(idea),
        "trading_style": data.get("style"),
        "direction": data.get("direction"),
        "timeframe": data.get("timeframe"),
        "market": _market(data) if data else None,
    }
