"""Intraday data contract: complete bins and n_minutes, one rule for preflight and read."""

from datetime import date, datetime
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import intraday_access as ia
from signal_engine.backtest.data_readiness import state as st
from signal_engine.backtest.data_readiness.contract import DataContract, resolve_contract
from signal_engine.timeutils import IST

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2025, 1, 2)
FIRST, LAST = 9 * 60 + 15, 15 * 60 + 10
FULL = list(range(FIRST, LAST + 1, 5))          # 72 bins


def _ts(d, minute):
    return int(datetime(d.year, d.month, d.day, minute // 60, minute % 60,
                        tzinfo=IST).timestamp())


def _rows(symbol, d, minutes, n=5):
    return [{"symbol": symbol, "interval": "5m", "timestamp": _ts(d, m), "open": 1.0,
             "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0,
             "n_minutes": n[m] if isinstance(n, dict) and m in n else (5 if isinstance(n, dict)
                                                                       else n)} for m in minutes]


def _con(rows, with_n=True):
    df = pd.DataFrame(rows)
    if not with_n:
        df = df.drop(columns="n_minutes")
    con = duckdb.connect(":memory:")
    con.register("bars", df)
    con.execute("CREATE VIEW market_data AS SELECT * FROM bars")
    return con


def _contract(min_n=1, bins=("09:15", "15:10"), families=("price",), freq="5m"):
    return SimpleNamespace(field_families=families, min_n_minutes=min_n, complete_bins=bins,
                           frequency=freq)


def _candidates(*pairs):
    return pd.DataFrame({"symbol": [p[0] for p in pairs],
                         "session": pd.to_datetime([p[1] for p in pairs])})


def _excl(con, contract, *pairs, symbols=("AAA",)):
    out = ia.data_exclusions(con, contract, _candidates(*pairs), list(symbols))
    return sorted((r.symbol, r.session.date().isoformat(), r.reason) for r in out.itertuples())


def test_complete_session_is_not_excluded():
    assert _excl(_con(_rows("AAA", D1, FULL)), _contract(), ("AAA", D1)) == []


def test_missing_bin_in_the_middle_excludes_the_session():
    con = _con(_rows("AAA", D1, [m for m in FULL if m != 12 * 60]))
    assert _excl(con, _contract(), ("AAA", D1)) == [("AAA", "2024-01-02", ia.DATA_MISSING_BIN)]


@pytest.mark.parametrize("gone", [FIRST, LAST])
def test_boundary_bins_are_required(gone):
    con = _con(_rows("AAA", D1, [m for m in FULL if m != gone]))
    assert _excl(con, _contract(), ("AAA", D1)) == [("AAA", "2024-01-02", ia.DATA_MISSING_BIN)]


def test_bars_after_the_last_bin_are_ignored_by_completeness_and_n_minutes():
    extra = _rows("AAA", D1, [15 * 60 + 15, 15 * 60 + 20, 15 * 60 + 25], n=1)
    con = _con(_rows("AAA", D1, FULL) + extra)
    assert _excl(con, _contract(min_n=5), ("AAA", D1)) == []


def test_a_missing_bin_outside_the_window_does_not_exclude():
    con = _con(_rows("AAA", D1, [m for m in FULL if m != LAST]) + _rows("AAA", D1, [LAST + 5]))
    assert _excl(con, _contract(bins=("09:15", "15:05")), ("AAA", D1)) == []


def test_low_n_minutes_in_the_window_excludes():
    con = _con(_rows("AAA", D1, FULL, n={FIRST + 10: 2}))
    assert _excl(con, _contract(min_n=3), ("AAA", D1)) == [
        ("AAA", "2024-01-02", ia.DATA_MIN_N_MINUTES)]
    assert _excl(con, _contract(min_n=2), ("AAA", D1)) == []


def test_missing_bin_and_low_n_minutes_report_the_missing_bin():
    rows = _rows("AAA", D1, [m for m in FULL if m != 700], n={FIRST: 1})
    assert _excl(_con(rows), _contract(min_n=2), ("AAA", D1)) == [
        ("AAA", "2024-01-02", ia.DATA_MISSING_BIN)]


def test_candidate_session_without_any_bar_is_missing_its_bins():
    con = _con(_rows("AAA", D1, FULL))
    assert _excl(con, _contract(), ("AAA", D2)) == [("AAA", "2024-01-03", ia.DATA_MISSING_BIN)]


def test_exclusion_is_per_symbol_and_session():
    rows = _rows("AAA", D1, FULL) + _rows("BBB", D1, FULL[:-1]) + _rows("BBB", D2, FULL)
    got = _excl(_con(rows), _contract(), ("AAA", D1), ("BBB", D1), ("BBB", D2),
                symbols=("AAA", "BBB"))
    assert got == [("BBB", "2024-01-02", ia.DATA_MISSING_BIN)]


def test_off_grid_bar_does_not_count_as_a_bin():
    rows = _rows("AAA", D1, [m for m in FULL if m != 700]) + _rows("AAA", D1, [701])
    assert _excl(_con(rows), _contract(), ("AAA", D1)) == [
        ("AAA", "2024-01-02", ia.DATA_MISSING_BIN)]


def test_no_declared_contract_excludes_nothing():
    con = _con(_rows("AAA", D1, FULL[:3]), with_n=False)
    for c in (SimpleNamespace(field_families=None),
              _contract(min_n=None, bins=None)):
        assert _excl(con, c, ("AAA", D1)) == []


def test_min_n_minutes_without_a_column_is_refused():
    con = _con(_rows("AAA", D1, FULL), with_n=False)
    with pytest.raises(ia.NMinutesUnavailable):
        ia.data_exclusions(con, _contract(), _candidates(("AAA", D1)), ["AAA"])


def test_bins_only_contract_works_without_a_column():
    con = _con(_rows("AAA", D1, FULL[:-1]), with_n=False)
    got = _excl(con, _contract(min_n=None), ("AAA", D1))
    assert got == [("AAA", "2024-01-02", ia.DATA_MISSING_BIN)]


# ---------------------------------------------------------------- contract object

def _frozen(data):
    return SimpleNamespace(intent={"universe": ["AAA"], "timeframe": "5m", "data": data},
                           resolved={})


def test_resolve_contract_reads_min_n_minutes_and_complete_bins():
    c = resolve_contract(_frozen({"frequency": "5m", "field_families": ["price", "volume"],
                                  "min_n_minutes": 2,
                                  "complete_bins": {"first": "09:15", "last": "15:10"}}))
    assert c.min_n_minutes == 2 and c.complete_bins == ("09:15", "15:10")
    d = c.to_dict()
    assert d["min_n_minutes"] == 2 and d["complete_bins"] == ["09:15", "15:10"]


def test_contract_without_the_block_keeps_its_old_dict():
    c = resolve_contract(SimpleNamespace(intent={"universe": ["AAA"], "timeframe": "5m"},
                                         resolved={}))
    assert c.min_n_minutes is None and c.complete_bins is None
    assert "min_n_minutes" not in c.to_dict() and "complete_bins" not in c.to_dict()
    old = c.to_dict()
    old["universe_symbols"], old["notes"] = tuple(old["universe_symbols"]), tuple(old["notes"])
    assert DataContract(**old).complete_bins is None


# ---------------------------------------------------------------- read path

def _quality(symbol, days):
    return pd.DataFrame({"symbol": symbol, "session": pd.to_datetime(days), "cls": "legacy",
                         "price_valid": True, "volume_valid": True, "final_bar_valid": True})


def test_read_bars_returns_n_minutes_and_drops_excluded_sessions(monkeypatch):
    monkeypatch.setattr(ia, "load_quality", lambda m, root=None: _quality("AAA", [D1, D2, D3]))
    rows = (_rows("AAA", D1, FULL, n={FIRST: 3}) + _rows("AAA", D2, FULL[:-1])
            + _rows("AAA", D3, FULL + [15 * 60 + 15], n=5))
    m = {"dataset_version": "v", "frequency": "5m"}
    out = ia.read_bars(_con(rows), m, _contract(), ["AAA"], "5m", None)["AAA"]
    assert sorted({t.date() for t in out.index}) == [D1, D3]
    assert out.n_minutes.dtype.kind == "i"
    assert out.n_minutes.iloc[0] == 3
    assert len(out) == 72 + 73          # bars are not trimmed, only sessions dropped
    strict = ia.read_bars(_con(rows), m, _contract(min_n=4), ["AAA"], "5m", None)["AAA"]
    assert sorted({t.date() for t in strict.index}) == [D3]


def test_read_bars_without_a_column_or_declaration_is_unchanged(monkeypatch):
    monkeypatch.setattr(ia, "load_quality", lambda m, root=None: _quality("AAA", [D1]))
    m = {"dataset_version": "v", "frequency": "5m"}
    df = ia.read_bars(_con(_rows("AAA", D1, FULL[:3]), with_n=False), m,
                      SimpleNamespace(field_families=("price",)), ["AAA"], "5m", None)["AAA"]
    assert "n_minutes" not in df.columns and len(df) == 3


def test_read_and_preflight_exclude_the_same_sessions(monkeypatch):
    days = [D1, D2]
    q = _quality("AAA", days)
    monkeypatch.setattr(ia, "load_quality", lambda m, root=None: q)
    rows = _rows("AAA", D1, FULL) + _rows("AAA", D2, FULL[:-1])
    con = _con(rows)
    contract = _contract()
    avail = ia.availability(q, ("price",), [])
    pre = ia.apply_data_exclusions(con, contract, avail, ["AAA"])
    read = ia.read_bars(con, {"dataset_version": "v", "frequency": "5m"}, contract, ["AAA"],
                        "5m", None)["AAA"]
    assert sorted({t.date() for t in read.index}) == sorted(
        pre[pre.available].session.dt.date.unique())
    assert list(pre[~pre.available].reason) == [ia.DATA_MISSING_BIN]


# ---------------------------------------------------------------- preflight

class _Cm:
    def __init__(self, con):
        self.con = con

    def __enter__(self):
        return self.con

    def __exit__(self, *a):
        return False


def _preflight(monkeypatch, rows, days, contract, quality=None, **kw):
    q = quality if quality is not None else _quality("AAA", days)
    monkeypatch.setattr(ia, "load_quality", lambda m, root=None: q)
    monkeypatch.setattr(ia.snapshot, "open_version", lambda *a, **k: _Cm(_con(rows)))
    cal = SimpleNamespace(version="v", expected_sessions=lambda a, b: tuple(days))
    contract.universe_symbols = ("AAA",)
    contract.universe_name, contract.universe_source = "u", "s"
    contract.universe_version, contract.history_sessions = "x", 1
    report = SimpleNamespace(dataset={}, state=None, reasons=None, created_ist="")
    return ia.run_preflight(report, manifest={"dataset_version": "v"}, contract=contract,
                            calendar=cal, stop=days[-1], start=days[0], root=None,
                            git_dir=None, **kw)


def test_preflight_names_excluded_sessions_with_reasons_and_years(monkeypatch):
    days = [D1, D2, D3]
    rows = (_rows("AAA", D1, FULL) + _rows("AAA", D2, FULL[:-1])
            + _rows("AAA", D3, FULL, n={FIRST: 1}))
    out = _preflight(monkeypatch, rows, days, _contract(min_n=2))
    assert out.state == st.READY_WITH_WARNINGS
    assert out.universe["eligible"] == ["AAA"]
    assert out.exclusions == [{"symbol": "AAA", "class": "session", "sessions": 2,
                               "reasons": {ia.DATA_MISSING_BIN: 1, ia.DATA_MIN_N_MINUTES: 1},
                               "years": {"2024": 1, "2025": 1}}]
    assert out.universe["session_exclusions_by_year"] == {
        "2024": {ia.DATA_MISSING_BIN: 1}, "2025": {ia.DATA_MIN_N_MINUTES: 1}}
    assert out.counts["symbol_sessions_available"] == 1


def test_preflight_is_not_ready_when_no_symbol_is_eligible(monkeypatch):
    days = [D1, D2]
    rows = _rows("AAA", D1, FULL[:-1]) + _rows("AAA", D2, FULL[1:])
    out = _preflight(monkeypatch, rows, days, _contract())
    assert out.state == st.NOT_READY and out.reasons == ["no_eligible_symbols"]
    assert out.exclusions[0]["reasons"] == {ia.DATA_MISSING_BIN: 2}


def test_preflight_quality_reasons_stay_and_are_not_double_counted(monkeypatch):
    days = [D1, D2]
    q = _quality("AAA", days)
    q.loc[0, "price_valid"] = False
    rows = _rows("AAA", D1, FULL[:2]) + _rows("AAA", D2, FULL)
    out = _preflight(monkeypatch, rows, days, _contract(), quality=q)
    assert out.exclusions[0]["reasons"] == {"price_invalid": 1}
    assert out.universe["eligible"] == ["AAA"]


def test_preflight_clean_session_is_ready_and_old_contract_unchanged(monkeypatch):
    out = _preflight(monkeypatch, _rows("AAA", D1, FULL), [D1], _contract())
    assert out.state == st.READY and out.exclusions == []


def test_preflight_require_ready_turns_exclusions_into_not_ready(monkeypatch):
    days = [D1, D2]
    rows = _rows("AAA", D1, FULL) + _rows("AAA", D2, FULL[:-1])
    out = _preflight(monkeypatch, rows, days, _contract(), require_ready=True)
    assert out.state == st.NOT_READY and out.reasons == ["require_ready"]
    ok = _preflight(monkeypatch, rows, days, _contract(), require_ready=False)
    assert ok.state == st.READY_WITH_WARNINGS


def test_preflight_missing_n_minutes_column_is_a_not_ready_reason(monkeypatch):
    q = _quality("AAA", [D1])
    monkeypatch.setattr(ia, "load_quality", lambda m, root=None: q)
    monkeypatch.setattr(ia.snapshot, "open_version",
                        lambda *a, **k: _Cm(_con(_rows("AAA", D1, FULL), with_n=False)))
    cal = SimpleNamespace(version="v", expected_sessions=lambda a, b: (D1,))
    contract = _contract()
    contract.universe_symbols, contract.universe_name = ("AAA",), "u"
    contract.universe_source, contract.universe_version = "s", "x"
    contract.history_sessions = 1
    report = SimpleNamespace(dataset={}, state=None, reasons=None, created_ist="")
    out = ia.run_preflight(report, manifest={"dataset_version": "v"}, contract=contract,
                           calendar=cal, stop=D1, start=D1, root=None, git_dir=None)
    assert out.state == st.NOT_READY and out.reasons == ["DATA-NO-N-MINUTES"]
