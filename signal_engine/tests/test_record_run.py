"""record_run: run an idea's backtest and store the result as an immutable run record.

The contract pinned here is what makes a history of runs trustworthy: a run file is never
overwritten, ids never collide, the lineage points at a run that exists, the record
validates against the registry schema, and one failing idea cannot stop the others.
"""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br

SUPPORTED = {"orb", "ema9_vwap"}

IDEA_MD = """---
id: {id}
title: Sample {slug}
status: rejected
verdict: no-edge
category: intraday-breakout
horizon: intraday
source: {{type: video, ref: "https://example.invalid/v"}}
summary: One line.
result: Nothing.
backtest_module: {module}
revisit_when: Never.
last_reviewed: 2026-09-29
---

Body.
"""


def _trade(day, r_gross, r_net):
    return Trade(
        symbol="A.NS",
        day=day,
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=f"{day} 15:00",
        reason="tp",
        r_gross=r_gross,
        r_net=r_net,
    )


def _days(n=40):
    return [date(2026, 1 + i // 20, 1 + i % 20) for i in range(n)]


def _execution(n=40):
    days = _days(n)
    trades = [_trade(d, 1.1 if i % 3 else -1.0, 1.0 if i % 3 else -1.1) for i, d in enumerate(days)]
    return rr.Execution(
        trades=trades,
        params={"strategy": {"or_minutes": 15}, "run": {"cost_bps": 16.0}},
        data={
            "source": "historify",
            "as_of": days[-1].isoformat(),
            "universe": "historify-3-symbols",
            "bar_size": "5m",
        },
        period={"start": days[0].isoformat(), "end": days[-1].isoformat()},
        costs={"model": "india_intraday_bps", "round_trip_bps": 16.0},
        days=[d.isoformat() for d in days],
    )


def _fake_execute(key):
    assert key in SUPPORTED
    return _execution()


def _make_idea(root: Path, number: str, slug: str, module: str) -> Path:
    folder = root / "research" / "ideas" / f"idea-{number}-{slug}"
    folder.mkdir(parents=True)
    (folder / "idea.md").write_text(
        IDEA_MD.format(id=f"IDEA-{number}", slug=slug, module=module), encoding="utf-8"
    )
    return folder


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _make_idea(tmp_path, "009", "orb", "backtest/strategies/orb.py")
    _make_idea(tmp_path, "012", "ema9-vwap-cross", "backtest/strategies/ema9_vwap.py")
    _make_idea(tmp_path, "022", "dual-momentum", "backtest/dual_momentum.py")
    return tmp_path


class TestRunIds:
    def test_first_run_is_v1(self, tmp_path):
        assert rr.next_run_id(tmp_path, "2026-09-30", "orb") == "2026-09-30-orb-v1"

    def test_next_number_follows_the_highest_existing_across_dates(self, tmp_path):
        (tmp_path / "2026-09-01-orb-v1.json").write_text("{}")
        (tmp_path / "2026-09-15-orb-v2.json").write_text("{}")
        assert rr.next_run_id(tmp_path, "2026-09-30", "orb") == "2026-09-30-orb-v3"

    def test_other_slugs_and_stray_files_are_ignored(self, tmp_path):
        (tmp_path / "2026-09-01-ema9-v7.json").write_text("{}")
        (tmp_path / "notes.json").write_text("{}")
        (tmp_path / "2026-09-01-orb-vX.json").write_text("{}")
        assert rr.next_run_id(tmp_path, "2026-09-30", "orb") == "2026-09-30-orb-v1"

    def test_missing_directory_is_fine(self, tmp_path):
        assert rr.next_run_id(tmp_path / "nope", "2026-09-30", "orb") == "2026-09-30-orb-v1"


class TestWriteRun:
    def test_writes_strict_json_and_creates_the_directory(self, tmp_path):
        target = rr.write_run(tmp_path / "runs", {"run_id": "r1", "x": 1.5})
        assert target == tmp_path / "runs" / "r1.json"
        assert json.loads(target.read_text()) == {"run_id": "r1", "x": 1.5}

    def test_never_overwrites_and_leaves_the_original_intact(self, tmp_path):
        first = rr.write_run(tmp_path, {"run_id": "r1", "v": 1})
        with pytest.raises(FileExistsError):
            rr.write_run(tmp_path, {"run_id": "r1", "v": 2})
        assert json.loads(first.read_text())["v"] == 1

    def test_no_temp_files_are_left_behind(self, tmp_path):
        rr.write_run(tmp_path, {"run_id": "r1"})
        with pytest.raises(FileExistsError):
            rr.write_run(tmp_path, {"run_id": "r1"})
        assert [p.name for p in tmp_path.iterdir()] == ["r1.json"]

    def test_nan_is_rejected_not_written(self, tmp_path):
        with pytest.raises(ValueError):
            rr.write_run(tmp_path, {"run_id": "r1", "x": float("nan")})
        assert list(tmp_path.iterdir()) == []


class TestOosStart:
    def test_splits_the_last_30_percent_of_the_sessions(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 29)]
        trades = [_trade(date(2026, 1, d), 1, 1) for d in range(1, 29)]
        assert rr.default_oos_start(days, trades) == days[19]

    def test_falls_back_to_trade_days_when_the_session_cut_has_no_trades_after_it(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 29)]
        trades = [_trade(date(2026, 1, d), 1, 1) for d in range(1, 9)]
        cut = rr.default_oos_start(days, trades)
        assert "2026-01-01" < cut <= "2026-01-08"

    def test_too_few_trade_days_means_no_split(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 29)]
        assert rr.default_oos_start(days, [_trade(date(2026, 1, 5), 1, 1)]) is None
        assert rr.default_oos_start(days, []) is None


class TestJsonable:
    def test_dataclasses_tuples_and_odd_types_become_json(self):
        from dataclasses import dataclass

        @dataclass
        class P:
            a: tuple = (1, 2)
            b: date = date(2026, 1, 2)

        out = rr.jsonable({"p": P(), "s": {3}, "n": None})
        assert out == {"p": {"a": [1, 2], "b": "2026-01-02"}, "s": [3], "n": None}
        json.dumps(out, allow_nan=False)


class TestGitVersion:
    def test_outside_a_repo_is_unknown(self, tmp_path):
        assert rr.git_version(tmp_path) == "unknown"

    def test_inside_the_repo_is_a_short_sha_optionally_dirty(self):
        version = rr.git_version(Path(__file__).parent)
        assert version == "unknown" or version.removesuffix("-dirty").isalnum()


class TestStrategyKey:
    def test_key_is_the_module_stem_when_supported(self):
        assert (
            rr.strategy_key({"backtest_module": "backtest/strategies/orb.py"}, SUPPORTED) == "orb"
        )

    @pytest.mark.parametrize(
        "idea", [{"backtest_module": "backtest/dual_momentum.py"}, {}, {"backtest_module": ""}]
    )
    def test_unsupported_or_missing_module_is_none(self, idea):
        assert rr.strategy_key(idea, SUPPORTED) is None


def _regime_inputs():
    import numpy as np

    from signal_engine.backtest.regime_data import RegimeInputs

    idx = pd.bdate_range("2024-01-01", periods=800)
    rng = np.random.default_rng(3)
    close = pd.Series(20000 * np.exp(np.cumsum(rng.normal(0.0004, 0.008, len(idx)))), index=idx)
    vix = pd.Series(15 + rng.normal(0, 1, len(idx)), index=idx)
    return RegimeInputs(close=close, vix=vix, source={"vix_source": "test"})


class TestRegimesInRecord:
    def _record(self, root, **kw):
        return rr.record_idea(
            root,
            "IDEA-009",
            execute=_fake_execute,
            run_date="2026-09-30",
            supported=SUPPORTED,
            **kw,
        )

    def test_no_regimes_unless_a_loader_is_given(self, root):
        rec = json.loads(self._record(root).read_text())
        assert "regimes" not in rec

    def test_loader_adds_a_valid_regimes_block_with_agreement(self, root):
        rec = json.loads(self._record(root, regimes=_regime_inputs).read_text())
        assert br._validate_run(rec) == []
        block = rec["regimes"]
        assert block["source"]["vix_source"] == "test"
        assert set(block["source"]["vol_agreement"]) == {"days", "exact", "opposite"}
        assert 0 <= block["unclassified_trades"] <= rec["metrics"]["trades"]

    def test_loader_failure_still_records_and_says_so(self, root):
        def broken():
            raise RuntimeError("offline")

        rec = json.loads(self._record(root, regimes=broken).read_text())
        assert "regimes" not in rec
        assert "Regime analysis not recorded" in rec["notes"]
        assert "offline" in rec["notes"]


class TestRecordIdea:
    def _record(self, root, idea="IDEA-009", **kw):
        return rr.record_idea(
            root, idea, execute=_fake_execute, run_date="2026-09-30", supported=SUPPORTED, **kw
        )

    def test_writes_a_valid_v2_record_in_the_ideas_runs_folder(self, root):
        path = self._record(root)
        assert path == root / "research/ideas/idea-009-orb/runs/2026-09-30-orb-v1.json"
        rec = json.loads(path.read_text())
        assert br._validate_run(rec) == []
        assert rec["params"]["strategy"] == {"or_minutes": 15}
        assert rec["data"]["universe"] == "historify-3-symbols"
        assert rec["split"]["type"] == "oos"
        assert rec["parent_run_id"] is None

    def test_default_trial_count_is_flagged_as_a_lower_bound(self, root):
        rec = json.loads(self._record(root).read_text())
        assert rec["n_trials"] == 1
        assert "lower bound" in rec["notes"]

    def test_explicit_trials_are_recorded_and_not_called_a_lower_bound(self, root):
        rec = json.loads(self._record(root, trials=12).read_text())
        assert rec["n_trials"] == 12
        assert "lower bound" not in rec["notes"]

    def test_a_second_run_gets_v2_and_keeps_the_first(self, root):
        first = self._record(root)
        second = self._record(root, parent="2026-09-30-orb-v1", note="tighter stop")
        assert second.name == "2026-09-30-orb-v2.json"
        assert first.exists()
        rec = json.loads(second.read_text())
        assert rec["parent_run_id"] == "2026-09-30-orb-v1"
        assert rec["change_note"] == "tighter stop"

    def test_unknown_parent_is_rejected_and_nothing_is_written(self, root):
        with pytest.raises(ValueError, match="parent"):
            self._record(root, parent="no-such-run")
        assert not (root / "research/ideas/idea-009-orb/runs").exists()

    def test_unknown_idea_and_unsupported_idea_raise(self, root):
        with pytest.raises(KeyError):
            self._record(root, idea="IDEA-999")
        with pytest.raises(rr.Unsupported, match="dual_momentum"):
            self._record(root, idea="IDEA-022")

    def test_explicit_oos_start_and_validation_notes_are_used(self, root):
        rec = json.loads(
            self._record(root, oos_start="2026-02-05", validation=["suite passed"]).read_text()
        )
        assert rec["split"]["oos_start"] == "2026-02-05"
        assert rec["validation"] == ["suite passed"]

    def test_registry_loads_the_written_history(self, root):
        self._record(root)
        self._record(root, parent="2026-09-30-orb-v1")
        runs = br._load_runs(root / "research/ideas/idea-009-orb")[0]
        assert [r["run_id"] for r in runs] == ["2026-09-30-orb-v1", "2026-09-30-orb-v2"]


class TestMain:
    def _main(self, root, *argv, execute=_fake_execute):
        return rr.main(["--root", str(root), *argv], execute=execute, supported=SUPPORTED)

    def test_single_idea_run_returns_zero(self, root):
        assert self._main(root, "--idea", "IDEA-009") == 0
        assert (root / "research/ideas/idea-009-orb/runs/2026-09-30-orb-v1.json").exists() or any(
            (root / "research/ideas/idea-009-orb/runs").glob("*-orb-v1.json")
        )

    def test_unknown_or_unsupported_explicit_idea_is_an_error(self, root):
        assert self._main(root, "--idea", "IDEA-999") == 1
        assert self._main(root, "--idea", "IDEA-022") == 1

    def test_all_runs_supported_ideas_and_skips_the_rest(self, root):
        assert self._main(root, "--all") == 0
        assert any((root / "research/ideas/idea-009-orb/runs").glob("*.json"))
        assert any((root / "research/ideas/idea-012-ema9-vwap-cross/runs").glob("*.json"))
        assert not (root / "research/ideas/idea-022-dual-momentum/runs").exists()

    def test_one_failing_idea_does_not_stop_the_others_but_fails_the_exit_code(self, root):
        def flaky(key):
            if key == "orb":
                raise RuntimeError("no data")
            return _execution()

        assert self._main(root, "--all", execute=flaky) == 1
        assert not (root / "research/ideas/idea-009-orb/runs").exists()
        assert any((root / "research/ideas/idea-012-ema9-vwap-cross/runs").glob("*.json"))

    def test_requires_idea_or_all(self, root):
        with pytest.raises(SystemExit):
            self._main(root)


class TestPositionCaps:
    """Every run records what a capped account would have kept, not just the raw edge."""

    def _record(self, root, **kw):
        return rr.record_idea(
            root,
            "IDEA-009",
            execute=_fake_execute,
            run_date="2026-09-30",
            supported=SUPPORTED,
            **kw,
        )

    def test_default_caps_are_three_five_ten(self, root):
        rec = json.loads(self._record(root).read_text())
        assert [c["max_positions"] for c in rec["position_caps"]] == [3, 5, 10]
        assert "peak_concurrent" in rec["metrics"]
        assert br._validate_run(rec) == []

    def test_caps_can_be_switched_off(self, root):
        rec = json.loads(self._record(root, position_caps=()).read_text())
        assert "position_caps" not in rec

    def test_custom_caps_are_used(self, root):
        rec = json.loads(self._record(root, position_caps=(2, 4)).read_text())
        assert [c["max_positions"] for c in rec["position_caps"]] == [2, 4]


class TestParseCaps:
    def test_comma_list(self):
        assert rr.parse_caps("3, 5,10") == (3, 5, 10)

    @pytest.mark.parametrize("off", ["none", "NONE", "", "off"])
    def test_none_switches_caps_off(self, off):
        assert rr.parse_caps(off) == ()

    @pytest.mark.parametrize("bad", ["x", "3,y", "0", "-2", "2.5"])
    def test_bad_values_are_rejected(self, bad):
        with pytest.raises(ValueError, match="caps"):
            rr.parse_caps(bad)


class TestMainCaps:
    def _main(self, root, *argv):
        return rr.main(["--root", str(root), *argv], execute=_fake_execute, supported=SUPPORTED)

    def _rec(self, root):
        path = next((root / "research/ideas/idea-009-orb/runs").glob("*.json"))
        return json.loads(path.read_text())

    def test_caps_option_is_passed_through(self, root):
        assert self._main(root, "--idea", "IDEA-009", "--caps", "2,4") == 0
        assert [c["max_positions"] for c in self._rec(root)["position_caps"]] == [2, 4]

    def test_caps_none_skips(self, root):
        assert self._main(root, "--idea", "IDEA-009", "--caps", "none") == 0
        assert "position_caps" not in self._rec(root)

    def test_bad_caps_exit_before_running_anything(self, root):
        with pytest.raises(SystemExit):
            self._main(root, "--idea", "IDEA-009", "--caps", "x")
        assert not (root / "research/ideas/idea-009-orb/runs").exists()


class TestBaselineInRecord:
    BASELINE = {
        "method": "same-symbol random entry",
        "gross_bps": 30.0,
        "net_bps": 6.0,
        "excess_net_bps": 20.5,
    }

    def _record(self, root, execution):
        return rr.record_idea(
            root,
            "IDEA-009",
            execute=lambda key: execution,
            run_date="2026-09-30",
            supported=SUPPORTED,
        )

    def test_execution_baseline_lands_in_the_record(self, root):
        from dataclasses import replace

        rec = json.loads(
            self._record(root, replace(_execution(), baseline=self.BASELINE)).read_text()
        )
        assert rec["baseline"] == self.BASELINE
        assert br._validate_run(rec) == []

    def test_no_baseline_means_no_key(self, root):
        rec = json.loads(self._record(root, _execution()).read_text())
        assert "baseline" not in rec
