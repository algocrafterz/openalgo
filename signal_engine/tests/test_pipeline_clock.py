"""Bar-clock arithmetic is pure and shared; SimClock advances instantly."""

import asyncio

import pandas as pd

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
