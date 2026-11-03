"""
Integration tests for the session-authed /api/strategy-pnl route.

Run with: uv run pytest test/test_strategy_pnl_route.py -v
"""

import os
import sys

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blueprints.auth as auth_bp_module  # noqa: E402
import blueprints.strategy_pnl as strategy_pnl_module  # noqa: E402
from limiter import limiter  # noqa: E402


@pytest.fixture(autouse=True)
def _no_background_reconcile(monkeypatch):
    """The routes kick off a broker reconcile; keep these tests off the network."""
    monkeypatch.setattr(strategy_pnl_module, "schedule_reconcile", lambda mode, creds: False)


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(strategy_pnl_module.strategy_pnl_bp)
    # redirect(url_for("auth.logout")) needs this route to exist for url_for to resolve.
    app.register_blueprint(auth_bp_module.auth_bp)
    return app


def _log_in(client):
    import utils.session as session_utils

    with client.session_transaction() as session:
        session["user"] = "rajandran"
        session["logged_in"] = True
        session["login_time"] = "2026-01-01T09:00:00+05:30"
        session["broker"] = "flattrade"
    return session_utils


def test_unauthenticated_request_is_rejected():
    app = _app()
    with app.test_client() as client:
        response = client.get("/api/strategy-pnl")

    assert response.status_code in (302, 401)


def test_authenticated_request_returns_strategy_shape(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(strategy_pnl_module, "get_auth_token", lambda _user: "tok123")
    monkeypatch.setattr(
        strategy_pnl_module,
        "get_strategy_performance",
        lambda *_a, **_k: (
            True,
            {
                "status": "success",
                "mode": "live",
                "strategies": [{"strategy": "ORB", "realized": 100.0}],
                "count": 1,
                "book_mixed_mode": True,
            },
            200,
        ),
    )

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    assert body["strategies"][0]["strategy"] == "ORB"
    assert body["book_mixed_mode"] is True


def test_missing_broker_in_session_returns_400(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(strategy_pnl_module, "get_auth_token", lambda _user: "tok123")

    app = _app()
    with app.test_client() as client:
        import utils.session as session_utils

        with client.session_transaction() as session:
            session["user"] = "rajandran"
            session["logged_in"] = True
            session["login_time"] = "2026-01-01T09:00:00+05:30"
            # no broker set
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl")

    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


def test_no_auth_token_redirects_to_logout(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(strategy_pnl_module, "get_auth_token", lambda _user: None)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl")

    assert response.status_code == 302


def test_daily_route_passes_query_params_and_returns_metrics(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_get_daily_performance(strategy, period, start=None, end=None, **kw):
        captured["strategy"] = strategy
        captured["period"] = period
        captured["range"] = (start, end)
        return True, {"status": "success", "mode": "live", "trades_count": 0}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_get_daily_performance)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl/daily?strategy=ORB&period=90d")

    assert response.status_code == 200
    assert captured == {"strategy": "ORB", "period": "90d", "range": (None, None)}
    assert response.get_json()["status"] == "success"


def test_daily_route_defaults_period_to_30d_and_strategy_to_none(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_get_daily_performance(strategy, period, start=None, end=None, **kw):
        captured["strategy"] = strategy
        captured["period"] = period
        return True, {"status": "success"}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_get_daily_performance)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        client.get("/api/strategy-pnl/daily")

    assert captured == {"strategy": None, "period": "30d"}


def test_compare_route_returns_strategy_list(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(
        strategy_pnl_module,
        "get_strategy_comparison",
        lambda period, **kw: (
            True,
            {"status": "success", "mode": "live", "period": period, "strategies": []},
            200,
        ),
    )

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl/compare?period=ytd")

    assert response.status_code == 200
    body = response.get_json()
    assert body["period"] == "ytd"
    assert body["strategies"] == []


def test_daily_route_unauthenticated_is_rejected():
    app = _app()
    with app.test_client() as client:
        response = client.get("/api/strategy-pnl/daily")

    assert response.status_code in (302, 401)


def test_daily_and_compare_routes_pass_custom_range(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_daily(strategy, period, start=None, end=None, **kw):
        captured["daily"] = (period, start, end)
        return True, {"status": "success"}, 200

    def fake_compare(period, start=None, end=None, **kw):
        captured["compare"] = (period, start, end)
        return True, {"status": "success", "strategies": []}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_daily)
    monkeypatch.setattr(strategy_pnl_module, "get_strategy_comparison", fake_compare)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        q = "period=custom&start=2026-10-01&end=2026-10-15"
        client.get(f"/api/strategy-pnl/daily?{q}")
        client.get(f"/api/strategy-pnl/compare?{q}")

    assert captured["daily"] == ("custom", "2026-10-01", "2026-10-15")
    assert captured["compare"] == ("custom", "2026-10-01", "2026-10-15")


def test_repeated_params_reach_the_service(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_daily(strategy, period, start=None, end=None, *, include=None, exclude=None):
        captured["daily"] = (strategy, include, exclude)
        return True, {"status": "success"}, 200

    def fake_compare(period, start=None, end=None, *, include=None, exclude=None):
        captured["compare"] = (include, exclude)
        return True, {"status": "success", "strategies": []}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_daily)
    monkeypatch.setattr(strategy_pnl_module, "get_strategy_comparison", fake_compare)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        q = "strategy=A&strategy=%20B%20&strategy=A&strategy=&exclude=C&exclude=D"
        client.get(f"/api/strategy-pnl/daily?{q}")
        client.get(f"/api/strategy-pnl/compare?{q}")

    assert captured["daily"] == ("A", ["B"], ["C", "D"])
    assert captured["compare"] == (["A", "B"], ["C", "D"])


def test_exclude_only_leaves_strategy_none(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_daily(strategy, period, start=None, end=None, *, include=None, exclude=None):
        captured["args"] = (strategy, include, exclude)
        return True, {"status": "success"}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_daily)
    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        client.get("/api/strategy-pnl/daily?exclude=X")

    assert captured["args"] == (None, [], ["X"])


def test_filter_size_limits_return_a_plain_400(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return True, {"status": "success"}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake)
    monkeypatch.setattr(strategy_pnl_module, "get_strategy_comparison", fake)
    monkeypatch.setattr(strategy_pnl_module, "get_trades_for_export", fake)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        many = (
            "&".join(f"strategy=S{i}" for i in range(60))
            + "&"
            + "&".join(f"exclude=E{i}" for i in range(41))
        )
        long_name = "x" * 65
        for path in ("daily", "compare", "trades/export"):
            for query in (many, f"strategy={long_name}"):
                response = client.get(f"/api/strategy-pnl/{path}?{query}")
                assert response.status_code == 400
                body = response.get_json()
                assert body["status"] == "error" and body["message"]
    assert calls == []


def test_same_filter_in_another_order_shares_one_service_call(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    from utils.single_flight import SingleFlight

    monkeypatch.setattr(
        strategy_pnl_module, "_pnl_flights", SingleFlight(join_window=5, wait_timeout=10)
    )
    monkeypatch.setattr(strategy_pnl_module, "_with_data_health", lambda r, _c: r)
    calls = []

    def fake_daily(strategy, period, start=None, end=None, *, include=None, exclude=None):
        calls.append(strategy)
        return True, {"status": "success"}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_daily)
    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        client.get("/api/strategy-pnl/daily?strategy=A&strategy=B&exclude=X&exclude=Y")
        client.get("/api/strategy-pnl/daily?strategy=B&strategy=A&exclude=Y&exclude=X")

    assert len(calls) == 1


def test_export_route_passes_repeated_params(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_export(strategy, period, start=None, end=None, *, include=None, exclude=None):
        captured["args"] = (strategy, include, exclude)
        return True, {"status": "success", "trades": []}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_trades_for_export", fake_export)
    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        client.get("/api/strategy-pnl/trades/export?strategy=A&strategy=B&exclude=C&exclude=D")

    assert captured["args"] == ("A", ["B"], ["C", "D"])


def test_different_filters_do_not_share_one_service_call(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    from utils.single_flight import SingleFlight

    monkeypatch.setattr(
        strategy_pnl_module, "_pnl_flights", SingleFlight(join_window=5, wait_timeout=10)
    )
    monkeypatch.setattr(strategy_pnl_module, "_with_data_health", lambda r, _c: r)
    calls = []

    def fake_daily(strategy, period, start=None, end=None, *, include=None, exclude=None):
        calls.append((strategy, include, exclude))
        return True, {"status": "success"}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_daily)
    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        client.get("/api/strategy-pnl/daily?strategy=X")
        client.get("/api/strategy-pnl/daily?exclude=X")

    assert len(calls) == 2


# ---- capital query value --------------------------------------------------------


def _capital_client(monkeypatch, fake_daily):
    from utils.single_flight import SingleFlight

    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(
        strategy_pnl_module, "_pnl_flights", SingleFlight(join_window=5, wait_timeout=10)
    )
    monkeypatch.setattr(strategy_pnl_module, "_with_data_health", lambda r, _c: r)
    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_daily)
    app = _app()
    client = app.test_client()
    session_utils = _log_in(client)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    return client


def test_daily_route_passes_capital_to_the_service(monkeypatch):
    seen = []

    def fake_daily(
        strategy, period, start=None, end=None, *, include=None, exclude=None, capital=None
    ):
        seen.append(capital)
        return True, {"status": "success"}, 200

    client = _capital_client(monkeypatch, fake_daily)
    assert client.get("/api/strategy-pnl/daily?capital=1000000").status_code == 200
    assert seen == [1000000.0]


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_capital_is_treated_as_absent(monkeypatch, raw):
    calls = []

    def strict_daily(strategy, period, start=None, end=None, *, include=None, exclude=None):
        calls.append(1)
        return True, {"status": "success"}, 200

    client = _capital_client(monkeypatch, strict_daily)
    assert client.get(f"/api/strategy-pnl/daily?capital={raw}").status_code == 200
    assert calls == [1]


@pytest.mark.parametrize(
    "raw", ["0", "-5", "abc", "nan", "inf", "-inf", "1e400", "1000000000001", "1e13"]
)
def test_invalid_capital_is_refused_without_calling_the_service(monkeypatch, raw):
    calls = []

    def fake_daily(*a, **k):
        calls.append(1)
        return True, {"status": "success"}, 200

    client = _capital_client(monkeypatch, fake_daily)
    response = client.get(f"/api/strategy-pnl/daily?capital={raw}")
    assert response.status_code == 400
    body = response.get_json()
    assert body["status"] == "error"
    message = body["message"].lower()
    assert message
    for word in ("400", "exception", "valueerror", "traceback", "http", "nan", "float"):
        assert word not in message
    assert calls == []


def test_capital_at_the_ceiling_is_accepted(monkeypatch):
    seen = []

    def fake_daily(
        strategy, period, start=None, end=None, *, include=None, exclude=None, capital=None
    ):
        seen.append(capital)
        return True, {"status": "success"}, 200

    client = _capital_client(monkeypatch, fake_daily)
    assert client.get("/api/strategy-pnl/daily?capital=1000000000000").status_code == 200
    assert seen == [1_000_000_000_000.0]


def test_different_capitals_do_not_share_a_flight_but_the_same_capital_does(monkeypatch):
    seen = []

    def fake_daily(
        strategy, period, start=None, end=None, *, include=None, exclude=None, capital=None
    ):
        seen.append(capital)
        return True, {"status": "success"}, 200

    client = _capital_client(monkeypatch, fake_daily)
    client.get("/api/strategy-pnl/daily?capital=100")
    client.get("/api/strategy-pnl/daily?capital=200")
    client.get("/api/strategy-pnl/daily?capital=200")
    client.get("/api/strategy-pnl/daily")
    assert seen == [100.0, 200.0, None]


def test_compare_and_export_ignore_capital(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    seen = {}

    def fake_compare(period, start=None, end=None, *, include=None, exclude=None):
        seen["compare"] = True
        return True, {"status": "success"}, 200

    def fake_export(strategy, period, start=None, end=None, *, include=None, exclude=None):
        seen["export"] = True
        return True, {"status": "success", "start_date": "a", "end_date": "b", "trades": []}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_strategy_comparison", fake_compare)
    monkeypatch.setattr(strategy_pnl_module, "get_trades_for_export", fake_export)
    monkeypatch.setattr(strategy_pnl_module, "_with_data_health", lambda r, _c: r)
    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        assert client.get("/api/strategy-pnl/compare?capital=abc").status_code == 200
        assert client.get("/api/strategy-pnl/trades/export?capital=-1").status_code == 200
    assert seen == {"compare": True, "export": True}


def test_excursions_route_passes_filter_and_period(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_excursions(strategy, period, start=None, end=None, **kw):
        captured.update(strategy=strategy, period=period, include=kw["include"], exclude=kw["exclude"])
        return True, {"status": "success", "excursions": None}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_excursion_quality", fake_excursions)
    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
        response = client.get(
            "/api/strategy-pnl/daily/excursions?strategy=ORB&strategy=VWAP&exclude=EMA&period=90d"
        )

    assert response.status_code == 200
    assert captured == {"strategy": "ORB", "period": "90d", "include": ["VWAP"], "exclude": ["EMA"]}


def test_excursions_route_unauthenticated_is_rejected():
    app = _app()
    with app.test_client() as client:
        assert client.get("/api/strategy-pnl/daily/excursions").status_code in (302, 401)
