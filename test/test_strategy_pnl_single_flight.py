"""Overlapping and back-to-back /api/strategy-pnl reads share one broker position book.

2026-11-02: each strategy-pnl read costs one positionbook call, and two browser tabs on the P&L
page each ran the same reads in the same second. The reads now share one fetch while it runs and
keep its result for a few seconds, so any number of tabs cost one broker call per window.
"""

import threading
import time
from datetime import datetime

import pytest
from flask import Flask

import blueprints.auth as auth_bp_module
import blueprints.strategy_pnl as strategy_pnl_module
from limiter import limiter
from signal_engine.timeutils import IST
from utils.single_flight import SingleFlight


def test_a_result_is_reused_within_the_ttl_and_refetched_after_it():
    flights = SingleFlight(join_window=5, wait_timeout=5)
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    assert flights.do(("k",), fetch, ttl=0.2) == {"n": 1}
    assert flights.do(("k",), fetch, ttl=0.2) == {"n": 1}
    time.sleep(0.25)
    assert flights.do(("k",), fetch, ttl=0.2) == {"n": 2}


def test_without_a_ttl_nothing_is_kept():
    flights = SingleFlight(join_window=5, wait_timeout=5)
    calls = []
    flights.do(("k",), lambda: calls.append(1))
    flights.do(("k",), lambda: calls.append(1))
    assert len(calls) == 2


def test_a_failed_fetch_is_not_kept_for_the_ttl():
    flights = SingleFlight(join_window=5, wait_timeout=5)
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("broker busy")
        return "ok"

    with pytest.raises(RuntimeError):
        flights.do(("k",), fetch, ttl=5)
    assert flights.do(("k",), fetch, ttl=5) == "ok"
    assert len(calls) == 2


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(strategy_pnl_module.strategy_pnl_bp)
    # check_session_validity redirects to auth.login, so that endpoint has to exist.
    app.register_blueprint(auth_bp_module.auth_bp)
    return app


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(strategy_pnl_module, "get_auth_token", lambda _user: "tok")
    monkeypatch.setattr(strategy_pnl_module, "schedule_reconcile", lambda *a, **k: False)
    monkeypatch.setattr(strategy_pnl_module, "_with_data_health", lambda r, _c: r)
    monkeypatch.setattr(
        strategy_pnl_module, "_pnl_flights", SingleFlight(join_window=5, wait_timeout=10)
    )
    calls = []
    release = threading.Event()

    def performance(*_a, **_k):
        calls.append(1)
        release.wait(5)
        return True, {"status": "success", "strategies": []}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_strategy_performance", performance)
    return calls, release


def _log_in(client):
    with client.session_transaction() as session:
        session["user"] = "rajandran"
        session["logged_in"] = True
        session["login_time"] = datetime.now(IST).isoformat()
        session["broker"] = "flattrade"


def test_tabs_polling_at_the_same_time_share_one_broker_read(routed):
    calls, release = routed
    app = _app()
    results = []

    def hit():
        with app.test_client() as client:
            _log_in(client)
            results.append(client.get("/api/strategy-pnl").status_code)

    threads = [threading.Thread(target=hit) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    release.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1
    assert results == [200, 200, 200]


def test_a_read_just_after_another_reuses_its_result(routed):
    calls, release = routed
    release.set()
    app = _app()
    with app.test_client() as client:
        _log_in(client)
        client.get("/api/strategy-pnl")
        client.get("/api/strategy-pnl")
    assert len(calls) == 1
