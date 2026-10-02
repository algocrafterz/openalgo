"""Deployment profiles: declared once, shared, mapped to RunConfig fields."""

from __future__ import annotations

import copy

import pytest
import yaml

from signal_engine.backtest.types import RunConfig
from signal_engine.research.intake import profiles

GOOD = {
    "name": "test-profile",
    "broker": "testbroker",
    "market": "NSE/BSE cash equity",
    "products": {
        "MIS": {
            "force_flat": "15:00",
            "standing_time_exit": "14:45",
            "latest_exit_bar_close": "14:55",
            "exit_convention": "engine_exit_before_force_flat",
        },
        "CNC": {"carry_forward": True},
    },
}


def _write(folder, data, name="test-profile"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return folder


def _load_with(tmp_path, data):
    _write(tmp_path, data)
    return profiles.load_profile("test-profile", tmp_path)


class TestShippedProfile:
    def test_flattrade_nse_cash_loads(self):
        p = profiles.load_profile("flattrade-nse-cash")
        assert p.name == "flattrade-nse-cash"
        assert p.broker == "flattrade"
        assert p.supports("MIS") and p.supports("CNC")

    def test_mis_rules_match_the_checklist(self):
        mis = profiles.load_profile("flattrade-nse-cash").products["MIS"]
        assert mis.force_flat_min == 15 * 60
        assert mis.standing_time_exit_min == 14 * 60 + 45
        assert mis.latest_exit_bar_close_min == 14 * 60 + 55
        assert mis.carry_forward is False
        assert mis.exit_convention

    def test_cnc_carries_forward_with_no_cutoff(self):
        cnc = profiles.load_profile("flattrade-nse-cash").products["CNC"]
        assert cnc.carry_forward is True
        assert cnc.force_flat_min is None
        assert cnc.standing_time_exit_min is None


class TestRunConfigMapping:
    def test_mis_maps_to_runconfig_fields(self):
        p = profiles.load_profile("flattrade-nse-cash")
        assert p.run_config_fields("MIS") == {
            "mis_squareoff_min": 900,
            "squareoff_margin_min": 5,
            "time_exit_min": 885,
        }

    def test_mapped_fields_match_the_engine_defaults(self):
        p = profiles.load_profile("flattrade-nse-cash")
        base = RunConfig()
        applied = base.with_(**p.run_config_fields("MIS"))
        assert applied.mis_squareoff_min == base.mis_squareoff_min
        assert applied.squareoff_margin_min == base.squareoff_margin_min
        assert applied.time_exit_min == base.time_exit_min

    def test_cnc_has_no_intraday_cutoffs_to_map(self):
        assert profiles.load_profile("flattrade-nse-cash").run_config_fields("CNC") == {}

    def test_unsupported_product_raises(self, tmp_path):
        cnc_only = {**GOOD, "products": {"CNC": {"carry_forward": True}}}
        p = _load_with(tmp_path, cnc_only)
        with pytest.raises(profiles.ProfileError, match="MIS"):
            p.run_config_fields("MIS")


class TestLoading:
    def test_unknown_profile_name_is_rejected(self, tmp_path):
        with pytest.raises(profiles.ProfileError, match="no-such"):
            profiles.load_profile("no-such", tmp_path)

    def test_name_must_match_the_file(self, tmp_path):
        _write(tmp_path, {**GOOD, "name": "other"})
        with pytest.raises(profiles.ProfileError, match="name"):
            profiles.load_profile("test-profile", tmp_path)

    def test_unreadable_yaml_is_rejected(self, tmp_path):
        (tmp_path / "broken.yaml").write_text("a: [unclosed", encoding="utf-8")
        with pytest.raises(profiles.ProfileError, match="unreadable"):
            profiles.load_profile("broken", tmp_path)

    def test_valid_profile_loads(self, tmp_path):
        assert _load_with(tmp_path, GOOD).broker == "testbroker"

    @pytest.mark.parametrize("key", ["name", "broker", "market", "products"])
    def test_missing_top_level_key(self, tmp_path, key):
        _write(tmp_path, {k: v for k, v in GOOD.items() if k != key})
        with pytest.raises(profiles.ProfileError, match=key):
            profiles.load_profile("test-profile", tmp_path)

    def test_unknown_top_level_key(self, tmp_path):
        _write(tmp_path, {**GOOD, "entry": "x"})
        with pytest.raises(profiles.ProfileError, match="entry"):
            profiles.load_profile("test-profile", tmp_path)

    def test_unknown_product(self, tmp_path):
        _write(tmp_path, {**GOOD, "products": {"NRML": {"carry_forward": True}}})
        with pytest.raises(profiles.ProfileError, match="NRML"):
            profiles.load_profile("test-profile", tmp_path)

    @pytest.mark.parametrize(
        "missing", ["force_flat", "standing_time_exit", "latest_exit_bar_close", "exit_convention"]
    )
    def test_mis_requires_every_cutoff(self, tmp_path, missing):
        data = copy.deepcopy(GOOD)
        del data["products"]["MIS"][missing]
        _write(tmp_path, data)
        with pytest.raises(profiles.ProfileError, match=missing):
            profiles.load_profile("test-profile", tmp_path)

    def test_mis_cutoffs_must_be_ordered(self, tmp_path):
        data = copy.deepcopy(GOOD)
        data["products"]["MIS"]["standing_time_exit"] = "14:58"
        _write(tmp_path, data)
        with pytest.raises(profiles.ProfileError, match="order"):
            profiles.load_profile("test-profile", tmp_path)

    def test_force_flat_must_not_precede_latest_exit_bar(self, tmp_path):
        data = copy.deepcopy(GOOD)
        data["products"]["MIS"]["force_flat"] = "14:50"
        _write(tmp_path, data)
        with pytest.raises(profiles.ProfileError, match="order"):
            profiles.load_profile("test-profile", tmp_path)

    @pytest.mark.parametrize("value", ["3pm", "25:00", "15:60", 1500])
    def test_bad_time_format(self, tmp_path, value):
        data = copy.deepcopy(GOOD)
        data["products"]["MIS"]["force_flat"] = value
        _write(tmp_path, data)
        with pytest.raises(profiles.ProfileError, match="force_flat"):
            profiles.load_profile("test-profile", tmp_path)

    def test_mis_cannot_carry_forward(self, tmp_path):
        data = copy.deepcopy(GOOD)
        data["products"]["MIS"]["carry_forward"] = True
        _write(tmp_path, data)
        with pytest.raises(profiles.ProfileError, match="carry_forward"):
            profiles.load_profile("test-profile", tmp_path)

    def test_cnc_must_declare_carry_forward(self, tmp_path):
        data = copy.deepcopy(GOOD)
        data["products"]["CNC"] = {}
        _write(tmp_path, data)
        with pytest.raises(profiles.ProfileError, match="carry_forward"):
            profiles.load_profile("test-profile", tmp_path)

    def test_cnc_has_no_mis_cutoff(self, tmp_path):
        data = copy.deepcopy(GOOD)
        data["products"]["CNC"]["force_flat"] = "15:00"
        _write(tmp_path, data)
        with pytest.raises(profiles.ProfileError, match="force_flat"):
            profiles.load_profile("test-profile", tmp_path)

    def test_every_problem_is_listed(self, tmp_path):
        data = {k: v for k, v in GOOD.items() if k != "broker"}
        _write(tmp_path, {**data, "entry": "x"})
        with pytest.raises(profiles.ProfileError) as err:
            profiles.load_profile("test-profile", tmp_path)
        assert "entry" in str(err.value) and "broker" in str(err.value)


class TestHash:
    def test_sha256_is_stable_under_key_order_and_formatting(self, tmp_path):
        first = _load_with(tmp_path / "a", GOOD)
        reordered = {k: GOOD[k] for k in reversed(list(GOOD))}
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "test-profile.yaml").write_text(
            yaml.safe_dump(reordered, sort_keys=False, default_flow_style=True), encoding="utf-8"
        )
        second = profiles.load_profile("test-profile", tmp_path / "b")
        assert first.sha256 == second.sha256
        assert len(first.sha256) == 64

    def test_sha256_changes_with_content(self, tmp_path):
        changed = copy.deepcopy(GOOD)
        changed["products"]["MIS"]["force_flat"] = "15:10"
        assert _load_with(tmp_path / "a", GOOD).sha256 != _load_with(tmp_path / "b", changed).sha256

    def test_as_dict_round_trips_the_content(self):
        content = profiles.load_profile("flattrade-nse-cash").as_dict()
        assert content["name"] == "flattrade-nse-cash"
        assert content["products"]["MIS"]["force_flat"] == "15:00"


class TestReviewHardening:
    @pytest.mark.parametrize(
        "name", ["../x", "/etc/passwd", "a/b", "x\n", "flattrade-nse-cash\n", "", ".hidden", 5]
    )
    def test_unsafe_profile_names_are_rejected(self, name):
        with pytest.raises(profiles.ProfileError, match="not found"):
            profiles.load_profile(name)

    def test_profile_is_hashable(self):
        p = profiles.load_profile("flattrade-nse-cash")
        assert isinstance(hash(p), int)

    @pytest.mark.parametrize("bar_min", [1, 5, 15, 60])
    def test_latest_exit_bar_closes_by_14_55_for_every_bar_size(self, bar_min):
        fields = profiles.load_profile("flattrade-nse-cash").run_config_fields("MIS")
        last_start = fields["mis_squareoff_min"] - fields["squareoff_margin_min"] - bar_min
        assert last_start + bar_min == 14 * 60 + 55
