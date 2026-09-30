"""Tests for the research idea registry builder (signal_engine/research/build_registry.py).

The registry is the single place that answers "how many ideas do we have, what did we
conclude, and when should we look again". These tests pin the contract that keeps it
trustworthy: bad frontmatter and dangling links fail loudly instead of rendering a
plausible-looking but wrong page.
"""

import json
from pathlib import Path

import pytest

from signal_engine.research import build_registry as br

VALID = """---
id: IDEA-001
title: Sample idea
status: rejected
verdict: no-edge
category: intraday-momentum
horizon: intraday
source:
  type: video
  author: Someone
  ref: "https://example.invalid/v"
  captured: 2026-09-22
summary: One line of what the idea is.
result: Lost money before costs.
strategy_path: strategy_library/intraday/sample
revisit_when: Never.
last_reviewed: 2026-09-29
tags: [vwap, ema]
---

Body text, not parsed.
"""


def _write_idea(root: Path, folder: str, text: str) -> Path:
    d = root / "research" / "ideas" / folder
    d.mkdir(parents=True)
    (d / "idea.md").write_text(text, encoding="utf-8")
    return d


@pytest.fixture
def base(tmp_path: Path) -> Path:
    (tmp_path / "strategy_library" / "intraday" / "sample").mkdir(parents=True)
    return tmp_path


def test_load_valid_idea(base: Path):
    _write_idea(base, "idea-001-sample", VALID)
    ideas = br.load_ideas(base)
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea["id"] == "IDEA-001"
    assert idea["source"]["captured"] == "2026-09-22"  # dates normalised to str
    assert idea["last_reviewed"] == "2026-09-29"
    assert idea["tags"] == ["vwap", "ema"]


def test_underscore_folders_are_skipped(base: Path):
    _write_idea(base, "_inbox", "not an idea")
    _write_idea(base, "_template", VALID.replace("IDEA-001", "IDEA-000"))
    assert br.load_ideas(base) == []


def test_missing_required_field_is_reported(base: Path):
    _write_idea(
        base, "idea-001-sample", VALID.replace("summary: One line of what the idea is.\n", "")
    )
    with pytest.raises(br.RegistryError, match="summary"):
        br.load_ideas(base)


def test_invalid_status_is_reported(base: Path):
    _write_idea(base, "idea-001-sample", VALID.replace("status: rejected", "status: maybe"))
    with pytest.raises(br.RegistryError, match="status"):
        br.load_ideas(base)


def test_no_frontmatter_is_reported(base: Path):
    _write_idea(base, "idea-001-sample", "just text")
    with pytest.raises(br.RegistryError, match="frontmatter"):
        br.load_ideas(base)


def test_dangling_path_is_reported(base: Path):
    _write_idea(
        base,
        "idea-001-sample",
        VALID.replace("strategy_library/intraday/sample", "strategy_library/nope"),
    )
    with pytest.raises(br.RegistryError, match="strategy_library/nope"):
        br.load_ideas(base)


def test_duplicate_id_is_reported(base: Path):
    _write_idea(base, "idea-001-a", VALID)
    _write_idea(base, "idea-002-b", VALID)
    with pytest.raises(br.RegistryError, match="duplicate"):
        br.load_ideas(base)


def test_id_must_match_folder_prefix(base: Path):
    _write_idea(base, "idea-009-sample", VALID)
    with pytest.raises(br.RegistryError, match="folder"):
        br.load_ideas(base)


def test_summarize_counts(base: Path):
    _write_idea(base, "idea-001-a", VALID)
    _write_idea(
        base,
        "idea-002-b",
        VALID.replace("IDEA-001", "IDEA-002")
        .replace("status: rejected", "status: inbox")
        .replace("verdict: no-edge", "verdict: none"),
    )
    s = br.summarize(br.load_ideas(base))
    assert s["total"] == 2
    assert s["by_status"] == {"inbox": 1, "rejected": 1}
    assert s["by_category"] == {"intraday-momentum": 2}


def test_render_markdown_lists_every_idea(base: Path):
    _write_idea(base, "idea-001-a", VALID)
    md = br.render_markdown(br.load_ideas(base))
    assert "IDEA-001" in md and "Sample idea" in md and "no-edge" in md


def test_render_html_escapes_content(base: Path):
    _write_idea(base, "idea-001-a", VALID.replace("Sample idea", "<script>alert(1)</script>"))
    page = br.render_html(br.load_ideas(base))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_render_json_roundtrip(base: Path):
    _write_idea(base, "idea-001-a", VALID)
    data = json.loads(br.render_json(br.load_ideas(base)))
    assert data["summary"]["total"] == 1
    assert data["ideas"][0]["id"] == "IDEA-001"


def test_write_then_check_is_clean_and_detects_drift(base: Path):
    _write_idea(base, "idea-001-a", VALID)
    assert br.main(["--root", str(base)]) == 0
    assert br.main(["--root", str(base), "--check"]) == 0
    (base / "research" / "REGISTRY.md").write_text("stale", encoding="utf-8")
    assert br.main(["--root", str(base), "--check"]) == 1


def test_main_returns_nonzero_on_invalid(base: Path):
    _write_idea(base, "idea-001-a", "no frontmatter")
    assert br.main(["--root", str(base)]) == 1


RUN = {
    "run_id": "2026-09-29-orb-v1",
    "date": "2026-09-29",
    "code_version": "0bdf3d32a",
    "data": {
        "source": "openalgo-historify",
        "as_of": "2026-09-28",
        "universe": "fno-208",
        "bar_size": "5m",
    },
    "period": {"start": "2021-01-01", "end": "2026-09-25"},
    "costs": {"model": "zerodha-mis", "round_trip_bps": 9.4},
    "split": {"type": "oos", "oos_start": "2025-01-01"},
    "metrics": {"trades": 1200, "gross_bps": 8.1, "net_bps": -1.3, "t_stat": 1.9},
}


def _write_run(folder: Path, run: dict, name: str = "run.json") -> None:
    runs = folder / "runs"
    runs.mkdir(exist_ok=True)
    (runs / name).write_text(json.dumps(run), encoding="utf-8")


def test_runs_are_attached_and_latest_selected(base: Path):
    d = _write_idea(base, "idea-001-a", VALID)
    _write_run(d, RUN, "a.json")
    _write_run(d, {**RUN, "run_id": "2026-10-01-orb-v2", "date": "2026-10-01"}, "b.json")
    idea = br.load_ideas(base)[0]
    assert [r["run_id"] for r in idea["runs"]] == ["2026-09-29-orb-v1", "2026-10-01-orb-v2"]
    assert br.latest_run(idea)["run_id"] == "2026-10-01-orb-v2"
    s = br.summarize(br.load_ideas(base))
    assert s["total_runs"] == 2 and s["ideas_with_runs"] == 1


def test_idea_without_runs_has_no_latest(base: Path):
    _write_idea(base, "idea-001-a", VALID)
    assert br.latest_run(br.load_ideas(base)[0]) is None


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda r: r.pop("code_version"), "code_version"),
        (lambda r: r["metrics"].pop("net_bps"), "net_bps"),
        (lambda r: r["metrics"].update(trades="many"), "trades"),
        (lambda r: r["costs"].pop("round_trip_bps"), "round_trip_bps"),
        (lambda r: r["data"].pop("as_of"), "as_of"),
        (lambda r: r["period"].pop("end"), "end"),
    ],
)
def test_invalid_run_is_reported(base: Path, mutate, match):
    d = _write_idea(base, "idea-001-a", VALID)
    run = json.loads(json.dumps(RUN))
    mutate(run)
    _write_run(d, run)
    with pytest.raises(br.RegistryError, match=match):
        br.load_ideas(base)


def test_unparseable_run_is_reported(base: Path):
    d = _write_idea(base, "idea-001-a", VALID)
    (d / "runs").mkdir()
    (d / "runs" / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(br.RegistryError, match="bad.json"):
        br.load_ideas(base)


def test_duplicate_run_id_is_reported(base: Path):
    d = _write_idea(base, "idea-001-a", VALID)
    _write_run(d, RUN, "a.json")
    _write_run(d, RUN, "b.json")
    with pytest.raises(br.RegistryError, match="duplicate run_id"):
        br.load_ideas(base)


def test_renderers_show_latest_run(base: Path):
    d = _write_idea(base, "idea-001-a", VALID)
    _write_run(d, RUN)
    ideas = br.load_ideas(base)
    assert "-1.3" in br.render_markdown(ideas)
    assert "-1.3" in br.render_html(ideas)
    assert json.loads(br.render_json(ideas))["ideas"][0]["runs"][0]["metrics"]["trades"] == 1200


# ---------------------------------------------------------------------------
# Run record schema v2: history, lineage and tearsheet data. Every v2 key is optional so
# v1 records stay valid; a malformed v2 key must fail loudly, not render a wrong page.
# ---------------------------------------------------------------------------

V2 = {
    "params": {"or_minutes": 15, "sl_atr": 1.5},
    "parent_run_id": None,
    "change_note": "first baseline",
    "n_trials": 3,
    "promoted": False,
    "cost_sensitivity": [
        {"round_trip_bps": 0, "net_bps": 8.1},
        {"round_trip_bps": 10, "net_bps": -1.9},
    ],
    "in_sample": {"trades": 900, "net_bps": 1.2},
    "out_of_sample": {"trades": 300, "net_bps": -4.0},
    "series": {
        "equity_r": [["2026-01-02", 1.0], ["2026-01-05", 0.5]],
        "drawdown_r": [["2026-01-02", 0.0], ["2026-01-05", -0.5]],
        "monthly": [{"month": "2026-01", "n": 2, "total_r": 0.5, "net_r": 0.25}],
    },
}


def _setup_run(base: Path, run: dict, name: str = "a.json") -> Path:
    d = _write_idea(base, "idea-001-sample", VALID)
    _write_run(d, run, name)
    return d


def test_v1_run_without_v2_keys_is_still_valid(base: Path):
    _setup_run(base, RUN)
    assert len(br.load_ideas(base)[0]["runs"]) == 1


def test_v2_run_is_accepted(base: Path):
    _setup_run(base, {**RUN, **V2})
    assert br.load_ideas(base)[0]["runs"][0]["n_trials"] == 3


@pytest.mark.parametrize(
    "patch",
    [
        {"n_trials": 0},
        {"n_trials": True},
        {"n_trials": 2.5},
        {"promoted": "yes"},
        {"params": ["not", "a", "dict"]},
        {"change_note": 5},
        {"cost_sensitivity": {"round_trip_bps": 0}},
        {"cost_sensitivity": [{"round_trip_bps": "0", "net_bps": 1}]},
        {"series": {"equity_r": [["2026-01-02"]]}},
        {"series": {"equity_r": "nope"}},
        {"series": {"monthly": [{"month": "2026-01"}]}},
        {"in_sample": [1]},
    ],
)
def test_malformed_v2_keys_are_rejected(base: Path, patch: dict):
    _setup_run(base, {**RUN, **V2, **patch})
    with pytest.raises(br.RegistryError):
        br.load_ideas(base)


def test_extra_metric_keys_must_be_numeric(base: Path):
    run = {**RUN, "metrics": {**RUN["metrics"], "profit_factor": "high"}}
    _setup_run(base, run)
    with pytest.raises(br.RegistryError, match="profit_factor"):
        br.load_ideas(base)


def test_parent_run_must_exist_in_same_idea(base: Path):
    d = _setup_run(base, {**RUN, **V2, "parent_run_id": "no-such-run"})
    with pytest.raises(br.RegistryError, match="parent_run_id"):
        br.load_ideas(base)
    child = {
        **RUN,
        **V2,
        "run_id": "2026-10-01-orb-v2",
        "date": "2026-10-01",
        "parent_run_id": RUN["run_id"],
    }
    (d / "runs" / "a.json").write_text(json.dumps({**RUN, **V2}), encoding="utf-8")
    _write_run(d, child, "b.json")
    runs = br.load_ideas(base)[0]["runs"]
    assert runs[1]["parent_run_id"] == RUN["run_id"]


def test_run_cannot_be_its_own_parent(base: Path):
    _setup_run(base, {**RUN, **V2, "parent_run_id": RUN["run_id"]})
    with pytest.raises(br.RegistryError, match="parent_run_id"):
        br.load_ideas(base)


def test_registry_json_omits_heavy_series(base: Path):
    _setup_run(base, {**RUN, **V2})
    ideas = br.load_ideas(base)
    assert "series" in ideas[0]["runs"][0]  # the API reads full runs from load_ideas
    published = json.loads(br.render_json(ideas))["ideas"][0]["runs"][0]
    assert "series" not in published
    assert published["metrics"]["trades"] == 1200


CAPS = [
    {"max_positions": 5, "trades": 400, "taken_pct": 33.3, "gross_bps": 4.0, "net_bps": -12.0},
    {"max_positions": 10, "trades": 800, "taken_pct": 66.7, "gross_bps": 5.0, "net_bps": -11.0},
]


def test_position_caps_are_accepted(base: Path):
    _setup_run(base, {**RUN, "position_caps": CAPS})
    assert len(br.load_ideas(base)[0]["runs"][0]["position_caps"]) == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"position_caps": {"max_positions": 5}},
        {"position_caps": [{"max_positions": 0, "trades": 1, "gross_bps": 1, "net_bps": 1}]},
        {"position_caps": [{"max_positions": 2.5, "trades": 1, "gross_bps": 1, "net_bps": 1}]},
        {"position_caps": [{"max_positions": True, "trades": 1, "gross_bps": 1, "net_bps": 1}]},
        {"position_caps": [{"max_positions": 5, "trades": 1, "gross_bps": "x", "net_bps": 1}]},
        {"position_caps": [{"max_positions": 5, "gross_bps": 1, "net_bps": 1}]},
    ],
)
def test_malformed_position_caps_are_rejected(base: Path, patch: dict):
    _setup_run(base, {**RUN, **patch})
    with pytest.raises(br.RegistryError, match="position_caps"):
        br.load_ideas(base)


def test_concurrency_metrics_must_be_numeric(base: Path):
    run = {**RUN, "metrics": {**RUN["metrics"], "peak_concurrent": "many"}}
    _setup_run(base, run)
    with pytest.raises(br.RegistryError, match="peak_concurrent"):
        br.load_ideas(base)


BASELINE = {
    "method": "same-symbol random entry",
    "gross_bps": 30.0,
    "net_bps": 6.0,
    "excess_net_bps": 20.5,
}


def test_baseline_is_accepted(base: Path):
    _setup_run(base, {**RUN, "baseline": {**BASELINE, "excess_t_stat": 1.4}})
    assert br.load_ideas(base)[0]["runs"][0]["baseline"]["excess_net_bps"] == 20.5


@pytest.mark.parametrize(
    "patch",
    [
        {"baseline": [1]},
        {"baseline": {"gross_bps": 1.0, "net_bps": 1.0}},
        {"baseline": {**BASELINE, "excess_net_bps": "x"}},
        {"baseline": {**BASELINE, "excess_t_stat": "x"}},
        {"baseline": {**BASELINE, "method": 3}},
    ],
)
def test_malformed_baseline_is_rejected(base: Path, patch: dict):
    _setup_run(base, {**RUN, **patch})
    with pytest.raises(br.RegistryError, match="baseline"):
        br.load_ideas(base)
