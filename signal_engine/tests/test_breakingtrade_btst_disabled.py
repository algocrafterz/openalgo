"""BTST is switched off (no delivery % at the vendor); the rest of the 14:45+ window is not."""

from __future__ import annotations

from datetime import datetime

from signal_engine.analysis.breakingtrade import __main__ as poller


def test_btst_switch_is_off():
    assert poller.BTST_ENABLED is False


def test_disabled_window_sends_only_the_intraday_summary(monkeypatch):
    from signal_engine.analysis.breakingtrade import alerts, btst, eod_summary

    calls = []
    monkeypatch.setattr(eod_summary, "alert_intraday_eod_summary", lambda *a: calls.append("sum"))
    monkeypatch.setattr(btst, "candidates", lambda *a, **k: calls.append("btst") or [])
    monkeypatch.setattr(alerts, "alert_btst", lambda *a, **k: calls.append("alert_btst"))
    poller._btst_window_disabled(datetime(2026, 11, 11, 14, 50))
    assert calls == ["sum"]


def test_summary_failure_does_not_raise(monkeypatch):
    from signal_engine.analysis.breakingtrade import eod_summary

    def boom(*_a):
        raise RuntimeError("db locked")

    monkeypatch.setattr(eod_summary, "alert_intraday_eod_summary", boom)
    poller._btst_window_disabled(datetime(2026, 11, 11, 14, 50))
