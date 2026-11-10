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


#: The session basis of a run that trades the symbols' own sessions. It is the behaviour of every
#: run before the setting existed, so it adds nothing to the digest and old fingerprints stay equal.
SESSION_BASIS_LEGACY = "own"
SESSION_BASIS_KEY = "session_basis"


def execution_fingerprint(
    resolved: Mapping[str, Mapping[str, Any]], session_basis: str | None = None
) -> str:
    """Hash the values of the non-cost ``execution.*`` fields and the session basis.

    Args:
        resolved: ``{field: {"value": ..., "ledger_id": ...}}``.
        session_basis: The run's session basis (``common`` or ``own``), a data-layer setting that
            decides which sessions each symbol trades. None or ``own`` (the behaviour before the
            setting existed) leaves the digest unchanged; ``common`` is part of it.

    Returns:
        A 64-character hex digest. Ledger ids and ``profile.*`` fields are ignored.
    """
    values = {
        name: entry["value"]
        for name, entry in resolved.items()
        if name.startswith("execution.") and name not in COST_FIELDS
    }
    if session_basis not in (None, SESSION_BASIS_LEGACY):
        values[SESSION_BASIS_KEY] = session_basis
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
    session_basis: str | None = None,
) -> dict[str, str]:
    """Compute every component fingerprint of a run."""
    rules = rules_fingerprint(intent)
    params = params_fingerprint(intent.get("parameters"))
    return {
        "rules": rules,
        "params": params,
        "strategy": strategy_fingerprint(rules, params),
        "execution": execution_fingerprint(resolved, session_basis),
        "cost": cost_fingerprint(resolved, cost_model),
        "universe": universe_fingerprint(symbols, universe_definition),
        "profile": profile_fingerprint(profile_content),
        "dataset": dataset_fingerprint,
    }
