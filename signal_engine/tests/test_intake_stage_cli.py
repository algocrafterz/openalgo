"""intake stage / state / classify: the lifecycle commands and the stage log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from signal_engine.research import build_registry as br
from signal_engine.research.intake import cli, frontmatter
from signal_engine.tests.intake_fixtures import make_spec, write_spec
from signal_engine.tests.test_registry_stage import IDEA, _setup

VERSIONS = (
    "- version: v1\n  strategy_fingerprint: " + "a" * 64 + "\n  parent: null\n"
    "  created_ist: x\n  note: ''\n"
    "- version: v2\n  strategy_fingerprint: " + "b" * 64 + "\n  parent: v1\n"
    "  created_ist: x\n  note: ''\n"
)
READY = {"promotion_decision": "READY_FOR_NEXT_STAGE"}


def _idea(tmp_path, lifecycle="stage: inbox\n", extra="", spec=True, versions=False) -> Path:
    folder = tmp_path / "research" / "ideas" / "idea-001-sample"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "idea.md").write_text(IDEA.format(lifecycle=lifecycle, extra=extra), encoding="utf-8")
    if spec:
        write_spec(folder, make_spec())
    if versions:
        (folder / "versions.yaml").write_text(VERSIONS, encoding="utf-8")
    return folder


def _run(tmp_path, *argv):
    return cli.main(list(argv), root=tmp_path)


def _log(folder: Path) -> list[dict]:
    path = folder / "stage-log.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []


def _fm(folder: Path) -> dict:
    return frontmatter.read(folder)


class TestStageForward:
    def test_inbox_to_backtest_needs_a_spec(self, tmp_path, capsys):
        folder = _idea(tmp_path, spec=False)
        assert _run(tmp_path, "stage", "IDEA-001", "backtest") == 1
        assert "spec.yaml" in capsys.readouterr().out
        assert _fm(folder)["stage"] == "inbox" and _log(folder) == []

    def test_inbox_to_backtest_with_a_spec(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "backtest") == 0
        assert _fm(folder)["stage"] == "backtest"
        (entry,) = _log(folder)
        assert (entry["from"], entry["to"], entry["run_id"]) == ("inbox", "backtest", None)
        assert entry["date_ist"].endswith("+05:30")
        assert set(entry) == {"date_ist", "from", "to", "reason", "run_id"}

    def test_moving_forward_skipping_a_stage_is_refused(self, tmp_path, capsys):
        folder = _idea(tmp_path, versions=True)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--version", "v1") == 1
        assert "backtest" in capsys.readouterr().out
        assert _fm(folder)["stage"] == "inbox"

    @pytest.mark.parametrize(
        "state, extra", [("rejected", ""), ("shelved", "revisit_when: later\n")]
    )
    def test_forward_is_refused_while_rejected_or_shelved(self, tmp_path, capsys, state, extra):
        folder = _idea(tmp_path, f"stage: inbox\nresearch_state: {state}\n", extra)
        assert _run(tmp_path, "stage", "IDEA-001", "backtest") == 1
        assert state in capsys.readouterr().out
        assert _fm(folder)["stage"] == "inbox" and _log(folder) == []

    def test_staying_on_the_same_stage_is_refused(self, tmp_path):
        _idea(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "inbox") == 1

    def test_unknown_stage_exits_two(self, tmp_path):
        with pytest.raises(SystemExit) as err:
            _idea(tmp_path)
            _run(tmp_path, "stage", "IDEA-001", "bogus")
        assert err.value.code == 2

    def test_unknown_idea_exits_two(self, tmp_path):
        _idea(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-999", "backtest") == 2


class TestHandEditedFlatRecord:
    @pytest.mark.parametrize(
        "field,fake",
        [
            ("promotion_decision", "READY_FOR_NEXT_STAGE"),
            ("run_verdict", "EDGE"),
            ("next_action", {"action": "Advance to the next stage", "reason": "x", "gate": None}),
        ],
    )
    def test_paper_refused_when_flat_record_contradicts_report(self, tmp_path, capsys, field, fake):
        folder = _idea(tmp_path, "stage: backtest\n", versions=True)
        # the hashed report says NOT_READY / MARGINAL; one flat field is edited to look promotable
        _setup(folder, promotion_decision="NOT_READY", run_verdict="MARGINAL")
        path = folder / "runs" / "2026-09-29-orb-v1.json"
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec[field] = fake
        path.write_text(json.dumps(rec), encoding="utf-8")
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--version", "v1") == 1
        out = capsys.readouterr().out
        assert "report" in out
        assert _fm(folder)["stage"] == "backtest"


class TestBacktestToPaper:
    def _ready(self, tmp_path, **patch):
        folder = _idea(tmp_path, "stage: backtest\n", versions=True)
        _setup(folder, **{**READY, **patch})
        return folder

    def test_requires_a_version(self, tmp_path, capsys):
        folder = self._ready(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "paper") == 2
        assert "--version" in capsys.readouterr().out
        assert _fm(folder)["stage"] == "backtest"

    def test_version_must_be_registered(self, tmp_path, capsys):
        self._ready(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--version", "v9") == 2
        assert "v9" in capsys.readouterr().out

    def test_uses_the_latest_qualifying_run_and_records_it(self, tmp_path):
        folder = self._ready(tmp_path)
        _setup(folder, "2026-09-30-orb-v1", date="2026-09-30", **READY)
        _setup(folder, "2026-10-01-orb-v1", date="2026-10-01", promotion_decision="NOT_READY")
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--version", "v1") == 0
        data = _fm(folder)
        assert data["stage"] == "paper"
        assert data["promoted_version"] == "v1"
        assert data["promoted_run_id"] == "2026-09-30-orb-v1"
        assert _log(folder)[-1]["run_id"] == "2026-09-30-orb-v1"
        assert len(br.load_ideas(tmp_path)) == 1  # the registry accepts what the CLI wrote

    def test_explicit_run_is_used(self, tmp_path):
        folder = self._ready(tmp_path)
        _setup(folder, "2026-09-30-orb-v1", date="2026-09-30", **READY)
        assert (
            _run(
                tmp_path,
                "stage",
                "IDEA-001",
                "paper",
                "--version",
                "v1",
                "--run",
                "2026-09-29-orb-v1",
            )
            == 0
        )
        assert _fm(folder)["promoted_run_id"] == "2026-09-29-orb-v1"

    @pytest.mark.parametrize(
        "patch",
        [
            {"promotion_decision": "NOT_READY"},
            {"promotion_decision": None},
            {"reproducible": False, "promotion_eligible": False},
            {"code_dirty": True, "promotion_eligible": False},
            {"version": "v2"},
        ],
    )
    def test_run_conditions_refuse(self, tmp_path, capsys, patch):
        folder = self._ready(tmp_path, **patch)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--version", "v1") == 1
        assert _fm(folder)["stage"] == "backtest" and _log(folder) == []
        assert "run" in capsys.readouterr().out

    def test_explicit_run_that_does_not_qualify_refuses_even_if_another_does(self, tmp_path):
        folder = self._ready(tmp_path, promotion_decision="NOT_READY")
        _setup(folder, "2026-09-30-orb-v1", date="2026-09-30", **READY)
        assert (
            _run(
                tmp_path,
                "stage",
                "IDEA-001",
                "paper",
                "--version",
                "v1",
                "--run",
                "2026-09-29-orb-v1",
            )
            == 1
        )

    def test_unknown_run_exits_two(self, tmp_path):
        self._ready(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--version", "v1", "--run", "zzz") == 2

    def test_no_runs_at_all_refuses(self, tmp_path):
        folder = _idea(tmp_path, "stage: backtest\n", versions=True)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--version", "v1") == 1
        assert _fm(folder)["stage"] == "backtest"

    def test_rejected_idea_cannot_move_forward(self, tmp_path):
        folder = _idea(tmp_path, "stage: backtest\nresearch_state: rejected\n", versions=True)
        _setup(folder, **READY)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--version", "v1") == 1


class TestPaperLiveRetired:
    def _paper(self, tmp_path):
        folder = _idea(
            tmp_path,
            "stage: paper\n",
            "promoted_version: v1\npromoted_run_id: 2026-09-29-orb-v1\n",
            versions=True,
        )
        _setup(folder, **READY)
        return folder

    def test_paper_to_live_is_refused(self, tmp_path, capsys):
        folder = self._paper(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "live") == 1
        assert "G7 and G8 gates are not implemented" in capsys.readouterr().out
        assert _fm(folder)["stage"] == "paper" and _log(folder) == []

    def test_retired_to_live_is_refused_too(self, tmp_path):
        folder = _idea(tmp_path, "stage: retired\n")
        assert _run(tmp_path, "stage", "IDEA-001", "live", "--reason", "back") == 1
        assert _fm(folder)["stage"] == "retired"

    def _live(self, tmp_path):
        folder = self._paper(tmp_path)
        text = (folder / "idea.md").read_text().replace("stage: paper", "stage: live")
        (folder / "idea.md").write_text(text)
        return folder

    def test_live_to_retired_needs_a_reason(self, tmp_path, capsys):
        folder = self._live(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "retired") == 2
        assert "--reason" in capsys.readouterr().out
        assert _fm(folder)["stage"] == "live"

    def test_live_to_retired_with_a_reason(self, tmp_path):
        folder = self._live(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "retired", "--reason", "G9 failed") == 0
        assert _fm(folder)["stage"] == "retired"
        assert _log(folder)[-1]["reason"] == "G9 failed"
        assert _log(folder)[-1]["from"] == "live"

    def test_paper_cannot_jump_to_retired(self, tmp_path):
        self._paper(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "retired", "--reason", "x") == 1


class TestBackward:
    def test_backward_needs_a_reason(self, tmp_path, capsys):
        folder = _idea(tmp_path, "stage: backtest\n")
        assert _run(tmp_path, "stage", "IDEA-001", "inbox") == 2
        assert "--reason" in capsys.readouterr().out
        assert _fm(folder)["stage"] == "backtest"

    def test_backward_with_a_reason_is_always_allowed(self, tmp_path):
        folder = _idea(tmp_path, "stage: backtest\nresearch_state: rejected\n")
        assert _run(tmp_path, "stage", "IDEA-001", "inbox", "--reason", "restart") == 0
        assert _fm(folder)["stage"] == "inbox"
        assert _log(folder)[-1]["reason"] == "restart"

    def test_paper_back_to_backtest_keeps_the_promotion_pointers(self, tmp_path):
        folder = _idea(
            tmp_path,
            "stage: paper\n",
            "promoted_version: v1\npromoted_run_id: 2026-09-29-orb-v1\n",
            versions=True,
        )
        _setup(folder, **READY)
        assert _run(tmp_path, "stage", "IDEA-001", "backtest", "--reason", "retest") == 0
        assert _fm(folder)["promoted_version"] == "v1"

    def test_empty_reason_counts_as_missing(self, tmp_path):
        _idea(tmp_path, "stage: backtest\n")
        assert _run(tmp_path, "stage", "IDEA-001", "inbox", "--reason", "  ") == 2


class TestStageLog:
    def test_log_is_append_only_and_earlier_text_is_untouched(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "stage", "IDEA-001", "backtest") == 0
        first = (folder / "stage-log.yaml").read_bytes()
        assert _run(tmp_path, "stage", "IDEA-001", "inbox", "--reason", "redo") == 0
        second = (folder / "stage-log.yaml").read_bytes()
        assert second.startswith(first) and len(second) > len(first)
        assert [e["to"] for e in _log(folder)] == ["backtest", "inbox"]

    def test_existing_hand_edits_survive(self, tmp_path):
        folder = _idea(tmp_path)
        (folder / "stage-log.yaml").write_text(
            "# my note\n- {date_ist: x, from: a, to: b, reason: r, run_id: null}", encoding="utf-8"
        )
        assert _run(tmp_path, "stage", "IDEA-001", "backtest") == 0
        text = (folder / "stage-log.yaml").read_text(encoding="utf-8")
        assert text.startswith(
            "# my note\n- {date_ist: x, from: a, to: b, reason: r, run_id: null}\n"
        )
        assert len(_log(folder)) == 2

    def test_a_refused_move_writes_nothing(self, tmp_path):
        folder = _idea(tmp_path, spec=False)
        _run(tmp_path, "stage", "IDEA-001", "backtest")
        assert not (folder / "stage-log.yaml").exists()


class TestState:
    def test_sets_research_state(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "state", "IDEA-001", "promising") == 0
        assert _fm(folder)["research_state"] == "promising"

    def test_unknown_state_exits_two(self, tmp_path):
        _idea(tmp_path)
        with pytest.raises(SystemExit) as err:
            _run(tmp_path, "state", "IDEA-001", "bogus")
        assert err.value.code == 2

    def test_shelved_needs_revisit_when(self, tmp_path, capsys):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "state", "IDEA-001", "shelved") == 2
        assert "--revisit-when" in capsys.readouterr().out
        assert "research_state" not in _fm(folder)

    def test_shelved_with_revisit_when_is_stored_and_registry_valid(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "state", "IDEA-001", "shelved", "--revisit-when", "after: data") == 0
        data = _fm(folder)
        assert data["research_state"] == "shelved" and data["revisit_when"] == "after: data"
        assert br.load_ideas(tmp_path)[0]["status"] == "shelved"

    def test_revisit_when_can_accompany_any_state(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "state", "IDEA-001", "active", "--revisit-when", "soon") == 0
        assert _fm(folder)["revisit_when"] == "soon"

    def test_state_change_never_touches_runs_or_stage_log(self, tmp_path):
        folder = _idea(tmp_path, "stage: backtest\n")
        _setup(folder)
        before = (folder / "runs" / "2026-09-29-orb-v1.json").read_bytes()
        _run(tmp_path, "state", "IDEA-001", "weak_evidence")
        assert (folder / "runs" / "2026-09-29-orb-v1.json").read_bytes() == before
        assert not (folder / "stage-log.yaml").exists()


class TestClassify:
    def test_sets_category_and_tags(self, tmp_path):
        folder = _idea(tmp_path)
        assert (
            _run(
                tmp_path,
                "classify",
                "IDEA-001",
                "--category",
                "Mean Reversion",
                "--tags",
                "vwap, VWAP,ema",
            )
            == 0
        )
        data = _fm(folder)
        assert data["strategy_category"] == "Mean Reversion"
        assert data["strategy_tags"] == ["vwap", "ema"]
        assert br.load_ideas(tmp_path)[0]["classification"]["strategy_category"] == "Mean Reversion"

    def test_category_alone_leaves_tags_alone(self, tmp_path):
        folder = _idea(tmp_path, extra="strategy_tags: [keep]\n")
        assert _run(tmp_path, "classify", "IDEA-001", "--category", "Grid") == 0
        assert _fm(folder)["strategy_tags"] == ["keep"]

    def test_tags_alone_leave_category_alone(self, tmp_path):
        folder = _idea(tmp_path, extra="strategy_category: Options\n")
        assert _run(tmp_path, "classify", "IDEA-001", "--tags", "x") == 0
        assert _fm(folder)["strategy_category"] == "Options"

    def test_empty_tags_clear_the_list(self, tmp_path):
        folder = _idea(tmp_path, extra="strategy_tags: [a]\n")
        assert _run(tmp_path, "classify", "IDEA-001", "--tags", "") == 0
        assert _fm(folder)["strategy_tags"] == []

    def test_unknown_category_exits_two(self, tmp_path, capsys):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "classify", "IDEA-001", "--category", "Astrology") == 2
        assert "Astrology" in capsys.readouterr().out
        assert "strategy_category" not in _fm(folder)

    def test_nothing_to_set_exits_two(self, tmp_path):
        _idea(tmp_path)
        assert _run(tmp_path, "classify", "IDEA-001") == 2

    def test_taxonomy_file_in_root_is_honoured(self, tmp_path):
        folder = _idea(tmp_path)
        (tmp_path / "research" / "taxonomy.yaml").write_text(
            "categories:\n  - Seasonality\n  - Other\n", encoding="utf-8"
        )
        assert _run(tmp_path, "classify", "IDEA-001", "--category", "Seasonality") == 0
        assert _fm(folder)["strategy_category"] == "Seasonality"

    def test_works_without_a_spec(self, tmp_path):
        _idea(tmp_path, spec=False)
        assert _run(tmp_path, "classify", "IDEA-001", "--category", "Other") == 0


def test_json_in_registry_is_unaffected_by_cli_idea_md_edit(tmp_path):
    _idea(tmp_path)
    _run(tmp_path, "state", "IDEA-001", "promising")
    data = json.loads(br.render_json(br.load_ideas(tmp_path)))
    assert (data["ideas"] if isinstance(data, dict) else data)[0]["research_state"] == "promising"


class TestLogFirst:
    def test_a_failed_frontmatter_write_leaves_the_stage_and_names_the_log(
        self, tmp_path, monkeypatch, capsys
    ):
        folder = _idea(tmp_path)

        def boom(*_a, **_k):
            raise frontmatter.FrontmatterError("disk full")

        monkeypatch.setattr(frontmatter, "set_keys", boom)
        assert _run(tmp_path, "stage", "IDEA-001", "backtest") == 2
        assert "stage-log" in capsys.readouterr().out
        assert _fm(folder)["stage"] == "inbox"
        assert _log(folder)[-1]["to"] == "backtest"

    def test_the_log_is_written_before_the_frontmatter(self, tmp_path, monkeypatch):
        folder = _idea(tmp_path)
        seen = []
        real = frontmatter.set_keys

        def spy(f, updates):
            seen.append(len(_log(folder)))
            real(f, updates)

        monkeypatch.setattr(frontmatter, "set_keys", spy)
        assert _run(tmp_path, "stage", "IDEA-001", "backtest") == 0
        assert seen == [1]


class TestRejectedNeedsABasis:
    def test_rejected_without_a_basis_exits_two_and_writes_nothing(self, tmp_path, capsys):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "state", "IDEA-001", "rejected") == 2
        assert "--rejection-basis" in capsys.readouterr().out
        assert "research_state" not in _fm(folder)

    def test_unknown_basis_exits_two(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "state", "IDEA-001", "rejected", "--rejection-basis", "vibes") == 2
        assert "research_state" not in _fm(folder)

    def test_rejected_with_a_basis_is_stored_and_registry_valid(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "state", "IDEA-001", "rejected", "--rejection-basis", "overfit") == 0
        data = _fm(folder)
        assert data["research_state"] == "rejected" and data["rejection_basis"] == "overfit"
        assert br.load_ideas(tmp_path)[0]["status"] == "rejected"

    def test_leaving_rejected_removes_the_basis_so_the_registry_stays_valid(self, tmp_path):
        folder = _idea(tmp_path)
        _run(tmp_path, "state", "IDEA-001", "rejected", "--rejection-basis", "overfit")
        assert _run(tmp_path, "state", "IDEA-001", "needs_more_evidence") == 0
        assert "rejection_basis" not in _fm(folder)
        assert len(br.load_ideas(tmp_path)) == 1

    def test_a_basis_on_another_state_is_refused(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "state", "IDEA-001", "active", "--rejection-basis", "overfit") == 2
        assert "rejection_basis" not in _fm(folder)


class TestReturnToPaperNeedsEvidence:
    def _retired(self, tmp_path, with_run):
        folder = _idea(
            tmp_path,
            "stage: retired\n",
            "promoted_version: v1\npromoted_run_id: 2026-09-29-orb-v1\n",
            versions=True,
        )
        if with_run:
            _setup(folder, **READY)
        return folder

    def test_retired_to_paper_is_refused_without_a_qualifying_run(self, tmp_path, capsys):
        folder = self._retired(tmp_path, with_run=False)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--reason", "retry") == 1
        assert "qualif" in capsys.readouterr().out
        assert _fm(folder)["stage"] == "retired" and _log(folder) == []

    def test_retired_to_paper_is_refused_when_the_stored_run_stopped_qualifying(self, tmp_path):
        folder = _idea(
            tmp_path,
            "stage: retired\n",
            "promoted_version: v1\npromoted_run_id: 2026-09-29-orb-v1\n",
            versions=True,
        )
        _setup(folder, promotion_decision="NOT_READY")
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--reason", "retry") == 1
        assert _fm(folder)["stage"] == "retired"

    def test_retired_to_paper_with_a_qualifying_run_is_allowed(self, tmp_path):
        folder = self._retired(tmp_path, with_run=True)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--reason", "retry") == 0
        assert _fm(folder)["stage"] == "paper"

    def test_retired_without_stored_pointers_is_refused(self, tmp_path):
        folder = _idea(tmp_path, "stage: retired\n", versions=True)
        assert _run(tmp_path, "stage", "IDEA-001", "paper", "--reason", "retry") == 1
        assert _fm(folder)["stage"] == "retired"
