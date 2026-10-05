"""Intraday read path: the strategy contract against the quality table (plan Part A6, #20)."""

from datetime import date, datetime
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import intraday_access as ia
from signal_engine.backtest.data_readiness.contract import DataContract, resolve_contract
from signal_engine.timeutils import IST

D = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2025, 1, 2)]


def _quality():
    return pd.DataFrame({
        "symbol": "AAA", "session": pd.to_datetime(D), "cls": "legacy",
        "price_valid": [True, True, False, True], "volume_valid": [True, False, False, True],
        "final_bar_valid": [True, True, False, False]})


def _contract(families):
    return SimpleNamespace(field_families=families)


def test_undeclared_contract_requires_every_family():
    a = ia.availability(_quality(), ia.required_families(_contract(None)), [])
    assert list(a.available) == [True, False, False, False]
    assert list(a.reason) == [None, "volume_invalid", "price_invalid", "final_bar_invalid"]


def test_price_only_contract_uses_sessions_with_invalid_volume():
    a = ia.availability(_quality(), ("price",), [])
    assert list(a.available) == [True, True, False, True]


def test_usable_range_removal_applies_only_to_its_family():
    removals = [{"family": "volume", "year": "2025", "rate": 0.2}]
    assert list(ia.availability(_quality(), ("price", "volume"), removals).reason) == [
        None, "volume_invalid", "price_invalid", "usable_range_removed:volume"]
    assert ia.availability(_quality(), ("price",), removals).available.iloc[3]


def test_unknown_family_is_refused():
    with pytest.raises(ValueError):
        ia.required_families(_contract(("vwap",)))


def _con():
    rows = []
    for d in D:
        ts = int(datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST).timestamp())
        rows.append({"symbol": "AAA", "interval": "5m", "timestamp": ts, "open": 1.0,
                     "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0})
    con = duckdb.connect(":memory:")
    con.register("bars", pd.DataFrame(rows))
    con.execute("CREATE VIEW market_data AS SELECT * FROM bars")
    return con


def test_read_bars_serves_available_sessions_and_withholds_invalid_volume(monkeypatch):
    monkeypatch.setattr(ia, "load_quality", lambda m, root=None: _quality())
    m = {"dataset_version": "v", "frequency": "5m"}
    df = ia.read_bars(_con(), m, _contract(("price",)), ["AAA"], "5m", None)["AAA"]
    assert [t.date() for t in df.index] == [D[0], D[1], D[3]]
    assert df.Volume.isna().tolist() == [False, True, False]
    assert str(df.index.tz) == str(IST)
    out = ia.read_bars(_con(), m, _contract(None), ["AAA"], "5m", "2024-12-31")
    assert [t.date() for t in out["AAA"].index] == [D[0]]
    with pytest.raises(ValueError):
        ia.read_bars(_con(), m, _contract(None), ["AAA"], "1m", None)


def _frozen(intent):
    return SimpleNamespace(intent={"universe": ["AAA"], **intent}, resolved={})


def test_contract_reads_declared_field_families_for_intraday_only():
    c = resolve_contract(_frozen({"timeframe": "5m",
                                  "data": {"field_families": ["price", "volume"]}}))
    assert c.field_families == ("price", "volume")
    assert c.to_dict()["field_families"] == ["price", "volume"]
    daily = resolve_contract(_frozen({"timeframe": "1d"}))
    assert daily.field_families is None and "field_families" not in daily.to_dict()


def test_recorded_contracts_without_the_field_still_load():
    old = resolve_contract(_frozen({"timeframe": "1d"})).to_dict()
    old["universe_symbols"] = tuple(old["universe_symbols"])
    old["notes"] = tuple(old["notes"])
    assert DataContract(**old).field_families is None
