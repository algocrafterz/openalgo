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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    # Never attempt a real network call from a test.
    monkeypatch.setattr(alerts, "send", lambda text, kind=None: (False, None))


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
