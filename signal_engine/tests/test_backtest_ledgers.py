"""Lineage ledgers: recorder invariants, reason-code reachability, reconciliation, overhead."""

from __future__ import annotations

import dataclasses
import gc
import time

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import ledgers
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
from signal_engine.strategy_core.exits import ExitPlan

IST = "Asia/Kolkata"
ID_FIELDS = ("signal_id", "order_id", "entry_fill_id", "exit_fill_id", "position_id")
FLAT = (100.0, 100.5, 99.5, 100.0)


# ---- fixtures -----------------------------------------------------------------------


def make_frame(sessions, start="09:15", freq="5min"):
    """sessions: list of (day, rows); rows are (open, high, low, close)."""
    parts = []
    for day, rows in sessions:
        idx = pd.date_range(f"{day} {start}", periods=len(rows), freq=freq, tz=IST)
        parts.append(pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx))
    df = pd.concat(parts)
    df["Volume"] = 1000.0
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df


def one_session(rows=None, n=12, day="2026-06-04"):
    return make_frame([(day, rows if rows is not None else [FLAT] * n)])


class Script(Strategy):
    """Fires the given {bar: (direction, sl, tp)} and nothing else. Optional ladder targets."""

    name = "script"
    tag = "S"

    def __init__(self, plan, targets=None):
        self.plan, self.targets = plan, targets

    def prepare(self, df, p):
        return df

    def entry(self, c, i, p, direction):
        spec = self.plan.get(i)
        if spec is None or spec[0] != direction:
            return None
        meta = {"targets": self.targets} if self.targets else {}
        return EntrySignal(direction, spec[1], spec[2], "S", meta=meta)


class SwingScript:
    name = "swing-script"
    tag = "W"

    def __init__(self, plan):
        self.plan = plan

    def prepare(self, df, p):
        return df

    def prepare_for(self, symbol, df, p):
        return df

    def prepare_key(self, p):
        return ("k",)

    def reset_symbol(self, p): ...
    def on_bar(self, c, i, p): ...
    def on_entry(self, c, i, p, sig): ...

    def trail(self, c, i, p, pos):
        return None

    def custom_exit(self, c, i, p, pos):
        return None

    def entry_at_open(self, c, i, p, direction):
        spec = self.plan.get(i)
        if spec is None or spec[0] != direction:
            return None
        return EntrySignal(direction, spec[1], spec[2], "W")


RUN = RunConfig(
    skip_open_minutes=0,
    entry_cutoff_min=15 * 60,
    time_exit_min=15 * 60,
    max_trades_per_day=9,
    min_sl_pct=0.0,
)


def go(df, strat, run=RUN, record=True, sym="TEST"):
    rec = LedgerRecorder() if record else None
    trades = simulate(Ctx(df, sym), strat, None, run, rec=rec)
    return trades, rec


def codes(frames, scope=None):
    ex = frames["exclusions"]
    if scope:
        ex = ex[ex["scope"] == scope]
    return {k: int(v) for k, v in ex.groupby("reason_code")["count"].sum().items()}


def strip(trades):
    return [{k: v for k, v in dataclasses.asdict(t).items() if k not in ID_FIELDS} for t in trades]


# ---- reason codes and ids -----------------------------------------------------------


class TestCodesAndIds:
    def test_taxonomy_complete_and_unique(self):
        assert len(ledgers.LIVE_CODES) == 12
        # 12 live + 6 reserved + 1 opt-in (ENTRY_SKIPPED_BEYOND_LEVEL, skip-at-fill rule)
        assert ledgers.OPT_IN_CODES == ("ENTRY_SKIPPED_BEYOND_LEVEL",)
        assert len(ledgers.REASON_CODES) == 19
        assert len(set(ledgers.REASON_CODES)) == 19
        for code in (
            "POSITION_CAP",
            "SYMBOL_MIN_SESSIONS",
            "SPLIT_DAY",
            "BAD_BAR",
            "NOT_ELIGIBLE",
            "WARMUP",
        ):
            assert ledgers.is_known(code)
        assert not ledgers.is_known("MADE_UP")

    def test_unknown_code_raises(self):
        rec = LedgerRecorder()
        with pytest.raises(ValueError, match="unknown exclusion reason code"):
            rec.add_exclusion("X", "bar", "MADE_UP")
        with pytest.raises(ValueError):
            ledgers.code_index("MADE_UP")
        with pytest.raises(ValueError, match="scope"):
            rec.add_exclusion("X", "galaxy", ledgers.WARMUP)

    def test_reserved_codes_land_in_frames(self):
        rec = LedgerRecorder()
        rec.add_exclusion("AAA", "symbol", ledgers.SYMBOL_MIN_SESSIONS, detail="3 sessions")
        ex = rec.to_frames()["exclusions"]
        assert list(ex["reason_code"]) == ["SYMBOL_MIN_SESSIONS"]
        assert list(ex["scope"]) == ["symbol"]

    def test_id_builders(self):
        ts = pd.Timestamp("2026-06-04 09:20", tz=IST)
        sid = ledgers.signal_id("SBIN", ts, 1)
        assert sid == "SBIN|2026-06-04T09:20:00+05:30|1"
        assert (
            ledgers.signal_id("SBIN", ts.tz_convert("UTC"), -1)
            == "SBIN|2026-06-04T09:20:00+05:30|-1"
        )
        oid = ledgers.order_id(sid)
        assert oid == "O|" + sid
        assert ledgers.fill_id(oid, "entry", 1) == f"F|{oid}|entry1"
        assert ledgers.position_id(f"F|{oid}|entry1") == f"P|F|{oid}|entry1"

    def test_recorder_serves_one_run(self):
        rec = LedgerRecorder()
        c = Ctx(one_session(), "T")
        rec.begin(c)
        with pytest.raises(ValueError, match="already recorded"):
            rec.begin(c)


# ---- equivalence with and without a recorder ----------------------------------------


def random_sessions(seed, n_sess=6, n_bars=75, start_day=1):
    rng = np.random.default_rng(seed)
    out, px = [], 100.0
    for s in range(n_sess):
        rows = []
        for _ in range(n_bars):
            o = px * (1 + rng.normal(0, 0.0015))
            cl = o * (1 + rng.normal(0, 0.003))
            rows.append(
                (
                    o,
                    max(o, cl) * (1 + abs(rng.normal(0, 0.002))),
                    min(o, cl) * (1 - abs(rng.normal(0, 0.002))),
                    cl,
                )
            )
            px = cl
        out.append(
            (str((pd.Timestamp("2026-06-01") + pd.Timedelta(days=start_day - 1 + s)).date()), rows)
        )
    return make_frame(out)


class Momo(Strategy):
    name = "momo"
    tag = "M"

    def prepare(self, df, p):
        return df

    def entry(self, c, i, p, direction):
        if i < 2:
            return None
        cl, prev = float(c["Close"][i]), float(c["Close"][i - 1])
        if direction == 1 and cl > prev * 1.0015:
            return EntrySignal(
                1,
                cl * 0.996,
                cl * 1.008,
                "M",
                meta={"targets": (cl * 1.004, cl * 1.008, cl * 1.012)},
            )
        if direction == -1 and cl < prev * 0.9985:
            return EntrySignal(
                -1,
                cl * 1.004,
                cl * 0.992,
                "M",
                meta={"targets": (cl * 0.996, cl * 0.992, cl * 0.988)},
            )
        return None


RUN_VARIANTS = [
    RUN,
    RUN.with_(one_trade_per_direction=False, max_trades_per_day=4, skip_open_minutes=15),
    RUN.with_(allow_shorts=False, entry_cutoff_min=11 * 60),
    RUN.with_(max_trades_per_day=1, min_sl_pct=0.003),
    RUN.with_(
        exit_plan=ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven"),
        one_trade_per_direction=False,
    ),
    RUN.with_(
        exit_plan=ExitPlan(fractions=(0.4, 0.3, 0.3), stop_rule="tp_buffer"),
        time_exit_min=14 * 60 + 45,
    ),
]


class TestEquivalence:
    @pytest.mark.parametrize("k", range(len(RUN_VARIANTS)))
    def test_intraday_trades_identical_with_recorder(self, k):
        df = random_sessions(seed=k)
        plain, _ = go(df, Momo(), RUN_VARIANTS[k], record=False)
        rec_trades, rec = go(df, Momo(), RUN_VARIANTS[k])
        assert len(plain) > 3
        assert strip(plain) == strip(rec_trades)
        assert all(t.signal_id is None and t.position_id is None for t in plain)
        assert all(
            t.signal_id and t.order_id and t.entry_fill_id and t.exit_fill_id and t.position_id
            for t in rec_trades
        )
        assert rec.reconcile() == []

    def test_existing_fixture_strategies(self):
        from signal_engine.tests.test_backtest_engine import FireOnce, bars

        df = bars(
            [(100, 101, 99, 100), (100, 101, 99, 100), (105, 106, 104, 105), (105, 106, 104, 105)]
        )
        a = simulate(Ctx(df, "T"), FireOnce(at=1, sl=90, tp=200), None, RUN)
        rec = LedgerRecorder()
        b = simulate(Ctx(df, "T"), FireOnce(at=1, sl=90, tp=200), None, RUN, rec=rec)
        assert strip(a) == strip(b)
        assert rec.reconcile() == []

    def test_swing_trades_identical_with_recorder(self):
        df = random_sessions(seed=3, n_sess=40, n_bars=1)
        plan = {i: (1, float(df["Close"].iloc[i - 1]) * 0.97, 1e9) for i in range(2, 38, 5)}
        cfg = SwingRunConfig(max_hold_bars=7)
        plain = simulate_swing(Ctx(df, "T"), SwingScript(plan), None, cfg)
        rec = LedgerRecorder()
        got = simulate_swing(Ctx(df, "T"), SwingScript(plan), None, cfg, rec=rec)
        assert len(plain) >= 3
        assert strip(plain) == strip(got)
        assert all(t.position_id is None for t in plain)
        assert rec.reconcile() == []


# ---- reachability of every reason code ----------------------------------------------


class TestReasonCodesIntraday:
    def test_no_next_bar_per_session(self):
        df = make_frame([("2026-06-04", [FLAT] * 6), ("2026-06-05", [FLAT] * 6)])
        _, rec = go(df, Script({}))
        ex = rec.to_frames()["exclusions"]
        assert list(ex["reason_code"]) == ["NO_NEXT_BAR"] * 2
        assert list(ex["scope"]) == ["bar"] * 2
        assert list(ex["count"]) == [1, 1]
        assert [str(d) for d in ex["date"]] == ["2026-06-04", "2026-06-05"]
        assert rec.reconcile() == []

    def test_max_trades_day(self):
        rows = [FLAT, FLAT, (100, 101.5, 99.5, 100)] + [FLAT] * 9  # TP on the fill bar
        trades, rec = go(
            one_session(rows), Script({1: (1, 99.0, 101.0)}), RUN.with_(max_trades_per_day=1)
        )
        assert [t.reason for t in trades] == ["TP"]
        f = rec.to_frames()
        assert codes(f, "bar") == {"MAX_TRADES_DAY": 9, "NO_NEXT_BAR": 1}  # bars 2..10
        assert rec.reconcile() == []

    def test_open_window(self):
        _, rec = go(one_session(), Script({}), RUN.with_(skip_open_minutes=15))
        assert codes(rec.to_frames(), "bar") == {"OPEN_WINDOW": 3, "NO_NEXT_BAR": 1}

    def test_entry_cutoff(self):
        _, rec = go(one_session(), Script({}), RUN.with_(entry_cutoff_min=9 * 60 + 45))
        assert codes(rec.to_frames(), "bar") == {"ENTRY_CUTOFF": 5, "NO_NEXT_BAR": 1}

    def test_direction_disabled(self):
        _, rec = go(one_session(), Script({}), RUN.with_(allow_shorts=False))
        assert codes(rec.to_frames(), "bar") == {"DIRECTION_DISABLED": 11, "NO_NEXT_BAR": 1}

    def test_direction_already_taken(self):
        rows = [FLAT, FLAT, (100, 101.5, 99.5, 100)] + [FLAT] * 9
        trades, rec = go(one_session(rows), Script({1: (1, 99.0, 101.0)}))
        assert len(trades) == 1
        assert codes(rec.to_frames(), "bar") == {"DIRECTION_ALREADY_TAKEN": 9, "NO_NEXT_BAR": 1}

    def test_position_open(self):
        trades, rec = go(one_session(), Script({1: (1, 99.0, 150.0)}))
        assert trades[0].reason == "EOD"
        assert codes(rec.to_frames(), "bar") == {"POSITION_OPEN": 9, "NO_NEXT_BAR": 1}

    @pytest.mark.parametrize(
        "sl,run_kw,code",
        [
            (100.0, {}, "RISK_INVALID"),
            (101.0, {}, "STOP_WRONG_SIDE"),
            (99.9, {"min_sl_pct": 0.01}, "STOP_TOO_TIGHT"),
        ],
    )
    def test_signal_rejections(self, sl, run_kw, code):
        trades, rec = go(one_session(), Script({1: (1, sl, 120.0)}), RUN.with_(**run_kw))
        assert trades == []
        f = rec.to_frames()
        assert codes(f, "signal") == {code: 1}
        sig = f["signals"]
        assert len(sig) == 1 and not bool(sig["accepted"].iloc[0])
        assert sig["exclusion_id"].iloc[0] == "X|" + sig["signal_id"].iloc[0]
        assert f["orders"].empty
        assert rec.reconcile() == []

    def test_order_dropped_at_session_end(self):
        df = one_session()
        df.loc[df.index[3], "new_session"] = True  # a session boundary inside the day
        trades, rec = go(df, Script({2: (1, 99.0, 120.0)}))
        assert trades == []
        f = rec.to_frames()
        assert codes(f, "order") == {"ORDER_DROPPED_SESSION_END": 1}
        o = f["orders"].iloc[0]
        assert o["status"] == "DROPPED" and pd.isna(o["fill_bar_time"])
        assert o["exclusion_id"] == "X|" + o["order_id"]
        assert bool(f["signals"]["accepted"].iloc[0])
        assert f["fills"].empty
        assert rec.reconcile() == []

    def test_order_dropped_position_open_is_recordable(self):
        """The engine cannot reach this today (a position is flat whenever an order is armed
        and nothing opens one before the fill), so the branch is defensive. The recorder
        side is pinned here directly."""
        rec = LedgerRecorder()
        b = rec.begin(Ctx(one_session(), "T"))
        b.signal(2, 1, "S", 100.0, 99.0, 120.0, 1.0)
        b.order(2, 1, -1, ledgers.CODE_INDEX[ledgers.ORDER_DROPPED_POSITION_OPEN])
        f = rec.to_frames()
        assert codes(f, "order") == {"ORDER_DROPPED_POSITION_OPEN": 1}
        assert rec.reconcile() == []

    def test_all_live_codes_covered(self):
        covered = {
            "MAX_TRADES_DAY",
            "OPEN_WINDOW",
            "ENTRY_CUTOFF",
            "DIRECTION_DISABLED",
            "DIRECTION_ALREADY_TAKEN",
            "RISK_INVALID",
            "STOP_WRONG_SIDE",
            "STOP_TOO_TIGHT",
            "POSITION_OPEN",
            "NO_NEXT_BAR",
            "ORDER_DROPPED_SESSION_END",
            "ORDER_DROPPED_POSITION_OPEN",
        }
        assert covered == set(ledgers.LIVE_CODES)

    def test_per_day_counts_equal_independent_count(self):
        df = random_sessions(seed=9, n_sess=5)
        run = RUN.with_(skip_open_minutes=15, max_trades_per_day=1)
        trades, rec = go(df, Momo(), run)
        ex = rec.to_frames()["exclusions"]
        ex = ex[ex["scope"] == "bar"]
        # independent: bars before the open window end, and one last bar per session
        days = df["day"].to_numpy()
        for day in sorted(set(days)):
            rows = ex[(ex["date"] == day)]
            got = dict(zip(rows["reason_code"], rows["count"], strict=True))
            assert got["NO_NEXT_BAR"] == 1
            assert got["OPEN_WINDOW"] == 3
        total = int(ex["count"].sum())
        assert total == rec._buffers["TEST"].n_skipped
        assert rec.reconcile() == []


class TestReasonCodesSwing:
    def frame(self, n=12):
        return make_frame(
            [
                (
                    str((pd.Timestamp("2026-06-01") + pd.Timedelta(days=d - 1)).date()),
                    [(100.0, 101.0, 99.0, 100.0)],
                )
                for d in range(1, n + 1)
            ]
        )

    def run(self, plan, **kw):
        rec = LedgerRecorder()
        cfg = SwingRunConfig(**kw)
        t = simulate_swing(Ctx(self.frame(), "SW"), SwingScript(plan), None, cfg, rec=rec)
        return t, rec

    @pytest.mark.parametrize(
        "sl,code", [(100.0, "RISK_INVALID"), (101.0, "STOP_WRONG_SIDE"), (99.9, "STOP_TOO_TIGHT")]
    )
    def test_signal_rejections(self, sl, code):
        t, rec = self.run({2: (1, sl, 1e9)})
        assert t == []
        f = rec.to_frames()
        assert codes(f, "signal") == {code: 1}
        assert rec.reconcile() == []

    def test_direction_disabled_and_position_open(self):
        t, rec = self.run({2: (1, 90.0, 1e9)}, max_hold_bars=3)
        assert len(t) == 1
        # position held bars 2..5 (entry bar plus 3), then closed by MAX_HOLD on bar 5
        f = rec.to_frames()
        got = codes(f, "bar")
        flat_bars = 12 - 4
        assert got["DIRECTION_DISABLED"] == flat_bars
        assert got["POSITION_OPEN"] == 3  # bars 3..5, the entry bar is not a skip
        assert rec.reconcile() == []

    def test_three_rows_one_next_open(self):
        t, rec = self.run({2: (1, 90.0, 1e9)}, max_hold_bars=2)
        f = rec.to_frames()
        s, o = f["signals"].iloc[0], f["orders"].iloc[0]
        entry = f["fills"][f["fills"]["kind"] == "entry"].iloc[0]
        assert s["bar_time"] == o["submit_time"] == o["fill_bar_time"] == entry["fill_time"]
        assert entry["fill_price"] == 100.0  # the open, no slippage
        assert t[0].signal_id == s["signal_id"] and t[0].order_id == o["order_id"]
        assert t[0].entry_fill_id == entry["fill_id"]


# ---- lineage walk, ladder, determinism ----------------------------------------------


class TestLineage:
    def test_walk_from_trade_ids(self):
        df = one_session([FLAT, FLAT, (105, 106, 104, 105), (105, 112, 104, 111)] + [FLAT] * 4)
        trades, rec = go(df, Script({1: (1, 90.0, 110.0)}))
        t = trades[0]
        f = rec.to_frames()
        sig = f["signals"].set_index("signal_id").loc[t.signal_id]
        order = f["orders"].set_index("order_id").loc[t.order_id]
        fills = f["fills"].set_index("fill_id")
        entry, exit_ = fills.loc[t.entry_fill_id], fills.loc[t.exit_fill_id]
        assert order["signal_id"] == t.signal_id
        assert entry["position_id"] == exit_["position_id"] == t.position_id
        assert entry["order_id"] == order.name == t.order_id
        assert sig["bar_time"] < entry["fill_time"]
        assert order["submit_time"] == sig["bar_time"]
        assert order["fill_bar_time"] == entry["fill_time"] == t.entry_time
        assert entry["fill_price"] == 105.0 == df["Open"].iloc[2]  # next bar open
        assert sig["signal_price"] == 100.0
        assert exit_["fill_time"] >= entry["fill_time"]
        assert exit_["exit_reason"] == t.reason == "TP"
        assert exit_["fill_price"] == 110.0
        assert (entry["side"], exit_["side"]) == ("BUY", "SELL")
        assert order["status"] == "FILLED" and order["order_type"] == "MARKET"
        for col in ("bar_time",):
            assert str(f["signals"][col].dt.tz) == IST
        assert str(f["fills"]["fill_time"].dt.tz) == IST

    def test_short_sides(self):
        df = one_session([FLAT, FLAT, (95, 96, 94, 95), (95, 96, 80, 85)] + [FLAT] * 4)
        trades, rec = go(df, Script({1: (-1, 110.0, 90.0)}))
        fills = rec.to_frames()["fills"]
        assert list(fills["side"]) == ["SELL", "BUY"]
        assert trades[0].direction == -1

    def test_entered_through_stop_still_has_both_fills(self):
        df = one_session([FLAT, FLAT, (98, 99, 97, 98)] + [FLAT] * 5)
        trades, rec = go(df, Script({1: (1, 99.0, 120.0)}))
        assert trades[0].reason == "SL_GAP_ENTRY"
        f = rec.to_frames()
        assert list(f["fills"]["kind"]) == ["entry", "exit"]
        assert rec.reconcile() == []

    def test_ladder_exits_have_legs(self):
        rows = [FLAT, FLAT, (100, 101.5, 99.5, 100), (100, 102.5, 99.5, 102)] + [FLAT] * 4
        plan = ExitPlan(fractions=(0.5, 0.5))
        run = RUN.with_(exit_plan=plan)
        trades, rec = go(
            one_session(rows), Script({1: (1, 99.0, 101.0)}, targets=(101.0, 102.0, 103.0)), run
        )
        t = trades[0]
        assert t.legs == 2
        f = rec.to_frames()["fills"]
        ex = f[f["kind"] == "exit"].reset_index(drop=True)
        assert list(ex["leg"]) == [1, 2]
        assert list(ex["exit_reason"]) == ["TP1", "TP2"]
        assert list(ex["fill_price"]) == [101.0, 102.0]
        assert ex["fill_time"].iloc[0] < ex["fill_time"].iloc[1]  # legs filled on their bars
        assert t.exit_fill_id == ex["fill_id"].iloc[1]
        assert len(set(f["position_id"])) == 1
        assert rec.reconcile() == []

    def test_ladder_time_exit_leg(self):
        rows = [FLAT, FLAT, (100, 101.5, 99.5, 100)] + [FLAT] * 9
        run = RUN.with_(exit_plan=ExitPlan(fractions=(0.5, 0.5)))
        trades, rec = go(
            one_session(rows), Script({1: (1, 99.0, 101.0)}, targets=(101.0, 130.0)), run
        )
        assert trades[0].legs == 2
        f = rec.to_frames()["fills"]
        assert list(f[f["kind"] == "exit"]["exit_reason"]) == ["TP1", "EOD"]
        assert rec.reconcile() == []


class TestDeterminism:
    def frames(self, order):
        dfs = {s: random_sessions(seed=i, n_sess=3) for i, s in enumerate("ABC")}
        rec = LedgerRecorder()
        bt = Backtest(Momo(), {s: dfs[s] for s in order}, RUN)
        trades = bt.trades(None, "all", recorder=rec)
        return trades, rec.to_frames(), rec

    def test_same_frames_across_runs_and_insertion_order(self):
        _, a, rec = self.frames("ABC")
        _, b, _ = self.frames("ABC")
        _, c, _ = self.frames("CAB")
        for k in a:
            pd.testing.assert_frame_equal(a[k], b[k])
            pd.testing.assert_frame_equal(a[k], c[k])
        syms = list(a["signals"]["symbol"])
        assert syms == sorted(syms)
        assert rec.reconcile() == []

    def test_exact_columns_and_unique_ids(self):
        _, f, _ = self.frames("ABC")
        assert list(f["signals"].columns) == ledgers.SIGNAL_COLS
        assert list(f["orders"].columns) == ledgers.ORDER_COLS
        assert list(f["fills"].columns) == ledgers.FILL_COLS
        assert list(f["exclusions"].columns) == ledgers.EXCLUSION_COLS
        assert f["signals"]["signal_id"].is_unique
        assert f["fills"]["fill_id"].is_unique
        assert f["exclusions"]["exclusion_id"].is_unique

    def test_empty_run_has_typed_empty_frames(self):
        rec = LedgerRecorder()
        f = rec.to_frames()
        assert list(f["fills"].columns) == ledgers.FILL_COLS
        assert f["signals"].empty
        assert rec.reconcile() == []


# ---- reconciliation catches damage ---------------------------------------------------


class TestReconcile:
    def setup_method(self):
        df = random_sessions(seed=5, n_sess=4)
        self.trades, self.rec = go(df, Momo())
        self.f = self.rec.to_frames()

    def test_clean(self):
        assert len(self.trades) > 3
        assert self.rec.reconcile() == []
        assert self.rec.reconcile(self.f, trades=self.trades) == []

    def test_deleted_exit_fill(self):
        fills = self.f["fills"]
        drop = fills[fills["kind"] == "exit"].index[0]
        bad = {**self.f, "fills": fills.drop(index=drop)}
        assert self.rec.reconcile(bad)

    def test_deleted_entry_fill(self):
        fills = self.f["fills"]
        drop = fills[fills["kind"] == "entry"].index[0]
        bad = {**self.f, "fills": fills.drop(index=drop)}
        assert self.rec.reconcile(bad)

    def test_deleted_signal_row(self):
        sig = self.f["signals"]
        bad = {**self.f, "signals": sig.drop(index=sig.index[0])}
        assert self.rec.reconcile(bad)

    def test_deleted_rejected_signal_exclusion(self):
        df = one_session()
        _, rec = go(df, Script({1: (1, 101.0, 120.0)}))
        f = rec.to_frames()
        bad = {**f, "exclusions": f["exclusions"][f["exclusions"]["scope"] != "signal"]}
        assert any("rejected without an exclusion" in p for p in rec.reconcile(bad))

    def test_deleted_bar_aggregate_breaks_counts(self):
        ex = self.f["exclusions"]
        bad = {**self.f, "exclusions": ex.drop(index=ex[ex["scope"] == "bar"].index[0])}
        assert any("bar exclusions" in p for p in rec_problems(self.rec, bad))

    def test_duplicate_ids_reported(self):
        sig = self.f["signals"]
        bad = {**self.f, "signals": pd.concat([sig, sig.iloc[[0]]], ignore_index=True)}
        assert any("duplicate" in p for p in self.rec.reconcile(bad))

    def test_trade_without_recorded_position(self):
        t = dataclasses.replace(self.trades[0], position_id="P|nope")
        assert self.rec.reconcile(self.f, trades=[t] + self.trades[1:])


def rec_problems(rec, frames):
    return rec.reconcile(frames)


# ---- harness wiring ------------------------------------------------------------------


class TestHarness:
    def frames(self):
        return {"A": random_sessions(1, 4), "B": random_sessions(2, 4)}

    def test_recorder_passed_only_when_given(self):
        seen = []

        def engine(c, strategy, p, run, **kw):
            seen.append(kw)
            return simulate(c, strategy, p, run, **kw)

        bt = Backtest(Momo(), self.frames(), RUN, engine=engine)
        bt.trades(None, "all")
        assert seen == [{}, {}]
        rec = LedgerRecorder()
        bt2 = Backtest(Momo(), self.frames(), RUN, engine=engine)
        t = bt2.trades(None, "all", recorder=rec)
        assert seen[2:] == [{"rec": rec}, {"rec": rec}]
        assert all(x.position_id for x in t)

    def test_engine_without_rec_param_keeps_working(self):
        def legacy(c, strategy, p, run):
            return simulate(c, strategy, p, run)

        bt = Backtest(Momo(), self.frames(), RUN, engine=legacy)
        assert bt.trades(None, "all")

    def test_recorder_never_reaches_sweeps(self):
        seen = []

        def engine(c, strategy, p, run, rec=None):
            seen.append(rec)
            return simulate(c, strategy, p, run, rec=rec)

        bt = Backtest(Momo(), self.frames(), RUN, engine=engine)
        bt.confirm(None)
        bt.cost_sensitivity(None)
        bt.by_symbol(None)
        assert seen and all(r is None for r in seen)

    def test_windowed_run_still_reconciles_against_engine_trades(self):
        rec = LedgerRecorder()
        bt = Backtest(Momo(), self.frames(), RUN)
        bt.trades(None, "all", recorder=rec)
        assert rec.reconcile() == []

    def test_recorder_unused_when_none(self, monkeypatch):
        calls = []
        orig = LedgerRecorder.begin
        monkeypatch.setattr(
            LedgerRecorder, "begin", lambda self, c: calls.append(1) or orig(self, c)
        )
        t, _ = go(random_sessions(1, 3), Momo(), record=False)
        Backtest(Momo(), self.frames(), RUN).trades(None, "all")
        assert t and calls == []
        assert all(x.signal_id is None for x in t)


# ---- overhead ------------------------------------------------------------------------


class Bench(Strategy):
    """Entry with the per-bar cost of a real adapter (a rolling mean and a range read on numpy
    slices, about 5 microseconds), so the recorder is measured against a realistic loop and not
    against a strategy that does nothing."""

    name = "bench"
    tag = "B"

    def prepare(self, df, p):
        return df

    def entry(self, c, i, p, direction):
        if i < 12:
            return None
        cl = c["Close"]
        x, y = float(cl[i]), float(cl[i - 1])
        mean = float(cl[i - 10 : i].mean())
        rng = float(c["High"][i - 10 : i].max() - c["Low"][i - 10 : i].min())
        vol = float(cl[i - 12 : i].std())
        if rng <= 0 or vol < 0:
            return None
        if direction == 1 and x > y * 1.002 and x > mean:
            return EntrySignal(1, x * 0.996, x * 1.008, "B")
        if direction == -1 and x < y * 0.998 and x < mean:
            return EntrySignal(-1, x * 1.004, x * 0.992, "B")
        return None


def best_of(fn, n=3):
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


class TestOverhead:
    def test_recorder_overhead_and_none_path(self):
        run = RUN.with_(skip_open_minutes=15, entry_cutoff_min=11 * 60, max_trades_per_day=2)
        ctxs = [Ctx(random_sessions(seed=s, n_sess=30, n_bars=75), f"S{s}") for s in range(40)]
        strat = Bench()

        def plain():
            for c in ctxs:
                simulate(c, strat, None, run)

        def none():
            for c in ctxs:
                simulate(c, strat, None, run, rec=None)

        def recorded():
            rec = LedgerRecorder()
            for c in ctxs:
                simulate(c, strat, None, run, rec=rec)

        plain()  # warm up
        gc.disable()
        t_plain, t_none, t_rec = [], [], []
        for _ in range(12):  # interleaved, best of 12 each (3 minimum), gc off for stability
            t_plain.append(best_of(plain, 1))
            t_none.append(best_of(none, 1))
            t_rec.append(best_of(recorded, 1))
        gc.enable()
        ratio = min(t_rec) / min(t_plain)
        print(f"recorder overhead ratio: {ratio:.3f} (plain {min(t_plain):.3f}s)")
        # Target is about 1.2x; measured 1.08 to 1.28 on a noisy host (2.1x before the id building
        # moved out of the loop). 1.5x catches a real regression without flaking on timer noise.
        assert ratio < 1.5
        assert min(t_none) / min(t_plain) < 1.25  # same code path: noise bound only


# ---- review fixes ---------------------------------------------------------------------


def flat_panel(n_days, n_bars=75):
    """Many sessions of flat bars, built in bulk (no strategy activity)."""
    days = pd.bdate_range("2010-01-04", periods=n_days)
    mins = pd.timedelta_range("09:15:00", periods=n_bars, freq="5min")
    idx = pd.DatetimeIndex((days.values[:, None] + mins.values[None, :]).ravel()).tz_localize(IST)
    df = pd.DataFrame(
        {"Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.0, "Volume": 1000.0}, index=idx
    )
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df


class TestBoundedCounters:
    def test_skip_state_is_per_day_not_per_bar(self):
        import tracemalloc

        df = flat_panel(2000)  # 150,000 bars, every one a skip
        c = Ctx(df, "BIG")
        run = RUN.with_(skip_open_minutes=15)
        tracemalloc.start()
        simulate(c, Script({}), None, run)
        base = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        tracemalloc.start()
        rec = LedgerRecorder()
        simulate(c, Script({}), None, run, rec=rec)
        peak = tracemalloc.get_traced_memory()[1] - base  # what the recorder adds
        tracemalloc.stop()
        b = rec._buffers["BIG"]
        assert peak < 3_000_000  # one int per skipped bar would be ~5 MB+
        assert b.n_skipped == 2000 * 11  # per day: 3 open-window, 7 past the cutoff, 1 last bar
        assert len(b.agg[1]) == 2000 * 3  # (day, code) pairs, not bars
        f = rec.to_frames()["exclusions"]
        assert len(f) == 6000
        assert int(f["count"].sum()) == b.n_skipped
        assert rec.reconcile() == []

    def test_aggregate_rows_match_independent_count(self):
        df = random_sessions(seed=21, n_sess=5)
        _, rec = go(df, Momo(), RUN.with_(skip_open_minutes=15))
        ex = rec.to_frames()["exclusions"]
        ex = ex[ex["scope"] == "bar"]
        # bar_time is the first bar that was skipped for that reason on that day
        row = ex[(ex["reason_code"] == "OPEN_WINDOW")].iloc[0]
        first_of_day = df.index[df["day"] == row["date"]][0]
        assert row["bar_time"] == first_of_day and row["count"] == 3


class TestUnitsInDetail:
    def test_direction_skips_say_per_direction(self):
        _, rec = go(one_session(), Script({}), RUN.with_(allow_shorts=False))
        ex = rec.to_frames()["exclusions"].set_index("reason_code")
        assert ex.loc["DIRECTION_DISABLED", "detail"] == "per direction per bar"
        assert ex.loc["NO_NEXT_BAR", "detail"] == "per bar"


class TestExtraExclusions:
    def test_identical_extras_get_unique_ids(self):
        rec = LedgerRecorder()
        for _ in range(2):
            rec.add_exclusion("AAA", "symbol", ledgers.SPLIT_DAY, date="2026-06-04")
        ids = rec.to_frames()["exclusions"]["exclusion_id"]
        assert ids.is_unique and len(ids) == 2

    def test_extras_do_not_break_reconcile(self):
        df = random_sessions(seed=5, n_sess=4)
        trades, rec = go(df, Momo())
        rec.add_exclusion("TEST", "bar", ledgers.NOT_ELIGIBLE, date="2026-06-02", count=7)
        rec.add_exclusion("TEST", "signal", ledgers.WARMUP)
        rec.add_exclusion("TEST", "order", ledgers.POSITION_CAP)
        f = rec.to_frames()
        assert rec.reconcile(f) == []
        ex = f["exclusions"]
        eng = ex[ex["scope"] == "bar"]
        bad = {**f, "exclusions": ex.drop(index=eng[eng["reason_code"] == "NO_NEXT_BAR"].index[0])}
        assert rec.reconcile(bad)  # a deleted engine row still fails


class TestFillOrder:
    def test_legs_sort_numerically(self):
        rec = LedgerRecorder()
        b = rec.begin(Ctx(one_session(), "T"))
        b.signal(1, 1, "S", 100.0, 99.0, 120.0, 1.0)
        b.order(1, 1, 2)
        b.entry_fill(1, 1, 2, 100.0)
        for leg in (10, 2, 1):
            b.exit_fill(1, 1, 3, 101.0, f"TP{leg}", leg)
        fills = rec.to_frames()["fills"]
        assert list(fills[fills["kind"] == "exit"]["leg"]) == [1, 2, 10]


class TestTimeUnits:
    @pytest.mark.parametrize("unit", ["s", "ms", "us"])
    def test_non_ns_index_ids(self, unit):
        df = one_session([FLAT, FLAT, (105, 106, 104, 105), (105, 112, 104, 111)] + [FLAT] * 4)
        df.index = df.index.as_unit(unit)
        trades, rec = go(df, Script({1: (1, 90.0, 110.0)}))
        assert trades[0].signal_id == "TEST|2026-06-04T09:20:00+05:30|1"
        f = rec.to_frames()
        assert f["signals"]["signal_id"].iloc[0] == trades[0].signal_id
        assert rec.reconcile() == []
