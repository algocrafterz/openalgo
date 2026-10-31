"""The /logs/bugs page reads the signal_engine tracker file and never writes it."""

import json

import pytest

from blueprints.bug_tracker import TRACKER_PATH, read_tracker


def test_points_at_the_signal_engine_tracker():
    assert TRACKER_PATH.replace("\\", "/").endswith("signal_engine/bugs/bugs.json")


def test_a_missing_file_is_an_empty_list(tmp_path):
    assert read_tracker(str(tmp_path / "none.json")) == []


def test_returns_the_bugs(tmp_path):
    path = tmp_path / "bugs.json"
    path.write_text(json.dumps({"version": 1, "bugs": [{"id": "BUG-001"}]}))
    assert read_tracker(str(path)) == [{"id": "BUG-001"}]


def test_a_file_that_is_not_a_tracker_is_refused(tmp_path):
    path = tmp_path / "bugs.json"
    path.write_text(json.dumps([1, 2]))
    with pytest.raises(ValueError):
        read_tracker(str(path))
