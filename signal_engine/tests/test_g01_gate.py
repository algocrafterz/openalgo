"""Attestation ledger and the G0.1 gate: validity follows the commits and the covered paths."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date
from pathlib import Path

import pytest

from signal_engine.backtest import attestations as at
from signal_engine.backtest import evaluation as ev
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}  # fmt: skip


def git(repo, *args):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=ENV, check=True)
    return out.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "signal_engine" / "strategy_core").mkdir(parents=True)
    (r / "signal_engine" / "adapters").mkdir()
    (r / "signal_engine" / "other").mkdir()
    (r / "signal_engine" / "strategy_core" / "core.py").write_text("RULE = 1\n")
    (r / "signal_engine" / "adapters" / "a.py").write_text("import core\n")
    (r / "signal_engine" / "other" / "x.py").write_text("X = 1\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=ENV)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "c1")
    return r


def commit(repo, rel, text, msg="c"):
    p = repo / rel
    p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


COVERED = ["signal_engine/strategy_core", "signal_engine/adapters/a.py"]


def attest(repo, commit_sha=None, **kw):
    args = {"root": repo / "signal_engine", "gate": "G0.1", "commit": commit_sha or git(repo, "rev-parse", "HEAD"),
                "covered_paths": COVERED, "strategies": ["VwapReversion"],
                "evidence": {"checks": [{"name": "t", "result": "ok"}], "modes": {"VwapReversion": "backtest"}},
                "reviewer": "rev", "on": date(2026, 10, 10)}  # fmt: skip
    args.update(kw)
    return at.append(at.ledger_path(repo / "signal_engine"), **args)


def verify(repo, run_commit=None, strategy="VwapReversion", dirty=()):
    run = run_commit if run_commit is not None else git(repo, "rev-parse", "HEAD")
    return at.verify("G0.1", strategy, run, repo / "signal_engine", dirty)


class TestLedger:
    def test_a_record_is_chained_hashed_and_numbered(self, repo):
        a, b = attest(repo), attest(repo)
        assert (a["id"], b["id"]) == ("G0.1-001", "G0.1-002")
        assert a["prev_sha256"] == at.GENESIS and b["prev_sha256"] == a["content_sha256"]
        assert at.chain_problems(at.load(at.ledger_path(repo / "signal_engine"))) == []

    def test_it_only_ever_appends(self, repo):
        attest(repo)
        path = at.ledger_path(repo / "signal_engine")
        first = path.read_text()
        attest(repo)
        assert path.read_text().startswith(first)

    def test_it_refuses_what_it_cannot_stand_behind(self, repo):
        with pytest.raises(at.AttestationError, match="not known"):
            attest(repo, commit_sha="deadbeef" * 5)
        with pytest.raises(at.AttestationError, match="does not exist"):
            attest(repo, covered_paths=["signal_engine/nope"])
        with pytest.raises(at.AttestationError, match="required"):
            attest(repo, evidence={})
        with pytest.raises(at.AttestationError, match="required"):
            attest(repo, reviewer="  ")
        (repo / "signal_engine" / "strategy_core" / "core.py").write_text("RULE = 2\n")
        with pytest.raises(at.AttestationError, match="uncommitted"):
            attest(repo)

    def test_an_edited_deleted_or_reordered_line_breaks_the_chain(self, repo):
        attest(repo), attest(repo)
        path = at.ledger_path(repo / "signal_engine")
        lines = path.read_text().splitlines()
        edited = json.loads(lines[0])
        edited["reviewer"] = "someone else"
        path.write_text("\n".join([json.dumps(edited), lines[1]]) + "\n")
        assert at.chain_problems(at.load(path))
        path.write_text(lines[1] + "\n")
        assert at.chain_problems(at.load(path))
        path.write_text("\n".join([lines[1], lines[0]]) + "\n")
        assert at.chain_problems(at.load(path))

    def test_a_ledger_that_is_not_sound_refuses_new_records(self, repo):
        attest(repo)
        path = at.ledger_path(repo / "signal_engine")
        path.write_text(path.read_text().replace("rev", "evil"))
        with pytest.raises(at.AttestationError, match="not sound"):
            attest(repo)


class TestVerify:
    def test_the_attested_commit_itself_is_valid(self, repo):
        attest(repo)
        v = verify(repo)
        assert v["status"] == "valid" and v["record"]["id"] == "G0.1-001" and v["record"]["modes"] == "backtest"

    def test_a_later_commit_that_leaves_the_covered_paths_alone_is_valid(self, repo):
        attest(repo)
        commit(repo, "signal_engine/other/x.py", "X = 2\n")
        assert verify(repo)["status"] == "valid"

    def test_a_covered_file_changing_after_the_attested_commit_makes_it_stale(self, repo):
        attest(repo)
        commit(repo, "signal_engine/strategy_core/core.py", "RULE = 2\n")
        v = verify(repo)
        assert v["status"] == "stale" and "core.py" in v["reasons"][0]

    def test_a_newer_record_at_the_new_state_restores_validity(self, repo):
        attest(repo)
        commit(repo, "signal_engine/adapters/a.py", "import core2\n")
        assert verify(repo)["status"] == "stale"
        attest(repo)
        v = verify(repo)
        assert v["status"] == "valid" and v["record"]["id"] == "G0.1-002"

    def test_uncommitted_changes_under_a_covered_path_make_it_stale(self, repo):
        attest(repo)
        v = verify(repo, dirty=["signal_engine/strategy_core/core.py"])
        assert v["status"] == "stale" and "uncommitted" in v["reasons"][0]
        assert verify(repo, dirty=["signal_engine/other/x.py"])["status"] == "valid"

    def test_a_commit_that_is_not_an_ancestor_is_invalid(self, repo):
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "side")
        side = commit(repo, "signal_engine/other/x.py", "X = 3\n")
        git(repo, "checkout", "-q", "-")
        main_tip = commit(repo, "signal_engine/other/x.py", "X = 4\n")
        attest(repo, commit_sha=side)
        v = verify(repo, run_commit=main_tip)
        assert v["status"] == "invalid" and "not an ancestor" in v["reasons"][0]
        assert base != side

    def test_a_strategy_the_record_does_not_cover_is_missing(self, repo):
        attest(repo)
        v = verify(repo, strategy="Orb")
        assert v["status"] == "missing" and "Orb" in v["reasons"][0]

    def test_no_ledger_is_missing(self, repo):
        assert verify(repo)["status"] == "missing"

    def test_a_broken_chain_is_invalid(self, repo):
        attest(repo)
        path = at.ledger_path(repo / "signal_engine")
        path.write_text(path.read_text().replace("rev", "evil"))
        assert verify(repo)["status"] == "invalid"

    def test_an_unknown_run_commit_is_invalid(self, repo):
        attest(repo)
        assert verify(repo, run_commit="unknown")["status"] == "invalid"
        assert verify(repo, run_commit="")["status"] == "invalid"

    def test_a_record_for_another_gate_does_not_count(self, repo):
        attest(repo, gate="G0.7")
        assert verify(repo)["status"] == "missing"


def _g01(attestation):
    return _row(_evaluate(_noisy_book(), attestation=attestation), "G0.1")


class TestGate:
    VALID = {"gate": "G0.1", "status": "valid", "reasons": [],
             "record": {"id": "G0.1-001", "commit": "a" * 40, "reviewer": "rev", "date": "2026-10-10",
                        "covered_paths": ["signal_engine/strategy_core"], "checks": 7, "modes": "backtest only"}}  # fmt: skip

    def test_a_valid_attestation_passes_and_says_who_and_what(self):
        row = _g01(self.VALID)
        assert row["status"] == "pass" and row["hard"] is False and row["value"] == "G0.1-001"
        for part in ("rev", "aaaaaaaaa", "ancestor", "7 recorded checks", "backtest only", "not a proof"):
            assert part in row["evidence"], part

    @pytest.mark.parametrize("status", ["missing", "stale", "invalid"])
    def test_anything_else_is_not_evaluated_with_the_reason(self, status):
        row = _g01({"gate": "G0.1", "status": status, "reasons": ["because"], "record": None})
        assert row["status"] == "not_evaluated" and "because" in row["evidence"] and status in row["evidence"]

    def test_no_lookup_is_not_evaluated(self):
        assert _g01(None)["status"] == "not_evaluated"

    def test_it_is_registered_and_a_not_evaluated_row_blocks_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        assert "G0.1" in ev._EVALUATORS
        result = _derive(_evaluation("pass", G0_1="not_evaluated"))
        assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G0.1"


class TestCommittedLedger:
    LEDGER = Path(__file__).resolve().parents[1] / "attestations" / "ledger.ndjson"

    def test_the_committed_ledger_is_a_sound_chain(self):
        records = at.load(self.LEDGER)
        assert records and at.chain_problems(records) == []

    def test_the_first_attestation_names_what_it_covers_and_what_it_does_not(self):
        first = at.load(self.LEDGER)[0]
        assert first["gate"] == "G0.1" and first["id"] == "G0.1-001"
        assert set(first["strategies"]) == {"VwapReversion", "Breakout", "EmaVwapEarly"}
        assert "signal_engine/strategy_core" in first["covered_paths"]
        ev_ = first["evidence"]
        assert ev_["known_gaps"] and ev_["not_covered"] and "owner" in first["reviewer"]
        assert all("0 failed" in c["result"] for c in ev_["checks"])
        assert "no paper or live adapter" in ev_["modes"]["VwapReversion"]
