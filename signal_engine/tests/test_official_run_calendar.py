"""The official NSE calendar owns a published-dataset run's calendar.

Calendar decides whether the market was scheduled to trade; data quality decides whether a
symbol-session is usable. A date is never dropped from the run calendar because its data is
poor, and an official session with no usable symbol is named with its reason.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.data_collect import DataCollector
from signal_engine.backtest.data_readiness import intraday_access as ia
from signal_engine.backtest.session import derive_calendar, official_calendar

IST = "Asia/Kolkata"
D = dt.date
#: Official sessions of the stub calendar: Thu 27 Aug to Tue 1 Sep 2026 (29-30 Aug a weekend).
SESSIONS = (D(2026, 8, 27), D(2026, 8, 28), D(2026, 8, 31), D(2026, 9, 1))
OFFICIAL = SimpleNamespace(
    version="stub-official-v1",
    expected_sessions=lambda start, end: tuple(d for d in SESSIONS if start <= d <= end),
)


def _bars(days, n: int = 3) -> pd.DataFrame:
    stamps = [pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15 + 5 * i)
              for d in days for i in range(n)]
    idx = pd.DatetimeIndex(stamps).tz_localize(IST)
    c = np.linspace(100, 101, len(idx))
    return pd.DataFrame({"Open": c, "High": c + 0.1, "Low": c - 0.1, "Close": c,
                         "Volume": 100.0}, index=idx)


def _layer(frames, collector=None, out_of_scope=()):
    return rr._build_data_layer(frames, collector or DataCollector(), {},
                                official=OFFICIAL, out_of_scope=out_of_scope)


class TestOfficialCalendar:
    def test_full_data_every_official_session_is_a_trading_day(self):
        frames = {f"S{i}": _bars(SESSIONS) for i in range(5)}
        layer = _layer(frames)
        assert layer.calendar.trading_days == SESSIONS
        assert layer.calendar.source == "official_nse_calendar"
        assert layer.calendar_check["empty_sessions"] == {}
        assert layer.calendar_check["bars_on_non_sessions"] == []
        rr._check_calendar_at_load(layer)

    def test_partial_data_session_stays_a_trading_day(self):
        frames = {f"S{i}": _bars(SESSIONS) for i in range(4)}
        frames["S4"] = _bars([d for d in SESSIONS if d != D(2026, 8, 28)])
        layer = _layer(frames)
        assert D(2026, 8, 28) in layer.calendar.trading_days
        assert layer.calendar_check["empty_sessions"] == {}

    def test_session_with_no_valid_data_is_named_not_dropped(self):
        gap = D(2026, 8, 28)
        frames = {f"S{i}": _bars([d for d in SESSIONS if d != gap]) for i in range(5)}
        col = DataCollector()
        col.empty_sessions = {gap: ia.ALL_SYMBOLS_INVALID}
        layer = _layer(frames, col)
        assert gap in layer.calendar.trading_days
        assert layer.calendar_check["empty_sessions"] == {"2026-08-28": "ALL_SYMBOLS_INVALID"}
        rr._check_calendar_at_load(layer)

    def test_session_absent_from_every_record_is_no_valid_data(self):
        gap = D(2026, 8, 28)
        frames = {f"S{i}": _bars([d for d in SESSIONS if d != gap]) for i in range(5)}
        layer = _layer(frames)
        assert layer.calendar_check["empty_sessions"] == {"2026-08-28": "NO_VALID_DATA"}

    def test_session_below_the_old_quorum_keeps_its_valid_data(self):
        # 2026-08-31: 3 of 10 symbols have valid bars; the derived calendar's 80% quorum
        # drops the date, the official calendar keeps it and its bars reach the engine
        day = D(2026, 8, 31)
        frames = {f"S{i}": _bars(SESSIONS if i < 3 else [d for d in SESSIONS if d != day])
                  for i in range(10)}
        assert day not in derive_calendar(frames).trading_days
        layer = _layer(frames)
        assert day in layer.calendar.trading_days
        assert layer.calendar_check["empty_sessions"] == {}
        assert (layer.frames["S0"].index.date == day).any()

    def test_out_of_scope_session_is_listed_and_not_a_trading_day(self):
        muhurat = D(2026, 8, 31)
        frames = {f"S{i}": _bars([d for d in SESSIONS if d != muhurat]) for i in range(5)}
        layer = _layer(frames, out_of_scope=(muhurat,))
        assert muhurat not in layer.calendar.trading_days
        assert layer.calendar_check["out_of_scope_sessions"] == ["2026-08-31"]
        assert "2026-08-31" not in layer.calendar_check["empty_sessions"]
        rr._check_calendar_at_load(layer)

    def test_bars_on_an_out_of_scope_session_are_an_anomaly(self):
        muhurat = D(2026, 8, 31)
        layer = _layer({"S0": _bars(SESSIONS)}, out_of_scope=(muhurat,))
        assert layer.calendar_check["bars_on_non_sessions"] == ["2026-08-31"]
        with pytest.raises(rr.LoadRejected, match="not official sessions"):
            rr._check_calendar_at_load(layer)

    def test_bars_on_a_non_trading_date_reject_the_run(self):
        saturday = D(2026, 8, 29)
        layer = _layer({"S0": _bars([*SESSIONS, saturday])})
        assert saturday not in layer.calendar.trading_days
        assert layer.calendar_check["bars_on_non_sessions"] == ["2026-08-29"]
        with pytest.raises(rr.LoadRejected, match="not official sessions"):
            rr._check_calendar_at_load(layer)

    def test_same_trading_days_as_derived_when_every_session_has_coverage(self):
        # IDEA-027 / IDEA-028: their derived calendar equalled the official one, so ownership
        # moves without moving a single trading day
        frames = {f"S{i}": _bars(SESSIONS) for i in range(5)}
        assert (official_calendar(frames, OFFICIAL).trading_days
                == derive_calendar(frames).trading_days)

    def test_as_of_cuts_the_official_range(self):
        frames = {"S0": _bars(SESSIONS)}
        cal = official_calendar(frames, OFFICIAL, as_of=D(2026, 8, 28))
        assert cal.trading_days == SESSIONS[:2]


class TestEmptySessionReasons:
    """read_bars names every session that ends with no usable symbol."""

    def _avail(self, rows):
        return pd.DataFrame(rows, columns=["symbol", "session", "available", "reason"]).assign(
            session=lambda f: pd.to_datetime(f.session))

    def test_all_symbols_invalid(self):
        avail = self._avail([("A", "2026-08-28", False, "price_invalid"),
                             ("B", "2026-08-28", False, "volume_invalid")])
        assert ia.empty_session_reasons(avail, set()) == {D(2026, 8, 28): ia.ALL_SYMBOLS_INVALID}

    def test_all_excluded_by_contract(self):
        avail = self._avail([("A", "2026-08-28", False, "price_invalid"),
                             ("B", "2026-08-28", False, "DATA-MISSING-BIN")])
        assert (ia.empty_session_reasons(avail, set())
                == {D(2026, 8, 28): ia.ALL_EXCLUDED_BY_CONTRACT})

    def test_available_but_no_bars_is_no_valid_data(self):
        avail = self._avail([("A", "2026-08-28", True, None)])
        assert ia.empty_session_reasons(avail, set()) == {D(2026, 8, 28): ia.NO_VALID_DATA}

    def test_loaded_session_is_not_empty(self):
        avail = self._avail([("A", "2026-08-28", True, None), ("B", "2026-08-28", False,
                                                               "price_invalid")])
        assert ia.empty_session_reasons(avail, {D(2026, 8, 28)}) == {}
