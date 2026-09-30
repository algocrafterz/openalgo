"""dispatch_signal: the shared tail of the pipeline, callable without Telegram text."""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.main import dispatch_signal, handle_message
from signal_engine.models import Direction, ValidationResult, ValidationStatus
from signal_engine.tests.pipeline_fixtures import _mock_signal, _valid_message

VALID = ValidationResult(status=ValidationStatus.VALID)


class TestDispatchSignal:
    @pytest.mark.asyncio
    async def test_valid_entry_reaches_handle_entry(self):
        with (
            patch("signal_engine.main.validate", return_value=VALID),
            patch("signal_engine.main._handle_entry", new_callable=AsyncMock) as entry,
            patch("signal_engine.main._handle_exit", new_callable=AsyncMock) as exit_,
        ):
            sig = _mock_signal()
            await dispatch_signal(sig)
        entry.assert_awaited_once_with(sig)
        exit_.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_exit_reaches_handle_exit(self):
        sig = _mock_signal()
        sig.direction = Direction.EXIT
        with (
            patch("signal_engine.main.validate", return_value=VALID),
            patch("signal_engine.main._handle_entry", new_callable=AsyncMock) as entry,
            patch("signal_engine.main._handle_exit", new_callable=AsyncMock) as exit_,
        ):
            await dispatch_signal(sig)
        exit_.assert_awaited_once_with(sig)
        entry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_signal_is_declined_not_dispatched(self):
        bad = ValidationResult(status=ValidationStatus.INVALID, reason="bad")
        with (
            patch("signal_engine.main.validate", return_value=bad),
            patch("signal_engine.main._decline", new_callable=AsyncMock) as decline,
            patch("signal_engine.main._handle_entry", new_callable=AsyncMock) as entry,
        ):
            await dispatch_signal(_mock_signal())
        decline.assert_awaited_once()
        entry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_message_delegates_to_dispatch_signal(self):
        sig = _mock_signal()
        with (
            patch("signal_engine.main.parse", return_value=sig),
            patch("signal_engine.main.dispatch_signal", new_callable=AsyncMock) as disp,
        ):
            await handle_message(_valid_message())
        disp.assert_awaited_once_with(sig)

    @pytest.mark.asyncio
    async def test_handle_message_drops_before_dispatch_when_python_sourced(self):
        sig = _mock_signal()
        with (
            patch("signal_engine.main.parse", return_value=sig),
            patch("signal_engine.main.single_source.should_drop_alert", return_value=True),
            patch("signal_engine.main.single_source.record_drop", return_value=1),
            patch("signal_engine.main.dispatch_signal", new_callable=AsyncMock) as disp,
        ):
            await handle_message(_valid_message())
        disp.assert_not_awaited()
