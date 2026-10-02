"""Freeze: the gate between intake and a run, and the source of the strategy identity."""

from __future__ import annotations

from datetime import datetime

import pytest
import yaml

from signal_engine.research.intake import fingerprints, freeze, ledger, versions
from signal_engine.research.intake.spec import SpecError
from signal_engine.tests.intake_fixtures import AMBIGUOUS_TIMING, make_spec, write_spec
from signal_engine.timeutils import IST

NOW = datetime(2026, 10, 2, 14, 0, tzinfo=IST)
IDEA_MD = "---\nid: IDEA-001\ntitle: T\nstage: backtest\n---\n\nBody.\n"


def _freeze(folder, **kw):
    kw.setdefault("now", NOW)
    return freeze.freeze(folder, **kw)


def _ready(tmp_path, **changes):
    return write_spec(tmp_path, make_spec(**changes))


class TestGate:
    def test_ready_idea_freezes(self, tmp_path):
        frozen = _freeze(_ready(tmp_path))
        assert frozen.intent["style"] == "intraday"
        assert frozen.profile_name == "flattrade-nse-cash"
        assert len(frozen.profile_sha256) == 64

    def test_missing_spec_is_a_spec_error(self, tmp_path):
        with pytest.raises(SpecError, match="spec.yaml is required"):
            _freeze(tmp_path)

    def test_incomplete_spec_is_a_spec_error(self, tmp_path):
        with pytest.raises(SpecError, match="entry"):
            _freeze(_ready(tmp_path, entry=["Buy when momentum is strong"]))

    def test_assumption_review_is_refused_naming_the_entries(self, tmp_path):
        folder = _ready(tmp_path, portfolio={"mode": "single_position"})
        with pytest.raises(freeze.FreezeError, match="ASSUMPTION_REVIEW") as err:
            _freeze(folder)
        assert "portfolio.scope" in str(err.value)

    def test_confirmed_assumption_freezes(self, tmp_path):
        folder = _ready(tmp_path, portfolio={"mode": "single_position"})
        led = ledger.resolve(make_spec(portfolio={"mode": "single_position"}))
        ledger.save(folder, ledger.confirm(led, [led.get("portfolio.scope").id]))
        frozen = _freeze(folder)
        assert frozen.intent["portfolio"]["scope"] == "per_symbol"

    def test_stale_confirmation_is_dropped_when_the_inference_changes(self, tmp_path):
        folder = _ready(tmp_path, portfolio={"mode": "single_position"})
        led = ledger.resolve(make_spec(portfolio={"mode": "single_position"}))
        ledger.save(folder, ledger.confirm(led, [led.get("portfolio.scope").id]))
        write_spec(
            folder,
            make_spec(
                portfolio={"mode": "single_position"},
                filters=["Only one position at a time across the universe"],
            ),
        )
        with pytest.raises(freeze.FreezeError, match="portfolio.scope"):
            _freeze(folder)

    def test_blocked_idea_is_refused(self, tmp_path):
        folder = _ready(tmp_path)
        ledger.save(folder, ledger.Ledger(entries=(), blocked_reason="source withdrawn"))
        with pytest.raises(freeze.FreezeError, match="BACKTEST_BLOCKED"):
            _freeze(folder)

    def test_nothing_is_written_when_refused(self, tmp_path):
        folder = _ready(tmp_path, portfolio={"mode": "single_position"})
        with pytest.raises(freeze.FreezeError):
            _freeze(folder)
        assert not (folder / "versions.yaml").exists()
        assert not (folder / "assumptions.yaml").exists()


class TestSealedLedger:
    def test_ledger_carries_confirmations(self, tmp_path):
        folder = _ready(tmp_path, **AMBIGUOUS_TIMING)
        led = ledger.resolve(make_spec(**AMBIGUOUS_TIMING))
        high = [e.id for e in led.entries if e.materiality == "high"]
        ledger.save(folder, ledger.confirm(led, high))
        frozen = _freeze(folder)
        assert all(e.user_confirmed for e in frozen.ledger if e.materiality == "high")

    def test_unconfirmed_medium_entries_are_surfaced(self, tmp_path):
        frozen = _freeze(_ready(tmp_path))
        fields = {e.field for e in frozen.unconfirmed_medium}
        assert {"execution.slippage_bps_per_side", "timing.signal_evaluation"} <= fields
        assert all(e.source != "user" for e in frozen.unconfirmed_medium)

    def test_override_with_a_reason_is_user_sourced_in_the_sealed_ledger(self, tmp_path):
        folder = _ready(tmp_path)
        (folder / "execution.yaml").write_text(
            yaml.safe_dump({"slippage_bps_per_side": {"value": 8, "reason": "thin names"}}),
            encoding="utf-8",
        )
        frozen = _freeze(folder)
        assert frozen.resolved["execution.slippage_bps_per_side"]["value"] == 8
        assert "execution.slippage_bps_per_side" not in {e.field for e in frozen.unconfirmed_medium}


class TestStructuralInvariant:
    def test_every_resolved_leaf_maps_to_exactly_one_ledger_entry(self, tmp_path):
        frozen = _freeze(_ready(tmp_path))
        by_field = {e.field: e for e in frozen.ledger}
        assert frozen.resolved
        for name, leaf in frozen.resolved.items():
            entry = by_field[name]
            assert leaf["ledger_id"] == entry.id
            assert leaf["value"] == entry.value
        ids = [leaf["ledger_id"] for leaf in frozen.resolved.values()]
        assert len(ids) == len(set(ids))

    def test_resolved_covers_every_execution_and_profile_entry(self, tmp_path):
        frozen = _freeze(_ready(tmp_path))
        expected = {e.field for e in frozen.ledger if e.section in ("execution", "profile")}
        assert set(frozen.resolved) == expected

    def test_a_field_without_a_ledger_entry_makes_the_build_raise(self):
        led = ledger.resolve(make_spec())
        with pytest.raises(freeze.FreezeError, match="execution.invented"):
            freeze.build_resolved(led, ["execution.bar_size", "execution.invented"])

    def test_build_resolved_reads_values_from_the_ledger(self):
        led = ledger.resolve(make_spec())
        out = freeze.build_resolved(led, ["execution.bar_size"])
        assert out == {
            "execution.bar_size": {
                "value": "5m",
                "ledger_id": led.get("execution.bar_size").id,
            }
        }


class TestFingerprints:
    def test_matches_the_fingerprint_module_on_the_effective_intent(self, tmp_path):
        frozen = _freeze(_ready(tmp_path))
        assert frozen.rules_fingerprint == fingerprints.rules_fingerprint(frozen.intent)
        assert frozen.params_fingerprint == fingerprints.params_fingerprint(
            frozen.intent["parameters"]
        )
        assert frozen.strategy_fingerprint == fingerprints.strategy_fingerprint(
            frozen.rules_fingerprint, frozen.params_fingerprint
        )

    def test_stable_under_yaml_formatting_and_key_order(self, tmp_path):
        a = _freeze(_ready(tmp_path / "a"))
        spec = make_spec()
        reordered = {k: spec[k] for k in reversed(list(spec))}
        folder = tmp_path / "b"
        folder.mkdir()
        (folder / "spec.yaml").write_text(
            yaml.safe_dump(reordered, sort_keys=False, default_flow_style=True), encoding="utf-8"
        )
        assert _freeze(folder).strategy_fingerprint == a.strategy_fingerprint

    def test_any_intent_change_changes_it(self, tmp_path):
        a = _freeze(_ready(tmp_path / "a"))
        b = _freeze(_ready(tmp_path / "b", exit=["Close below VWAP", "Maximum hold 20 bars"]))
        assert a.strategy_fingerprint != b.strategy_fingerprint

    def test_profile_switch_does_not_change_it(self, tmp_path):
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        src = yaml.safe_load(
            (
                __import__("pathlib").Path(__file__).resolve().parents[1]
                / "research"
                / "profiles"
                / "flattrade-nse-cash.yaml"
            ).read_text(encoding="utf-8")
        )
        src["name"] = "other-broker"
        src["products"]["MIS"]["force_flat"] = "15:10"
        (profiles_dir / "other-broker.yaml").write_text(yaml.safe_dump(src), encoding="utf-8")
        a = _freeze(_ready(tmp_path / "a"), profiles_dir=None)
        b = _freeze(_ready(tmp_path / "b", profile="other-broker"), profiles_dir=profiles_dir)
        assert a.strategy_fingerprint == b.strategy_fingerprint
        assert a.profile_sha256 != b.profile_sha256

    def test_stated_product_equal_to_the_derived_one_changes_nothing(self, tmp_path):
        a = _freeze(_ready(tmp_path / "a"))
        b = _freeze(_ready(tmp_path / "b", product=None))
        assert a.strategy_fingerprint == b.strategy_fingerprint

    def test_renaming_does_not_change_it(self, tmp_path):
        a = _freeze(_ready(tmp_path / "a"))
        b = _freeze(_ready(tmp_path / "b", name="A better name"))
        assert a.strategy_fingerprint == b.strategy_fingerprint

    def test_a_parameter_edit_moves_params_but_not_rules(self, tmp_path):
        a = _freeze(_ready(tmp_path / "a"))
        b = _freeze(_ready(tmp_path / "b", parameters={"ema_fast": 12}))
        assert a.rules_fingerprint == b.rules_fingerprint
        assert a.params_fingerprint != b.params_fingerprint
        assert a.strategy_fingerprint != b.strategy_fingerprint

    def test_an_inferred_timing_change_changes_the_rules_fingerprint(self, tmp_path):
        a = _freeze(_ready(tmp_path / "a"))
        b = _freeze(
            _ready(tmp_path / "b", timing={"signal_evaluation": "bar_open"}),
        )
        assert a.rules_fingerprint != b.rules_fingerprint


class TestVersionRegistration:
    def test_first_freeze_creates_v1(self, tmp_path):
        frozen = _freeze(_ready(tmp_path))
        assert (frozen.version, frozen.version_created, frozen.parent_version) == ("v1", True, None)
        assert (
            versions.load_versions(tmp_path)[0].strategy_fingerprint == frozen.strategy_fingerprint
        )

    def test_refreezing_the_same_strategy_reuses_the_version(self, tmp_path):
        _freeze(_ready(tmp_path))
        again = _freeze(tmp_path)
        assert (again.version, again.version_created) == ("v1", False)
        assert len(versions.load_versions(tmp_path)) == 1

    def test_a_changed_rule_creates_the_next_version_with_current_as_parent(self, tmp_path):
        (tmp_path / "idea.md").write_text(IDEA_MD, encoding="utf-8")
        _freeze(_ready(tmp_path))
        versions.set_pointer(tmp_path, "current_version", "v1")
        write_spec(tmp_path, make_spec(exit=["Close below VWAP", "Maximum hold 20 bars"]))
        second = _freeze(tmp_path)
        assert (second.version, second.parent_version) == ("v2", "v1")

    def test_parent_and_label_overrides(self, tmp_path):
        _freeze(_ready(tmp_path))
        write_spec(tmp_path, make_spec(exit=["Close below VWAP", "Maximum hold 20 bars"]))
        second = _freeze(tmp_path, parent_version="v1", version_label="v1b", note="tighter exit")
        assert (second.version, second.parent_version) == ("v1b", "v1")
        assert versions.load_versions(tmp_path)[-1].note == "tighter exit"

    def test_register_version_false_writes_nothing(self, tmp_path):
        frozen = _freeze(_ready(tmp_path), register_version=False)
        assert frozen.version is None
        assert not (tmp_path / "versions.yaml").exists()

    def test_a_parameter_only_edit_is_a_new_version(self, tmp_path):
        _freeze(_ready(tmp_path))
        write_spec(tmp_path, make_spec(parameters={"ema_fast": 12}))
        assert _freeze(tmp_path).version == "v2"


class TestDeferredRegistration:
    def test_register_after_the_run_creates_the_version(self, tmp_path):
        frozen = _freeze(_ready(tmp_path), register_version=False)
        assert not (tmp_path / "versions.yaml").exists()
        done = freeze.register(tmp_path, frozen, now=NOW)
        assert (done.version, done.version_created) == ("v1", True)
        assert done.strategy_fingerprint == frozen.strategy_fingerprint
        assert versions.load_versions(tmp_path)[0].version == "v1"

    def test_dry_register_checks_without_writing(self, tmp_path):
        frozen = _freeze(_ready(tmp_path), register_version=False)
        with pytest.raises(versions.VersionError, match="label"):
            freeze.register(tmp_path, frozen, label="nope", dry_run=True)
        assert not (tmp_path / "versions.yaml").exists()


# ---- from_run_folder -----------------------------------------------------------------------
# A frozen spec rebuilt from a run folder alone (rerun and reproduce start here).

import json as _json  # noqa: E402
import os as _os  # noqa: E402

from signal_engine.backtest import manifest as _mf  # noqa: E402
from signal_engine.tests import test_run_artifact_write as _raw  # noqa: E402

_stub_provenance = _raw._stub_provenance
root = _raw.root


def _run_folder(root_):
    path = _raw._record(root_, execute=lambda key: _raw._execution())
    return _raw._folder(path)


def _rewrite(path, text):
    _os.chmod(path, 0o644)
    path.write_text(text, encoding="utf-8")


def _fix_hash(folder, rel):
    """Make the manifest agree with an edited artifact so only the fingerprint check can object."""
    import hashlib

    manifest_path = folder / "manifest.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][rel] = hashlib.sha256((folder / rel).read_bytes()).hexdigest()
    _rewrite(manifest_path, _json.dumps(manifest))


class TestFromRunFolder:
    def test_round_trip_equals_the_original_frozen_spec(self, root):
        idea = root / "research" / "ideas" / "idea-009-orb"
        original = freeze.freeze(idea, idea_id="IDEA-009", register_version=False)
        folder = _run_folder(root)
        rebuilt = freeze.from_run_folder(folder)
        assert rebuilt.intent == original.intent
        assert rebuilt.resolved == original.resolved
        assert rebuilt.profile == original.profile
        assert rebuilt.profile_name == original.profile_name
        assert rebuilt.profile_sha256 == original.profile_sha256
        assert rebuilt.ledger == original.ledger
        assert rebuilt.unconfirmed_medium == original.unconfirmed_medium
        assert rebuilt.rules_fingerprint == original.rules_fingerprint
        assert rebuilt.params_fingerprint == original.params_fingerprint
        assert rebuilt.strategy_fingerprint == original.strategy_fingerprint
        assert rebuilt.version == "v1"
        assert rebuilt.parent_version is None
        assert rebuilt.version_created is False

    def test_ignores_the_ideas_current_spec(self, root):
        folder = _run_folder(root)
        before = freeze.from_run_folder(folder)
        idea = root / "research" / "ideas" / "idea-009-orb"
        edited = make_spec(parameters={"ema_fast": 5, "ema_slow": 9, "atr_len": 3})
        (idea / "spec.yaml").write_text(yaml.safe_dump(edited), encoding="utf-8")
        after = freeze.from_run_folder(folder)
        assert after == before
        assert after.intent["parameters"] != edited["parameters"]

    def test_writes_nothing(self, root):
        folder = _run_folder(root)
        listing = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        freeze.from_run_folder(folder)
        assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == listing

    def test_tampered_spec_is_refused(self, root):
        folder = _run_folder(root)
        _rewrite(folder / "strategy" / "spec.yaml", "style: swing\n")
        with pytest.raises(freeze.FreezeError, match="spec.yaml"):
            freeze.from_run_folder(folder)

    def test_tampered_config_is_refused(self, root):
        folder = _run_folder(root)
        _rewrite(folder / "execution" / "config.json", "{}")
        with pytest.raises(freeze.FreezeError, match="config.json"):
            freeze.from_run_folder(folder)

    def test_tampered_manifest_fingerprint_is_refused(self, root):
        folder = _run_folder(root)
        manifest_path = folder / "manifest.json"
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["fingerprints"]["rules"] = "0" * 64
        _rewrite(manifest_path, _json.dumps(manifest))
        with pytest.raises(freeze.FreezeError, match="rules fingerprint"):
            freeze.from_run_folder(folder)

    def test_non_canonical_spec_with_matching_hash_is_refused_by_fingerprint(self, root):
        folder = _run_folder(root)
        intent = yaml.safe_load((folder / "strategy" / "spec.yaml").read_text(encoding="utf-8"))
        intent["parameters"] = {"ema_fast": 99}
        _rewrite(folder / "strategy" / "spec.yaml", yaml.safe_dump(intent, sort_keys=True))
        _fix_hash(folder, "strategy/spec.yaml")
        assert _mf.verify_artifacts(folder) == []
        with pytest.raises(freeze.FreezeError, match="params fingerprint"):
            freeze.from_run_folder(folder)

    def test_missing_manifest_is_refused(self, root):
        folder = _run_folder(root)
        _os.chmod(folder, 0o755)
        _os.chmod(folder / "manifest.json", 0o644)
        (folder / "manifest.json").unlink()
        with pytest.raises(freeze.FreezeError):
            freeze.from_run_folder(folder)
