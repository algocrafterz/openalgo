"""Deterministic synthetic daily panel and the snapshot used to guard swing-strategy behaviour."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from signal_engine.backtest import data
from signal_engine.backtest.__main__ import REGISTRY, _register
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.tearsheet import build_run_record

SWING_KEYS = (
    "ibs_meanrev",
    "gap_rsi",
    "liquidity_sweep",
    "phoenix",
    "ema_pullback",
    "value_zone",
)


def synthetic_frames(n_sym: int = 8, n: int = 700, seed: int = 7) -> dict[str, pd.DataFrame]:
    """Random-walk daily bars shaped like the Historify daily loader's output."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-04", periods=n, tz="Asia/Kolkata")
    out = {}
    for k in range(n_sym):
        r = rng.normal(0.0004, 0.018, n)
        close = 100 * np.exp(np.cumsum(r))
        op = close * (1 + rng.normal(0, 0.006, n))
        hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
        lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
        raw = pd.DataFrame(
            {
                "Open": op,
                "High": hi,
                "Low": lo,
                "Close": close,
                "Volume": rng.integers(1_000, 90_000, n).astype(float),
            },
            index=idx,
        )
        out[f"S{k}"] = data._downcast(data._split_adjust_daily(raw))
    return out


def swing_snapshot() -> dict:
    """Trade-list hash, headline metrics and series hash of each shipped swing strategy."""
    _register()
    snap = {}
    for key in SWING_KEYS:
        cls, params_cls, kind = REGISTRY[key]
        assert kind == "swing"
        bt = Backtest(
            cls(),
            synthetic_frames(),
            SwingRunConfig(cost_bps=24.0),
            engine=simulate_swing,
            release_raw=False,
        )
        trades = bt.trades(params_cls())
        rows = [
            (
                t.symbol,
                str(t.day),
                t.direction,
                str(t.entry_time),
                repr(t.entry),
                str(t.exit_time),
                repr(t.exit),
                t.reason,
                repr(t.r_gross),
                repr(t.r_net),
                repr(t.risk),
            )
            for t in trades
        ]
        meta: dict = {
            "trades": len(trades),
            "sha": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
        }
        if trades:
            rec = build_run_record(
                trades,
                run_id="2026-01-01-x-v1",
                run_date="2026-01-01",
                code_version="x",
                data={
                    "source": "s",
                    "as_of": "2026-01-01",
                    "universe": "u",
                    "bar_size": "1d",
                },
                period={"start": "2021-01-04", "end": "2023-09-01"},
                costs={"model": "flat-round-trip-bps", "round_trip_bps": 24.0},
                horizon="swing",
            )
            meta["metrics"] = rec["metrics"]
            meta["series_sha"] = hashlib.sha256(
                json.dumps(rec["series"], sort_keys=True).encode()
            ).hexdigest()
        snap[key] = meta
    return snap
