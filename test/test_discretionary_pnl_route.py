"""Discretionary performance API (Stage 3a): feature gate, auth, shapes, filters, quotes opt-in,
fills drill-down, single-flight isolation and read-only guarantees.

Temp SQLite for the ledger and the derived book; auth, quotes and the position book are
mocked, so there is no broker call and no HTTP. Every test builds its own Flask app.
Run with: uv run pytest test/test_discretionary_pnl_route.py -q
"""

import hashlib
import itertools
import os
import sys
from datetime import datetime

import pytest
from flask import Flask
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blueprints.auth as auth_bp_module  # noqa: E402
import blueprints.discretionary_pnl as route_module  # noqa: E402
from database import discretionary_book_db as book  # noqa: E402
from database import discretionary_ledger_db as ledger  # noqa: E402
from limiter import limiter  # noqa: E402
from services import discretionary_pnl_service as pnl  # noqa: E402
from utils.single_flight import SingleFlight  # noqa: E402

UI_FLAG = "DISCRETIONARY_PERFORMANCE_UI_ENABLED"
LEDGER_FLAG = "DISCRETIONARY_LEDGER_ENABLED"
NOW = datetime(2026, 11, 17, 11, 0, 0)  # Tuesday, market open

ROUTES = (
    "/api/discretionary-pnl/summary",
    "/api/discretionary-pnl/trades/1/fills",
    "/api/discretionary-pnl/unresolved",
)
ALL_ROUTES = ("/api/discretionary-pnl/status",) + ROUTES
DISABLED_BODY = {
    "status": "error",
    "code": "feature_disabled",
    "message": "Discretionary performance is turned off.",
}


class Env:
    def __init__(self, eng):
        self.eng = eng
        self.quote_calls = []
        self.positionbook_calls = 0
        self.quotes = {}
        self._n = itertools.count(1)

    def fill(self, side, qty, price, when, **kw):
        n = next(self._n)
        row = {
            "broker": kw.get("broker", "flattrade"),
            "account": kw.get("account", "A"),
            "order_id": f"O{n}",
            "fill_id": f"F{n}",
            "symbol": kw.get("symbol", "SBIN"),
            "exchange": kw.get("exchange", "NSE"),
            "product": kw.get("product", "MIS"),
            "side": side,
            "qty": float(qty),
            "price": float(price),
            "fill_time": when,
            "occurrence_idx": 0,
            "dedup_key": f"k{n}",
            "attribution_state": kw.get("state", "manual"),
            "strategy": None,
            "synced_at": when,
            "raw_hash": None,
        }
        assert ledger.insert_fills([row]) == 1
        return n

    def ledger_digest(self):
        with self.eng.connect() as c:
            rows = c.execute(text("SELECT * FROM ledger_fills ORDER BY id")).fetchall()
        return len(rows), hashlib.sha256(repr(rows).encode()).hexdigest()


def d(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm)


def seed(env):
    # SBIN (flattrade, A): one clean win, then one entry closed by two partial exits
    env.fill("BUY", 10, 100, d(10, 9, 20))
    env.fill("SELL", 10, 110, d(10, 9, 30))
    env.fill("BUY", 10, 100, d(11, 9, 20))
    env.fill("SELL", 4, 90, d(11, 9, 30))
    env.fill("SELL", 6, 95, d(11, 9, 40))
    # TCS CNC (flattrade, B): overnight win
    env.fill("BUY", 5, 3000, d(12, 14, 0), symbol="TCS", product="CNC", account="B")
    env.fill("SELL", 5, 3100, d(13, 10, 0), symbol="TCS", product="CNC", account="B")
    # INFY short (zerodha, Z)
    env.fill("SELL", 10, 200, d(16, 10, 0), symbol="INFY", broker="zerodha", account="Z")
    env.fill("BUY", 10, 190, d(16, 11, 0), symbol="INFY", broker="zerodha", account="Z")
    # MCX trade: no charge model, so its charges are unavailable
    env.fill("BUY", 1, 7000, d(16, 12, 0), symbol="CRUDE", exchange="MCX", account="M")
    env.fill("SELL", 1, 7100, d(16, 13, 0), symbol="CRUDE", exchange="MCX", account="M")
    # open ITC position
    env.fill("BUY", 10, 400, d(17, 10, 0), symbol="ITC")
    # unresolved and strategy fills never count
    env.fill("BUY", 3, 50, d(17, 10, 5), symbol="UNRES", state="unresolved")
    env.fill("BUY", 2, 60, d(17, 10, 6), symbol="STRAT", state="strategy_tag")


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'disc.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    originals = (ledger.engine, book.engine)
    for mod in (ledger, book):
        mod.db_session.remove()
        mod.db_session.configure(bind=eng)
        monkeypatch.setattr(mod, "engine", eng)
    ledger.init_discretionary_ledger_db()
    book.init_discretionary_book_db()
    e = Env(eng)

    def fake_quotes(auth_token, feed_token, broker, symbols):
        e.quote_calls.append((broker, [(s["symbol"], s["exchange"]) for s in symbols]))
        results = [
            {
                "symbol": s["symbol"],
                "exchange": s["exchange"],
                "data": {"ltp": e.quotes.get((s["symbol"], s["exchange"]), 410.0)},
            }
            for s in symbols
        ]
        return True, {"status": "success", "results": results}, 200

    def forbidden_positionbook(auth_token, broker):
        e.positionbook_calls += 1
        raise AssertionError("position book must never be called by the routes")

    def forbidden_reconcile(*a, **k):
        raise AssertionError("reconcile must never be called by the routes")

    def calendar_down(day, exchange):
        raise RuntimeError("calendar unavailable in tests")

    monkeypatch.setattr(pnl, "_now", lambda: NOW)
    monkeypatch.setattr(pnl, "_auth_provider", lambda broker, account=None: ("tok", "feed"))
    monkeypatch.setattr(pnl, "_account_auth_provider", lambda broker, account: ("tok", "feed"))
    monkeypatch.setattr(pnl, "_get_multiquotes", fake_quotes)
    monkeypatch.setattr(pnl, "_get_positionbook", forbidden_positionbook)
    monkeypatch.setattr(pnl, "reconcile", forbidden_reconcile)
    monkeypatch.setattr(pnl, "_tables_ready", False)
    monkeypatch.setattr(pnl, "_quote_cache", {})
    monkeypatch.setattr(pnl, "_calendar_window", calendar_down)
    monkeypatch.setattr(pnl, "_confirmed_closed", lambda day, ex: False)
    monkeypatch.setattr(pnl, "_calendar_cache", {})
    monkeypatch.delenv("DISCRETIONARY_RELIABLE_SINCE", raising=False)
    monkeypatch.delenv(UI_FLAG, raising=False)
    monkeypatch.delenv(LEDGER_FLAG, raising=False)
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(
        route_module, "_disc_flights", SingleFlight(join_window=5.0, wait_timeout=60.0)
    )
    yield e
    for mod in (ledger, book):
        mod.db_session.remove()
        mod.db_session.configure(bind=originals[0] if mod is ledger else originals[1])


def enable(monkeypatch):
    monkeypatch.setenv(UI_FLAG, "true")
    monkeypatch.setenv(LEDGER_FLAG, "true")


def make_app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(route_module.discretionary_pnl_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    return app


def log_in(c, user="rajandran"):
    with c.session_transaction() as s:
        s["user"] = user
        s["logged_in"] = True
        s["login_time"] = "2026-01-01T09:00:00+05:30"
        s["broker"] = "flattrade"


@pytest.fixture
def client(env, monkeypatch):
    import utils.session as session_utils

    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    app = make_app()
    with app.test_client() as c:
        log_in(c)
        yield c


def get(client, url, **params):
    return client.get(url, query_string=params)


def summary(client, **params):
    r = get(client, "/api/discretionary-pnl/summary", **{"period": "all", **params})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ---------------------------------------------------------------- gate and auth


@pytest.mark.parametrize("url", ROUTES)
def test_disabled_by_default_returns_404_body(client, url):
    r = get(client, url)
    assert r.status_code == 404
    assert r.get_json() == DISABLED_BODY


def test_status_default_is_disabled(client):
    r = get(client, "/api/discretionary-pnl/status")
    assert r.status_code == 200
    assert r.get_json() == {"status": "success", "enabled": False}


@pytest.mark.parametrize(
    ("ui", "led", "expected"),
    [
        (None, None, False),
        ("true", None, False),
        (None, "true", False),
        ("false", "true", False),
        ("true", "false", False),
        ("true", "true", True),
        ("1", "yes", True),
        ("ON", " True ", True),
        ("0", "1", False),
        ("enabled", "true", False),
        ("", "true", False),
    ],
)
def test_status_flag_combinations(client, monkeypatch, ui, led, expected):
    for name, value in ((UI_FLAG, ui), (LEDGER_FLAG, led)):
        if value is not None:
            monkeypatch.setenv(name, value)
    assert get(client, "/api/discretionary-pnl/status").get_json()["enabled"] is expected
    code = get(client, "/api/discretionary-pnl/summary").status_code
    assert (code == 200) is expected and (code == 404) is not expected


def test_flag_is_read_on_every_request(client, monkeypatch):
    assert get(client, "/api/discretionary-pnl/summary").status_code == 404
    enable(monkeypatch)
    assert get(client, "/api/discretionary-pnl/summary").status_code == 200
    monkeypatch.setenv(LEDGER_FLAG, "false")
    assert get(client, "/api/discretionary-pnl/summary").status_code == 404


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("url", ALL_ROUTES)
def test_unauthenticated_request_is_rejected(env, monkeypatch, enabled, url):
    if enabled:
        enable(monkeypatch)
    with make_app().test_client() as c:
        assert c.get(url).status_code in (302, 401)


# ---------------------------------------------------------------- summary shape


def test_enabled_summary_has_the_documented_shape(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    body = summary(client)
    for key in (
        "status",
        "period",
        "metrics",
        "build_id",
        "market_hours_source",
        "realized_gross",
        "charges",
        "net",
        "realized_net_of_charges",
        "net_definition",
        "charges_unavailable_trades",
        "unrealized_total",
        "open_positions",
        "per_identity",
        "sync_health",
        "warnings",
        "by_period",
        "by_symbol",
        "by_side",
        "by_product",
        "by_holding",
        "hold",
        "trades",
        "trades_total",
        "trades_truncated",
        "positions",
        "positions_total",
        "positions_truncated",
        "withheld",
        "labels",
        "quotes_requested",
    ):
        assert key in body, key
    assert body["status"] == "success"
    assert set(body["by_period"]) == {"daily", "weekly", "monthly"}
    assert set(body["by_holding"]) == {"intraday", "overnight"}
    assert set(body["hold"]) == {"overall", "winners", "losers"}
    assert body["withheld"]["sharpe_ratio"] == "no capital basis"
    assert "metrics.net_profit" in body["labels"]["realized_gross"]
    # Completed trades (a position flat to flat): SBIN +100, SBIN -70 (ONE position closed by
    # two partial exits of -40 and -30: two legs, one trade), TCS +500, INFY +100, CRUDE +100.
    # The old expectation 6 counted each closing leg as a trade.
    assert body["metrics"]["trades_count"] == 5
    assert body["realized_gross"] == pytest.approx(730.0)
    assert body["metrics"]["net_profit"] == pytest.approx(730.0)  # gross, despite its name
    assert body["realized_net_of_charges"] == pytest.approx(
        body["realized_gross"] - body["charges"]
    )
    assert body["charges_unavailable_trades"] == 1  # MCX
    assert body["trades_total"] == 5 and body["trades_truncated"] == 0
    assert body["positions_total"] == 1 and body["positions"][0]["symbol"] == "ITC"
    assert body["build_id"]
    trade = body["trades"][0]
    for key in (
        "id",
        "trade_date",
        "symbol",
        "direction",
        "realized_pnl",
        "charges",
        "overnight",
        "legs",
    ):
        assert key in trade
    closed = [t["closed_at"] for t in body["trades"]]
    assert closed == sorted(closed, reverse=True)  # newest first


def test_breakdowns_reconcile_with_the_headline(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    body = summary(client)
    m = body["metrics"]
    daily = {r["period"]: r for r in body["by_period"]["daily"]}
    for point in m["daily_pnl"]:
        if point["date"] in daily:
            assert round(daily[point["date"]]["realized_gross"], 2) == point["pnl"]
        else:
            assert point["pnl"] == 0.0
    for granularity in ("daily", "weekly", "monthly"):
        rows = body["by_period"][granularity]
        assert sum(r["realized_gross"] for r in rows) == pytest.approx(body["realized_gross"])
        # money is rounded to 4 places per row, so a sum can differ by a rounding unit
        assert sum(r["charges"] for r in rows) == pytest.approx(body["charges"], abs=1e-3)
        assert sum(r["trades"] for r in rows) == m["trades_count"]
        assert sum(r["net_of_charges"] for r in rows) == pytest.approx(body["net"], abs=1e-3)
    for name in ("by_symbol", "by_side", "by_product"):
        rows = body[name]
        assert sum(r["trades"] for r in rows) == m["trades_count"]
        assert sum(r["realized_gross"] for r in rows) == pytest.approx(body["realized_gross"])
    assert {r["key"] for r in body["by_side"]} == {"LONG", "SHORT"}
    assert body["by_holding"]["overnight"]["trades"] == 1
    assert body["by_holding"]["overnight"]["realized_gross"] == pytest.approx(500.0)
    # 4 intraday completed trades: SBIN +100, SBIN -70 (2 legs, 1 trade), INFY, CRUDE (was 5
    # when each SBIN partial exit counted as a trade)
    assert body["by_holding"]["intraday"]["trades"] == 4
    assert body["by_holding"]["intraday"]["realized_gross"] == pytest.approx(230.0)
    assert body["by_period"]["weekly"][0]["period"] == "2026-11-09"
    assert body["by_period"]["monthly"][0]["period"] == "2026-11"


def test_summary_agrees_with_get_summary(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    body = summary(client)
    ref = pnl.get_summary("all", None, None, {}, with_quotes=False)
    for key in ("realized_gross", "charges", "net", "metrics", "per_identity", "build_id"):
        assert body[key] == ref[key]


def test_trade_and_position_tables_are_limited_with_a_truncated_count(client, env, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(pnl, "TABLE_LIMIT", 3)
    seed(env)
    body = summary(client)
    # 5 completed trades (the two SBIN partial exits are one trade; was 6 legs -> 6 / 3)
    assert len(body["trades"]) == 3 and body["trades_total"] == 5 and body["trades_truncated"] == 2
    assert body["metrics"]["trades_count"] == 5  # the limit never changes a number
    assert body["realized_gross"] == pytest.approx(730.0)  # totals computed before the cut
    assert sum(r["trades"] for r in body["by_period"]["daily"]) == 5


# ---------------------------------------------------------------- filters


def test_filters_apply_to_every_number_table_and_chart(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    for params, symbols, pnl_total in (
        ({"broker": "zerodha"}, {"INFY"}, 100.0),
        ({"symbol": "SBIN"}, {"SBIN"}, 30.0),
        ({"account": "B"}, {"TCS"}, 500.0),
        ({"broker": "flattrade", "account": "A"}, {"SBIN"}, 30.0),
    ):
        body = summary(client, **params)
        assert body["realized_gross"] == pytest.approx(pnl_total), params
        assert body["metrics"]["net_profit"] == pytest.approx(pnl_total)
        assert {t["symbol"] for t in body["trades"]} == symbols
        assert {r["key"] for r in body["by_symbol"]} == symbols
        assert body["metrics"]["trades_count"] == body["trades_total"]
        for granularity in ("daily", "weekly", "monthly"):
            rows = body["by_period"][granularity]
            assert sum(r["realized_gross"] for r in rows) == pytest.approx(pnl_total)
        for p in body["positions"]:  # open positions obey the same filters
            for field in ("broker", "account", "symbol"):
                if field in params:
                    assert p[field] == params[field]
    only_itc = summary(client, symbol="ITC")
    assert only_itc["trades_total"] == 0 and only_itc["positions_total"] == 1
    assert only_itc["realized_gross"] == 0.0 and only_itc["by_symbol"] == []
    assert summary(client, broker="nobody")["metrics"]["trades_count"] == 0


def test_pooled_ratios_are_computed_on_the_filtered_set(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    body = summary(client, symbol="SBIN")
    # SBIN completed trades: +100 and -70 (the -40 and -30 partial exits close ONE position)
    # -> 1 win of 2 trades = 50 %, PF = 100 / 70 = 1.43. The old 33.33 % counted the two
    # legs as two losing trades; the profit factor is unchanged (same gross profit / loss).
    assert body["metrics"]["trades_count"] == 2
    assert body["metrics"]["win_rate"] == pytest.approx(50.0)
    assert body["metrics"]["profit_factor"] == pytest.approx(1.43)
    assert body["metrics"]["avg_loss"] == pytest.approx(70.0)
    (row,) = body["by_symbol"]
    assert row["win_rate"] == body["metrics"]["win_rate"]
    assert row["profit_factor"] == body["metrics"]["profit_factor"]


def test_date_filters(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    body = summary(client, period="custom", start="2026-11-12", end="2026-11-16")
    assert body["period"]["start"] == "2026-11-12" and body["period"]["end"] == "2026-11-16"
    assert body["metrics"]["trades_count"] == 3  # TCS (close on 13th), INFY, CRUDE
    assert body["realized_gross"] == pytest.approx(700.0)
    assert {t["trade_date"] for t in body["trades"]} == {"2026-11-13", "2026-11-16"}
    one = summary(client, period="1d")
    assert one["period"]["start"] == one["period"]["end"] == "2026-11-16"
    assert one["metrics"]["trades_count"] == 2


# ---------------------------------------------------------------- bad parameters


@pytest.mark.parametrize(
    "params",
    [
        {"period": "bogus"},
        {"period": "custom"},
        {"period": "custom", "start": "2026-11-16", "end": "2026-11-10"},
        {"period": "custom", "start": "not-a-date", "end": "2026-11-16"},
        {"start": "16/11/2026"},
        {"end": "2026-13-45"},
        {"quotes": "2"},
        {"quotes": "yes"},
        {"symbol": "X" * 200},
        {"broker": "B" * 200},
        {"account": "A" * 200},
    ],
)
def test_bad_parameters_return_400_with_a_message(client, env, monkeypatch, params):
    enable(monkeypatch)
    seed(env)
    r = get(client, "/api/discretionary-pnl/summary", **params)
    assert r.status_code == 400
    body = r.get_json()
    assert body["status"] == "error" and len(body["message"]) > 10


def test_trade_id_must_be_an_integer(client, monkeypatch):
    enable(monkeypatch)
    assert get(client, "/api/discretionary-pnl/trades/abc/fills").status_code == 404


# ---------------------------------------------------------------- quotes opt-in


def test_quotes_are_off_by_default_and_make_no_broker_call(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    body = summary(client)
    assert env.quote_calls == [] and env.positionbook_calls == 0
    assert body["positions"][0]["price_status"] == "not_requested"
    assert body["positions"][0]["unrealized"] is None
    assert body["quotes_requested"] is False
    summary(client, quotes="0")
    assert env.quote_calls == []


def test_quotes_1_uses_the_mocked_quote_function_only(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    env.quotes[("ITC", "NSE")] = 410.0
    body = summary(client, quotes="1")
    assert env.quote_calls == [("flattrade", [("ITC", "NSE")])]
    assert env.positionbook_calls == 0
    pos = body["positions"][0]
    assert pos["ltp"] == 410.0 and pos["unrealized"] == pytest.approx(100.0)
    assert pos["price_status"] in ("live", "stale", "last_close")
    assert body["quotes_requested"] is True
    assert body["unrealized_total"] == pytest.approx(100.0)
    assert body["realized_gross"] == pytest.approx(730.0)  # realized never depends on quotes


def test_quote_modes_do_not_share_a_cached_result(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    off = summary(client)
    on = summary(client, quotes="1")
    assert off["positions"][0]["price_status"] == "not_requested"
    assert on["positions"][0]["price_status"] != "not_requested"


# ---------------------------------------------------------------- fills drill-down


def test_fills_drill_down_returns_exactly_the_linked_fills(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    body = summary(client, symbol="SBIN")
    by_pnl = {t["realized_pnl"]: t for t in body["trades"]}
    # completed trades: the -40 and -30 partial exits are legs of ONE -70 trade (was three
    # rows +100 / -40 / -30, one per closing leg)
    assert set(by_pnl) == {100.0, -70.0}
    multi = by_pnl[-70.0]
    assert multi["legs"] == 2 and multi["closed_quantity"] == 10.0
    assert multi["exit_price"] == pytest.approx((4 * 90 + 6 * 95) / 10)  # 93.0
    bid = body["build_id"]
    r = get(client, f"/api/discretionary-pnl/trades/{multi['id']}/fills", build_id=bid)
    assert r.status_code == 200
    out = r.get_json()
    assert out["status"] == "success" and out["build_id"] == body["build_id"]
    assert out["trade"]["id"] == multi["id"] and out["trade"]["legs"] == 2
    # ALL legs' fills: the one entry fill (consumed 4 + 6 by the two legs, merged) and both exits
    assert [f["role"] for f in out["fills"]] == ["ENTRY", "EXIT", "EXIT"]
    entry, exit1, exit2 = out["fills"]
    assert (entry["qty_used"], entry["price"], entry["side"]) == (10.0, 100.0, "BUY")
    assert (exit1["qty_used"], exit1["price"], exit1["side"]) == (4.0, 90.0, "SELL")
    assert (exit2["qty_used"], exit2["price"], exit2["side"]) == (6.0, 95.0, "SELL")
    assert exit1["fill_ref"] != exit2["fill_ref"]
    for f in out["fills"]:
        for key in ("fill_time", "order_id", "fill_id", "broker", "account", "fill_ref"):
            assert f[key] not in (None, "")
        assert not any("token" in k or k == "raw_hash" for k in f)
    # exactly the stored link rows of both legs (2 entry parts + 2 exits) before the merge
    with env.eng.connect() as c:
        n = c.execute(
            text(
                "SELECT COUNT(*) FROM disc_trade_fills f JOIN disc_trades t ON t.id = f.trade_id "
                "WHERE t.symbol = 'SBIN' AND t.position_seq = :seq"
            ),
            {"seq": multi["position_seq"]},
        ).scalar()
    assert n == out["linked_fills"] == 4
    # a single-leg trade returns its entry and its exit
    one = get(
        client, f"/api/discretionary-pnl/trades/{by_pnl[100.0]['id']}/fills", build_id=bid
    ).get_json()
    assert [f["role"] for f in one["fills"]] == ["ENTRY", "EXIT"]


def test_fills_drill_down_of_an_open_position_is_404(client, env, monkeypatch):
    """An open position is not a completed trade: its position id answers 404."""
    enable(monkeypatch)
    seed(env)
    body = summary(client, symbol="ITC")
    assert body["trades"] == [] and body["positions_total"] == 1
    with env.eng.connect() as c:
        pid = c.execute(text("SELECT id FROM disc_positions WHERE symbol = 'ITC'")).scalar()
    r = get(client, f"/api/discretionary-pnl/trades/{pid}/fills", build_id=body["build_id"])
    assert r.status_code == 404 and r.get_json()["code"] == "trade_not_found"


def test_fills_drill_down_unknown_trade_is_404(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    bid = summary(client)["build_id"]
    r = get(client, "/api/discretionary-pnl/trades/999999/fills", build_id=bid)
    assert r.status_code == 404
    body = r.get_json()
    assert body["status"] == "error" and body["code"] == "trade_not_found"


def test_fills_require_a_build_id(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    tid = summary(client, symbol="TCS")["trades"][0]["id"]
    r = get(client, f"/api/discretionary-pnl/trades/{tid}/fills")
    assert r.status_code == 400
    assert r.get_json()["code"] == "build_id_required"


def fresh_flights(monkeypatch):
    monkeypatch.setattr(
        route_module, "_disc_flights", SingleFlight(join_window=5.0, wait_timeout=60.0)
    )


def test_fills_with_a_stale_build_id_are_rejected_not_reassigned(client, env, monkeypatch):
    """Trade ids are reassigned by a rebuild. A back-dated round trip makes the old id point
    at a different trade; the request still carrying the old build_id must be refused."""
    enable(monkeypatch)
    seed(env)
    first = summary(client, symbol="TCS")
    tid, old_build = first["trades"][0]["id"], first["build_id"]
    ok = get(client, f"/api/discretionary-pnl/trades/{tid}/fills", build_id=old_build)
    assert ok.status_code == 200 and ok.get_json()["trade"]["symbol"] == "TCS"
    # back-dated round trip: it sorts before everything, so every id after it can shift
    env.fill("BUY", 1, 10, d(2, 9, 20), symbol="EARLY")
    env.fill("SELL", 1, 12, d(2, 9, 30), symbol="EARLY")
    fresh_flights(monkeypatch)
    new = summary(client)
    assert new["build_id"] != old_build
    reused = [t for t in new["trades"] if t["id"] == tid]
    assert reused and reused[0]["symbol"] != "TCS"  # the old id now names another trade
    stale = get(client, f"/api/discretionary-pnl/trades/{tid}/fills", build_id=old_build)
    assert stale.status_code == 409
    body = stale.get_json()
    assert body["code"] == "build_changed" and body["current_build_id"] == new["build_id"]
    assert "trade" not in body and "fills" not in body
    # with the current build the right id for TCS returns TCS
    tcs = next(t for t in new["trades"] if t["symbol"] == "TCS")
    cur = get(client, f"/api/discretionary-pnl/trades/{tcs['id']}/fills", build_id=new["build_id"])
    assert cur.status_code == 200 and cur.get_json()["trade"]["symbol"] == "TCS"


def test_stale_build_with_unknown_trade_is_409_not_404(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    summary(client)
    r = get(client, "/api/discretionary-pnl/trades/1/fills", build_id="not-the-build")
    assert r.status_code == 409 and r.get_json()["code"] == "build_changed"


def test_huge_trade_id_is_404_not_500(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    bid = summary(client)["build_id"]
    for tid in (10**20, 2**63, 2**63 - 1):
        r = get(client, f"/api/discretionary-pnl/trades/{tid}/fills", build_id=bid)
        assert r.status_code == 404, tid
        assert r.get_json()["code"] == "trade_not_found"


def test_json_responses_are_not_cacheable(client, env, monkeypatch):
    seed(env)
    for url in ALL_ROUTES:  # disabled answers too
        assert get(client, url).headers["Cache-Control"] == "no-store", url
    enable(monkeypatch)
    bid = summary(client)["build_id"]
    for url, params in (
        ("/api/discretionary-pnl/status", {}),
        ("/api/discretionary-pnl/summary", {"period": "all"}),
        ("/api/discretionary-pnl/summary", {"period": "bogus"}),
        ("/api/discretionary-pnl/trades/1/fills", {"build_id": bid}),
        ("/api/discretionary-pnl/trades/1/fills", {}),
        ("/api/discretionary-pnl/unresolved", {}),
    ):
        assert get(client, url, **params).headers["Cache-Control"] == "no-store", (url, params)


def test_long_period_is_not_echoed_in_full(client, env, monkeypatch):
    enable(monkeypatch)
    r = get(client, "/api/discretionary-pnl/summary", period="x" * 70)
    assert r.status_code == 400
    msg = r.get_json()["message"]
    assert "x" * 30 not in msg and len(msg) < 120


# ---------------------------------------------------------------- unresolved


def test_unresolved_route(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    # a CNC sell with no opening buy: holdings from before the ledger, so an orphan
    env.fill("SELL", 99, 70, d(17, 9, 0), symbol="GHOST", product="CNC")
    body = get(client, "/api/discretionary-pnl/unresolved").get_json()
    assert body["status"] == "success"
    assert [u["symbol"] for u in body["unresolved"]] == ["UNRES"]
    assert body["unresolved_count"] == 1
    assert "raw_hash" not in body["unresolved"][0]
    assert [o["symbol"] for o in body["orphans"]] == ["GHOST"]
    assert body["orphans_count"] == 1
    other = get(client, "/api/discretionary-pnl/unresolved", symbol="SBIN").get_json()
    assert other["unresolved"] == []
    one = get(client, "/api/discretionary-pnl/unresolved", symbol="UNRES").get_json()
    assert one["unresolved_count"] == 1
    none = get(client, "/api/discretionary-pnl/unresolved", broker="zerodha").get_json()
    assert none["unresolved_count"] == 0


def test_summary_carries_execution_counts(client, env, monkeypatch):
    enable(monkeypatch)
    for mm in (20, 21, 22):
        env.fill("BUY", 1, 10, d(17, 9, mm), symbol="TCS", state="unresolved")
    body = summary(client, period="1d")
    assert (body["executions_collected"], body["executions_unresolved"]) == (3, 3)
    assert body["trades_total"] == 0 and body["positions_total"] == 0
    assert "executions_collected" in body["labels"]["executions"]
    seed(env)  # on the 17th: one manual ITC buy, one unresolved and one strategy fill
    day = {"period": "custom", "start": "2026-11-17", "end": "2026-11-17"}
    body = summary(client, **day)
    assert (body["executions_collected"], body["executions_unresolved"]) == (5, 4)
    body = summary(client, **day, symbol="SBIN")
    assert (body["executions_collected"], body["executions_unresolved"]) == (0, 0)


# ---------------------------------------------------------------- single flight


def test_single_flight_shares_identical_reads_but_never_mixes_users_or_filters(env, monkeypatch):
    import utils.session as session_utils

    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    enable(monkeypatch)
    seed(env)
    calls = []
    real = pnl.get_performance

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(pnl, "get_performance", spy)
    app = make_app()
    a, b = app.test_client(), app.test_client()
    log_in(a, "alice")
    log_in(b, "bob")
    url = "/api/discretionary-pnl/summary"
    first = a.get(url, query_string={"period": "all"}).get_json()
    again = a.get(url, query_string={"period": "all"}).get_json()
    assert first == again and len(calls) == 1  # shared within the ttl
    b.get(url, query_string={"period": "all"})
    assert len(calls) == 2  # another user: its own read
    zero = a.get(url, query_string={"period": "all", "broker": "zerodha"}).get_json()
    assert len(calls) == 3 and zero["realized_gross"] == pytest.approx(100.0)
    assert first["realized_gross"] == pytest.approx(730.0)
    a.get(url, query_string={"period": "all", "symbol": "SBIN"})
    a.get(url, query_string={"period": "all", "account": "B"})
    a.get(url, query_string={"period": "all", "quotes": "1"})
    a.get(url, query_string={"period": "30d"})
    assert len(calls) == 7  # every filter is part of the key
    a.get(url, query_string={"period": "all", "broker": "zerodha"})
    assert len(calls) == 7


# ---------------------------------------------------------------- read only


def test_no_route_writes_the_ledger_or_calls_a_broker(client, env, monkeypatch):
    enable(monkeypatch)
    seed(env)
    before = env.ledger_digest()
    assert before[0] == 14
    for url, params in (
        ("/api/discretionary-pnl/status", {}),
        ("/api/discretionary-pnl/summary", {"period": "all"}),
        ("/api/discretionary-pnl/summary", {"period": "30d", "symbol": "SBIN"}),
        ("/api/discretionary-pnl/trades/1/fills", {"build_id": "x"}),
        ("/api/discretionary-pnl/unresolved", {}),
    ):
        get(client, url, **params)
        assert env.ledger_digest() == before, url
    assert env.positionbook_calls == 0 and env.quote_calls == []
    summary(client, quotes="1")
    assert env.ledger_digest() == before
    assert len(env.quote_calls) == 1 and env.positionbook_calls == 0


def test_disabled_routes_touch_nothing(client, env):
    seed(env)
    before = env.ledger_digest()
    for url in ALL_ROUTES:
        get(client, url)
    assert env.ledger_digest() == before
    assert env.quote_calls == [] and env.positionbook_calls == 0
    with env.eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM disc_trades")).scalar() == 0  # no rebuild


def test_blueprint_declares_only_get_routes():
    for rule in make_app().url_map.iter_rules():
        if rule.rule.startswith("/api/discretionary-pnl"):
            assert rule.methods - {"HEAD", "OPTIONS"} == {"GET"}, rule.rule


def test_summary_carries_the_today_block_and_honours_filters(client, env, monkeypatch):
    enable(monkeypatch)
    env.fill("BUY", 45, 100, d(17, 9, 30))
    env.fill("SELL", 20, 110, d(17, 9, 45))
    env.fill("BUY", 3, 10, d(17, 9, 50), symbol="TCS", state="unresolved")
    body = summary(client, period="30d")
    t = body["today"]
    assert t["session_date"] == "2026-11-17" and t["quotes_requested"] is False
    assert (t["completed_positions"], t["open_positions"], t["partially_closed_positions"]) == (
        0,
        1,
        1,
    )
    assert t["realized_gross"] == 200 and t["unrealized"] is None
    pending = t["pending_attribution"]
    assert pending["grace_count"] + pending["other_total"] == 1
    assert "today" in body["labels"]
    other = summary(client, period="30d", symbol="ITC")["today"]
    assert other["open_positions"] == 0 and other["realized_gross"] == 0
    assert other["pending_attribution"]["grace_count"] == 0


def test_unresolved_route_carries_a_reason_code(client, env, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(
        pnl,
        "unresolved_reasons",
        lambda fills, now=None: {
            f["id"]: {"reason": "before_coverage", "coverage_from": "2026-11-19"} for f in fills
        },
    )
    seed(env)
    body = get(client, "/api/discretionary-pnl/unresolved").get_json()
    (row,) = body["unresolved"]
    assert (row["reason"], row["coverage_from"]) == ("before_coverage", "2026-11-19")
    assert row["attribution_state"] == "unresolved"
