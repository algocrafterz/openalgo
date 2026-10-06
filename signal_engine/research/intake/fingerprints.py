"""Fingerprints that identify a strategy version.

A strategy is its rules plus its parameters. The rules fingerprint covers the effective
intent minus parameters and minus metadata that does not change behaviour (name, source,
rationale, disclosure) and minus the deployment profile, which has its own fingerprint: a
profile switch is a configuration change, not a new strategy. Every digest is SHA-256 over
canonical JSON, so key order and YAML formatting never matter.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

#: Intent keys that do not change what the strategy does.
NON_RULE_KEYS = (
    "schema_version",
    "name",
    "source",
    "profile",
    "parameters",
    "disclosure",
    "costs",
    "rationale",
    "kill_criterion",
)


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, non-ASCII kept."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rules_fingerprint(intent: Mapping[str, Any]) -> str:
    """Fingerprint of everything in the intent that decides what the strategy does."""
    rules = {k: v for k, v in intent.items() if k not in NON_RULE_KEYS}
    return sha256_hex(canonical_json(rules))


def params_fingerprint(parameters: Any) -> str:
    """Fingerprint of the tunable values. The word 'none' and an empty mapping are equal."""
    if not isinstance(parameters, Mapping):
        parameters = {}
    return sha256_hex(canonical_json(dict(parameters)))


def strategy_fingerprint(rules: str, params: str) -> str:
    """The identity of a strategy version: its rules and its parameters."""
    return sha256_hex(canonical_json({"rules": rules, "params": params}))
