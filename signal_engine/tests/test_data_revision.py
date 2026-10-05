"""Daily-bar de-duplication of the Historify store, recorded as a dataset revision.

A daily bar is stamped 05:30 IST. Some stores also hold copies of the same session stamped at other
times, and a loader that normalises to the IST date sees them as duplicates and keeps the last one,
which can be a near-empty fragment. The revision keeps the canonically stamped row and records
every row it removes, so the change is reproducible and reversible.
"""

from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest import data_revision as rev

IST = "Asia/Kolkata"


def _ts(day: str, hhmm: str) -> int:
    return int(pd.Timestamp(f"{day} {hhmm}", tz=IST).timestamp())


def _row(symbol, day, hhmm, o=100.0, h=105.0, lo=99.0, c=101.0, v=1000, interval="D"):
    return (symbol, "NSE", interval, _ts(day, hhmm), o, h, lo, c, v, 0)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "h.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE market_data (symbol VARCHAR, exchange VARCHAR, interval VARCHAR, "
        "timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, "
        "oi BIGINT)"
    )
    rows = [
        # a clean symbol: one bar a day
        _row("CLEAN", "2026-01-05", "05:30"),
        _row("CLEAN", "2026-01-06", "05:30"),
        # identical copies at 11:00 and 16:30
        _row("DUP", "2026-01-05", "05:30"),
        _row("DUP", "2026-01-05", "11:00"),
        _row("DUP", "2026-01-05", "16:30"),
        # copies that differ: a thin fragment after the session
        _row("DUP", "2026-01-06", "05:30", o=200, h=210, lo=195, c=205, v=5_000_000),
        _row("DUP", "2026-01-06", "11:00", o=210, h=210, lo=210, c=205, v=2),
        # a copy of the previous session stamped after midnight, so on the next IST date
        _row("SHIFT", "2026-03-12", "05:30", c=50.0),
        _row("SHIFT", "2026-03-12", "14:45", c=50.0),
        _row("SHIFT", "2026-03-13", "01:45", c=50.0),
        _row("SHIFT", "2026-03-13", "05:30", c=52.0),
        # a date with no canonical bar: nothing to keep, so nothing may be removed
        _row("ODD", "2026-02-02", "11:00"),
        _row("ODD", "2026-02-02", "16:30"),
        # other intervals are never touched
        _row("DUP", "2026-01-05", "09:15", interval="1m"),
        _row("DUP", "2026-01-05", "09:15", interval="1m"),
    ]
    con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.close()
    return db


def _count(db, where="1=1"):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM market_data WHERE {where}").fetchone()[0]
    finally:
        con.close()


class TestPlan:
    def test_only_non_canonical_copies_of_a_canonical_bar_are_planned(self, store):
        plan = rev.plan_daily_dedup(store)
        got = sorted(
            zip(plan["symbol"], plan["ist_date"].astype(str), plan["ist_time"], strict=True)
        )
        assert got == [
            ("DUP", "2026-01-05", "11:00"),
            ("DUP", "2026-01-05", "16:30"),
            ("DUP", "2026-01-06", "11:00"),
            ("SHIFT", "2026-03-12", "14:45"),
            ("SHIFT", "2026-03-13", "01:45"),
        ]

    def test_a_date_without_a_canonical_bar_is_left_alone(self, store):
        plan = rev.plan_daily_dedup(store)
        assert "ODD" not in set(plan["symbol"])

    def test_planning_changes_nothing(self, store):
        before = _count(store)
        rev.plan_daily_dedup(store)
        assert _count(store) == before

    def test_each_planned_row_says_whether_it_differs_from_the_kept_bar(self, store):
        plan = rev.plan_daily_dedup(store).set_index(["symbol", "ist_time", "ist_date"])
        same = plan.xs("DUP", level="symbol")
        flags = {
            t: bool(f)
            for t, f in zip(same.index.get_level_values("ist_time"), same["differs"], strict=True)
        }
        assert flags["16:30"] is False
        # 11:00 appears on two dates: identical on the 5th, different on the 6th
        d = plan.loc[("DUP", "11:00")]
        assert sorted(d["differs"].tolist()) == [False, True]

    def test_unresolved_dates_are_counted(self, store):
        assert rev.unresolved_dates(store) == [("ODD", "NSE", "2026-02-02")]


class TestApply:
    def test_removes_planned_rows_and_keeps_the_canonical_bar(self, store, tmp_path):
        m = rev.apply_daily_dedup(
            store, tmp_path / "rev", name="r1", applied_at="2026-10-04 12:00 IST"
        )
        assert m["removed_rows"] == 5
        assert _count(store, "interval='D' AND symbol='DUP'") == 2
        con = duckdb.connect(str(store), read_only=True)
        kept = con.execute(
            "SELECT volume FROM market_data WHERE symbol='DUP' AND interval='D' ORDER BY timestamp"
        ).fetchall()
        con.close()
        assert kept == [(1000,), (5_000_000,)]

    def test_other_intervals_and_unresolved_dates_are_untouched(self, store, tmp_path):
        rev.apply_daily_dedup(store, tmp_path / "rev", name="r1", applied_at="t")
        assert _count(store, "interval='1m'") == 2
        assert _count(store, "symbol='ODD'") == 2

    def test_shifted_copy_does_not_displace_the_real_bar(self, store, tmp_path):
        rev.apply_daily_dedup(store, tmp_path / "rev", name="r1", applied_at="t")
        con = duckdb.connect(str(store), read_only=True)
        closes = con.execute(
            "SELECT close FROM market_data WHERE symbol='SHIFT' ORDER BY timestamp"
        ).fetchall()
        con.close()
        assert closes == [(50.0,), (52.0,)]

    def test_second_apply_removes_nothing(self, store, tmp_path):
        rev.apply_daily_dedup(store, tmp_path / "rev", name="r1", applied_at="t")
        again = rev.apply_daily_dedup(store, tmp_path / "rev2", name="r2", applied_at="t")
        assert again["removed_rows"] == 0

    def test_removed_rows_are_written_so_the_change_can_be_undone(self, store, tmp_path):
        out = tmp_path / "rev"
        m = rev.apply_daily_dedup(store, out, name="r1", applied_at="t")
        removed = pd.read_csv(out / "r1-removed-rows.csv")
        assert len(removed) == m["removed_rows"] == 5
        assert {"symbol", "timestamp", "open", "high", "low", "close", "volume"} <= set(
            removed.columns
        )

    def test_manifest_records_rule_counts_and_affected_symbols(self, store, tmp_path):
        out = tmp_path / "rev"
        rev.apply_daily_dedup(store, out, name="r1", applied_at="2026-10-04 12:00 IST")
        m = json.loads((out / "r1.json").read_text(encoding="utf-8"))
        assert m["rule"] == rev.RULE
        assert m["applied_at"] == "2026-10-04 12:00 IST"
        assert m["rows_before"] - m["rows_after"] == m["removed_rows"] == 5
        assert m["removed_differing_rows"] == 2  # DUP 11:00 on the 6th and SHIFT 01:45 on the 13th
        assert m["by_symbol"] == {"DUP": 3, "SHIFT": 2}
        assert m["unresolved_dates"] == [["ODD", "NSE", "2026-02-02"]]

    def test_dry_run_writes_and_deletes_nothing(self, store, tmp_path):
        out = tmp_path / "rev"
        before = _count(store)
        m = rev.apply_daily_dedup(store, out, name="r1", applied_at="t", dry_run=True)
        assert m["removed_rows"] == 5
        assert _count(store) == before
        assert not out.exists()

    def test_the_dataset_fingerprint_changes(self, store, tmp_path):
        from signal_engine.backtest import manifest

        syms = ["CLEAN", "DUP", "SHIFT", "ODD"]
        before = manifest.dataset_fingerprint(syms, "1d", "2026-10-04", db_path=store)
        rev.apply_daily_dedup(store, tmp_path / "rev", name="r1", applied_at="t")
        after = manifest.dataset_fingerprint(syms, "1d", "2026-10-04", db_path=store)
        assert before != after


class TestRollback:
    def test_reinserting_the_recorded_rows_restores_the_original_fingerprint(self, store, tmp_path):
        from signal_engine.backtest import manifest

        syms = ["CLEAN", "DUP", "SHIFT", "ODD"]
        before = manifest.dataset_fingerprint(syms, "1d", "2026-10-04", db_path=store)
        out = tmp_path / "rev"
        rev.apply_daily_dedup(store, out, name="r1", applied_at="t")
        removed = pd.read_csv(out / "r1-removed-rows.csv")
        con = duckdb.connect(str(store))
        con.register("back", removed)
        con.execute(
            "INSERT INTO market_data SELECT symbol, exchange, 'D', timestamp, open, high, low, "
            "close, volume, 0 FROM back"
        )
        con.close()
        assert manifest.dataset_fingerprint(syms, "1d", "2026-10-04", db_path=store) == before
