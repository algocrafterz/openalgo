"""Validation of an analysis the trader pasted back from claude.ai.

The backend never calls a model. The trader copies the prompt into claude.ai,
gets an answer, and may paste it back here. This module only checks that the
pasted text follows the response schema (version 1) and cross-checks it
against the labels the server computed. Nothing is stored.

The constants below are the single source of truth: `schema_text` (used by the
prompt) and `validate_response` are both generated from them.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from services.performance_eval.dataset import (
    LABEL_INSUFFICIENT,
    LABEL_NEGATIVE,
    LABEL_NO_EDGE,
    LABEL_POSSIBLE_EDGE,
)

SCHEMA_VERSION = 1
#: The only assessments a response may use. Same four labels as the dataset.
LABELS = (LABEL_INSUFFICIENT, LABEL_NO_EDGE, LABEL_POSSIBLE_EDGE, LABEL_NEGATIVE)
CONFIDENCES = ("low", "medium", "high")
OBSERVATION_KINDS = ("observed", "inference", "hypothesis")

MAX_RESPONSE_CHARS = 60_000
MAX_PROBLEMS = 20
MAX_SUMMARY = 1200
MAX_NAME = 64
MAX_OBSERVATION_TEXT = 600
MAX_EVIDENCE_PATH = 120
MAX_EVIDENCE_PER_OBSERVATION = 8
MAX_TEXT_ITEM = 400
MAX_STRATEGIES = 20
MAX_OBSERVATIONS = 12
MAX_WEAKNESSES = 8
MAX_SUGGESTED_TESTS = 8
MAX_OVERLAP_NOTES = 6
MAX_LIMITATIONS = 12

TOP_LEVEL_KEYS = (
    "schema_version",
    "package_hash",
    "summary",
    "overall_confidence",
    "strategies",
    "portfolio",
    "limitations",
)
_REQUIRED_TOP = tuple(k for k in TOP_LEVEL_KEYS if k != "portfolio")
_STRATEGY_KEYS = (
    "name",
    "assessment",
    "n",
    "confidence",
    "observations",
    "weaknesses",
    "suggested_tests",
)
_PORTFOLIO_KEYS = ("assessment", "observations", "overlap_notes")
_OBSERVATION_KEYS = ("text", "kind", "evidence", "n")

INVALID_MESSAGE = (
    "The pasted analysis could not be accepted. Fix the items listed below, "
    "or ask Claude to repeat the answer in the requested format, then paste it again."
)
NOT_JSON_MESSAGE = (
    "The pasted text has no JSON block. Ask Claude to repeat the answer and end it "
    "with the JSON block described in the prompt, then paste it again."
)
TOO_LONG_MESSAGE = f"The pasted text is too long. Paste at most {MAX_RESPONSE_CHARS} characters."
HASH_MISMATCH_WARNING = "This analysis was written for different data than the current selection."

_FENCE_OPENER = re.compile(r"[ \t]*```[ \t]*json[ \t]*", re.IGNORECASE)
MAX_FENCE_OPENERS = 20
TOO_MANY_FENCES_MESSAGE = (
    "The pasted text has too many code blocks. Ask Claude to repeat the answer with "
    "one JSON block at the end, then paste it again."
)
_HTMLISH = re.compile(r"<\s*/?\s*[A-Za-z!?]")
_JS_URL = re.compile(r"java\s*script\s*:", re.IGNORECASE)


def schema_text(package_hash: str | None = None) -> str:
    """The response schema as text for the prompt, generated from the constants above."""
    hash_hint = package_hash or "<copy the package_hash given in the prompt>"
    labels = " | ".join(f'"{x}"' for x in LABELS)
    conf = " | ".join(f'"{x}"' for x in CONFIDENCES)
    kinds = " | ".join(f'"{x}"' for x in OBSERVATION_KINDS)
    observation = (
        f'{{"text": "<=%d characters", "kind": {kinds}, '
        f'"evidence": ["dataset path, <=%d characters each, at most %d, optional"], '
        f'"n": whole number or null}}'
    ) % (MAX_OBSERVATION_TEXT, MAX_EVIDENCE_PATH, MAX_EVIDENCE_PER_OBSERVATION)
    return "\n".join(
        [
            "{",
            f'  "schema_version": {SCHEMA_VERSION},',
            f'  "package_hash": "{hash_hint}",',
            f'  "summary": "plain text, at most {MAX_SUMMARY} characters",',
            f'  "overall_confidence": {conf},',
            f'  "strategies": [   // at most {MAX_STRATEGIES}, one entry per selected strategy',
            "    {",
            f'      "name": "exact strategy name from the data, at most {MAX_NAME} characters",',
            f'      "assessment": {labels},',
            '      "n": number of closed trades, whole number, 0 or more,',
            f'      "confidence": {conf},',
            f'      "observations": [{observation}],   // at most {MAX_OBSERVATIONS}',
            f'      "weaknesses": ["text, at most {MAX_TEXT_ITEM} characters"],   // at most {MAX_WEAKNESSES}',
            f'      "suggested_tests": ["a test to run, at most {MAX_TEXT_ITEM} characters"]   // at most {MAX_SUGGESTED_TESTS}',
            "    }",
            "  ],",
            '  "portfolio": {   // or null',
            f'    "assessment": {labels},',
            f'    "observations": [same shape as above],   // at most {MAX_OBSERVATIONS}',
            f'    "overlap_notes": ["text, at most {MAX_TEXT_ITEM} characters"]   // at most {MAX_OVERLAP_NOTES}',
            "  },",
            f'  "limitations": ["text, at most {MAX_TEXT_ITEM} characters"]   // 1 to {MAX_LIMITATIONS} items, never empty',
            "}",
            "Plain text only inside every string. No markup, no links.",
            "The comments after // are for you; do not put comments in the JSON itself.",
        ]
    )


class _Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []
        self.total = 0

    def add(self, path: str, message: str) -> None:
        self.total += 1
        if len(self.items) < MAX_PROBLEMS:
            self.items.append({"path": path, "message": message})

    def __bool__(self) -> bool:
        return self.total > 0


def _clean(text: str) -> str:
    """Strip, and remove control characters (a newline and a tab are kept)."""
    kept = [c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf")]
    return "".join(kept).strip()


def _fenced_block(text: str) -> tuple[str | None, bool]:
    """The body of the first json fence, found in one pass over the lines.

    Returns (body or None, too_many_openers). Later fences are ignored.
    """
    lines = text.splitlines()
    openers = sum(1 for line in lines if _FENCE_OPENER.fullmatch(line))
    if openers > MAX_FENCE_OPENERS:
        return None, True
    for i, line in enumerate(lines):
        if not _FENCE_OPENER.fullmatch(line):
            continue
        for j in range(i + 1, len(lines)):
            closing = lines[j].rstrip()
            if closing.endswith("```"):
                body = [*lines[i + 1 : j], closing[:-3]]
                return "\n".join(body), False
        return None, False
    return None, False


def _extract_json(text: str) -> tuple[Any, str | None]:
    """The parsed JSON from pasted text: the first json fence, else the whole text."""
    block, too_many = _fenced_block(text)
    if too_many:
        return None, TOO_MANY_FENCES_MESSAGE
    candidate = block if block is not None else text
    try:
        return json.loads(candidate), None
    except (ValueError, RecursionError):
        return None, NOT_JSON_MESSAGE


def _string(
    value: Any,
    path: str,
    limit: int,
    problems: _Problems,
    *,
    allow_empty: bool = False,
    check_markup: bool = True,
) -> str | None:
    if not isinstance(value, str):
        problems.add(path, "This must be text.")
        return None
    cleaned = _clean(value)
    if not cleaned and not allow_empty:
        problems.add(path, "This must not be empty.")
        return None
    if len(cleaned) > limit:
        problems.add(path, f"This is too long. Keep it under {limit} characters.")
        return None
    if check_markup and (_HTMLISH.search(cleaned) or _JS_URL.search(cleaned)):
        problems.add(path, "This contains markup or a link. Use plain text only.")
        return None
    return cleaned


def _count(value: Any, path: str, problems: _Problems, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        problems.add(path, "This must be a whole number, 0 or more.")
        return None
    return value


def _choice(value: Any, path: str, allowed: tuple[str, ...], problems: _Problems) -> str | None:
    if not isinstance(value, str) or value not in allowed:
        if path.endswith("assessment"):
            problems.add(
                path,
                "Use only one of these assessments: "
                + ", ".join(allowed)
                + ". Ratings such as Good, Average or Poor are not accepted.",
            )
        else:
            problems.add(path, "Use only one of: " + ", ".join(allowed) + ".")
        return None
    return value


def _list(value: Any, path: str, limit: int, problems: _Problems) -> list | None:
    if not isinstance(value, list):
        problems.add(path, "This must be a list.")
        return None
    if len(value) > limit:
        problems.add(path, f"Too many items. Keep at most {limit}.")
        return None
    return value


def _unknown_keys(obj: dict, allowed: tuple[str, ...], path: str, problems: _Problems) -> None:
    for key in obj:
        if key not in allowed:
            shown = _clean(str(key))[:64]
            where = f"{path}.{shown}" if path else shown
            problems.add(where, "This field is not part of the requested format.")


def _text_list(
    value: Any, path: str, limit: int, item_limit: int, problems: _Problems, *, minimum: int = 0
) -> list[str]:
    items = _list(value, path, limit, problems)
    out: list[str] = []
    if items is None:
        return out
    if len(items) < minimum:
        problems.add(path, "This list must not be empty.")
    for i, item in enumerate(items):
        text = _string(item, f"{path}[{i}]", item_limit, problems)
        if text is not None:
            out.append(text)
    return out


def _observations(value: Any, path: str, problems: _Problems, warnings: list[str]) -> list[dict]:
    items = _list(value, path, MAX_OBSERVATIONS, problems)
    out: list[dict] = []
    if items is None:
        return out
    for i, raw in enumerate(items):
        here = f"{path}[{i}]"
        if not isinstance(raw, dict):
            problems.add(here, "This must be an object.")
            continue
        _unknown_keys(raw, _OBSERVATION_KEYS, here, problems)
        text = _string(raw.get("text"), f"{here}.text", MAX_OBSERVATION_TEXT, problems)
        kind = _choice(raw.get("kind"), f"{here}.kind", OBSERVATION_KINDS, problems)
        evidence_raw = raw.get("evidence", [])
        evidence = _text_list(
            evidence_raw,
            f"{here}.evidence",
            MAX_EVIDENCE_PER_OBSERVATION,
            MAX_EVIDENCE_PATH,
            problems,
        )
        n = _count(raw.get("n"), f"{here}.n", problems, nullable=True)
        if kind == "observed" and not evidence:
            warnings.append(f"A statement marked observed names no data it comes from ({here}).")
        if text is not None and kind is not None:
            out.append({"text": text, "kind": kind, "evidence": evidence, "n": n})
    return out


def validate_response(
    text_or_obj: Any,
    *,
    package_hash: str,
    deterministic_labels: Mapping[str, str],
    pooled_label: str | None = None,
    trade_counts: Mapping[str, int] | None = None,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any], list[dict[str, str]]]:
    """Check a pasted analysis and cross-check it against the server's numbers.

    Args:
        text_or_obj: The pasted text (a json fence, or the whole text as JSON)
            or an already parsed object.
        package_hash: The hash of the dataset the server rebuilt just now.
        deterministic_labels: Strategy name to its deterministic label. The keys
            are the selection: no other strategy name is accepted.
        pooled_label: The deterministic label of the pooled result, if known.
        trade_counts: Strategy name to its closed trade count, if known.

    Returns:
        (ok, evaluation, checks, problems). `evaluation` is None when not ok.
        `checks` holds package_match, label_conflicts and warnings.
    """
    checks: dict[str, Any] = {"package_match": False, "label_conflicts": [], "warnings": []}
    problems = _Problems()

    if isinstance(text_or_obj, str):
        text_or_obj = text_or_obj.lstrip("\ufeff").strip()
        if len(text_or_obj) > MAX_RESPONSE_CHARS:
            problems.add("response", TOO_LONG_MESSAGE)
            return False, None, checks, problems.items
        obj, error = _extract_json(text_or_obj)
        if error:
            problems.add("response", error)
            return False, None, checks, problems.items
    else:
        obj = text_or_obj
        try:
            size = len(json.dumps(obj, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            problems.add("response", NOT_JSON_MESSAGE)
            return False, None, checks, problems.items
        if size > MAX_RESPONSE_CHARS:
            problems.add("response", TOO_LONG_MESSAGE)
            return False, None, checks, problems.items

    if not isinstance(obj, dict):
        problems.add("response", NOT_JSON_MESSAGE)
        return False, None, checks, problems.items

    warnings: list[str] = checks["warnings"]
    conflicts: list[dict[str, Any]] = checks["label_conflicts"]
    _unknown_keys(obj, TOP_LEVEL_KEYS, "", problems)
    for key in _REQUIRED_TOP:
        if key not in obj:
            problems.add(key, "This field is missing.")

    version = obj.get("schema_version")
    if "schema_version" in obj and (
        isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION
    ):
        problems.add("schema_version", f"This must be {SCHEMA_VERSION}.")
    echoed = None
    if "package_hash" in obj:
        echoed = _string(obj["package_hash"], "package_hash", 128, problems)
    summary = (
        _string(obj["summary"], "summary", MAX_SUMMARY, problems) if "summary" in obj else None
    )
    confidence = (
        _choice(obj["overall_confidence"], "overall_confidence", CONFIDENCES, problems)
        if "overall_confidence" in obj
        else None
    )

    strategies_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_strategies = (
        _list(obj["strategies"], "strategies", MAX_STRATEGIES, problems)
        if "strategies" in obj
        else None
    )
    for i, raw in enumerate(raw_strategies or []):
        here = f"strategies[{i}]"
        if not isinstance(raw, dict):
            problems.add(here, "This must be an object.")
            continue
        _unknown_keys(raw, _STRATEGY_KEYS, here, problems)
        for key in _STRATEGY_KEYS:
            if key not in raw:
                problems.add(f"{here}.{key}", "This field is missing.")
        # A name is matched against the selection exactly, so markup in it is
        # only ever refused as an unknown strategy. A real name may contain '<'.
        name = _string(raw.get("name"), f"{here}.name", MAX_NAME, problems, check_markup=False)
        if name is not None:
            if name not in deterministic_labels:
                problems.add(
                    f"{here}.name",
                    "This strategy is not part of the current selection.",
                )
            elif name in seen:
                problems.add(f"{here}.name", "This strategy appears more than once.")
            seen.add(name)
        assessment = _choice(raw.get("assessment"), f"{here}.assessment", LABELS, problems)
        n = _count(raw.get("n"), f"{here}.n", problems)
        strat_conf = _choice(raw.get("confidence"), f"{here}.confidence", CONFIDENCES, problems)
        observations = _observations(
            raw.get("observations"), f"{here}.observations", problems, warnings
        )
        weaknesses = _text_list(
            raw.get("weaknesses"), f"{here}.weaknesses", MAX_WEAKNESSES, MAX_TEXT_ITEM, problems
        )
        tests = _text_list(
            raw.get("suggested_tests"),
            f"{here}.suggested_tests",
            MAX_SUGGESTED_TESTS,
            MAX_TEXT_ITEM,
            problems,
        )
        expected = deterministic_labels.get(name) if name is not None else None
        conflict = bool(assessment and expected and assessment != expected)
        if conflict:
            note = (
                "The analysis reports a possible edge where the data is too small to say."
                if expected == LABEL_INSUFFICIENT and assessment == LABEL_POSSIBLE_EDGE
                else "The analysis assessment differs from the label computed from your trades."
            )
            conflicts.append(
                {
                    "strategy": name,
                    "assessment": assessment,
                    "deterministic_label": expected,
                    "message": note,
                }
            )
        counted = (trade_counts or {}).get(name) if name is not None else None
        if n is not None and counted is not None and n != counted:
            warnings.append(f"The analysis says {n} trades for {name}; the data has {counted}.")
        strategies_out.append(
            {
                "name": name,
                "assessment": assessment,
                "n": n,
                "confidence": strat_conf,
                "observations": observations,
                "weaknesses": weaknesses,
                "suggested_tests": tests,
                "deterministic_label": expected,
                "conflict": conflict,
            }
        )

    portfolio_out = None
    portfolio = obj.get("portfolio")
    if portfolio is not None:
        if not isinstance(portfolio, dict):
            problems.add("portfolio", "This must be an object or null.")
        else:
            _unknown_keys(portfolio, _PORTFOLIO_KEYS, "portfolio", problems)
            for key in _PORTFOLIO_KEYS:
                if key not in portfolio:
                    problems.add(f"portfolio.{key}", "This field is missing.")
            assessment = _choice(
                portfolio.get("assessment"), "portfolio.assessment", LABELS, problems
            )
            conflict = bool(assessment and pooled_label and assessment != pooled_label)
            if conflict:
                conflicts.append(
                    {
                        "strategy": None,
                        "assessment": assessment,
                        "deterministic_label": pooled_label,
                        "message": "The portfolio assessment differs from the label computed "
                        "from your trades.",
                    }
                )
            portfolio_out = {
                "assessment": assessment,
                "observations": _observations(
                    portfolio.get("observations"), "portfolio.observations", problems, warnings
                ),
                "overlap_notes": _text_list(
                    portfolio.get("overlap_notes"),
                    "portfolio.overlap_notes",
                    MAX_OVERLAP_NOTES,
                    MAX_TEXT_ITEM,
                    problems,
                ),
                "deterministic_label": pooled_label,
                "conflict": conflict,
            }

    limitations = _text_list(
        obj.get("limitations"),
        "limitations",
        MAX_LIMITATIONS,
        MAX_TEXT_ITEM,
        problems,
        minimum=1,
    )

    if problems:
        return False, None, checks, problems.items

    checks["package_match"] = echoed == package_hash
    if not checks["package_match"]:
        warnings.insert(0, HASH_MISMATCH_WARNING)
    evaluation = {
        "schema_version": SCHEMA_VERSION,
        "package_hash": echoed,
        "summary": summary,
        "overall_confidence": confidence,
        "strategies": strategies_out,
        "portfolio": portfolio_out,
        "limitations": limitations,
    }
    return True, evaluation, checks, []
