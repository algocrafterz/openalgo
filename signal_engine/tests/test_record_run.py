"""record_run: run an idea's backtest and store the result as an immutable run record.

The contract pinned here is what makes a history of runs trustworthy: a run file is never
overwritten, ids never collide, the lineage points at a run that exists, the record
validates against the registry schema, and one failing idea cannot stop the others.
"""

import json
from datetime import date
from types import SimpleNamespace
from pathlib import Path

import pandas as pd
import pytest
import yaml

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br
from signal_engine.tests.cost_fixtures import statutory
from signal_engine.tests.intake_fixtures import GOOD_SPEC

SUPPORTED = {"orb", "ema9_vwap"}

IDEA_MD = """---
id: {id}
title: Sample {slug}
stage: inbox
research_state: rejected
strategy_category: Breakout
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
        **statutory(trades, 16.0),
        params={"strategy": {"or_minutes": 15}, "run": {"cost_bps": 16.0}},
        data={
            "source": "historify",
            "as_of": days[-1].isoformat(),
            "universe": "historify-3-symbols",
            "bar_size": "5m",
        },
        period={"start": days[0].isoformat(), "end": days[-1].isoformat()},
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
    (folder / "spec.yaml").write_text(yaml.safe_dump(GOOD_SPEC), encoding="utf-8")
    return folder


@pytest.fixture(autouse=True)
def _stub_provenance(monkeypatch):
    """Roots here are not git checkouts and have no Historify store: stub both lookups."""
    from signal_engine.backtest import manifest

    monkeypatch.setattr(
        manifest,
        "git_state",
        lambda root: {"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []},
    )
    monkeypatch.setattr(manifest, "dataset_fingerprint", lambda symbols, interval, as_of: "d" * 64)


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

    def test_a_loader_is_never_called_on_a_published_dataset_run(self, root):
        """Owner decision 1 (plan Phase 4): NIFTY/VIX are not in any published dataset, so the
        regime block is unavailable; no loader (live store or Yahoo) is ever called."""
        called = []
        rec = json.loads(self._record(root, regimes=lambda: called.append(1)
                                      or _regime_inputs()).read_text())
        assert called == [] and "regimes" not in rec
        assert br._validate_run(rec) == []
        assert "Regime analysis not available" in rec["notes"]

    def test_a_broken_loader_cannot_affect_the_run(self, root):
        def broken():
            raise RuntimeError("offline")

        rec = json.loads(self._record(root, regimes=broken).read_text())
        assert "regimes" not in rec
        assert "Regime analysis not available" in rec["notes"]
        assert "offline" not in rec["notes"]


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

    def test_trial_count_comes_from_the_ledger_and_the_note_says_so(self, root):
        rec = json.loads(self._record(root).read_text())
        assert rec["n_trials"] == 1
        assert "trials.yaml" in rec["notes"]
        assert "lower bound" not in rec["notes"] and "defaulted" not in rec["notes"]

    def test_external_trials_are_added_to_the_ledger_count(self, root):
        rec = json.loads(self._record(root, external_trials=11, external_reason="grid").read_text())
        assert rec["n_trials"] == 12
        assert rec["trials"] == {
            "material": 1,
            "external": 11,
            "total": 12,
            "source": "trials.yaml",
        }
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


def _declare_reference_lines(root: Path, levels: list) -> None:
    path = root / "research" / "ideas" / "idea-009-orb" / "spec.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["costs"] = {"reference_round_trip_bps": levels}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _record_with_settings(root: Path, seen: dict) -> Path:
    def execute(key, settings=None):
        seen.update(settings or {})
        trades = _execution().trades
        return rr.Execution(
            **{
                **_execution().__dict__,
                **statutory(trades, 16.0, reference_bps=(settings or {}).get(
                    "reference_round_trip_bps", ())),
            }
        )

    return rr.record_idea(
        root, "IDEA-009", execute=execute, run_date="2026-09-30", supported=SUPPORTED
    )


class TestStatutoryCostBasisRecord:
    def test_record_reports_32_bps_scenario_when_spec_declares_it(self, root):
        _declare_reference_lines(root, [32])
        seen: dict = {}
        record = json.loads(_record_with_settings(root, seen).read_text(encoding="utf-8"))
        assert seen["reference_round_trip_bps"] == [32]
        ids = [s["id"] for s in record["cost_model"]["scenarios"]]
        assert ids == ["primary", "generic_discount_broker", "flat_32bps", "legacy_flat"]
        assert record["costs"]["reference_round_trip_bps"] == [32.0]
        assert record["costs"]["model"] == "statutory-per-trade"
        assert br._validate_run(record) == []

    def test_no_reference_scenarios_without_declaration(self, root):
        seen: dict = {}
        record = json.loads(_record_with_settings(root, seen).read_text(encoding="utf-8"))
        assert "reference_round_trip_bps" not in seen
        ids = [s["id"] for s in record["cost_model"]["scenarios"]]
        assert ids == ["primary", "generic_discount_broker", "legacy_flat"]
        assert record["costs"]["reference_round_trip_bps"] == []

    def test_changing_reference_lines_does_not_add_a_trial(self, root):
        from signal_engine.research.intake import trials as trials_mod

        folder = root / "research" / "ideas" / "idea-009-orb"
        _declare_reference_lines(root, [32])
        _record_with_settings(root, {})
        _declare_reference_lines(root, [48, 64])
        _record_with_settings(root, {})
        assert trials_mod.count(folder).material == 1

    def test_record_refuses_run_without_statutory_model(self, root):
        ideas = root / "research" / "ideas" / "idea-009-orb"

        def snapshot():
            return sorted(p.relative_to(ideas).as_posix() for p in ideas.rglob("*"))

        before = snapshot()

        def execute(key, settings=None):
            raise rr.CostModelUnavailable("per-trade cost model unavailable: no config")

        with pytest.raises(rr.CostModelUnavailable):
            rr.record_idea(
                root, "IDEA-009", execute=execute, run_date="2026-09-30", supported=SUPPORTED
            )
        assert snapshot() == before
        assert not (ideas / "trials.yaml").exists() and not (ideas / "runs").exists()


class TestPrimaryCostModel:
    def test_the_profile_broker_prices_the_model(self):
        model = rr._primary_cost_model("intraday", {"broker": "flattrade", "product": "MIS"})
        assert model.broker == "flattrade" and model.product == "MIS"

    def test_a_missing_config_key_is_a_cost_model_fault_not_a_fallback(self, tmp_path, monkeypatch):
        bad = tmp_path / "config.yaml"
        bad.write_text("sizing: {sandbox_capital: 100000}\n", encoding="utf-8")
        monkeypatch.setattr(rr, "CONFIG_PATH", bad)
        with pytest.raises(rr.CostModelUnavailable, match="risk_per_trade|broker"):
            rr._primary_cost_model("intraday", {})

    def test_product_must_match_the_frozen_product(self):
        with pytest.raises(rr.CostModelUnavailable, match="execution.product"):
            rr._primary_cost_model("intraday", {"product": "CNC"})

    def test_unknown_exchange_rejected(self, tmp_path, monkeypatch):
        config = yaml.safe_load(rr.CONFIG_PATH.read_text(encoding="utf-8"))
        config["broker"]["exchange"] = "MCX"
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        monkeypatch.setattr(rr, "CONFIG_PATH", path)
        with pytest.raises(rr.CostModelUnavailable, match="exchange"):
            rr._primary_cost_model("swing", {})

    def test_no_stop_without_fixed_notional_fails(self):
        with pytest.raises(rr.CostModelUnavailable, match="fixed notional"):
            rr._primary_cost_model("swing", {"stop": "none"})
        model = rr._primary_cost_model(
            "swing", {"stop": "none", "capital_per_position": 100000}
        )
        assert model.fixed_notional == 100000.0


class TestCrossSectionalBlockInRecord:
    BLOCK = {
        "method": "engine replay",
        "sessions": 40,
        "sessions_population": 40,
        "sampled": False,
        "trades": 40,
        "replays": 800,
        "replays_filled": 790,
        "replay_skips": {"STOP_TOO_TIGHT": 10},
        "strategy_net_bps": 12.0,
        "null_net_bps": 8.0,
        "excess_net_bps": 4.0,
        "excess_t_stat": 1.2,
    }

    def _record(self, root, **changes):
        from dataclasses import replace

        execution = replace(_execution(), **changes)
        path = rr.record_idea(
            root,
            "IDEA-009",
            execute=lambda key: execution,
            run_date="2026-09-30",
            supported=SUPPORTED,
        )
        return json.loads(path.read_text())

    def test_block_lands_in_the_record_and_validates(self, root):
        rec = self._record(root, baseline_cross_sectional=self.BLOCK)
        assert rec["baseline_cross_sectional"] == self.BLOCK
        assert br._validate_run(rec) == []

    def test_no_hook_no_block(self, root):
        assert "baseline_cross_sectional" not in self._record(root)

    def test_old_record_without_block_loads(self, root):
        rec = self._record(root)
        assert br._validate_run(rec) == []

    def test_block_does_not_change_fingerprints_or_evaluation_rows(self, tmp_path):
        records = []
        for name, changes in (("a", {}), ("b", {"baseline_cross_sectional": self.BLOCK})):
            folder = tmp_path / name
            _make_idea(folder, "009", "orb", "backtest/strategies/orb.py")
            records.append(self._record(folder, **changes))
        plain, with_block = records
        assert with_block["baseline_cross_sectional"] == self.BLOCK
        # Only the block (and the manifest hash that covers the artifact files) may differ.
        drop = {"baseline_cross_sectional", "manifest_sha256"}
        assert {k: v for k, v in plain.items() if k not in drop} == {
            k: v for k, v in with_block.items() if k not in drop
        }

    def test_registry_rejects_malformed_block(self, root):
        rec = self._record(root, baseline_cross_sectional=self.BLOCK)
        for bad in (
            "text",
            {**self.BLOCK, "excess_net_bps": "4"},
            {**self.BLOCK, "replay_skips": [1]},
            {k: v for k, v in self.BLOCK.items() if k != "null_net_bps"},
            {**self.BLOCK, "excess_t_stat": True},
        ):
            problems = br._validate_run({**rec, "baseline_cross_sectional": bad})
            assert any("baseline_cross_sectional" in p for p in problems), bad


class TestPerturbationInRecord:
    EVIDENCE = {
        "version": 1,
        "state": "complete",
        "reason": "",
        "base": {"trades": 500, "net_r": 0.5, "net_bps": 20.0},
        "neighbours": [
            {"parameter": f"p{i}", "multiplier": 0.8, "value": i, "base_value": 9, "trades": 100,
             "net_r": 0.4, "net_bps": 15.0, "params_fingerprint": "x"}
            for i in range(6)
        ],
        "diagnostic_not_trials": True,
    }  # fmt: skip

    def _record(self, root, **changes):
        from dataclasses import replace

        execution = replace(_execution(), **changes)
        path = rr.record_idea(
            root,
            "IDEA-009",
            execute=lambda key: execution,
            run_date="2026-09-30",
            supported=SUPPORTED,
        )
        return path, json.loads(path.read_text())

    @staticmethod
    def _row(record):
        return next(
            r
            for d in record["evaluation"]["dimensions"]
            for r in d["rows"]
            if r["gate"] == "G4.1"
        )

    def test_evidence_is_sealed_in_the_run_folder_and_scored(self, root):
        path, rec = self._record(root, perturbation=self.EVIDENCE)
        folder = path.parent / rec["artifact_dir"].rstrip("/")
        assert json.loads((folder / "evidence" / "perturbation.json").read_text()) == self.EVIDENCE
        manifest = json.loads((folder / "manifest.json").read_text())
        assert "evidence/perturbation.json" in manifest["artifacts"]
        assert self._row(rec)["status"] == "pass"

    def test_a_run_without_the_stage_has_no_file_and_the_gate_is_not_evaluated(self, root):
        path, rec = self._record(root)
        folder = path.parent / rec["artifact_dir"].rstrip("/")
        assert not (folder / "evidence" / "perturbation.json").exists()
        assert self._row(rec)["status"] == "not_evaluated"

    def test_the_stage_adds_no_trial(self, root):
        _, plain = self._record(root)
        assert plain["n_trials"] == 1

    def test_an_unsupported_strategy_kind_records_a_not_run_state(self):
        ev = rr._perturbation_stage(
            supported=False, meta=None, trades=[object()] * 3, days=[], strategy=None
        )
        assert ev["state"] == "unsupported" and ev["base"]["trades"] == 3


class TestLiveCapsInRecord:
    @staticmethod
    def _evidence(net_r=0.2, trades=500):
        def row(slots, primary):
            return {"max_open_positions": slots, "primary": primary, "trades": trades,
                    "net_r": net_r, "net_bps": 20.0, "win_rate": 0.5, "peak_open_positions": slots,
                    "vetoes": {"band": 1, "daily": 0, "weekly": 0, "monthly": 0, "slots": 2,
                               "trades_day": 0}, "trade_sha256": "x"}
        return {
            "version": 1, "state": "complete", "diagnostic_not_trials": True,
            "headline": {"trades": 900, "net_r": 0.3, "net_bps": 25.0},
            "base_check": {"trades": 900, "net_r": 0.3, "net_bps": 25.0, "trade_sha256": "y"},
            "passes": [row(1, True), row(2, False), row(3, False)],
            "assumptions": ["a"], "limitations": ["l"],
        }  # fmt: skip

    def _record(self, root, **changes):
        from dataclasses import replace

        execution = replace(_execution(), **changes)
        path = rr.record_idea(root, "IDEA-009", execute=lambda key: execution,
                              run_date="2026-09-30", supported=SUPPORTED)
        return path, json.loads(path.read_text())

    @staticmethod
    def _row(record):
        return next(r for d in record["evaluation"]["dimensions"] for r in d["rows"] if r["gate"] == "G5.2")

    def test_evidence_is_sealed_in_the_run_folder_and_scored(self, root):
        ev = self._evidence()
        path, rec = self._record(root, live_caps=ev)
        folder = path.parent / rec["artifact_dir"].rstrip("/")
        assert json.loads((folder / "evidence" / "live_caps.json").read_text()) == ev
        manifest = json.loads((folder / "manifest.json").read_text())
        assert "evidence/live_caps.json" in manifest["artifacts"]
        row = self._row(rec)
        assert row["status"] == "pass" and row["value"] == 0.2

    def test_a_capped_book_below_the_expectancy_bar_fails_the_gate(self, root):
        _, rec = self._record(root, live_caps=self._evidence(net_r=0.01))
        assert self._row(rec)["status"] == "fail"

    def test_a_run_without_the_stage_has_no_file_and_the_gate_is_not_evaluated(self, root):
        path, rec = self._record(root)
        folder = path.parent / rec["artifact_dir"].rstrip("/")
        assert not (folder / "evidence" / "live_caps.json").exists()
        assert self._row(rec)["status"] == "not_evaluated"

    def test_the_stage_adds_no_trial(self, root):
        _, rec = self._record(root, live_caps=self._evidence())
        assert rec["n_trials"] == 1

    def test_an_unsupported_strategy_kind_records_a_not_run_state(self):
        ev = rr._live_caps_stage(supported=False, trades=[], days=[])
        assert ev["state"] == "unsupported" and ev["diagnostic_not_trials"] is True

    def test_an_execute_callable_without_the_option_is_refused_not_ignored(self):
        with pytest.raises(ValueError, match="live-caps"):
            rr._execute_frozen(lambda key: None, "k", SimpleNamespace(resolved={}, intent={}, profile={}),
                               run_live_caps=True)


class TestMonteCarloInRecord:
    def test_the_recorded_run_carries_the_evidence_file_and_scores_the_gate(self, root):
        from dataclasses import replace

        execution = _execution()
        execution = replace(execution, monte_carlo=rr._monte_carlo_stage(execution))
        path = rr.record_idea(root, "IDEA-009", execute=lambda key: execution,
                              run_date="2026-09-30", supported=SUPPORTED)
        record = json.loads(path.read_text())
        folder = path.parent / record["artifact_dir"].rstrip("/")
        assert (folder / "evidence" / "monte_carlo.json").exists()
        manifest = json.loads((folder / "manifest.json").read_text())
        assert "evidence/monte_carlo.json" in manifest["artifacts"]
        row = next(r for d in record["evaluation"]["dimensions"] for r in d["rows"] if r["gate"] == "G5.1")
        assert row["status"] in ("pass", "fail")  # evaluated, bound to this run's trades

    def test_a_run_without_the_stage_leaves_it_not_evaluated_and_writes_no_file(self, root):
        path = rr.record_idea(root, "IDEA-009", execute=lambda key: _execution(),
                              run_date="2026-09-30", supported=SUPPORTED)
        record = json.loads(path.read_text())
        folder = path.parent / record["artifact_dir"].rstrip("/")
        assert not (folder / "evidence" / "monte_carlo.json").exists()
        row = next(r for d in record["evaluation"]["dimensions"] for r in d["rows"] if r["gate"] == "G5.1")
        assert row["status"] == "not_evaluated"


class TestLatencyInRecord:
    @staticmethod
    def _evidence(execution, late_r=0.05):
        from signal_engine.backtest.live_caps import trade_hash
        from signal_engine.backtest.perturbation import trade_metrics

        base = {**trade_metrics(execution.trades), "trade_sha256": trade_hash(execution.trades)}
        return {"version": 1, "state": "complete", "reason": "", "delay_bars": 1,
                "diagnostic_not_trials": True, "limitations": ["l"], "base": base,
                "shared": {"bar_size": "5m"},
                "delayed": {"trades": len(execution.trades), "net_r": late_r, "net_bps": 1.0}}

    def _row(self, record):
        return next(r for d in record["evaluation"]["dimensions"] for r in d["rows"] if r["gate"] == "G4.2")

    def test_the_evidence_is_sealed_in_the_run_folder_and_scored(self, root):
        from dataclasses import replace

        execution = _execution()
        execution = replace(execution, latency=self._evidence(execution))
        path = rr.record_idea(root, "IDEA-009", execute=lambda key: execution,
                              run_date="2026-09-30", supported=SUPPORTED)
        record = json.loads(path.read_text())
        folder = path.parent / record["artifact_dir"].rstrip("/")
        assert (folder / "evidence" / "latency.json").exists()
        manifest = json.loads((folder / "manifest.json").read_text())
        assert "evidence/latency.json" in manifest["artifacts"]
        assert self._row(record)["status"] == "report"

    def test_a_run_without_the_stage_leaves_it_not_evaluated_and_writes_no_file(self, root):
        path = rr.record_idea(root, "IDEA-009", execute=lambda key: _execution(),
                              run_date="2026-09-30", supported=SUPPORTED)
        record = json.loads(path.read_text())
        folder = path.parent / record["artifact_dir"].rstrip("/")
        assert not (folder / "evidence" / "latency.json").exists()
        assert self._row(record)["status"] == "not_evaluated"

    def test_an_execute_callable_without_the_option_is_refused_not_silently_skipped(self):
        from types import SimpleNamespace

        frozen = SimpleNamespace(resolved={}, intent={}, profile={})
        with pytest.raises(ValueError, match="latency"):
            rr._execute_frozen(lambda key: None, "k", frozen, run_latency=True)
