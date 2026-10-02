"""research/taxonomy.yaml, its loader, and the tag and category rules."""

from __future__ import annotations

import pytest

from signal_engine.research.intake import taxonomy

EXPECTED = (
    "Trend Following",
    "Mean Reversion",
    "Breakout",
    "Momentum",
    "Market Structure",
    "Statistical / Quantitative",
    "Grid",
    "Arbitrage",
    "Options",
    "Other",
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestLoader:
    def test_shipped_taxonomy_lists_the_agreed_categories(self):
        assert taxonomy.load_categories() == EXPECTED

    def test_loads_a_custom_file(self, tmp_path):
        path = _write(tmp_path / "t.yaml", "categories:\n  - Alpha\n  - Other\n")
        assert taxonomy.load_categories(path) == ("Alpha", "Other")

    @pytest.mark.parametrize(
        "text, message",
        [
            ("categories: []\n", "non-empty"),
            ("categories:\n  - Alpha\n", "Other"),
            ("categories:\n  - Other\n  - Other\n", "unique"),
            ("categories:\n  - Other\n  - other\n", "unique"),
            ("categories:\n  - Other\n  - 3\n", "text"),
            ("categories:\n  - Other\n  - '  '\n", "text"),
            ("categories: Other\n", "list"),
            ("other: 1\n", "categories"),
            ("- a\n", "mapping"),
            ("a: [unclosed\n", "unreadable"),
        ],
    )
    def test_invalid_files_are_refused(self, tmp_path, text, message):
        with pytest.raises(taxonomy.TaxonomyError, match=message):
            taxonomy.load_categories(_write(tmp_path / "t.yaml", text))

    def test_missing_file_is_refused(self, tmp_path):
        with pytest.raises(taxonomy.TaxonomyError, match="unreadable"):
            taxonomy.load_categories(tmp_path / "nope.yaml")

    def test_extending_the_file_makes_the_new_category_valid(self, tmp_path):
        path = _write(tmp_path / "t.yaml", "categories:\n  - Alpha\n  - Other\n")
        assert "Beta" not in taxonomy.load_categories(path)
        _write(path, "categories:\n  - Alpha\n  - Beta\n  - Other\n")
        assert "Beta" in taxonomy.load_categories(path)

    def test_result_is_cached_per_path(self, tmp_path):
        a = _write(tmp_path / "a.yaml", "categories:\n  - A\n  - Other\n")
        b = _write(tmp_path / "b.yaml", "categories:\n  - B\n  - Other\n")
        assert taxonomy.load_categories(a) == ("A", "Other")
        assert taxonomy.load_categories(b) == ("B", "Other")
        assert taxonomy.load_categories(a) is taxonomy.load_categories(a)


class TestNormaliseTags:
    def test_strips_and_deduplicates_case_insensitively_keeping_first_spelling(self):
        assert taxonomy.normalise_tags(["  VWAP ", "vwap", "Pullback", "PULLBACK", "ema"]) == [
            "VWAP",
            "Pullback",
            "ema",
        ]

    def test_order_is_kept(self):
        assert taxonomy.normalise_tags(["b", "a", "B", "c"]) == ["b", "a", "c"]

    def test_empty_list(self):
        assert taxonomy.normalise_tags([]) == []

    def test_unicode_case_folding(self):
        assert taxonomy.normalise_tags(["Straße", "STRASSE"]) == ["Straße"]

    @pytest.mark.parametrize("bad", ["vwap", None, 5, {"a": 1}])
    def test_non_list_is_refused(self, bad):
        with pytest.raises(taxonomy.TaxonomyError, match="list"):
            taxonomy.normalise_tags(bad)

    @pytest.mark.parametrize("bad", [[""], ["  "], [1], [None], ["ok", ["nested"]]])
    def test_non_text_or_blank_items_are_refused(self, bad):
        with pytest.raises(taxonomy.TaxonomyError, match="non-empty"):
            taxonomy.normalise_tags(bad)


class TestParseTagArgument:
    def test_splits_a_comma_separated_argument(self):
        assert taxonomy.parse_tags("a, b ,c") == ["a", "b", "c"]

    def test_empty_argument_is_an_empty_list(self):
        assert taxonomy.parse_tags("") == []
        assert taxonomy.parse_tags(" , ") == []

    def test_duplicates_collapse(self):
        assert taxonomy.parse_tags("VWAP,vwap") == ["VWAP"]
