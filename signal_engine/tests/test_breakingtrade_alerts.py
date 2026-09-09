"""Alert persistence and formatting (alerts.py), and the poller's schedule helper.

The load-bearing property here is that an alert is RECORDED even when it cannot be delivered -
the record is the experiment, delivery is only a convenience.
"""

from datetime import date, datetime

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import alerts, store
from signal_engine.analysis.breakingtrade.__main__ import (
    AUTO_STOP_TIME,
    POLL_WINDOWS,
    _due_marks,
    _within_poll_hours,
)

# Captured at import time, before the autouse fixture below replaces alerts.send with a mock -
# TestSendMonospace needs the REAL implementation to test its actual HTTP payload construction.
_real_send = alerts.send


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    # Never attempt a real network call from a test.
    monkeypatch.setattr(alerts, "send", lambda text, kind=None, monospace=False: (False, None))


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"result": {"message_id": 1}}


class TestSendMonospace:
    """send()'s own HTTP payload - the actual bug this fixed: every column-padded message
    (f"{x:<10}") was sent as plain text with no parse_mode, and Telegram renders that in a
    proportional font where the padding does nothing - so every 'aligned' table was ragged on
    the phone. monospace=True must wrap in a code block and set parse_mode; the default must
    not, so a short conversational alert doesn't get an unnecessary grey box."""

    def test_monospace_wraps_in_a_markdown_code_block(self, monkeypatch):
        monkeypatch.setattr(
            alerts, "_env",
            lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc", "BREAKINGTRADE_CHAT_ID_INTRADAY": "-100X"},
        )
        captured = {}

        def fake_post(url, json, timeout):
            captured.update(json)
            return _FakeResponse()

        monkeypatch.setattr(alerts.httpx, "post", fake_post)

        _real_send("col1  col2", kind="intraday_transition", monospace=True)

        assert captured["text"] == "```\ncol1  col2\n```"
        assert captured["parse_mode"] == "Markdown"

    def test_default_sends_plain_text_with_no_parse_mode(self, monkeypatch):
        monkeypatch.setattr(
            alerts, "_env",
            lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc", "BREAKINGTRADE_CHAT_ID_INTRADAY": "-100X"},
        )
        captured = {}

        def fake_post(url, json, timeout):
            captured.update(json)
            return _FakeResponse()

        monkeypatch.setattr(alerts.httpx, "post", fake_post)

        _real_send("a short note", kind="intraday_transition")

        assert captured["text"] == "a short note"
        assert "parse_mode" not in captured


def test_alert_is_recorded_even_when_delivery_fails():
    """Undelivered must still mean captured - otherwise a Telegram outage silently erases the
    record of what the strategy claimed."""
    alerts.record("health", "something happened")
    stored = alerts.history()
    assert len(stored) == 1
    assert stored.iloc[0]["delivered"] == 0


def test_transitions_alert_one_row_per_symbol():
    count = alerts.alert_transitions(
        {"Live Print in Formation Up": ["VOLTAS", "PFC"], "Gap-Down Rescue": ["KEI"]},
        datetime(2026, 9, 4, 11, 16),
    )
    assert count == 3
    stored = alerts.history()
    assert set(stored["symbol"]) == {"VOLTAS", "PFC", "KEI"}
    assert set(stored["kind"]) == {"intraday_transition"}


def test_transitions_message_explains_the_call_in_plain_english():
    """The whole point: a trader reading 'GapDnRescue' on its own shouldn't have to look it up."""
    alerts.alert_transitions({"Gap-Down Rescue": ["KEI"]}, datetime(2026, 9, 4, 11, 16))
    with alerts._connect() as conn:
        message = conn.execute("SELECT message FROM alerts LIMIT 1").fetchone()[0]
    assert "bought it back" in message
    assert "(GapDnRescue)" in message


def test_scan_reason_has_an_entry_for_every_scan_alert_transitions_can_receive():
    """Every scan name scans.py can hand to alert_transitions must have a plain-English
    reason - a missing entry means a trader sees the unhelpful fallback with no warning."""
    from signal_engine.analysis.breakingtrade.scans import SCANS

    for scan in SCANS:
        assert scan.name in alerts._SCAN_REASON, f"no reason documented for {scan.name!r}"


def test_scan_reason_fallback_for_an_unknown_scan():
    assert "not documented yet" in alerts.scan_reason("Some New Vendor Scan")


def _plan(**overrides):
    from types import SimpleNamespace

    defaults = {
        "symbol": "TCS",
        "direction": "up",
        "entry": 2283.1,
        "stop": 2270.0,
        "targets": [2310.0, 2325.0, 2340.0],
        "triggered_at": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_trade_signal_includes_the_reason_when_a_scan_name_is_given():
    alerts.alert_trade_signal(_plan(), scan_name="Gap-Down Rescue")
    with alerts._connect() as conn:
        message = conn.execute("SELECT message FROM alerts LIMIT 1").fetchone()[0]
    assert "Reason: Gapped down but buyers stepped in" in message


def test_trade_signal_omits_the_reason_line_when_no_scan_name_is_given():
    """No scan (e.g. a manually-tested plan) must not print a bogus reason."""
    alerts.alert_trade_signal(_plan())
    with alerts._connect() as conn:
        message = conn.execute("SELECT message FROM alerts LIMIT 1").fetchone()[0]
    assert "Reason:" not in message


def test_trade_signal_shows_r_r_for_a_real_trade_plan():
    """The R:R quick-glance line (matching ORB/BREAKOUT's own alerts) must appear for a real
    TradePlan, which always has reward_risk - the SimpleNamespace test double above omits it
    on purpose (see alert_trade_signal()'s getattr) but production plans never do."""
    from signal_engine.analysis.breakingtrade.trigger import TradePlan

    plan = TradePlan(
        symbol="TCS", direction="up", day_type="Trend", entry=2283.1, stop=2270.0,
        targets=[2296.1, 2302.6, 2309.1], split=(0.5, 0.3, 0.2), ib_high=2290.0,
        ib_low=2270.0, atr=13.1, risk_per_share=13.1,
    )
    alerts.alert_trade_signal(plan)
    with alerts._connect() as conn:
        message = conn.execute("SELECT message FROM alerts LIMIT 1").fetchone()[0]
    assert "R:R: 1:1.98" in message  # (2309.1 - 2283.1) / 13.1, rounded


def test_trade_signal_with_a_reason_still_parses_as_a_valid_signal():
    """Reason: is not one of parser.py's mandatory fields - it must not break parsing."""
    from signal_engine import parser

    alerts.alert_trade_signal(_plan(), scan_name="Gap-Down Rescue")
    with alerts._connect() as conn:
        message = conn.execute("SELECT message FROM alerts LIMIT 1").fetchone()[0]
    signal = parser.parse(message)
    assert signal is not None
    assert signal.symbol == "TCS"
    assert signal.entry == 2283.1


def test_no_transitions_records_nothing():
    """Silence is not an event - alerting 'nothing changed' every poll trains the reader to
    ignore the channel."""
    assert alerts.alert_transitions({}, datetime(2026, 9, 4, 11, 16)) == 0
    assert alerts.history().empty


def _watchlist():
    return pd.DataFrame(
        [
            {
                "symbol": "SWIGGY",
                "price": 276.1,
                "change_pct": 2.53,
                "delivery_pct": 0.807,
                "day_type": "Normal Var",
            }
        ]
    )


def test_btst_alert_records_the_candidate():
    assert alerts.alert_btst(_watchlist(), datetime(2026, 9, 4, 14, 50)) == 1
    stored = alerts.history()
    assert len(stored) == 1
    assert stored.iloc[0]["scan"] == "BTST"


def test_empty_btst_list_is_still_recorded():
    """A day with no candidates is a data point, not a non-event."""
    alerts.alert_btst(pd.DataFrame(), datetime(2026, 9, 4, 14, 50))
    assert alerts.history().iloc[0]["kind"] == "btst_empty"


# ---------------------------------------------------------------------------
# Channel routing - two strategies must never share a channel
# ---------------------------------------------------------------------------


def test_each_strategy_routes_to_its_own_channel(monkeypatch):
    monkeypatch.setattr(
        alerts,
        "_env",
        lambda: {
            "BREAKINGTRADE_BOT_TOKEN": "123:abc",
            "BREAKINGTRADE_CHAT_ID_BTST": "-100BTST",
            "BREAKINGTRADE_CHAT_ID_INTRADAY": "-100INTRA",
        },
    )
    assert alerts.chat_id_for("btst") == "-100BTST"
    assert alerts.chat_id_for("btst_empty") == "-100BTST"
    assert alerts.chat_id_for("intraday_transition") == "-100INTRA"
    assert alerts.chat_id_for("health") == "-100INTRA"


def test_missing_channel_never_falls_back_to_another(monkeypatch):
    """An earlier single-channel setup delivered these into the channel breakout.pine uses.
    Falling back on a missing key would silently repeat that, so it must not deliver at all."""
    monkeypatch.setattr(
        alerts,
        "_env",
        lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc", "BREAKINGTRADE_CHAT_ID_INTRADAY": "-100X"},
    )
    assert alerts.chat_id_for("btst") is None


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def test_due_marks_cover_every_scheduled_poll():
    marks = [f"{m:%H:%M}" for m in _due_marks(date(2026, 9, 7))]
    assert "09:20" in marks  # opening stretch
    assert "10:31" in marks  # phased just after a TPO period closes
    assert "14:50" in marks  # BTST decision, before the 15:15 cutoff
    assert "15:20" not in marks  # after the cutoff - would be an untradeable list
    assert len(marks) == len(set(marks))


def test_due_marks_stop_before_the_cas_cutoff():
    """Nothing may be scheduled after 15:10: continuous trading in F&O stocks ends at 15:15."""
    latest = max(f"{m:%H:%M}" for m in _due_marks(date(2026, 9, 7)))
    assert latest <= "15:10"


def test_heartbeat_is_quiet_during_the_scheduled_lunch_gap():
    """The false alarm this locks in: 13:00-14:50 is documented POLL_WINDOWS silence, not a
    failed poller - the heartbeat must not watch there."""
    from datetime import time as _time

    assert _within_poll_hours(_time(13, 30)) is False
    assert _within_poll_hours(_time(14, 21)) is False


def test_heartbeat_watches_inside_every_scheduled_window():
    from datetime import time as _time

    assert _within_poll_hours(_time(9, 30)) is True  # first window
    assert _within_poll_hours(_time(11, 0)) is True  # second window
    assert _within_poll_hours(_time(15, 0)) is True  # third window


def test_heartbeat_buffer_covers_a_failure_right_at_a_windows_close():
    from datetime import time as _time

    assert _within_poll_hours(_time(13, 2)) is True  # 2 min past the 13:00 window close


def test_auto_stop_is_safely_after_the_last_scheduled_window():
    """The self-stop time must never cut off a mark that could still be due - it exists to end
    the day, not to shorten it."""
    last_window_end = max(end for _start, end, _minutes in POLL_WINDOWS)
    assert AUTO_STOP_TIME > last_window_end
