"""Store audit: every rule over the whole landing store, with symbol/date evidence (plan Phase 1)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import audit
from signal_engine.backtest.data_readiness import policy as pol
from signal_engine.backtest.data_readiness.calendar import CalendarError, NseCalendar
from signal_engine.tests.dq_fixtures import clean_daily, daily, make_store, minute, weekdays

POLICY = pol.load_policy(Path(__file__).resolve().parents[1] / "research" / "data_policy.yaml")
DAYS = weekdays("2026-01-05", "2026-01-30")


def _calendar(holidays=(), specials=(), mocks=(), unconfirmed=(), years=(2026, 2026)):
    """A synthetic official calendar; the default has no holidays, so DAYS are all sessions."""
    as_dates = lambda xs: frozenset(pd.Timestamp(x).date() for x in xs)  # noqa: E731
    return NseCalendar(holidays=as_dates(holidays),
                       specials={pd.Timestamp(x).date(): None for x in specials},
                       mocks=as_dates(mocks), unconfirmed=as_dates(unconfirmed), coverage=years,
                       regular_session=("09:15", "15:30"), version="test-cal-v1",
                       source="synthetic")


CAL = _calendar()


def _audit(store, out, intervals=("D",), calendar=CAL, **kw):
    return audit.run_audit(store, out, POLICY, intervals=intervals, calendar=calendar, **kw)


@pytest.fixture
def store(tmp_path):
    rows = []
    for s in ("A", "B", "C", "D"):
        rows += clean_daily(s, DAYS)
    rows += [daily("A", "2026-01-11")]                       # isolated Sunday bar
    rows += [daily(s, "2026-01-18") for s in ("A", "B", "C", "D")]  # panel-wide Sunday
    rows += [daily("B", DAYS[3], hhmm="11:00", v=7)]         # SBIN-style duplicate
    rows += [minute("C", DAYS[0], "09:15", v=-4)]            # negative 1m volume
    rows += [minute("C", DAYS[0], "09:16")]
    return make_store(tmp_path / "landing.duckdb", rows)


def test_audit_writes_its_outputs(store, tmp_path):
    out = tmp_path / "out"
    report = _audit(store, out, ("D", "1m"))
    assert (out / "audit.json").exists()
    assert (out / "AUDIT.md").exists()
    assert (out / "per_symbol.parquet").exists()
    assert (out / "evidence" / "D" / "SNAP-DUP-KEY.parquet").exists()
    on_disk = json.loads((out / "audit.json").read_text())
    assert on_disk["policy_version"] == POLICY.version
    assert on_disk == json.loads(json.dumps(report, default=str))


def test_audit_finds_the_planted_issues(store, tmp_path):
    report = _audit(store, tmp_path / "out", ("D", "1m"))
    d = report["intervals"]["D"]["rules"]
    assert d["SNAP-DUP-KEY"]["rows"] == 1
    assert d["SNAP-DUP-KEY"]["symbols"] == {"B": 1}
    assert d["CAL-UNEXPECTED-DATE"]["sessions"] == ["2026-01-18"]
    assert d["CAL-UNEXPECTED-ISOLATED"]["symbols"] == {"A": 1}
    m = report["intervals"]["1m"]["rules"]
    assert m["VAL-NEG-VOL"]["symbols"] == {"C": 1}


def test_dimension_status_has_no_score(store, tmp_path):
    report = _audit(store, tmp_path / "out")
    dims = report["intervals"]["D"]["dimensions"]
    assert dims["snapshot_integrity"] == "FAIL"
    assert dims["calendar"] == "FAIL"
    assert set(dims.values()) <= {"PASS", "WARNING", "FAIL", "NOT_EVALUATED"}
    assert "score" not in json.dumps(report).lower()


def test_projection_against_the_exclusion_thresholds(store, tmp_path):
    report = _audit(store, tmp_path / "out")
    proj = report["intervals"]["D"]["projection"]
    assert proj["requested_symbols"] == 4
    assert proj["policy"] == {"max_defect_excluded_symbols": 10,
                              "max_defect_excluded_symbol_share": 0.05}
    # daily, lookback strategies: a session defect excludes the whole symbol (D16)
    assert "A" in proj["defect_excluded_symbols"]
    assert proj["state"] == "NOT_READY"


def test_evidence_is_symbol_and_session(store, tmp_path):
    out = tmp_path / "out"
    _audit(store, out)
    ev = pd.read_parquet(out / "evidence" / "D" / "CAL-UNEXPECTED-ISOLATED.parquet")
    assert list(ev.columns[:3]) == ["symbol", "session", "rows"]


def test_per_symbol_summary(store, tmp_path):
    out = tmp_path / "out"
    _audit(store, out)
    ps = pd.read_parquet(out / "per_symbol.parquet")
    row = ps[(ps["interval"] == "D") & (ps["symbol"] == "B")].iloc[0]
    assert row["sessions"] == 21  # 20 weekdays plus the planted panel-wide Sunday
    assert row["first_session"] == pd.Timestamp("2026-01-05").date()


def test_store_is_not_modified(store, tmp_path):
    before = store.read_bytes()
    _audit(store, tmp_path / "out", ("D", "1m"))
    assert store.read_bytes() == before


def test_sql_failure_raises_not_passes(tmp_path):
    empty = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(empty))
    con.execute("CREATE TABLE other (x INT)")
    con.close()
    with pytest.raises(duckdb.Error):
        _audit(empty, tmp_path / "out")


def test_proxy_calendar_sees_a_day_missing_from_daily(tmp_path):
    """Real-store pattern: on some trading days only one symbol has a daily bar while the 1m
    data has every symbol. A daily-only derived calendar drops those days, which hides the gap."""
    gap = DAYS[5]
    rows = []
    for s in ("A", "B", "C", "D", "E"):
        rows += clean_daily(s, [d for d in DAYS if d != gap or s == "A"])
        rows += [minute(s, d, "09:15") for d in DAYS]
    store = make_store(tmp_path / "gap.duckdb", rows)
    report = _audit(store, tmp_path / "out", calendar=None, proxy=True)
    d = report["intervals"]["D"]["rules"]
    assert d["CAL-UNEXPECTED-ISOLATED"]["rows"] == 0
    missing = d["CMP-MISSING"]["symbols"] | d["CMP-MISSING-FEW"]["symbols"]
    assert set(missing) == {"B", "C", "D", "E"}


def test_exclusions_on_a_blocked_date_are_attributed_to_the_block(tmp_path):
    rows = []
    for s in ("A", "B", "C", "D"):
        rows += clean_daily(s, DAYS, v=5_000_000)
        rows.append(daily(s, "2026-01-18", o=100, h=100, lo=100, c=100, v=10))  # mock Sunday
    store = make_store(tmp_path / "mock.duckdb", rows)
    report = _audit(store, tmp_path / "out")
    proj = report["intervals"]["D"]["projection"]
    assert proj["blocks"] == ["CAL-UNEXPECTED-DATE"]
    assert proj["defect_excluded_symbols"] == []
    assert proj["exclusions_on_blocked_dates"] == 4


def test_weekday_proxy_calendar_excludes_weekends(store):
    from signal_engine.backtest.data_readiness.rules import connect_readonly

    with connect_readonly(store) as con:
        cal = audit.proxy_calendar(con)
    assert all(d.weekday() < 5 for d in cal.sessions)
    assert cal.source.startswith("derived_from_market_data")


def test_a_skipped_applicable_rule_is_not_a_pass(tmp_path):
    """No created_at column: CMP-LATEST cannot run, so completeness must not read PASS."""
    path = tmp_path / "nocreated.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE market_data (symbol VARCHAR, exchange VARCHAR, interval VARCHAR, "
                "timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
                "volume BIGINT, oi BIGINT)")
    con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [r[:10] for r in clean_daily("A", DAYS)])
    con.close()
    report = _audit(path, tmp_path / "out")
    blk = report["intervals"]["D"]
    assert blk["dimensions"]["completeness"] == "NOT_EVALUATED"
    assert "not_evaluated:CMP-LATEST" in blk["projection"]["warnings"]
    assert blk["projection"]["state"] == "READY_WITH_WARNINGS"


def test_an_interval_with_no_rows_blocks(tmp_path):
    store = make_store(tmp_path / "donly.duckdb", clean_daily("A", DAYS))
    report = _audit(store, tmp_path / "out", ("1m",))
    proj = report["intervals"]["1m"]["projection"]
    assert proj["state"] == "NOT_READY"
    assert "no_rows" in proj["blocks"]


def test_cli_fail_on_not_ready(store, tmp_path):
    args = ["--db", str(store), "--interval", "D", "--out", str(tmp_path / "o")]
    assert audit.main(args) == 0
    assert audit.main([*args, "--fail-on-not-ready"]) == 1


def test_a_missing_daily_session_excludes_the_symbol_when_sessions_cannot_be_dropped(tmp_path):
    """Owner decision (IDEA-027): a missing daily session shifts the 252-session lookback, so it
    excludes the whole symbol even below the 2% share that only warns elsewhere."""
    cal = weekdays("2025-01-01", "2026-01-30")
    rows = []
    for s in ("A", "B", "C", "D", "E"):
        held = [d for d in cal if not (s == "A" and d == cal[100])]
        rows += clean_daily(s, held)
        rows += [minute(s, d, "09:15") for d in cal]
    store = make_store(tmp_path / "few.duckdb", rows)
    report = _audit(store, tmp_path / "out", ("D", "1m"),
                    calendar=_calendar(years=(2025, 2026)))
    d = report["intervals"]["D"]
    assert d["rules"]["CMP-MISSING-FEW"]["symbols"] == {"A": 1}
    assert d["projection"]["defect_excluded_by_rule"] == {"CMP-MISSING-FEW": ["A"]}
    m = report["intervals"]["1m"]["projection"]
    assert m["defect_excluded_symbols"] == []


# ---- official calendar (Phase 2) -----------------------------------------------------------

def test_official_calendar_finds_a_day_missing_from_every_symbol(tmp_path):
    """The point of an independent calendar: a session absent from ALL data is still expected."""
    gap = DAYS[5]
    rows = []
    for s in ("A", "B"):
        rows += clean_daily(s, [d for d in DAYS if d != gap])
    report = _audit(make_store(tmp_path / "s.duckdb", rows), tmp_path / "out")
    r = report["intervals"]["D"]["rules"]
    assert set(r["CMP-MISSING-FEW"]["symbols"]) | set(r["CMP-MISSING"]["symbols"]) == {"A", "B"}


def test_holiday_bars_are_unexpected_and_special_sessions_are_not(tmp_path):
    cal = _calendar(holidays=["2026-01-26"], specials=["2026-01-31"])  # a Saturday session
    rows = []
    for s in ("A", "B", "C"):
        rows += clean_daily(s, DAYS) + [daily(s, "2026-01-31")]
    report = _audit(make_store(tmp_path / "s.duckdb", rows), tmp_path / "out", calendar=cal)
    r = report["intervals"]["D"]["rules"]
    assert r["CAL-UNEXPECTED-DATE"]["sessions"] == ["2026-01-26"]


def test_mock_and_unconfirmed_dates_are_not_sessions(tmp_path):
    cal = _calendar(mocks=["2026-01-31"], unconfirmed=["2026-01-25"])
    rows = []
    for s in ("A", "B", "C"):
        rows += clean_daily(s, DAYS) + [daily(s, "2026-01-31"), daily(s, "2026-01-25")]
    report = _audit(make_store(tmp_path / "s.duckdb", rows), tmp_path / "out", calendar=cal)
    assert report["intervals"]["D"]["rules"]["CAL-UNEXPECTED-DATE"]["sessions"] == [
        "2026-01-25", "2026-01-31"]


def test_report_records_the_calendar_version_and_source(store, tmp_path):
    report = _audit(store, tmp_path / "out")
    meta = report["intervals"]["D"]["calendar"]
    assert meta["version"] == "test-cal-v1" and meta["source"] == "nse_calendar: synthetic"


def test_data_outside_the_calendar_coverage_fails(tmp_path):
    rows = clean_daily("A", DAYS)
    with pytest.raises(CalendarError):
        _audit(make_store(tmp_path / "s.duckdb", rows), tmp_path / "out",
               calendar=_calendar(years=(2025, 2025)))


def test_default_is_the_repository_calendar(store, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(audit, "load_calendar", lambda: seen.setdefault("cal", CAL))
    audit.run_audit(store, tmp_path / "out", POLICY, intervals=("D",))
    assert seen["cal"] is CAL
