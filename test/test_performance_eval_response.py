"""Validation of an analysis pasted back from claude.ai.

Run with: PYTHONPATH=. uv run pytest test/test_performance_eval_response.py -v
"""

import copy
import json
import time

import pytest

import services.performance_eval.ai_response as air
from services.performance_eval.dataset import (
    LABEL_INSUFFICIENT,
    LABEL_NEGATIVE,
    LABEL_NO_EDGE,
    LABEL_POSSIBLE_EDGE,
)

HASH = "a" * 64
LABELS = {"ALPHA": LABEL_POSSIBLE_EDGE, "BETA": LABEL_INSUFFICIENT}
COUNTS = {"ALPHA": 100, "BETA": 12}


def _observation(**over):
    base = {
        "text": "Win rate is 55 percent over 100 trades.",
        "kind": "observed",
        "evidence": ["strategies[0].compare.win_rate"],
        "n": 100,
    }
    base.update(over)
    return base


def _valid():
    return {
        "schema_version": 1,
        "package_hash": HASH,
        "summary": "A short summary.",
        "overall_confidence": "low",
        "strategies": [
            {
                "name": "ALPHA",
                "assessment": LABEL_POSSIBLE_EDGE,
                "n": 100,
                "confidence": "medium",
                "observations": [_observation()],
                "weaknesses": ["Profit depends on a few days."],
                "suggested_tests": ["Backtest with the best day removed."],
            },
            {
                "name": "BETA",
                "assessment": LABEL_INSUFFICIENT,
                "n": 12,
                "confidence": "low",
                "observations": [],
                "weaknesses": [],
                "suggested_tests": [],
            },
        ],
        "portfolio": {
            "assessment": LABEL_POSSIBLE_EDGE,
            "observations": [_observation()],
            "overlap_notes": ["Few shared days."],
        },
        "limitations": ["Small sample."],
    }


def _check(value, **kw):
    kw.setdefault("package_hash", HASH)
    kw.setdefault("deterministic_labels", LABELS)
    kw.setdefault("pooled_label", LABEL_POSSIBLE_EDGE)
    kw.setdefault("trade_counts", COUNTS)
    return air.validate_response(value, **kw)


def _paths(problems):
    return [p["path"] for p in problems]


def test_a_valid_sample_passes_as_an_object_and_as_text():
    for value in (
        _valid(),
        json.dumps(_valid()),
        "Intro\n```json\n" + json.dumps(_valid()) + "\n```",
    ):
        ok, evaluation, checks, problems = _check(value)
        assert ok and problems == []
        assert checks["package_match"] is True
        assert checks["label_conflicts"] == [] and checks["warnings"] == []
        assert [s["name"] for s in evaluation["strategies"]] == ["ALPHA", "BETA"]
        assert evaluation["strategies"][0]["deterministic_label"] == LABEL_POSSIBLE_EDGE
        assert evaluation["strategies"][0]["conflict"] is False
        assert evaluation["portfolio"]["conflict"] is False


def test_portfolio_may_be_null():
    value = _valid()
    value["portfolio"] = None
    ok, evaluation, _c, _p = _check(value)
    assert ok and evaluation["portfolio"] is None
    del value["portfolio"]
    assert _check(value)[0]


def test_fence_extraction_uses_the_first_json_block():
    first = _valid()
    second = _valid()
    second["summary"] = "second"
    text = (
        "words\n```json\n"
        + json.dumps(first)
        + "\n```\nmore\n```json\n"
        + json.dumps(second)
        + "\n```"
    )
    ok, evaluation, _c, _p = _check(text)
    assert ok and evaluation["summary"] == "A short summary."


def test_trailing_text_after_a_fence_is_ignored_but_trailing_text_after_bare_json_is_not():
    text = "```json\n" + json.dumps(_valid()) + "\n```\nThanks for reading."
    assert _check(text)[0]
    ok, _e, _c, problems = _check(json.dumps(_valid()) + "\nThanks for reading.")
    assert not ok and problems[0]["path"] == "response"


@pytest.mark.parametrize(
    "text", ["", "hello", "{not json", "```json\n{oops\n```", "[1, 2]", "42", "null"]
)
def test_not_json_or_not_an_object_is_refused(text):
    ok, evaluation, checks, problems = _check(text)
    assert not ok and evaluation is None
    assert problems[0]["path"] == "response"
    assert checks["package_match"] is False


def test_unknown_top_level_key_is_refused():
    value = _valid()
    value["extra"] = 1
    ok, _e, _c, problems = _check(value)
    assert not ok and "extra" in _paths(problems)


@pytest.mark.parametrize("bad", ["Good", "Average", "Poor", "profitable", "", 3, None])
def test_assessment_outside_the_four_labels_is_refused(bad):
    value = _valid()
    value["strategies"][0]["assessment"] = bad
    ok, _e, _c, problems = _check(value)
    assert not ok
    found = [p for p in problems if p["path"] == "strategies[0].assessment"]
    assert found and "Good, Average or Poor are not accepted" in found[0]["message"]


def test_portfolio_assessment_is_closed_too():
    value = _valid()
    value["portfolio"]["assessment"] = "Good"
    ok, _e, _c, problems = _check(value)
    assert not ok and "portfolio.assessment" in _paths(problems)


@pytest.mark.parametrize(
    "path,bad",
    [
        (("summary",), 5),
        (("overall_confidence",), "certain"),
        (("schema_version",), 2),
        (("schema_version",), True),
        (("schema_version",), "1"),
        (("strategies", 0, "n"), "100"),
        (("strategies", 0, "n"), -1),
        (("strategies", 0, "n"), 1.5),
        (("strategies", 0, "n"), True),
        (("strategies", 0, "confidence"), "very high"),
        (("strategies", 0, "observations"), "text"),
        (("strategies", 0, "weaknesses"), [1]),
        (("strategies", 0, "observations", 0, "kind"), "fact"),
        (("strategies", 0, "observations", 0, "n"), "x"),
        (("strategies", 0, "observations", 0, "evidence"), "path"),
        (("limitations",), "text"),
        (("strategies",), {}),
    ],
)
def test_wrong_types_and_values_are_refused(path, bad):
    value = _valid()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    ok, _e, _c, problems = _check(value)
    assert not ok and problems


def test_missing_required_fields_are_named():
    value = _valid()
    del value["summary"]
    del value["strategies"][0]["weaknesses"]
    ok, _e, _c, problems = _check(value)
    assert not ok
    assert {"summary", "strategies[0].weaknesses"} <= set(_paths(problems))


@pytest.mark.parametrize(
    "mutate,path",
    [
        (lambda v: v.__setitem__("summary", "x" * (air.MAX_SUMMARY + 1)), "summary"),
        (
            lambda v: v["strategies"][0].__setitem__("name", "x" * (air.MAX_NAME + 1)),
            "strategies[0].name",
        ),
        (
            lambda v: v["strategies"][0]["observations"][0].__setitem__(
                "text", "x" * (air.MAX_OBSERVATION_TEXT + 1)
            ),
            "strategies[0].observations[0].text",
        ),
        (
            lambda v: v["strategies"][0]["observations"][0].__setitem__(
                "evidence", ["x" * (air.MAX_EVIDENCE_PATH + 1)]
            ),
            "strategies[0].observations[0].evidence[0]",
        ),
        (
            lambda v: v["strategies"][0].__setitem__("weaknesses", ["x" * (air.MAX_TEXT_ITEM + 1)]),
            "strategies[0].weaknesses[0]",
        ),
        (lambda v: v.__setitem__("limitations", ["x" * (air.MAX_TEXT_ITEM + 1)]), "limitations[0]"),
    ],
)
def test_over_length_is_refused(mutate, path):
    value = _valid()
    mutate(value)
    ok, _e, _c, problems = _check(value)
    assert not ok and path in _paths(problems)


@pytest.mark.parametrize(
    "mutate,path",
    [
        (
            lambda v: v["strategies"][0].__setitem__(
                "observations", [_observation()] * (air.MAX_OBSERVATIONS + 1)
            ),
            "strategies[0].observations",
        ),
        (
            lambda v: v["strategies"][0].__setitem__(
                "weaknesses", ["w"] * (air.MAX_WEAKNESSES + 1)
            ),
            "strategies[0].weaknesses",
        ),
        (
            lambda v: v["strategies"][0].__setitem__(
                "suggested_tests", ["t"] * (air.MAX_SUGGESTED_TESTS + 1)
            ),
            "strategies[0].suggested_tests",
        ),
        (
            lambda v: v["portfolio"].__setitem__(
                "overlap_notes", ["o"] * (air.MAX_OVERLAP_NOTES + 1)
            ),
            "portfolio.overlap_notes",
        ),
        (lambda v: v.__setitem__("limitations", ["l"] * (air.MAX_LIMITATIONS + 1)), "limitations"),
        (
            lambda v: v.__setitem__(
                "strategies",
                [copy.deepcopy(v["strategies"][0]) for _ in range(air.MAX_STRATEGIES + 1)],
            ),
            "strategies",
        ),
    ],
)
def test_too_many_items_are_refused(mutate, path):
    value = _valid()
    mutate(value)
    ok, _e, _c, problems = _check(value)
    assert not ok and path in _paths(problems)


def test_limits_at_the_boundary_are_accepted():
    value = _valid()
    value["summary"] = "s" * air.MAX_SUMMARY
    value["limitations"] = ["l" * air.MAX_TEXT_ITEM] * air.MAX_LIMITATIONS
    value["strategies"][0]["weaknesses"] = ["w"] * air.MAX_WEAKNESSES
    assert _check(value)[0]


def test_empty_limitations_are_refused():
    value = _valid()
    value["limitations"] = []
    ok, _e, _c, problems = _check(value)
    assert not ok and "limitations" in _paths(problems)


def test_unknown_and_duplicate_strategy_names_are_refused():
    value = _valid()
    value["strategies"][0]["name"] = "alpha"
    ok, _e, _c, problems = _check(value)
    assert not ok and "strategies[0].name" in _paths(problems)

    value = _valid()
    value["strategies"][1]["name"] = "ALPHA"
    ok, _e, _c, problems = _check(value)
    assert not ok and "strategies[1].name" in _paths(problems)

    value = _valid()
    value["strategies"][0]["name"] = "NOT-SELECTED"
    ok, _e, _c, problems = _check(value)
    assert not ok


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        "a <b>bold</b> claim",
        "x <!-- c -->",
        "go to javascript:alert(1)",
        "JavaScript :x",
        "< img src=x>",
    ],
)
def test_markup_and_script_links_are_refused(text):
    for mutate in (
        lambda v, t=text: v.__setitem__("summary", t),
        lambda v, t=text: v["strategies"][0]["weaknesses"].append(t),
        lambda v, t=text: v["strategies"][0]["observations"][0].__setitem__("text", t),
        lambda v, t=text: v["portfolio"]["overlap_notes"].append(t),
        lambda v, t=text: v["limitations"].append(t),
    ):
        value = _valid()
        mutate(value)
        ok, _e, _c, problems = _check(value)
        assert not ok and problems


def test_a_less_than_sign_before_a_number_is_plain_text():
    value = _valid()
    value["summary"] = "Win rate below 50 percent when n < 30 and x<5."
    assert _check(value)[0]


def test_oversized_input_is_refused():
    big = "x" * (air.MAX_RESPONSE_CHARS + 1)
    ok, _e, _c, problems = _check(big)
    assert not ok and problems[0]["path"] == "response"
    assert "too long" in problems[0]["message"]
    value = _valid()
    value["summary"] = "s" * air.MAX_RESPONSE_CHARS
    ok, _e, _c, problems = _check(value)
    assert not ok and problems[0]["path"] == "response"


def test_problems_are_capped_at_twenty():
    value = _valid()
    value["strategies"] = [
        {"name": f"S{i}", "assessment": "Good", "n": -1, "confidence": "x"} for i in range(15)
    ]
    ok, _e, _c, problems = _check(value)
    assert not ok and len(problems) == air.MAX_PROBLEMS == 20


def test_hash_mismatch_is_a_warning_not_an_error():
    ok, _e, checks, _p = _check(_valid(), package_hash="b" * 64)
    assert ok
    assert checks["package_match"] is False
    assert (
        checks["warnings"][0]
        == "This analysis was written for different data than the current selection."
    )


def test_label_conflict_is_reported_and_marked():
    value = _valid()
    value["strategies"][0]["assessment"] = LABEL_NEGATIVE
    ok, evaluation, checks, _p = _check(value)
    assert ok
    assert evaluation["strategies"][0]["conflict"] is True
    assert evaluation["strategies"][0]["deterministic_label"] == LABEL_POSSIBLE_EDGE
    assert checks["label_conflicts"][0]["strategy"] == "ALPHA"


def test_a_possible_edge_on_insufficient_data_is_a_conflict():
    value = _valid()
    value["strategies"][1]["assessment"] = LABEL_POSSIBLE_EDGE
    ok, evaluation, checks, _p = _check(value)
    assert ok and evaluation["strategies"][1]["conflict"] is True
    assert "too small" in checks["label_conflicts"][0]["message"]


def test_portfolio_label_conflict():
    value = _valid()
    value["portfolio"]["assessment"] = LABEL_NO_EDGE
    ok, evaluation, checks, _p = _check(value)
    assert ok and evaluation["portfolio"]["conflict"] is True
    assert checks["label_conflicts"][0]["strategy"] is None


def test_a_different_trade_count_is_a_warning():
    value = _valid()
    value["strategies"][0]["n"] = 99
    ok, _e, checks, _p = _check(value)
    assert ok and any("99 trades for ALPHA" in w for w in checks["warnings"])


def test_observed_without_evidence_is_a_warning():
    value = _valid()
    value["strategies"][0]["observations"][0]["evidence"] = []
    ok, _e, checks, _p = _check(value)
    assert ok and any("names no data" in w for w in checks["warnings"])
    value["strategies"][0]["observations"][0]["kind"] = "inference"
    ok, _e, checks, _p = _check(value)
    assert ok and checks["warnings"] == []


def test_evidence_is_optional():
    value = _valid()
    del value["strategies"][0]["observations"][0]["evidence"]
    del value["strategies"][0]["observations"][0]["n"]
    ok, evaluation, _c, _p = _check(value)
    assert ok
    assert evaluation["strategies"][0]["observations"][0]["evidence"] == []
    assert evaluation["strategies"][0]["observations"][0]["n"] is None


def test_sanitised_output_has_control_characters_removed_and_is_stripped():
    value = _valid()
    value["summary"] = "  Hello\x00 wor\x07ld\u202e  "
    value["strategies"][0]["weaknesses"] = ["a\x1b[31mred"]
    ok, evaluation, _c, _p = _check(value)
    assert ok
    assert evaluation["summary"] == "Hello world"
    assert evaluation["strategies"][0]["weaknesses"] == ["a[31mred"]


def test_output_contains_only_known_fields():
    ok, evaluation, _c, _p = _check(_valid())
    assert ok
    assert set(evaluation) == set(air.TOP_LEVEL_KEYS)
    assert set(evaluation["strategies"][0]) == set(air._STRATEGY_KEYS) | {
        "deterministic_label",
        "conflict",
    }
    json.dumps(evaluation, allow_nan=False)


def test_validation_is_deterministic_and_does_not_change_its_input():
    value = _valid()
    before = copy.deepcopy(value)
    first = _check(value)
    second = _check(value)
    assert first == second
    assert value == before


def test_the_message_for_each_problem_is_a_trader_sentence():
    value = _valid()
    value["strategies"][0]["assessment"] = "Good"
    value["summary"] = 5
    _ok, _e, _c, problems = _check(value)
    for problem in problems:
        message = problem["message"]
        assert message.endswith(".")
        for word in ("Traceback", "Exception", "KeyError", "TypeError", "HTTP", "status code"):
            assert word not in message


# -- fix round 1 -------------------------------------------------------------------------


def test_many_fence_openers_are_refused_quickly():
    text = "```json\n" * 7500
    start = time.perf_counter()
    ok, _e, _c, problems = _check(text)
    assert time.perf_counter() - start < 1
    assert not ok and problems[0]["message"] == air.TOO_MANY_FENCES_MESSAGE


def test_unclosed_openers_below_the_cap_finish_quickly_and_are_not_json():
    text = "```json\n" * air.MAX_FENCE_OPENERS + "x" * 50_000
    start = time.perf_counter()
    ok, _e, _c, problems = _check(text)
    assert time.perf_counter() - start < 1
    assert not ok and problems[0]["message"] == air.NOT_JSON_MESSAGE


def test_a_normal_fenced_block_still_extracts_with_text_either_side():
    text = "Intro\n```json\n" + json.dumps(_valid(), indent=2) + "\n```\nOutro ```json"
    assert _check(text)[0]
    same_line = "```JSON\n" + json.dumps(_valid()) + "```"
    assert _check(same_line)[0]


def test_deeply_nested_object_is_a_trader_sentence_not_a_crash():
    nested: dict = {}
    node = nested
    for _ in range(200_000):
        node["a"] = {}
        node = node["a"]
    ok, _e, _c, problems = _check(nested)
    assert not ok
    assert problems == [{"path": "response", "message": air.NOT_JSON_MESSAGE}]


def test_deeply_nested_text_is_a_trader_sentence_not_a_crash():
    text = "```json\n" + "[" * 50_000 + "\n```"
    ok, _e, _c, problems = _check(text)
    assert not ok
    assert problems == [{"path": "response", "message": air.NOT_JSON_MESSAGE}]


def test_unknown_keys_are_truncated_and_stripped_of_control_characters():
    value = _valid()
    value["k" * 500] = 1
    value["bad\x00\x1b[31mkey\u202e"] = 1
    value["strategies"][0]["z" * 300] = 1
    ok, _e, _c, problems = _check(value)
    assert not ok
    for item in problems:
        assert len(item["path"]) <= len("strategies[0].") + 64
        assert all(c.isprintable() for c in item["path"])
    paths = _paths(problems)
    assert "k" * 64 in paths and "k" * 65 not in "".join(paths)
    assert any(p.startswith("bad") and "\x1b" not in p for p in paths)
