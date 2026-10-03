"""`intake migrate-classification`: legacy category/tags -> strategy_category/strategy_tags."""

from __future__ import annotations

from pathlib import Path

import pytest

from signal_engine.research import build_registry as br
from signal_engine.research.intake import cli, frontmatter, taxonomy

BASE = (
    "id: IDEA-{n}\ntitle: Sample\nstage: inbox\n{legacy}horizon: intraday\n"
    "source: {{type: self}}\nsummary: One line.\nlast_reviewed: 2026-09-29\n"
)


def _idea(root: Path, n: str, legacy: str, nl: str = "\n") -> Path:
    folder = root / "research" / "ideas" / f"idea-{n}-sample"
    folder.mkdir(parents=True, exist_ok=True)
    fm = BASE.format(n=n, legacy=legacy).replace("\n", nl)
    (folder / "idea.md").write_bytes(f"---{nl}{fm}---{nl}{nl}Body.{nl}".encode())
    return folder


def _run(root: Path, *extra: str) -> int:
    return cli.main(["migrate-classification", *extra], root=root)


class TestMapping:
    def test_every_legacy_category_maps_to_a_taxonomy_category(self):
        cats = taxonomy.load_categories()
        assert set(taxonomy.LEGACY_CATEGORY_MAP) == {
            "intraday-momentum",
            "intraday-mean-reversion",
            "intraday-breakout",
            "market-profile",
            "swing-momentum",
            "swing-mean-reversion",
            "swing-value",
            "options",
            "tooling",
        }
        assert set(taxonomy.LEGACY_CATEGORY_MAP.values()) <= set(cats)
        assert taxonomy.LEGACY_CATEGORY_MAP["market-profile"] == "Market Structure"
        assert taxonomy.LEGACY_CATEGORY_MAP["swing-value"] == "Statistical / Quantitative"


class TestMigrate:
    @pytest.mark.parametrize(
        "legacy,expected",
        [
            ("intraday-momentum", "Momentum"),
            ("intraday-mean-reversion", "Mean Reversion"),
            ("intraday-breakout", "Breakout"),
            ("market-profile", "Market Structure"),
            ("swing-momentum", "Momentum"),
            ("swing-mean-reversion", "Mean Reversion"),
            ("swing-value", "Statistical / Quantitative"),
            ("options", "Options"),
            ("tooling", "Other"),
        ],
    )
    def test_category_is_mapped_and_legacy_removed(self, tmp_path, legacy, expected):
        folder = _idea(tmp_path, "001", f"category: {legacy}\n")
        assert _run(tmp_path) == 0
        fm = frontmatter.read(folder)
        assert fm["strategy_category"] == expected
        assert "category" not in fm and "tags" not in fm

    def test_unknown_legacy_value_becomes_other_and_is_reported(self, tmp_path, capsys):
        folder = _idea(tmp_path, "001", "category: astrology\n")
        assert _run(tmp_path) == 0
        assert frontmatter.read(folder)["strategy_category"] == "Other"
        assert "unknown legacy category 'astrology'" in capsys.readouterr().out

    def test_tags_merge_after_existing_strategy_tags_case_insensitively(self, tmp_path):
        folder = _idea(
            tmp_path,
            "001",
            "category: options\ntags: [VWAP, ema, Ema]\nstrategy_tags: [vwap, Gap]\n",
        )
        assert _run(tmp_path) == 0
        fm = frontmatter.read(folder)
        assert fm["strategy_tags"] == ["vwap", "Gap", "ema"]
        assert "tags" not in fm

    def test_existing_different_strategy_category_is_kept(self, tmp_path, capsys):
        folder = _idea(tmp_path, "001", "category: options\nstrategy_category: Grid\n")
        assert _run(tmp_path) == 0
        fm = frontmatter.read(folder)
        assert fm["strategy_category"] == "Grid" and "category" not in fm
        assert "kept existing" in capsys.readouterr().out

    def test_taxonomy_lacking_a_mapped_category_gives_other_and_is_reported(self, tmp_path, capsys):
        (tmp_path / "research").mkdir()
        (tmp_path / "research" / "taxonomy.yaml").write_text(
            "categories:\n  - Other\n", encoding="utf-8"
        )
        folder = _idea(tmp_path, "001", "category: intraday-breakout\n")
        assert _run(tmp_path) == 0
        assert frontmatter.read(folder)["strategy_category"] == "Other"
        assert "not in the taxonomy" in capsys.readouterr().out

    def test_idempotent(self, tmp_path, capsys):
        folder = _idea(tmp_path, "001", "category: options\ntags: [a]\n")
        _run(tmp_path)
        once = (folder / "idea.md").read_bytes()
        capsys.readouterr()
        assert _run(tmp_path) == 0
        assert (folder / "idea.md").read_bytes() == once
        assert "0 changed" in capsys.readouterr().out

    def test_dry_run_writes_nothing_and_prints_changes(self, tmp_path, capsys):
        folder = _idea(tmp_path, "001", "category: options\ntags: [a]\n")
        before = (folder / "idea.md").read_bytes()
        assert _run(tmp_path, "--dry-run") == 0
        assert (folder / "idea.md").read_bytes() == before
        out = capsys.readouterr().out
        assert "IDEA-001" in out and "Options" in out and "1 would change" in out

    def test_crlf_file_keeps_its_line_endings(self, tmp_path):
        folder = _idea(tmp_path, "001", "category: market-profile\ntags: [a, b]\n", nl="\r\n")
        assert _run(tmp_path) == 0
        raw = (folder / "idea.md").read_bytes()
        assert b"\n" not in raw.replace(b"\r\n", b"")
        fm = frontmatter.read(folder)
        assert fm["strategy_category"] == "Market Structure"
        assert fm["strategy_tags"] == ["a", "b"]
        assert "category" not in fm and "tags" not in fm

    def test_unreadable_idea_exits_one_but_others_are_migrated(self, tmp_path, capsys):
        good = _idea(tmp_path, "001", "category: options\n")
        bad = _idea(tmp_path, "002", "")
        (bad / "idea.md").write_text("no frontmatter here", encoding="utf-8")
        assert _run(tmp_path) == 1
        assert frontmatter.read(good)["strategy_category"] == "Options"
        assert "IDEA-002" in capsys.readouterr().out

    def test_migrated_file_passes_the_registry(self, tmp_path):
        _idea(tmp_path, "001", "category: intraday-breakout\ntags: [vwap]\n")
        with pytest.raises(br.RegistryError, match="migrate-classification"):
            br.load_ideas(tmp_path)
        assert _run(tmp_path) == 0
        (entry,) = br.load_ideas(tmp_path)
        assert entry["strategy_category"] == "Breakout"
        assert entry["strategy_tags"] == ["vwap"]
        assert "category" not in entry and "tags" not in entry
