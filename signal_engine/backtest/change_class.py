"""Classify what differs between two runs, from their manifests.

Components are compared in the order of ORDER. The strategy fingerprint is not compared on its
own because rules plus params already cover it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STRATEGY_CHANGE = "STRATEGY_CHANGE"
PARAMETER_CHANGE = "PARAMETER_CHANGE"
EXECUTION_CHANGE = "EXECUTION_CHANGE"
COST_CHANGE = "COST_CHANGE"
UNIVERSE_CHANGE = "UNIVERSE_CHANGE"
DEPLOYMENT_PROFILE_CHANGE = "DEPLOYMENT_PROFILE_CHANGE"
DATASET_CHANGE = "DATASET_CHANGE"
DATA_RANGE_CHANGE = "DATA_RANGE_CHANGE"
ENGINE_CHANGE = "ENGINE_CHANGE"
RESEARCH_RERUN = "RESEARCH_RERUN"

#: Order of the component table: rules, params, execution, cost, universe, profile, dataset,
#: period, engine.
ORDER = (
    STRATEGY_CHANGE,
    PARAMETER_CHANGE,
    EXECUTION_CHANGE,
    COST_CHANGE,
    UNIVERSE_CHANGE,
    DEPLOYMENT_PROFILE_CHANGE,
    DATASET_CHANGE,
    DATA_RANGE_CHANGE,
    ENGINE_CHANGE,
)

_FINGERPRINT_CLASSES = (
    ("rules", STRATEGY_CHANGE),
    ("params", PARAMETER_CHANGE),
    ("execution", EXECUTION_CHANGE),
    ("cost", COST_CHANGE),
    ("universe", UNIVERSE_CHANGE),
    ("profile", DEPLOYMENT_PROFILE_CHANGE),
    ("dataset", DATASET_CHANGE),
)
_PERIOD_KEYS = ("start", "end")
_ENGINE_KEYS = ("engine_version", "code_commit")


def _section(manifest: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = manifest.get(key)
    return value if isinstance(value, Mapping) else {}


def _differs(a: Mapping[str, Any], b: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    """True if any key differs; a missing key and None are equal, present vs None differ."""
    return any(a.get(key) != b.get(key) for key in keys)


def classify(manifest_a: Mapping[str, Any], manifest_b: Mapping[str, Any]) -> list[str]:
    """Return every changed component in ORDER, or ``[RESEARCH_RERUN]`` if none differ.

    Args:
        manifest_a: The earlier (parent) run manifest.
        manifest_b: The later run manifest.

    Returns:
        A list of change class strings. Inputs are not modified.
    """
    fa, fb = _section(manifest_a, "fingerprints"), _section(manifest_b, "fingerprints")
    changed = {cls for key, cls in _FINGERPRINT_CLASSES if _differs(fa, fb, (key,))}
    if _differs(_section(manifest_a, "period"), _section(manifest_b, "period"), _PERIOD_KEYS):
        changed.add(DATA_RANGE_CHANGE)
    if _differs(_section(manifest_a, "engine"), _section(manifest_b, "engine"), _ENGINE_KEYS):
        changed.add(ENGINE_CHANGE)
    result = [cls for cls in ORDER if cls in changed]
    return result or [RESEARCH_RERUN]


def first_run_classes() -> list[str]:
    """Change classes of an idea's first run: none, since there is nothing to compare with."""
    return []
