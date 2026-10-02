"""The intake CLI: check, resolve, confirm, questions, block and set-current."""

from __future__ import annotations

import pytest

from signal_engine.research.intake import cli, ledger, versions
from signal_engine.tests.intake_fixtures import make_spec, write_spec

IDEA_MD = "---\nid: IDEA-012\ntitle: T\nstage: backtest\n---\n\nBody.\n"


def _idea(tmp_path, **changes):
    folder = tmp_path / "research" / "ideas" / "idea-012-sample"
    write_spec(folder, make_spec(**changes))
    (folder / "idea.md").write_text(IDEA_MD, encoding="utf-8")
    return folder


def _run(tmp_path, *argv):
    return cli.main(list(argv), root=tmp_path)


class TestCheck:
    def test_ready_idea_exits_zero_and_prints_the_state(self, tmp_path, capsys):
        _idea(tmp_path)
        assert _run(tmp_path, "check", "IDEA-012") == 0
        assert "BACKTEST_READY" in capsys.readouterr().out

    def test_review_idea_exits_one_and_lists_the_entry(self, tmp_path, capsys):
        _idea(tmp_path, portfolio={"mode": "single_position"})
        assert _run(tmp_path, "check", "IDEA-012") == 1
        out = capsys.readouterr().out
        assert "ASSUMPTION_REVIEW" in out and "portfolio.scope" in out

    def test_draft_idea_lists_the_blocker(self, tmp_path, capsys):
        _idea(tmp_path, entry=None)
        assert _run(tmp_path, "check", "IDEA-012") == 1
        out = capsys.readouterr().out
        assert "DRAFT" in out and "entry" in out

    def test_check_does_not_write_anything(self, tmp_path):
        folder = _idea(tmp_path)
        _run(tmp_path, "check", "IDEA-012")
        assert not (folder / "assumptions.yaml").exists()

    def test_unknown_idea_exits_two(self, tmp_path, capsys):
        _idea(tmp_path)
        assert _run(tmp_path, "check", "IDEA-999") == 2
        assert "IDEA-999" in capsys.readouterr().out

    def test_missing_spec_exits_one(self, tmp_path, capsys):
        folder = _idea(tmp_path)
        (folder / "spec.yaml").unlink()
        assert _run(tmp_path, "check", "IDEA-012") == 1
        assert "spec.yaml" in capsys.readouterr().out


class TestResolveAndConfirm:
    def test_resolve_writes_the_working_ledger(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "resolve", "IDEA-012") == 0
        assert ledger.load(folder) is not None

    def test_confirm_flips_the_entry_and_reaches_ready(self, tmp_path, capsys):
        folder = _idea(tmp_path, portfolio={"mode": "single_position"})
        _run(tmp_path, "resolve", "IDEA-012")
        target = ledger.load(folder).get("portfolio.scope").id
        assert _run(tmp_path, "confirm", "IDEA-012", target) == 0
        assert ledger.load(folder).get("portfolio.scope").user_confirmed is True
        capsys.readouterr()
        assert _run(tmp_path, "check", "IDEA-012") == 0

    def test_confirm_resolves_first_when_there_is_no_ledger_yet(self, tmp_path):
        folder = _idea(tmp_path, portfolio={"mode": "single_position"})
        fresh = ledger.resolve(make_spec(portfolio={"mode": "single_position"}))
        target = fresh.get("portfolio.scope").id
        assert _run(tmp_path, "confirm", "IDEA-012", target) == 0
        assert ledger.load(folder).get("portfolio.scope").user_confirmed is True

    def test_confirm_unknown_id_exits_two_and_writes_nothing(self, tmp_path):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "confirm", "IDEA-012", "A-999") == 2
        assert ledger.load(folder) is None

    def test_changed_spec_resets_confirmation_on_the_next_resolve(self, tmp_path):
        folder = _idea(tmp_path, portfolio={"mode": "single_position"})
        _run(tmp_path, "resolve", "IDEA-012")
        _run(tmp_path, "confirm", "IDEA-012", ledger.load(folder).get("portfolio.scope").id)
        write_spec(
            folder,
            make_spec(
                portfolio={"mode": "single_position"},
                filters=["Only one position at a time across the universe"],
            ),
        )
        _run(tmp_path, "resolve", "IDEA-012")
        assert ledger.load(folder).get("portfolio.scope").user_confirmed is False


class TestQuestionsAndBlock:
    def test_ask_records_a_question_and_moves_a_draft_to_needs_clarification(
        self, tmp_path, capsys
    ):
        folder = _idea(tmp_path, entry=None)
        assert _run(tmp_path, "ask", "IDEA-012", "What is the breakout?") == 0
        assert ledger.load(folder).open_questions == ("What is the breakout?",)
        capsys.readouterr()
        _run(tmp_path, "check", "IDEA-012")
        assert "NEEDS_CLARIFICATION" in capsys.readouterr().out

    def test_clear_questions(self, tmp_path):
        folder = _idea(tmp_path, entry=None)
        _run(tmp_path, "ask", "IDEA-012", "q")
        assert _run(tmp_path, "clear-questions", "IDEA-012") == 0
        assert ledger.load(folder).open_questions == ()

    def test_block_and_unblock(self, tmp_path, capsys):
        folder = _idea(tmp_path)
        assert _run(tmp_path, "block", "IDEA-012", "source withdrawn") == 0
        assert ledger.load(folder).blocked_reason == "source withdrawn"
        capsys.readouterr()
        _run(tmp_path, "check", "IDEA-012")
        assert "BACKTEST_BLOCKED" in capsys.readouterr().out
        assert _run(tmp_path, "unblock", "IDEA-012") == 0
        assert ledger.load(folder).blocked_reason is None

    def test_asking_keeps_existing_confirmations(self, tmp_path):
        folder = _idea(tmp_path, portfolio={"mode": "single_position"})
        _run(tmp_path, "resolve", "IDEA-012")
        _run(tmp_path, "confirm", "IDEA-012", ledger.load(folder).get("portfolio.scope").id)
        _run(tmp_path, "ask", "IDEA-012", "anything else?")
        assert ledger.load(folder).get("portfolio.scope").user_confirmed is True


class TestSetCurrent:
    def test_set_current_writes_the_pointer(self, tmp_path):
        folder = _idea(tmp_path)
        versions.resolve_version(folder, "a" * 64)
        assert _run(tmp_path, "set-current", "IDEA-012", "v1") == 0
        assert versions.read_pointers(folder)["current_version"] == "v1"

    def test_unknown_version_exits_two(self, tmp_path):
        _idea(tmp_path)
        assert _run(tmp_path, "set-current", "IDEA-012", "v5") == 2

    def test_unknown_command_is_an_argparse_error(self, tmp_path):
        with pytest.raises(SystemExit):
            _run(tmp_path, "explode", "IDEA-012")
