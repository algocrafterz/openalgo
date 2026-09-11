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
    # chat_id_for() calls _current_mode_suffix(), which calls OpenAlgo over HTTP - fake the
    # underlying review.trading_mode() call (never _current_mode_suffix itself, or
    # TestCurrentModeSuffix below couldn't exercise the real function) so tests are
    # deterministic and never touch the network, and reset its cache so no test leaks state
    # into the next one.
    monkeypatch.setattr(alerts.review, "trading_mode", lambda: ("analyze", True))
    alerts._mode_cache["checked_at"] = 0.0


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
        monkeypatch.setattr(alerts, "_env", lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc"})
        monkeypatch.setattr(
            _se_config, "settings",
            _fake_settings(channels=[TelegramChannel(name="intraday-breakingtrade-analyze", id=-100)]),
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
        monkeypatch.setattr(alerts, "_env", lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc"})
        monkeypatch.setattr(
            _se_config, "settings",
            _fake_settings(channels=[TelegramChannel(name="intraday-breakingtrade-analyze", id=-100)]),
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
    on purpose (see alert_trade_signal()'s getattr) but production plans never do.

    2026-09-11: the expected value changed from 1:1.98 to 1:0.99 because it was WRONG. It
    measured to targets[-1] while the alert's TP line carries targets[0], overstating every
    alert by exactly 2.00x. See TradePlan.reward_risk."""
    from signal_engine.analysis.breakingtrade.trigger import TradePlan

    plan = TradePlan(
        symbol="TCS", direction="up", day_type="Trend", entry=2283.1, stop=2270.0,
        targets=[2296.1, 2302.6, 2309.1], split=(0.5, 0.3, 0.2), ib_high=2290.0,
        ib_low=2270.0, atr=13.1, risk_per_share=13.1,
    )
    alerts.alert_trade_signal(plan)
    with alerts._connect() as conn:
        message = conn.execute("SELECT message FROM alerts LIMIT 1").fetchone()[0]
    assert "R:R: 1:0.99" in message  # (2296.1 - 2283.1) / 13.1 - the TP actually sent
    # The ladder is still reported, under its own label rather than as R:R.
    assert "Runner target (not traded yet): 2309.1 = 1:1.98" in message


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
# Channel routing - every chat id now comes from config.yaml (settings), never .env - see
# alerts.py's module docstring. `_fake_settings` stands in for the real Settings singleton.
# ---------------------------------------------------------------------------

import signal_engine.config as _se_config
from signal_engine.config import TelegramChannel


def _fake_settings(channels=(), btst=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        telegram_channels=tuple(channels),
        breakingtrade_btst_channels=dict(btst or {}),
    )


def test_each_strategy_routes_to_its_own_channel(monkeypatch):
    monkeypatch.setattr(alerts, "_env", lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc"})
    monkeypatch.setattr(
        _se_config,
        "settings",
        _fake_settings(
            channels=[TelegramChannel(name="intraday-breakingtrade-analyze", id=-100)],
            btst={"analyze": TelegramChannel(name="intraday-breakingtrade-btst-analyze", id=-200)},
        ),
    )
    assert alerts.chat_id_for("btst") == "-200"
    assert alerts.chat_id_for("btst_empty") == "-200"
    assert alerts.chat_id_for("intraday_transition") == "-100"
    assert alerts.chat_id_for("health") == "-100"


def test_watchlist_trade_signal_routes_to_its_own_channel(monkeypatch):
    """The two outcomes must never land in the same channel, or the whole point of comparing
    them side by side on separate paper P&L is lost."""
    monkeypatch.setattr(
        _se_config,
        "settings",
        _fake_settings(channels=[
            TelegramChannel(name="intraday-breakingtrade-analyze", id=-100),
            TelegramChannel(name="intraday-breakingtrade-watchlist-analyze", id=-200),
        ]),
    )
    assert alerts.chat_id_for("trade_signal") == "-100"
    assert alerts.chat_id_for("trade_signal_watchlist") == "-200"


# ---------------------------------------------------------------------------
# Channel routing - the SAME strategy must never mix its paper and live channels
# ---------------------------------------------------------------------------


def test_chat_id_for_uses_the_analyze_channel_in_analyze_mode(monkeypatch):
    monkeypatch.setattr(alerts, "_current_phase", lambda: "analyze")
    monkeypatch.setattr(
        _se_config,
        "settings",
        _fake_settings(channels=[
            TelegramChannel(name="intraday-breakingtrade-analyze", id=-100),
            TelegramChannel(name="intraday-breakingtrade-live", id=-200),
        ]),
    )
    assert alerts.chat_id_for("trade_signal") == "-100"


def test_chat_id_for_uses_the_live_channel_in_live_mode(monkeypatch):
    monkeypatch.setattr(alerts, "_current_phase", lambda: "live")
    monkeypatch.setattr(
        _se_config,
        "settings",
        _fake_settings(channels=[
            TelegramChannel(name="intraday-breakingtrade-analyze", id=-100),
            TelegramChannel(name="intraday-breakingtrade-live", id=-200),
        ]),
    )
    assert alerts.chat_id_for("trade_signal") == "-200"


def test_chat_id_for_does_not_deliver_to_live_until_the_live_channel_is_configured(monkeypatch):
    """A strategy still in its paper phase has no -live entry in config.yaml yet - flipping
    OpenAlgo to live (e.g. testing) must not accidentally fall back to the analyze channel; it
    must simply not deliver, exactly like any other unconfigured channel."""
    monkeypatch.setattr(alerts, "_current_phase", lambda: "live")
    monkeypatch.setattr(
        _se_config,
        "settings",
        _fake_settings(channels=[TelegramChannel(name="intraday-breakingtrade-analyze", id=-100)]),
    )
    assert alerts.chat_id_for("trade_signal") is None


def test_btst_chat_id_for_does_not_deliver_to_live_until_configured(monkeypatch):
    """Same rule as above, for BTST's separate breakingtrade_btst_channels mapping."""
    monkeypatch.setattr(alerts, "_current_phase", lambda: "live")
    monkeypatch.setattr(
        _se_config,
        "settings",
        _fake_settings(btst={"analyze": TelegramChannel(name="btst-analyze", id=-100)}),
    )
    assert alerts.chat_id_for("btst") is None


class TestCurrentPhase:
    """_current_phase() reads OpenAlgo's live analyze/live state via review.trading_mode() -
    these tests fake that call rather than hitting the network."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        alerts._mode_cache["checked_at"] = 0.0
        yield
        alerts._mode_cache["checked_at"] = 0.0

    def test_analyze_mode_maps_to_the_analyze_phase(self, monkeypatch):
        monkeypatch.setattr(alerts.review, "trading_mode", lambda: ("analyze", True))
        assert alerts._current_phase() == "analyze"

    def test_live_mode_maps_to_the_live_phase(self, monkeypatch):
        monkeypatch.setattr(alerts.review, "trading_mode", lambda: ("live", False))
        assert alerts._current_phase() == "live"

    def test_unreachable_openalgo_defaults_to_the_analyze_phase(self, monkeypatch):
        """An unreachable OpenAlgo must never be treated as license to post to the live
        channel - default to the lower-stakes destination, same reasoning as
        alert_started()'s three-state mode banner."""
        monkeypatch.setattr(alerts.review, "trading_mode", lambda: ("unknown", False))
        assert alerts._current_phase() == "analyze"

    def test_mode_is_cached_within_the_ttl(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            alerts.review, "trading_mode",
            lambda: (calls.append(1), ("live", False))[1],
        )
        alerts._current_phase()
        alerts._current_phase()
        assert len(calls) == 1


class TestPrimeModeCache:
    """__main__.py's _watch() already calls review.trading_mode() once at startup for its own
    log line and the alert_started() banner - prime_mode_cache() seeds _current_phase()'s
    cache from that result so alert_started()'s first send doesn't immediately repeat the
    identical OpenAlgo API call from a cold cache."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        alerts._mode_cache["checked_at"] = 0.0
        yield
        alerts._mode_cache["checked_at"] = 0.0

    def test_priming_with_analyze_avoids_a_refetch(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            alerts.review, "trading_mode",
            lambda: (calls.append(1), ("analyze", True))[1],
        )
        alerts.prime_mode_cache("analyze", True)
        assert alerts._current_phase() == "analyze"
        assert calls == []

    def test_priming_with_live_avoids_a_refetch(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            alerts.review, "trading_mode",
            lambda: (calls.append(1), ("live", False))[1],
        )
        alerts.prime_mode_cache("live", False)
        assert alerts._current_phase() == "live"
        assert calls == []

    def test_priming_with_unknown_mode_still_defaults_to_analyze(self):
        alerts.prime_mode_cache("unknown", False)
        assert alerts._current_phase() == "analyze"


def test_watchlist_trade_signal_uses_the_full_unabbreviated_strategy_name():
    """No short forms - a trader scanning two channels needs the full name at a glance, and
    parser.py's strategy tag must match config.yaml's strategy_profiles/blacklist keys."""
    alerts.alert_trade_signal(
        _plan(), strategy="BREAKINGTRADE-WATCHLIST", kind="trade_signal_watchlist"
    )
    with alerts._connect() as conn:
        message, kind = conn.execute("SELECT message, kind FROM alerts LIMIT 1").fetchone()
    assert message.startswith("BREAKINGTRADE-WATCHLIST LONG")
    assert kind == "trade_signal_watchlist"


def test_confirmed_trade_signal_still_defaults_to_the_original_strategy_and_kind():
    """Existing CONFIRMED callers (entry_watch.py, _emit_trade_signals) must keep working
    unchanged - the new kind/strategy params are additive, not a breaking rename."""
    alerts.alert_trade_signal(_plan())
    with alerts._connect() as conn:
        message, kind = conn.execute("SELECT message, kind FROM alerts LIMIT 1").fetchone()
    assert message.startswith("BREAKINGTRADE LONG")
    assert kind == "trade_signal"


def test_missing_channel_never_falls_back_to_another(monkeypatch):
    """An earlier single-channel setup delivered these into the channel breakout.pine uses.
    Falling back on a missing key would silently repeat that, so it must not deliver at all."""
    monkeypatch.setattr(
        _se_config,
        "settings",
        _fake_settings(channels=[TelegramChannel(name="intraday-breakingtrade-analyze", id=-100)]),
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
