"""Session-coverage safeguard, part 1: the pure coverage module and the data layer.

Hand-built frames and calendars. Covers per-symbol missing full sessions (counts, ranges, split
and out-of-scope subtraction), partial sessions counted separately, the coverage classes, the
session basis (default by basket size, common intersection, own), the `session_after_gap`
column, the record block, the registry validator, the fingerprint and the run comparison.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import compare as compare_mod
from signal_engine.backtest import fingerprints as fp
from signal_engine.backtest import record_run as rr
from signal_engine.backtest import session_coverage as sc
from signal_engine.backtest.data_collect import DataCollector
from signal_engine.backtest.data_readiness import policy as data_policy
from signal_engine.tests.test_data_session_integration import (  # noqa: F401
    SETTINGS,
    store,
    store_db,
)

IST = "Asia/Kolkata"
D = dt.date

#: Mon 1 Jun to Fri 19 Jun 2026: 15 weekdays. 5 Jun is a declared out-of-scope session (a stand-in
#: for Muhurat), so the run calendar has 14 sessions.
ALL = tuple(D(2026, 6, d) for d in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16, 17, 18, 19))
MUHURAT = D(2026, 6, 5)
CAL = tuple(d for d in ALL if d != MUHURAT)
OFFICIAL = SimpleNamespace(
    version="stub-official-v1",
    expected_sessions=lambda start, end: tuple(d for d in ALL if start <= d <= end),
)

POLICY = data_policy.load_policy()


def bars(days, n: int = 3) -> pd.DataFrame:
    stamps = [
        pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15 + 5 * i) for d in days for i in range(n)
    ]
    idx = pd.DatetimeIndex(stamps).tz_localize(IST)
    c = np.linspace(100, 101, len(idx))
    return pd.DataFrame(
        {"Open": c, "High": c + 0.1, "Low": c - 0.1, "Close": c, "Volume": 100.0}, index=idx
    )


def without(*gone):
    return [d for d in CAL if d not in gone]


def _layer(frames, collector=None, basis=None, policy=POLICY, cfg=None, **kw):
    return rr._build_data_layer(
        frames,
        collector or DataCollector(),
        cfg or {},
        official=OFFICIAL,
        out_of_scope=[MUHURAT],
        coverage_policy=policy,
        session_basis=basis,
        **kw,
    )


class TestPolicyConfig:
    def test_thresholds_come_from_the_data_policy(self):
        assert POLICY.incomplete_session_share == pytest.approx(0.01)
        assert POLICY.severe_session_share == pytest.approx(0.05)
        assert POLICY.common_basis_max_symbols == 50
        assert POLICY.listed_symbols == 25

    @pytest.mark.parametrize(
        "key,value",
        [
            ("incomplete_session_share", 1.5),
            ("severe_session_share", -0.1),
            ("common_basis_max_symbols", 0),
            ("listed_symbols", 0),
            ("incomplete_session_share", 0.2),  # above severe_session_share
        ],
    )
    def test_bad_values_fail_fast(self, tmp_path, key, value):
        import yaml

        doc = yaml.safe_load(data_policy.DEFAULT_PATH.read_text())
        doc["session_coverage"][key] = value
        path = tmp_path / "p.yaml"
        path.write_text(yaml.safe_dump(doc))
        with pytest.raises(data_policy.PolicyError):
            data_policy.load_policy(path)

    def test_a_missing_key_has_no_in_code_default(self, tmp_path):
        import yaml

        doc = yaml.safe_load(data_policy.DEFAULT_PATH.read_text())
        del doc["session_coverage"]["listed_symbols"]
        path = tmp_path / "p.yaml"
        path.write_text(yaml.safe_dump(doc))
        with pytest.raises(data_policy.PolicyError, match="listed_symbols"):
            data_policy.load_policy(path)


class TestMissingFullSessions:
    def test_two_missing_sessions_are_counted_and_compressed(self):
        loaded = set(without(D(2026, 6, 3), D(2026, 6, 4)))
        row = sc.symbol_coverage("A", loaded, CAL)
        assert row["calendar_sessions"] == 14
        assert row["loaded_sessions"] == 12
        assert row["missing_full_sessions"]["count"] == 2
        assert row["missing_full_sessions"]["ranges"] == ["2026-06-03..2026-06-04"]
        assert row["coverage_share"] == pytest.approx(12 / 14, abs=1e-6)

    def test_a_weekend_does_not_split_a_range_but_a_loaded_session_does(self):
        loaded = set(without(D(2026, 6, 12), D(2026, 6, 15), D(2026, 6, 17)))
        row = sc.symbol_coverage("A", loaded, CAL)
        assert row["missing_full_sessions"]["ranges"] == [
            "2026-06-12..2026-06-15",
            "2026-06-17",
        ]

    def test_split_excluded_sessions_are_subtracted_not_counted_missing(self):
        gone = (D(2026, 6, 3), D(2026, 6, 10))
        loaded = set(without(*gone))
        row = sc.symbol_coverage("A", loaded, CAL, split_excluded=[D(2026, 6, 10)])
        assert row["split_excluded"] == ["2026-06-10"]
        assert row["missing_full_sessions"]["count"] == 1
        assert row["missing_full_sessions"]["ranges"] == ["2026-06-03"]
        # 14 sessions, one split-excluded: coverage is judged on the other 13
        assert row["coverage_share"] == pytest.approx(12 / 13, abs=1e-6)

    def test_muhurat_out_of_scope_is_never_missing(self):
        # the run calendar already left the declared out-of-scope session out
        row = sc.symbol_coverage("A", set(CAL), CAL)
        assert row["missing_full_sessions"]["count"] == 0
        assert MUHURAT not in CAL
        assert row["coverage_share"] == 1.0

    def test_sessions_before_first_and_after_last_are_outside_the_span_not_missing(self):
        loaded = {d for d in CAL if D(2026, 6, 9) <= d <= D(2026, 6, 16)}
        row = sc.symbol_coverage("A", loaded, CAL)
        assert row["first_session"] == "2026-06-09"
        assert row["missing_full_sessions"]["count"] == 0
        assert row["outside_span_sessions"] == 14 - 6

    def test_partial_sessions_are_separate_and_taken_from_the_audit(self):
        row = sc.symbol_coverage(
            "A",
            set(CAL),
            CAL,
            partial={"sessions": 14, "incomplete_sessions": 3, "missing_bars": 17},
        )
        assert row["missing_full_sessions"]["count"] == 0
        assert row["partial_sessions"] == {"audited": True, "count": 3, "missing_bars": 17}

    def test_unaudited_partial_numbers_are_unknown_not_zero(self):
        row = sc.symbol_coverage("A", set(CAL), CAL)
        assert row["partial_sessions"] == {"audited": False, "count": None, "missing_bars": None}

    def test_compress_isolated_and_empty(self):
        out = sc.compress_sessions([D(2026, 6, 2), D(2026, 6, 4)], CAL)
        assert out == ["2026-06-02", "2026-06-04"]
        assert sc.compress_sessions([], CAL) == []


class TestDqReuse:
    def test_dq_report_carries_per_symbol_completeness_for_the_block(self):
        from signal_engine.backtest.dq_run import build_data_quality

        full = bars(CAL, n=75)
        short = bars(CAL, n=75)
        day = D(2026, 6, 9)
        short = short.drop(short.index[short.index.date == day][:10])
        col = DataCollector()
        col.interval = "5m"
        report = build_data_quality(
            {"A": full, "B": short}, col, calendar=CAL, expected_bars_for=lambda d: 75
        )
        comp = report["summary"]["session_completeness"]
        assert comp["A"] == {"sessions": 14, "incomplete_sessions": 0, "missing_bars": 0}
        assert comp["B"] == {"sessions": 14, "incomplete_sessions": 1, "missing_bars": 10}


class TestCoverageClass:
    @staticmethod
    def _row(missing, expected=100, partial=0):
        """A symbol row from the real builder: `expected` sessions, the first `missing` absent."""
        days = [dt.date(2000, 1, 1) + dt.timedelta(days=i) for i in range(expected + 2)]
        # the first and last session are loaded so the window is the whole calendar
        loaded = set(days) - set(days[1 : 1 + missing])
        audit = {"sessions": expected, "incomplete_sessions": partial, "missing_bars": 0}
        row = sc.symbol_coverage("X", loaded, days, partial=audit)
        assert row["expected_sessions_after_exclusions"] == expected + 2
        return row

    @staticmethod
    def _cls(row):
        return sc.symbol_class(row, POLICY.incomplete_session_share, POLICY.severe_session_share)

    def test_complete_needs_no_missing_session_and_no_partial_session(self):
        assert self._cls(self._row(0)) == sc.COMPLETE
        assert self._cls(self._row(0, partial=1)) == sc.MINOR_GAPS

    @pytest.mark.parametrize(
        ("percent", "expected_class"),
        [
            (0.0, sc.COMPLETE),
            (0.99, sc.MINOR_GAPS),
            (1.0, sc.MINOR_GAPS),
            (1.01, sc.MATERIAL_GAPS),
            (5.0, sc.MATERIAL_GAPS),
            (5.01, sc.SEVERE_GAPS),
        ],
    )
    def test_threshold_boundaries_are_inclusive_upward(self, percent, expected_class):
        # 10000 expected sessions after exclusions: percent * 100 missing sessions
        days = [dt.date(1990, 1, 1) + dt.timedelta(days=i) for i in range(10_000)]
        n_missing = round(percent * 100)
        loaded = set(days) - set(days[1 : 1 + n_missing])
        row = sc.symbol_coverage("X", loaded, days)
        assert row["expected_sessions_after_exclusions"] == 10_000
        assert row["missing_full_sessions"]["count"] == n_missing
        assert row["missing_share"] == pytest.approx(percent / 100, abs=1e-9)
        assert self._cls(row) == expected_class

    def test_exact_one_and_five_percent_on_a_small_denominator(self):
        one = self._row(1, expected=98)  # 1 of 100
        five = self._row(5, expected=98)  # 5 of 100
        assert one["missing_share"] == 0.01 and self._cls(one) == sc.MINOR_GAPS
        assert five["missing_share"] == 0.05 and self._cls(five) == sc.MATERIAL_GAPS

    def test_denominator_excludes_split_excluded_sessions(self):
        # 100 official sessions, 2 split-excluded, 1 missing: 1 of 98, not 1 of 100
        days = [dt.date(2000, 1, 1) + dt.timedelta(days=i) for i in range(100)]
        gone = {days[10]}
        split = [days[20], days[30]]
        loaded = set(days) - gone - set(split)
        row = sc.symbol_coverage("X", loaded, days, split_excluded=split)
        assert row["expected_sessions_after_exclusions"] == 98
        assert row["missing_full_sessions"]["count"] == 1
        assert row["missing_share"] == pytest.approx(1 / 98, abs=1e-6)
        assert row["missing_full_sessions"]["share"] == row["missing_share"]
        assert row["missing_share"] != pytest.approx(1 / 100, abs=1e-6)

    def test_denominator_excludes_out_of_scope_sessions_through_the_calendar(self):
        # Muhurat (5 Jun) is not in the run calendar: 14 sessions, not 15
        block = _layer({"A": bars(without(D(2026, 6, 3)))}, basis="own").session_coverage
        row = block["symbols"][0]
        assert block["calendar"]["out_of_scope_sessions"] == ["2026-06-05"]
        assert row["expected_sessions_after_exclusions"] == 14
        assert row["missing_share"] == pytest.approx(1 / 14, abs=1e-6)

    def test_run_class_is_the_worst_symbol_with_counts_per_class(self):
        frames = {
            "A": bars(CAL),
            "B": bars(without(D(2026, 6, 3))),  # 1 of 14 = 7.1 percent
            "C": bars(CAL),
        }
        block = _layer(frames, basis="own").session_coverage
        assert block["coverage_class"] == sc.SEVERE_GAPS
        rows = {r["symbol"]: r for r in block["symbols"]}
        assert rows["A"]["class"] == sc.COMPLETE
        assert rows["B"]["class"] == sc.SEVERE_GAPS
        uni = block["universe"]
        assert (uni["symbols_complete"], uni["symbols_minor_gaps"]) == (2, 0)
        assert (uni["symbols_material_gaps"], uni["symbols_severe_gaps"]) == (0, 1)
        assert uni["expected_sessions_after_exclusions_total"] == 14 * 3
        assert uni["missing_full_sessions_total"] == 1

    def test_a_clean_run_is_complete_and_has_no_disclosure(self):
        layer = _layer({"A": bars(CAL), "B": bars(CAL)}, basis="own")
        assert layer.session_coverage["coverage_class"] == sc.COMPLETE
        assert layer.session_coverage["universe"]["missing_full_sessions_total"] == 0
        assert "disclosure" not in layer.session_coverage

    def test_disclosure_only_for_material_and_severe_runs_verbatim(self):
        days = [dt.date(1990, 1, 1) + dt.timedelta(days=i) for i in range(100)]
        cal = SimpleNamespace(trading_days=days, source="t", version="v")

        def run(n_missing, n_partial=0):
            loaded = {"S": set(days) - set(days[1 : 1 + n_missing])}
            audit = {"S": {"sessions": 100, "incomplete_sessions": n_partial, "missing_bars": 0}}
            return sc.build_block(
                loaded=loaded,
                calendar=cal,
                out_of_scope=[],
                split_excluded={},
                partial=audit,
                policy=POLICY,
                basis="own",
                basis_source="setting",
                kept_sessions=set().union(*loaded.values()),
                after_gap={},
            )

        assert "disclosure" not in run(1)  # 1 of 100: minor
        assert run(2)["coverage_class"] == sc.MATERIAL_GAPS
        assert run(2)["disclosure"] == (
            "Some trading sessions are missing from the stored history for part of this basket; "
            "results may be affected."
        )
        assert run(6)["coverage_class"] == sc.SEVERE_GAPS
        assert run(6)["disclosure"] == (
            "Many trading sessions are missing for part of this basket. Treat this result as "
            "indicative only, not as reliable historical validation."
        )

    def test_classes_never_reject_a_run(self):
        frames = {"A": bars(without(*CAL[1:-1])), "B": bars(CAL)}
        layer = _layer(frames, basis="own")
        assert layer.session_coverage["coverage_class"] == sc.SEVERE_GAPS
        rr._check_calendar_at_load(layer)  # does not raise


class TestSessionBasis:
    def test_default_is_common_up_to_the_limit_and_own_above(self):
        assert sc.resolve_basis(None, 2, 50) == ("common", "default")
        assert sc.resolve_basis(None, 50, 50) == ("common", "default")
        assert sc.resolve_basis(None, 51, 50) == ("own", "default")

    def test_single_symbol_is_always_own(self):
        assert sc.resolve_basis(None, 1, 50)[0] == "own"
        assert sc.resolve_basis("common", 1, 50)[0] == "own"

    def test_a_setting_wins_and_an_unknown_value_is_refused(self):
        assert sc.resolve_basis("own", 3, 50) == ("own", "setting")
        assert sc.resolve_basis("common", 80, 50) == ("common", "setting")
        with pytest.raises(sc.CoverageError):
            sc.resolve_basis("intersection", 3, 50)

    def test_common_removes_the_sessions_any_symbol_lacks(self):
        a_gap, b_gap = D(2026, 6, 3), D(2026, 6, 11)
        frames = {"A": bars(without(a_gap)), "B": bars(without(b_gap)), "C": bars(CAL)}
        layer = _layer(frames, basis="common")
        for sym in "ABC":
            assert set(layer.frames[sym].index.date) == set(CAL) - {a_gap, b_gap}
        eff = layer.session_coverage["effective"]
        assert layer.session_basis == "common"
        assert eff["sessions_kept"] == 12
        assert eff["sessions_excluded_by_basis"] == {
            "count": 2,
            "ranges": ["2026-06-03", "2026-06-11"],
        }
        assert eff["period"] == {"start": "2026-06-01", "end": "2026-06-19"}

    def test_common_records_the_effective_period_when_an_edge_session_goes(self):
        frames = {"A": bars(without(D(2026, 6, 1))), "B": bars(CAL)}
        layer = _layer(frames, basis="common")
        assert layer.session_coverage["effective"]["period"]["start"] == "2026-06-02"

    def test_own_leaves_frames_untouched(self):
        frames = {"A": bars(without(D(2026, 6, 3))), "B": bars(CAL)}
        before = {s: f.index.copy() for s, f in frames.items()}
        layer = _layer(frames, basis="own")
        for sym in frames:
            assert layer.frames[sym].index.equals(before[sym])
        eff = layer.session_coverage["effective"]
        assert eff["sessions_excluded_by_basis"]["count"] == 0
        # own still reports what the intersection WOULD drop, as a warning
        assert eff["common_would_drop"] == {"count": 1, "ranges": ["2026-06-03"]}
        assert any("common would drop 1" in w for w in layer.session_coverage["warnings"])

    def test_the_default_applies_by_basket_size(self):
        small = {f"S{i}": bars(CAL if i else without(D(2026, 6, 3))) for i in range(3)}
        assert _layer(small).session_basis == "common"
        policy = POLICY.__class__(**{**POLICY.__dict__, "common_basis_max_symbols": 2})
        assert _layer(small, policy=policy).session_basis == "own"

    def test_single_symbol_run_is_own_with_coverage_recorded(self):
        layer = _layer({"A": bars(without(D(2026, 6, 3)))})
        assert layer.session_basis == "own"
        block = layer.session_coverage
        assert block["session_basis"]["used"] == "own"
        assert block["universe"]["missing_full_sessions_total"] == 1
        assert block["effective"]["common_would_drop"]["count"] == 0

    def test_without_a_policy_the_frames_come_back_exactly_as_before(self):
        frames = {"A": bars(without(D(2026, 6, 3))), "B": bars(CAL)}
        layer = _layer(frames, policy=None)
        assert layer.session_coverage is None and layer.session_basis is None
        assert sc.AFTER_GAP_COLUMN not in layer.frames["A"].columns


class TestAfterGapColumn:
    def test_every_bar_of_the_first_session_after_a_missing_one_is_flagged(self):
        frames = {"A": bars(without(D(2026, 6, 3), D(2026, 6, 4)))}
        df = _layer(frames, basis="own").frames["A"]
        flagged = set(df.index.date[df[sc.AFTER_GAP_COLUMN].to_numpy()])
        # 3 and 4 Jun are missing, 5 Jun is out of scope: 8 Jun is the first loaded session after
        assert flagged == {D(2026, 6, 8)}
        assert int(df[sc.AFTER_GAP_COLUMN].sum()) == 3  # all three bars of that session
        assert df[sc.AFTER_GAP_COLUMN].dtype == bool

    def test_the_session_after_that_is_not_flagged_and_the_first_session_never_is(self):
        frames = {"A": bars(without(D(2026, 6, 1), D(2026, 6, 3)))}
        df = _layer(frames, basis="own").frames["A"]
        flagged = set(df.index.date[df[sc.AFTER_GAP_COLUMN].to_numpy()])
        assert flagged == {D(2026, 6, 4)}

    def test_a_split_excluded_session_is_a_gap_the_frame_bridges(self):
        col = DataCollector()
        col.record_split_days("A", [D(2026, 6, 10)])
        layer = _layer({"A": bars(without(D(2026, 6, 10)))}, col, basis="own")
        df = layer.frames["A"]
        assert set(df.index.date[df[sc.AFTER_GAP_COLUMN].to_numpy()]) == {D(2026, 6, 11)}
        row = layer.session_coverage["symbols"][0]
        assert row["missing_full_sessions"]["count"] == 0  # subtracted from the missing count
        assert row["after_gap_sessions"]["count"] == 1  # but the bridge is still guarded

    def test_common_basis_creates_the_gap_in_the_other_symbols_too(self):
        frames = {"A": bars(without(D(2026, 6, 10))), "B": bars(CAL)}
        b = _layer(frames, basis="common").frames["B"]
        assert set(b.index.date[b[sc.AFTER_GAP_COLUMN].to_numpy()]) == {D(2026, 6, 11)}

    def test_a_gapless_frame_has_no_flag(self):
        df = _layer({"A": bars(CAL)}, basis="own").frames["A"]
        assert not df[sc.AFTER_GAP_COLUMN].any()

    def test_carry_column_puts_it_back_after_a_prepare_that_drops_it(self):
        raw = _layer({"A": bars(without(D(2026, 6, 3)))}, basis="own").frames["A"]
        prepared = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        out = sc.carry_column(prepared, raw[sc.AFTER_GAP_COLUMN])
        assert out[sc.AFTER_GAP_COLUMN].to_numpy().sum() == int(raw[sc.AFTER_GAP_COLUMN].sum())
        assert sc.carry_column(prepared, None) is prepared
        assert sc.carry_column(out, raw[sc.AFTER_GAP_COLUMN]) is out


class TestBlock:
    def test_block_is_json_serialisable_and_stable(self):
        frames = {"A": bars(without(D(2026, 6, 3))), "B": bars(CAL)}
        one = _layer(copy.deepcopy(frames)).session_coverage
        two = _layer(copy.deepcopy(frames)).session_coverage
        assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
        assert json.loads(json.dumps(one)) == one

    def test_block_states_the_basis_period_exclusions_and_continuity(self):
        block = _layer({"A": bars(without(D(2026, 6, 3))), "B": bars(CAL)}).session_coverage
        assert block["session_basis"]["used"] == "common"
        assert block["session_basis"]["source"] == "default"
        assert block["calendar"]["out_of_scope_sessions"] == ["2026-06-05"]
        assert block["effective"]["sessions_excluded_by_basis"]["count"] == 1
        assert "NOT reset" in block["continuity_note"]
        assert block["thresholds"]["incomplete_session_share"] == pytest.approx(0.01)
        defs = block["definitions"]
        assert defs["missing_full_sessions"] != defs["partial_sessions"]

    def test_large_universe_lists_only_the_worst_and_aggregates_all(self):
        frames = {f"S{i:02d}": bars(CAL) for i in range(30)}
        frames["S07"] = bars(without(D(2026, 6, 3), D(2026, 6, 4)))
        frames["S21"] = bars(without(D(2026, 6, 9)))
        block = _layer(frames, basis="own").session_coverage
        assert block["symbols_listed"] == 25 and block["symbols_not_listed"] == 5
        assert [r["symbol"] for r in block["symbols"][:2]] == ["S07", "S21"]
        assert block["universe"]["symbols"] == 30
        assert block["universe"]["missing_full_sessions_total"] == 3

    def test_partial_numbers_come_from_the_dq_report(self):
        col = DataCollector()
        col.interval = "5m"
        full = bars(CAL, n=75)
        short = bars(CAL, n=75)
        short = short.drop(short.index[short.index.date == D(2026, 6, 9)][:10])
        layer = _layer({"A": full, "B": short}, col, basis="own", expected_bars_for=lambda d: 75)
        rows = {r["symbol"]: r for r in layer.session_coverage["symbols"]}
        assert rows["B"]["partial_sessions"] == {"audited": True, "count": 1, "missing_bars": 10}
        assert rows["B"]["missing_full_sessions"]["count"] == 0
        assert layer.session_coverage["universe"]["missing_bars_total"] == 10
        assert layer.session_coverage["coverage_class"] == sc.MINOR_GAPS


class TestRecordAndRegistry:
    @staticmethod
    def _record(**kw):
        from signal_engine.backtest.tearsheet import build_run_record

        return build_run_record(
            [],
            run_id="r",
            run_date="2026-11-05",
            code_version="v",
            data={"source": "t"},
            period={"start": "2026-06-01", "end": "2026-06-19"},
            costs={"round_trip_bps": 0.0, "model": "flat"},
            **kw,
        )

    def test_record_carries_the_block_and_the_registry_accepts_it(self):
        from signal_engine.research.build_registry import _validate_run_v2

        block = _layer({"A": bars(CAL), "B": bars(CAL)}).session_coverage
        record = self._record(session_coverage=block)
        assert record["session_coverage"]["coverage_class"] == sc.COMPLETE
        assert _validate_run_v2(record) == []

    @pytest.mark.parametrize("cls", ["COMPLETE", "MINOR_GAPS", "MATERIAL_GAPS", "SEVERE_GAPS"])
    def test_the_registry_accepts_each_new_class_and_refuses_the_old_names(self, cls):
        from signal_engine.research.build_registry import _validate_run_v2

        good = _layer({"A": bars(CAL), "B": bars(CAL)}).session_coverage
        assert _validate_run_v2({"session_coverage": {**good, "coverage_class": cls}}) == []
        for old in ("INCOMPLETE_USABLE", "GAPS_MATERIAL"):
            assert _validate_run_v2({"session_coverage": {**good, "coverage_class": old}})

    def test_an_older_record_without_the_block_still_validates(self):
        from signal_engine.research.build_registry import _validate_run_v2

        record = self._record()
        assert "session_coverage" not in record
        assert _validate_run_v2(record) == []
        assert _validate_run_v2({"n_trials": 1, "promoted": False}) == []

    def test_malformed_blocks_are_rejected(self):
        from signal_engine.research.build_registry import _validate_run_v2

        good = _layer({"A": bars(CAL), "B": bars(CAL)}).session_coverage
        assert _validate_run_v2({"session_coverage": []})
        assert _validate_run_v2({"session_coverage": {**good, "coverage_class": "FINE"}})
        assert _validate_run_v2({"session_coverage": {**good, "session_basis": {"used": "union"}}})
        no_note = {k: v for k, v in good.items() if k != "continuity_note"}
        assert _validate_run_v2({"session_coverage": no_note})

    def test_the_block_reaches_the_run_artifact_report(self):
        from signal_engine.backtest import run_artifact

        assert "session_coverage" in run_artifact._REPORT_BLOCKS


class TestFingerprint:
    RESOLVED = {
        "execution.warmup_bars": {"value": 5, "ledger_id": "x"},
        "execution.slippage_bps_per_side": {"value": 4, "ledger_id": "s"},
    }

    def test_common_changes_the_execution_fingerprint(self):
        assert fp.execution_fingerprint(self.RESOLVED, "common") != fp.execution_fingerprint(
            self.RESOLVED
        )

    def test_own_and_unset_keep_every_older_fingerprint(self):
        base = fp.execution_fingerprint(self.RESOLVED)
        assert fp.execution_fingerprint(self.RESOLVED, "own") == base
        assert fp.execution_fingerprint(self.RESOLVED, None) == base

    def test_component_fingerprints_carry_it_and_the_change_class_sees_it(self):
        from signal_engine.backtest import change_class

        def comps(basis):
            return fp.component_fingerprints(
                intent={"entry": "x"},
                resolved=self.RESOLVED,
                cost_model=None,
                symbols=["A"],
                universe_definition="u",
                profile_content={"p": 1},
                dataset_fingerprint="d",
                session_basis=basis,
            )

        own, common = comps("own"), comps("common")
        assert own["execution"] != common["execution"]
        assert {k: v for k, v in own.items() if k != "execution"} == {
            k: v for k, v in common.items() if k != "execution"
        }
        got = change_class.classify({"fingerprints": own}, {"fingerprints": common})
        assert got == [change_class.EXECUTION_CHANGE]


class TestCompare:
    @staticmethod
    def _run(basis, kept, sha, period=("2026-06-01", "2026-06-19")):
        block = {
            "session_basis": {"used": basis},
            "coverage_class": "COMPLETE",
            "effective": {
                "period": {"start": period[0], "end": period[1]},
                "sessions_kept": kept,
                "sessions_sha256": sha,
            },
        }
        return SimpleNamespace(report={"session_coverage": block}, period=period)

    def test_different_bases_are_incomparable_with_a_plain_reason(self):
        out = compare_mod._session_comparability(
            self._run("common", 12, "a"), self._run("own", 14, "b")
        )
        assert out["comparable"] is False
        assert "different session bases" in out["reason"]
        assert "12" in out["reason"] and "14" in out["reason"]

    def test_same_basis_same_period_different_sets_is_incomparable(self):
        out = compare_mod._session_comparability(
            self._run("own", 12, "a"), self._run("own", 13, "b")
        )
        assert out["comparable"] is False and "different session sets" in out["reason"]

    def test_identical_sets_are_comparable(self):
        out = compare_mod._session_comparability(
            self._run("common", 12, "a"), self._run("common", 12, "a")
        )
        assert out["comparable"] is True and out["reason"] is None

    def test_different_periods_are_left_to_the_period_warning(self):
        out = compare_mod._session_comparability(
            self._run("own", 12, "a"), self._run("own", 14, "b", ("2026-05-01", "2026-06-19"))
        )
        assert out["comparable"] is True

    def test_an_older_run_leaves_comparability_unknown(self):
        old = SimpleNamespace(report={}, period=("2026-06-01", "2026-06-19"))
        out = compare_mod._session_comparability(old, self._run("own", 12, "a"))
        assert out["comparable"] is None and "predate" in out["reason"]


class TestExecuteBacktestWiring:
    """The real funnel on the integration store (symbols that list on different dates)."""

    def test_the_run_records_coverage_and_the_basis_it_used(self, store):  # noqa: F811
        exe = rr.execute_backtest(
            "k", settings={**SETTINGS, "session_basis": "own"}, dataset=store["res"]
        )
        block = exe.session_coverage
        assert exe.session_basis == "own" and block["session_basis"]["used"] == "own"
        assert block["session_basis"]["source"] == "setting"
        assert block["universe"]["symbols"] == 6
        # BBB's split day is dropped by the loader, so the frame bridges it: its next session
        # takes no entries, and the count is the ledger's
        excl = exe.ledgers["exclusions"]
        in_ledger = int(excl.loc[excl["reason_code"] == "SESSION_AFTER_GAP", "count"].sum())
        assert block["entries_skipped_session_after_gap"] == in_ledger > 0
        bbb = next(r for r in block["symbols"] if r["symbol"] == "BBB")
        assert bbb["split_excluded"] and bbb["missing_full_sessions"]["count"] == 0
        assert bbb["after_gap_sessions"]["count"] >= 1
        assert json.loads(json.dumps(block)) == block

    def test_the_default_for_a_small_basket_is_common_and_shrinks_the_period(self, store):  # noqa: F811
        exe = rr.execute_backtest("k", settings=SETTINGS, run_truncation=False, dataset=store["res"])
        block = exe.session_coverage
        assert exe.session_basis == "common"
        assert block["session_basis"]["source"] == "default"
        assert block["effective"]["sessions_excluded_by_basis"]["count"] > 0
        # the effective period is what the engine saw
        assert exe.period == {
            "start": block["effective"]["period"]["start"],
            "end": block["effective"]["period"]["end"],
        }
        assert any("basis common removed" in w for w in block["warnings"])
        assert exe.ledger_problems == []

    def test_a_bad_basis_is_refused_before_the_engine(self, store):  # noqa: F811
        with pytest.raises(sc.CoverageError):
            rr.execute_backtest(
                "k", settings={**SETTINGS, "session_basis": "union"}, dataset=store["res"]
            )

    def test_the_basis_reaches_the_fingerprint_inputs(self):
        exe = SimpleNamespace(
            symbols=["A"], data={"universe": "u"}, session_basis="common"
        )
        frozen = SimpleNamespace(
            intent={"entry": "x"},
            resolved={"execution.slippage_bps_per_side": {"value": 4, "ledger_id": "s"}},
            profile={"p": 1},
        )
        from signal_engine.backtest import run_artifact

        common = run_artifact.material_fingerprints(frozen, exe, None)
        exe.session_basis = "own"
        own = run_artifact.material_fingerprints(frozen, exe, None)
        exe.session_basis = None
        legacy = run_artifact.material_fingerprints(frozen, exe, None)
        assert common["execution"] != own["execution"]
        assert own == legacy
        assert common["rules"] == own["rules"] and common["cost"] == own["cost"]
