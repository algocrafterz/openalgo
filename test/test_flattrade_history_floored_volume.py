"""Flattrade history floors a negative bar volume to 0; the floor must not be silent.

2.0.2.8 floors TPSeries' negative 'intv' (its closing-session counter switch) so charts accept the
history. signal_engine's intraday dataset rule C3 flags a session by a negative regular-session
volume, and it only ever sees the floored value, so a regular-session counter fault would pass as
a quiet zero-volume bar. The parser therefore reports what it floored: a warning outside the known
NSE/BSE closing session, a debug line inside it. The returned frame is unchanged.
"""

import logging

import pytest

from broker.flattrade.api import data as fd

DAY = "23-10-2026"


def _candle(hhmm: str, volume: int) -> dict:
    return {"stat": "Ok", "time": f"{DAY} {hhmm}:00", "into": "100", "inth": "101",
            "intl": "99", "intc": "100", "intv": str(volume), "oi": "0"}


@pytest.fixture
def history(monkeypatch):
    monkeypatch.setenv("BROKER_API_KEY", "uid:::secret")
    monkeypatch.setattr(fd, "get_br_symbol", lambda symbol, exchange: "INFY-EQ")
    monkeypatch.setattr(fd, "get_token", lambda symbol, exchange: "1594")

    def run(candles, exchange="NSE"):
        monkeypatch.setattr(fd, "get_api_response", lambda *a, **k: list(reversed(candles)))
        return fd.BrokerData("token").get_history("INFY", exchange, "1m", "2026-10-23", "2026-10-23")

    return run


def _floor_warnings(caplog):
    return [r for r in caplog.records
            if r.name == fd.logger.name and r.levelno == logging.WARNING and "floored" in r.getMessage()]


def test_a_regular_session_negative_volume_is_floored_and_warned(history, caplog):
    caplog.set_level(logging.DEBUG, logger=fd.logger.name)
    df = history([_candle("13:59", 500), _candle("14:00", -13357992), _candle("14:01", 700)])

    assert df["volume"].tolist() == [500, 0, 700]
    warnings = _floor_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "INFY" in message and "NSE" in message and "1 bar" in message and "14:00" in message


def test_a_closing_session_negative_volume_is_floored_without_a_warning(history, caplog):
    caplog.set_level(logging.DEBUG, logger=fd.logger.name)
    df = history([_candle("15:19", 500), _candle("15:20", -500), _candle("15:21", 700)])

    assert df["volume"].tolist() == [500, 0, 700]
    assert _floor_warnings(caplog) == []
    assert any(r.levelno == logging.DEBUG and "floored" in r.getMessage() for r in caplog.records)


def test_no_negative_volume_reports_nothing(history, caplog):
    caplog.set_level(logging.DEBUG, logger=fd.logger.name)
    df = history([_candle("10:00", 500), _candle("10:01", 0)])

    assert df["volume"].tolist() == [500, 0]
    assert not [r for r in caplog.records if "floored" in r.getMessage()]


def test_the_closing_window_exemption_is_nse_bse_only(history, caplog):
    """MCX trades into the evening; 15:20 there is the regular session."""
    caplog.set_level(logging.DEBUG, logger=fd.logger.name)
    df = history([_candle("15:20", -500)], exchange="MCX")

    assert df["volume"].tolist() == [0]
    assert len(_floor_warnings(caplog)) == 1
