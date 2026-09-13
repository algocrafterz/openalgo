"""Tests for the compute-daily/decide-weekly watchlist screen.

Covers what does not need the network: the digest message can never be mistaken for a
trade signal (the same "observation traffic must never look like a trade" convention
used throughout signal_engine/pinescripts), the weekly gate, daily-history conviction
tracking, and maybe_run_weekly_screen()'s orchestration of the two speeds.
run_daily_scan()/_beta_vs_nifty()/fetch_surveillance_symbols() hit yfinance and NSE
respectively and are exercised by hand (uv run --group analysis python -m
signal_engine.scripts.watchlist_screen --dry-run), not here - a live-data assertion
in the suite would be flaky by definition and would fail in CI with no network access.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.scripts import watchlist_screen as wls
from signal_engine.scripts.watchlist_screen import (
    BLOCKED_SYMBOLS,
    DAILY_WINDOW,
    DailyScan,
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
        result = _sample_result(conviction={"TCS": 8, "INFY": 1, "SAIL": 4})
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
        result = _sample_result(conviction={"TCS": 8, "INFY": 1, "SAIL": 4})
        message = format_digest(result)
        assert f"TCS (8/{DAILY_WINDOW})" in message
        assert f"INFY (1/{DAILY_WINDOW})" in message
        assert f"SAIL (4/{DAILY_WINDOW})" in message

    def test_symbol_missing_from_conviction_defaults_to_zero(self):
        """A symbol with no recorded conviction (e.g. computed out of band) should
        read as never having been seen, not crash the digest."""
        message = format_digest(_sample_result(conviction={}))
        assert f"TCS (0/{DAILY_WINDOW})" in message


class TestBlockedSymbols:
    def test_matches_the_hard_blacklist_this_session_established(self):
        """YESBANK (config.yaml global blacklist) and BHEL (ORB/BREAKOUT hard block) -
        a signal on either is rejected downstream regardless of how well it screens.
        NSE ASM/GSM membership is checked live and layered on top of this, not baked
        into the static set, since surveillance status changes over time."""
        assert BLOCKED_SYMBOLS == frozenset({"YESBANK", "BHEL"})


class TestWeeklyGate:
    """The daily scan (run_daily_scan) is unconditional; only the finalize/notify
    step is gated, and only per ISO calendar week."""

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
        wls._save_state({"last_run_week": wls._current_week_key(now)})
        assert should_run_this_week(now) is False

    def test_a_new_iso_week_reopens_the_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        wls._save_state({"last_run_week": wls._current_week_key(datetime(2026, 9, 13))})
        assert should_run_this_week(datetime(2026, 9, 21)) is True

    def test_year_boundary_uses_iso_week_not_calendar_year(self):
        """Naive (year, day // 7) breaks at the turn of the year; ISO week numbering
        does not - two nearby late-December dates can fall in the same ISO week."""
        key_1 = wls._current_week_key(datetime(2026, 12, 28))
        key_2 = wls._current_week_key(datetime(2026, 12, 30))
        assert key_1 == key_2


class TestDailyScanHistory:
    """The part that replaced a coarse 4-week conviction count with a much richer
    10-trading-day one, per the reasoning that fast-moving factors (RVOL) should be
    checked as often as they change, not on the same clock as the slow one (beta)."""

    def _record_day(self, monkeypatch, tmp_path, date_str: str, symbols: list[str]):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        state = wls._load_state()
        history = state.get("daily_history", [])
        history = [e for e in history if e["date"] != date_str]
        history.append({"date": date_str, "symbols": symbols})
        state["daily_history"] = history[-DAILY_WINDOW:]
        wls._save_state(state)

    def test_first_appearance_is_one_hit(self, tmp_path, monkeypatch):
        self._record_day(monkeypatch, tmp_path, "2026-09-13", ["TCS"])
        assert compute_conviction(["TCS"]) == {"TCS": 1}

    def test_repeated_days_accumulate(self, tmp_path, monkeypatch):
        self._record_day(monkeypatch, tmp_path, "2026-09-10", ["TCS", "INFY"])
        self._record_day(monkeypatch, tmp_path, "2026-09-11", ["TCS"])
        self._record_day(monkeypatch, tmp_path, "2026-09-12", ["TCS"])
        assert compute_conviction(["TCS"]) == {"TCS": 3}
        assert compute_conviction(["INFY"]) == {"INFY": 1}

    def test_a_symbol_absent_from_history_starts_at_zero(self, tmp_path, monkeypatch):
        self._record_day(monkeypatch, tmp_path, "2026-09-10", ["TCS"])
        assert compute_conviction(["NEWSTOCK"]) == {"NEWSTOCK": 0}

    def test_history_capped_at_daily_window(self, tmp_path, monkeypatch):
        """Days older than DAILY_WINDOW must drop out - conviction measures recent
        persistence, not all-time appearances."""
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        start = datetime(2026, 1, 1)
        for offset in range(DAILY_WINDOW + 3):
            day = (start + timedelta(days=offset)).date().isoformat()
            self._record_day(monkeypatch, tmp_path, day, ["TCS"])
        assert compute_conviction(["TCS"])["TCS"] == DAILY_WINDOW

    def test_running_the_same_day_twice_does_not_duplicate(self, tmp_path, monkeypatch):
        """A bot restarted twice in one day must not double-count that day - history
        is keyed by calendar date, overwritten not appended."""
        self._record_day(monkeypatch, tmp_path, "2026-09-13", ["TCS"])
        self._record_day(monkeypatch, tmp_path, "2026-09-13", ["TCS", "INFY"])
        assert compute_conviction(["TCS"]) == {"TCS": 1}
        assert compute_conviction(["INFY"]) == {"INFY": 1}


class TestRankingTieBreak:
    """Regression: on the very first run (or any time several symbols tie on
    conviction), sorting by conviction alone falls back to alphabetical order and
    silently discards the RVOL/ATR% ranking that mattered before conviction existed."""

    def test_equal_conviction_breaks_tie_by_rvol_then_atr(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        # Alphabetically AAPL-like name would sort first if conviction tied and
        # nothing else broke the tie - it must NOT win here despite that.
        daily = DailyScan(
            qualifying_symbols=["AAA", "ZZZ"], universe_size=2,
            surveillance_fetch_failed=False, data_through="2026-09-11",
            metrics={"AAA": (0.20, 0.9), "ZZZ": (0.30, 1.5)},
        )
        monkeypatch.setattr(wls, "_beta_vs_nifty", lambda syms: dict.fromkeys(syms, 1.5))
        result = wls.run_weekly_screen(daily)
        assert result.symbols[0] == "ZZZ", "higher RVOL must win the tie, not the alphabet"


class TestMaybeRunWeeklyScreen:
    """The function openalgoscheduler._run_startup() calls as its last step, on
    every startup - the daily scan always runs; only the finalize is gated."""

    def _stub_pipeline(self, monkeypatch, tmp_path, qualifying=None):
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(wls, "WATCHLIST_FILE", tmp_path / "watchlist.txt")
        daily = DailyScan(
            qualifying_symbols=qualifying or ["TCS"], universe_size=1,
            surveillance_fetch_failed=False, data_through="2026-09-11",
        )
        monkeypatch.setattr(wls, "run_daily_scan", lambda: daily)
        monkeypatch.setattr(wls, "_beta_vs_nifty", lambda syms: dict.fromkeys(syms, 1.5))
        sent = {"called": False}

        async def fake_send(message):
            sent["called"] = True
            return True

        monkeypatch.setattr(wls, "send_digest", fake_send)
        return sent

    def test_daily_scan_always_runs_even_when_gated_off(self, tmp_path, monkeypatch):
        """This is the whole point of the redesign: the cheap scan must run on every
        startup regardless of the weekly gate, so history stays dense."""
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        wls._save_state({"last_run_week": wls._current_week_key()})  # gate satisfied
        called = {"n": 0}

        def fake_daily():
            called["n"] += 1
            return DailyScan(["TCS"], 1, False, "2026-09-11")

        monkeypatch.setattr(wls, "run_daily_scan", fake_daily)
        assert maybe_run_weekly_screen() is False  # gated off
        assert called["n"] == 1  # but the daily scan still ran

    def test_finalizes_and_marks_the_gate_on_first_call(self, tmp_path, monkeypatch):
        sent = self._stub_pipeline(monkeypatch, tmp_path)
        assert maybe_run_weekly_screen() is True
        assert sent["called"] is True
        assert should_run_this_week() is False

    def test_second_call_same_week_skips_finalize_not_the_daily_scan(self, tmp_path, monkeypatch):
        sent = self._stub_pipeline(monkeypatch, tmp_path)
        maybe_run_weekly_screen()
        sent["called"] = False
        assert maybe_run_weekly_screen() is False
        assert sent["called"] is False, "gated finalize must not touch Telegram at all"

    def test_force_bypasses_an_already_satisfied_gate(self, tmp_path, monkeypatch):
        sent = self._stub_pipeline(monkeypatch, tmp_path)
        maybe_run_weekly_screen()
        sent["called"] = False
        assert maybe_run_weekly_screen(force=True) is True
        assert sent["called"] is True

    def test_digest_sent_carries_daily_history_conviction(self, tmp_path, monkeypatch):
        """The digest actually sent must reflect real accumulated daily history, not
        an empty conviction dict - a real regression this split could hide."""
        monkeypatch.setattr(wls, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(wls, "WATCHLIST_FILE", tmp_path / "watchlist.txt")
        state = {"daily_history": [
            {"date": "2026-09-10", "symbols": ["TCS"]},
            {"date": "2026-09-11", "symbols": ["TCS"]},
        ]}
        wls._save_state(state)
        daily = DailyScan(["TCS"], 1, False, "2026-09-12")
        monkeypatch.setattr(wls, "run_daily_scan", lambda: daily)
        monkeypatch.setattr(wls, "_beta_vs_nifty", lambda syms: {"TCS": 1.5})
        captured = {}

        async def fake_send(message):
            captured["message"] = message
            return True

        monkeypatch.setattr(wls, "send_digest", fake_send)
        maybe_run_weekly_screen()
        assert f"TCS (2/{DAILY_WINDOW})" in captured["message"]


class TestSurveillanceFetchFallback:
    def test_network_failure_falls_back_to_static_blacklist_and_reports_it(self, monkeypatch):
        """A briefly-unreachable NSE endpoint must not abort the whole daily scan -
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
