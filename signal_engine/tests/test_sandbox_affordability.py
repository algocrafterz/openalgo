"""Analyze mode must refuse an order the sandbox cannot fund, instead of letting it bounce.

2026-09-11, live: 14 of the day's 26 validated signals were rejected by the sandbox with
"Insufficient funds", 13 of them after the balance had drained to Rs 3,073 - while the engine
went on sizing every one off the full Rs 1,00,000 `sandbox_capital` override.

  Sizing [UPL]: capital=100,000 ... qty=198 value=113,246 total_risk=905(0.90%)
  Order rejected for UPL [analyze]: Insufficient funds. Required: Rs 22671.0,
                                    Available: Rs 3073.24, Shortage: Rs 19597.76

Three guards exist and none fired, because all three read the OVERRIDE rather than the real
balance: fetch_available_capital() short-circuits to sandbox_capital in analyze mode, so
min_capital_for_entry compared Rs 1,00,000 against its Rs 5,000 floor and passed; and the
margin check is skipped outright in analyze mode.

The cost is not the wasted round trip, it is the DATA: a signal refused for capacity is
recorded as a broker REJECTION, which reads in the ledger exactly like a broker problem
rather than "the sandbox was out of money". config.yaml's analyze profile removed the slot
caps specifically so declines would say something about the STRATEGY - this silently put a
capacity cap back, wearing a different hat.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import api_client, main
from signal_engine.tests.conftest import make_signal as _make_signal


class TestFetchFundsAvailable:
    @pytest.mark.asyncio
    async def test_reports_the_real_balance_ignoring_the_sandbox_override(self):
        """The whole point: this one never substitutes sandbox_capital."""
        with patch.object(api_client, "_post_json",
                          AsyncMock(return_value={"status": "success",
                                                  "data": {"availablecash": "3073.24"}})):
            assert await api_client.fetch_funds_available() == pytest.approx(3073.24)

    @pytest.mark.asyncio
    async def test_a_failure_reports_none_not_zero(self):
        """None means "unknown" and must not be read as "broke" - an unreachable funds API
        should not block every entry."""
        with patch.object(api_client, "_post_json", AsyncMock(side_effect=RuntimeError("down"))):
            assert await api_client.fetch_funds_available() is None

    @pytest.mark.asyncio
    async def test_a_non_success_body_reports_none(self):
        with patch.object(api_client, "_post_json",
                          AsyncMock(return_value={"status": "error"})):
            assert await api_client.fetch_funds_available() is None


class TestAnalyzeAffordabilityGate:
    def _signal(self):
        return _make_signal(entry=571.95, sl=567.38, tp=577.15, strategy="BREAKINGTRADE")

    @pytest.mark.asyncio
    async def test_an_unaffordable_order_is_declined_not_sent(self):
        """The real UPL case: 198 shares at Rs 571.95 needs ~Rs 22,671 of margin against a
        Rs 3,073 balance."""
        with patch.object(main, "fetch_trading_mode", AsyncMock(return_value=("analyze", True))), \
             patch.object(main, "fetch_funds_available", AsyncMock(return_value=3073.24)), \
             patch.object(main, "_decline", AsyncMock()) as decline, \
             patch.object(main.notifier, "notify_order_rejected", AsyncMock()), \
             patch.object(main.risk_engine, "calculate_quantity", return_value=198):
            result = await main._resolve_entry_quantity(self._signal(), 100_000.0, 100_000.0)
        assert result is None
        assert decline.await_args.kwargs["stage"] == "sandbox_margin"

    @pytest.mark.asyncio
    async def test_the_decline_reason_names_both_figures(self):
        with patch.object(main, "fetch_trading_mode", AsyncMock(return_value=("analyze", True))), \
             patch.object(main, "fetch_funds_available", AsyncMock(return_value=3073.24)), \
             patch.object(main, "_decline", AsyncMock()) as decline, \
             patch.object(main.notifier, "notify_order_rejected", AsyncMock()), \
             patch.object(main.risk_engine, "calculate_quantity", return_value=198):
            await main._resolve_entry_quantity(self._signal(), 100_000.0, 100_000.0)
        reason = decline.await_args.kwargs["reason"]
        assert "3,073" in reason and "sandbox" in reason.lower()

    @pytest.mark.asyncio
    async def test_an_affordable_order_proceeds(self):
        with patch.object(main, "fetch_trading_mode", AsyncMock(return_value=("analyze", True))), \
             patch.object(main, "fetch_funds_available", AsyncMock(return_value=90_000.0)), \
             patch.object(main.risk_engine, "calculate_quantity", return_value=198):
            result = await main._resolve_entry_quantity(self._signal(), 100_000.0, 100_000.0)
        assert result is not None
        assert result[0] == 198

    @pytest.mark.asyncio
    async def test_an_unreadable_balance_does_not_block_the_trade(self):
        """Unknown is not broke. Blocking on a transient funds-API failure would throw away
        signals for a reason that has nothing to do with the sandbox being empty."""
        with patch.object(main, "fetch_trading_mode", AsyncMock(return_value=("analyze", True))), \
             patch.object(main, "fetch_funds_available", AsyncMock(return_value=None)), \
             patch.object(main.risk_engine, "calculate_quantity", return_value=198):
            result = await main._resolve_entry_quantity(self._signal(), 100_000.0, 100_000.0)
        assert result is not None

    @pytest.mark.asyncio
    async def test_live_mode_is_untouched_by_this_gate(self):
        """Live has its own margin API path - this must not double-gate it."""
        with patch.object(main, "fetch_trading_mode", AsyncMock(return_value=("live", False))), \
             patch.object(main, "fetch_funds_available", AsyncMock(return_value=1.0)) as funds, \
             patch.object(main, "adjust_qty_for_margin", AsyncMock(return_value=198)), \
             patch.object(main.risk_engine, "calculate_quantity", return_value=198):
            result = await main._resolve_entry_quantity(self._signal(), 35_000.0, 35_000.0)
        assert result is not None
        funds.assert_not_awaited()
