"""Opt-in feed-first answers from the quotes service.

A caller that sends ``max_age_seconds`` gets the live-feed price when the feed is healthy and
the symbol's last tick is that fresh. In every other case the request is served by the broker
exactly as before. The key is authenticated first, so the feed path never answers a caller
that the REST path would have refused.
"""

from __future__ import annotations

import pytest
from marshmallow import ValidationError

from restx_api.data_schemas import QuotesSchema
from services import quotes_service


@pytest.fixture
def calls(monkeypatch):
    seen = {"broker": 0, "feed": []}

    monkeypatch.setattr(
        quotes_service,
        "get_auth_token_broker",
        lambda api_key, include_feed_token=False: (
            ("tok", None, "flattrade") if api_key == "good" else (None, None, None)
        ),
    )

    def broker(auth_token, feed_token, broker_name, symbol, exchange):
        seen["broker"] += 1
        return True, {"status": "success", "data": {"ltp": 99.0, "open": 1.0}}, 200

    monkeypatch.setattr(quotes_service, "get_quotes_with_auth", broker)

    def feed(symbol, exchange, max_age):
        seen["feed"].append((symbol, exchange, max_age))
        return seen.get("feed_result")

    monkeypatch.setattr(quotes_service, "_feed_quote", feed)
    return seen


def test_fresh_feed_price_is_returned_without_a_broker_call(calls):
    calls["feed_result"] = {"ltp": 123.4, "bid": 0, "ask": 0, "source": "feed", "age": 0.42}
    ok, body, status = quotes_service.get_quotes("SBIN", "NSE", api_key="good", max_age_seconds=2)
    assert (ok, status) == (True, 200)
    assert body == {
        "status": "success",
        "data": {"ltp": 123.4, "source": "feed", "age_seconds": 0.42},
    }
    assert calls["broker"] == 0
    assert calls["feed"] == [("SBIN", "NSE", 2.0)]


def test_stale_or_unavailable_feed_falls_back_to_the_broker(calls):
    calls["feed_result"] = None
    ok, body, status = quotes_service.get_quotes("SBIN", "NSE", api_key="good", max_age_seconds=2)
    assert (ok, status) == (True, 200)
    assert body["data"]["ltp"] == 99.0 and "source" not in body["data"]
    assert calls["broker"] == 1


def test_without_the_option_the_feed_is_never_consulted(calls):
    calls["feed_result"] = {"ltp": 1.0, "bid": 0, "ask": 0, "source": "feed", "age": 0.1}
    quotes_service.get_quotes("SBIN", "NSE", api_key="good")
    assert calls["feed"] == []
    assert calls["broker"] == 1


def test_zero_means_the_option_is_off(calls):
    quotes_service.get_quotes("SBIN", "NSE", api_key="good", max_age_seconds=0)
    assert calls["feed"] == []


def test_an_invalid_key_is_refused_before_the_feed_is_consulted(calls):
    calls["feed_result"] = {"ltp": 1.0, "bid": 0, "ask": 0, "source": "feed", "age": 0.1}
    ok, body, status = quotes_service.get_quotes("SBIN", "NSE", api_key="bad", max_age_seconds=2)
    assert (ok, status) == (False, 403)
    assert calls["feed"] == []


def test_a_feed_error_is_a_fallback_not_a_failure(calls, monkeypatch):
    def boom(symbol, exchange, max_age):
        raise RuntimeError("cache gone")

    monkeypatch.setattr(quotes_service, "_feed_quote", boom)
    ok, body, status = quotes_service.get_quotes("SBIN", "NSE", api_key="good", max_age_seconds=2)
    assert (ok, status) == (True, 200)
    assert calls["broker"] == 1


def test_schema_accepts_the_option_within_bounds():
    base = {"apikey": "k", "symbol": "SBIN", "exchange": "NSE"}
    assert QuotesSchema().load({**base, "max_age_seconds": 2})["max_age_seconds"] == 2
    assert "max_age_seconds" not in QuotesSchema().load(base)


@pytest.mark.parametrize("bad", [-1, 10.5, "abc"])
def test_schema_rejects_an_out_of_range_option(bad):
    with pytest.raises(ValidationError):
        QuotesSchema().load(
            {"apikey": "k", "symbol": "SBIN", "exchange": "NSE", "max_age_seconds": bad}
        )
