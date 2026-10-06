"""Run record additions: n_minutes distribution, strategy diagnostics, spec provenance."""

import hashlib
import subprocess

import pandas as pd
import pytest

from signal_engine.backtest import run_diagnostics as rd


def _frame(stamps, n):
    return pd.DataFrame({"Close": 1.0, "n_minutes": n},
                        index=pd.DatetimeIndex(pd.to_datetime(stamps)))


def test_distribution_counts_overall_and_by_year_with_all_five_keys():
    frames = {
        "A": _frame(["2024-01-02 09:15", "2024-01-02 09:20", "2025-01-02 09:15"], [5, 3, 5]),
        "B": _frame(["2025-01-02 09:15", "2025-01-02 09:20"], [1, 5]),
    }
    got = rd.n_minutes_distribution(frames)
    assert got["overall"] == {"1": 1, "2": 0, "3": 1, "4": 0, "5": 3}
    assert got["by_year"]["2024"] == {"1": 0, "2": 0, "3": 1, "4": 0, "5": 1}
    assert got["by_year"]["2025"] == {"1": 1, "2": 0, "3": 0, "4": 0, "5": 2}
    assert got["bars"] == 5


def test_distribution_is_none_when_no_frame_has_the_column():
    plain = pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex(["2024-01-02 09:15"]))
    assert rd.n_minutes_distribution({"A": plain}) is None
    assert rd.n_minutes_distribution({}) is None


def test_distribution_keeps_an_unexpected_value_visible():
    got = rd.n_minutes_distribution({"A": _frame(["2024-01-02 09:15"], [7])})
    assert got["overall"]["7"] == 1 and got["overall"]["5"] == 0


def test_strategy_diagnostics_reads_the_method():
    class S:
        def diagnostics(self):
            return {"arms": 3, "confirms": 1}

    assert rd.strategy_diagnostics(S()) == {"arms": 3, "confirms": 1}


def test_strategy_diagnostics_is_none_without_the_method():
    assert rd.strategy_diagnostics(object()) is None


@pytest.mark.parametrize("bad", [[1], {"a": "x"}, {"a": 1.5}, {1: 2}, {"a": True}])
def test_strategy_diagnostics_must_be_a_mapping_of_name_to_int(bad):
    class S:
        def diagnostics(self):
            return bad

    with pytest.raises(TypeError):
        rd.strategy_diagnostics(S())


def test_spec_provenance_records_sha256_and_the_last_commit(tmp_path):
    folder = tmp_path / "idea"
    folder.mkdir()
    spec = folder / "spec.yaml"
    spec.write_text("name: x\n", encoding="utf-8")
    assert rd.spec_provenance(folder)["commit"] is None
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "."], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-qm", "spec"], cwd=tmp_path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True, check=True).stdout.strip()
    got = rd.spec_provenance(folder)
    assert got == {"path": "spec.yaml", "sha256": hashlib.sha256(b"name: x\n").hexdigest(),
                   "commit": head, "modified_since_commit": False}
    spec.write_text("name: y\n", encoding="utf-8")
    assert rd.spec_provenance(folder)["modified_since_commit"] is True


def test_spec_provenance_without_a_spec_is_none(tmp_path):
    assert rd.spec_provenance(tmp_path) is None
