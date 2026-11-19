"""Source identification by explicit order tags and broker order evidence.

Everything is mocked: no broker call, no order, no HTTP (an autouse spy fails the test if any
httpx request is attempted). Temp SQLite only. Fixtures are real-shaped and masked (order ids,
account ids, IP addresses are synthetic).

Run with: uv run pytest test/test_discretionary_source_identification.py -q
"""

import copy
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from database import discretionary_book_db as book
from database import discretionary_ledger_db as ledger
from database import strategy_book_db as sb
from services import discretionary_ledger_service as svc
from utils import order_source_tag as ost

FLAGS = (
    "DISCRETIONARY_ORDER_TAG_ENABLED",
    "DISCRETIONARY_SOURCE_FROM_ORDERBOOK",
    "DISCRETIONARY_ORDERSOURCE_MANUAL",
    "DISCRETIONARY_ORDERSOURCE_API_VERIFIED",
    "DISCRETIONARY_ENGINE_LIVE_ONLY",
)


@pytest.fixture(autouse=True)
def http_spy(monkeypatch):
    """Every flag starts OFF, and any real outbound httpx request fails the test."""
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)
    calls = []

    def refuse(self, request, *a, **k):
        calls.append(str(request.url))
        raise AssertionError(f"outbound HTTP attempted: {request.url}")

    async def refuse_async(self, request, *a, **k):
        calls.append(str(request.url))
        raise AssertionError(f"outbound HTTP attempted: {request.url}")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse_async)
    yield calls
    assert calls == [], calls


def flags_on(monkeypatch, *names):
    for name in names:
        monkeypatch.setenv(name, "true")


# ------------------------------------------------------------------ tag format


def test_tag_format_length_charset_and_version():
    tag = ost.build_order_tag("Momentum Long", "live")
    assert tag == "OA1L" + ost.strategy_code("Momentum Long")
    assert len(tag) == 12 <= ost.TAG_MAX_LEN <= 20
    assert tag.isalnum() and tag.isascii() and tag == tag.upper()
    assert ost.build_order_tag("  Momentum Long ", "LIVE") == tag  # stripped, case of mode
    assert ost.build_order_tag("Momentum Long v2") != tag  # no shared-prefix collision
    long_name = "X" * 300 + " & ünïcode"
    assert len(ost.build_order_tag(long_name)) == 12
    assert ost.parse_remarks(tag) == {"kind": "tag", "mode": "L", "code": tag[4:]}


@pytest.mark.parametrize(
    "strategy,mode",
    [
        ("", "live"),
        ("   ", "live"),
        (None, "live"),
        (123, "live"),
        ("UI Exit Position", "live"),
        ("Squareoff", "live"),
        ("S1", "analyze"),
        ("S1", "paper"),
        ("S1", ""),
    ],
)
def test_no_tag_for_non_strategy_or_non_live(strategy, mode):
    assert ost.build_order_tag(strategy, mode) is None


def test_tag_build_never_raises(monkeypatch):
    def boom(_):
        raise RuntimeError("x")

    monkeypatch.setattr(ost, "strategy_code", boom)
    assert ost.build_order_tag("S1") is None


@pytest.mark.parametrize(
    "value,kind",
    [
        ("OA1L9F3A2C1B", "tag"),
        ("  OA1L9F3A2C1B  ", "tag"),
        ("OA1P9F3A2C1B", "tag"),  # parsed; classify rejects a non-live mode
        ("OA1L9F3A2C1", "malformed"),
        ("OA1L9F3A2C1BZ", "malformed"),
        ("OA1l9f3a2c1b", "malformed"),
        ("OA1-L-9F3A2C1B", "malformed"),
        ("OA2L9F3A2C1B", "other_version"),
        ("OA9", "other_version"),
        ("", "empty"),
        ("   ", "empty"),
        (None, "empty"),
        (17, "empty"),
        # a broker-generated note with a price is not an OpenAlgo tag
        ("Order price 2357.40 modified at 11:36:22 by RMS web", "foreign"),
        ("my note", "foreign"),
    ],
)
def test_parse_remarks(value, kind):
    assert ost.parse_remarks(value)["kind"] == kind


# ------------------------------------------------------------------ Flattrade payload


ORDER = {
    "apikey": "OPENALGO-KEY-MASKED",
    "strategy": "Momentum Long",
    "symbol": "TCS",
    "exchange": "NSE",
    "action": "BUY",
    "quantity": "1",
    "pricetype": "LIMIT",
    "product": "MIS",
    "price": "2000",
    "trigger_price": "0",
    "disclosed_quantity": "0",
}

# The PlaceOrder body as the code before this change built it for ORDER (key order included).
BASELINE_JDATA = {
    "uid": "FTUSER",
    "actid": "FTUSER",
    "exch": "NSE",
    "tsym": "TCS-EQ",
    "qty": "1",
    "prc": "2000",
    "trgprc": "0",
    "dscqty": "0",
    "prd": "I",
    "trantype": "B",
    "prctyp": "LMT",
    "mkt_protection": "0",
    "ret": "DAY",
    "ordersource": "API",
}
BASELINE_PAYLOAD = "jData=" + json.dumps(BASELINE_JDATA) + "&jKey=AUTH-MASKED"


class FakeClient:
    def __init__(self):
        self.posts = []

    def post(self, url, content=None, headers=None):
        self.posts.append((url, content))
        return SimpleNamespace(
            status_code=200, json=lambda: {"stat": "Ok", "norenordno": "26111900000999"}
        )


@pytest.fixture
def flattrade_api(monkeypatch):
    from broker.flattrade.api import order_api
    from broker.flattrade.mapping import transform_data as td

    client = FakeClient()
    monkeypatch.setenv("BROKER_API_KEY", "FTUSER:::SECRET")
    monkeypatch.setattr(order_api, "get_httpx_client", lambda: client)
    monkeypatch.setattr(order_api, "get_token", lambda s, e: "11536")
    monkeypatch.setattr(order_api.ORDER_LIMITER, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(td, "get_br_symbol", lambda s, e: f"{s}-EQ")
    return SimpleNamespace(api=order_api, client=client)


def _place(flattrade_api, order=None):
    import restx_api  # noqa: F401  (place_order_service import cycle resolves through it)
    from services.place_order_service import _place_with_source_tag

    data = copy.deepcopy(order or ORDER)
    res, resp, oid = _place_with_source_tag(flattrade_api.api, data, "AUTH-MASKED")
    assert oid == "26111900000999"
    return flattrade_api.client.posts[-1][1], data


def test_payload_flag_off_is_byte_identical(flattrade_api):
    payload, data = _place(flattrade_api)
    assert payload == BASELINE_PAYLOAD
    assert ost.ORDER_TAG_KEY not in data
    # the direct broker call (no hook at all) builds the very same bytes
    flattrade_api.api.place_order_api(copy.deepcopy(ORDER), "AUTH-MASKED")
    assert flattrade_api.client.posts[-1][1] == BASELINE_PAYLOAD


def test_payload_flag_on_adds_only_remarks(flattrade_api, monkeypatch):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    payload, data = _place(flattrade_api)
    tag = ost.build_order_tag("Momentum Long")
    expected = dict(BASELINE_JDATA, remarks=tag)
    assert payload == "jData=" + json.dumps(expected) + "&jKey=AUTH-MASKED"
    assert ost.ORDER_TAG_KEY not in data  # removed after the call


@pytest.mark.parametrize("strategy", ["UI Exit Position", "Squareoff", ""])
def test_payload_flag_on_non_strategy_orders_untagged(flattrade_api, monkeypatch, strategy):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    payload, _ = _place(flattrade_api, dict(ORDER, strategy=strategy))
    assert payload == BASELINE_PAYLOAD


def test_tag_build_failure_does_not_alter_the_order(flattrade_api, monkeypatch):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")

    def boom(*a, **k):
        raise RuntimeError("tag failure")

    monkeypatch.setattr(ost, "build_order_tag", boom)
    payload, data = _place(flattrade_api)
    assert payload == BASELINE_PAYLOAD
    assert ost.ORDER_TAG_KEY not in data


def test_tag_key_removed_even_when_the_broker_call_raises(monkeypatch):
    import restx_api  # noqa: F401
    from services.place_order_service import _place_with_source_tag

    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    seen = {}

    def place_order_api(data, auth):
        seen.update(data)
        raise RuntimeError("network")

    module = SimpleNamespace(ACCEPTS_ORDER_SOURCE_TAG=True, place_order_api=place_order_api)
    data = copy.deepcopy(ORDER)
    with pytest.raises(RuntimeError):
        _place_with_source_tag(module, data, "A")
    assert seen[ost.ORDER_TAG_KEY] == ost.build_order_tag("Momentum Long")
    assert data == ORDER


def test_other_brokers_never_receive_the_key(monkeypatch):
    import restx_api  # noqa: F401
    from services.place_order_service import _place_with_source_tag

    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    seen = []
    module = SimpleNamespace(place_order_api=lambda d, a: (seen.append(dict(d)), None, "1")[1:])
    data = copy.deepcopy(ORDER)
    _place_with_source_tag(module, data, "A")
    assert seen == [ORDER] and data == ORDER


def test_transform_ignores_a_key_when_the_flag_is_off_or_the_tag_invalid(monkeypatch):
    from broker.flattrade.mapping import transform_data as td

    monkeypatch.setattr(td, "get_br_symbol", lambda s, e: f"{s}-EQ")
    data = dict(ORDER, apikey="FTUSER")
    data[ost.ORDER_TAG_KEY] = ost.build_order_tag("S1")
    assert "remarks" not in td.transform_data(dict(data), "1")  # flag off
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    assert td.transform_data(dict(data), "1")["remarks"] == data[ost.ORDER_TAG_KEY]
    data[ost.ORDER_TAG_KEY] = "hello & bye"
    assert "remarks" not in td.transform_data(dict(data), "1")


def test_analyze_mode_order_never_reaches_the_tag_hook(monkeypatch):
    import restx_api  # noqa: F401  (import order of place_order_service)
    from services import place_order_service as pos

    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    monkeypatch.setattr(pos, "get_analyze_mode", lambda: True)
    with (
        patch("services.sandbox_service.sandbox_place_order", return_value=(True, {}, 200)),
        patch.object(pos, "_place_with_source_tag") as hook,
        patch.object(pos.bus, "publish"),
    ):
        pos.place_order_with_auth(copy.deepcopy(ORDER), "A", "flattrade", dict(ORDER))
    assert not hook.called


def test_live_place_order_path_tags_through_the_service(flattrade_api, monkeypatch):
    import restx_api  # noqa: F401
    from services import place_order_service as pos

    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    monkeypatch.setattr(pos, "get_analyze_mode", lambda: False)
    with patch.object(pos.bus, "publish") as pub:
        ok, resp, code = pos.place_order_with_auth(
            copy.deepcopy(ORDER), "AUTH-MASKED", "flattrade", dict(ORDER)
        )
    assert (ok, code, resp["orderid"]) == (True, 200, "26111900000999")
    body = json.loads(flattrade_api.client.posts[-1][1][len("jData=") : -len("&jKey=AUTH-MASKED")])
    assert body["remarks"] == ost.build_order_tag("Momentum Long")
    event = pub.call_args[0][0]
    assert ost.ORDER_TAG_KEY not in json.dumps(event.request_data)


# ------------------------------------------------------------------ OrderBook helper

ACT = "FT00XX01"
TCS_ORDERS = [  # masked ids; 5 complete (the TCS fills) + 2 cancelled
    ("26111900000101", "S", "25", "COMPLETE", "11:36:20 19-11-2026"),
    ("26111900000102", "S", "10", "COMPLETE", "12:22:03 19-11-2026"),
    ("26111900000103", "B", "25", "COMPLETE", "12:22:13 19-11-2026"),
    ("26111900000104", "S", "10", "COMPLETE", "12:23:10 19-11-2026"),
    ("26111900000105", "B", "20", "COMPLETE", "12:25:50 19-11-2026"),
    ("26111900000106", "B", "10", "CANCELED", "12:24:00 19-11-2026"),
    ("26111900000107", "S", "5", "CANCELED", "12:24:30 19-11-2026"),
]
BROKER_NOTE = "Order price 2357.40 modified at 11:36:22 by RMS web"


def orderbook_rows():
    rows = []
    for oid, side, qty, status, when in TCS_ORDERS:
        row = {
            "stat": "Ok",
            "norenordno": oid,
            "uid": ACT,
            "actid": ACT,
            "exch": "NSE",
            "tsym": "TCS-EQ",
            "qty": qty,
            "trantype": side,
            "prctyp": "LMT",
            "ret": "DAY",
            "prd": "I",
            "status": status,
            "st_intrn": status,
            "norentm": when,
            "ordersource": "WEB",
            "src_uid": ACT,
            "ui_dev_code": "WEB-MASKED",
            "ipaddr": "10.0.0.1",
            "kidid": "1",
        }
        if status == "COMPLETE":
            row["fillshares"] = qty
        rows.append(row)
    rows[0]["remarks"] = BROKER_NOTE
    return rows


def test_map_order_sources_on_the_real_shaped_orderbook():
    from broker.flattrade.mapping.order_data import map_order_sources

    raw = orderbook_rows()
    snapshot = copy.deepcopy(raw)
    out = map_order_sources(raw)
    assert raw == snapshot  # pure
    assert len(out) == 7
    assert {v["ordersource"] for v in out.values()} == {"WEB"}
    assert out["26111900000101"]["remarks"] == BROKER_NOTE
    assert len(BROKER_NOTE) == 51
    assert [k for k, v in out.items() if v["remarks"]] == ["26111900000101"]
    assert [k for k, v in out.items() if v["status"] == "CANCELED"] == [
        "26111900000106",
        "26111900000107",
    ]
    assert all("fillshares" not in r for r in raw if r["status"] == "CANCELED")
    assert out["26111900000105"]["norentm"] == "12:25:50 19-11-2026"
    assert ost.parse_remarks(out["26111900000101"]["remarks"])["kind"] == "foreign"


def test_map_order_sources_empty_error_and_tradebook_rows():
    from broker.flattrade.mapping.order_data import FlattradeTradeBookError, map_order_sources

    assert map_order_sources({"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}) == {}
    with pytest.raises(FlattradeTradeBookError) as e:
        map_order_sources({"stat": "Not_Ok", "emsg": "Session Expired :  Invalid Session Key"})
    assert e.value.is_auth_error
    # TradeBook rows: no ordersource, no remarks (as observed); repeated ids collapse
    tb = [
        {"norenordno": "X1", "flid": "1"},
        {"norenordno": "X1", "flid": "2", "remarks": "OA1LAAAAAAAA"},
    ]
    assert map_order_sources(tb) == {
        "X1": dict(
            dict.fromkeys(
                (
                    "ordersource",
                    "status",
                    "norentm",
                    "src_uid",
                    "uid",
                    "actid",
                    "algo_name",
                    "snonum",
                )
            ),
            remarks="OA1LAAAAAAAA",
        )
    }


# ------------------------------------------------------------------ classify decision table

DAY = datetime(2026, 11, 19, 12, 0, 0)
ANCHOR = datetime(2026, 11, 18, 9, 0, 0)  # covered from 2026-11-19
FILL = {"order_id": "O1", "broker": "flattrade", "fill_time": DAY}
JUST_AFTER = DAY + timedelta(seconds=30)  # inside the 30 min grace


def ev(ob="ok", os_="WEB", rk="empty", tc=None, tm=None):
    return {"ob": ob, "os": os_, "rk": rk, "tc": tc, "tm": tm, "rs": None}


def cls(evidence=None, tags=None, unknown=(), engine=None, anchor=ANCHOR, now=JUST_AFTER, **kw):
    return svc.classify_with_evidence(
        dict(FILL),
        tags or {},
        set(unknown),
        set() if engine is None else engine,
        anchor,
        now,
        evidence=evidence,
        code_names=kw.get("code_names", {}),
    )


CODE = ost.strategy_code("Momentum Long")


def test_remarks_tag_is_signal_live_immediately_without_coverage(monkeypatch):
    state, strategy, reason, source, label = cls(
        ev(os_="API", rk="tag", tc=CODE, tm="L"), anchor=None, code_names={CODE: "Momentum Long"}
    )
    assert (state, strategy, source) == ("strategy_tag", "Momentum Long", "remarks")
    assert svc.source_label(state) == "SIGNAL_LIVE"
    assert label == f"remarks tag {CODE}"
    # unknown code: still an OpenAlgo strategy order; name unknown
    assert cls(ev(os_="API", rk="tag", tc=CODE, tm="L"))[:2] == ("strategy_tag", f"tag:{CODE}")
    # even when the OrderBook row was missing (TradeBook echo) or unread
    assert cls(ev(ob="error", os_=None, rk="tag", tc=CODE, tm="L"))[0] == "strategy_tag"


def test_remarks_tag_conflicting_with_registered_tag_is_unclassified():
    tags = {"O1": [("Other Strategy", "flattrade")]}
    state, *_rest, label = cls(ev(rk="tag", tc=CODE, tm="L"), tags=tags)
    assert state == "unresolved" and "vs registered" in label
    ok = {"O1": [("Momentum Long", "flattrade")]}
    assert cls(ev(rk="tag", tc=CODE, tm="L"), tags=ok)[:2] == ("strategy_tag", "Momentum Long")
    ui = {"O1": [("UI Exit Position", "flattrade")]}
    assert cls(ev(rk="tag", tc=CODE, tm="L"), tags=ui)[0] == "unresolved"


@pytest.mark.parametrize(
    "evidence",
    [
        ev(rk="tag", tc=CODE, tm="P"),  # non-live mode at a broker: anomaly
        ev(rk="malformed"),
        ev(rk="other_version"),
    ],
)
def test_unrecognised_openalgo_remarks_never_manual(monkeypatch, evidence):
    flags_on(
        monkeypatch, "DISCRETIONARY_ORDERSOURCE_MANUAL", "DISCRETIONARY_ORDERSOURCE_API_VERIFIED"
    )
    assert cls(evidence, now=DAY + timedelta(days=2))[0] == "unresolved"


def test_web_ordersource_complete_read_gate_on_is_manual_immediately(monkeypatch):
    flags_on(
        monkeypatch, "DISCRETIONARY_ORDERSOURCE_MANUAL", "DISCRETIONARY_ORDERSOURCE_API_VERIFIED"
    )
    state, _, reason, source, label = cls(ev(rk="foreign"))
    assert (state, source) == ("manual", "ordersource")
    assert svc.source_label(state) == "MANUAL"
    assert label == "ordersource WEB, orderbook ok" and "tag coverage ok" in reason


@pytest.mark.parametrize(
    "gate", [(), ("DISCRETIONARY_ORDERSOURCE_MANUAL",), ("DISCRETIONARY_ORDERSOURCE_API_VERIFIED",)]
)
def test_web_ordersource_without_both_flags_falls_back_to_grace(monkeypatch, gate):
    flags_on(monkeypatch, *gate)
    assert cls(ev())[:3] == ("unresolved", None, "within manual grace period")
    assert cls(ev(), now=DAY + timedelta(minutes=31))[0] == "manual"


def test_web_ordersource_still_needs_coverage_and_a_clean_registry(monkeypatch):
    flags_on(
        monkeypatch, "DISCRETIONARY_ORDERSOURCE_MANUAL", "DISCRETIONARY_ORDERSOURCE_API_VERIFIED"
    )
    assert cls(ev(), anchor=DAY)[2] == "fill date before tag coverage"  # not covered
    assert cls(ev(), anchor=None)[0] == "unresolved"
    assert cls(ev(), unknown={"O1"})[0] == "unresolved"
    assert svc.classify(dict(FILL), {}, set(), None, ANCHOR, JUST_AFTER, evidence=ev())[0] == (
        "unresolved"
    )  # engine db unreadable
    assert cls(ev(), engine={"O1"})[0] == "strategy_engine"
    assert cls(ev(), tags={"O1": [("S1", "flattrade")]})[0] == "strategy_tag"


@pytest.mark.parametrize("gate", [False, True])
def test_api_ordersource_without_tag_is_unclassified_forever(monkeypatch, gate):
    if gate:
        flags_on(
            monkeypatch,
            "DISCRETIONARY_ORDERSOURCE_MANUAL",
            "DISCRETIONARY_ORDERSOURCE_API_VERIFIED",
        )
    for source in ("API", "api"):
        state, *_r, label = cls(ev(os_=source), now=DAY + timedelta(days=3))
        assert state == "unresolved" and label == "ordersource API, no tag"


@pytest.mark.parametrize(
    "evidence",
    [
        None,  # setting off / never read
        ev(ob="error", os_=None),  # OrderBook unreadable
        ev(ob="missing", os_=None),  # order not in the OrderBook
        ev(os_=None),  # ordersource absent
        ev(os_=""),  # ordersource blank
    ],
)
def test_no_positive_evidence_falls_back_to_legacy(monkeypatch, evidence):
    flags_on(
        monkeypatch, "DISCRETIONARY_ORDERSOURCE_MANUAL", "DISCRETIONARY_ORDERSOURCE_API_VERIFIED"
    )
    assert cls(evidence)[:3] == ("unresolved", None, "within manual grace period")
    assert cls(evidence, now=DAY + timedelta(minutes=31))[0] == "manual"
    assert cls(evidence, anchor=DAY, now=DAY + timedelta(days=1))[0] == "unresolved"


def test_evidence_none_is_exactly_the_legacy_classify():
    legacy = svc.classify(dict(FILL), {}, set(), set(), ANCHOR, JUST_AFTER)
    assert legacy == ("unresolved", None, "within manual grace period", "none")


def test_build_evidence_merges_tradebook_tag_and_never_stores_raw_remarks():
    row = {"remarks": BROKER_NOTE, "ordersource": "WEB"}
    e = svc.build_evidence(row, None, True)
    assert e == {
        "ob": "ok",
        "os": "WEB",
        "rk": "foreign",
        "tm": None,
        "tc": None,
        "rs": "orderbook",
    }
    assert "2357" not in json.dumps(e)
    tag = ost.build_order_tag("S1")
    e = svc.build_evidence({"ordersource": "API"}, {"remarks": tag}, True)
    assert (e["rk"], e["tc"], e["rs"]) == ("tag", tag[4:], "tradebook")
    assert svc.build_evidence(None, None, True)["ob"] == "missing"
    assert svc.build_evidence(None, None, False)["ob"] == "error"


# ------------------------------------------------------------------ engine live-only


def _trades_db(path, with_mode=True):
    con = sqlite3.connect(path)
    cols = "id INTEGER PRIMARY KEY, order_id TEXT, strategy TEXT" + (
        ", trade_mode TEXT" if with_mode else ""
    )
    con.execute(f"CREATE TABLE trades ({cols})")
    if with_mode:
        con.executemany(
            "INSERT INTO trades (order_id, strategy, trade_mode) VALUES (?, 's', ?)",
            [
                ("L1", "live"),
                ("A1", "analyze"),
                ("U1", "unknown"),
                ("N1", None),
                ("M1", "live"),
                ("M1", "analyze"),
            ],
        )
    else:
        con.execute("INSERT INTO trades (order_id, strategy) VALUES ('L1', 's')")
    con.commit()
    con.close()


def test_engine_live_only_filter(tmp_path, monkeypatch):
    path = tmp_path / "trades.db"
    _trades_db(path)
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", str(path))
    ids = ["L1", "A1", "U1", "N1", "M1", "ZZ"]
    assert svc._engine_known_order_ids(ids) == {"L1", "A1", "U1", "N1", "M1"}  # flag off
    flags_on(monkeypatch, "DISCRETIONARY_ENGINE_LIVE_ONLY")
    got = svc._engine_known_order_ids(ids)
    assert got == {"L1", "M1"} and got.uncertain == {"U1", "N1"}
    f = {"order_id": "U1", "broker": "flattrade", "fill_time": DAY}
    assert svc.classify(f, {}, set(), got, ANCHOR, DAY + timedelta(days=1))[0] == "unresolved"
    f["order_id"] = "A1"  # an analyze row no longer blocks manual
    assert svc.classify(f, {}, set(), got, ANCHOR, DAY + timedelta(days=1))[0] == "manual"


def test_engine_live_only_without_mode_column_is_uncertain(tmp_path, monkeypatch):
    path = tmp_path / "trades.db"
    _trades_db(path, with_mode=False)
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", str(path))
    flags_on(monkeypatch, "DISCRETIONARY_ENGINE_LIVE_ONLY")
    got = svc._engine_known_order_ids(["L1"])
    assert got == set() and got.uncertain == {"L1"}


# ------------------------------------------------------------------ sync end to end (TCS)

TCS_FILLS = [  # (flid, order, side, qty, price, time) - 2026-11-19, real-shaped, masked ids
    ("F101", "26111900000101", "S", "25", "2357.4", "11:36:22"),
    ("F102", "26111900000102", "S", "10", "2345.7", "12:22:04"),
    ("F103", "26111900000103", "B", "5", "2346.9", "12:22:14"),
    ("F104", "26111900000103", "B", "19", "2346.9", "12:22:17"),
    ("F105", "26111900000103", "B", "1", "2346.9", "12:22:20"),
    ("F106", "26111900000104", "S", "10", "2346.7", "12:23:11"),
    ("F107", "26111900000105", "B", "20", "2348.1", "12:25:51"),
]
LAST_FILL = datetime(2026, 11, 19, 12, 25, 51)


def tradebook_rows():
    rows = []
    for flid, oid, side, qty, price, when in TCS_FILLS:
        rows.append(
            {
                "stat": "Ok",
                "norenordno": oid,
                "uid": ACT,
                "actid": ACT,
                "exch": "NSE",
                "prctyp": "LMT",
                "ret": "DAY",
                "prd": "I",
                "flid": flid,
                "fltm": f"19-11-2026 {when}",
                "trantype": side,
                "tsym": "TCS-EQ",
                "qty": qty,
                "token": "11536",
                "fillshares": qty,
                "flqty": qty,
                "pp": "2",
                "ls": "1",
                "ti": "0.10",
                "prc": price,
                "flprc": price,
                "norentm": f"{when} 19-11-2026",
                "exch_tm": f"19-11-2026 {when}",
                "exchordid": "1100000000" + flid[1:],
                "avgprc": price,
                "cstFrm": "C",
                "dname": "TCS",
                "ordersource_absent_in_tradebook": None,
            }
        )
    for r in rows:
        r.pop("ordersource_absent_in_tradebook")
    return rows


@pytest.fixture
def lenv(tmp_path, monkeypatch):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'db.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    mods = (ledger, sb, book)
    originals = [m.engine for m in mods]
    for m in mods:
        m.db_session.remove()
        m.db_session.configure(bind=eng)
        monkeypatch.setattr(m, "engine", eng)
    ledger.init_discretionary_ledger_db()
    book.init_discretionary_book_db()
    sb.Base.metadata.create_all(eng)
    se = tmp_path / "trades.db"
    con = sqlite3.connect(se)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, order_id TEXT, strategy TEXT)")
    con.commit()
    con.close()
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", str(se))
    # coverage: an earlier successful sync (2026-11-18) makes 2026-11-19 a covered day
    ledger.record_sync_run(
        broker="flattrade",
        account=ACT,
        started_at=datetime(2026, 11, 18, 15, 40),
        finished_at=datetime(2026, 11, 18, 15, 40, 5),
        status="ok",
        fetched=0,
        inserted=0,
    )
    state = SimpleNamespace(eng=eng, ob_calls=0, tb_calls=0, ob=orderbook_rows(), ob_fail=False)
    yield state
    for m, o in zip(mods, originals, strict=True):
        m.db_session.remove()
        m.db_session.configure(bind=o)


def run_sync(state, now):
    def get_trade_book(auth):
        state.tb_calls += 1
        return tradebook_rows()

    def get_order_book(auth):
        state.ob_calls += 1
        if state.ob_fail:
            raise RuntimeError("orderbook down")
        return copy.deepcopy(state.ob)

    auth = SimpleNamespace(name="u", user_id=ACT, broker="flattrade", is_revoked=False)
    with (
        patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
        patch("database.auth_db.get_auth_token", return_value="AUTH-MASKED"),
        patch("utils.session.has_login_this_trading_session", return_value=True),
        patch("broker.flattrade.api.order_api.get_trade_book", side_effect=get_trade_book),
        patch("broker.flattrade.api.order_api.get_order_book", side_effect=get_order_book),
        patch(
            "broker.flattrade.mapping.order_data.get_oa_symbol",
            side_effect=lambda sym, exch: {"TCS-EQ": "TCS"}.get(sym),
        ),
    ):
        return svc.sync_once("u", lambda: now)


def q(state, sql):
    with state.eng.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def states(state):
    return [r["attribution_state"] for r in q(state, "SELECT * FROM ledger_fills ORDER BY id")]


def completed_tcs(monkeypatch):
    from services import discretionary_pnl_service as pnl

    monkeypatch.setattr(pnl, "_now", lambda: LAST_FILL + timedelta(minutes=1))
    trades = pnl.get_completed_trades(symbol="TCS")
    return trades


ALL_GATES = (
    "DISCRETIONARY_SOURCE_FROM_ORDERBOOK",
    "DISCRETIONARY_ORDERSOURCE_MANUAL",
    "DISCRETIONARY_ORDERSOURCE_API_VERIFIED",
)


def test_tcs_acceptance_gate_on_all_manual_immediately(lenv, monkeypatch):
    flags_on(monkeypatch, *ALL_GATES)
    now = LAST_FILL + timedelta(seconds=40)  # well inside the 30 min grace
    out = run_sync(lenv, now)
    assert out["status"] == "ok" and out["inserted"] == 7
    assert lenv.ob_calls == 1 and lenv.tb_calls == 1  # one OrderBook read per sync
    assert states(lenv) == ["manual"] * 7
    audit = q(lenv, "SELECT * FROM ledger_attribution_audit ORDER BY id")
    assert len(audit) == 7
    assert {a["source"] for a in audit} == {"ordersource"}
    assert {a["evidence"] for a in audit} == {"ordersource WEB, orderbook ok"}
    ev0 = json.loads(
        q(lenv, "SELECT source_evidence FROM ledger_fills WHERE id=1")[0]["source_evidence"]
    )
    assert ev0 == {
        "ob": "ok",
        "os": "WEB",
        "rk": "foreign",
        "rs": "orderbook",
        "tc": None,
        "tm": None,
    }
    # partial fills of one order share its evidence
    ev3 = {
        r["source_evidence"]
        for r in q(lenv, "SELECT * FROM ledger_fills WHERE order_id='26111900000103'")
    }
    assert len(ev3) == 1
    trades = completed_tcs(monkeypatch)
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "SHORT"
    assert round(t["entry_price"], 4) == 2352.4222
    assert round(t["exit_price"], 4) == 2347.4333
    assert round(t["realized_pnl"], 4) == 224.5
    assert round(t["charges"], 4) == 37.5449
    assert round(t["net_pnl"], 4) == 186.9551
    # duplicate syncs and events: no new fill, no state churn, no extra audit row
    for _ in range(2):
        again = run_sync(lenv, now + timedelta(seconds=10))
        assert again["inserted"] == 0
    assert states(lenv) == ["manual"] * 7
    assert len(q(lenv, "SELECT * FROM ledger_attribution_audit")) == 7
    assert len(q(lenv, "SELECT * FROM ledger_fills")) == 7


def test_tcs_gate_off_keeps_previous_behaviour(lenv, monkeypatch):
    # the evidence is read and stored, but without the manual gate the grace still decides
    flags_on(monkeypatch, "DISCRETIONARY_SOURCE_FROM_ORDERBOOK")
    run_sync(lenv, LAST_FILL + timedelta(seconds=40))
    assert states(lenv)[:1] == ["manual"]  # 11:36 fill is older than 30 min
    assert states(lenv)[1:] == ["unresolved"] * 6
    run_sync(lenv, LAST_FILL + timedelta(minutes=31))
    assert states(lenv) == ["manual"] * 7
    assert {a["source"] for a in q(lenv, "SELECT * FROM ledger_attribution_audit")} == {"none"}


def test_tcs_all_flags_off_reads_no_orderbook_and_is_legacy(lenv, monkeypatch):
    run_sync(lenv, LAST_FILL + timedelta(seconds=40))
    assert lenv.ob_calls == 0
    assert states(lenv) == ["manual"] + ["unresolved"] * 6
    assert {r["source_evidence"] for r in q(lenv, "SELECT * FROM ledger_fills")} == {None}
    run_sync(lenv, LAST_FILL + timedelta(minutes=31))
    assert states(lenv) == ["manual"] * 7
    assert len(completed_tcs(monkeypatch)) == 1


def test_orderbook_failure_does_not_fail_the_sync_and_falls_back(lenv, monkeypatch):
    flags_on(monkeypatch, *ALL_GATES)
    lenv.ob_fail = True
    out = run_sync(lenv, LAST_FILL + timedelta(seconds=40))
    assert out["status"] == "ok" and out["inserted"] == 7
    assert states(lenv) == ["manual"] + ["unresolved"] * 6  # legacy grace
    assert {r["source_evidence"] for r in q(lenv, "SELECT * FROM ledger_fills")} == {None}
    lenv.ob_fail = False  # next sync reads it: identified at once
    run_sync(lenv, LAST_FILL + timedelta(seconds=50))
    assert states(lenv) == ["manual"] * 7


def test_api_order_tagged_is_signal_live_and_untagged_api_unclassified(lenv, monkeypatch):
    flags_on(monkeypatch, *ALL_GATES)
    tag = ost.build_order_tag("Momentum Long")
    for row in lenv.ob:
        row["ordersource"] = "API"
    lenv.ob[2]["remarks"] = tag  # the 3-fill order is tagged
    with lenv.eng.begin() as c:  # the lookup knows the name from an older registered tag
        c.execute(
            text(
                "INSERT INTO strategy_order_tags (orderid,user_id,strategy,symbol,exchange,product,"
                "mode,broker,applied_quantity,applied_notional,created_at) VALUES "
                "('OLD1','u','Momentum Long','TCS','NSE','MIS','live','flattrade',0,0,:c)"
            ),
            {"c": datetime(2026, 11, 1, 10, 0)},
        )
    run_sync(lenv, LAST_FILL + timedelta(seconds=40))
    got = q(lenv, "SELECT order_id, attribution_state, strategy FROM ledger_fills ORDER BY id")
    tagged = [r for r in got if r["order_id"] == "26111900000103"]
    assert [(r["attribution_state"], r["strategy"]) for r in tagged] == [
        ("strategy_tag", "Momentum Long")
    ] * 3  # partial fills inherit
    others = [r["attribution_state"] for r in got if r["order_id"] != "26111900000103"]
    assert others == ["unresolved"] * 4
    # never manual for untagged API orders, even long after the grace
    run_sync(lenv, LAST_FILL + timedelta(hours=3))
    assert [
        r["attribution_state"]
        for r in q(lenv, "SELECT * FROM ledger_fills ORDER BY id")
        if r["order_id"] != "26111900000103"
    ] == ["unresolved"] * 4
    audit = q(lenv, "SELECT * FROM ledger_attribution_audit WHERE source='remarks'")
    assert len(audit) == 3 and audit[0]["evidence"] == f"remarks tag {tag[4:]}"


def test_remarks_tag_upgrades_an_earlier_manual_fill(lenv, monkeypatch):
    run_sync(lenv, LAST_FILL + timedelta(minutes=31))  # legacy: all manual
    assert states(lenv) == ["manual"] * 7
    flags_on(monkeypatch, "DISCRETIONARY_SOURCE_FROM_ORDERBOOK")
    lenv.ob[0]["remarks"] = ost.build_order_tag("X")
    lenv.ob[0]["ordersource"] = "API"
    run_sync(lenv, LAST_FILL + timedelta(minutes=32))
    assert states(lenv) == ["strategy_tag"] + ["manual"] * 6


# ------------------------------------------------------------------ migration


OLD_FILLS_DDL = """CREATE TABLE ledger_fills (id INTEGER NOT NULL PRIMARY KEY, broker VARCHAR(40) NOT NULL,
account VARCHAR(64) NOT NULL, order_id VARCHAR(64) NOT NULL, fill_id VARCHAR(64), symbol VARCHAR(80)
NOT NULL, exchange VARCHAR(20) NOT NULL, product VARCHAR(20) NOT NULL, side VARCHAR(4) NOT NULL,
qty FLOAT NOT NULL, price FLOAT NOT NULL, fill_time DATETIME NOT NULL, occurrence_idx INTEGER NOT
NULL, dedup_key VARCHAR(400) NOT NULL, attribution_state VARCHAR(20) NOT NULL, strategy
VARCHAR(120), synced_at DATETIME NOT NULL, raw_hash VARCHAR(64), UNIQUE (dedup_key))"""
OLD_AUDIT_DDL = """CREATE TABLE ledger_attribution_audit (id INTEGER NOT NULL PRIMARY KEY, fill_ref
INTEGER NOT NULL, dedup_key VARCHAR(400) NOT NULL, from_state VARCHAR(20), to_state VARCHAR(20) NOT
NULL, reason VARCHAR(200) NOT NULL, source VARCHAR(20) NOT NULL, at DATETIME NOT NULL)"""


def _old_db(path):
    con = sqlite3.connect(path)
    con.execute(OLD_FILLS_DDL)
    con.execute(OLD_AUDIT_DDL)
    con.execute(
        "INSERT INTO ledger_fills VALUES (1,'flattrade','A','O1','F1','TCS','NSE','MIS','BUY',1,"
        "1,'2026-11-18 10:00:00',0,'k1','manual',NULL,'2026-11-18 10:00:00','h')"
    )
    con.execute(
        "INSERT INTO ledger_attribution_audit VALUES (1,1,'k1','unresolved','manual','r','none',"
        "'2026-11-18 10:00:00')"
    )
    con.commit()
    con.close()


def _columns(path, table):
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def test_migration_script_adds_columns_idempotently(tmp_path, monkeypatch):
    import importlib.util
    import pathlib

    upgrade = pathlib.Path(svc.__file__).resolve().parent.parent / "upgrade"
    monkeypatch.syspath_prepend(str(upgrade))  # the script imports its sibling _pragmas
    script = upgrade / "migrate_discretionary_ledger.py"
    spec = importlib.util.spec_from_file_location("migrate_disc_ledger_t", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db = tmp_path / "old.db"
    _old_db(db)
    with sqlite3.connect(db) as con:
        before = con.execute("SELECT * FROM ledger_fills").fetchall()
    for _ in range(2):
        assert mod.run(str(db)) == 0
    assert _columns(db, "ledger_fills")[-1] == "source_evidence"
    assert _columns(db, "ledger_attribution_audit")[-1] == "evidence"
    assert _columns(db, "ledger_fills").count("source_evidence") == 1
    with sqlite3.connect(db) as con:
        after = con.execute("SELECT * FROM ledger_fills").fetchall()
        audit = con.execute("SELECT evidence FROM ledger_attribution_audit").fetchall()
    assert after == [before[0] + (None,)] and audit == [(None,)]
    fresh = tmp_path / "fresh.db"
    for _ in range(2):
        assert mod.run(str(fresh)) == 0
    assert "source_evidence" in _columns(fresh, "ledger_fills")


def test_db_init_adds_columns_idempotently(tmp_path, monkeypatch):
    db = tmp_path / "old2.db"
    _old_db(db)
    eng = create_engine(f"sqlite:///{db}", poolclass=NullPool)
    assert ledger.add_missing_columns(eng) == [
        "ledger_fills.source_evidence",
        "ledger_attribution_audit.evidence",
    ]
    assert ledger.add_missing_columns(eng) == []
    with eng.connect() as c:
        assert c.execute(text("SELECT source_evidence FROM ledger_fills")).fetchall() == [(None,)]


# ------------------------------------------------------------------ human-channel allow-list


def test_manual_ordersources_default_and_setting(monkeypatch):
    assert ost.manual_ordersources() == {"WEB", "MOB"}
    monkeypatch.setenv("DISCRETIONARY_MANUAL_ORDERSOURCES", "   ")
    assert ost.manual_ordersources() == {"WEB", "MOB"}
    monkeypatch.setenv("DISCRETIONARY_MANUAL_ORDERSOURCES", " web, Mob ,tt,API,bad value,")
    assert ost.manual_ordersources() == {"WEB", "MOB", "TT"}  # API never; junk ignored
    monkeypatch.setenv("DISCRETIONARY_MANUAL_ORDERSOURCES", "WEB")
    assert ost.is_human_ordersource(" web ") and not ost.is_human_ordersource("MOB")
    for value in (None, "", "  ", 7, "API", "api"):
        assert not ost.is_human_ordersource(value)


GATES_ON = ("DISCRETIONARY_ORDERSOURCE_MANUAL", "DISCRETIONARY_ORDERSOURCE_API_VERIFIED")
LATER = DAY + timedelta(days=3)  # far past any grace


@pytest.mark.parametrize("source", ["WEB", "web", "Mob", " MOB "])
def test_allow_listed_human_channel_is_manual(monkeypatch, source):
    flags_on(monkeypatch, *GATES_ON)
    assert cls(ev(os_=source))[:1] == ("manual",)


@pytest.mark.parametrize("gates", [GATES_ON, ()])
@pytest.mark.parametrize("source", ["TT", "SYSTEM", "RMS", "ADMIN", "X1", "?"])
def test_other_order_sources_are_never_manual(monkeypatch, source, gates):
    flags_on(monkeypatch, *gates)
    for now in (JUST_AFTER, LATER):
        state, strategy, reason, _src, label = cls(ev(os_=source), now=now)
        assert state == "unresolved" and strategy is None
        assert "not an allow-listed human channel" in reason and "not allow-listed" in label


def test_allow_list_setting_extends_the_channels(monkeypatch):
    flags_on(monkeypatch, *GATES_ON)
    assert cls(ev(os_="TT"))[0] == "unresolved"
    monkeypatch.setenv("DISCRETIONARY_MANUAL_ORDERSOURCES", "WEB,MOB,TT")
    assert cls(ev(os_="TT"))[0] == "manual"


def test_allow_listed_channel_with_gate_off_keeps_the_legacy_grace(monkeypatch):
    assert cls(ev(os_="WEB"))[2] == "within manual grace period"
    assert cls(ev(os_="WEB"), now=LATER)[0] == "manual"


# ------------------------------------------------------------------ system-generated orders


def _price_variants(note):
    """The observed note's structure with other numbers: decided by class, not by its text."""
    import re

    out = [note]
    for price, clock in (("101.05", "09:15:00"), ("48210.5", "15:29:59"), ("7", "10:00:01")):
        variant = re.sub(r"\d+\.\d+", price, note, count=1)
        out.append(re.sub(r"\d{2}:\d{2}:\d{2}", clock, variant, count=1))
    return out


@pytest.mark.parametrize("note", _price_variants(BROKER_NOTE))
def test_observed_broker_note_class_is_neither_tag_nor_system(monkeypatch, note):
    assert ost.parse_remarks(note)["kind"] == "foreign"
    assert ost.system_marker({"remarks": note, "ordersource": "WEB"}) is None
    flags_on(monkeypatch, *GATES_ON)
    evidence = svc.build_evidence({"remarks": note, "ordersource": "WEB"}, None, True)
    assert "sm" not in evidence
    assert cls(evidence)[0] == "manual"


@pytest.mark.parametrize(
    "remarks,marker",
    [
        ("Square Off by RMS", "square_off"),
        ("SQUAREOFF", "square_off"),
        ("square-off order", "square_off"),
        ("Sq Off", "square_off"),
        ("SqOff 15:20", "square_off"),
        ("Auto Exit 15:15", "auto"),
        ("auto_close intraday", "auto"),
        ("RMS cut position", "rms_action"),
        ("closed by RMS", "rms_action"),
        ("MTM loss", "mtm"),
        ("Liquidation", "liquidation"),
        ("forced close", "liquidation"),
        ("expiry settlement", "expiry"),
        ("physical delivery close", "expiry"),
        ("GTT triggered", "gtt"),
        ("oco leg", "gtt"),
        ("Bracket leg", "bracket_cover"),
        ("cover order SL", "bracket_cover"),
    ],
)
def test_system_remark_pattern_classes(remarks, marker):
    assert ost.system_marker({"remarks": remarks}) == marker
    assert ost.system_marker({"remarks": remarks.lower()}) == marker


@pytest.mark.parametrize(
    "row",
    [
        {"remarks": "RMS"},  # a bare RMS mention is not an action
        {"remarks": "modified by RMS web"},
        {"remarks": "automatic"},
        {"remarks": "my cover note"},
        {"remarks": "squared away"},
        {"remarks": "OA1L9F3A2C1B"},  # our own tag is never a system marker
        {"remarks": ""},
        {"src_uid": "ft00xx01", "uid": "FT00XX01", "actid": "FT00XX01"},
        {"src_uid": "", "uid": "FT00XX01"},
        {"src_uid": "OTHER"},  # no own id to compare with: not decided
        {},
        None,
        "not a row",
    ],
)
def test_system_marker_negatives(row):
    assert ost.system_marker(row) is None


def test_system_marker_fields():
    assert ost.system_marker({"snonum": "123"}) == "bracket_cover_child"
    assert ost.system_marker({"algo_name": "TWAP"}) == "algo"
    assert ost.system_marker({"src_uid": "RMSADMIN", "uid": ACT, "actid": ACT}) == "other_user"

    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("bad row")

    assert ost.system_marker(Boom()) == "unreadable"  # unreadable -> never manual


@pytest.mark.parametrize("gates", [GATES_ON, ()])
def test_system_marked_order_is_never_manual(monkeypatch, gates):
    flags_on(monkeypatch, *gates)
    row = {"remarks": "Square Off by RMS", "ordersource": "WEB"}
    evidence = svc.build_evidence(row, None, True)
    assert evidence["sm"] == "square_off" and "Square" not in json.dumps(evidence)
    for now in (JUST_AFTER, LATER):
        state, _s, reason, _src, label = cls(evidence, now=now)
        assert state == "unresolved" and "system-generated" in reason
        assert label == "system marker square_off"
    assert "system marker square_off" in svc.evidence_label(evidence)
    # a registered tag or an engine order id still decides first
    assert cls(evidence, engine={"O1"})[0] == "strategy_engine"


def test_rms_square_off_of_a_manual_position_stays_unclassified(lenv, monkeypatch):
    """A broker-generated SELL closing a manual BUY: the BUY is MANUAL, the SELL UNCLASSIFIED.

    Accounting consequence (by design, the safe direction): the manual position keeps an
    open/EXIT PENDING leg until the SELL is resolved; it is never booked as a manual exit.
    """
    flags_on(monkeypatch, *ALL_GATES)
    rms = next(r for r in lenv.ob if r["norenordno"] == "26111900000104")
    rms.update(remarks="SQUARE OFF BY RMS", src_uid="RMSADMIN")
    run_sync(lenv, LAST_FILL + timedelta(days=1))
    rows = q(lenv, "SELECT order_id, attribution_state, source_evidence FROM ledger_fills")
    by_order = {r["order_id"]: r for r in rows}
    assert by_order["26111900000104"]["attribution_state"] == "unresolved"
    assert json.loads(by_order["26111900000104"]["source_evidence"])["sm"] == "square_off"
    assert {r["attribution_state"] for o, r in by_order.items() if o != "26111900000104"} == {
        "manual"
    }


# ------------------------------------------------------------------ tag conflicts / collisions


@pytest.mark.parametrize("source", ["WEB", "web", "MOB"])
def test_tag_on_a_human_channel_order_is_an_evidence_conflict(source):
    state, strategy, reason, _src, label = cls(
        ev(os_=source, rk="tag", tc=CODE, tm="L"), code_names={CODE: "Momentum Long"}
    )
    assert (state, strategy) == ("unresolved", None)
    assert "evidence conflict" in reason and "vs ordersource" in label


def test_tag_with_api_or_unread_source_is_signal_live():
    for evidence in (
        ev(os_="API", rk="tag", tc=CODE, tm="L"),
        ev(ob="error", os_=None, rk="tag", tc=CODE, tm="L"),
        ev(ob="missing", os_=None, rk="tag", tc=CODE, tm="L"),
        ev(os_="TT", rk="tag", tc=CODE, tm="L"),  # not a listed human channel
    ):
        assert cls(evidence)[:2] == ("strategy_tag", f"tag:{CODE}")


def test_ambiguous_strategy_code_is_never_a_guessed_name():
    names = {CODE: ("Momentum Long", "Other Name")}
    state, strategy, reason, source, label = cls(
        ev(os_="API", rk="tag", tc=CODE, tm="L"), code_names=names
    )
    assert (state, strategy, source) == ("strategy_tag", f"tag:{CODE}", "remarks")
    assert "ambiguous" in reason and label == f"remarks tag {CODE}, ambiguous"


def test_strategy_code_names_marks_collisions(monkeypatch):
    rows = [("Alpha",), ("Beta",), (" Alpha ",), ("Gamma",), (None,), ("",)]

    class Query:
        def filter(self, *a):
            return self

        def distinct(self):
            return self

        def all(self):
            return rows

    monkeypatch.setattr(sb.db_session, "query", lambda *a: Query())
    monkeypatch.setattr(
        ost, "strategy_code", lambda n: "DEADBEEF" if n.strip() in ("Alpha", "Beta") else "0000AAAA"
    )
    assert svc._strategy_code_names() == {
        "DEADBEEF": ("Alpha", "Beta"),  # collision: both kept, no winner
        "0000AAAA": "Gamma",
    }


@pytest.mark.parametrize(
    "value,kind",
    [
        ("oa1l9f3a2c1b", "malformed"),  # lower-cased by someone: not trusted
        ("Oa1L9F3A2C1B", "malformed"),
        ("XOA1L9F3A2C1B", "malformed"),  # prefix
        ("OA1L9F3A2C1B-1", "malformed"),  # suffix
        ("Rejected: OA1L9F3A2C1B by exchange", "malformed"),  # inside a broker note
        ("OA1L9F3A", "malformed"),  # truncated by the broker
        ("OA1", "malformed"),
        ("OA1L9F3A2C1B\u200b", "malformed"),  # zero-width char kept by strip
        ("\u0921\u0947\u091f\u093e \u2713", "foreign"),  # unicode note
        ("x" * 100_000, "foreign"),  # very long
        ("x" * 600 + "OA1L9F3A2C1B", "foreign"),  # beyond the scan window
        (b"OA1L9F3A2C1B", "empty"),  # not text
        ({"remarks": "OA1L9F3A2C1B"}, "empty"),
    ],
)
def test_parse_remarks_edge_cases(value, kind):
    assert ost.parse_remarks(value)["kind"] == kind


def test_parse_remarks_never_raises_on_a_hostile_string():
    class Hostile(str):
        def strip(self, *a):
            raise RuntimeError("boom")

    assert ost.parse_remarks(Hostile("OA1L9F3A2C1B"))["kind"] == "malformed"
    assert svc.build_evidence({"remarks": Hostile("x"), "ordersource": "WEB"}, None, True)


# ------------------------------------------------------------------ tagging never blocks


class Recorder:
    """A broker module stand-in: records each call's payload, returns a fixed response."""

    ACCEPTS_ORDER_SOURCE_TAG = True

    def __init__(self, raise_exc=None):
        self.calls = []
        self.response = (SimpleNamespace(status=200), {"stat": "Ok"}, "26111900000999")
        self.raise_exc = raise_exc

    def place_order_api(self, data, auth):
        self.calls.append(dict(data) if isinstance(data, dict) else data)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _hook():
    import restx_api  # noqa: F401
    from services.place_order_service import _place_with_source_tag

    return _place_with_source_tag


def _assert_untagged_once(module, data, result):
    assert result is module.response  # the broker's response, unchanged
    assert module.calls == [ORDER]  # called exactly once, with the unchanged payload
    assert data == ORDER


@pytest.mark.parametrize(
    "inject",
    ["sha256", "tag_enabled", "build_order_tag", "strategy_code"],
)
def test_tag_builder_failures_place_the_order_unchanged(monkeypatch, inject):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")

    def boom(*a, **k):
        raise RuntimeError("injected")

    if inject == "sha256":
        monkeypatch.setattr(ost.hashlib, "sha256", boom)
    else:
        monkeypatch.setattr(ost, inject, boom)
    module, data = Recorder(), copy.deepcopy(ORDER)
    _assert_untagged_once(module, data, _hook()(module, data, "A"))


@pytest.mark.parametrize("strategy", [None, 123, ["x"], object(), "", "  ", "Squareoff"])
def test_unusable_strategy_places_the_order_unchanged(monkeypatch, strategy):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    module = Recorder()
    data = dict(ORDER, strategy=strategy)
    result = _hook()(module, data, "A")
    assert result is module.response and len(module.calls) == 1
    assert ost.ORDER_TAG_KEY not in module.calls[0] and data == dict(ORDER, strategy=strategy)


@pytest.mark.parametrize("flag_value", ["", "0", "false", "maybe", "\u2713", "on "])
def test_flag_values_other_than_true_never_tag(monkeypatch, flag_value):
    monkeypatch.setenv("DISCRETIONARY_ORDER_TAG_ENABLED", flag_value)
    module, data = Recorder(), copy.deepcopy(ORDER)
    result = _hook()(module, data, "A")
    if flag_value.strip().lower() == "on":
        assert module.calls[0][ost.ORDER_TAG_KEY] == ost.build_order_tag("Momentum Long")
        assert result is module.response and data == ORDER
    else:
        _assert_untagged_once(module, data, result)


def test_broker_attribute_missing_odd_or_raising_never_tags(monkeypatch):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    for accepts in ("yes", 1, None):
        module, data = Recorder(), copy.deepcopy(ORDER)
        module.ACCEPTS_ORDER_SOURCE_TAG = accepts
        _assert_untagged_once(module, data, _hook()(module, data, "A"))

    class Raising(Recorder):
        @property
        def ACCEPTS_ORDER_SOURCE_TAG(self):  # noqa: N802
            raise RuntimeError("injected")

    module, data = Raising(), copy.deepcopy(ORDER)
    _assert_untagged_once(module, data, _hook()(module, data, "A"))


def test_order_data_mutation_failures_never_block(monkeypatch):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")

    class NoSet(dict):
        def __setitem__(self, k, v):
            raise RuntimeError("injected set")

    module, data = Recorder(), NoSet(ORDER)
    _assert_untagged_once(module, data, _hook()(module, data, "A"))

    class NoPop(dict):
        def pop(self, *a):
            raise RuntimeError("injected pop")

    module, data = Recorder(), NoPop(ORDER)
    result = _hook()(module, data, "A")  # cleanup failure must not replace the result
    assert result is module.response and len(module.calls) == 1
    assert module.calls[0][ost.ORDER_TAG_KEY] == ost.build_order_tag("Momentum Long")

    module = Recorder()
    assert _hook()(module, None, "A") is module.response and module.calls == [None]


def test_broker_exception_propagates_unchanged_and_key_removed(monkeypatch):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    exc = RuntimeError("network")
    module, data = Recorder(raise_exc=exc), copy.deepcopy(ORDER)
    with pytest.raises(RuntimeError) as caught:
        _hook()(module, data, "A")
    assert caught.value is exc and len(module.calls) == 1 and data == ORDER


def test_broker_internal_retry_sees_the_same_tag_and_key_is_removed(monkeypatch):
    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    sent = []

    class Retrying(Recorder):
        def place_order_api(self, data, auth):
            for _ in range(2):  # a broker module that retries its own send
                sent.append(data.get(ost.ORDER_TAG_KEY))
            return super().place_order_api(data, auth)

    module, data = Retrying(), copy.deepcopy(ORDER)
    assert _hook()(module, data, "A") is module.response
    assert sent == [ost.build_order_tag("Momentum Long")] * 2 and data == ORDER


def test_concurrent_calls_own_and_shared_dicts(monkeypatch):
    import threading

    flags_on(monkeypatch, "DISCRETIONARY_ORDER_TAG_ENABLED")
    hook, module, shared = _hook(), Recorder(), copy.deepcopy(ORDER)
    own = [copy.deepcopy(ORDER) for _ in range(16)]
    results, errors = [], []

    def run(d):
        try:
            results.append(hook(module, d, "A"))
        except Exception as e:  # pragma: no cover - the assertion below reports it
            errors.append(e)

    threads = [threading.Thread(target=run, args=(d,)) for d in own] + [
        threading.Thread(target=run, args=(shared,)) for _ in range(16)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(results) == 32 and len(module.calls) == 32
    assert all(r is module.response for r in results)
    assert all(d == ORDER for d in own) and shared == ORDER
    tag = ost.build_order_tag("Momentum Long")
    assert {c.get(ost.ORDER_TAG_KEY) for c in module.calls} <= {tag, None}


def test_smart_basket_split_paths_never_tag():
    import pathlib

    root = pathlib.Path(svc.__file__).resolve().parent.parent
    for rel in (
        "services/basket_order_service.py",
        "services/split_order_service.py",
        "broker/flattrade/api/order_api.py",
    ):
        source = (root / rel).read_text(encoding="utf-8")
        assert "_place_with_source_tag" not in source and "build_order_tag" not in source


# ------------------------------------------------------------------ stored states are sticky


def test_turning_live_only_on_later_never_downgrades_stored_rows(lenv, monkeypatch):
    import os as _os

    con = sqlite3.connect(_os.environ["SIGNAL_ENGINE_TRADES_DB"])
    con.execute("INSERT INTO trades (order_id, strategy) VALUES ('26111900000101', 's')")
    con.commit()
    con.close()
    run_sync(lenv, LAST_FILL + timedelta(minutes=40))  # live-only off: engine id matches
    before = states(lenv)
    assert before == ["strategy_engine"] + ["manual"] * 6
    audit_before = q(lenv, "SELECT COUNT(*) AS n FROM ledger_attribution_audit")[0]["n"]
    flags_on(monkeypatch, "DISCRETIONARY_ENGINE_LIVE_ONLY", *ALL_GATES)
    for row in lenv.ob:
        row["ordersource"] = "API"  # evidence that would now say "never manual"
    run_sync(lenv, LAST_FILL + timedelta(days=1))
    assert states(lenv) == before  # no downgrade of strategy_engine or manual
    assert q(lenv, "SELECT COUNT(*) AS n FROM ledger_attribution_audit")[0]["n"] == audit_before
