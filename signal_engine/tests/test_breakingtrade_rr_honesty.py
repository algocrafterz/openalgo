"""The advertised R:R must describe the trade that is actually placed.

Found in production, 2026-09-11: ALL 25 of the day's BreakingTrade alerts overstated their
R:R by exactly 2.00x. The message sends `TP: plan.targets[0]` (the 1.0x IB target) but the
`R:R:` line was computed against `plan.targets[-1]` (the 2.0x one), so the two numbers on
adjacent lines described different trades.

It is not a rounding quibble. The staged ladder is computed but NOT wired through - every
BreakingTrade TP HIT exits 100% at targets[0] (see config.yaml's BREAKINGTRADE profile) - so
the advertised R:R described an exit sequence the engine never performs. Real examples:

    UNIONBANK  entry 178.28  SL 175.44  TP 179.99  ->  real 1:0.60, advertised 1:1.2
    UPL        entry 571.95  SL 567.38  TP 577.15  ->  real 1:1.14, advertised 1:2.28

UNIONBANK is the sharp case: the channel showed 1:1.2 and the engine then IGNORED it for
being under the 0.75 minimum, on the same numbers.
"""

import pytest

from signal_engine.analysis.breakingtrade.trigger import TradePlan


def _plan(entry=100.0, stop=98.0, targets=(102.0, 103.0, 104.0)):
    return TradePlan(
        symbol="SBIN", direction="up", day_type=None, entry=entry, stop=stop,
        targets=list(targets), split=(0.5, 0.3, 0.2), ib_high=101.0, ib_low=99.0,
        atr=1.0, risk_per_share=abs(entry - stop), triggered_at=None, notes=[],
    )


class TestRewardRiskDescribesTheTpThatIsSent:
    def test_long_uses_the_first_target(self):
        # risk 2.0, first target +2.0 -> 1:1.0 (NOT +4.0 -> 1:2.0)
        assert _plan().reward_risk == 1.0

    def test_short_uses_the_first_target(self):
        plan = TradePlan(
            symbol="SBIN", direction="down", day_type=None, entry=100.0, stop=102.0,
            targets=[98.0, 97.0, 96.0], split=(0.5, 0.3, 0.2), ib_high=101.0,
            ib_low=99.0, atr=1.0, risk_per_share=2.0, triggered_at=None, notes=[],
        )
        assert plan.reward_risk == 1.0

    def test_the_production_unionbank_case(self):
        """The channel said 1:1.2 for a trade the engine then rejected at 0.60."""
        plan = _plan(entry=178.28, stop=175.44, targets=(179.99, 181.7, 183.41))
        assert plan.reward_risk == pytest.approx(0.6, abs=0.01)

    def test_the_production_upl_case(self):
        plan = _plan(entry=571.95, stop=567.38, targets=(577.15, 582.35, 587.55))
        assert plan.reward_risk == pytest.approx(1.14, abs=0.01)

    def test_no_targets_yields_none(self):
        assert _plan(targets=()).reward_risk is None

    def test_zero_risk_yields_none(self):
        plan = _plan(entry=100.0, stop=100.0)
        assert plan.reward_risk is None


class TestRunnerRewardRiskIsSeparateAndLabelled:
    def test_the_final_target_is_still_available(self):
        """The ladder is real information - it just is not what `R:R` means."""
        assert _plan().reward_risk_runner == 2.0

    def test_it_is_none_when_there_is_no_ladder(self):
        assert _plan(targets=(102.0,)).reward_risk_runner is None


class TestTheAlertMessageIsSelfConsistent:
    def test_the_rr_line_matches_the_tp_line(self):
        from signal_engine.analysis.breakingtrade import alerts

        plan = _plan(entry=178.28, stop=175.44, targets=(179.99, 181.7, 183.41))
        message = alerts.build_trade_signal_message(plan, "BREAKINGTRADE")
        fields = dict(
            line.split(": ", 1) for line in message.splitlines() if ": " in line
        )
        entry, sl, tp = float(fields["Entry"]), float(fields["SL"]), float(fields["TP"])
        advertised = float(fields["R:R"].lstrip("1:"))
        assert advertised == pytest.approx(abs(tp - entry) / abs(entry - sl), abs=0.01)

    def test_the_runner_target_is_named_not_folded_into_rr(self):
        from signal_engine.analysis.breakingtrade import alerts

        message = alerts.build_trade_signal_message(_plan(), "BREAKINGTRADE")
        assert "Runner target" in message
        assert "R:R: 1:1.0" in message
