"""fire_to_signal is a pure mapping from a strategy Fire to the pipeline's Signal."""

import math

import pandas as pd
import pytest

from signal_engine.models import Direction
from signal_engine.pipeline.signals import fire_to_signal, sig_id
from signal_engine.pipeline.strategy import Fire

IST = "Asia/Kolkata"
BAR = pd.Timestamp("2026-09-30 09:55", tz=IST)


def make_fire(direction=1, **ctx) -> Fire:
    context = {"score": 9, "conf": 2, "pdh": 101.5, "ibl": 98.0, "tp2": 103.0, "tp3": 104.0}
    context.update(ctx)
    return Fire(
        symbol="TCS",
        bar_time=BAR,
        direction=direction,
        entry_ref=100.0,
        sl=99.0 if direction == 1 else 101.0,
        targets=(101.0, 102.0, 103.0) if direction == 1 else (99.0, 98.0, 97.0),
        tag="PDH-RT",
        context=context,
    )


class TestSigId:
    def test_format_matches_the_pine_shape(self):
        assert sig_id("TCS", BAR) == "TCS-20260930-0955"

    def test_bar_time_must_be_tz_aware(self):
        with pytest.raises(ValueError, match="tz-aware"):
            sig_id("TCS", pd.Timestamp("2026-09-30 09:55"))

    def test_utc_time_is_shifted_to_ist_before_formatting(self):
        utc = pd.Timestamp("2026-09-30 04:25", tz="UTC")  # 09:55 IST
        assert sig_id("TCS", utc) == "TCS-20260930-0955"


class TestFireToSignal:
    def test_long_mapping(self):
        s = fire_to_signal(make_fire(1), product="MIS", exchange="NSE")
        assert (s.strategy, s.direction, s.symbol) == ("BREAKOUT", Direction.LONG, "TCS")
        assert (s.entry, s.sl, s.tp) == (100.0, 99.0, 101.0)
        assert (s.exchange, s.product, s.time) == ("NSE", "MIS", "09:55")
        assert s.sig_id == "TCS-20260930-0955"

    def test_short_mapping(self):
        s = fire_to_signal(make_fire(-1), product="MIS")
        assert s.direction == Direction.SHORT
        assert (s.sl, s.tp) == (101.0, 99.0)

    def test_strategy_name_comes_from_the_argument(self):
        s = fire_to_signal(make_fire(1), product="MIS", strategy="ORB")
        assert s.strategy == "ORB"

    def test_context_carries_trigger_and_level_keys_for_structural_runner_sl(self):
        s = fire_to_signal(make_fire(1), product="MIS")
        assert s.context["trigger"] == "PDH-RT"
        assert s.context["pdh"] == 101.5
        assert s.context["tp2"] == 103.0 and s.context["tp3"] == 104.0

    def test_nan_context_values_are_dropped(self):
        s = fire_to_signal(make_fire(1, pvah=math.nan), product="MIS")
        assert "pvah" not in s.context

    def test_received_at_is_naive(self):
        s = fire_to_signal(make_fire(1), product="MIS")
        assert s.received_at.tzinfo is None

    def test_no_targets_is_an_error(self):
        f = make_fire(1)
        bad = Fire(**{**f.__dict__, "targets": ()})
        with pytest.raises(ValueError, match="targets"):
            fire_to_signal(bad, product="MIS")
