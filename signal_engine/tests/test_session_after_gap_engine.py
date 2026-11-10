"""Session-coverage safeguard, part 2: SESSION_AFTER_GAP in the engines and the harness funnel.

The first loaded session after a missing official session takes no new entries; the next session
does. The skip is recorded as a signal exclusion with its symbol and date. A frame without the
`session_after_gap` column (hand-built, older paths) behaves exactly as before.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import ledgers
from signal_engine.backtest import session_coverage as sc
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx, EntrySignal
from signal_engine.tests.test_backtest_ledgers import SwingScript
from signal_engine.tests.test_backtest_missing_bar_guard import IST, RUN, frame

DAYS = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
TIMES = ["09:15", "09:20", "09:25", "09:30", "09:35", "09:40", "09:45", "09:50"]
BAR = (100.0, 101.0, 99.0, 100.0)


def multi(days=DAYS, gap_days=()):
    """Several 8-bar sessions with the session columns the loader builds; gap_days get the flag."""
    df = pd.concat([frame([BAR] * len(TIMES), TIMES, day=d) for d in days])
    d = pd.Series(df.index.date, index=df.index)
    df["new_session"] = (d != d.shift(1)).to_numpy()
    df["day"] = d
    df["from_open"] = df["mins"] - d.map(df.groupby(d)["mins"].min())
    flagged = {pd.Timestamp(x).date() for x in gap_days}
    df[sc.AFTER_GAP_COLUMN] = np.array([x in flagged for x in df.index.date], dtype=bool)
    return df


class PerSession(Strategy):
    """Fires a long once per session, on the fourth bar."""

    name = "per-session"

    def __init__(self):
        self.fired = False

    def prepare(self, df, p):
        return df

    def reset_session(self, p):
        self.fired = False

    def entry(self, c, i, p, d):
        if d != 1 or self.fired or c["from_open"][i] != 15:
            return None
        return EntrySignal(direction=1, sl=90.0, tp=130.0, tag="T")

    def on_entry(self, c, i, p, sig):
        self.fired = True


def run(df, rec=None):
    return simulate(Ctx(df, "TEST"), PerSession(), None, RUN, rec)


class TestReasonCodeContract:
    def test_the_code_is_appended_last_and_earlier_indexes_are_unchanged(self):
        assert ledgers.REASON_CODES[-1] == "SESSION_AFTER_GAP"
        assert ledgers.REASON_CODES[-2] == "ENTRY_SKIPPED_MISSING_BAR"
        assert ledgers.CODE_INDEX["WARMUP"] == 17
        assert ledgers.CODE_INDEX["ENTRY_SKIPPED_BEYOND_LEVEL"] == 18
        assert ledgers.CODE_INDEX["ENTRY_SKIPPED_MISSING_BAR"] == 19
        assert ledgers.CODE_INDEX["SESSION_AFTER_GAP"] == 20
        assert len(ledgers.REASON_CODES) == 21


class TestIntradayEntrySkip:
    def test_the_entry_on_the_first_session_after_a_gap_is_skipped_and_the_next_is_taken(self):
        trades = run(multi(gap_days=["2026-06-02"]))
        assert [str(t.day) for t in trades] == ["2026-06-01", "2026-06-03", "2026-06-04"]

    def test_the_ledger_records_the_exclusion_with_symbol_and_date(self):
        rec = LedgerRecorder()
        run(multi(gap_days=["2026-06-02"]), rec)
        frames = rec.to_frames()
        exc = frames["exclusions"]
        hit = exc[exc["reason_code"] == "SESSION_AFTER_GAP"]
        assert len(hit) == 1
        row = hit.iloc[0]
        assert row["scope"] == "signal" and row["symbol"] == "TEST"
        assert str(row["date"]) == "2026-06-02" and int(row["count"]) == 1
        assert int((~frames["signals"]["accepted"]).sum()) == 1
        assert rec.reconcile() == []

    def test_a_gapless_run_is_unchanged_with_or_without_the_column(self):
        with_col = multi()
        without_col = with_col.drop(columns=[sc.AFTER_GAP_COLUMN])
        a, b = run(with_col), run(without_col)
        assert len(a) == len(b) == 4
        assert [(t.day, t.entry, t.exit, t.reason, t.r_net) for t in a] == [
            (t.day, t.entry, t.exit, t.reason, t.r_net) for t in b
        ]

    def test_a_frame_without_the_column_never_skips(self):
        df = multi(gap_days=["2026-06-02"]).drop(columns=[sc.AFTER_GAP_COLUMN])
        assert len(run(df)) == 4

    def test_a_skipped_signal_makes_no_order_and_uses_no_trade_slot(self):
        rec = LedgerRecorder()
        run(multi(gap_days=["2026-06-02", "2026-06-03"]), rec)
        frames = rec.to_frames()
        assert len(frames["orders"]) == 2  # sessions 1 and 4 only
        assert rec.reconcile() == []


class _Drops(PerSession):
    """A strategy whose prepare() rebuilds the frame from OHLCV only, as the real adapters do."""

    def prepare(self, df, p):
        from signal_engine.backtest import indicators

        return indicators.add_session_columns(df[["Open", "High", "Low", "Close", "Volume"]])


class TestHarnessFunnel:
    def test_the_column_survives_a_prepare_that_rebuilds_the_frame(self):
        df = multi(gap_days=["2026-06-02"])
        bt = Backtest(_Drops(), {"TEST": df}, RUN)
        trades = bt.trades(None)
        assert [str(t.day) for t in trades] == ["2026-06-01", "2026-06-03", "2026-06-04"]

    def test_a_frame_without_the_column_through_the_funnel_is_unchanged(self):
        df = multi().drop(columns=[sc.AFTER_GAP_COLUMN])
        assert len(Backtest(_Drops(), {"TEST": df}, RUN).trades(None)) == 4

    def test_every_registered_intraday_adapter_keeps_the_column(self):
        """prepare_for of the real adapters, through the funnel's carry."""
        from signal_engine.backtest import __main__ as cli

        cli._register()
        df = _real_like()
        checked = []
        for name, (cls, params_cls, kind) in sorted(cli.REGISTRY.items()):
            if kind == "swing" or getattr(cls, "prepare_panel", None) is not None:
                continue
            bt = Backtest(cls(), {"TEST": df.copy()}, RUN)
            try:
                ctx = bt._ctx("TEST", bt.frames["TEST"], params_cls())
            except Exception:  # noqa: BLE001 - an adapter that needs other columns is not in scope
                continue
            assert sc.AFTER_GAP_COLUMN in ctx.a, name
            assert int(ctx.a[sc.AFTER_GAP_COLUMN].sum()) == int(df[sc.AFTER_GAP_COLUMN].sum())
            checked.append(name)
        assert checked, "no registered adapter could be prepared on the synthetic frame"


def _real_like():
    """Three sessions of 75 five-minute bars with the loader's frame shape and one flagged day."""
    rng = np.random.default_rng(7)
    parts = []
    for day in ["2026-06-01", "2026-06-02", "2026-06-03"]:
        idx = pd.date_range(f"{day} 09:15", periods=75, freq="5min", tz=IST)
        c = 100 + np.cumsum(rng.normal(0, 0.1, len(idx)))
        parts.append(
            pd.DataFrame(
                {"Open": c, "High": c + 0.2, "Low": c - 0.2, "Close": c, "Volume": 1000.0},
                index=idx,
            )
        )
    df = pd.concat(parts)
    flagged = pd.Timestamp("2026-06-02").date()
    df[sc.AFTER_GAP_COLUMN] = np.array([x == flagged for x in df.index.date])
    return df


def swing_frame(n=8):
    idx = pd.bdate_range("2026-01-05", periods=n, tz="Asia/Kolkata")
    o = np.full(n, 100.0)
    df = pd.DataFrame(
        {"Open": o, "High": o * 1.001, "Low": o * 0.999, "Close": o, "Volume": 1000.0}, index=idx
    )
    df["day"] = pd.Series(df.index.date, index=df.index)
    return df


class TestSwingEntrySkip:
    def test_the_entry_on_a_bar_after_a_gap_is_skipped_when_the_column_is_present(self):
        df = swing_frame()
        flag = np.zeros(len(df), dtype=bool)
        flag[2] = True
        df[sc.AFTER_GAP_COLUMN] = flag
        strat = SwingScript({2: (1, 90.0, math.inf), 4: (1, 90.0, math.inf)})
        rec = LedgerRecorder()
        trades = simulate_swing(Ctx(df, "A"), strat, None, SwingRunConfig(), rec)
        assert [pd.Timestamp(t.entry_time) for t in trades] == [df.index[4]]
        exc = rec.to_frames()["exclusions"]
        assert (exc["reason_code"] == "SESSION_AFTER_GAP").sum() == 1
        assert rec.reconcile() == []

    def test_without_the_column_the_swing_engine_is_unchanged(self):
        df = swing_frame()
        strat = SwingScript({2: (1, 90.0, math.inf)})
        (t,) = simulate_swing(Ctx(df, "A"), strat, None, SwingRunConfig())
        assert pd.Timestamp(t.entry_time) == df.index[2]
