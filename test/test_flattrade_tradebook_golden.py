"""Golden test: the public tradebook output for the masked live Flattrade rows.

The real chain services.tradebook_service.get_tradebook -> import_broker_module ->
map_trade_data -> transform_tradebook_data -> attach_strategy -> format_trade_data runs
unmodified. Only the raw broker call (get_trade_book), the symbol DB lookup
(get_oa_symbol), the strategy-tag DB lookup, the analyze-mode flag and the API-key
resolution are mocked. The expected JSON below was recorded from the code BEFORE the
per-fill ledger work (map_trade_fills) was added and must never change with it: the
public /api/v1/tradebook output is byte-identical.

Run with: uv run pytest test/test_flattrade_tradebook_golden.py -q
"""

import copy
import json
from unittest.mock import patch

import pytest
from flask import Flask
from flask_restx import Api
from test_flattrade_tradebook_live_shape import LIVE_ROWS

from limiter import limiter
from restx_api import tradebook as tradebook_api
from services import tradebook_service

SYMBOLS = {("TCS-EQ", "NSE"): "TCS"}
TAGS = {"261117000001": "Alpha", "261117000004": "Beta"}

EXPECTED_JSON = """{
 "data": [
  {
   "action": "SELL",
   "average_price": 2419.2,
   "exchange": "NSE",
   "orderid": "261117000001",
   "product": "MIS",
   "quantity": 45,
   "strategy": "Alpha",
   "symbol": "TCS",
   "timestamp": "13:05:39 17-11-2026",
   "trade_value": 108864.0
  },
  {
   "action": "BUY",
   "average_price": 2425.0,
   "exchange": "NSE",
   "orderid": "261117000002",
   "product": "MIS",
   "quantity": 10,
   "strategy": "",
   "symbol": "TCS",
   "timestamp": "12:44:30 17-11-2026",
   "trade_value": 24250.0
  },
  {
   "action": "BUY",
   "average_price": 2431.5,
   "exchange": "NSE",
   "orderid": "261117000003",
   "product": "MIS",
   "quantity": 10,
   "strategy": "",
   "symbol": "TCS",
   "timestamp": "12:24:49 17-11-2026",
   "trade_value": 24315.0
  },
  {
   "action": "BUY",
   "average_price": 2433.5,
   "exchange": "NSE",
   "orderid": "261117000004",
   "product": "MIS",
   "quantity": 25,
   "strategy": "Beta",
   "symbol": "TCS",
   "timestamp": "12:11:32 17-11-2026",
   "trade_value": 60837.5
  },
  {
   "action": "BUY",
   "average_price": 2433.5,
   "exchange": "NSE",
   "orderid": "261117000004",
   "product": "MIS",
   "quantity": 25,
   "strategy": "Beta",
   "symbol": "TCS",
   "timestamp": "12:11:30 17-11-2026",
   "trade_value": 60837.5
  }
 ],
 "status": "success"
}"""


def _patches():
    return (
        patch(
            "broker.flattrade.api.order_api.get_trade_book",
            side_effect=lambda auth: copy.deepcopy(LIVE_ROWS),
        ),
        patch(
            "broker.flattrade.mapping.order_data.get_oa_symbol",
            side_effect=lambda symbol, exchange: SYMBOLS.get((symbol, exchange)),
        ),
        patch(
            "services.strategy_tag_enrichment.get_strategies_for_orderids",
            side_effect=lambda ids: {i: TAGS[i] for i in ids if i in TAGS},
        ),
        patch("database.settings_db.get_analyze_mode", return_value=False),
        patch(
            "services.tradebook_service.get_auth_token_broker",
            return_value=("TOKEN-MASKED", "flattrade"),
        ),
    )


def _run(call):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        return call()


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=1)


def test_service_output_is_golden():
    ok, body, code = _run(lambda: tradebook_service.get_tradebook(api_key="K"))
    assert (ok, code) == (True, 200)
    assert _canon(body) == EXPECTED_JSON


def test_internal_call_output_is_golden():
    ok, body, code = _run(
        lambda: tradebook_service.get_tradebook(auth_token="TOKEN-MASKED", broker="flattrade")
    )
    assert (ok, code) == (True, 200)
    assert _canon(body) == EXPECTED_JSON


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    monkeypatch.setattr(limiter, "enabled", False)
    Api(app).add_namespace(tradebook_api.api, path="/api/v1/tradebook")
    return app.test_client()


def test_api_v1_tradebook_response_is_golden(client):
    resp = _run(lambda: client.post("/api/v1/tradebook", json={"apikey": "K"}))
    assert resp.status_code == 200
    assert _canon(resp.get_json()) == EXPECTED_JSON


def test_partial_fill_still_two_legacy_order_level_rows():
    """get_tradebook keeps returning order-level rows (23+2 fill -> two rows of 25)."""
    _, body, _ = _run(lambda: tradebook_service.get_tradebook(api_key="K"))
    rows = [r for r in body["data"] if r["orderid"] == "261117000004"]
    assert [r["quantity"] for r in rows] == [25, 25]
