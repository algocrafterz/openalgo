"""frontmatter.set_keys edits only the named frontmatter lines."""

from __future__ import annotations

import pytest
import yaml

from signal_engine.research.intake import frontmatter, versions

LF = (
    "---\n"
    "id: IDEA-001   # keep me\n"
    "title: T\n"
    "# a comment line\n"
    "stage: inbox\n"
    "source: {type: self}\n"
    "---\n"
    "\n"
    "Body: not frontmatter\nstage: body-line\n"
)


def _write(tmp_path, text: str, newline: str | None = None):
    path = tmp_path / "idea.md"
    data = text if newline is None else text.replace("\n", newline)
    path.write_bytes(data.encode("utf-8"))
    return path


class TestSetKeys:
    def test_replaces_only_the_named_line(self, tmp_path):
        path = _write(tmp_path, LF)
        frontmatter.set_keys(tmp_path, {"stage": "backtest"})
        assert path.read_text(encoding="utf-8") == LF.replace("stage: inbox", "stage: backtest", 1)

    def test_adds_an_absent_key_at_the_end_of_the_block(self, tmp_path):
        path = _write(tmp_path, LF)
        frontmatter.set_keys(tmp_path, {"research_state": "promising"})
        assert path.read_text(encoding="utf-8") == LF.replace(
            "source: {type: self}\n---", "source: {type: self}\nresearch_state: promising\n---", 1
        )

    def test_comments_other_keys_and_body_are_preserved(self, tmp_path):
        path = _write(tmp_path, LF)
        frontmatter.set_keys(tmp_path, {"stage": "paper", "new_key": "x"})
        text = path.read_text(encoding="utf-8")
        assert "id: IDEA-001   # keep me\n" in text
        assert "# a comment line\n" in text
        assert text.endswith("Body: not frontmatter\nstage: body-line\n")

    @pytest.mark.parametrize("newline", ["\r\n", "\n"])
    def test_line_endings_are_preserved(self, tmp_path, newline):
        path = _write(tmp_path, LF, newline)
        frontmatter.set_keys(tmp_path, {"stage": "backtest", "added": "1"})
        raw = path.read_bytes().decode("utf-8")
        if newline == "\r\n":
            assert "\n" not in raw.replace("\r\n", "")
        else:
            assert "\r" not in raw
        assert f"stage: backtest{newline}" in raw and f"added: '1'{newline}---" in raw

    def test_values_are_quoted_safely(self, tmp_path):
        _write(tmp_path, LF)
        tricky = "needs: quoting # not a comment 'q' \"d\""
        frontmatter.set_keys(tmp_path, {"revisit_when": tricky, "flag": "yes", "n": None})
        data = frontmatter.read(tmp_path)
        assert data["revisit_when"] == tricky
        assert data["flag"] == "yes"  # a string stays a string, not a YAML boolean
        assert data["n"] is None

    def test_unicode_and_newline_values_round_trip(self, tmp_path):
        _write(tmp_path, LF)
        frontmatter.set_keys(tmp_path, {"note": "line1\nline2 ₹"})
        assert frontmatter.read(tmp_path)["note"] == "line1\nline2 ₹"

    def test_list_values_are_written_as_one_line(self, tmp_path):
        path = _write(tmp_path, LF)
        frontmatter.set_keys(tmp_path, {"strategy_tags": ["VWAP", "Pull back"]})
        assert frontmatter.read(tmp_path)["strategy_tags"] == ["VWAP", "Pull back"]
        assert path.read_text(encoding="utf-8").count("strategy_tags") == 1

    def test_replacing_a_block_list_removes_its_item_lines(self, tmp_path):
        text = "---\nid: IDEA-001\nstrategy_tags:\n  - a\n  - b\nstage: inbox\n---\nBody\n"
        _write(tmp_path, text)
        frontmatter.set_keys(tmp_path, {"strategy_tags": ["c"]})
        data = frontmatter.read(tmp_path)
        assert data["strategy_tags"] == ["c"] and data["stage"] == "inbox"

    def test_replacing_a_zero_indent_block_list(self, tmp_path):
        text = "---\nid: IDEA-001\ntags:\n- a\n- b\nstage: inbox\n---\n"
        _write(tmp_path, text)
        frontmatter.set_keys(tmp_path, {"tags": ["z"]})
        assert frontmatter.read(tmp_path) == {"id": "IDEA-001", "tags": ["z"], "stage": "inbox"}

    def test_nested_key_with_same_name_is_not_touched(self, tmp_path):
        text = "---\nid: IDEA-001\nsource:\n  stage: nested\nstage: inbox\n---\n"
        _write(tmp_path, text)
        frontmatter.set_keys(tmp_path, {"stage": "backtest"})
        data = frontmatter.read(tmp_path)
        assert data["source"] == {"stage": "nested"} and data["stage"] == "backtest"

    def test_closing_marker_of_an_empty_tail_is_preserved(self, tmp_path):
        path = _write(tmp_path, "---\nid: IDEA-001\n---")
        frontmatter.set_keys(tmp_path, {"stage": "inbox"})
        assert path.read_text(encoding="utf-8") == "---\nid: IDEA-001\nstage: inbox\n---"

    def test_empty_updates_change_nothing(self, tmp_path):
        path = _write(tmp_path, LF)
        frontmatter.set_keys(tmp_path, {})
        assert path.read_text(encoding="utf-8") == LF

    def test_missing_file_and_missing_frontmatter_raise(self, tmp_path):
        with pytest.raises(frontmatter.FrontmatterError, match="idea.md"):
            frontmatter.set_keys(tmp_path, {"stage": "inbox"})
        _write(tmp_path, "no frontmatter here\n")
        with pytest.raises(frontmatter.FrontmatterError, match="frontmatter"):
            frontmatter.set_keys(tmp_path, {"stage": "inbox"})

    @pytest.mark.parametrize("key", ["", "bad key", "a:b", "x\ny", "-lead"])
    def test_unsafe_key_names_are_refused(self, tmp_path, key):
        path = _write(tmp_path, LF)
        with pytest.raises(frontmatter.FrontmatterError, match="key"):
            frontmatter.set_keys(tmp_path, {key: "v"})
        assert path.read_text(encoding="utf-8") == LF

    def test_nothing_is_written_when_any_key_is_refused(self, tmp_path):
        path = _write(tmp_path, LF)
        with pytest.raises(frontmatter.FrontmatterError):
            frontmatter.set_keys(tmp_path, {"stage": "paper", "bad key": "v"})
        assert path.read_text(encoding="utf-8") == LF

    def test_result_is_valid_yaml(self, tmp_path):
        _write(tmp_path, LF)
        frontmatter.set_keys(tmp_path, {"stage": "paper", "a": [1, "x"], "b": "k: v"})
        block = (tmp_path / "idea.md").read_text(encoding="utf-8").split("---")[1]
        assert yaml.safe_load(block)["b"] == "k: v"


class TestSetPointerUsesIt:
    def test_set_pointer_still_edits_one_line(self, tmp_path):
        (tmp_path / "idea.md").write_text(
            "---\nid: IDEA-001\n# c\ncurrent_version: v1\n---\nBody\n", encoding="utf-8"
        )
        (tmp_path / "versions.yaml").write_text(
            "- version: v1\n  strategy_fingerprint: " + "a" * 64 + "\n  parent: null\n"
            "  created_ist: x\n  note: ''\n"
            "- version: v2\n  strategy_fingerprint: " + "b" * 64 + "\n  parent: v1\n"
            "  created_ist: x\n  note: ''\n",
            encoding="utf-8",
        )
        versions.set_pointer(tmp_path, "current_version", "v2")
        assert (tmp_path / "idea.md").read_text(encoding="utf-8") == (
            "---\nid: IDEA-001\n# c\ncurrent_version: v2\n---\nBody\n"
        )

    def test_set_pointer_without_frontmatter_raises_version_error(self, tmp_path):
        (tmp_path / "idea.md").write_text("plain\n", encoding="utf-8")
        (tmp_path / "versions.yaml").write_text(
            "- version: v1\n  strategy_fingerprint: " + "a" * 64 + "\n  parent: null\n"
            "  created_ist: x\n  note: ''\n",
            encoding="utf-8",
        )
        with pytest.raises(versions.VersionError, match="frontmatter"):
            versions.set_pointer(tmp_path, "current_version", "v1")


class TestKeyMatching:
    def _fm(self, *lines: str) -> str:
        return "---\n" + "\n".join(lines) + "\n---\n\nbody\n"

    def test_duplicate_keys_last_replaced_and_earlier_removed(self, tmp_path):
        path = _write(tmp_path, self._fm("id: A", "stage: inbox", "title: T", "stage: backtest"))
        frontmatter.set_keys(tmp_path, {"stage": "paper"})
        assert path.read_text(encoding="utf-8") == self._fm("id: A", "title: T", "stage: paper")

    def test_quoted_key_is_replaced(self, tmp_path):
        path = _write(tmp_path, self._fm("id: A", '"stage": inbox', "'title': T"))
        frontmatter.set_keys(tmp_path, {"stage": "paper", "title": "U"})
        text = path.read_text(encoding="utf-8")
        assert text == self._fm("id: A", "stage: paper", "title: U")

    def test_space_before_colon_is_replaced(self, tmp_path):
        path = _write(tmp_path, self._fm("id: A", "stage : inbox"))
        frontmatter.set_keys(tmp_path, {"stage": "paper"})
        assert path.read_text(encoding="utf-8") == self._fm("id: A", "stage: paper")
        assert frontmatter.read(tmp_path)["stage"] == "paper"

    def test_longer_key_with_same_prefix_is_not_touched(self, tmp_path):
        path = _write(
            tmp_path, self._fm("current_version_note: keep", "current_version: v1", "stage: x")
        )
        frontmatter.set_keys(tmp_path, {"current_version": "v2"})
        assert path.read_text(encoding="utf-8") == self._fm(
            "current_version_note: keep", "current_version: v2", "stage: x"
        )

    def test_longer_key_alone_means_the_key_is_added(self, tmp_path):
        path = _write(tmp_path, self._fm("current_version_note: keep"))
        frontmatter.set_keys(tmp_path, {"current_version": "v2"})
        assert path.read_text(encoding="utf-8") == self._fm(
            "current_version_note: keep", "current_version: v2"
        )

    def test_duplicate_with_continuation_lines_removed_whole(self, tmp_path):
        path = _write(tmp_path, self._fm("tags:", "  - a", "  - b", "id: A", "tags: [c]"))
        frontmatter.set_keys(tmp_path, {"tags": ["z"]})
        assert path.read_text(encoding="utf-8") == self._fm("id: A", "tags: [z]")


class TestAtomicWrite:
    def test_failed_replace_leaves_file_byte_identical(self, tmp_path, monkeypatch):
        import os

        path = _write(tmp_path, LF)
        before = path.read_bytes()

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            frontmatter.set_keys(tmp_path, {"stage": "paper"})
        assert path.read_bytes() == before
        assert [p.name for p in tmp_path.iterdir()] == ["idea.md"]

    def test_file_mode_is_preserved(self, tmp_path):
        import os

        path = _write(tmp_path, LF)
        os.chmod(path, 0o640)
        frontmatter.set_keys(tmp_path, {"stage": "paper"})
        assert os.stat(path).st_mode & 0o777 == 0o640
        assert [p.name for p in tmp_path.iterdir()] == ["idea.md"]


class TestRemoveKeys:
    def _folder(self, tmp_path, body):
        (tmp_path / "idea.md").write_text(f"---\n{body}---\n\nBody\n", encoding="utf-8")
        return tmp_path

    def test_removes_the_key_and_keeps_everything_else(self, tmp_path):
        folder = self._folder(
            tmp_path, "id: A\n# keep me\nrejection_basis: overfit\nstage: backtest\n"
        )
        frontmatter.remove_keys(folder, ["rejection_basis"])
        text = (folder / "idea.md").read_text(encoding="utf-8")
        assert text == "---\nid: A\n# keep me\nstage: backtest\n---\n\nBody\n"

    def test_removes_duplicates_and_continuation_lines(self, tmp_path):
        folder = self._folder(tmp_path, "tags: a\ntags:\n  - b\n  - c\nid: A\n")
        frontmatter.remove_keys(folder, ["tags"])
        assert (folder / "idea.md").read_text(encoding="utf-8") == "---\nid: A\n---\n\nBody\n"

    def test_absent_key_changes_nothing(self, tmp_path):
        folder = self._folder(tmp_path, "id: A\n")
        before = (folder / "idea.md").read_bytes()
        frontmatter.remove_keys(folder, ["nope"])
        assert (folder / "idea.md").read_bytes() == before

    def test_does_not_remove_a_longer_key(self, tmp_path):
        folder = self._folder(tmp_path, "stage_note: x\nstage: backtest\n")
        frontmatter.remove_keys(folder, ["stage"])
        assert "stage_note: x" in (folder / "idea.md").read_text(encoding="utf-8")

    def test_crlf_is_preserved(self, tmp_path):
        (tmp_path / "idea.md").write_bytes(b"---\r\nid: A\r\nold: 1\r\n---\r\n\r\nBody\r\n")
        frontmatter.remove_keys(tmp_path, ["old"])
        assert (tmp_path / "idea.md").read_bytes() == b"---\r\nid: A\r\n---\r\n\r\nBody\r\n"

    def test_bad_key_name_is_rejected(self, tmp_path):
        folder = self._folder(tmp_path, "id: A\n")
        with pytest.raises(frontmatter.FrontmatterError):
            frontmatter.remove_keys(folder, ["bad key"])
