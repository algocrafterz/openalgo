"""Entry caps derived from a frozen spec's re-entry rule.

`re_entry.max_per_symbol_day` is the number of RE-ENTRY events per symbol per trading session,
across both directions; the initial entry does not count. A frozen strategy that states an explicit
per-direction rule keeps it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from signal_engine.strategy_core import trade_policy as tp
from signal_engine.strategy_core.trade_policy import EntryCaps, entry_caps_from_spec

DAY1, DAY2 = dt.date(2026, 10, 12), dt.date(2026, 10, 13)
IDEAS = Path(__file__).resolve().parent.parent / "research" / "ideas"


def _policy(caps: EntryCaps) -> tp.TradePolicy:
    return tp.TradePolicy(
        max_entries_per_direction_per_day=caps.max_entries_per_direction,
        max_trades_per_day=caps.max_trades_per_day,
    )


def _fill(state: tp.SymbolDay, direction: int) -> tp.SymbolDay:
    """A filled entry that has already exited (flat again), as the policy counts fills."""
    return tp.SymbolDay(
        day=state.day,
        long_fills=state.long_fills + (direction == 1),
        short_fills=state.short_fills + (direction == -1),
        trades=state.trades + 1,
    )


class TestDerivation:
    def test_no_re_entry_means_one_entry_per_symbol_session(self):
        caps = entry_caps_from_spec({"allowed": False})
        assert (caps.max_trades_per_day, caps.max_entries_per_direction) == (1, None)

    def test_zero_re_entries_means_one_entry(self):
        caps = entry_caps_from_spec({"allowed": True, "max_per_symbol_day": 0})
        assert caps.max_trades_per_day == 1

    def test_the_initial_entry_does_not_count(self):
        caps = entry_caps_from_spec({"allowed": True, "max_per_symbol_day": 2})
        assert caps.max_trades_per_day == 3

    def test_an_explicit_direction_rule_wins_and_keeps_its_own_count(self):
        caps = entry_caps_from_spec(
            {"allowed": True, "max_per_symbol_day": 2}, per_direction_rule=2, both_directions=True
        )
        assert (caps.max_entries_per_direction, caps.max_trades_per_day) == (2, 4)
        assert "direction" in caps.rule

    def test_a_one_sided_strategy_with_a_direction_rule_caps_that_side_only(self):
        caps = entry_caps_from_spec(None, per_direction_rule=2, both_directions=False)
        assert (caps.max_entries_per_direction, caps.max_trades_per_day) == (2, 2)

    def test_no_rule_at_all_derives_nothing(self):
        assert entry_caps_from_spec(None) is None

    @pytest.mark.parametrize("bad", [-1, 1.5, True, "2", None])
    def test_a_malformed_count_is_refused_not_guessed(self, bad):
        with pytest.raises(ValueError, match="max_per_symbol_day"):
            entry_caps_from_spec({"allowed": True, "max_per_symbol_day": bad})

    def test_re_entry_not_allowed_with_a_count_is_refused(self):
        with pytest.raises(ValueError, match="not allowed"):
            entry_caps_from_spec({"allowed": False, "max_per_symbol_day": 2})

    def test_a_non_boolean_allowed_is_refused(self):
        with pytest.raises(ValueError, match="allowed"):
            entry_caps_from_spec({"allowed": "yes"})


class TestStatedSessionTotal:
    """An explicit per-session trade total in the spec's own words must agree with the field."""

    def test_the_total_is_read_from_the_rule_text(self):
        rules = ["one position at a time; at most 2 trades per symbol per session, reported"]
        assert tp.stated_session_total(rules) == 2
        assert tp.stated_session_total("At most 1 trade per symbol per day") == 1

    def test_a_per_direction_count_is_not_a_session_total(self):
        rules = ["at most 2 accepted entries per symbol per direction per day"]
        assert tp.stated_session_total(rules) is None

    def test_no_text_states_nothing(self):
        assert tp.stated_session_total(None) is None
        assert tp.stated_session_total(["enter on the close"]) is None

    def test_two_different_totals_in_the_text_are_refused(self):
        with pytest.raises(tp.SpecConflict, match="1.*2|2.*1"):
            tp.stated_session_total(
                ["at most 1 trade per symbol per day", "at most 2 trades per symbol per session"]
            )

    def test_an_agreeing_total_is_accepted(self):
        caps = entry_caps_from_spec(
            {"allowed": True, "max_per_symbol_day": 1}, stated_session_total=2
        )
        assert caps.max_trades_per_day == 2

    def test_a_disagreeing_total_is_refused_with_both_readings(self):
        with pytest.raises(tp.SpecConflict) as err:
            entry_caps_from_spec({"allowed": True, "max_per_symbol_day": 2}, stated_session_total=2)
        assert "2 trades" in str(err.value) and "3" in str(err.value)

    def test_a_conflict_is_a_value_error(self):
        assert issubclass(tp.SpecConflict, ValueError)


class TestRunFields:
    def test_the_default_one_trade_per_direction_is_cleared(self):
        from signal_engine.backtest.types import RunConfig

        assert RunConfig().one_trade_per_direction is True  # the hidden second cap
        fields = entry_caps_from_spec({"allowed": True, "max_per_symbol_day": 2}).run_fields()
        assert fields == {
            "max_trades_per_day": 3,
            "max_entries_per_direction": None,
            "one_trade_per_direction": False,
        }

    def test_an_explicit_direction_rule_sets_both_caps(self):
        fields = entry_caps_from_spec(None, per_direction_rule=2).run_fields()
        assert (fields["max_entries_per_direction"], fields["max_trades_per_day"]) == (2, 4)


class TestEnforcement:
    """The shared policy applies the derived caps, in a session and across sessions."""

    def test_zero_re_entries_blocks_the_second_entry(self):
        policy = _policy(entry_caps_from_spec({"allowed": True, "max_per_symbol_day": 0}))
        state = _fill(tp.roll(None, DAY1), 1)
        assert tp.can_arm(policy, state, 1) == tp.MAX_TRADES_DAY

    def test_the_exact_limit_is_allowed_and_one_more_is_not(self):
        policy = _policy(entry_caps_from_spec({"allowed": True, "max_per_symbol_day": 2}))
        state = tp.roll(None, DAY1)
        assert tp.can_arm(policy, state, 1) == tp.OK  # initial entry
        state = _fill(state, 1)
        assert tp.can_arm(policy, state, 1) == tp.OK  # re-entry 1
        state = _fill(state, 1)
        assert tp.can_arm(policy, state, 1) == tp.OK  # re-entry 2, exactly the limit
        state = _fill(state, 1)
        assert tp.can_arm(policy, state, 1) == tp.MAX_TRADES_DAY  # a third re-entry is refused

    def test_the_limit_is_shared_by_both_directions(self):
        policy = _policy(entry_caps_from_spec({"allowed": True, "max_per_symbol_day": 1}))
        state = _fill(tp.roll(None, DAY1), 1)  # long
        assert tp.can_arm(policy, state, -1) == tp.OK  # a short is the one allowed re-entry
        state = _fill(state, -1)
        assert tp.can_arm(policy, state, 1) == tp.MAX_TRADES_DAY
        assert tp.can_arm(policy, state, -1) == tp.MAX_TRADES_DAY

    def test_an_explicit_direction_rule_counts_each_direction_on_its_own(self):
        caps = entry_caps_from_spec(
            {"allowed": True, "max_per_symbol_day": 2}, per_direction_rule=2, both_directions=True
        )
        policy = _policy(caps)
        state = tp.roll(None, DAY1)
        for _ in range(2):
            state = _fill(state, 1)
        assert tp.can_arm(policy, state, 1) == tp.DIRECTION_ALREADY_TAKEN
        assert tp.can_arm(policy, state, -1) == tp.OK
        for _ in range(2):
            state = _fill(state, -1)
        assert tp.can_arm(policy, state, -1) == tp.MAX_TRADES_DAY

    def test_a_new_trading_session_resets_every_counter(self):
        policy = _policy(entry_caps_from_spec({"allowed": False}))
        state = _fill(tp.roll(None, DAY1), 1)
        assert tp.can_arm(policy, state, 1) == tp.MAX_TRADES_DAY
        assert tp.can_arm(policy, tp.roll(state, DAY2), 1) == tp.OK


class TestBacktestEngineUsesTheDerivedCaps:
    """The caps reach the engine's RunConfig, so the backtest enforces what the spec states."""

    def _run(self, spec_re_entry, signals=6):
        from signal_engine.backtest.engine import simulate
        from signal_engine.backtest.strategies.base import Strategy
        from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
        from signal_engine.tests.test_backtest_engine import bars

        class EveryThird(Strategy):
            name = "every-third"

            def prepare(self, df, p):
                return df

            def entry(self, c, i, p, direction):
                if direction != 1 or i % 3 != 1:
                    return None
                return EntrySignal(direction=1, sl=99.0, tp=100.5, tag="T")

        rows = [(100, 101.5, 98.5, 100)] * (signals * 3 + 3)
        df = bars(rows)
        caps = entry_caps_from_spec(spec_re_entry)
        run = RunConfig(
            skip_open_minutes=0,
            entry_cutoff_min=15 * 60,
            time_exit_min=15 * 60,
            min_sl_pct=0.0,
            **caps.run_fields(),
        )
        return simulate(Ctx(df, "T"), EveryThird(), None, run)

    def test_no_re_entry_gives_one_trade_in_the_session(self):
        assert len(self._run({"allowed": False})) == 1

    def test_two_re_entries_give_three_trades(self):
        assert len(self._run({"allowed": True, "max_per_symbol_day": 2})) == 3


class TestFrozenSpecsAreReconciledNotConcealed:
    """What each frozen spec now enforces. A change to a frozen spec or to the rule shows here."""

    @staticmethod
    def _caps(idea_glob):
        path = next(IDEAS.glob(idea_glob)) / "spec.yaml"
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        rule = (spec.get("parameters") or {}).get("max_entries_per_direction_per_symbol_per_day")
        return entry_caps_from_spec(
            spec.get("re_entry"),
            per_direction_rule=rule,
            both_directions=spec["direction"] == "both",
            stated_session_total=tp.stated_session_total(spec.get("entry")),
        )

    def test_breakout_keeps_its_explicit_direction_rule(self):
        caps = self._caps("idea-031-*")
        assert (caps.max_entries_per_direction, caps.max_trades_per_day) == (2, 4)

    def test_vwap_reversion_v2_states_one_rule_two_trades_per_session(self):
        """IDEA-029 v2 resolves the v1 ambiguity in the direction its own text and its recorded
        runs took: 2 trades per symbol per session in total, the first entry and one re-entry."""
        caps = self._caps("idea-029-*")
        assert (caps.max_trades_per_day, caps.max_entries_per_direction) == (2, None)

    def test_vwap_reversion_v2_reaches_the_engine_as_two_trades(self):
        from signal_engine.backtest import record_run as rr
        from signal_engine.backtest.types import RunConfig
        from signal_engine.research.intake.freeze import freeze

        folder = next(IDEAS.glob("idea-029-*"))
        frozen = freeze(folder, idea_id="IDEA-029", register_version=False)
        versions = yaml.safe_load((folder / "versions.yaml").read_text(encoding="utf-8"))
        assert frozen.strategy_fingerprint == versions[-1]["strategy_fingerprint"]
        run = rr._apply_entry_caps(RunConfig(), rr._intent_settings(frozen.intent))
        assert (run.max_trades_per_day, run.max_entries_per_direction) == (2, None)

    def test_vwap_reversion_v1_stays_frozen_and_still_conflicts(self):
        """The v1 intent each recorded run was made from is untouched (rebuilt from the run
        folder, fingerprint-checked) and is still refused, not silently reread as v2."""
        from signal_engine.research.intake.freeze import from_run_folder

        folder = next(IDEAS.glob("idea-029-*"))
        v1 = from_run_folder(folder / "runs" / "2026-10-06-vwap-2sd-range-mean-reversion-v1")
        assert v1.intent["re_entry"]["max_per_symbol_day"] == 2
        with pytest.raises(tp.SpecConflict, match="2 trades.*3"):
            entry_caps_from_spec(
                v1.intent["re_entry"],
                stated_session_total=tp.stated_session_total(v1.intent.get("entry")),
            )
        versions = yaml.safe_load((folder / "versions.yaml").read_text(encoding="utf-8"))
        assert [(v["version"], v["parent"]) for v in versions] == [("v1", None), ("v2", "v1")]
        assert versions[0]["strategy_fingerprint"] == v1.strategy_fingerprint

    def test_displacement_without_re_entry_is_one_trade(self):
        assert self._caps("idea-032-*").max_trades_per_day == 1

    def test_early_displacement_fifty_re_entries(self):
        assert self._caps("idea-033-*").max_trades_per_day == 51


class TestRecordedRunAppliesTheCaps:
    """execute_backtest overlays the spec's caps on the strategy's own constants."""

    SPEC = {
        "direction": "both",
        "re_entry": {"allowed": True, "max_per_symbol_day": 1},
        "parameters": {"x": 1},
    }

    def test_the_intent_fields_reach_the_settings(self):
        from signal_engine.backtest import record_run as rr

        cfg = rr._intent_settings(
            {**self.SPEC, "parameters": {"max_entries_per_direction_per_symbol_per_day": 2}}
        )
        assert cfg["re_entry"] == self.SPEC["re_entry"]
        assert cfg["per_direction_rule"] == 2 and cfg["direction"] == "both"

    def test_the_spec_wins_over_a_strategy_constant(self):
        from signal_engine.backtest import record_run as rr
        from signal_engine.backtest.types import RunConfig

        strategy_run = RunConfig().with_(max_trades_per_day=9, one_trade_per_direction=True)
        run = rr._apply_entry_caps(strategy_run, rr._intent_settings(self.SPEC))
        assert (run.max_trades_per_day, run.one_trade_per_direction) == (2, False)
        assert run.max_entries_per_direction is None

    def test_a_spec_without_a_rule_leaves_the_strategy_constants(self):
        from signal_engine.backtest import record_run as rr
        from signal_engine.backtest.types import RunConfig

        strategy_run = RunConfig().with_(max_trades_per_day=9)
        assert rr._apply_entry_caps(strategy_run, {}) == strategy_run

    def test_a_text_and_field_conflict_stops_the_run(self):
        from signal_engine.backtest import record_run as rr
        from signal_engine.backtest.types import RunConfig

        spec = {
            **self.SPEC,
            "re_entry": {"allowed": True, "max_per_symbol_day": 2},
            "entry": ["at most 2 trades per symbol per session"],
        }
        cfg = rr._intent_settings(spec)
        assert cfg["stated_session_total"] == 2
        with pytest.raises(tp.SpecConflict):
            rr._apply_entry_caps(RunConfig(), cfg)

    def test_the_applied_rule_is_recorded_for_audit(self):
        from signal_engine.backtest import record_run as rr
        from signal_engine.backtest.types import RunConfig

        record = rr._entry_caps_record(rr._intent_settings(self.SPEC))
        assert record["max_trades_per_day"] == 2 and "re-entry events" in record["rule"]
        assert rr._entry_caps_record({}) is None
