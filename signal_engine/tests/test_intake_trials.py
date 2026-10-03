"""The append-only trial ledger: counting rule, validation and append-only writes."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from signal_engine.research.intake import trials
from signal_engine.research.intake.trials import TrialCount, TrialError
from signal_engine.timeutils import IST

NOW = datetime(2026, 10, 4, 10, 0, tzinfo=IST)
KEYS = ("rules", "params", "execution", "cost")


def _fp(seed: str = "a") -> dict:
    return {k: (seed + str(i)) * 32 for i, k in enumerate(KEYS)}


def _file(tmp_path):
    return tmp_path / "trials.yaml"


def _raw(tmp_path, text: str) -> None:
    _file(tmp_path).write_text(text, encoding="utf-8")


def _material_block(n=1, fp=None) -> str:
    fp = fp or _fp()
    lines = [f"- trial: {n}", "  kind: material", "  run_id: r1", "  fingerprints:"]
    lines += [f"    {k}: {fp[k]}" for k in KEYS]
    lines.append("  created_ist: '2026-10-04T10:00:00+05:30'")
    return "\n".join(lines) + "\n"


class TestCounting:
    def test_empty_folder_counts_zero(self, tmp_path):
        assert trials.count(tmp_path) == TrialCount(0, 0, 0)
        assert trials.load_trials(tmp_path) == []

    def test_distinct_tuples_each_add_one(self, tmp_path):
        first = trials.register_material(tmp_path, _fp("a"), "r1", now=NOW)
        second = trials.register_material(tmp_path, _fp("b"), "r2", now=NOW)
        assert (first.trial, first.created) == (1, True)
        assert (second.trial, second.created) == (2, True)
        assert trials.count(tmp_path) == TrialCount(2, 0, 2)

    def test_same_tuple_twice_adds_nothing(self, tmp_path):
        trials.register_material(tmp_path, _fp(), "r1", now=NOW)
        before = _file(tmp_path).read_bytes()
        again = trials.register_material(tmp_path, _fp(), "r2", now=NOW)
        assert again.created is False and again.trial == 1
        assert again.count.total == 1
        assert _file(tmp_path).read_bytes() == before

    def test_extra_keys_are_ignored(self, tmp_path):
        trials.register_material(tmp_path, _fp(), "r1", now=NOW)
        extra = {**_fp(), "dataset": "f" * 64, "profile": "e" * 64, "strategy": "d" * 64}
        again = trials.register_material(tmp_path, extra, "r2", now=NOW)
        assert again.created is False
        assert "dataset" not in _file(tmp_path).read_text()

    def test_universe_alone_is_not_a_trial(self, tmp_path):
        # a data refresh that adds a symbol changes the universe fingerprint, not the trial
        trials.register_material(tmp_path, _fp(), "r1", now=NOW)
        refreshed = {**_fp(), "universe": "9" * 64}
        again = trials.register_material(tmp_path, refreshed, "r2", now=NOW)
        assert again.created is False and again.count.total == 1
        assert "universe" not in _file(tmp_path).read_text()

    def test_retry_with_the_same_run_id_never_double_counts(self, tmp_path):
        first = trials.register_material(tmp_path, _fp(), "r1", now=NOW)
        retry = trials.register_material(tmp_path, _fp(), "r1", now=NOW)
        assert (retry.trial, retry.created) == (first.trial, False)
        assert trials.count(tmp_path).total == 1

    def test_changing_any_one_key_is_a_new_trial(self, tmp_path):
        trials.register_material(tmp_path, _fp(), "r1", now=NOW)
        for key in KEYS:
            changed = {**_fp(), key: "9" * 64}
            assert trials.register_material(tmp_path, changed, "r", now=NOW).created
            trials_before = trials.count(tmp_path).material
            assert trials.register_material(tmp_path, changed, "r", now=NOW).created is False
            assert trials.count(tmp_path).material == trials_before

    def test_missing_key_raises(self, tmp_path):
        partial = _fp()
        del partial["cost"]
        with pytest.raises(TrialError, match="cost"):
            trials.register_material(tmp_path, partial, "r1", now=NOW)

    def test_bad_fingerprint_rejected_before_writing(self, tmp_path):
        with pytest.raises(TrialError):
            trials.register_material(tmp_path, {**_fp(), "rules": "xyz"}, "r1", now=NOW)
        assert not _file(tmp_path).exists()

    def test_externals_add_to_total(self, tmp_path):
        trials.register_material(tmp_path, _fp(), "r1", now=NOW)
        result = trials.add_external(tmp_path, 12, "tuned in a notebook", now=NOW)
        assert result.trial == 2 and result.created is True
        assert result.count == TrialCount(1, 12, 13)
        trials.add_external(tmp_path, 3, "more", now=NOW)
        assert trials.count(tmp_path) == TrialCount(1, 15, 16)

    def test_trial_number_is_max_plus_one(self, tmp_path):
        _raw(tmp_path, _material_block(5))
        assert trials.add_external(tmp_path, 1, "x", now=NOW).trial == 6

    def test_external_validation(self, tmp_path):
        for bad in (0, -1, True, 1.5):
            with pytest.raises(TrialError):
                trials.add_external(tmp_path, bad, "why", now=NOW)
        for reason in ("", "   "):
            with pytest.raises(TrialError):
                trials.add_external(tmp_path, 1, reason, now=NOW)
        assert not _file(tmp_path).exists()


class TestWrites:
    def test_dry_run_writes_nothing(self, tmp_path):
        result = trials.register_material(tmp_path, _fp(), "r1", now=NOW, dry_run=True)
        assert result.created is True and result.trial == 1
        assert result.count.total == 1
        assert not _file(tmp_path).exists()

    def test_dry_run_known_tuple(self, tmp_path):
        trials.register_material(tmp_path, _fp(), "r1", now=NOW)
        result = trials.register_material(tmp_path, _fp(), "r2", now=NOW, dry_run=True)
        assert result.created is False

    def test_append_only_existing_bytes_never_change(self, tmp_path):
        trials.register_material(tmp_path, _fp("a"), "r1", now=NOW)
        previous = _file(tmp_path).read_bytes()
        for step in (
            lambda: trials.register_material(tmp_path, _fp("b"), "r2", now=NOW),
            lambda: trials.add_external(tmp_path, 2, "reason", now=NOW),
            lambda: trials.register_material(tmp_path, _fp("c"), "r3", now=NOW),
        ):
            step()
            current = _file(tmp_path).read_bytes()
            assert current.startswith(previous) and len(current) > len(previous)
            previous = current

    def test_comments_and_missing_trailing_newline_preserved(self, tmp_path):
        head = "# ledger\n" + _material_block(1).rstrip("\n")
        _raw(tmp_path, head)
        trials.add_external(tmp_path, 1, "x", now=NOW)
        text = _file(tmp_path).read_text()
        assert text.startswith(head) and len(trials.load_trials(tmp_path)) == 2

    def test_created_is_ist_isoformat(self, tmp_path):
        utc = datetime(2026, 10, 4, 4, 30, tzinfo=UTC)
        trials.register_material(tmp_path, _fp(), "r1", now=utc)
        entry = trials.load_trials(tmp_path)[0]
        assert entry.created_ist == "2026-10-04T10:00:00+05:30"

    def test_naive_now_rejected(self, tmp_path):
        naive = datetime(2026, 10, 4, 10, 0)
        with pytest.raises(ValueError):
            trials.register_material(tmp_path, _fp(), "r1", now=naive)
        with pytest.raises(ValueError):
            trials.add_external(tmp_path, 1, "x", now=naive)
        assert not _file(tmp_path).exists()

    def test_round_trip_fields(self, tmp_path):
        trials.register_material(tmp_path, _fp(), "run-9", now=NOW)
        trials.add_external(tmp_path, 4, "sweep", now=NOW)
        material, external = trials.load_trials(tmp_path)
        assert material.kind == "material" and material.run_id == "run-9"
        assert material.fingerprints == _fp()
        assert external.kind == "external" and external.count == 4
        assert external.reason == "sweep"

    def test_flow_style_list_refused_on_append(self, tmp_path):
        _raw(tmp_path, "[]\n")
        with pytest.raises(TrialError, match="block-style"):
            trials.add_external(tmp_path, 1, "x", now=NOW)
        assert _file(tmp_path).read_text() == "[]\n"


class TestMalformed:
    @pytest.mark.parametrize(
        "text",
        [
            "a: 1\n",
            "- 5\n",
            "- trial: 1\n  kind: other\n  created_ist: x\n",
            "- trial: 1\n  kind: external\n  count: 0\n  reason: r\n  created_ist: x\n",
            "- trial: 1\n  kind: external\n  count: 1\n  reason: ''\n  created_ist: x\n",
            "- trial: 1\n  kind: external\n  count: 1\n  reason: r\n  created_ist: 5\n",
            "- trial: 1\n  kind: external\n  count: 1\n  reason: r\n",
            "- trial: 0\n  kind: external\n  count: 1\n  reason: r\n  created_ist: x\n",
            "- [unclosed\n",
        ],
    )
    def test_invalid_entries_raise(self, tmp_path, text):
        _raw(tmp_path, text)
        with pytest.raises(TrialError):
            trials.load_trials(tmp_path)

    def test_bad_fingerprint(self, tmp_path):
        _raw(tmp_path, _material_block(1, {**_fp(), "rules": "short"}))
        with pytest.raises(TrialError, match="fingerprint"):
            trials.load_trials(tmp_path)

    def test_duplicate_trial_number(self, tmp_path):
        ext = "- trial: 1\n  kind: external\n  count: 1\n  reason: r\n  created_ist: x\n"
        _raw(tmp_path, ext + ext)
        with pytest.raises(TrialError, match="duplicate trial"):
            trials.load_trials(tmp_path)

    def test_trial_numbers_must_increase(self, tmp_path):
        _raw(tmp_path, _material_block(2, _fp("a")) + _material_block(1, _fp("b")))
        with pytest.raises(TrialError, match="order"):
            trials.load_trials(tmp_path)

    def test_duplicate_material_tuple(self, tmp_path):
        _raw(tmp_path, _material_block(1) + _material_block(2))
        with pytest.raises(TrialError, match="duplicate"):
            trials.load_trials(tmp_path)

    def test_malformed_file_refuses_append_and_is_untouched(self, tmp_path):
        _raw(tmp_path, "a: 1\n")
        with pytest.raises(TrialError):
            trials.add_external(tmp_path, 1, "x", now=NOW)
        with pytest.raises(TrialError):
            trials.register_material(tmp_path, _fp(), "r", now=NOW)
        assert _file(tmp_path).read_text() == "a: 1\n"
