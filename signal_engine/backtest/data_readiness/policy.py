"""The data-readiness policy: thresholds loaded from a versioned YAML file.

Every value is required and unknown keys are rejected, so a typo cannot silently fall back to a
default. The version is the sha256 of the parsed values, so comments and key order do not change
it, but any value change does.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from signal_engine.research.intake.fingerprints import canonical_json, sha256_hex

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "research" / "data_policy.yaml"
SCHEMA = 1

_SHARES = {
    "thresholds": ("bad_bar_share", "max_missing_session_share", "zero_volume_share",
                   "placeholder_volume_ratio", "calendar_date_symbol_share",
                   "latest_session_min_bar_share"),
    "exclusions": ("max_defect_excluded_symbol_share",),
    "adjustment": ("cross_basis_tolerance", "jump_ratio_tolerance"),
}
_COUNTS = {
    "thresholds": ("placeholder_lookback_sessions",),
    "exclusions": ("max_defect_excluded_symbols",),
    "diagnostics": ("stale_repeat_sessions",),
    "adjustment": ("cross_basis_min_sessions",),
}
_POSITIVE = {
    "diagnostics": ("spike_return", "cross_volume_ratio_low", "cross_volume_ratio_high"),
    "adjustment": ("jump_min_move",),
}
#: Non-empty lists of ratios strictly between 0 and 1.
_RATIO_LISTS = {"adjustment": ("jump_ratios",)}
_TIMES = {"thresholds": ("daily_close_time",)}
#: Counts that must be at least 1 (a zero lookback is an invalid window frame).
_AT_LEAST_ONE = {"thresholds": ("placeholder_lookback_sessions",),
                 "adjustment": ("cross_basis_min_sessions",)}
_HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


class PolicyError(ValueError):
    """The policy file is missing, malformed or out of range."""


@dataclass(frozen=True)
class Policy:
    """Validated policy values plus the content version."""

    bad_bar_share: float
    max_missing_session_share: float
    zero_volume_share: float
    placeholder_volume_ratio: float
    placeholder_lookback_sessions: int
    calendar_date_symbol_share: float
    latest_session_min_bar_share: float
    daily_close_time: str
    max_defect_excluded_symbols: int
    max_defect_excluded_symbol_share: float
    spike_return: float
    stale_repeat_sessions: int
    cross_volume_ratio_low: float
    cross_volume_ratio_high: float
    cross_basis_tolerance: float
    cross_basis_min_sessions: int
    jump_min_move: float
    jump_ratio_tolerance: float
    jump_ratios: tuple[float, ...]
    version: str
    source: str


def _expected() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for table in (_SHARES, _COUNTS, _POSITIVE, _TIMES, _RATIO_LISTS):
        for section, keys in table.items():
            out.setdefault(section, set()).update(keys)
    return out


def _check_section(doc: dict, section: str, keys: set[str]) -> dict:
    block = doc.get(section)
    if not isinstance(block, dict):
        raise PolicyError(f"policy section '{section}' is missing")
    missing = sorted(keys - block.keys())
    if missing:
        raise PolicyError(f"policy '{section}' is missing {', '.join(missing)}")
    unknown = sorted(block.keys() - keys)
    if unknown:
        raise PolicyError(f"policy '{section}' has unknown key {', '.join(unknown)}")
    return block


def _is_number(v: Any) -> bool:
    """A finite int or float; NaN and infinity are rejected (`nan <= 0` is False)."""
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def _check_values(doc: dict) -> None:
    for section, keys in _SHARES.items():
        for k in keys:
            v = doc[section][k]
            if not _is_number(v) or not 0 <= v <= 1:
                raise PolicyError(f"{section}.{k} must be a share between 0 and 1, got {v!r}")
    for section, keys in _COUNTS.items():
        for k in keys:
            v = doc[section][k]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise PolicyError(f"{section}.{k} must be a non-negative integer, got {v!r}")
    for section, keys in _POSITIVE.items():
        for k in keys:
            v = doc[section][k]
            if not _is_number(v) or v <= 0:
                raise PolicyError(f"{section}.{k} must be a positive number, got {v!r}")
    for section, keys in _TIMES.items():
        for k in keys:
            v = doc[section][k]
            if not isinstance(v, str) or not _HHMM.fullmatch(v):
                raise PolicyError(f"{section}.{k} must be HH:MM (00:00-23:59), got {v!r}")
    for section, keys in _AT_LEAST_ONE.items():
        for k in keys:
            if doc[section][k] < 1:
                raise PolicyError(f"{section}.{k} must be at least 1, got {doc[section][k]!r}")
    for section, keys in _RATIO_LISTS.items():
        for k in keys:
            v = doc[section][k]
            if not isinstance(v, list) or not v \
                    or not all(_is_number(x) and 0 < x < 1 for x in v):
                raise PolicyError(f"{section}.{k} must be a non-empty list of ratios strictly "
                                  f"between 0 and 1, got {v!r}")
    d = doc["diagnostics"]
    if d["cross_volume_ratio_low"] >= d["cross_volume_ratio_high"]:
        raise PolicyError("diagnostics.cross_volume_ratio_low must be below the high bound")


def load_policy(path: Path | str = DEFAULT_PATH) -> Policy:
    """Load and validate the policy file. Raises PolicyError on any problem."""
    p = Path(path)
    if not p.is_file():
        raise PolicyError(f"policy file not found: {p}")
    try:
        doc: Any = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy file is not valid YAML: {p}: {exc}") from exc
    if not isinstance(doc, dict):
        raise PolicyError(f"policy file must hold a mapping: {p}")
    if doc.get("schema") != SCHEMA:
        raise PolicyError(f"policy schema must be {SCHEMA}, got {doc.get('schema')!r}")
    expected = _expected()
    unknown = sorted(doc.keys() - expected.keys() - {"schema"})
    if unknown:
        raise PolicyError(f"policy has unknown section {', '.join(unknown)}")
    flat: dict[str, Any] = {}
    for section, keys in expected.items():
        flat.update(_check_section(doc, section, keys))
    _check_values(doc)
    for section, keys in _RATIO_LISTS.items():
        flat.update({k: tuple(float(x) for x in doc[section][k]) for k in keys})
    version = sha256_hex(canonical_json(doc))
    return Policy(**flat, version=version, source=str(p))


def exclusion_thresholds(policy: Policy) -> dict[str, Any]:
    """The defect-exclusion thresholds, as recorded in reports."""
    return {
        "max_defect_excluded_symbols": policy.max_defect_excluded_symbols,
        "max_defect_excluded_symbol_share": policy.max_defect_excluded_symbol_share,
    }
