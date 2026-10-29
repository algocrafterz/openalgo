import pandas as pd
import pytest

from signal_engine.shadow.tick_bars import Tick, build_bars, parse_exchange_time
from signal_engine.timeutils import IST


def _t(hms, ltp, vol, hi=None, lo=None, exch=None, day="2026-10-29"):
    arrival = pd.Timestamp(f"{day} {hms}", tz=IST)
    return Tick("SBIN", arrival, parse_exchange_time(exch, arrival), ltp, vol, hi, lo)


def test_ohlc_and_volume_delta_per_bar():
    ticks = [
        _t("09:15:02", 100.0, 1000),
        _t("09:17:00", 101.0, 1500),
        _t("09:19:59", 99.5, 2000),
        _t("09:20:01", 100.5, 2600),
    ]
    bars = build_bars(ticks)
    first = bars.loc[pd.Timestamp("2026-10-29 09:15", tz=IST)]
    assert (first.open, first.high, first.low, first.close) == (100.0, 101.0, 99.5, 99.5)
    assert first.volume == 2000 and first.ticks == 3
    second = bars.loc[pd.Timestamp("2026-10-29 09:20", tz=IST)]
    assert second.volume == 600 and second.close == 100.5


def test_day_extremes_recover_a_high_the_updates_skipped():
    ticks = [
        _t("09:15:02", 100.0, 100, hi=100.0, lo=100.0),
        _t("09:19:50", 100.2, 200, hi=100.2, lo=99.0),
        _t("09:20:10", 100.1, 300, hi=100.9, lo=99.0),
    ]
    bars = build_bars(ticks)
    second = bars.loc[pd.Timestamp("2026-10-29 09:20", tz=IST)]
    assert second.high == 100.1 and second.high_aug == 100.9
    assert second.low_aug == second.low


def test_a_silent_bar_is_kept_with_zero_ticks_and_the_volume_moves_on():
    bars = build_bars([_t("09:15:02", 100.0, 100), _t("09:25:01", 101.0, 900)])
    silent = bars.loc[pd.Timestamp("2026-10-29 09:20", tz=IST)]
    assert silent.ticks == 0 and pd.isna(silent.close)
    assert bars.loc[pd.Timestamp("2026-10-29 09:25", tz=IST)].volume == 800


def test_ticks_outside_the_session_and_zero_prices_are_dropped():
    bars = build_bars([_t("09:14:59", 100.0, 1), _t("09:16:00", 0.0, 2), _t("15:30:00", 100.0, 3)])
    assert bars.empty


def test_exchange_time_moves_a_late_arriving_trade_to_its_own_bar():
    late = _t("09:20:00", 105.0, 500, exch="09:19:59")
    ticks = [_t("09:16:00", 100.0, 100), late]
    by_arrival = build_bars(ticks, "arrival")
    by_exchange = build_bars(ticks, "exchange")
    assert by_arrival.loc[pd.Timestamp("2026-10-29 09:15", tz=IST)].close == 100.0
    assert by_exchange.loc[pd.Timestamp("2026-10-29 09:15", tz=IST)].close == 105.0


@pytest.mark.parametrize("raw", [None, "", "garbage", "25:61:61"])
def test_unreadable_exchange_time_is_none(raw):
    assert parse_exchange_time(raw, pd.Timestamp("2026-10-29 10:00", tz=IST)) is None


def test_unknown_time_source_is_refused():
    with pytest.raises(ValueError):
        build_bars([], "wallclock")
