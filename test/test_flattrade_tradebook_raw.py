"""Flattrade per-fill parser (map_trade_fills), plus proof the old mapper is unchanged.

Unit-level checks on masked, synthetic rows. The real-shaped sample (a live TradeBook
captured and masked) lives in test_flattrade_tradebook_live_shape.py and the contract
checks in test_flattrade_fill_contract.py. Fields: flid, fltm, flqty, flprc, norenordno,
uid, actid, exch, tsym, prd, trantype, qty, norentm, avgprc (may be absent). fltm
'01-01-1980 00:00:00' is the documented 'missing' placeholder.
No network call is made: the HTTP layer is mocked.

Run with: uv run pytest test/test_flattrade_tradebook_raw.py -q
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from broker.flattrade.mapping import order_data as raw
from broker.flattrade.mapping.order_data import transform_tradebook_data

OA_SYMBOLS = {("SBIN-EQ", "NSE"): "SBIN"}


@pytest.fixture(autouse=True)
def _symbol_lookup():
    with patch.object(
        raw, "get_oa_symbol", side_effect=lambda sym, exch: OA_SYMBOLS.get((sym, exch))
    ):
        yield


def _row(**kw):
    row = {
        "stat": "Ok",
        "flid": "FLID0001",
        "fltm": "17-11-2026 10:15:30",
        "flqty": "5",
        "flprc": "101.25",
        "norenordno": "ORD0001",
        "uid": "UID-MASKED",
        "actid": "ACT-MASKED",
        "exch": "NSE",
        "tsym": "SBIN-EQ",
        "prd": "I",
        "trantype": "B",
        "qty": "10",
        "prc": "0.00",
        "norentm": "10:15:29 17-11-2026",
    }
    row.update(kw)
    return {k: v for k, v in row.items() if v is not None}


def test_normalizes_documented_fields():
    fills, skipped = raw.map_trade_fills([_row()])
    assert skipped == 0
    (f,) = fills
    assert f["fill_id"] == "FLID0001"
    assert f["account"] == "ACT-MASKED"
    assert f["order_id"] == "ORD0001"
    assert f["side"] == "BUY"
    assert f["qty"] == 5.0  # fill qty, not the order qty of 10
    assert f["price"] == 101.25
    assert f["fill_time"] == datetime(2026, 11, 17, 10, 15, 30)
    assert f["symbol"] == "SBIN" and f["exchange"] == "NSE" and f["product"] == "MIS"
    assert len(f["raw_hash"]) == 64


def test_placeholder_1980_fltm_is_skipped_not_replaced_by_order_time():
    fills, skipped = raw.map_trade_fills([_row(fltm="01-01-1980 00:00:00")])
    assert fills == [] and skipped == 1  # norentm is order time, never substituted


def test_missing_fltm_is_skipped_even_with_norentm():
    fills, skipped = raw.map_trade_fills([_row(fltm=None)])
    assert fills == [] and skipped == 1


def test_missing_flprc_is_skipped_even_with_avgprc_and_prc():
    fills, skipped = raw.map_trade_fills(
        [_row(flprc=None, avgprc="99.5"), _row(flprc=None, prc="98.0", flid="X2")]
    )
    assert fills == [] and skipped == 2


def test_missing_flqty_is_skipped_even_with_order_qty():
    fills, skipped = raw.map_trade_fills([_row(flqty=None, qty="100")])
    assert fills == [] and skipped == 1


def test_missing_flid_is_skipped():
    fills, skipped = raw.map_trade_fills([_row(flid=None), _row(flid="  ", trantype="S")])
    assert fills == [] and skipped == 2


def test_zero_or_garbage_fill_values_are_skipped():
    rows = [_row(flprc="0"), _row(flqty="0", flid="X2"), _row(flqty="abc", flid="X3")]
    fills, skipped = raw.map_trade_fills(rows)
    assert fills == [] and skipped == 3


def test_partial_fills_keep_their_own_quantity_and_price():
    rows = [
        _row(flid="F1", flqty="40", flprc="100.0", qty="100", avgprc="100.4"),
        _row(flid="F2", flqty="60", flprc="100.5", qty="100", avgprc="100.4"),
    ]
    fills, skipped = raw.map_trade_fills(rows)
    assert skipped == 0
    assert [(f["qty"], f["price"]) for f in fills] == [(40.0, 100.0), (60.0, 100.5)]


def test_sell_side_and_good_rows_survive_alongside_skipped_ones():
    fills, skipped = raw.map_trade_fills([_row(trantype="S"), _row(flprc=None, flid="BAD")])
    assert skipped == 1 and [f["side"] for f in fills] == ["SELL"]


def test_extract_rows_empty_book_and_errors():
    assert raw._fill_rows({"stat": "Not_Ok", "emsg": 'Error Occurred : 5 :"no data"'}) == []
    assert raw._fill_rows([_row()]) == [_row()]
    with pytest.raises(raw.FlattradeTradeBookError) as e:
        raw._fill_rows({"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"})
    assert e.value.is_auth_error is True
    with pytest.raises(raw.FlattradeTradeBookError) as e2:
        raw._fill_rows({"stat": "Not_Ok", "emsg": "something else"})
    assert e2.value.is_auth_error is False


def test_existing_mapper_behaviour_unchanged():
    """transform_tradebook_data still ignores flid/flqty/flprc/fltm and reads qty/avgprc."""
    (t,) = transform_tradebook_data([_row(avgprc="100.0")])
    assert t == {
        "symbol": "SBIN-EQ",
        "exchange": "NSE",
        "product": "I",
        "action": "B",
        "quantity": 10,
        "average_price": 100.0,
        "trade_value": 1000.0,
        "orderid": "ORD0001",
        "timestamp": "10:15:29 17-11-2026",
    }
