"""Backfill request ranges must stay strictly inside Angel One's internal chunk limits.

Angel's plugin (broker/angel/api/data.py) splits a request into chunks of `limit` days and
ends every chunk except the last at 00:00 of its final day, so a request of exactly `limit`
days loses that final day's intraday bars. Verified 2026-10-04: 2019-12-11..2020-01-09
(30 days) returned no 2020-01-09 1m bars, 2019-12-12..2020-01-09 (29 days) did.
"""
import pandas as pd

from signal_engine.backtest import backfill as bf

#: Angel's own per-interval chunk limits, copied from broker/angel/api/data.py.
ANGEL_LIMITS = {"1m": 30, "D": 2000}


def _spans(days):
    out = list(bf._chunks(pd.Timestamp("2019-01-01"), pd.Timestamp("2026-10-01"), days))
    return [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in out]


def test_intraday_chunk_is_shorter_than_angel_limit():
    assert bf.CHUNK_DAYS < ANGEL_LIMITS["1m"]
    assert max((b - a).days + 1 for a, b in _spans(bf.CHUNK_DAYS)) < ANGEL_LIMITS["1m"]


def test_daily_chunk_is_shorter_than_angel_limit():
    assert bf.DAILY_CHUNK_DAYS < ANGEL_LIMITS["D"]
    assert max((b - a).days + 1 for a, b in _spans(bf.DAILY_CHUNK_DAYS)) < ANGEL_LIMITS["D"]


def test_chunks_are_contiguous_and_cover_the_range():
    spans = _spans(bf.CHUNK_DAYS)
    assert spans[0][0] == pd.Timestamp("2019-01-01")
    assert spans[-1][1] == pd.Timestamp("2026-10-01")
    for (_, prev_end), (start, _) in zip(spans, spans[1:], strict=False):
        assert start == prev_end + pd.Timedelta(days=1)
