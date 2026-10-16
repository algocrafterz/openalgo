"""Bar-clock arithmetic is pure and shared; SimClock advances instantly."""

import asyncio

import pandas as pd
import pytest

from signal_engine.pipeline.clock import SimClock, WallClock, next_cycle

IST = "Asia/Kolkata"


def ts(s):
    return pd.Timestamp(f"2026-09-30 {s}", tz=IST)


class TestNextCycle:
    def test_mid_bar_returns_next_boundary_plus_delay(self):
        close, run_at = next_cycle(ts("10:01:10"), 5, 3)
        assert close == ts("10:05:00")
        assert run_at == ts("10:05:03")

    def test_inside_the_delay_window_still_targets_the_boundary_just_passed(self):
        close, run_at = next_cycle(ts("10:05:02"), 5, 3)
        assert close == ts("10:05:00")
        assert run_at == ts("10:05:03")

    def test_after_the_delay_moves_to_the_next_boundary(self):
        close, _ = next_cycle(ts("10:05:03"), 5, 3)
        assert close == ts("10:10:00")

    def test_exactly_on_boundary_without_delay(self):
        close, run_at = next_cycle(ts("10:00:00"), 5, 0)
        assert close == ts("10:05:00") and run_at == close

    def test_boundaries_align_to_the_session_open(self):
        close, _ = next_cycle(ts("09:16:00"), 15, 0)
        assert close == ts("09:30:00")

    def test_utc_input_is_converted_to_ist(self):
        now = pd.Timestamp("2026-09-30 04:31:10", tz="UTC")  # 10:01:10 IST
        close, _ = next_cycle(now, 5, 3)
        assert close == ts("10:05:00")
        assert close.utcoffset() == pd.Timedelta(hours=5, minutes=30)


class TestSimClock:
    def test_sleep_until_advances_now_without_waiting(self):
        clock = SimClock(ts("09:50"))
        asyncio.run(clock.sleep_until(ts("10:05:03")))
        assert clock.now() == ts("10:05:03")

    def test_never_moves_backwards(self):
        clock = SimClock(ts("10:10"))
        asyncio.run(clock.sleep_until(ts("10:05")))
        assert clock.now() == ts("10:10")

    def test_requires_tz_aware_start(self):
        import pytest

        with pytest.raises(ValueError):
            SimClock(pd.Timestamp("2026-09-30 10:00"))


class TestWallClock:
    def test_now_is_ist_aware(self):
        assert WallClock().now().utcoffset() == pd.Timedelta(hours=5, minutes=30)

    def test_sleep_until_a_past_time_returns_immediately(self):
        asyncio.run(WallClock().sleep_until(WallClock().now() - pd.Timedelta(seconds=5)))


class _FakeWall(WallClock):
    """WallClock whose now() is a settable value, so a test can jump the wall clock."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t


class TestWallClockSurvivesSuspend:
    """asyncio.sleep runs on the monotonic clock, which stops while the host is suspended.

    A single long sleep to the next session would then wake hours late (2026-10-16: the runner
    slept through the whole trading day). sleep_until must nap in short slices and re-read the
    wall clock after each.
    """

    def test_wakes_when_wall_clock_jumps_past_target_during_a_nap(self, monkeypatch):
        clock = _FakeWall(ts("15:00:00"))
        target = ts("15:00:00") + pd.Timedelta(hours=18)
        naps = []

        async def fake_sleep(seconds):
            naps.append(seconds)
            clock.t = target + pd.Timedelta(seconds=1)  # host suspended; wall clock leapt ahead

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        asyncio.run(clock.sleep_until(target))

        assert len(naps) == 1
        assert naps[0] <= WallClock.MAX_NAP_S

    def test_never_naps_longer_than_the_cap_and_ends_at_the_target(self, monkeypatch):
        clock = _FakeWall(ts("09:00:00"))
        target = ts("09:00:00") + pd.Timedelta(seconds=100)
        naps = []

        async def fake_sleep(seconds):
            naps.append(seconds)
            clock.t = clock.t + pd.Timedelta(seconds=seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        asyncio.run(clock.sleep_until(target))

        assert all(n <= WallClock.MAX_NAP_S for n in naps)
        assert sum(naps) == pytest.approx(100)
        assert clock.t >= target
