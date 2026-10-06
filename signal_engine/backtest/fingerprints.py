"""Per-component fingerprints that identify a backtest run.

Re-exports the strategy fingerprints from ``research.intake.fingerprints`` and adds the
execution, cost, universe, profile and dataset components. Every digest is SHA-256 over
canonical JSON, so key order never matters. Resolved-assumption ledger ids are never hashed,
only values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from signal_engine.research.intake.fingerprints import (
    canonical_json,
    params_fingerprint,
    rules_fingerprint,
    sha256_hex,
    strategy_fingerprint,
)

__all__ = [
    "canonical_json",
    "component_fingerprints",
    "cost_fingerprint",
    "execution_fingerprint",
    "params_fingerprint",
    "profile_fingerprint",
    "rules_fingerprint",
    "sha256_hex",
    "strategy_fingerprint",
    "universe_fingerprint",
]

SLIPPAGE_FIELD = "execution.slippage_bps_per_side"
ROUND_TRIP_FIELD = "execution.cost_bps_round_trip"
COST_FIELDS = (SLIPPAGE_FIELD, ROUND_TRIP_FIELD)


def execution_fingerprint(resolved: Mapping[str, Mapping[str, Any]]) -> str:
    """Hash the values of the non-cost ``execution.*`` fields.

    Args:
        resolved: ``{field: {"value": ..., "ledger_id": ...}}``.

    Returns:
        A 64-character hex digest. Ledger ids and ``profile.*`` fields are ignored.
    """
    values = {
        name: entry["value"]
        for name, entry in resolved.items()
        if name.startswith("execution.") and name not in COST_FIELDS
    }
    return sha256_hex(canonical_json(values))


def cost_fingerprint(resolved: Mapping[str, Mapping[str, Any]], cost_model: Any) -> str:
    """Hash slippage and the primary cost identity (a JSON-able mapping or None).

    The frozen flat round-trip figure is only a comparison scenario now, so it is not hashed:
    changing it must not create a new trial.
    """
    payload = {
        "slippage": resolved[SLIPPAGE_FIELD]["value"],
        "model": cost_model,
    }
    return sha256_hex(canonical_json(payload))


def universe_fingerprint(symbols: Iterable[str], definition: str) -> str:
    """Hash the sorted unique symbols plus the universe definition string."""
    payload = {"symbols": sorted(set(symbols)), "definition": definition}
    return sha256_hex(canonical_json(payload))


def profile_fingerprint(profile_content: Mapping[str, Any]) -> str:
    """Hash the deployment profile content (equals ``Profile.sha256`` for ASCII content)."""
    return sha256_hex(canonical_json(dict(profile_content)))


def component_fingerprints(
    *,
    intent: Mapping[str, Any],
    resolved: Mapping[str, Mapping[str, Any]],
    cost_model: Any,
    symbols: Iterable[str],
    universe_definition: str,
    profile_content: Mapping[str, Any],
    dataset_fingerprint: str,
) -> dict[str, str]:
    """Compute every component fingerprint of a run."""
    rules = rules_fingerprint(intent)
    params = params_fingerprint(intent.get("parameters"))
    return {
        "rules": rules,
        "params": params,
        "strategy": strategy_fingerprint(rules, params),
        "execution": execution_fingerprint(resolved),
        "cost": cost_fingerprint(resolved, cost_model),
        "universe": universe_fingerprint(symbols, universe_definition),
        "profile": profile_fingerprint(profile_content),
        "dataset": dataset_fingerprint,
    }
