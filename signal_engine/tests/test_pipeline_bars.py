"""Closed-bar rule, replay and OpenAlgo history sources, retry-until-present."""

import asyncio

import httpx
import pandas as pd
import pytest

from signal_engine.pipeline.bars import (
    MissingBarsError,
    OpenAlgoHistorySource,
    ReplaySource,
    closed_only,
    fetch_current,
)
from signal_engine.pipeline.clock import SimClock

IST = "Asia/Kolkata"
DAY = pd.Timestamp("2026-09-30", tz=IST).date()


def ts(s):
    return pd.Timestamp(f"2026-09-30 {s}", tz=IST)


def frame(times, base=100.0):
    idx = pd.DatetimeIndex([ts(t) for t in times])
    n = len(idx)
    return (
        pd.DataFrame(
            {"Open": base, "High": base + 1, "Low": base - 1, "Close": base, "Volume": 10.0},
            index=idx,
        )
        if n
        else pd.DataFrame()
    )


class TestClosedOnly:
    def test_drops_the_forming_bar(self):
        df = frame(["09:15", "09:20", "09:25"])
        out = closed_only(df, ts("09:25:00"), 5)
        assert list(out.index) == [ts("09:15"), ts("09:20")]

    def test_bar_is_closed_at_exactly_open_plus_interval(self):
        df = frame(["09:15", "09:20"])
        assert len(closed_only(df, ts("09:25:00"), 5)) == 2

    def test_one_second_early_is_still_forming(self):
        df = frame(["09:15", "09:20"])
        assert len(closed_only(df, ts("09:24:59"), 5)) == 1

    def test_empty_frame_passes_through(self):
        assert closed_only(pd.DataFrame(), ts("10:00"), 5).empty


class TestReplaySource:
    def make(self):
        d5 = {"A": frame(["09:15", "09:20", "09:25"]), "B": frame(["09:15", "09:20"])}
        d1 = {"A": frame(["09:15", "09:16"])}
        return ReplaySource(d5, d1)

    def test_returns_only_closed_bars_of_the_day(self):
        out = asyncio.run(self.make().fetch(["A", "B"], 5, DAY, ts("09:25:00")))
        assert len(out["A"]) == 2 and len(out["B"]) == 2

    def test_other_days_are_excluded(self):
        old = frame(["09:15"])
        old.index = old.index - pd.Timedelta(days=1)
        src = ReplaySource({"A": pd.concat([old, frame(["09:15"])])})
        out = asyncio.run(src.fetch(["A"], 5, DAY, ts("10:00")))
        assert len(out["A"]) == 1

    def test_missing_symbols_are_named_and_partial_frames_kept(self):
        with pytest.raises(MissingBarsError) as e:
            asyncio.run(self.make().fetch(["A", "ZZZ", "YYY"], 5, DAY, ts("09:25")))
        assert e.value.missing == ["ZZZ", "YYY"]
        assert "A" in e.value.frames
        assert "ZZZ" in str(e.value) and "YYY" in str(e.value)

    def test_one_minute_interval_uses_the_one_minute_frames(self):
        out = asyncio.run(self.make().fetch(["A"], 1, DAY, ts("09:17")))
        assert len(out["A"]) == 2


class FakeClient:
    """Stands in for httpx.AsyncClient: the suite blocks real AsyncClient.post by design."""

    def __init__(self, handler):
        self._handler = handler

    async def post(self, url, json=None, timeout=None):
        request = httpx.Request("POST", url, json=json)
        result = self._handler(request, json)
        if asyncio.iscoroutine(result):
            result = await result
        result.request = request
        return result


def history_client(payloads: dict, calls: list | None = None):
    def handler(request, body):
        if calls is not None:
            calls.append(body)
        rows = payloads.get(body["symbol"])
        if rows is None:
            return httpx.Response(200, json={"status": "success", "data": []})
        if rows == "boom":
            return httpx.Response(500, json={"status": "error"})
        return httpx.Response(200, json={"status": "success", "data": rows})

    return FakeClient(handler)


def epoch(s):
    return int(ts(s).timestamp())


def row(s, px=100.0):
    return {
        "timestamp": epoch(s),
        "open": px,
        "high": px + 1,
        "low": px - 1,
        "close": px,
        "volume": 5,
    }


class TestOpenAlgoHistorySource:
    def make(self, client, **kw):
        return OpenAlgoHistorySource(
            client, base_url="http://x", api_key="k", timeout_s=5, max_concurrent=2, **kw
        )

    def test_epoch_seconds_become_ist_and_columns_are_capitalised(self):
        client = history_client({"A": [row("09:15"), row("09:20")]})
        out = asyncio.run(self.make(client).fetch(["A"], 5, DAY, ts("09:25")))
        df = out["A"]
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.index[0] == ts("09:15")
        assert df.index[0].utcoffset() == pd.Timedelta(hours=5, minutes=30)

    def test_forming_bar_is_dropped(self):
        client = history_client({"A": [row("09:15"), row("09:20")]})
        out = asyncio.run(self.make(client).fetch(["A"], 5, DAY, ts("09:22")))
        assert list(out["A"].index) == [ts("09:15")]

    def test_payload_has_key_symbol_interval_and_day(self):
        calls: list = []
        client = history_client({"A": [row("09:15")]}, calls)
        asyncio.run(self.make(client).fetch(["A"], 5, DAY, ts("09:25")))
        c = calls[0]
        assert (c["apikey"], c["symbol"], c["exchange"], c["interval"]) == ("k", "A", "NSE", "5m")
        assert c["start_date"] == c["end_date"] == "2026-09-30"

    def test_missing_and_failing_symbols_are_all_named(self):
        client = history_client({"A": [row("09:15")], "C": "boom"})
        with pytest.raises(MissingBarsError) as e:
            asyncio.run(self.make(client).fetch(["A", "B", "C"], 5, DAY, ts("09:25")))
        assert sorted(e.value.missing) == ["B", "C"]
        assert list(e.value.frames) == ["A"]

    def test_timeout_is_treated_as_missing(self):
        def handler(request, body):
            raise httpx.ReadTimeout("slow", request=request)

        client = FakeClient(handler)
        with pytest.raises(MissingBarsError) as e:
            asyncio.run(self.make(client).fetch(["A"], 5, DAY, ts("09:25")))
        assert e.value.missing == ["A"]

    def test_concurrency_is_bounded(self):
        live = {"now": 0, "peak": 0}

        async def handler(request, body):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.01)
            live["now"] -= 1
            return httpx.Response(200, json={"data": [row("09:15")]})

        client = FakeClient(handler)
        asyncio.run(self.make(client).fetch([f"S{i}" for i in range(8)], 5, DAY, ts("09:25")))
        assert live["peak"] <= 2

    def test_one_minute_interval_string(self):
        calls: list = []
        client = history_client({"A": [row("09:15")]}, calls)
        asyncio.run(self.make(client).fetch(["A"], 1, DAY, ts("09:25")))
        assert calls[0]["interval"] == "1m"


class FlakySource:
    """Serves the just-closed bar only after `ready_after` calls."""

    def __init__(self, ready_after):
        self.calls = 0
        self.ready_after = ready_after

    async def fetch(self, symbols, interval_min, day, now):
        self.calls += 1
        times = ["09:15", "09:20"] if self.calls >= self.ready_after else ["09:15"]
        return {s: frame(times) for s in symbols}


class TestFetchCurrent:
    def test_retries_until_the_just_closed_bar_arrives(self):
        clock = SimClock(ts("09:25:03"))
        src = FlakySource(ready_after=3)
        frames, late = asyncio.run(
            fetch_current(src, ["A"], 5, DAY, clock, ts("09:25"), deadline_s=25, backoff_s=2)
        )
        assert late == [] and src.calls == 3 and len(frames["A"]) == 2

    def test_gives_up_at_the_deadline_and_names_late_symbols(self):
        clock = SimClock(ts("09:25:03"))
        src = FlakySource(ready_after=999)
        frames, late = asyncio.run(
            fetch_current(src, ["A", "B"], 5, DAY, clock, ts("09:25"), deadline_s=6, backoff_s=2)
        )
        assert late == ["A", "B"] and frames == {}

    def test_missing_symbols_from_the_source_are_reported_late(self):
        class Missing:
            async def fetch(self, symbols, interval_min, day, now):
                raise MissingBarsError(["B"], {"A": frame(["09:15", "09:20"])})

        clock = SimClock(ts("09:25:03"))
        frames, late = asyncio.run(
            fetch_current(
                Missing(), ["A", "B"], 5, DAY, clock, ts("09:25"), deadline_s=1, backoff_s=1
            )
        )
        assert list(frames) == ["A"] and late == ["B"]
