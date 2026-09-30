"""Execution sinks: the only place modes differ after the strategy."""

import asyncio

from signal_engine.models import Direction, Signal
from signal_engine.pipeline.sink import RecordingSink


def sig():
    return Signal(
        strategy="BREAKOUT",
        direction=Direction.LONG,
        symbol="TCS",
        entry=100.0,
        sl=99.0,
        tp=101.0,
        sig_id="TCS-20260930-0955",
    )


class TestRecordingSink:
    def test_records_and_never_dispatches(self):
        sink = RecordingSink()
        assert sink.dispatches is False
        asyncio.run(sink.submit(sig()))
        assert [s.sig_id for s in sink.signals] == ["TCS-20260930-0955"]


class TestDispatchSink:
    def test_submit_hands_the_signal_to_dispatch(self):
        import asyncio
        from unittest.mock import AsyncMock

        from signal_engine.models import Direction, Signal
        from signal_engine.pipeline.sink import DispatchSink

        dispatch = AsyncMock()
        sink = DispatchSink(dispatch)
        sig = Signal(
            strategy="BREAKOUT", direction=Direction.LONG, symbol="TCS", entry=1, sl=0.9, tp=1.2
        )
        asyncio.run(sink.submit(sig))
        dispatch.assert_awaited_once_with(sig)
        assert sink.dispatches is True

    def test_dispatch_errors_propagate_to_the_cycle(self):
        import asyncio
        from unittest.mock import AsyncMock

        import pytest

        from signal_engine.models import Direction, Signal
        from signal_engine.pipeline.sink import DispatchSink

        sink = DispatchSink(AsyncMock(side_effect=RuntimeError("boom")))
        sig = Signal(
            strategy="BREAKOUT", direction=Direction.LONG, symbol="TCS", entry=1, sl=0.9, tp=1.2
        )
        with pytest.raises(RuntimeError):
            asyncio.run(sink.submit(sig))

    def test_on_bar_is_a_no_op_for_real_broker_sinks(self):
        import asyncio

        import pandas as pd

        from signal_engine.pipeline.sink import DispatchSink, RecordingSink

        ts = pd.Timestamp("2026-09-30 10:00", tz="Asia/Kolkata")
        for sink in (RecordingSink(), DispatchSink(lambda s: None)):
            assert asyncio.run(sink.on_bar("TCS", ts, 1, 2, 0.5, 1.5)) is None
