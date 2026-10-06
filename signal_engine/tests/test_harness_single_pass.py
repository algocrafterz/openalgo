"""Backtest(single_pass=True): same trades as the cached run, nothing retained."""

import pytest

from signal_engine.backtest.__main__ import REGISTRY, _register
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.tests.swing_fixtures import synthetic_frames


def _make(**kw):
    _register()
    cls, params_cls, _ = REGISTRY["ibs_meanrev"]
    bt = Backtest(
        cls(), synthetic_frames(), SwingRunConfig(cost_bps=24.0), engine=simulate_swing, **kw
    )
    return bt, params_cls()


def test_single_pass_matches_cached_run_and_releases():
    cached, p = _make(release_raw=False)
    expected = cached.trades(p)
    assert expected
    bt, p = _make(release_raw=True, single_pass=True)
    got = bt.trades(p)
    assert got == expected
    assert bt._prep == {}
    assert all(f is None for f in bt.frames.values())
    with pytest.raises(RuntimeError, match="single_pass"):
        bt.trades(p)


@pytest.mark.parametrize("window", ["is", "oos"])
def test_single_pass_filters_window_like_cached_run(window):
    cached, p = _make(release_raw=False)
    expected = cached.trades(p, window=window)
    bt, p = _make(release_raw=True, single_pass=True)
    assert bt.trades(p, window=window) == expected


def test_single_pass_passes_recorder_like_cached_run():
    cached, p = _make(release_raw=False)
    rec_cached = LedgerRecorder()
    expected = cached.trades(p, recorder=rec_cached)
    bt, p = _make(release_raw=True, single_pass=True)
    rec = LedgerRecorder()
    assert bt.trades(p, recorder=rec) == expected
    assert sorted(rec._buffers) == sorted(rec_cached._buffers)
    assert rec._buffers


def test_single_pass_requires_release_raw():
    with pytest.raises(ValueError, match="release_raw"):
        _make(release_raw=False, single_pass=True)
