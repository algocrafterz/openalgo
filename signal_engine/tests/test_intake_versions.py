"""Strategy versions: an append-only list, an automatic label, an optional branch label."""

from __future__ import annotations

from datetime import datetime

import pytest
import yaml

from signal_engine.research.intake import versions
from signal_engine.timeutils import IST

NOW = datetime(2026, 10, 2, 14, 0, tzinfo=IST)
FP_A, FP_B, FP_C, FP_D = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
IDEA_MD = "---\nid: IDEA-001\ntitle: T\nstatus: evaluating\n---\n\nBody text.\n"


def _resolve(folder, fp, **kw):
    return versions.resolve_version(folder, fp, now=NOW, **kw)


class TestResolve:
    def test_first_fingerprint_creates_v1(self, tmp_path):
        result = _resolve(tmp_path, FP_A, note="baseline")
        assert (result.version, result.created, result.parent) == ("v1", True, None)
        entry = versions.load_versions(tmp_path)[0]
        assert entry.strategy_fingerprint == FP_A and entry.note == "baseline"
        assert entry.created_ist.endswith("+05:30")

    def test_known_fingerprint_reuses_its_version_and_writes_nothing(self, tmp_path):
        _resolve(tmp_path, FP_A)
        before = (tmp_path / "versions.yaml").read_bytes()
        again = _resolve(tmp_path, FP_A)
        assert (again.version, again.created) == ("v1", False)
        assert (tmp_path / "versions.yaml").read_bytes() == before

    def test_unknown_fingerprint_appends_the_next_version(self, tmp_path):
        _resolve(tmp_path, FP_A)
        result = _resolve(tmp_path, FP_B, default_parent="v1")
        assert (result.version, result.created, result.parent) == ("v2", True, "v1")

    def test_explicit_parent_overrides_the_default(self, tmp_path):
        _resolve(tmp_path, FP_A)
        _resolve(tmp_path, FP_B, default_parent="v1")
        result = _resolve(tmp_path, FP_C, default_parent="v2", parent="v1")
        assert result.parent == "v1"

    def test_unknown_parent_is_rejected(self, tmp_path):
        _resolve(tmp_path, FP_A)
        with pytest.raises(versions.VersionError, match="v9"):
            _resolve(tmp_path, FP_B, parent="v9")
        assert len(versions.load_versions(tmp_path)) == 1

    def test_first_version_with_a_parent_is_rejected(self, tmp_path):
        with pytest.raises(versions.VersionError, match="v1"):
            _resolve(tmp_path, FP_A, parent="v1")

    def test_branch_label_is_used(self, tmp_path):
        _resolve(tmp_path, FP_A)
        _resolve(tmp_path, FP_B, default_parent="v1")
        result = _resolve(tmp_path, FP_C, default_parent="v2", label="v3b")
        assert result.version == "v3b"

    def test_next_integer_follows_branch_labels(self, tmp_path):
        _resolve(tmp_path, FP_A)
        _resolve(tmp_path, FP_B, default_parent="v1", label="v3b")
        assert _resolve(tmp_path, FP_C, default_parent="v3b").version == "v4"

    @pytest.mark.parametrize("label", ["3", "version3", "v", "V3", "v3 b", "v3bb"])
    def test_bad_label_format_is_rejected(self, tmp_path, label):
        _resolve(tmp_path, FP_A)
        with pytest.raises(versions.VersionError, match="label"):
            _resolve(tmp_path, FP_B, label=label)

    def test_taken_label_is_rejected(self, tmp_path):
        _resolve(tmp_path, FP_A)
        with pytest.raises(versions.VersionError, match="already"):
            _resolve(tmp_path, FP_B, label="v1")

    def test_label_on_a_known_fingerprint_is_ignored(self, tmp_path):
        _resolve(tmp_path, FP_A)
        assert _resolve(tmp_path, FP_A, label="v7").version == "v1"


class TestAppendOnly:
    def test_existing_bytes_are_never_rewritten(self, tmp_path):
        _resolve(tmp_path, FP_A)
        first = (tmp_path / "versions.yaml").read_bytes()
        _resolve(tmp_path, FP_B, default_parent="v1")
        second = (tmp_path / "versions.yaml").read_bytes()
        assert second.startswith(first) and len(second) > len(first)
        _resolve(tmp_path, FP_C, default_parent="v2")
        assert (tmp_path / "versions.yaml").read_bytes().startswith(second)

    def test_file_only_grows_across_reuse_and_append(self, tmp_path):
        sizes = []
        for i, fp in enumerate((FP_A, FP_A, FP_B, FP_B, FP_C)):
            _resolve(tmp_path, fp, **({"default_parent": "v1"} if i else {}))
            sizes.append((tmp_path / "versions.yaml").stat().st_size)
        assert sizes == sorted(sizes)


class TestLoad:
    def test_absent_file_is_empty(self, tmp_path):
        assert versions.load_versions(tmp_path) == []

    def test_unreadable_file_raises(self, tmp_path):
        (tmp_path / "versions.yaml").write_text("- [unclosed", encoding="utf-8")
        with pytest.raises(versions.VersionError, match="unreadable"):
            versions.load_versions(tmp_path)

    def _write(self, tmp_path, rows):
        (tmp_path / "versions.yaml").write_text(yaml.safe_dump(rows), encoding="utf-8")

    def _row(self, version, fp, parent=None):
        return {
            "version": version,
            "strategy_fingerprint": fp,
            "parent": parent,
            "created_ist": "2026-10-02T14:00:00+05:30",
            "note": "",
        }

    def test_duplicate_version_is_rejected(self, tmp_path):
        self._write(tmp_path, [self._row("v1", FP_A), self._row("v1", FP_B)])
        with pytest.raises(versions.VersionError, match="duplicate"):
            versions.load_versions(tmp_path)

    def test_duplicate_fingerprint_is_rejected(self, tmp_path):
        self._write(tmp_path, [self._row("v1", FP_A), self._row("v2", FP_A, "v1")])
        with pytest.raises(versions.VersionError, match="duplicate"):
            versions.load_versions(tmp_path)

    def test_parent_must_precede_the_child(self, tmp_path):
        self._write(tmp_path, [self._row("v1", FP_A, "v2"), self._row("v2", FP_B)])
        with pytest.raises(versions.VersionError, match="parent"):
            versions.load_versions(tmp_path)

    def test_missing_key_is_rejected(self, tmp_path):
        row = self._row("v1", FP_A)
        del row["created_ist"]
        self._write(tmp_path, [row])
        with pytest.raises(versions.VersionError, match="created_ist"):
            versions.load_versions(tmp_path)

    def test_not_a_list_is_rejected(self, tmp_path):
        self._write(tmp_path, {"v1": FP_A})
        with pytest.raises(versions.VersionError, match="list"):
            versions.load_versions(tmp_path)


class TestPointers:
    def test_set_current_adds_the_key_and_keeps_the_rest(self, tmp_path):
        (tmp_path / "idea.md").write_text(IDEA_MD, encoding="utf-8")
        _resolve(tmp_path, FP_A)
        versions.set_pointer(tmp_path, "current_version", "v1")
        text = (tmp_path / "idea.md").read_text(encoding="utf-8")
        assert "current_version: v1" in text
        assert text.endswith("---\n\nBody text.\n") and "title: T" in text

    def test_set_current_replaces_an_existing_value(self, tmp_path):
        (tmp_path / "idea.md").write_text(IDEA_MD, encoding="utf-8")
        _resolve(tmp_path, FP_A)
        _resolve(tmp_path, FP_B, default_parent="v1")
        versions.set_pointer(tmp_path, "current_version", "v1")
        versions.set_pointer(tmp_path, "current_version", "v2")
        text = (tmp_path / "idea.md").read_text(encoding="utf-8")
        assert text.count("current_version") == 1 and "current_version: v2" in text

    def test_unknown_version_is_rejected(self, tmp_path):
        (tmp_path / "idea.md").write_text(IDEA_MD, encoding="utf-8")
        with pytest.raises(versions.VersionError, match="v1"):
            versions.set_pointer(tmp_path, "current_version", "v1")

    def test_unknown_key_is_rejected(self, tmp_path):
        (tmp_path / "idea.md").write_text(IDEA_MD, encoding="utf-8")
        _resolve(tmp_path, FP_A)
        with pytest.raises(versions.VersionError, match="key"):
            versions.set_pointer(tmp_path, "latest_version", "v1")

    def test_read_pointers(self, tmp_path):
        (tmp_path / "idea.md").write_text(IDEA_MD, encoding="utf-8")
        assert versions.read_pointers(tmp_path) == {
            "current_version": None,
            "promoted_version": None,
        }
        _resolve(tmp_path, FP_A)
        versions.set_pointer(tmp_path, "current_version", "v1")
        assert versions.read_pointers(tmp_path)["current_version"] == "v1"

    def test_read_pointers_without_idea_md(self, tmp_path):
        assert versions.read_pointers(tmp_path) == {
            "current_version": None,
            "promoted_version": None,
        }


class TestHardening:
    def test_dry_run_validates_and_writes_nothing(self, tmp_path):
        result = _resolve(tmp_path, FP_A, dry_run=True)
        assert (result.version, result.created) == ("v1", True)
        assert not (tmp_path / "versions.yaml").exists()

    def test_dry_run_still_rejects_a_bad_label(self, tmp_path):
        with pytest.raises(versions.VersionError, match="label"):
            _resolve(tmp_path, FP_A, label="nope", dry_run=True)

    def test_flow_style_file_is_refused_on_append(self, tmp_path):
        (tmp_path / "versions.yaml").write_text("[]\n", encoding="utf-8")
        with pytest.raises(versions.VersionError, match="block"):
            _resolve(tmp_path, FP_A)
        assert (tmp_path / "versions.yaml").read_text(encoding="utf-8") == "[]\n"

    def test_comment_only_file_accepts_an_append(self, tmp_path):
        (tmp_path / "versions.yaml").write_text("# versions\n", encoding="utf-8")
        assert _resolve(tmp_path, FP_A).version == "v1"
        assert versions.load_versions(tmp_path)[0].version == "v1"

    @pytest.mark.parametrize("label", ["v1\nx", "v1\\", "release", "v1; rm"])
    def test_loaded_labels_are_validated(self, tmp_path, label):
        row = {
            "version": label,
            "strategy_fingerprint": FP_A,
            "parent": None,
            "created_ist": "2026-10-02T14:00:00+05:30",
            "note": "",
        }
        (tmp_path / "versions.yaml").write_text(yaml.safe_dump([row]), encoding="utf-8")
        with pytest.raises(versions.VersionError, match="label"):
            versions.load_versions(tmp_path)

    def test_loaded_fingerprints_are_validated(self, tmp_path):
        row = {
            "version": "v1",
            "strategy_fingerprint": "xyz",
            "parent": None,
            "created_ist": "2026-10-02T14:00:00+05:30",
            "note": "",
        }
        (tmp_path / "versions.yaml").write_text(yaml.safe_dump([row]), encoding="utf-8")
        with pytest.raises(versions.VersionError, match="fingerprint"):
            versions.load_versions(tmp_path)

    def test_malformed_frontmatter_is_a_version_error(self, tmp_path):
        (tmp_path / "idea.md").write_text("---\nkey: [unclosed\n---\n\nBody\n", encoding="utf-8")
        with pytest.raises(versions.VersionError, match="frontmatter"):
            versions.read_pointers(tmp_path)

    def test_set_pointer_keeps_crlf_endings(self, tmp_path):
        text = "---\r\nid: IDEA-001\r\ncurrent_version: v1\r\n---\r\n\r\nBody\r\n"
        (tmp_path / "idea.md").write_bytes(text.encode("utf-8"))
        _resolve(tmp_path, FP_A)
        _resolve(tmp_path, FP_B, default_parent="v1")
        versions.set_pointer(tmp_path, "current_version", "v2")
        raw = (tmp_path / "idea.md").read_bytes().decode("utf-8")
        assert raw == text.replace("current_version: v1", "current_version: v2")
        assert "\n" not in raw.replace("\r\n", "")

    def test_set_pointer_keeps_other_lines_that_start_with_the_key_name(self, tmp_path):
        text = "---\nid: IDEA-001\ncurrent_version_note: keep me\n---\n\nBody\n"
        (tmp_path / "idea.md").write_text(text, encoding="utf-8")
        _resolve(tmp_path, FP_A)
        versions.set_pointer(tmp_path, "current_version", "v1")
        out = (tmp_path / "idea.md").read_text(encoding="utf-8")
        assert "current_version_note: keep me" in out and "current_version: v1" in out
