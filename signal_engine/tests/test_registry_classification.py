"""Strategy classification: validated, carried into the registry, and never part of a version."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from signal_engine.research import build_registry as br
from signal_engine.research.intake import cli, freeze, frontmatter
from signal_engine.tests.intake_fixtures import make_spec, write_spec
from signal_engine.tests.test_registry_stage import IDEA, _setup
from signal_engine.timeutils import IST

NOW = datetime(2026, 10, 2, 14, 0, tzinfo=IST)


def _idea(root: Path, extra: str = "", stage: str = "inbox", spec: bool = False) -> Path:
    folder = root / "research" / "ideas" / "idea-001-sample"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "idea.md").write_text(
        IDEA.format(lifecycle=f"stage: {stage}\n", extra=extra), encoding="utf-8"
    )
    if spec:
        write_spec(folder, make_spec())
    return folder


def _entry(root: Path) -> dict:
    (idea,) = br.load_ideas(root)
    return idea


class TestCategoryAndTags:
    def test_absent_category_is_other_and_tags_empty(self, tmp_path):
        _idea(tmp_path)
        entry = _entry(tmp_path)
        assert entry["strategy_category"] == "Other"
        assert entry["strategy_tags"] == []

    def test_valid_category_and_normalised_tags(self, tmp_path):
        _idea(tmp_path, "strategy_category: Breakout\nstrategy_tags: [' VWAP ', vwap, Pullback]\n")
        entry = _entry(tmp_path)
        assert entry["strategy_category"] == "Breakout"
        assert entry["strategy_tags"] == ["VWAP", "Pullback"]

    def test_category_not_in_the_taxonomy_is_reported(self, tmp_path):
        _idea(tmp_path, "strategy_category: Astrology\n")
        with pytest.raises(br.RegistryError, match="strategy_category 'Astrology' not one of"):
            br.load_ideas(tmp_path)

    def test_category_is_case_sensitive(self, tmp_path):
        _idea(tmp_path, "strategy_category: breakout\n")
        with pytest.raises(br.RegistryError, match="strategy_category"):
            br.load_ideas(tmp_path)

    def test_non_list_tags_are_reported(self, tmp_path):
        _idea(tmp_path, "strategy_tags: vwap\n")
        with pytest.raises(br.RegistryError, match="strategy_tags must be a list"):
            br.load_ideas(tmp_path)

    @pytest.mark.parametrize("value", ["['']", "[1]", "[null]", "[[a]]"])
    def test_bad_tag_items_are_reported(self, tmp_path, value):
        _idea(tmp_path, f"strategy_tags: {value}\n")
        with pytest.raises(br.RegistryError, match="strategy_tags"):
            br.load_ideas(tmp_path)

    def test_entry_carries_no_legacy_keys_and_horizon_is_untouched(self, tmp_path):
        _idea(tmp_path, "strategy_category: Momentum\n")
        entry = _entry(tmp_path)
        assert "category" not in entry and "tags" not in entry
        assert entry["strategy_category"] == "Momentum" and entry["horizon"] == "intraday"

    @pytest.mark.parametrize("legacy", ["category: intraday-momentum\n", "tags: [vwap]\n"])
    def test_legacy_keys_are_rejected_with_the_migration_hint(self, tmp_path, legacy):
        _idea(tmp_path, legacy)
        with pytest.raises(br.RegistryError, match="migrate-classification") as exc:
            br.load_ideas(tmp_path)
        assert "category was replaced by strategy_category" in str(exc.value)
        assert "tags by strategy_tags" in str(exc.value)

    def test_category_is_no_longer_required(self, tmp_path):
        _idea(tmp_path)
        assert _entry(tmp_path)["strategy_category"] == "Other"

    def test_root_taxonomy_file_extends_the_valid_categories(self, tmp_path):
        _idea(tmp_path, "strategy_category: Seasonality\n")
        with pytest.raises(br.RegistryError, match="Seasonality"):
            br.load_ideas(tmp_path)
        (tmp_path / "research" / "taxonomy.yaml").write_text(
            "categories:\n  - Seasonality\n  - Other\n", encoding="utf-8"
        )
        assert _entry(tmp_path)["strategy_category"] == "Seasonality"

    def test_invalid_taxonomy_file_is_a_registry_problem(self, tmp_path):
        _idea(tmp_path)
        (tmp_path / "research" / "taxonomy.yaml").write_text("categories: []\n", encoding="utf-8")
        with pytest.raises(br.RegistryError, match="taxonomy"):
            br.load_ideas(tmp_path)


class TestClassificationBlock:
    def test_unavailable_parts_are_none_without_a_spec(self, tmp_path):
        _idea(tmp_path, "strategy_category: Breakout\nstrategy_tags: [a]\n")
        assert _entry(tmp_path)["classification"] == {
            "strategy_category": "Breakout",
            "strategy_tags": ["a"],
            "trading_style": None,
            "direction": None,
            "timeframe": None,
            "market": None,
        }

    def test_spec_and_profile_supply_style_direction_timeframe_market(self, tmp_path):
        _idea(tmp_path, stage="backtest", spec=True)
        block = _entry(tmp_path)["classification"]
        assert block["trading_style"] == "intraday"
        assert block["direction"] == "both"
        assert block["timeframe"] == "5m"
        assert block["market"] == "NSE/BSE cash equity"
        assert block["strategy_category"] == "Other" and block["strategy_tags"] == []

    def test_an_invalid_spec_fails_the_registry_so_gives_no_classification(self, tmp_path):
        folder = _idea(tmp_path)
        write_spec(folder, make_spec(entry=["Buy when momentum is strong"]))
        with pytest.raises(br.RegistryError, match="spec.yaml"):
            br.load_ideas(tmp_path)

    def test_unknown_profile_gives_no_market_but_keeps_the_rest(self):
        from signal_engine.research.intake import taxonomy

        block = taxonomy.classification_block(
            {},
            ("Other",),
            {"profile": "no-such-profile", "style": "swing", "direction": "long_only"},
        )
        assert block["market"] is None
        assert block["trading_style"] == "swing" and block["timeframe"] is None

    def test_block_is_in_registry_json(self, tmp_path):
        _idea(tmp_path)
        assert '"classification"' in br.render_json(br.load_ideas(tmp_path))

    def test_run_records_do_not_gain_classification(self, tmp_path):
        folder = _idea(tmp_path, "strategy_category: Breakout\n", stage="backtest", spec=True)
        _setup(folder)
        run = _entry(tmp_path)["runs"][0]
        assert not {"strategy_category", "strategy_tags", "classification"} & set(run)


def _tree(folder: Path) -> dict[str, str]:
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name != "idea.md"
    }


class TestClassificationIsolation:
    """Editing category and tags must not move a fingerprint, a version or a run."""

    def _frozen_idea(self, tmp_path):
        folder = _idea(tmp_path, stage="backtest", spec=True)
        first = freeze.freeze(folder, now=NOW)
        _setup(folder)
        return folder, first

    def test_freeze_fingerprints_and_version_are_identical(self, tmp_path):
        folder, first = self._frozen_idea(tmp_path)
        frontmatter.set_keys(
            folder, {"strategy_category": "Mean Reversion", "strategy_tags": ["x", "y"]}
        )
        second = freeze.freeze(folder, now=NOW)
        assert second.strategy_fingerprint == first.strategy_fingerprint
        assert second.rules_fingerprint == first.rules_fingerprint
        assert second.params_fingerprint == first.params_fingerprint
        assert second.version == first.version and second.version_created is False
        assert second.intent == first.intent

    def test_no_new_versions_entry_and_no_file_changes(self, tmp_path):
        folder, _ = self._frozen_idea(tmp_path)
        before = _tree(folder)
        frontmatter.set_keys(folder, {"strategy_category": "Grid", "strategy_tags": ["g"]})
        freeze.freeze(folder, now=NOW)
        assert _tree(folder) == before

    def test_classify_command_changes_nothing_but_idea_md(self, tmp_path):
        folder, first = self._frozen_idea(tmp_path)
        before = _tree(folder)
        assert (
            cli.main(
                ["classify", "IDEA-001", "--category", "Breakout", "--tags", "a,b"], root=tmp_path
            )
            == 0
        )
        assert _tree(folder) == before
        assert freeze.freeze(folder, now=NOW).strategy_fingerprint == first.strategy_fingerprint

    def test_classification_does_not_change_a_run_file_as_the_registry_sees_it(self, tmp_path):
        folder, _ = self._frozen_idea(tmp_path)
        before = _entry(tmp_path)["runs"]
        frontmatter.set_keys(folder, {"strategy_category": "Options"})
        assert _entry(tmp_path)["runs"] == before
