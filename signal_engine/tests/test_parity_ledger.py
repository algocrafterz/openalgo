"""The multi-session Pine versus Python parity ledger and its migration gate."""

import datetime as dt
import sqlite3

import pandas as pd
import pytest

from signal_engine.pipeline import parity_ledger as pl
from signal_engine.pipeline import shadow_report as sr
from signal_engine.pipeline.state import RunnerStateStore, ShadowEvent
from signal_engine.timeutils import IST

DAY = dt.date(2026, 10, 8)


def ts(hm):
    return pd.Timestamp(f"2026-10-08 {hm}", tz=IST)


def event(symbol, hm, direction="LONG", kind="signal"):
    sid = f"BREAKOUT-PY-{symbol}-20261008-{hm.replace(':', '')}"
    return ShadowEvent(DAY, "BREAKOUT-PY", symbol, ts(hm), kind, direction, sid)


def pine(symbol, hm, direction="LONG", status="SUCCESS"):
    sid = f"{symbol}-20261008-{hm.replace(':', '')}"
    return sr.PineSignal(sid, symbol, direction, ts(hm), status, "")


class TestWilson:
    def test_known_values(self):
        assert pl.wilson_lower(95, 100) == pytest.approx(0.8882, abs=1e-3)
        assert pl.wilson_lower(190, 200) == pytest.approx(0.9100, abs=1e-3)
        assert pl.wilson_lower(0, 10) == 0.0

    def test_no_trials_is_zero_not_an_error(self):
        assert pl.wilson_lower(0, 0) == 0.0

    def test_it_never_exceeds_the_observed_rate(self):
        for k, n in [(10, 10), (57, 60), (199, 200)]:
            assert pl.wilson_lower(k, n) <= k / n


class TestClassification:
    @pytest.mark.parametrize(
        "reason,expected",
        [
            ("outside the runner universe", pl.UNIVERSE),
            ("runner skipped it: outside the entry window", pl.SPEC_RULE),
            ("runner skipped it: sig_id already seen (x)", pl.SPEC_RULE),
            ("runner skipped it: price 12.0 below the floor", pl.SPEC_RULE),
            ("runner skipped it: something new", pl.UNEXPLAINED_PINE_ONLY),
            ("runner did not process this bar (engine down, or symbol never evaluated)", pl.OUTAGE),
            ("runner evaluated the bar and did not fire", pl.UNEXPLAINED_PINE_ONLY),
        ],
    )
    def test_pine_only_reasons(self, reason, expected):
        assert pl.classify_pine_only(reason) == expected


class TestDay:
    def test_a_day_is_classified_and_the_raw_numbers_are_kept(self):
        events = [event("TCS", "10:00"), event("INFY", "10:05"), event("SBIN", "10:10", "SHORT")]
        pine_rows = [pine("TCS", "10:05"), pine("SBIN", "10:15", "LONG"), pine("ZZZ", "10:20")]
        rep = sr.compare(events, pine_rows, universe={"TCS", "INFY", "SBIN"}, last_bars={},
                         runner_strategy="BREAKOUT-PY")
        d = pl.day_parity(rep, DAY, pine_signals=3, runner_signals=3)
        assert d.counts[pl.MATCHED] == 1  # TCS lines up after the one-bar shift
        assert d.counts[pl.UNEXPLAINED_DIRECTION] == 1  # SBIN: runner SHORT, Pine LONG
        assert d.counts[pl.UNEXPLAINED_RUNNER_ONLY] == 1  # INFY: Pine sent nothing
        assert d.counts[pl.UNIVERSE] == 1  # ZZZ: outside the universe
        assert (d.pine_signals, d.runner_signals) == (3, 3)


def _split(total, parts):
    """`total` spread over `parts` days with nothing lost to integer division."""
    return [total // parts + (1 if i < total % parts else 0) for i in range(parts)]


def tally(matched, unexplained, sessions, explained=0):
    t = pl.ParityTally()
    for i, (m, u) in enumerate(zip(_split(matched, sessions), _split(unexplained, sessions), strict=True)):
        d = pl.DayParity(dt.date(2026, 10, 1) + dt.timedelta(days=i))
        d.counts = {pl.MATCHED: m, pl.UNEXPLAINED_RUNNER_ONLY: u, pl.UNIVERSE: explained}
        t.days.append(d)
    return t


class TestGate:
    def test_enough_evidence_and_high_parity_meets_the_gate(self):
        t = tally(matched=380, unexplained=10, sessions=20)  # 390 signals, 97.4 percent
        verdict, reasons = t.gate()
        assert verdict == "MET" and reasons == []

    def test_a_high_rate_on_too_few_sessions_is_not_met(self):
        verdict, reasons = tally(matched=380, unexplained=10, sessions=5).gate()
        assert verdict == "NOT_MET" and any("sessions 5" in r for r in reasons)

    def test_a_high_rate_on_too_few_signals_is_not_met(self):
        verdict, reasons = tally(matched=100, unexplained=0, sessions=20).gate()
        assert verdict == "NOT_MET" and any("signals 100" in r for r in reasons)

    def test_the_wilson_bound_can_fail_while_the_point_estimate_passes(self):
        t = tally(matched=19, unexplained=1, sessions=1)  # 95 percent of 20 signals
        verdict, reasons = t.gate()
        assert verdict == "NOT_MET" and any("Wilson" in r for r in reasons)

    def test_explained_differences_stay_out_of_the_rate_but_are_reported(self):
        t = tally(matched=380, unexplained=10, sessions=20, explained=3)
        assert t.observed == pytest.approx(380 / 390)
        assert t.total(pl.UNIVERSE) == 60

    def test_no_data_is_never_a_pass(self):
        verdict, reasons = pl.ParityTally().gate()
        assert verdict == "NOT_MET" and any("sessions 0" in r for r in reasons)

    def test_the_report_says_retiring_pine_is_not_live_authorization_and_keeps_raw_numbers(self):
        text = pl.format_ledger(tally(8, 49, 1), "BREAKOUT-PY", "BREAKOUT")
        assert "not live authorization" in text and "raw:" in text and "NOT_MET" in text


class TestBuildTally:
    def test_only_days_the_runner_recorded_count_as_sessions(self, tmp_path):
        db = str(tmp_path / "runner_state.db")
        with RunnerStateStore(db) as store:
            store.record_shadow(DAY, "BREAKOUT-PY", "TCS", ts("10:00"), "signal", direction="LONG",
                                sig_id="BREAKOUT-PY-TCS-20261008-1000")
        trades = str(tmp_path / "trades.db")
        conn = sqlite3.connect(trades)
        conn.execute("create table trades (id integer primary key, strategy text, direction text, "
                     "symbol text, status text, message text, sig_id text, executed_at text, "
                     "trade_mode text, signal_time text, received_at text, entry real, sl real, tp real, "
                     "quantity int, order_id text, raw_message text, context text, fill_price real, "
                     "data_quality text)")
        conn.commit()
        conn.close()
        t = pl.build_tally(dt.date(2026, 10, 5), DAY, pine_strategy="BREAKOUT", runner_strategy="BREAKOUT-PY",
                           state_db=db, trades_db=trades, universe={"TCS"})
        assert t.sessions == 1 and t.total(pl.UNEXPLAINED_RUNNER_ONLY) == 1
