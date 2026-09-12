"""Tests for the weekly watchlist screen.

Covers what does not need the network: the digest message can never be mistaken for a
trade signal (the same "observation traffic must never look like a trade" convention
used throughout signal_engine/pinescripts), the weekly gate, conviction tracking, and
maybe_run_weekly_screen()'s orchestration. run_screen()/_beta_vs_nifty()/
fetch_surveillance_symbols() hit yfinance and NSE respectively and are exercised by
hand (uv run --group analysis python -m signal_engine.scripts.watchlist_screen
--dry-run), not here - a live-data assertion in the suite would be flaky by
definition and would fail in CI with no network access.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.scripts import watchlist_screen as wls
from signal_engine.scripts.watchlist_screen import (
    BLOCKED_SYMBOLS,
    CONVICTION_WINDOW,
    ScreenResult,
    compute_conviction,
    format_digest,
    maybe_run_weekly_screen,
    should_run_this_week,
)


def _sample_result(**overrides) -> ScreenResult:
    defaults = {
        "symbols": ["TCS", "INFY", "SAIL"],
        "universe_size": 210,
        "stage_counts": {
            "universe": 210,
            "liquidity_atr_rvol_not_surveilled": 142,
            "beta_band": 40,
        },
        "computed_at": datetime(2026, 9, 13, 7, 0),
        "data_through": "2026-09-11",
    }
    defaults.update(overrides)
    return ScreenResult(**defaults)


class TestDigestIsNotATradeSignal:
    def test_parser_rejects_the_digest_header(self):
        """The parser's header regex needs a LONG/SHORT/EXIT token to accept a message
        at all. A digest with none of those tokens is dropped whole, same as the
        RUNNER observation pattern - defense in depth even on an unsubscribed channel."""
        message = format_digest(_sample_result())
        assert parse(normalize(message)) is None

    @pytest.mark.parametrize("symbol", ["SAIL", "IDEA", "MCX"])
    def test_no_watchlist_symbol_creates_a_false_header(self, symbol):
        """None of the tickers themselves should combine with the header text to
        accidentally read as '<TAG> LONG | SYMBOL'."""
        result = _sample_result(symbols=[symbol])
        assert parse(normalize(format_digest(result))) is None

    def test_surveillance_fetch_failure_warning_also_stays_unparseable(self):
        result = _sample_result(surveillance_fetch_failed=True)
        assert parse(normalize(format_digest(result))) is None

    def test_conviction_annotations_also_stay_unparseable(self):
        result = _sample_result(conviction={"TCS": 4, "INFY": 1, "SAIL": 2})
        assert parse(normalize(format_digest(result))) is None


class TestDigestContent:
    def test_includes_every_symbol(self):
        message = format_digest(_sample_result())
        for sym in ["TCS", "INFY", "SAIL"]:
            assert sym in message

    def test_includes_stage_counts_and_data_date(self):
        """The funnel counts (universe -> liquidity/ATR/RVOL/surveillance -> beta
        band) are what let a reader sanity-check the screen without re-running it."""
        message = format_digest(_sample_result())
        assert "210" in message and "142" in message and "40" in message
        assert "2026-09-11" in message

    def test_states_tradingview_update_is_manual(self):
        """Regression: this script must never be read as "the watchlist is now live" -
        it updates the repo file and notifies; TradingView is a separate, human step."""
        message = format_digest(_sample_result()).lower()
        assert "manually" in message

    def test_surveillance_fetch_failure_is_surfaced_not_silent(self):
        """A degraded run (ASM/GSM check failed, static blacklist only) must say so in
        the message a human actually reads - never fail silently to a shorter list."""
        message = format_digest(_sample_result(surveillance_fetch_failed=True))
        assert "WARNING" in message and "ASM/GSM" in message

    def test_clean_run_has_no_warning(self):
        message = format_digest(_sample_result(surveillance_fetch_failed=False))
        assert "WARNING" not in message

    def test_conviction_shown_per_symbol(self):
        result = _sample_result(conviction={"TCS": 4, "INFY": 1, "SAIL": 2})
        message = format_digest(result)
        assert "TCS (4/4)" in message
        assert "INFY (1/4)" in message
        assert "SAIL (2/4)" in message

    def test_symbol_missing_from_conviction_defaults_to_one(self):
        """A symbol with no recorded conviction (e.g. computed out of band) should
        read as a first appearance, not crash the digest."""
        message = format_digest(_sample_result(conviction={}))
        assert "TCS (1/4)" in message


class TestBlockedSymbols:
    def test_matches_the_hard_blacklist_this_session_established(self):
        """YESBANK (config.yaml global blacklist) and BHEL (ORB/BREAKOUT hard block) -
        a signal on either is rejected downstream regardless of how well it screens.
        NSE ASM/GSM membership is checked live and layered on top of this, not baked
        into the static set, since surveillance status changes over time."""
        assert BLOCKED_SYMBOLS == frozenset({"YESBANK", "BHEL"})


class TestWeeklyGate:
    """This is what replaces a fixed cron time: the screen runs on whatever day the
    system next starts up, but only actually does work once per ISO calendar week."""

    def test_no_state_file_means_should_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        assert should_run_this_week() is True

    def test_corrupt_state_file_fails_toward_running_not_skipping(self, tmp_path, monkeypatch):
        """A missing/bad state file must never silently suppress the screen forever -
        that would be a worse failure than just running it an extra time."""
        state = tmp_path / "state.json"
        state.write_text("{not json")
        monkeypatch.setattr(wls, "STATE_FILE", state)
        assert should_run_this_week() is True

    def test_marking_this_week_gates_a_second_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        now = datetime(2026, 9, 13)  # a Sunday, ISO week 37 of 2026
        wls.mark_run_this_week(["TCS"], now)
        assert should_run_this_week(now) is False

    def test_a_new_iso_week_reopens_the_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        wls.mark_run_this_week(["TCS"], datetime(2026, 9, 13))
        assert should_run_this_week(datetime(2026, 9, 21)) is True

    def test_year_boundary_uses_iso_week_not_calendar_year(self):
        """Naive (year, day // 7) breaks at the turn of the year; ISO week numbering
        does not - 2026-12-31 and 2027-01-01 can fall in the same ISO week."""
        key_1 = wls._current_week_key(datetime(2026, 12, 28))
        key_2 = wls._current_week_key(datetime(2026, 12, 30))
        assert key_1 == key_2  # both mid-week, same ISO week


class TestConvictionTracking:
    def test_first_appearance_is_one_of_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        assert compute_conviction(["TCS"]) == {"TCS": 1}

    def test_repeated_appearance_accumulates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        wls.mark_run_this_week(["TCS", "INFY"], datetime(2026, 8, 30))
        wls.mark_run_this_week(["TCS"], datetime(2026, 9, 6))
        assert compute_conviction(["TCS"]) == {"TCS": 3}  # 2 past + this run
        assert compute_conviction(["INFY"]) == {"INFY": 2}  # 1 past + this run

    def test_a_symbol_absent_from_history_starts_fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        wls.mark_run_this_week(["TCS"], datetime(2026, 8, 30))
        assert compute_conviction(["NEWSTOCK"]) == {"NEWSTOCK": 1}

    def test_history_capped_at_conviction_window(self, tmp_path, monkeypatch):
        """Runs older than CONVICTION_WINDOW must not keep inflating a count forever -
        conviction measures recent persistence, not all-time appearances."""
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        start = datetime(2026, 1, 1)
        for week_offset in range(CONVICTION_WINDOW + 3):
            wls.mark_run_this_week(["TCS"], start + timedelta(weeks=week_offset))
        # This run would make it CONVICTION_WINDOW+1 if history were unbounded.
        assert compute_conviction(["TCS"])["TCS"] == CONVICTION_WINDOW + 1


class TestMaybeRunWeeklyScreen:
    """The function openalgoscheduler._run_startup() calls as its last step."""

    def _stub_pipeline(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(wls, "WATCHLIST_FILE", tmp_path / "watchlist.txt")
        result = _sample_result(symbols=["TCS"])
        monkeypatch.setattr(wls, "run_screen", lambda: result)
        sent = {"called": False}

        async def fake_send(message):
            sent["called"] = True
            return True

        monkeypatch.setattr(wls, "send_digest", fake_send)
        return sent

    def test_runs_and_marks_the_gate_on_first_call(self, tmp_path, monkeypatch):
        sent = self._stub_pipeline(monkeypatch, tmp_path)
        assert maybe_run_weekly_screen() is True
        assert sent["called"] is True
        assert should_run_this_week() is False

    def test_second_call_same_week_is_skipped(self, tmp_path, monkeypatch):
        sent = self._stub_pipeline(monkeypatch, tmp_path)
        maybe_run_weekly_screen()
        sent["called"] = False
        assert maybe_run_weekly_screen() is False
        assert sent["called"] is False, "gated call must not touch Telegram at all"

    def test_force_bypasses_an_already_satisfied_gate(self, tmp_path, monkeypatch):
        sent = self._stub_pipeline(monkeypatch, tmp_path)
        maybe_run_weekly_screen()
        sent["called"] = False
        assert maybe_run_weekly_screen(force=True) is True
        assert sent["called"] is True

    def test_result_passed_to_send_digest_carries_conviction(self, tmp_path, monkeypatch):
        """The digest actually sent must include conviction, not the empty dict
        run_screen() returns on its own - a real regression this refactor could hide."""
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(wls, "WATCHLIST_FILE", tmp_path / "watchlist.txt")
        wls.mark_run_this_week(["TCS"], datetime(2026, 8, 30))
        result = _sample_result(symbols=["TCS"])
        monkeypatch.setattr(wls, "run_screen", lambda: result)
        captured = {}

        async def fake_send(message):
            captured["message"] = message
            return True

        monkeypatch.setattr(wls, "send_digest", fake_send)
        maybe_run_weekly_screen()
        assert "TCS (2/4)" in captured["message"]


class TestSurveillanceFetchFallback:
    def test_network_failure_falls_back_to_static_blacklist_and_reports_it(self, monkeypatch):
        """A briefly-unreachable NSE endpoint must not abort the whole weekly screen -
        but the digest must say the run was degraded, never present it as clean."""
        import httpx

        class BoomClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **kw):
                raise httpx.ConnectError("simulated NSE outage")

        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: BoomClient())
        symbols, failed = wls.fetch_surveillance_symbols()
        assert failed is True
        assert symbols == set()
