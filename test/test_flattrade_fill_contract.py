"""Flattrade per-fill reader against the generic ledger contract.

map_trade_fills (broker/flattrade/mapping/order_data.py) is the only Flattrade-specific
code the ledger uses. Uses the masked live fixture (5 rows, a 23+2 partial fill of a 25
order); get_oa_symbol is mocked, no network.

Run with: uv run pytest test/test_flattrade_fill_contract.py -q
"""

import copy
from datetime import datetime
from unittest.mock import patch

import pytest
from test_flattrade_tradebook_live_shape import LIVE_ROWS

from broker.flattrade.mapping import order_data as od
from services import discretionary_ledger_service as svc
from services import tradebook_service


@pytest.fixture(autouse=True)
def _symbols():
    with patch.object(
        od, "get_oa_symbol", side_effect=lambda sym, exch: {"TCS-EQ": "TCS"}.get(sym)
    ):
        yield


def _fills(rows=None):
    return od.map_trade_fills(copy.deepcopy(LIVE_ROWS if rows is None else rows))


def test_every_fill_satisfies_the_generic_contract():
    fills, skipped = _fills()
    assert skipped == 0 and len(fills) == 5
    for f in fills:
        assert set(f) == set(svc.FILL_FIELDS)
        assert svc.validate_fill(f, "flattrade") is not None
        assert isinstance(f["qty"], float) and isinstance(f["price"], float)
        assert isinstance(f["fill_time"], datetime) and f["fill_time"].tzinfo is None
        assert isinstance(f["fill_id"], str) and f["fill_id"]
        assert f["side"] in ("BUY", "SELL")


def test_symbol_and_product_are_openalgo_values():
    fills, _ = _fills()
    assert {f["symbol"] for f in fills} == {"TCS"}
    assert {f["product"] for f in fills} == {"MIS"}
    assert {f["account"] for f in fills} == {"ACCT01"}


@pytest.mark.parametrize(
    ("exch", "prd", "expected"),
    [
        ("NSE", "C", "CNC"),
        ("BSE", "C", "CNC"),
        ("NSE", "I", "MIS"),
        ("NFO", "I", "MIS"),
        ("NFO", "M", "NRML"),
        ("MCX", "M", "NRML"),
        ("NSE", "M", None),  # no OpenAlgo name: unverifiable, never stored raw
        ("NSE", "B", None),
    ],
)
def test_product_mapping_matches_the_existing_mapper(exch, prd, expected):
    row = dict(LIVE_ROWS[0], exch=exch, prd=prd)
    with patch.object(od, "get_oa_symbol", return_value="X"):
        fills, skipped = od.map_trade_fills([row])
    if expected is None:
        assert fills == [] and skipped == 1
    else:
        assert [f["product"] for f in fills] == [expected] and skipped == 0


def test_partial_fill_gives_two_fills_summing_to_the_order_while_legacy_stays_two_25s():
    fills, _ = _fills()
    parts = [f for f in fills if f["order_id"] == "261117000004"]
    assert sorted((f["fill_id"], f["qty"], f["price"]) for f in parts) == [
        ("900000004", 23.0, 2433.5),
        ("900000005", 2.0, 2433.5),
    ]
    assert sum(f["qty"] for f in parts) == 25.0
    assert sorted(f["fill_time"] for f in parts) == [
        datetime(2026, 11, 17, 12, 11, 30),
        datetime(2026, 11, 17, 12, 11, 32),
    ]
    # the legacy public path is untouched: order-level rows, two rows of 25
    legacy = od.transform_tradebook_data(od.map_trade_data(copy.deepcopy(LIVE_ROWS)))
    assert [r["quantity"] for r in legacy if r["orderid"] == "261117000004"] == [25, 25]


def test_no_order_level_fallback_for_missing_per_fill_fields():
    for drop in ("flid", "flqty", "flprc", "fltm"):
        row = {k: v for k, v in LIVE_ROWS[0].items() if k != drop}
        assert od.map_trade_fills([row]) == ([], 1), drop


def test_symbol_lookup_failure_skips_and_counts():
    rows = [dict(LIVE_ROWS[0]), dict(LIVE_ROWS[1], tsym="NOPE-EQ")]
    fills, skipped = od.map_trade_fills(rows)
    assert [f["fill_id"] for f in fills] == ["900000001"] and skipped == 1


def test_prices_stay_unrounded_and_input_is_not_mutated():
    rows = copy.deepcopy(LIVE_ROWS[:1])
    rows[0]["flprc"] = "2419.2037"
    before = copy.deepcopy(rows)
    fills, _ = od.map_trade_fills(rows)
    assert fills[0]["price"] == 2419.2037
    assert rows == before


def test_account_is_actid_then_uid_then_default():
    r = dict(LIVE_ROWS[0])
    assert od.map_trade_fills([r], "D")[0][0]["account"] == "ACCT01"
    r.pop("actid")
    assert od.map_trade_fills([r], "D")[0][0]["account"] == "ACCT01"  # uid
    r.pop("uid")
    assert od.map_trade_fills([r], "D")[0][0]["account"] == "D"


def test_declares_skip_window_and_error_markers():
    assert od.SYNC_SKIP_WINDOW == ((5, 0), (6, 0))
    assert od.map_trade_fills({"stat": "Not_Ok", "emsg": 'Error Occurred : 5 :"no data"'}) == (
        [],
        0,
    )
    with pytest.raises(od.FlattradeTradeBookError) as e:
        od.map_trade_fills({"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"})
    assert e.value.is_auth_error is True


def test_service_discovers_flattrade_by_name_and_reads_only_get_trade_book():
    reader = svc.load_broker_reader("flattrade")
    from broker.flattrade.api import order_api

    assert reader.map_trade_fills is od.map_trade_fills
    assert reader.get_trade_book is order_api.get_trade_book
    assert reader.skip_window == ((5, 0), (6, 0))
    assert not hasattr(order_api, "get_trade_book_raw")


def test_other_brokers_do_not_get_a_reader_or_window():
    for name in ("zerodha", "kotak", "groww", "angel"):
        assert svc.load_broker_reader(name) is None
    assert svc.load_broker_reader("fakebroker") is None
    assert tradebook_service.import_broker_module("flattrade") is not None


@pytest.mark.parametrize(
    "exch,prd,expected",
    [
        ("BFO", "M", "NRML"),
        ("BFO", "I", "MIS"),
        ("CDS", "M", "NRML"),
        ("CDS", "I", "MIS"),
        ("MCX", "I", "MIS"),
        ("BCD", "M", None),  # BCD has no entry in the product table: skipped and counted
        ("NFO", "C", None),  # CNC exists only on NSE / BSE
        ("MCX", "C", None),
    ],
)
def test_product_mapping_other_exchanges(exch, prd, expected):
    row = dict(LIVE_ROWS[0], exch=exch, prd=prd)
    with patch.object(od, "get_oa_symbol", return_value="X"):
        fills, skipped = od.map_trade_fills([row])
    if expected is None:
        assert fills == [] and skipped == 1
        assert (skipped.product_unmapped, skipped.symbol_unmapped, skipped.unmapped) == (1, 0, 1)
    else:
        assert [f["product"] for f in fills] == [expected] and skipped == 0


def test_skip_reasons_are_reported_separately():
    rows = [
        dict(LIVE_ROWS[0]),
        dict(LIVE_ROWS[1], tsym="NOPE-EQ"),
        dict(LIVE_ROWS[2], prd="B"),
        {k: v for k, v in LIVE_ROWS[3].items() if k != "flqty"},
    ]
    lookup = lambda sym, exch: None if sym == "NOPE-EQ" else "X"  # noqa: E731
    with patch.object(od, "get_oa_symbol", side_effect=lookup):
        fills, skipped = od.map_trade_fills(rows)
    assert len(fills) == 1 and skipped == 3
    assert (skipped.missing_fields, skipped.symbol_unmapped, skipped.product_unmapped) == (1, 1, 1)
    assert skipped.unmapped == 2
