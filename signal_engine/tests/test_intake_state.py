"""Intake state is computed from the spec and the ledger, never stored."""

from __future__ import annotations

from signal_engine.research.intake import ledger, state
from signal_engine.tests.intake_fixtures import AMBIGUOUS_TIMING, make_spec


def _led(spec, **kw):
    return ledger.resolve(spec, **kw)


def _state(spec, led=None):
    return state.intake_state(spec, led if led is not None else _led(spec))


def _confirm_high(led):
    return ledger.confirm(
        led, [e.id for e in led.entries if e.materiality == "high" and not e.user_confirmed]
    )


class TestStates:
    def test_constants(self):
        assert state.STATES == (
            "DRAFT",
            "NEEDS_CLARIFICATION",
            "ASSUMPTION_REVIEW",
            "BACKTEST_READY",
            "BACKTEST_BLOCKED",
        )

    def test_complete_spec_is_backtest_ready(self):
        result = _state(make_spec())
        assert result.state == state.BACKTEST_READY
        assert result.blockers == () and result.unconfirmed_high == ()

    def test_missing_blocker_is_draft(self):
        result = _state(make_spec(entry=None))
        assert result.state == state.DRAFT
        assert [p.field for p in result.blockers] == ["entry"]

    def test_vague_blocker_is_draft(self):
        assert _state(make_spec(entry=["Buy when momentum is strong"])).state == state.DRAFT

    def test_validity_problem_is_never_ready(self):
        assert _state(make_spec(style="weekly")).state == state.DRAFT

    def test_open_questions_make_it_needs_clarification(self):
        spec = make_spec(entry=None)
        led = ledger.Ledger(
            entries=_led(spec).entries, open_questions=("what is the entry?",), next_id=50
        )
        assert _state(spec, led).state == state.NEEDS_CLARIFICATION

    def test_open_questions_do_not_matter_when_no_blocker(self):
        spec = make_spec()
        led = ledger.Ledger(entries=_led(spec).entries, open_questions=("stale",), next_id=50)
        assert _state(spec, led).state == state.BACKTEST_READY

    def test_unconfirmed_high_inference_is_assumption_review(self):
        spec = make_spec(portfolio={"mode": "single_position"})
        result = _state(spec)
        assert result.state == state.ASSUMPTION_REVIEW
        assert [e.field for e in result.unconfirmed_high] == ["portfolio.scope"]

    def test_ambiguous_timing_needs_review_until_confirmed(self):
        spec = make_spec(**AMBIGUOUS_TIMING)
        led = _led(spec)
        result = _state(spec, led)
        assert result.state == state.ASSUMPTION_REVIEW
        assert {e.field for e in result.unconfirmed_high} == {
            "timing.signal_evaluation",
            "timing.order_submission",
            "timing.fill_model",
        }
        assert _state(spec, _confirm_high(led)).state == state.BACKTEST_READY

    def test_user_stated_high_entries_never_block(self):
        assert _state(make_spec()).unconfirmed_high == ()

    def test_unconfirmed_medium_is_listed_but_not_blocking(self):
        result = _state(make_spec())
        fields = {e.field for e in result.unconfirmed_medium}
        assert "execution.slippage_bps_per_side" in fields
        assert "timing.signal_evaluation" in fields
        assert result.state == state.BACKTEST_READY

    def test_derivable_gap_without_a_ledger_entry_stays_a_blocker(self):
        spec = make_spec(portfolio={"mode": "single_position"})
        empty = ledger.Ledger(entries=())
        result = _state(spec, empty)
        assert result.state == state.DRAFT
        assert "portfolio.scope" in [p.field for p in result.blockers]

    def test_reasons_are_human_readable(self):
        result = _state(make_spec(entry=None))
        assert result.reasons and "entry" in result.reasons[0]


class TestBlocked:
    def test_blocked_reason_wins_over_everything(self):
        spec = make_spec(entry=None)
        led = ledger.Ledger(entries=_led(spec).entries, blocked_reason="source is a rumour")
        result = _state(spec, led)
        assert result.state == state.BACKTEST_BLOCKED
        assert "source is a rumour" in result.reasons[0]

    def test_clearing_blocked_reason_recomputes(self):
        spec = make_spec()
        blocked = ledger.Ledger(entries=_led(spec).entries, blocked_reason="hold")
        assert _state(spec, blocked).state == state.BACKTEST_BLOCKED
        cleared = ledger.Ledger(entries=blocked.entries, blocked_reason=None)
        assert _state(spec, cleared).state == state.BACKTEST_READY


class TestTransitions:
    def test_draft_to_clarification_to_draft_to_review_to_ready(self):
        spec = make_spec(entry=None, portfolio={"mode": "single_position"})
        led = _led(spec)
        assert _state(spec, led).state == state.DRAFT

        asked = ledger.Ledger(entries=led.entries, open_questions=("entry?",), next_id=led.next_id)
        assert _state(spec, asked).state == state.NEEDS_CLARIFICATION

        answered = make_spec(portfolio={"mode": "single_position"})
        led = ledger.resolve(answered, existing=asked)
        assert _state(answered, led).state == state.ASSUMPTION_REVIEW

        assert _state(answered, _confirm_high(led)).state == state.BACKTEST_READY
