"""override_sl: the runner Ladder's stop replaces the hand-written runner-stop rules."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine.main import _ladder_sl_override, _replace_runner_sl
from signal_engine.models import Direction, OrderStatus, TradeResult


def _pos(sl=2480.0, direction=Direction.LONG):
    pos = MagicMock()
    pos.symbol, pos.strategy, pos.exchange, pos.product = "RELIANCE", "BREAKOUT", "NSE", "MIS"
    pos.quantity = 35
    pos.entry_price = 2500.0
    pos.tp = 2525.0
    pos.sl = sl
    pos.direction = direction
    pos.sl_order_id = "SL001"
    pos.context = {"trigger": "VAH-RT", "pvah": "2490.0"}
    return pos


async def _run(pos, override, tp_level="TP1", extended=False, ok=True):
    status = OrderStatus.SUCCESS if ok else OrderStatus.REJECTED
    res = TradeResult(order_id="SL999", status=status, message="m")
    with (
        patch("signal_engine.main.place_sl_order", new_callable=AsyncMock, return_value=res) as place,
        patch("signal_engine.main.notifier.notify_sl_failed", new_callable=AsyncMock) as failed,
        patch("signal_engine.main.settings") as st,
    ):
        st.bracket_enabled = True
        st.tp1_runner_sl_buffer = 0.3
        st.use_extended_runner_tiers = extended
        await _replace_runner_sl(pos, pos.quantity, tp_level, override_sl=override)
    return place, failed


class TestOverrideSl:
    @pytest.mark.asyncio
    async def test_long_places_the_ladder_stop_exactly(self):
        pos = _pos(sl=2480.0)
        place, _ = await _run(pos, 2500.0)
        assert place.call_args.kwargs["sl_price"] == 2500.0
        assert pos.sl == 2500.0 and pos.sl_order_id == "SL999"

    @pytest.mark.asyncio
    async def test_structural_and_ratchet_rules_are_not_consulted(self):
        """The hand-written rule would give 2500 (structural) or 2517.5 (ratchet); override wins."""
        pos = _pos(sl=2480.0)
        place, _ = await _run(pos, 2503.0, extended=True)
        assert place.call_args.kwargs["sl_price"] == 2503.0

    @pytest.mark.asyncio
    async def test_long_never_loosens_an_existing_tighter_stop(self):
        pos = _pos(sl=2510.0)
        place, _ = await _run(pos, 2500.0)
        assert place.call_args.kwargs["sl_price"] == 2510.0

    @pytest.mark.asyncio
    async def test_short_places_the_ladder_stop_and_only_tightens(self):
        pos = _pos(sl=2520.0, direction=Direction.SHORT)
        place, _ = await _run(pos, 2500.0)
        assert place.call_args.kwargs["sl_price"] == 2500.0
        pos2 = _pos(sl=2490.0, direction=Direction.SHORT)
        place2, _ = await _run(pos2, 2500.0)
        assert place2.call_args.kwargs["sl_price"] == 2490.0

    @pytest.mark.asyncio
    async def test_tighten_only_applies_even_with_extended_tiers_off(self):
        pos = _pos(sl=2510.0)
        place, _ = await _run(pos, 2505.0, extended=False)
        assert place.call_args.kwargs["sl_price"] == 2510.0

    @pytest.mark.asyncio
    async def test_failed_placement_notifies_and_keeps_tracked_sl(self):
        pos = _pos(sl=2480.0)
        _, failed = await _run(pos, 2500.0, ok=False)
        failed.assert_awaited_once()
        assert pos.sl == 2480.0

    @pytest.mark.asyncio
    async def test_no_override_keeps_the_existing_rule(self):
        pos = _pos(sl=2500.0)
        place, _ = await _run(pos, None, extended=True)
        assert place.call_args.kwargs["sl_price"] == pytest.approx(2517.5)


class TestLadderSlOverride:
    def test_reads_a_number_from_context(self):
        sig = MagicMock()
        sig.context = {"ladder_sl": 2500.5}
        assert _ladder_sl_override(sig) == 2500.5

    def test_missing_key_is_none(self):
        sig = MagicMock()
        sig.context = {}
        assert _ladder_sl_override(sig) is None

    def test_non_dict_context_is_none(self):
        sig = MagicMock()  # context is a MagicMock, as in older tests
        assert _ladder_sl_override(sig) is None

    def test_non_numeric_or_non_positive_value_is_none(self):
        for bad in ("x", None, 0, -3.0, float("nan")):
            sig = MagicMock()
            sig.context = {"ladder_sl": bad}
            assert _ladder_sl_override(sig) is None
