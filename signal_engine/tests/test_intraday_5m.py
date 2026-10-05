"""5m aggregation and its independent validation (INTRADAY-5M-DATASET-PLAN.md section 8)."""

from datetime import date, datetime

import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import intraday_5m as i5
from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.timeutils import IST

T = ic.load_templates()
PRE = date(2024, 1, 2)
CAS = date(2026, 8, 4)


def _one(day, minutes, symbol="AAA"):
    rows = []
    for i, m in enumerate(minutes):
        ts = int(datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=IST).timestamp())
        p = 100.0 + i * 0.1
        rows.append({"symbol": symbol, "session": pd.Timestamp(day), "timestamp": ts,
                     "open": p, "high": p + 0.5, "low": p - 0.5, "close": p + 0.2,
                     "volume": 10.0 + i})
    return pd.DataFrame(rows)


def _exp(*days, symbol="AAA"):
    return pd.DataFrame({"symbol": symbol, "session": [pd.Timestamp(d) for d in days]})


def _check(m1, m5=None, exp=None):
    m5 = i5.aggregate(m1) if m5 is None else m5
    return i5.validate(m1, m5, T, exp if exp is not None else _exp(PRE, CAS))


def test_full_pre_auction_session_gives_75_bars_and_validates():
    m1 = _one(PRE, range(555, 930))
    m5 = i5.aggregate(m1)
    assert len(m5) == 75 and (m5.n_minutes == 5).all()
    first = m5.iloc[0]
    assert first.open == 100.0 and first.close == pytest.approx(100.6)
    assert first.volume == pytest.approx(sum(10.0 + i for i in range(5)))
    assert all(v == 0 for v in _check(m1).values())


def test_auction_print_is_its_own_1525_bar():
    m1 = _one(CAS, [*range(555, 915), 929])
    m5 = i5.aggregate(m1)
    assert len(m5) == 73
    last = m5.iloc[-1]
    assert datetime.fromtimestamp(last.timestamp, IST).strftime("%H:%M") == "15:25"
    assert last.n_minutes == 1 and last.close == m1.iloc[-1].close
    assert all(v == 0 for v in _check(m1).values())


def test_absent_minutes_leave_bins_absent_never_filled():
    m1 = _one(PRE, [*range(560, 600), *range(610, 930)])
    m5 = i5.aggregate(m1)
    stamps = {datetime.fromtimestamp(t, IST).strftime("%H:%M") for t in m5.timestamp}
    assert "09:15" not in stamps and "10:00" not in stamps and "09:20" in stamps
    assert all(v == 0 for v in _check(m1).values())


def test_no_bin_spans_two_sessions():
    m1 = pd.concat([_one(PRE, range(925, 930)), _one(date(2024, 1, 3), range(555, 560))])
    m5 = i5.aggregate(m1)
    assert len(m5) == 2 and m5.session.nunique() == 2
    assert all(v == 0 for v in _check(m1, exp=_exp(PRE, date(2024, 1, 3))).values())


@pytest.mark.parametrize("change, check", [
    (lambda d: d.assign(high=d.high.where(d.index != 3, d.high + 1)), "4_ohlc"),
    (lambda d: d.assign(volume=d.volume.where(d.index != 3, d.volume + 1)), "5_volume"),
    (lambda d: pd.concat([d, d.iloc[[3]]]), "6_duplicate"),
    (lambda d: d.assign(timestamp=d.timestamp.where(d.index != 3, d.timestamp + 120)),
     "1_bin_start"),
    (lambda d: d.drop(index=0), "2_first_bin"),
    (lambda d: d.assign(session=d.session.where(d.index != 3, pd.Timestamp("2024-01-03"))),
     "3_cross_session"),
])
def test_each_corruption_is_caught_by_its_check(change, check):
    m1 = _one(PRE, range(555, 930))
    bad = change(i5.aggregate(m1)).reset_index(drop=True)
    assert _check(m1, bad, exp=_exp(PRE, date(2024, 1, 3)))[check] > 0


def test_session_outside_the_expected_list_is_flagged():
    m1 = _one(PRE, range(555, 560))
    assert _check(m1, exp=_exp(CAS))["8_unexpected_session"] == 1


def test_auction_era_bins_between_continuous_end_and_auction_are_flagged():
    m1 = _one(CAS, [*range(555, 915), 916, 929])
    assert _check(m1)["9_auction"] > 0


def test_bar_count_above_the_template_maximum_is_flagged():
    m1 = _one(CAS, range(555, 930))     # continuous minutes the auction era does not have
    assert _check(m1)["7_bar_count"] == 1
