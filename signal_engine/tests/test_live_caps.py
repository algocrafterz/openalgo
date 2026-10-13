"""G5.2 live caps: entry limits applied while the strategy runs, on synthetic bars.

Every scenario builds just enough bars to force one decision, so a failure names the broken rule.
Prices are flat at 100 with a 10-point stop and target, so a trade is exactly -1R or +1R.
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pandas as pd
import pytest

from signal_engine.backtest import live_caps as lc
from signal_engine.backtest.engine import EntryRequest, simulate, simulate_steps
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig

IST = "Asia/Kolkata"
RUN = RunConfig(
    skip_open_minutes=0,
    entry_cutoff_min=15 * 60,
    time_exit_min=15 * 60,
    max_trades_per_day=9,
    min_sl_pct=0.0,
    cost_bps=0.0,
)
F = 0.1  # a 1R loss costs 10% of day-start equity, so the limits below trip fast


def session(date: str, outcomes: dict[int, str] | None = None, n: int = 12, price: float = 100.0):
    """One session of 5-minute bars from 09:15. outcomes maps a bar index to "SL" or "TP":
    that bar spans the stop (price-10) or the target (price+10) of a trade entered earlier."""
    rows = []
    for k in range(n):
        o = h = low = c = price
        hit = (outcomes or {}).get(k)
        if hit == "SL":
            low = price - 15
        elif hit == "TP":
            h = price + 15
        rows.append((o, h, low, c))
    idx = pd.date_range(f"{date} 09:15", periods=n, freq="5min", tz=IST)
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000.0
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df


def join(*frames: pd.DataFrame) -> pd.DataFrame:
    df = pd.concat(frames)
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["new_session"] = (day != day.shift(1)).to_numpy()
    return df


class Fire(Strategy):
    """A long with a 10-point stop and target on every bar listed in `at`."""

    name = "fire"

    def __init__(self, at, sl_off=10.0, tp_off=10.0, with_stop=True):
        self.at, self.sl_off, self.tp_off, self.with_stop = set(at), sl_off, tp_off, with_stop
        self.entries = 0
        self.seen: list[int] = []

    def prepare(self, df, p):
        return df

    def reset_symbol(self, p):
        self.entries = 0

    def entry(self, c, i, p, direction):
        if direction != 1 or i not in self.at:
            return None
        self.seen.append(i)
        px = float(c["Close"][i])
        sl = px - self.sl_off if self.with_stop else None
        return EntrySignal(direction=1, sl=sl, tp=px + self.tp_off, tag="T")

    def on_entry(self, c, i, p, sig):
        self.entries += 1


def ctxs(**symbols):
    """symbol -> (strategy, Ctx) from keyword (strategy, frame) pairs."""
    return {s: (strat, Ctx(df, s)) for s, (strat, df) in symbols.items()}


def caps(**kw) -> lc.LiveCaps:
    base = {
        "max_open_positions": 0,
        "max_trades_per_day": 0,
        "daily_loss_limit": 0.0,
        "weekly_loss_limit": 0.0,
        "monthly_loss_limit": 0.0,
        "min_entry_price": 0.0,
        "max_entry_price": 0.0,
        "risk_fraction": F,
    }
    base.update(kw)
    return lc.LiveCaps(**base)


def names(run: lc.CappedRun) -> list[tuple[str, str]]:
    return [(t.symbol, str(pd.Timestamp(t.entry_time).time())[:5]) for t in run.trades]


def drain(gen, answer=True):
    """Answer every request with `answer`; return the final trades."""
    try:
        while True:
            gen.send(answer)
    except StopIteration as done:
        return done.value


class TestEngineGate:
    def test_default_simulate_equals_an_ungated_generator_and_an_accept_all_gate(self):
        df = session("2026-06-01", {5: "TP"})
        plain = simulate(Ctx(df, "A"), Fire({2}), None, RUN)
        steps = simulate_steps(Ctx(df, "A"), Fire({2}), None, RUN)
        with pytest.raises(StopIteration) as done:
            next(steps)
        gated = simulate_steps(Ctx(df, "A"), Fire({2}), None, RUN, gated=True)
        next(gated)
        assert plain == done.value.value == drain(gated) and len(plain) == 1

    def test_the_request_carries_the_signal(self):
        df = session("2026-06-01")
        req = next(simulate_steps(Ctx(df, "A"), Fire({3}), None, RUN, gated=True))
        assert isinstance(req, EntryRequest)
        assert (req.symbol, req.bar, req.direction, req.price) == ("A", 3, 1, 100.0)
        assert req.stop == 90.0 and req.risk == 10.0
        assert req.entry_time == df.index[4] and req.trades == []

    def test_a_veto_opens_no_position_and_skips_on_entry(self):
        strat = Fire({3})
        gen = simulate_steps(Ctx(session("2026-06-01"), "A"), strat, None, RUN, gated=True)
        next(gen)
        assert drain(gen, False) == [] and strat.entries == 0

    def test_after_a_veto_the_symbol_keeps_evaluating_later_bars(self):
        df = session("2026-06-01")
        strat = Fire({3, 5})
        gen = simulate_steps(Ctx(df, "A"), strat, None, RUN, gated=True)
        first = next(gen)
        second = gen.send(False)
        assert (first.bar, second.bar) == (3, 5) and strat.seen == [3, 5]
        trades = drain(gen, True)
        assert len(trades) == 1 and trades[0].entry_time == df.index[6]
        assert strat.entries == 1

    def test_a_gated_run_cannot_record_ledgers(self):
        gen = simulate_steps(Ctx(session("2026-06-01"), "A"), Fire({3}), None, RUN, rec=object(), gated=True)
        with pytest.raises(ValueError):
            next(gen)


class TestSlots:
    @staticmethod
    def _two(b_fires):
        return ctxs(
            A=(Fire({2}), session("2026-06-01", {4: "SL"})),
            B=(Fire(b_fires), session("2026-06-01", {7: "SL"})),
        )

    def test_one_slot_vetoes_a_second_entry_while_the_first_is_open(self):
        run = lc.run_capped(self._two({3}), None, RUN, caps(max_open_positions=1))
        assert names(run) == [("A", "09:30")] and run.vetoes == {"slots": 1}

    def test_the_slot_is_held_through_the_end_of_the_exit_bar(self):
        # A exits in bar 4 (09:35-09:40). B signalling at bar 3 enters at 09:35: still open.
        # B signalling at bar 4 enters at 09:40, the end of A's exit bar: free.
        run = lc.run_capped(self._two({4}), None, RUN, caps(max_open_positions=1))
        assert names(run) == [("A", "09:30"), ("B", "09:40")] and run.vetoes == {}

    def test_a_vetoed_symbol_trades_its_next_signal_once_the_slot_is_free(self):
        run = lc.run_capped(self._two({3, 4}), None, RUN, caps(max_open_positions=1))
        assert names(run) == [("A", "09:30"), ("B", "09:40")] and run.vetoes == {"slots": 1}

    def test_two_slots_take_both(self):
        run = lc.run_capped(self._two({3}), None, RUN, caps(max_open_positions=2))
        assert len(run.trades) == 2 and run.vetoes == {} and run.peak_open == 2

    def test_same_instant_ties_break_by_hash_not_by_name(self):
        both = ctxs(
            AAA=(Fire({2}), session("2026-06-01", {5: "SL"})),
            ZZZ=(Fire({2}), session("2026-06-01", {5: "SL"})),
        )
        run = lc.run_capped(both, None, RUN, caps(max_open_positions=1))
        when = session("2026-06-01").index[3]
        want = min(("AAA", "ZZZ"), key=lambda s: lc._tie(s, when))
        assert [x.symbol for x in run.trades] == [want]

    def test_input_order_does_not_change_the_result(self):
        a = lc.run_capped(self._two({3, 4}), None, RUN, caps(max_open_positions=1))
        rev = dict(reversed(list(self._two({3, 4}).items())))
        b = lc.run_capped(rev, None, RUN, caps(max_open_positions=1))
        assert lc.trade_hash(a.trades) == lc.trade_hash(b.trades)


class TestTradeCountBandAndUnlimited:
    def test_trades_per_day_counts_entries_across_symbols_and_resets_daily(self):
        sym = {
            f"S{k}": (Fire({2}), join(session("2026-06-01", {5: "TP"}), session("2026-06-02", {5: "TP"})))
            for k in range(3)
        }
        sym = {s: (Fire({2, 14}), df) for s, (_f, df) in sym.items()}
        run = lc.run_capped(ctxs(**sym), None, RUN, caps(max_trades_per_day=2))
        per_day = pd.Series([pd.Timestamp(t.entry_time).date() for t in run.trades]).value_counts()
        assert per_day.max() == 2 and len(run.trades) == 4 and run.vetoes == {"trades_day": 2}

    def test_the_price_band_vetoes_and_a_zero_band_is_off(self):
        low, ok = session("2026-06-01", price=50.0), session("2026-06-01", price=400.0)
        c = ctxs(LOW=(Fire({2}, 5, 5), low), OK=(Fire({2}, 5, 5), ok))
        banded = lc.run_capped(c, None, RUN, caps(min_entry_price=300, max_entry_price=5000))
        assert [t.symbol for t in banded.trades] == ["OK"] and banded.vetoes == {"band": 1}
        assert len(lc.run_capped(c, None, RUN, caps()).trades) == 2

    def test_a_band_veto_frees_the_slot_it_never_used(self):
        c = ctxs(
            LOW=(Fire({2}, 5, 5), session("2026-06-01", price=50.0)),
            OK=(Fire({3}, 5, 5), session("2026-06-01", price=400.0)),
        )
        run = lc.run_capped(c, None, RUN, caps(min_entry_price=300, max_entry_price=5000, max_open_positions=1))
        assert [t.symbol for t in run.trades] == ["OK"]

    def test_with_every_limit_off_the_result_is_the_plain_per_symbol_run(self):
        frames = {s: session("2026-06-01", {4: "SL"}) for s in ("A", "B")}
        c = ctxs(A=(Fire({2, 6}), frames["A"]), B=(Fire({2}), frames["B"]))
        plain = simulate(Ctx(frames["A"], "A"), Fire({2, 6}), None, RUN) + simulate(
            Ctx(frames["B"], "B"), Fire({2}), None, RUN
        )
        assert lc.trade_hash(lc.run_capped(c, None, RUN, lc.unlimited(F)).trades) == lc.trade_hash(plain)


class TestLossLimits:
    def test_daily_limit_vetoes_after_enough_realised_losses_and_resets_next_day(self):
        # A and B lose and close by 09:45; C signals at bar 8 (09:55) and is vetoed; D signals
        # on the next session and is allowed
        two_days = join(session("2026-06-01"), session("2026-06-02"))
        c = ctxs(
            A=(Fire({2}), session("2026-06-01", {4: "SL"})),
            B=(Fire({3}), session("2026-06-01", {5: "SL"})),
            C=(Fire({8}), session("2026-06-01")),
            D=(Fire({12 + 2}), two_days),
        )
        run = lc.run_capped(c, None, RUN, caps(daily_loss_limit=0.15))
        assert run.vetoes == {"daily": 1}
        assert sorted(names(run)) == [("A", "09:30"), ("B", "09:35"), ("D", "09:30")]

    def test_a_win_does_not_offset_the_daily_gross_loss(self):
        c = ctxs(
            W=(Fire({2}), session("2026-06-01", {4: "TP"})),
            L1=(Fire({2}), session("2026-06-01", {4: "SL"})),
            L2=(Fire({3}), session("2026-06-01", {5: "SL"})),
            X=(Fire({8}), session("2026-06-01")),
        )
        run = lc.run_capped(c, None, RUN, caps(daily_loss_limit=0.15))
        assert run.vetoes == {"daily": 1}  # two losses of 0.1; the win does not net against them

    def test_weekly_limit_is_net_and_resets_on_monday(self):
        assert pd.Timestamp("2026-06-01").weekday() == 0
        c = {
            "L0": (Fire({2}), session("2026-06-01", {4: "SL"})),
            "L1": (Fire({2}), session("2026-06-02", {4: "SL"})),
            "W": (Fire({2}), session("2026-06-03")),  # Wednesday: the week is -0.2 net
            "M": (Fire({2}), session("2026-06-08")),  # next Monday: a new week
        }
        run = lc.run_capped(ctxs(**c), None, RUN, caps(weekly_loss_limit=0.15))
        assert run.vetoes == {"weekly": 1}
        assert sorted(t.symbol for t in run.trades) == ["L0", "L1", "M"]

    def test_weekly_net_includes_wins(self):
        c = {
            "W": (Fire({2}), session("2026-06-01", {4: "TP"})),
            "L": (Fire({2}), session("2026-06-02", {4: "SL"})),
            "X": (Fire({2}), session("2026-06-03")),
        }
        assert lc.run_capped(ctxs(**c), None, RUN, caps(weekly_loss_limit=0.05)).vetoes == {}

    def test_monthly_limit_spans_weeks(self):
        days = ["2026-06-01", "2026-06-08", "2026-06-15"]
        c = {f"L{k}": (Fire({2}), session(d, {4: "SL"})) for k, d in enumerate(days)}
        c["X"] = (Fire({2}), session("2026-06-22"))
        run = lc.run_capped(ctxs(**c), None, RUN, caps(monthly_loss_limit=0.25))
        assert run.vetoes == {"monthly": 1} and "X" not in [t.symbol for t in run.trades]

    def test_a_new_month_resets_the_monthly_limit(self):
        c = {f"L{k}": (Fire({2}), session(d, {4: "SL"})) for k, d in enumerate(["2026-06-01", "2026-06-02"])}
        c["N"] = (Fire({2}), session("2026-07-01"))
        assert lc.run_capped(ctxs(**c), None, RUN, caps(monthly_loss_limit=0.15)).vetoes == {}

    def test_unrealised_loss_counts_toward_the_daily_limit_but_a_winner_does_not(self):
        def moved(path):
            df = session("2026-06-01")
            for k, px in path.items():
                df.loc[df.index[k], ["Open", "High", "Low", "Close"]] = px
            return df

        # A enters at 09:30 and sits at 91 (-0.9R); B signals at bar 4. 0.9 x 0.1 = 0.09
        under = ctxs(
            A=(Fire({2}), moved({3: (100, 100, 91, 91), 4: (91, 91, 91, 91)})),
            B=(Fire({4}), session("2026-06-01")),
        )
        assert lc.run_capped(under, None, RUN, caps(daily_loss_limit=0.05)).vetoes == {"daily": 1}
        up = ctxs(
            A=(Fire({2}), moved({3: (100, 109, 100, 109), 4: (109, 109, 109, 109)})),
            B=(Fire({4}), session("2026-06-01")),
        )
        assert lc.run_capped(up, None, RUN, caps(daily_loss_limit=0.05)).vetoes == {}

    def test_the_pooled_allowance_scales_the_limits(self):
        c = {f"L{k}": (Fire({2}), session("2026-06-01", {4: "SL"})) for k in range(2)}
        c["X"] = (Fire({8}), session("2026-06-01"))
        full = lc.run_capped(ctxs(**c), None, RUN, caps(daily_loss_limit=0.30))
        half = lc.run_capped(ctxs(**c), None, RUN, caps(daily_loss_limit=0.30, pooled_allowance=0.5))
        assert full.vetoes == {} and half.vetoes == {"daily": 1}

    def test_day_start_equity_compounds_from_the_days_net_r(self):
        pf = lc.Portfolio(caps(daily_loss_limit=0.04), None, lambda s, t: None)
        day1 = pd.Timestamp("2026-06-01 09:30", tz=IST)
        assert pf.decide(SimpleNamespace(entry_time=day1, price=100.0)) == (True, None)
        pf.ingest(
            SimpleNamespace(
                symbol="A", entry_time=day1, exit_time=day1 + pd.Timedelta(minutes=5),
                direction=1, entry=100.0, risk=10.0, r_net=2.0, reason="TP",
            )
        )
        pf.decide(SimpleNamespace(entry_time=pd.Timestamp("2026-06-02 09:30", tz=IST), price=100.0))
        assert pf._day_start == pytest.approx(1.0 + F * 2.0)

    def test_a_trade_without_a_stop_cannot_be_sized(self):
        c = ctxs(A=(Fire({2}, with_stop=False), session("2026-06-01")))
        with pytest.raises(lc.LiveCapsError):
            lc.run_capped(c, None, RUN, lc.unlimited(F))


class TestDeterminism:
    @staticmethod
    def _book():
        return ctxs(
            A=(Fire({2, 6}), session("2026-06-01", {4: "SL", 8: "TP"})),
            B=(Fire({3, 7}), session("2026-06-01", {5: "SL", 9: "SL"})),
            C=(Fire({2, 5}), session("2026-06-01", {4: "TP", 7: "SL"})),
        )

    def test_repeated_runs_give_identical_trades_and_counts(self):
        cap = caps(max_open_positions=1, max_trades_per_day=3, daily_loss_limit=0.2)
        a, b = lc.run_capped(self._book(), None, RUN, cap), lc.run_capped(self._book(), None, RUN, cap)
        assert lc.trade_hash(a.trades) == lc.trade_hash(b.trades)
        assert a.vetoes == b.vetoes and a.taken == b.taken == len(a.trades)

    def test_the_contexts_can_be_reused_for_another_pass_without_leaking_state(self):
        book = self._book()
        first = lc.run_capped(book, None, RUN, caps(max_open_positions=1))
        wide = lc.run_capped(book, None, RUN, caps(max_open_positions=3))
        again = lc.run_capped(book, None, RUN, caps(max_open_positions=1))
        assert lc.trade_hash(first.trades) == lc.trade_hash(again.trades)
        assert len(wide.trades) >= len(first.trades)

    def test_no_limits_never_trades_less_than_a_cap(self):
        book = self._book()
        tight = lc.run_capped(book, None, RUN, caps(max_open_positions=1))
        none = lc.run_capped(book, None, RUN, lc.unlimited(F))
        assert len(none.trades) >= len(tight.trades)


class TestStageAndEvidence:
    @staticmethod
    def _frames():
        return {"A": session("2026-06-01", {4: "SL"}), "B": session("2026-06-01", {5: "TP"})}

    def _stage(self, headline=None, **kw):
        frames = self._frames()
        head = (
            headline
            if headline is not None
            else simulate(Ctx(frames["A"], "A"), Fire({2, 3}), None, RUN)
            + simulate(Ctx(frames["B"], "B"), Fire({2, 3}), None, RUN)
        )
        args = {
            "strategy": Fire({2, 3}), "params": None, "load_frames": lambda syms: {s: frames[s] for s in syms},
            "symbols": ["A", "B"], "run": RUN, "cost_model": None, "headline_trades": head,
            "caps": caps(max_open_positions=1),
        }
        args.update(kw)
        return lc.run_stage(**args)

    def test_a_complete_stage_has_a_base_check_one_primary_and_the_recorded_limits(self):
        ev = self._stage()
        assert ev["state"] == lc.COMPLETE and lc.validate_evidence(ev) == []
        assert ev["base_check"]["trades"] == ev["headline"]["trades"]
        assert [p["max_open_positions"] for p in ev["passes"]] == [1, 2, 3]
        assert [p["primary"] for p in ev["passes"]] == [True, False, False]
        assert ev["diagnostic_not_trials"] is True
        assert any("pooled" in x for x in ev["limitations"])
        assert any("unrealised" in x for x in ev["limitations"])

    def test_every_pass_and_the_base_check_carry_the_peak_margin_when_a_cost_model_is_given(self):
        from signal_engine.backtest.costs import recost
        from signal_engine.tests.cost_fixtures import make_model

        model = make_model()
        frames = self._frames()
        head = recost(simulate(Ctx(frames["A"], "A"), Fire({2, 3}), None, RUN)
                      + simulate(Ctx(frames["B"], "B"), Fire({2, 3}), None, RUN), model)
        ev = self._stage(cost_model=model, headline=head)
        assert ev["state"] == lc.COMPLETE
        margins = [p["margin"]["peak_fraction"] for p in ev["passes"]]
        assert all(m > 0 for m in margins) and margins == sorted(margins)  # more slots, no less margin
        assert ev["base_check"]["margin"]["peak_fraction"] >= margins[-1]
        assert ev["passes"][0]["margin"]["positions_at_peak"] == 1  # the primary holds one position
        assert self._stage()["passes"][0]["margin"] is None  # no cost model, no margin

    def test_every_pass_is_reproducible_from_the_same_inputs(self):
        a, b = self._stage(), self._stage()
        assert [p["trade_sha256"] for p in a["passes"]] == [p["trade_sha256"] for p in b["passes"]]
        assert a["base_check"]["trade_sha256"] == b["base_check"]["trade_sha256"]

    def test_a_base_check_that_does_not_match_the_headline_makes_the_stage_unusable(self):
        ev = self._stage(headline=[])
        assert ev["state"] == lc.BASE_REPLAY_MISMATCH and "passes" not in ev
        assert lc.validate_evidence(ev) == []  # well formed, but not a usable result

    def test_a_trade_without_a_stop_marks_the_stage_unsupported(self):
        head = simulate(Ctx(self._frames()["A"], "A"), Fire({2}, with_stop=False), None, RUN)
        ev = self._stage(strategy=Fire({2, 3}, with_stop=False), headline=head)
        assert ev["state"] == lc.UNSUPPORTED and "stop" in ev["reason"]

    def test_an_unexpected_error_is_recorded_not_raised(self):
        def boom(_):
            raise RuntimeError("no data")

        ev = self._stage(load_frames=boom)
        assert ev["state"] == lc.ERROR and "no data" in ev["reason"]

    def test_validation_rejects_incomplete_evidence(self):
        ev = self._stage()
        assert lc.validate_evidence({**ev, "passes": []})
        assert lc.validate_evidence({**ev, "limitations": []})
        two = [dict(p, primary=True) for p in ev["passes"]]
        assert lc.validate_evidence({**ev, "passes": two})
        assert lc.validate_evidence("x") and lc.validate_evidence({"state": "bogus"})


class TestLiveProfileAndSlotFormula:
    def test_caps_are_read_from_the_live_profile(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            textwrap.dedent(
                """
                sizing: {risk_per_trade: 0.01}
                mode_profiles:
                  live:
                    max_open_positions: -1
                    max_trades_per_day: 10
                    daily_loss_limit: 0.04
                    weekly_loss_limit: 0.08
                    monthly_loss_limit: 0.15
                    min_entry_price: 300
                    max_entry_price: 5000
                """
            )
        )
        c, src = lc.live_caps_from_config(path, max_open_positions=1)
        assert (c.max_trades_per_day, c.daily_loss_limit, c.weekly_loss_limit) == (10, 0.04, 0.08)
        assert (c.monthly_loss_limit, c.min_entry_price, c.max_entry_price) == (0.15, 300, 5000)
        assert (c.risk_fraction, c.max_open_positions) == (0.01, 1)
        assert src["live_config_max_open_positions"] == -1 and src["profile"] == "mode_profiles.live"

    def test_the_repository_config_loads_with_every_limit_set(self):
        c, _ = lc.live_caps_from_config()
        assert c.max_trades_per_day > 0 and c.daily_loss_limit > 0
        assert c.max_entry_price > c.min_entry_price > 0

    def test_the_slot_ceiling_matches_the_live_risk_engine_formula(self):
        from signal_engine.risk import RiskEngine

        for risk, margin, sl, slip in [(0.01, 0.20, 0.002, 0.10), (0.01, 0.20, 0.0056, 0.10), (0.02, 0.5, 0.01, 0.0)]:
            engine = object.__new__(RiskEngine)
            engine.risk_per_trade, engine.mis_margin_pct = risk, margin
            engine.worst_case_sl_pct, engine.slippage_factor = sl, slip
            engine.margin_reserve_buffer = 0.0
            assert lc.slot_ceiling(risk, margin, sl, slip) == engine._dynamic_max_open_positions(1e15)

    def test_the_primary_slot_for_the_live_strategy_set_is_one(self):
        assert lc.slot_ceiling(0.01, 0.20, 0.0020, 0.10) == 1
        assert lc.slot_ceiling(0.01, 0.20, 0.0056, 0.10) == 3

