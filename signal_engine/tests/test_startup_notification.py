"""The one consolidated startup/smoke-test Telegram message.

Before this, a successful startup silently sent nothing: notify_startup_result(True, ...)
and notify_engine_started() both fired before start_listener() ever calls
notifier.set_client(), so notify()'s module-level _client was still None and the message
was dropped (only a DEBUG/WARNING log recorded it). This file locks in the fix — one
message, built from data available before the listener connects, sent via a one-shot
client — and that OpenAlgo/broker checks and signal_engine's own checks land in separate,
clearly labelled sections.
"""

import dataclasses
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.config import TelegramChannel, settings
from signal_engine.notifier import build_startup_summary_message, notify_startup_summary
from signal_engine.smoke_test import CheckResult, SmokeTestReport


def _report(*results: CheckResult) -> SmokeTestReport:
    report = SmokeTestReport()
    for r in results:
        report.add(r)
    return report


def _all_passing_report() -> SmokeTestReport:
    return _report(
        CheckResult(name="1. Config", passed=True, message="OK — exchange=NSE product=MIS"),
        CheckResult(name="2. OpenAlgo reachable", passed=True, message="OK — HTTP 200"),
        CheckResult(name="3. Broker auth (funds API)", passed=True, message="OK — available capital: 80,313.62 INR"),
        CheckResult(name="4. Quote API (SBIN LTP)", passed=True, message="OK — SBIN LTP=1009.0"),
        CheckResult(name="5. Signal pipeline", passed=True, message="OK — parsed and validated"),
        CheckResult(name="6. Risk engine state", passed=True, message="OK — open=0/2 trades_today=0/10"),
        CheckResult(name="7. Database (risk store)", passed=True, message="OK — risk.db accessible"),
    )


def _with_settings(monkeypatch, **overrides):
    monkeypatch.setattr(
        "signal_engine.notifier.settings", dataclasses.replace(settings, **overrides)
    )


class TestBuildStartupSummaryMessage:
    def test_single_message_covers_every_check_exactly_once(self, monkeypatch):
        _with_settings(monkeypatch, telegram_channels=(TelegramChannel(name="intraday-orb", id=1),))
        report = _all_passing_report()

        msg = build_startup_summary_message(report, mode="LIVE", capital=80313.62, broker_name="flattrade")

        for check in report.checks:
            assert check.message in msg

    def test_openalgo_and_signal_engine_checks_land_in_separate_sections(self, monkeypatch):
        _with_settings(monkeypatch, telegram_channels=())
        report = _all_passing_report()

        msg = build_startup_summary_message(report, mode="LIVE", capital=1000.0, broker_name="flattrade")
        openalgo_section = msg.split("-- OpenAlgo --")[1].split("-- Signal Engine --")[0]
        engine_section = msg.split("-- Signal Engine --")[1]

        assert "OpenAlgo reachable" in openalgo_section
        assert "Broker auth" in openalgo_section
        assert "Quote API" in openalgo_section
        assert "Signal pipeline" not in openalgo_section
        assert "Risk engine state" not in openalgo_section

        assert "Signal pipeline" in engine_section
        assert "Risk engine state" in engine_section
        assert "Database" in engine_section

    def test_overall_status_reflects_all_passed(self, monkeypatch):
        _with_settings(monkeypatch, telegram_channels=())
        msg = build_startup_summary_message(_all_passing_report(), mode="LIVE", capital=1.0, broker_name="x")
        assert "READY" in msg
        assert "WARNING" not in msg

    def test_a_failed_check_is_visible_and_flips_overall_status(self, monkeypatch):
        _with_settings(monkeypatch, telegram_channels=())
        report = _report(
            CheckResult(name="2. OpenAlgo reachable", passed=True, message="OK — HTTP 200"),
            CheckResult(name="4. Quote API (SBIN LTP)", passed=False, message="timeout after 8s"),
        )

        msg = build_startup_summary_message(report, mode="LIVE", capital=0.0, broker_name="x")

        assert "FAIL" in msg
        assert "timeout after 8s" in msg
        assert "WARNINGS (1 check(s) failed)" in msg

    def test_config_and_channels_are_included_for_context(self, monkeypatch):
        _with_settings(
            monkeypatch,
            telegram_channels=(
                TelegramChannel(name="intraday-orb", id=1),
                TelegramChannel(name="intraday-breakout", id=2),
            ),
        )
        msg = build_startup_summary_message(_all_passing_report(), mode="ANALYZE", capital=100000.0, broker_name="flattrade")

        assert f"{settings.exchange}/{settings.product}/{settings.order_type}" in msg
        assert "intraday-orb, intraday-breakout" in msg
        assert "flattrade" in msg
        assert "ANALYZE" in msg
        assert "100,000" in msg


class TestNotifyStartupSummarySendsExactlyOnce:
    @pytest.mark.asyncio
    async def test_sends_one_message_via_a_oneshot_client(self, monkeypatch):
        """Regression guard for the silent-drop bug: this must NOT go through notify()/_client,
        which is still None at this point in startup — it needs its own client, like
        notify_startup_result already does for the failure path."""
        _with_settings(
            monkeypatch,
            telegram_channels=(TelegramChannel(name="intraday-orb", id=1),),
            notify_channel=TelegramChannel(name="signal-engine", id=99),
        )
        mock_client = AsyncMock()
        mock_client.is_user_authorized.return_value = True

        with patch("telethon.TelegramClient", return_value=mock_client):
            await notify_startup_summary(_all_passing_report(), "LIVE", 1000.0, "flattrade")

        assert mock_client.send_message.call_count == 1
        channel_id, message = mock_client.send_message.call_args[0]
        assert channel_id == 99
        assert "Signal Engine Startup" in message

    @pytest.mark.asyncio
    async def test_no_channel_configured_sends_nothing(self, monkeypatch):
        _with_settings(monkeypatch, telegram_channels=(), notify_channel=None)

        with patch("telethon.TelegramClient") as mock_cls:
            await notify_startup_summary(_all_passing_report(), "LIVE", 1000.0, "flattrade")

        mock_cls.assert_not_called()
