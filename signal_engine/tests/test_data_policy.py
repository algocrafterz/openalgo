"""The data policy file: every threshold the data-readiness rules use, versioned by content."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from signal_engine.backtest.data_readiness import policy as pol

REPO_POLICY = Path(__file__).resolve().parents[1] / "research" / "data_policy.yaml"


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "data_policy.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def _doc() -> dict:
    return yaml.safe_load(REPO_POLICY.read_text())


class TestRepoPolicy:
    def test_repo_policy_loads(self):
        p = pol.load_policy(REPO_POLICY)
        assert p.max_defect_excluded_symbols == 10
        assert p.max_defect_excluded_symbol_share == pytest.approx(0.05)
        assert p.bad_bar_share == pytest.approx(0.001)
        assert len(p.version) == 64

    def test_values_reused_from_dq_run_match(self):
        from signal_engine.backtest import dq_run

        p = pol.load_policy(REPO_POLICY)
        assert p.bad_bar_share == dq_run.DEFAULT_THRESHOLDS["bad_bar_share"]
        assert p.max_missing_session_share == (
            dq_run.DEFAULT_THRESHOLDS["incomplete_symbol_session_share"]
        )


class TestValidation:
    def test_missing_key_fails_fast(self, tmp_path):
        doc = _doc()
        del doc["exclusions"]["max_defect_excluded_symbols"]
        with pytest.raises(pol.PolicyError, match="max_defect_excluded_symbols"):
            pol.load_policy(_write(tmp_path, doc))

    def test_unknown_key_fails_fast(self, tmp_path):
        doc = _doc()
        doc["thresholds"]["surprise"] = 1
        with pytest.raises(pol.PolicyError, match="surprise"):
            pol.load_policy(_write(tmp_path, doc))

    def test_share_out_of_range_fails(self, tmp_path):
        doc = _doc()
        doc["exclusions"]["max_defect_excluded_symbol_share"] = 1.5
        with pytest.raises(pol.PolicyError, match="max_defect_excluded_symbol_share"):
            pol.load_policy(_write(tmp_path, doc))

    def test_negative_count_fails(self, tmp_path):
        doc = _doc()
        doc["exclusions"]["max_defect_excluded_symbols"] = -1
        with pytest.raises(pol.PolicyError, match="max_defect_excluded_symbols"):
            pol.load_policy(_write(tmp_path, doc))

    def test_missing_file_fails(self, tmp_path):
        with pytest.raises(pol.PolicyError, match="not found"):
            pol.load_policy(tmp_path / "absent.yaml")


class TestVersion:
    def test_version_changes_with_a_value(self, tmp_path):
        a = pol.load_policy(_write(tmp_path, _doc()))
        doc = _doc()
        doc["exclusions"]["max_defect_excluded_symbols"] = 11
        b = pol.load_policy(_write(tmp_path, doc))
        assert a.version != b.version

    def test_version_ignores_comments_and_key_order(self, tmp_path):
        a = pol.load_policy(_write(tmp_path, _doc()))
        path = tmp_path / "commented.yaml"
        path.write_text("# a new comment\n" + REPO_POLICY.read_text())
        assert pol.load_policy(path).version == a.version


class TestReviewFindings:
    @pytest.mark.parametrize("section,key,value", [
        ("diagnostics", "spike_return", float("nan")),
        ("diagnostics", "spike_return", float("inf")),
        ("thresholds", "bad_bar_share", float("nan")),
        ("thresholds", "daily_close_time", "ab:cd"),
        ("thresholds", "daily_close_time", "25:00"),
        ("thresholds", "placeholder_lookback_sessions", 0),
    ])
    def test_bad_values_fail_fast(self, tmp_path, section, key, value):
        doc = _doc()
        doc[section][key] = value
        with pytest.raises(pol.PolicyError, match=key):
            pol.load_policy(_write(tmp_path, doc))


class TestAdjustmentSection:
    def test_repo_adjustment_values(self):
        p = pol.load_policy(REPO_POLICY)
        assert p.cross_basis_tolerance == pytest.approx(0.05)
        assert p.cross_basis_min_sessions == 5
        assert p.jump_min_move == pytest.approx(0.20)
        assert p.jump_ratio_tolerance == pytest.approx(0.015)
        assert 0.5 in p.jump_ratios and isinstance(p.jump_ratios, tuple)

    @pytest.mark.parametrize("key,value", [
        ("cross_basis_tolerance", 1.5),
        ("cross_basis_min_sessions", 0),
        ("jump_min_move", -0.1),
        ("jump_ratio_tolerance", float("nan")),
        ("jump_ratios", []),
        ("jump_ratios", [0.5, 1.0]),
        ("jump_ratios", [0.5, "x"]),
        ("jump_ratios", 0.5),
    ])
    def test_bad_adjustment_values_fail_fast(self, tmp_path, key, value):
        doc = _doc()
        doc["adjustment"][key] = value
        with pytest.raises(pol.PolicyError, match=key):
            pol.load_policy(_write(tmp_path, doc))
