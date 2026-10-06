"""IDEA-029 VWAP 2SD reversion state machine, on hand-built bars.

Every bar below carries the values a decision reads at that bar's close (VWAP 100, bands
98 / 102, ATR 2 unless stated), so each test names one rule of the frozen spec.
"""

from __future__ import annotations

import pytest

from signal_engine.strategy_core.vwap_reversion import (
    DIAGNOSTIC_KEYS,
    MAX_TRADES_PER_SESSION,
    Bar,
    VwapReversionMachine,
    VwapReversionParams,
    entry_beyond_levels,
)

P = VwapReversionParams()


def bar(k, low, high, close, ema9=None, **kw):
    """VWAP 100, bands 98/102, ATR 2. ema9 defaults to just below a long close."""
    base = {
        "k": k,
        "high": high,
        "low": low,
        "close": close,
        "vwap": 100.0,
        "lower": 98.0,
        "upper": 102.0,
        "ema9": close - 0.1 if ema9 is None else ema9,
        "atr": 2.0,
        "regime_ok": True,
        "warm": True,
        "confirmable": True,
    }
    base.update(kw)
    return Bar(**base)


def arm_long(k=0, low=97.5):
    return bar(k, low=low, high=99.0, close=98.2, ema9=99.0)  # close below EMA9: no confirm


def confirm_long(k):
    return bar(k, low=98.1, high=99.2, close=98.6, ema9=98.5)


def short_bar(k, low, high, close, ema9=None, **kw):
    return bar(k, low=low, high=high, close=close, ema9=close + 0.1 if ema9 is None else ema9, **kw)


def run(machine, bars):
    return [machine.on_bar(b) for b in bars]


class TestParameters:
    def test_defaults_are_the_frozen_spec(self):
        assert vars(P) == {
            "vwap_sd": 2.0,
            "atr_period_5m": 14,
            "min_vwap_distance_atr": 0.75,
            "stop_buffer_atr": 0.20,
            "adx_period_15m": 14,
            "adx_max": 22.0,
            "ema_regime_period_15m": 20,
            "ema_regime_lookback_bars": 4,
            "ema_regime_max_change_atr": 0.25,
            "atr_period_15m": 14,
            "ema_entry_period_5m": 9,
            "arm_bars": 6,
            "warmup_15m_bars": 100,
        }
        assert MAX_TRADES_PER_SESSION == 2


class TestLongPath:
    def test_arm_then_confirm(self):
        m = VwapReversionMachine(P)
        out = run(m, [arm_long(0), confirm_long(1)])
        assert out[0] is None
        c = out[1]
        assert (c.bar, c.direction) == (1, 1)
        assert c.extreme == 97.5
        assert c.stop == pytest.approx(97.5 - 0.2 * 2.0)
        assert c.target == 100.0
        d = m.diagnostics()
        assert d["setups_armed_long"] == 1 and d["confirmations"] == 1

    def test_extreme_bar_cannot_confirm_itself(self):
        m = VwapReversionMachine(P)
        # arms AND would satisfy the confirmation tests on the same bar
        assert m.on_bar(bar(0, low=97.5, high=99.0, close=98.6, ema9=98.5)) is None
        assert m.diagnostics()["setups_armed_long"] == 1

    def test_displacement_below_threshold_does_not_arm(self):
        m = VwapReversionMachine(P)
        # band touched but (100 - 98.6) / 2 = 0.7 < 0.75
        m.on_bar(bar(0, low=98.6, high=99.0, close=98.8, lower=98.6))
        assert m.diagnostics()["setups_armed_long"] == 0

    def test_displacement_exactly_at_threshold_arms(self):
        m = VwapReversionMachine(P)
        m.on_bar(bar(0, low=98.5, high=99.0, close=98.8, lower=98.5))  # 1.5 / 2 = 0.75
        assert m.diagnostics()["setups_armed_long"] == 1

    def test_zero_atr_never_arms(self):
        m = VwapReversionMachine(P)
        m.on_bar(bar(0, low=97.0, high=99.0, close=98.2, ema9=99.0, atr=0.0))
        assert m.diagnostics()["setups_armed_long"] == 0


class TestShortPath:
    def test_arm_then_confirm(self):
        m = VwapReversionMachine(P)
        m.on_bar(short_bar(0, low=101.0, high=102.6, close=101.8, ema9=101.0))
        c = m.on_bar(short_bar(1, low=100.8, high=101.9, close=101.4, ema9=101.5))
        assert (c.bar, c.direction, c.extreme) == (1, -1, 102.6)
        assert c.stop == pytest.approx(102.6 + 0.4)
        assert c.target == 100.0
        assert m.diagnostics()["setups_armed_short"] == 1

    def test_low_touching_vwap_cancels(self):
        m = VwapReversionMachine(P)
        m.on_bar(short_bar(0, low=101.0, high=102.6, close=101.8, ema9=101.0))
        assert m.on_bar(short_bar(1, low=100.0, high=101.9, close=101.4, ema9=101.5)) is None
        assert m.diagnostics()["setups_cancelled_vwap_touch"] == 1

    def test_higher_high_replaces(self):
        m = VwapReversionMachine(P)
        m.on_bar(short_bar(0, low=101.0, high=102.6, close=101.8, ema9=101.0))
        m.on_bar(short_bar(1, low=101.0, high=103.0, close=102.0, ema9=101.0))
        c = m.on_bar(short_bar(2, low=100.8, high=101.9, close=101.4, ema9=101.5))
        assert c.extreme == 103.0
        assert m.diagnostics()["setups_replaced"] == 1


class TestSetupLifecycle:
    def test_deeper_extreme_that_passes_replaces_and_restarts_window(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0, low=97.5))
        # deeper low that also passes band and displacement: replaces, cannot confirm itself
        assert m.on_bar(bar(1, low=97.0, high=99.0, close=98.6, ema9=98.5)) is None
        # window restarts from bar 1: bars 2..7 may confirm
        for k in range(2, 7):
            assert m.on_bar(bar(k, low=98.1, high=99.0, close=98.2, ema9=99.0)) is None
        c = m.on_bar(confirm_long(7))
        assert c is not None and c.extreme == 97.0
        assert c.stop == pytest.approx(97.0 - 0.4)
        assert m.diagnostics()["setups_replaced"] == 1

    def test_deeper_extreme_that_fails_cancels(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0, low=97.5))
        # deeper low, but the band moved: Low 97.4 > lower band 97.3, so the test fails
        assert m.on_bar(bar(1, low=97.4, high=99.0, close=98.6, ema9=98.5, lower=97.3)) is None
        assert m.on_bar(confirm_long(2)) is None
        d = m.diagnostics()
        assert d["setups_cancelled_deeper_fail"] == 1 and d["confirmations"] == 0

    def test_equal_low_is_not_deeper(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0, low=97.5))
        c = m.on_bar(bar(1, low=97.5, high=99.2, close=98.6, ema9=98.5))
        assert c is not None and c.extreme == 97.5

    def test_high_touching_vwap_cancels(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0))
        assert m.on_bar(bar(1, low=98.1, high=100.0, close=98.6, ema9=98.5)) is None
        assert m.on_bar(confirm_long(2)) is None
        assert m.diagnostics()["setups_cancelled_vwap_touch"] == 1

    def test_expires_after_six_bars(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0))
        for k in range(1, 7):  # six non-confirming bars
            assert m.on_bar(bar(k, low=98.1, high=99.0, close=98.2, ema9=99.0)) is None
        assert m.diagnostics()["setups_expired"] == 1
        assert m.on_bar(confirm_long(7)) is None

    def test_sixth_bar_can_still_confirm(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0))
        for k in range(1, 6):
            m.on_bar(bar(k, low=98.1, high=99.0, close=98.2, ema9=99.0))
        assert m.on_bar(confirm_long(6)) is not None

    def test_cutoff_cancels_and_blocks_arming(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0))
        assert (
            m.on_bar(bar(1, low=98.1, high=99.2, close=98.6, ema9=98.5, confirmable=False)) is None
        )
        d = m.diagnostics()
        assert d["setups_cancelled_cutoff"] == 1 and d["confirmations"] == 0
        m.on_bar(bar(2, low=97.0, high=99.0, close=98.2, ema9=99.0, confirmable=False))
        assert m.diagnostics()["setups_armed_long"] == 1

    def test_session_reset_counts_a_live_setup_as_cutoff(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0))
        m.reset_session()
        assert m.diagnostics()["setups_cancelled_cutoff"] == 1
        assert m.on_bar(confirm_long(1)) is None


class TestGates:
    def test_invalid_regime_at_confirmation_cancels_the_setup(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0))
        assert m.on_bar(bar(1, low=98.1, high=99.2, close=98.6, ema9=98.5, regime_ok=False)) is None
        assert m.diagnostics()["blocked_by_regime"] == 1
        # cancelled, not carried: a later valid bar does not confirm the old setup
        assert m.on_bar(confirm_long(2)) is None
        assert m.diagnostics()["confirmations"] == 0

    def test_warmup_at_confirmation_cancels_the_setup(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0))
        assert m.on_bar(bar(1, low=98.1, high=99.2, close=98.6, ema9=98.5, warm=False)) is None
        assert m.on_bar(confirm_long(2)) is None
        d = m.diagnostics()
        assert d["blocked_by_warmup"] == 1 and d["confirmations"] == 0

    def test_close_must_be_above_the_band(self):
        m = VwapReversionMachine(P)
        m.on_bar(arm_long(0))
        assert m.on_bar(bar(1, low=97.6, high=99.0, close=97.9, ema9=97.8)) is None


class TestPositionAndReEntry:
    def test_no_setup_forms_while_in_position(self):
        m = VwapReversionMachine(P)
        run(m, [arm_long(0), confirm_long(1)])
        m.on_fill(2)
        m.on_bar(arm_long(2))
        m.on_bar(arm_long(3, low=96.0))
        assert m.diagnostics()["setups_armed_long"] == 1

    def test_re_entry_needs_a_new_extreme_after_the_exit_bar(self):
        m = VwapReversionMachine(P)
        run(m, [arm_long(0), confirm_long(1)])
        m.on_fill(2)
        m.on_bar(confirm_long(2))
        m.on_exit(3)
        m.on_bar(arm_long(3))  # the exit bar itself cannot arm
        assert m.on_bar(confirm_long(4)) is None
        m.on_bar(arm_long(5))
        assert m.on_bar(confirm_long(6)) is not None
        assert m.diagnostics()["setups_armed_long"] == 2

    def test_cap_blocks_the_third_confirmation(self):
        m = VwapReversionMachine(P)
        k = 0
        for _ in range(MAX_TRADES_PER_SESSION):
            run(m, [arm_long(k), confirm_long(k + 1)])
            m.on_fill(k + 2)
            m.on_exit(k + 2)
            k += 3
        assert m.on_bar(arm_long(k)) is None
        assert m.on_bar(confirm_long(k + 1)) is None
        d = m.diagnostics()
        assert d["confirmations"] == 3 and d["confirmations_blocked_by_cap"] == 1
        m.reset_session()
        run(m, [arm_long(k + 2)])
        assert m.on_bar(confirm_long(k + 3)) is not None

    def test_skipped_fill_is_not_a_trade(self):
        m = VwapReversionMachine(P)
        run(m, [arm_long(0), confirm_long(1)])
        m.on_fill_skipped(2)
        assert m.diagnostics()["entries_skipped_beyond_level"] == 1
        m.on_bar(arm_long(2))  # flat: a new setup can arm on the skipped fill bar
        assert m.on_bar(confirm_long(3)) is not None


class TestDiagnostics:
    def test_keys(self):
        assert set(VwapReversionMachine(P).diagnostics()) == set(DIAGNOSTIC_KEYS)
        assert set(DIAGNOSTIC_KEYS) == {
            "setups_armed_long",
            "setups_armed_short",
            "setups_replaced",
            "setups_cancelled_deeper_fail",
            "setups_cancelled_vwap_touch",
            "setups_expired",
            "setups_cancelled_cutoff",
            "setups_cancelled_position",
            "confirmations",
            "entries_skipped_beyond_level",
            "confirmations_blocked_by_cap",
            "blocked_by_warmup",
            "blocked_by_regime",
        }

    def test_counters_accumulate_across_symbols(self):
        m = VwapReversionMachine(P)
        run(m, [arm_long(0), confirm_long(1)])
        m.reset_symbol()
        run(m, [arm_long(0), confirm_long(1)])
        assert m.diagnostics()["confirmations"] == 2
        m.reset_diagnostics()
        assert sum(m.diagnostics().values()) == 0


class TestSkipAtFill:
    @pytest.mark.parametrize(
        "direction,fill,expected",
        [
            (1, 99.0, False),
            (1, 100.0, True),  # at the target
            (1, 97.1, True),  # at the stop
            (1, 96.0, True),
            (-1, 101.0, False),
            (-1, 100.0, True),
            (-1, 103.0, True),
        ],
    )
    def test_levels(self, direction, fill, expected):
        stop = 97.1 if direction == 1 else 103.0
        assert entry_beyond_levels(direction, fill, stop, 100.0) is expected


class TestApprovedDecisions:
    """The 2026-10-06 decisions on the five implementation gaps."""

    def test_stop_buffer_uses_the_extreme_bar_atr(self):
        m = VwapReversionMachine(P)
        m.on_bar(bar(0, low=97.0, high=99.0, close=98.2, ema9=99.0, atr=2.0))
        conf = m.on_bar(bar(1, low=98.1, high=99.2, close=98.6, ema9=98.5, atr=5.0))
        assert conf.stop == pytest.approx(97.0 - 0.20 * 2.0)

    def test_deeper_extreme_restates_the_atr(self):
        m = VwapReversionMachine(P)
        m.on_bar(bar(0, low=97.0, high=99.0, close=98.2, ema9=99.0, atr=2.0))
        m.on_bar(bar(1, low=96.0, high=98.0, close=97.0, ema9=99.0, atr=3.0))
        conf = m.on_bar(bar(2, low=97.5, high=99.2, close=98.6, ema9=98.5, atr=6.0))
        assert conf.stop == pytest.approx(96.0 - 0.20 * 3.0)

    def test_confirmation_keeps_the_opposite_setup(self):
        m = VwapReversionMachine(P)
        # one wide bar arms both sides
        m.on_bar(bar(0, low=97.5, high=102.5, close=100.5, ema9=100.0, vwap=100.0))
        conf = m.on_bar(bar(1, low=98.1, high=99.9, close=98.6, ema9=98.5, vwap=100.0))
        assert conf is not None and conf.direction == 1
        assert m._setups[1] is None and m._setups[-1] is not None

    def test_cap_blocked_confirmation_consumes_only_its_setup(self):
        m = VwapReversionMachine(P)
        m.on_fill(0)
        m.on_exit(0)
        m.on_fill(0)
        m.on_exit(0)
        m.on_bar(bar(1, low=97.5, high=102.5, close=100.5, ema9=100.0))
        assert m.on_bar(bar(2, low=98.1, high=99.9, close=98.6, ema9=98.5)) is None
        d = m.diagnostics()
        assert d["confirmations_blocked_by_cap"] == 1 and d["confirmations"] == 1
        assert m._setups[1] is None
        # the short setup is not cleared by the long confirmation: it is evaluated on the same
        # bar and ends by its own rule (the bar trades through VWAP)
        assert d["setups_cancelled_vwap_touch"] == 1

    def test_fill_cancels_the_opposite_setup(self):
        m = VwapReversionMachine(P)
        m.on_bar(bar(0, low=97.5, high=102.5, close=100.5, ema9=100.0))
        m.on_bar(bar(1, low=98.1, high=99.9, close=98.6, ema9=98.5))
        m.on_fill(2)
        assert m._setups == {1: None, -1: None}
        assert m.diagnostics()["setups_cancelled_position"] == 1
