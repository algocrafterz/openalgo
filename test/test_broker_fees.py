"""Per-broker brokerage in the cost schedules.

Statutory charges (STT, exchange fee, SEBI, stamp duty, GST) are the same whichever broker
places the order; brokerage and DP charges are the broker's own. Flattrade's published
schedule (https://flattrade.in/charges/, read 2026-10-01) charges NIL brokerage on equity
intraday and delivery, NSE 0.00307% / BSE 0.00375% exchange fee, DP Rs 20 per debit + GST.

Run with: uv run pytest test/test_broker_fees.py -v
"""

import pytest

from portfolio.broker_fees import DEFAULT_FEES, fees_for
from portfolio.costs import india_delivery, india_intraday

BUY = SELL = 100_000.0  # a Rs 1 lakh round trip


class TestRegistry:
    def test_flattrade_is_zero_brokerage_and_verified(self):
        f = fees_for("flattrade")
        assert f.intraday_rate == 0 and f.intraday_cap == 0 and f.delivery_flat == 0
        assert not f.assumed
        assert "flattrade.in" in f.source

    def test_lookup_ignores_case_and_whitespace(self):
        assert fees_for(" Flattrade ").key == "flattrade"

    @pytest.mark.parametrize("name", [None, "", "unknown", "somebroker"])
    def test_unmodelled_or_missing_broker_uses_the_assumed_default(self, name):
        f = fees_for(name)
        assert f is DEFAULT_FEES and f.assumed
        assert f.intraday_rate == pytest.approx(0.0003) and f.intraday_cap == 20.0


class TestIntradayByBroker:
    def test_flattrade_pays_no_brokerage_and_no_gst_on_it(self):
        b = india_intraday("NSE", broker="flattrade").breakdown(BUY, SELL, orders=2)
        assert b["brokerage"] == 0.0
        # GST is only on exchange fee + SEBI now
        assert b["tax"] == pytest.approx(0.18 * (b["exchange_txn"] + b["sebi"]))

    def test_default_keeps_the_discount_broker_brokerage(self):
        b = india_intraday("NSE").breakdown(BUY, SELL, orders=2)
        assert b["brokerage"] == pytest.approx(40.0)  # 0.03% of 1 lakh is Rs 30, capped at Rs 20, x2 orders

    def test_flattrade_is_cheaper_than_the_default_by_brokerage_plus_its_gst(self):
        flat = india_intraday("NSE", broker="flattrade").charge(BUY, SELL, orders=2)
        base = india_intraday("NSE").charge(BUY, SELL, orders=2)
        assert base - flat == pytest.approx(40.0 * 1.18)

    def test_nse_exchange_fee_is_the_published_0_00307_percent(self):
        b = india_intraday("NSE", broker="flattrade").breakdown(BUY, SELL, orders=2)
        assert b["exchange_txn"] == pytest.approx((BUY + SELL) * 0.0000307)
        b_bse = india_intraday("BSE", broker="flattrade").breakdown(BUY, SELL, orders=2)
        assert b_bse["exchange_txn"] == pytest.approx((BUY + SELL) * 0.0000375)

    def test_statutory_lines_do_not_depend_on_the_broker(self):
        a = india_intraday("NSE", broker="flattrade").breakdown(BUY, SELL, orders=2)
        b = india_intraday("NSE").breakdown(BUY, SELL, orders=2)
        for key in ("stt", "exchange_txn", "sebi", "stamp_duty"):
            assert a[key] == pytest.approx(b[key])


class TestDeliveryByBroker:
    def test_delivery_brokerage_is_nil_for_flattrade_and_default(self):
        flat = india_delivery("NSE", broker="flattrade").breakdown(BUY, SELL, orders=2)
        assert flat["brokerage"] == 0
        assert india_delivery("NSE").breakdown(BUY, SELL, orders=2)["brokerage"] == 0

    def test_a_broker_with_delivery_brokerage_charges_it(self):
        from dataclasses import replace

        from portfolio import broker_fees

        paid = replace(broker_fees.DEFAULT_FEES, key="paid", delivery_flat=20.0, assumed=False)
        broker_fees.BROKER_FEES["paid"] = paid
        try:
            b = india_delivery("NSE", broker="paid").breakdown(BUY, SELL, orders=2)
            assert b["brokerage"] == pytest.approx(40.0)
        finally:
            del broker_fees.BROKER_FEES["paid"]


def test_existing_presets_are_unchanged_for_callers_that_pass_no_broker():
    """The portfolio backtester builds these without a broker; its numbers must not move,
    except the intraday NSE exchange fee corrected to the published 0.00307%."""
    b = india_intraday("NSE").breakdown(BUY, SELL, orders=2)
    assert b["stt"] == pytest.approx(SELL * 0.00025)
    assert b["stamp_duty"] == pytest.approx(BUY * 0.00003)
    assert b["exchange_txn"] == pytest.approx((BUY + SELL) * 0.0000307)


class TestNeedsAttention:
    """A broker whose fees are assumed, or were read long ago, must never go unnoticed."""

    def test_verified_recent_broker_needs_nothing(self):
        from datetime import date

        from portfolio.broker_fees import attention_for

        assert attention_for("flattrade", today=date(2026, 12, 1)) is None

    def test_unmodelled_broker_is_called_out_by_name(self):
        from portfolio.broker_fees import attention_for

        msg = attention_for("zerodha")
        assert msg is not None and "zerodha" in msg and "broker_fees.py" in msg

    def test_missing_broker_record_is_called_out_without_a_name(self):
        from portfolio.broker_fees import attention_for

        assert "not recorded" in attention_for("unknown")
        assert "not recorded" in attention_for(None)

    def test_old_verification_asks_for_a_recheck(self):
        from datetime import date

        from portfolio.broker_fees import attention_for

        msg = attention_for("flattrade", today=date(2027, 6, 1))
        assert msg is not None and "re-verify" in msg and "flattrade.in" in msg

    def test_coverage_lists_plugin_brokers_without_a_published_entry(self):
        from portfolio.broker_fees import uncovered_brokers

        missing = uncovered_brokers(["flattrade", "zerodha", "dhan"])
        assert missing == ["zerodha", "dhan"]

    def test_unmodelled_broker_logs_one_warning_not_one_per_trade(self, monkeypatch):
        from portfolio import broker_fees

        calls = []
        monkeypatch.setattr(broker_fees.logger, "warning", lambda m, *a, **k: calls.append(m))
        broker_fees._warned.clear()
        for _ in range(5):
            fees_for("newbroker")
        assert len(calls) == 1 and "newbroker" in calls[0]
        fees_for("unknown")
        fees_for("")
        assert len(calls) == 1  # "not recorded" is not a broker to add
