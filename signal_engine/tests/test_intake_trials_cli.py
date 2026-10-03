"""The `trials` intake commands."""

from __future__ import annotations

from datetime import datetime

import pytest

from signal_engine.research.intake import cli, trials
from signal_engine.timeutils import IST

IDEA_MD = "---\nid: IDEA-012\ntitle: T\nstage: backtest\n---\n\nBody.\n"
FP = {k: str(i) * 64 for i, k in enumerate(("rules", "params", "execution", "cost"))}


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "research" / "ideas" / "idea-012-sample"
    path.mkdir(parents=True)
    (path / "idea.md").write_text(IDEA_MD, encoding="utf-8")
    return path


def _run(tmp_path, *argv):
    return cli.main(list(argv), root=tmp_path)


def test_show_empty(tmp_path, folder, capsys):
    assert _run(tmp_path, "trials", "show", "IDEA-012") == 0
    out = capsys.readouterr().out
    assert "material 0" in out and "external 0" in out and "total 0" in out


def test_show_lists_entries(tmp_path, folder, capsys):
    now = datetime(2026, 10, 4, 10, 0, tzinfo=IST)
    trials.register_material(folder, FP, "run-1", now=now)
    trials.add_external(folder, 5, "notebook sweep", now=now)
    assert _run(tmp_path, "trials", "show", "IDEA-012") == 0
    out = capsys.readouterr().out
    assert "material 1" in out and "external 5" in out and "total 6" in out
    assert "run-1" in out and "notebook sweep" in out and "+05:30" in out


def test_add_happy_path(tmp_path, folder, capsys):
    assert _run(tmp_path, "trials", "add", "IDEA-012", "7", "--reason", "grid search") == 0
    assert "total 7" in capsys.readouterr().out
    entry = trials.load_trials(folder)[0]
    assert entry.count == 7 and entry.reason == "grid search"
    assert entry.created_ist.endswith("+05:30")


@pytest.mark.parametrize("bad", ["0", "-2", "abc", "1.5"])
def test_add_bad_n_exits_two(tmp_path, folder, capsys, bad):
    assert _run(tmp_path, "trials", "add", "IDEA-012", bad, "--reason", "x") == 2
    assert not (folder / "trials.yaml").exists()


@pytest.mark.parametrize("extra", [[], ["--reason", "  "]])
def test_add_missing_reason_exits_two(tmp_path, folder, capsys, extra):
    assert _run(tmp_path, "trials", "add", "IDEA-012", "3", *extra) == 2
    assert "reason" in capsys.readouterr().out
    assert not (folder / "trials.yaml").exists()


def test_unknown_idea_exits_two(tmp_path, folder, capsys):
    assert _run(tmp_path, "trials", "show", "IDEA-999") == 2
    assert "IDEA-999" in capsys.readouterr().out


def test_malformed_ledger_exits_one(tmp_path, folder, capsys):
    (folder / "trials.yaml").write_text("a: 1\n", encoding="utf-8")
    assert _run(tmp_path, "trials", "show", "IDEA-012") == 1
    assert _run(tmp_path, "trials", "add", "IDEA-012", "1", "--reason", "x") == 1
    assert (folder / "trials.yaml").read_text() == "a: 1\n"
