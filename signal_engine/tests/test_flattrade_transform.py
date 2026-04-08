"""Tests for Flattrade transform_data — SL-M to SL-LMT MPP conversion.

Validates that Flattrade's transform_data correctly converts:
- MARKET -> LMT with MPP price
- SL-M -> SL-LMT with MPP price based on trigger_price
(Flattrade, like Shoonya, blocks MKT and SL-MKT order types via API.)
"""

import pytest
from unittest.mock import MagicMock, patch


def _make_order_data(**overrides):
    """Create a minimal order data dict with sensible defaults."""
    defaults = {
        "symbol": "WIPRO",
        "exchange": "NSE",
        "action": "SELL",
        "quantity": "89",
        "price": 0.0,
        "trigger_price": 200.9,
        "pricetype": "SL-M",
        "product": "MIS",
        "apikey": "TESTUSER",
    }
    defaults.update(overrides)
    return defaults


def _mock_quote(ltp="203.14", bid="203.10", ask="203.11", tick_size=0.01):
    """Return a mock quote response."""
    return {"ltp": ltp, "bid": bid, "ask": ask, "tick_size": tick_size}


@pytest.fixture
def mock_broker_data():
    """Mock BrokerData to avoid real API calls."""
    with patch("broker.flattrade.mapping.transform_data.BrokerData") as mock_cls:
        instance = MagicMock()
        instance.get_quotes.return_value = _mock_quote()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_get_br_symbol():
    """Mock symbol lookup to return a valid broker symbol."""
    with patch("broker.flattrade.mapping.transform_data.get_br_symbol") as mock:
        mock.return_value = "WIPRO-EQ"
        yield mock


class TestSlmToSlLmtConversion:
    """SL-M orders must be converted to SL-LMT with MPP price."""

    def test_sl_m_converts_to_sl_lmt(self, mock_broker_data, mock_get_br_symbol):
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="SL-M", action="SELL", trigger_price=200.9)
        result = transform_data(data, token="3787", auth_token="test_token")

        assert result["prctyp"] == "SL-LMT"

    def test_sl_lmt_has_nonzero_price(self, mock_broker_data, mock_get_br_symbol):
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="SL-M", action="SELL", trigger_price=200.9)
        result = transform_data(data, token="3787", auth_token="test_token")

        price = float(result["prc"])
        assert price > 0, "SL-LMT must have a non-zero limit price from MPP"

    def test_sl_lmt_price_below_trigger_for_sell(self, mock_broker_data, mock_get_br_symbol):
        """SELL SL: limit price must be below trigger (worse price for seller = fills on trigger)."""
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="SL-M", action="SELL", trigger_price=200.9)
        result = transform_data(data, token="3787", auth_token="test_token")

        price = float(result["prc"])
        trigger = float(result["trgprc"])
        assert price < trigger, f"SELL SL-LMT price {price} must be < trigger {trigger}"

    def test_sl_lmt_price_above_trigger_for_buy(self, mock_broker_data, mock_get_br_symbol):
        """BUY SL (short cover): limit price must be above trigger."""
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(
            pricetype="SL-M", action="BUY", trigger_price=200.9, price=0.0,
        )
        result = transform_data(data, token="3787", auth_token="test_token")

        price = float(result["prc"])
        trigger = float(result["trgprc"])
        assert price > trigger, f"BUY SL-LMT price {price} must be > trigger {trigger}"

    def test_trigger_price_unchanged(self, mock_broker_data, mock_get_br_symbol):
        """Trigger price must pass through untouched — MPP only sets limit price."""
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="SL-M", trigger_price=200.9)
        result = transform_data(data, token="3787", auth_token="test_token")

        assert float(result["trgprc"]) == 200.9

    def test_trantype_sell_for_long_sl(self, mock_broker_data, mock_get_br_symbol):
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="SL-M", action="SELL")
        result = transform_data(data, token="3787", auth_token="test_token")

        assert result["trantype"] == "S"

    def test_mpp_uses_trigger_price_not_ltp(self, mock_broker_data, mock_get_br_symbol):
        """MPP for SL-M should use trigger_price as base, not LTP from quotes."""
        from broker.flattrade.mapping.transform_data import transform_data

        # LTP=203.14 but trigger=100.0 — if MPP uses trigger, price should be near 100
        mock_broker_data.get_quotes.return_value = _mock_quote(ltp="203.14")
        data = _make_order_data(pricetype="SL-M", action="SELL", trigger_price=100.0)
        result = transform_data(data, token="3787", auth_token="test_token")

        price = float(result["prc"])
        # MPP subtracts ~2% from 100 for SELL = ~98. Should NOT be near 203.
        assert price < 105, f"Price {price} should be based on trigger 100, not LTP 203"
        assert price > 90, f"Price {price} unreasonably low"


class TestMarketToLimitConversion:
    """MARKET orders must still convert to LIMIT with MPP price (existing behavior)."""

    def test_market_converts_to_lmt(self, mock_broker_data, mock_get_br_symbol):
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="MARKET", action="BUY", trigger_price=0)
        result = transform_data(data, token="3787", auth_token="test_token")

        assert result["prctyp"] == "LMT"

    def test_market_lmt_has_nonzero_price(self, mock_broker_data, mock_get_br_symbol):
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="MARKET", action="BUY", trigger_price=0)
        result = transform_data(data, token="3787", auth_token="test_token")

        assert float(result["prc"]) > 0

    def test_market_buy_price_above_ltp(self, mock_broker_data, mock_get_br_symbol):
        """BUY MARKET→LIMIT: price must be above LTP (willing to pay more for fill)."""
        from broker.flattrade.mapping.transform_data import transform_data

        mock_broker_data.get_quotes.return_value = _mock_quote(ltp="200.0")
        data = _make_order_data(pricetype="MARKET", action="BUY", trigger_price=0)
        result = transform_data(data, token="3787", auth_token="test_token")

        assert float(result["prc"]) > 200.0


class TestLimitPassthrough:
    """LIMIT and SL orders should NOT go through MPP — pass through as-is."""

    def test_limit_order_unchanged(self, mock_broker_data, mock_get_br_symbol):
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="LIMIT", action="BUY", price=205.0, trigger_price=0)
        result = transform_data(data, token="3787", auth_token="test_token")

        assert result["prctyp"] == "LMT"
        assert result["prc"] == "205.0"

    def test_sl_limit_order_unchanged(self, mock_broker_data, mock_get_br_symbol):
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="SL", action="SELL", price=199.0, trigger_price=200.0)
        result = transform_data(data, token="3787", auth_token="test_token")

        assert result["prctyp"] == "SL-LMT"
        assert result["prc"] == "199.0"


class TestMppFallback:
    """When MPP fails (no auth token, quote error), SL-M should NOT silently pass through."""

    def test_sl_m_without_auth_token(self, mock_get_br_symbol):
        """Without auth_token, can't fetch quotes — SL-MKT passes through (broker will reject)."""
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="SL-M", action="SELL", trigger_price=200.9)
        result = transform_data(data, token="3787", auth_token=None)

        # Without auth_token, MPP can't fetch quotes — falls through as SL-MKT
        assert result["prctyp"] == "SL-MKT"

    def test_sl_m_with_zero_trigger_price(self, mock_broker_data, mock_get_br_symbol):
        """Zero trigger price means MPP has no base price — falls through as SL-MKT."""
        from broker.flattrade.mapping.transform_data import transform_data

        data = _make_order_data(pricetype="SL-M", action="SELL", trigger_price=0)
        result = transform_data(data, token="3787", auth_token="test_token")

        # trigger=0 means ltp=0 for SL-M path, MPP skips
        assert result["prctyp"] == "SL-MKT"
