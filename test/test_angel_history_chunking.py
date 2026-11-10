"""Angel One history chunking: every chunk must reach the end of its last day.

Offline only: get_api_response and the symbol lookups are replaced by a fake
that records each request window and returns synthetic candles for exactly
that window.
"""

from datetime import timedelta

import pandas as pd
import pytest

from broker.angel.api import data as angel_data

IST = "+05:30"


def _old_chunk_end(chunk_start, chunk_days, to_date):
    """The pre-fix rule, kept as a negative control."""
    return min(chunk_start + timedelta(days=chunk_days - 1), to_date)


class FakeAngel:
    """Records requests and answers with one candle per minute in the window."""

    def __init__(self, weekdays_only=False, empty_calls=(), extra_rows=None):
        self.requests = []
        self.weekdays_only = weekdays_only
        self.empty_calls = set(empty_calls)
        self.extra_rows = extra_rows or {}

    def __call__(self, endpoint, auth, method="GET", payload=""):
        idx = len(self.requests)
        self.requests.append((pd.Timestamp(payload["fromdate"]), pd.Timestamp(payload["todate"])))
        if idx in self.empty_calls:
            return {"status": True, "data": []}
        start, end = self.requests[-1]
        minutes = pd.date_range(start, end, freq="min")
        if self.weekdays_only:
            minutes = minutes[minutes.dayofweek < 5]
        rows = [[m.strftime("%Y-%m-%dT%H:%M:%S") + IST, 1, 2, 0.5, 1.5, 10] for m in minutes]
        rows.extend(self.extra_rows.get(idx, []))
        return {"status": True, "data": rows}


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(angel_data, "get_br_symbol", lambda s, e: s)
    monkeypatch.setattr(angel_data, "get_token", lambda s, e: "1")

    def _run(fake, start, end, interval="1m"):
        monkeypatch.setattr(angel_data, "get_api_response", fake)
        client = angel_data.BrokerData("token")
        return client.get_history("TEST", "NSE", interval, start, end)

    return _run


def _days(start, end):
    return pd.date_range(start, end, freq="D")


def _epochs(start, end, weekdays_only=False):
    minutes = pd.date_range(
        pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(hours=23, minutes=59), freq="min"
    )
    if weekdays_only:
        minutes = minutes[minutes.dayofweek < 5]
    return {int(m.tz_localize("Asia/Kolkata").timestamp()) for m in minutes}


def _assert_adjacent(requests, first_day, last_day):
    assert requests[0][0] == pd.Timestamp(first_day)
    for (_, prev_end), (next_start, _) in zip(requests, requests[1:], strict=False):
        assert prev_end.hour == 23 and prev_end.minute == 59
        assert next_start == prev_end.normalize() + pd.Timedelta(days=1)
    assert requests[-1][1] == pd.Timestamp(last_day) + pd.Timedelta(hours=23, minutes=59)


def test_70_day_range_covers_every_minute_of_every_day(run):
    fake = FakeAngel()
    df = run(fake, "2026-01-01", "2026-03-11")  # 70 days
    assert len(fake.requests) == 3
    _assert_adjacent(fake.requests, "2026-01-01", "2026-03-11")
    assert set(df["timestamp"]) == _epochs("2026-01-01", "2026-03-11")
    assert len(df) == 70 * 1440


def test_70_day_range_negative_control_old_rule_drops_boundary_days(run, monkeypatch):
    monkeypatch.setattr(angel_data, "_angel_chunk_end", _old_chunk_end)
    fake = FakeAngel()
    df = run(fake, "2026-01-01", "2026-03-11")
    missing = _epochs("2026-01-01", "2026-03-11") - set(df["timestamp"])
    assert missing, "old rule must lose minutes"
    lost_days = {
        pd.Timestamp(t, unit="s", tz="UTC").tz_convert("Asia/Kolkata").date().isoformat()
        for t in missing
    }
    # Last day of each non-final 30-day chunk (Jan 30 and Mar 1) is never requested,
    # and the rest of the day after 00:00 is lost on those days.
    assert {"2026-01-30", "2026-03-01"} <= lost_days


def test_exactly_one_chunk_long(run):
    fake = FakeAngel()
    df = run(fake, "2026-01-01", "2026-01-30")  # 30 days
    assert len(fake.requests) == 1
    _assert_adjacent(fake.requests, "2026-01-01", "2026-01-30")
    assert len(df) == 30 * 1440


def test_one_day_longer_than_a_chunk(run):
    fake = FakeAngel()
    df = run(fake, "2026-01-01", "2026-01-31")  # 31 days
    assert len(fake.requests) == 2
    _assert_adjacent(fake.requests, "2026-01-01", "2026-01-31")
    assert fake.requests[1][0] == pd.Timestamp("2026-01-31")
    assert set(df["timestamp"]) == _epochs("2026-01-01", "2026-01-31")


def test_shorter_than_a_chunk(run):
    fake = FakeAngel()
    df = run(fake, "2026-01-01", "2026-01-10")
    assert len(fake.requests) == 1
    _assert_adjacent(fake.requests, "2026-01-01", "2026-01-10")
    assert len(df) == 10 * 1440


def test_single_day(run):
    fake = FakeAngel()
    df = run(fake, "2026-01-07", "2026-01-07")
    assert fake.requests == [(pd.Timestamp("2026-01-07 00:00"), pd.Timestamp("2026-01-07 23:59"))]
    assert len(df) == 1440


def test_chunk_boundaries_on_weekends(run):
    # 2026-01-03 is a Saturday: chunk 1 ends Sunday 2026-02-01, chunk 2 starts Monday.
    fake = FakeAngel(weekdays_only=True)
    df = run(fake, "2026-01-03", "2026-03-04")
    assert len(fake.requests) == 3
    _assert_adjacent(fake.requests, "2026-01-03", "2026-03-04")
    assert fake.requests[0][1].dayofweek == 6
    assert set(df["timestamp"]) == _epochs("2026-01-03", "2026-03-04", weekdays_only=True)


def test_overlapping_rows_at_boundary_are_deduplicated(run):
    # Broker repeats the last minute of chunk 1 inside chunk 2's response.
    dup = ["2026-01-30T23:59:00" + IST, 1, 2, 0.5, 1.5, 10]
    fake = FakeAngel(extra_rows={1: [dup]})
    df = run(fake, "2026-01-01", "2026-02-15")
    assert df["timestamp"].is_unique
    assert df["timestamp"].is_monotonic_increasing
    assert set(df["timestamp"]) == _epochs("2026-01-01", "2026-02-15")


def test_empty_chunk_does_not_drop_neighbours(run):
    fake = FakeAngel(empty_calls=[1])
    df = run(fake, "2026-01-01", "2026-03-11")
    assert len(fake.requests) == 3
    expected = _epochs("2026-01-01", "2026-01-30") | _epochs("2026-03-02", "2026-03-11")
    assert set(df["timestamp"]) == expected


def test_all_chunks_empty_returns_empty_frame(run):
    fake = FakeAngel(empty_calls=[0, 1])
    df = run(fake, "2026-01-01", "2026-02-15")
    assert df.empty


def test_timestamp_offset_maps_to_correct_epoch(run):
    fake = FakeAngel()
    df = run(fake, "2026-01-07", "2026-01-07")
    # 09:15 IST on 2026-01-07 is 03:45 UTC.
    expected = int(pd.Timestamp("2026-01-07 03:45", tz="UTC").timestamp())
    assert expected in set(df["timestamp"])
    assert df["timestamp"].min() == int(pd.Timestamp("2026-01-06 18:30", tz="UTC").timestamp())


@pytest.mark.parametrize("interval,limit", [("1m", 30), ("5m", 100), ("15m", 200), ("D", 2000)])
def test_chunk_never_exceeds_interval_limit_and_no_gap(interval, limit):
    start = pd.Timestamp("2026-01-01")
    to_date = start + pd.Timedelta(days=limit * 2 + 5, hours=23, minutes=59)
    current = start
    windows = []
    while current <= to_date:
        end = angel_data._angel_chunk_end(current, limit, to_date)
        windows.append((current, end))
        assert end - current < pd.Timedelta(days=limit)
        current = end.normalize() + timedelta(days=1)
    assert len(windows) == 3
    for (_, e), (s, _) in zip(windows, windows[1:], strict=False):
        assert s == e.normalize() + pd.Timedelta(days=1)
    assert windows[-1][1] == to_date


def test_daily_interval_single_request_reaches_end_of_day(run):
    fake = FakeAngel()
    run(fake, "2026-01-01", "2026-03-11", interval="D")
    assert len(fake.requests) == 1
    assert fake.requests[0][1] == pd.Timestamp("2026-03-11 23:59")
