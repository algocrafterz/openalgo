"""Data-readiness rules: one passing and one failing synthetic store per rule (plan section 5.3)."""

from __future__ import annotations

import itertools
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from signal_engine.backtest.data_readiness import policy as pol
from signal_engine.backtest.data_readiness import rules as R
from signal_engine.tests.dq_fixtures import clean_daily, daily, make_store, minute, weekdays

POLICY = pol.load_policy(Path(__file__).resolve().parents[1] / "research" / "data_policy.yaml")
DAYS = weekdays("2026-01-05", "2026-01-30")  # 20 sessions
_SEQ = itertools.count()


def _ctx(calendar=None):
    cal = tuple(date.fromisoformat(d) for d in (calendar or DAYS))
    return R.Context(policy=POLICY, calendar=cal)


def _run(tmp_path, rows, rule_id, interval="D", calendar=None, symbols=None):
    store = make_store(tmp_path / f"s{next(_SEQ)}.duckdb", rows)
    with R.connect_readonly(store) as con:
        return R.evaluate(con, R.Scope(interval=interval, symbols=symbols), rule_id,
                          _ctx(calendar))


def _pairs(res):
    return sorted(zip(res.evidence["symbol"], res.evidence["session"].astype(str), strict=True))


class TestCatalogue:
    def test_every_rule_has_dimension_action_and_rationale(self):
        for rule in R.RULES.values():
            assert rule.dimension in R.DIMENSIONS
            assert isinstance(rule.action, R.Action)
            assert len(rule.rationale) > 20, rule.rule_id
            assert rule.version >= 1

    def test_unknown_rule_raises(self, tmp_path):
        with pytest.raises(KeyError):
            _run(tmp_path, [], "NOPE")

    def test_rule_for_other_interval_is_not_evaluated(self, tmp_path):
        res = _run(tmp_path, [], "CANON-DAILY-TS", interval="1m")
        assert res.status == R.NOT_EVALUATED


class TestIntegrity:
    def test_clean_daily_has_no_duplicates(self, tmp_path):
        res = _run(tmp_path, clean_daily("A", DAYS), "SNAP-DUP-KEY")
        assert res.status == R.EVALUATED and res.rows == 0

    def test_sbin_incident_duplicates_are_found(self, tmp_path):
        rows = clean_daily("SBIN", DAYS) + [
            daily("SBIN", "2026-01-06", hhmm="11:00"),
            daily("SBIN", "2026-01-06", hhmm="16:30", o=210, h=210, lo=210, c=205, v=2),
        ]
        res = _run(tmp_path, rows, "SNAP-DUP-KEY")
        assert res.rows == 2
        assert _pairs(res) == [("SBIN", "2026-01-06")]
        assert res.action is R.Action.BLOCK

    def test_minute_duplicate_timestamp_across_exchanges(self, tmp_path):
        rows = [minute("A", "2026-01-05", "09:15"),
                minute("A", "2026-01-05", "09:15", exchange="BSE")]
        res = _run(tmp_path, rows, "SNAP-DUP-KEY", interval="1m")
        assert res.rows == 1

    def test_daily_timestamp_not_0530(self, tmp_path):
        rows = clean_daily("A", DAYS[:-1]) + [daily("A", DAYS[-1], hhmm="11:00")]
        res = _run(tmp_path, rows, "CANON-DAILY-TS")
        assert _pairs(res) == [("A", DAYS[-1])]

    def test_exchange_ambiguity(self, tmp_path):
        rows = clean_daily("A", DAYS[:2]) + [daily("A", DAYS[2], exchange="BSE")]
        res = _run(tmp_path, rows, "SNAP-EXCHANGE")
        assert res.rows == 1 and list(res.evidence["symbol"]) == ["A"]

    def test_exchange_ok(self, tmp_path):
        assert _run(tmp_path, clean_daily("A", DAYS), "SNAP-EXCHANGE").rows == 0


class TestValidity:
    @pytest.mark.parametrize("rule_id,bad", [
        ("VAL-OHLC", {"h": 98.0}),               # high below open and close
        ("VAL-OHLC", {"lo": 102.0}),             # low above close
        ("VAL-NONPOS", {"lo": 0.0}),
        ("VAL-NEG-VOL", {"v": -5}),
    ])
    def test_bad_bar_is_one_session(self, tmp_path, rule_id, bad):
        rows = clean_daily("A", DAYS[:-1]) + [daily("A", DAYS[-1], **bad)]
        res = _run(tmp_path, rows, rule_id)
        assert _pairs(res) == [("A", DAYS[-1])]
        assert res.action is R.Action.EXCLUDE_SESSION

    def test_null_ohlc_row(self, tmp_path):
        rows = clean_daily("A", DAYS[:-1]) + [daily("A", DAYS[-1], c=None)]
        assert _run(tmp_path, rows, "VAL-NULL-ROW").rows == 1

    @pytest.mark.parametrize("rule_id", ["VAL-OHLC", "VAL-NONPOS", "VAL-NEG-VOL", "VAL-NULL-ROW"])
    def test_clean_series_passes(self, tmp_path, rule_id):
        assert _run(tmp_path, clean_daily("A", DAYS), rule_id).rows == 0

    def test_minute_rules_aggregate_to_sessions(self, tmp_path):
        rows = [minute("A", "2026-01-05", "09:15", v=-1), minute("A", "2026-01-05", "09:16", v=-2)]
        res = _run(tmp_path, rows, "VAL-NEG-VOL", interval="1m")
        assert res.rows == 2
        assert list(res.evidence["rows"]) == [2]

    def test_outside_hours_is_info(self, tmp_path):
        rows = [minute("A", "2026-01-05", "09:15"), minute("A", "2026-01-05", "15:45")]
        res = _run(tmp_path, rows, "INFO-OUTSIDE-HOURS", interval="1m")
        assert res.rows == 1 and res.action is R.Action.INFO


class TestLockedAndPlaceholder:
    def test_locked_bar_with_normal_volume_is_a_warning_only(self, tmp_path):
        rows = clean_daily("A", DAYS[:-1]) + [
            daily("A", DAYS[-1], o=100, h=100, lo=100, c=100, v=1000)]
        locked = _run(tmp_path, rows, "DIAG-LOCKED")
        assert locked.rows == 1 and locked.action is R.Action.WARNING
        assert _run(tmp_path, rows, "VAL-PLACEHOLDER").rows == 0

    def test_locked_bar_with_tiny_volume_is_a_placeholder(self, tmp_path):
        # 2026-09-13 pattern: O=H=L=C and about 0.02% of normal volume
        rows = clean_daily("A", DAYS[:-1], v=5_000_000) + [
            daily("A", DAYS[-1], o=100, h=100, lo=100, c=100, v=1050)]
        res = _run(tmp_path, rows, "VAL-PLACEHOLDER")
        assert _pairs(res) == [("A", DAYS[-1])]
        assert res.action is R.Action.EXCLUDE_SESSION

    def test_placeholder_needs_history(self, tmp_path):
        rows = [daily("A", DAYS[0], o=100, h=100, lo=100, c=100, v=1)]
        assert _run(tmp_path, rows, "VAL-PLACEHOLDER").rows == 0


class TestCalendar:
    def test_panel_wide_unexpected_date_blocks(self, tmp_path):
        sunday = "2026-01-11"
        rows = []
        for s in ("A", "B", "C"):
            rows += clean_daily(s, DAYS) + [daily(s, sunday)]
        res = _run(tmp_path, rows, "CAL-UNEXPECTED-DATE")
        assert res.rows == 1
        assert list(res.evidence["session"].astype(str)) == [sunday]
        assert res.action is R.Action.BLOCK
        assert _run(tmp_path, rows, "CAL-UNEXPECTED-ISOLATED").rows == 0

    def test_isolated_unexpected_date_excludes_the_session(self, tmp_path):
        sunday = "2026-01-11"
        rows = clean_daily("A", DAYS) + clean_daily("B", DAYS) + clean_daily("C", DAYS)
        rows.append(daily("A", sunday))
        assert _run(tmp_path, rows, "CAL-UNEXPECTED-DATE").rows == 0
        iso = _run(tmp_path, rows, "CAL-UNEXPECTED-ISOLATED")
        assert _pairs(iso) == [("A", sunday)]

    def test_calendar_rules_need_a_calendar(self, tmp_path):
        store = make_store(tmp_path / "s.duckdb", clean_daily("A", DAYS))
        with R.connect_readonly(store) as con:
            res = R.evaluate(con, R.Scope(interval="D"), "CAL-UNEXPECTED-DATE",
                             R.Context(policy=POLICY, calendar=None))
        assert res.status == R.NOT_EVALUATED


class TestCompleteness:
    def test_many_missing_sessions_exclude_the_symbol(self, tmp_path):
        held = DAYS[:5] + DAYS[10:]  # 5 of 20 calendar sessions missing inside the range
        res = _run(tmp_path, clean_daily("A", held), "CMP-MISSING")
        assert list(res.evidence["symbol"]) == ["A"]
        assert res.action is R.Action.EXCLUDE_SYMBOL
        assert _run(tmp_path, clean_daily("A", held), "CMP-MISSING-FEW").rows == 0

    def test_few_missing_sessions_warn(self, tmp_path):
        cal = weekdays("2025-01-01", "2026-01-30")  # long calendar so one gap is under 2%
        held = [d for d in cal if d != cal[100]]
        res = _run(tmp_path, clean_daily("A", held), "CMP-MISSING-FEW", calendar=cal)
        assert res.rows == 1 and res.action is R.Action.WARNING
        assert _run(tmp_path, clean_daily("A", held), "CMP-MISSING", calendar=cal).rows == 0

    def test_sessions_outside_the_symbol_range_are_not_missing(self, tmp_path):
        res = _run(tmp_path, clean_daily("A", DAYS[10:]), "CMP-MISSING")
        assert res.rows == 0

    def test_latest_daily_written_during_the_session(self, tmp_path):
        last = DAYS[-1]
        rows = clean_daily("A", DAYS[:-1]) + [
            daily("A", last, created=datetime.fromisoformat(f"{last} 14:05"))]
        res = _run(tmp_path, rows, "CMP-LATEST")
        assert _pairs(res) == [("A", last)]

    def test_latest_daily_written_after_the_close(self, tmp_path):
        assert _run(tmp_path, clean_daily("A", DAYS), "CMP-LATEST").rows == 0

    def test_latest_minute_session_short(self, tmp_path):
        full = [f"{9 + (15 + m) // 60:02d}:{(15 + m) % 60:02d}" for m in range(375)]
        rows = [minute("A", "2026-01-05", t) for t in full]
        rows += [minute("A", "2026-01-06", t) for t in full]
        rows += [minute("A", "2026-01-07", t) for t in full[:200]]
        res = _run(tmp_path, rows, "CMP-LATEST", interval="1m",
                   calendar=["2026-01-05", "2026-01-06", "2026-01-07"])
        assert _pairs(res) == [("A", "2026-01-07")]


class TestDiagnostics:
    def test_zero_volume_share(self, tmp_path):
        rows = clean_daily("A", DAYS[:10]) + clean_daily("A", DAYS[10:], v=0)
        res = _run(tmp_path, rows, "DIAG-ZERO-VOL")
        assert list(res.evidence["symbol"]) == ["A"]

    def test_spike_that_reverts(self, tmp_path):
        rows = clean_daily("A", DAYS[:5], c=100.0)
        rows.append(daily("A", DAYS[5], o=100, h=150, lo=99, c=150))
        rows += clean_daily("A", DAYS[6:], c=100.0)
        res = _run(tmp_path, rows, "DIAG-SPIKE")
        assert _pairs(res) == [("A", DAYS[5])]

    def test_stale_repeat(self, tmp_path):
        rows = clean_daily("A", DAYS[:5])
        rows += [daily("A", d, o=100, h=100, lo=100, c=100, v=0) for d in DAYS[5:9]]
        res = _run(tmp_path, rows, "DIAG-STALE-REPEAT")
        assert res.rows >= 1 and list(set(res.evidence["symbol"])) == ["A"]

    def test_late_start_symbol(self, tmp_path):
        rows = clean_daily("A", DAYS) + clean_daily("NEWNAME", DAYS[12:])
        res = _run(tmp_path, rows, "DIAG-LATE-START")
        assert list(res.evidence["symbol"]) == ["NEWNAME"]

    def test_cross_interval_volume_mismatch(self, tmp_path):
        rows = [daily("A", "2026-01-05", v=1000), daily("A", "2026-01-06", v=1000)]
        rows += [minute("A", "2026-01-05", "09:15", v=1000)]
        rows += [minute("A", "2026-01-06", "09:15", v=10)]
        res = _run(tmp_path, rows, "DIAG-CROSS-INTERVAL")
        assert _pairs(res) == [("A", "2026-01-06")]


class TestScope:
    def test_symbol_filter(self, tmp_path):
        rows = clean_daily("A", DAYS) + [daily("B", DAYS[0], v=-1)]
        assert _run(tmp_path, rows, "VAL-NEG-VOL", symbols=("A",)).rows == 0
        assert _run(tmp_path, rows, "VAL-NEG-VOL", symbols=("B",)).rows == 1

    def test_bad_table_name_rejected(self):
        with pytest.raises(ValueError):
            R.Scope(interval="D", table="market_data; DROP TABLE x")

    def test_unknown_interval_rejected(self):
        with pytest.raises(ValueError):
            R.Scope(interval="5m")

    def test_read_only_connection_cannot_write(self, tmp_path):
        store = make_store(tmp_path / "s.duckdb", clean_daily("A", DAYS))
        with R.connect_readonly(store) as con, pytest.raises(duckdb.Error):
            con.execute("DELETE FROM market_data")


class TestReviewFindings:
    """Regression tests for the Phase 1 review (python-reviewer, silent-failure-hunter)."""

    @pytest.mark.parametrize("bad", [{"h": float("nan")}, {"c": float("inf")},
                                     {"lo": float("-inf")}])
    def test_non_finite_price_is_an_invalid_row(self, tmp_path, bad):
        rows = clean_daily("A", DAYS[:-1]) + [daily("A", DAYS[-1], **bad)]
        res = _run(tmp_path, rows, "VAL-NULL-ROW")
        assert _pairs(res) == [("A", DAYS[-1])]

    def test_null_timestamp_is_an_invalid_row(self, tmp_path):
        bad = list(daily("A", DAYS[-1]))
        bad[3] = None
        rows = clean_daily("A", DAYS[:-1]) + [tuple(bad)]
        assert _run(tmp_path, rows, "VAL-NULL-ROW").rows == 1

    def test_empty_calendar_is_not_evaluated(self, tmp_path):
        store = make_store(tmp_path / "e.duckdb", clean_daily("A", DAYS))
        with R.connect_readonly(store) as con:
            res = R.evaluate(con, R.Scope(interval="D"), "CMP-MISSING",
                             R.Context(policy=POLICY, calendar=()))
        assert res.status == R.NOT_EVALUATED

    def test_table_name_with_trailing_newline_rejected(self):
        with pytest.raises(ValueError):
            R.Scope(interval="D", table="market_data\n")

    @pytest.mark.parametrize("rule_id", ["VAL-NEG-VOL", "VAL-OHLC"])
    def test_outside_hours_minute_bars_are_not_defects(self, tmp_path, rule_id):
        rows = [minute("A", "2026-01-05", "09:15"),
                minute("A", "2026-01-05", "15:45", v=-1, h=1.0)]
        assert _run(tmp_path, rows, rule_id, interval="1m").rows == 0

    def test_latest_daily_with_unknown_write_time_is_flagged(self, tmp_path):
        last = list(daily("A", DAYS[-1]))
        last[10] = None
        rows = clean_daily("A", DAYS[:-1]) + [tuple(last)]
        res = _run(tmp_path, rows, "CMP-LATEST")
        assert _pairs(res) == [("A", DAYS[-1])]

    def test_latest_minute_single_session_compared_across_symbols(self, tmp_path):
        full = [f"{9 + (15 + m) // 60:02d}:{(15 + m) % 60:02d}" for m in range(375)]
        rows = [minute(s, "2026-01-05", t) for s in ("A", "B") for t in full]
        rows += [minute("NEW", "2026-01-05", t) for t in full[:100]]
        res = _run(tmp_path, rows, "CMP-LATEST", interval="1m", calendar=["2026-01-05"])
        assert _pairs(res) == [("NEW", "2026-01-05")]

    def test_symbol_whose_data_stops_early_lags(self, tmp_path):
        rows = clean_daily("A", DAYS) + clean_daily("B", DAYS[:12])
        res = _run(tmp_path, rows, "FRESH-SYMBOL-LAG")
        assert list(res.evidence["symbol"]) == ["B"]
        assert res.action is R.Action.WARNING
        assert res.dimension == "freshness"

    def test_daily_stamp_uses_integer_ist(self, tmp_path):
        # 23:59 IST on the session date stays on that date; 00:01 IST moves to the next date
        rows = [daily("A", DAYS[0], hhmm="23:59"), daily("A", DAYS[1], hhmm="00:01")]
        res = _run(tmp_path, rows, "CANON-DAILY-TS")
        assert _pairs(res) == [("A", DAYS[0]), ("A", DAYS[1])]


def _split_series(symbol, days, at, factor, base=100.0):
    """Closes at `base`, then at `base * factor` from session index `at` (an unadjusted event)."""
    rows = []
    for i, d in enumerate(days):
        p = base if i < at else base * factor
        rows.append(daily(symbol, d, o=p, h=p * 1.01, lo=p * 0.99, c=p))
    return rows


class TestAdjustmentBasis:
    def test_unadjusted_split_inside_a_daily_series(self, tmp_path):
        rows = _split_series("A", DAYS, 10, 0.5) + _split_series("B", DAYS, 10, 0.2)
        res = _run(tmp_path, rows, "ADJ-JUMP")
        assert res.dimension == "adjustment" and res.action is R.Action.EXCLUDE_SYMBOL
        assert _pairs(res) == [("A", DAYS[10]), ("B", DAYS[10])]

    def test_consolidation_is_a_jump_too(self, tmp_path):
        res = _run(tmp_path, _split_series("A", DAYS, 10, 5.0), "ADJ-JUMP")
        assert _pairs(res) == [("A", DAYS[10])]

    def test_real_large_move_off_ratio_is_not_a_jump(self, tmp_path):
        rows = _split_series("A", DAYS, 10, 0.73) + _split_series("B", DAYS, 10, 0.9)
        assert _run(tmp_path, rows, "ADJ-JUMP").rows == 0

    def test_adjusted_series_has_no_jump(self, tmp_path):
        assert _run(tmp_path, clean_daily("A", DAYS), "ADJ-JUMP").rows == 0

    def test_unadjusted_split_between_1m_sessions(self, tmp_path):
        rows = [minute("A", DAYS[0], "09:15", o=100, h=100, lo=100, c=100),
                minute("A", DAYS[0], "15:29", o=100, h=100, lo=100, c=100),
                minute("A", DAYS[1], "09:15", o=20, h=20, lo=20, c=20),
                minute("A", DAYS[1], "15:29", o=20, h=20, lo=20, c=20)]
        res = _run(tmp_path, rows, "ADJ-JUMP", interval="1m")
        assert _pairs(res) == [("A", DAYS[1])]

    def _cross(self, symbol, days, d_close, m_close):
        rows = []
        for d in days:
            rows.append(daily(symbol, d, o=d_close, h=d_close, lo=d_close, c=d_close))
            rows.append(minute(symbol, d, "15:29", o=m_close, h=m_close, lo=m_close, c=m_close))
        return rows

    def test_daily_and_1m_on_different_bases(self, tmp_path):
        rows = (self._cross("A", DAYS[:6], 200.0, 100.0)    # 6 sessions at 2x: excluded
                + self._cross("B", DAYS[:6], 101.0, 100.2)  # normal close disagreement
                + self._cross("C", DAYS[:4], 200.0, 100.0)  # below the minimum session count
                + self._cross("C", DAYS[4:6], 100.0, 100.0))
        res = _run(tmp_path, rows, "ADJ-CROSS-BASIS", interval="1m")
        assert res.status == R.EVALUATED and res.action is R.Action.EXCLUDE_SYMBOL
        assert sorted(set(res.evidence["symbol"])) == ["A"] and res.rows == 6

    def test_cross_basis_needs_daily_bars(self, tmp_path):
        rows = [minute("A", DAYS[0], "15:29")]
        res = _run(tmp_path, rows, "ADJ-CROSS-BASIS", interval="1m")
        assert res.status == R.NOT_EVALUATED and "daily" in res.reason

    def test_jump_across_a_data_gap_is_not_evaluated_as_overnight(self, tmp_path):
        # Five sessions missing between the two closes: the move is not an overnight gap.
        rows = [r for i, r in enumerate(_split_series("A", DAYS, 10, 0.5)) if not 5 <= i < 10]
        assert _run(tmp_path, rows, "ADJ-JUMP").rows == 0

    def test_cross_basis_needs_daily_bars_for_the_scoped_symbols(self, tmp_path):
        rows = self._cross("A", DAYS[:6], 200.0, 100.0) + [minute("B", DAYS[0], "15:29")]
        res = _run(tmp_path, rows, "ADJ-CROSS-BASIS", interval="1m", symbols=("B",))
        assert res.status == R.NOT_EVALUATED

    def test_twenty_percent_circuit_gap_is_not_a_jump(self, tmp_path):
        # A 20% lower-circuit gap (2020-03-23, Paytm 2024-02-01) has the same price ratio as a
        # 1:4 bonus, so 0.8 is not in the policy's event list (found on the Angel rebuild).
        assert _run(tmp_path, _split_series("A", DAYS, 10, 0.8), "ADJ-JUMP").rows == 0
