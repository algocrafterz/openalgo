"""
Unit tests for the Strategy P&L page's backend service.

Covers both live/analyze branching, mode-aware leg filtering (a paper and a
live position of the same strategy/symbol are kept apart), and that a read
failure (unavailable strategy book, or a broker position-book error) is
surfaced as an explicit error rather than a silent zero P&L.

Pre-migration rows (mode='unknown') are deliberately excluded rather than
shown as a separate legacy group - they predate known accounting bugs in this
data (the sandbox P&L undercount and EOD partial-exit fixes) and are
untrusted, not merely unattributed.

Run with: uv run pytest test/test_strategy_performance_service.py -v
"""

import services.strategy_performance_service as perf_service
from database.strategy_book_db import StrategyBookUnavailable


def _leg(strategy="ORB", **overrides):
    leg = {
        "strategy": strategy,
        "symbol": "SBIN",
        "exchange": "NSE",
        "product": "MIS",
        "quantity": 10.0,
        "average_price": 500.0,
        "realized_pnl": 100.0,
        "today_realized_pnl": 100.0,
    }
    leg.update(overrides)
    return leg


def _legs_by_mode(current_legs=None, unknown_legs=None):
    """A `get_strategy_legs` fake that returns different lists depending on
    the `mode` kwarg. `unknown_legs` models pre-migration rows the service
    must never fetch or surface - a fake that ignores `mode` would hide a bug
    where untrusted legacy legs leak into the current mode's totals."""
    current_legs = current_legs or []
    unknown_legs = unknown_legs or []

    def _fake(mode=None, **_kwargs):
        if mode == "unknown":
            return unknown_legs
        return current_legs

    return _fake


def test_live_mode_uses_auth_token_and_broker(monkeypatch):
    monkeypatch.setattr(perf_service, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(perf_service, "get_strategy_legs", _legs_by_mode([_leg()]))

    captured = {}

    def fake_get_positionbook(**kwargs):
        captured.update(kwargs)
        return True, {"status": "success", "data": []}, 200

    monkeypatch.setattr(perf_service, "get_positionbook", fake_get_positionbook)

    success, response, status = perf_service.get_strategy_performance(
        "alice", "tok123", "flattrade"
    )

    assert success is True
    assert status == 200
    assert captured == {"auth_token": "tok123", "broker": "flattrade"}
    assert response["mode"] == "live"
    assert response["strategies"][0]["strategy"] == "ORB"


def test_analyze_mode_uses_api_key(monkeypatch):
    monkeypatch.setattr(perf_service, "get_analyze_mode", lambda: True)
    monkeypatch.setattr(perf_service, "get_strategy_legs", _legs_by_mode([_leg()]))
    monkeypatch.setattr(perf_service, "get_api_key_for_tradingview", lambda _user: "apikey-abc")

    captured = {}

    def fake_get_positionbook(**kwargs):
        captured.update(kwargs)
        return True, {"status": "success", "data": []}, 200

    monkeypatch.setattr(perf_service, "get_positionbook", fake_get_positionbook)

    success, response, status = perf_service.get_strategy_performance("alice", "tok123", "zerodha")

    assert success is True
    assert status == 200
    assert captured == {"api_key": "apikey-abc"}
    assert response["mode"] == "analyze"


def test_analyze_mode_without_api_key_returns_error(monkeypatch):
    monkeypatch.setattr(perf_service, "get_analyze_mode", lambda: True)
    monkeypatch.setattr(perf_service, "get_strategy_legs", _legs_by_mode([_leg()]))
    monkeypatch.setattr(perf_service, "get_api_key_for_tradingview", lambda _user: None)

    success, response, status = perf_service.get_strategy_performance("alice", "tok123", "zerodha")

    assert success is False
    assert status == 400
    assert "error" in response["status"]


def test_strategy_book_unavailable_returns_error_not_zero(monkeypatch):
    def _raise(mode=None, **_kwargs):
        raise StrategyBookUnavailable("book not initialized")

    monkeypatch.setattr(perf_service, "get_strategy_legs", _raise)

    success, response, status = perf_service.get_strategy_performance("alice", "tok123", "zerodha")

    assert success is False
    assert status == 503
    assert response["status"] == "error"
    assert "unavailable" in response["message"].lower()


def test_positionbook_error_returns_error_not_zero(monkeypatch):
    monkeypatch.setattr(perf_service, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(perf_service, "get_strategy_legs", _legs_by_mode([_leg()]))
    monkeypatch.setattr(
        perf_service,
        "get_positionbook",
        lambda **_kwargs: (False, {"status": "error", "message": "broker timeout"}, 500),
    )

    success, response, status = perf_service.get_strategy_performance("alice", "tok123", "zerodha")

    assert success is False
    assert status == 500
    assert response["status"] == "error"
    assert "broker timeout" in response["message"]


def test_empty_user_id_legs_are_included(monkeypatch):
    """signal_engine's /api/v1/placeorder body has no user_id; the strategy
    book's StrategyOrderTag.user_id is therefore empty for those legs. The
    service must not filter by user_id and drop them."""
    monkeypatch.setattr(perf_service, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(
        perf_service, "get_strategy_legs", _legs_by_mode([_leg("BREAKINGTRADE")])
    )
    monkeypatch.setattr(
        perf_service, "get_positionbook", lambda **_kwargs: (True, {"status": "success", "data": []}, 200)
    )

    success, response, _status = perf_service.get_strategy_performance("alice", "tok123", "zerodha")

    assert success is True
    assert response["strategies"][0]["strategy"] == "BREAKINGTRADE"


def test_current_mode_legs_are_filtered_to_the_active_mode(monkeypatch):
    """The service must pass the active mode ('live' here) to get_strategy_legs
    rather than reading every mode - this is the actual bug Phase 3 fixes."""
    monkeypatch.setattr(perf_service, "get_analyze_mode", lambda: False)
    captured_modes = []

    def fake_get_strategy_legs(mode=None, **_kwargs):
        captured_modes.append(mode)
        return [_leg()] if mode == "live" else []

    monkeypatch.setattr(perf_service, "get_strategy_legs", fake_get_strategy_legs)
    monkeypatch.setattr(
        perf_service, "get_positionbook", lambda **_kwargs: (True, {"status": "success", "data": []}, 200)
    )

    _success, response, _status = perf_service.get_strategy_performance("alice", "tok123", "zerodha")

    assert captured_modes == ["live"]
    assert len(response["strategies"]) == 1


def test_legacy_unknown_mode_legs_are_never_fetched_or_surfaced(monkeypatch):
    """Pre-migration (mode='unknown') legs are untrusted, not just unattributed
    - the service must not query for them at all, and the response must carry
    no legacy fields regardless of what the strategy book contains."""
    monkeypatch.setattr(perf_service, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(
        perf_service,
        "get_strategy_legs",
        _legs_by_mode(current_legs=[_leg("ORB")], unknown_legs=[_leg("LEGACY-STRAT")]),
    )
    monkeypatch.setattr(
        perf_service, "get_positionbook", lambda **_kwargs: (True, {"status": "success", "data": []}, 200)
    )

    _success, response, _status = perf_service.get_strategy_performance("alice", "tok123", "zerodha")

    assert [s["strategy"] for s in response["strategies"]] == ["ORB"]
    assert "legacy_strategies" not in response
    assert "legacy_count" not in response
