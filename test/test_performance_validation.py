"""
Tests for the Strategy Validation Status (performance_stats.validation_status and its wiring).

The Live-mode inputs below are SYNTHETIC test fixtures. They exercise the rules; they are not
evidence that any real strategy has been validated on live fills.

Run with: uv run pytest test/test_performance_validation.py -v
"""

from datetime import date

import pytest

import services.strategy_daily_performance_service as svc
from services import performance_stats as ps
from services import strategy_trade_enrichment as enr
from services.performance_insights_service import build_insights, comparison_extras


def _kw(**over):
    """Inputs for a strategy that passes every check, in Live mode."""
    base = {
        "trades": 200,
        "sessions": 80,
        "net_pf": 1.8,
        "net_expectancy": 50.0,
        "edge_verdict": ps.VERDICT_SUPPORTED,
        "concentration_stats": {
            "traded_days": 70,
            "net": 10000.0,
            "top1_days_share_pct": 12.0,
            "net_ex_best_day": 8800.0,
            "net_ex_best_five_days": 5000.0,
        },
        "drawdown": {"max_drawdown": -500.0, "drawdown_recovered": True},
        "last_exp20": 30.0,
        "stressed_pf": 1.5,
        "slippage_coverage_share": 0.95,
        "mode": "live",
    }
    base.update(over)
    return base


def _by_id(v):
    return {c["id"]: c for c in v["checks"]}


# ---- the four states and their transitions ----------------------------------------------


def test_all_checks_passing_in_live_mode_is_validated():
    v = ps.validation_status(**_kw())
    assert v["status"] == "validated" and v["label"] == "Validated"
    assert v["passed_count"] == v["total_count"] and v["failed"] == [] and v["blocking"] == []
    assert v["next_level"] is None
    assert set(v["dimensions"].values()) == {"pass"}


def test_too_little_data_is_a_neutral_not_enough_data_state():
    for trades, sessions in ((29, 80), (200, 9), (0, 0)):
        v = ps.validation_status(**_kw(trades=trades, sessions=sessions))
        assert v["status"] == "not_enough_data" and v["label"] == "Not enough data yet"
        assert v["next_level"] == "preliminary" and v["blocking"] == ["sample"]
        assert v["failed"] == []  # nothing is judged, so nothing is failed
        judged = [c for c in v["checks"] if c["id"] not in ("sample", "history", "live_fills")]
        assert {c["status"] for c in judged} == {"pending"}


def test_enough_data_with_weak_results_is_not_encouraging_not_preliminary():
    v = ps.validation_status(**_kw(sessions=30, net_pf=0.8, net_expectancy=-4.0))
    assert v["status"] == "not_encouraging"
    assert "net_pf_above_1" in v["failed"] and "net_expectancy_positive" in v["failed"]
    assert v["next_level"] == "preliminary"


def test_encouraging_but_short_history_is_preliminary():
    v = ps.validation_status(**_kw(sessions=30, edge_verdict=ps.VERDICT_POSITIVE_INCONCLUSIVE))
    assert v["status"] == "preliminary"
    assert v["next_level"] == "under_validation"
    assert "history" in v["blocking"] and "history" in v["waiting"]
    assert v["failed"] == []  # short history is pending, never a failure


def test_an_edge_check_that_cannot_judge_yet_keeps_the_status_preliminary():
    for verdict in (ps.VERDICT_INSUFFICIENT, None):
        v = ps.validation_status(**_kw(edge_verdict=verdict))
        assert v["status"] == "preliminary", verdict
        assert v["failed"] == []  # waiting on the evidence is pending or not assessed, not failed
        assert "edge_not_negative" in v["blocking"]


@pytest.mark.parametrize("sessions", [30, 59, 60, 80, 250])
@pytest.mark.parametrize("verdict", [ps.VERDICT_NEGATIVE, ps.VERDICT_INCONCLUSIVE])
def test_positive_profit_figures_with_no_positive_edge_are_not_encouraging(sessions, verdict):
    """The scenario that used to fall back to Preliminary: enough history, profit factor above 1
    and a positive average, but the edge check does not show a positive average."""
    v = ps.validation_status(**_kw(sessions=sessions, edge_verdict=verdict))
    c = _by_id(v)
    assert (
        c["net_pf_above_1"]["status"] == "pass" and c["net_expectancy_positive"]["status"] == "pass"
    )
    assert v["status"] == "not_encouraging" and v["label"] == "Not encouraging so far"
    assert "edge_not_negative" in v["failed"] and "edge_not_negative" in v["blocking"]
    assert "not encouraging" in v["summary"] and "edge check" in v["summary"]
    assert v["next_level"] == "preliminary"


def test_negative_and_inconclusive_evidence_stay_distinct():
    neg = ps.validation_status(**_kw(edge_verdict=ps.VERDICT_NEGATIVE))
    inc = ps.validation_status(**_kw(edge_verdict=ps.VERDICT_INCONCLUSIVE, net_expectancy=-1.0))
    assert neg["status"] == inc["status"] == "not_encouraging"
    assert "negative" in _by_id(neg)["edge_not_negative"]["detail"]
    assert "does not settle" in _by_id(inc)["edge_not_negative"]["detail"]
    assert "is negative" in neg["summary"] and "is negative" not in inc["summary"]
    assert "does not show a positive" in inc["summary"]


def test_the_history_boundary_does_not_change_the_edge_rule():
    """59 and 60 sessions differ only in the history check, never in how a failed edge is treated."""
    for verdict in (ps.VERDICT_NEGATIVE, ps.VERDICT_INCONCLUSIVE):
        at59 = ps.validation_status(**_kw(sessions=59, edge_verdict=verdict))
        at60 = ps.validation_status(**_kw(sessions=60, edge_verdict=verdict))
        assert at59["status"] == at60["status"] == "not_encouraging"
        assert _by_id(at59)["history"]["status"] == "pending"
        assert _by_id(at60)["history"]["status"] == "pass"
    ok59 = ps.validation_status(**_kw(sessions=59, edge_verdict=ps.VERDICT_POSITIVE_INCONCLUSIVE))
    ok60 = ps.validation_status(**_kw(sessions=60, edge_verdict=ps.VERDICT_POSITIVE_INCONCLUSIVE))
    assert ok59["status"] == "preliminary" and ok60["status"] == "under_validation"


def test_a_positive_edge_verdict_never_triggers_not_encouraging():
    for verdict in (ps.VERDICT_SUPPORTED, ps.VERDICT_POSITIVE_INCONCLUSIVE):
        assert ps.validation_status(**_kw(edge_verdict=verdict))["status"] != "not_encouraging"


def test_failed_edge_and_weak_profit_figures_are_both_listed_as_blockers():
    v = ps.validation_status(
        **_kw(net_pf=0.9, net_expectancy=-2.0, edge_verdict=ps.VERDICT_NEGATIVE)
    )
    assert v["status"] == "not_encouraging"
    assert set(v["blocking"]) == {"net_pf_above_1", "net_expectancy_positive", "edge_not_negative"}


def test_below_the_sample_minimum_a_negative_verdict_is_not_judged():
    v = ps.validation_status(**_kw(trades=20, sessions=8, edge_verdict=ps.VERDICT_NEGATIVE))
    assert v["status"] == "not_enough_data" and v["failed"] == []


def test_service_level_negative_edge_is_not_encouraging():
    """End to end through build_insights: profitable-looking trades, but a negative edge result."""
    a = _steady("STEADY")
    forced = dict(_edge(a), edge_verdict=ps.VERDICT_NEGATIVE)
    v = build_insights(a, SESSIONS, None, None, forced, "live")["validation"]
    assert v["status"] == "not_encouraging" and "edge_not_negative" in v["failed"]


def test_positive_but_inconclusive_statistics_is_under_validation():
    v = ps.validation_status(**_kw(edge_verdict=ps.VERDICT_POSITIVE_INCONCLUSIVE))
    assert v["status"] == "under_validation"
    c = _by_id(v)
    assert c["edge_not_negative"]["status"] == "pass"
    assert c["edge_supported"]["status"] == "pending"
    assert v["blocking"] == ["edge_supported"] and v["failed"] == []
    assert v["dimensions"]["statistical"] == "pending"
    assert v["dimensions"]["performance"] == "pass" and v["dimensions"]["robustness"] == "pass"


def test_strong_statistics_but_failed_robustness_is_under_validation_not_downgraded():
    conc = {
        "traded_days": 70, "net": 10000.0, "top1_days_share_pct": 55.0,
        "net_ex_best_day": 4500.0, "net_ex_best_five_days": -300.0,
    }  # fmt: skip
    v = ps.validation_status(**_kw(concentration_stats=conc, last_exp20=-5.0))
    assert v["status"] == "under_validation"  # blocks Validated, does not hide or downgrade
    assert set(v["failed"]) == {"best_day_share", "positive_ex_best_five", "recent_expectancy"}
    assert _by_id(v)["edge_supported"]["status"] == "pass"
    assert v["dimensions"]["robustness"] == "fail" and v["dimensions"]["statistical"] == "pass"
    assert v["next_level"] == "validated" and set(v["failed"]) <= set(v["blocking"])


# ---- execution evidence -----------------------------------------------------------------


def test_sandbox_mode_can_never_be_validated():
    v = ps.validation_status(**_kw(mode="analyze"))
    assert v["status"] == "under_validation"
    live = _by_id(v)["live_fills"]
    assert live["status"] == "not_assessed" and "simulated" in live["detail"]
    assert v["blocking"] == ["live_fills"] and v["waiting"] == ["live_fills"]
    assert v["dimensions"]["execution"] == "not_assessed"


def test_unknown_mode_is_not_assessed_not_a_pass():
    v = ps.validation_status(**_kw(mode=None))
    assert _by_id(v)["live_fills"]["status"] == "not_assessed"
    assert v["status"] == "under_validation"


def test_cost_stress_and_slippage_coverage_are_separate_execution_checks():
    v = ps.validation_status(**_kw(stressed_pf=0.95, slippage_coverage_share=0.6))
    assert set(v["failed"]) == {"cost_stress", "slippage_coverage"}
    assert v["status"] == "under_validation" and v["dimensions"]["execution"] == "fail"
    edge_case = ps.validation_status(**_kw(slippage_coverage_share=0.8))
    assert _by_id(edge_case)["slippage_coverage"]["status"] == "pass"  # exactly 80% passes
    assert _by_id(ps.validation_status(**_kw(stressed_pf=1.0)))["cost_stress"]["status"] == "fail"


def test_slippage_coverage_counts_only_trades_with_a_reliable_entry_time():
    rows = [
        {"entry_slippage": 1.0, "data_flag": None},
        {"entry_slippage": None, "data_flag": None},
        {"entry_slippage": 5.0, "data_flag": "overnight_mis"},  # unreliable: not counted
        {"entry_slippage": None, "data_flag": "missing_entry_time"},
    ]
    assert ps.slippage_coverage(rows) == 0.5
    assert ps.slippage_coverage([{"entry_slippage": 1.0, "data_flag": "overnight_mis"}]) is None


def test_stressed_profit_factor_raises_charges_by_half():
    rows = [
        {"net": 90.0, "cost": 10.0},
        {"net": -40.0, "cost": 10.0},
        {"net": 50.0, "cost": 0.0},
    ]
    # extra 5 on the two costed trades: gains 85 + 50, losses 45
    assert ps.stressed_profit_factor(rows) == pytest.approx(135 / 45)
    assert ps.stressed_profit_factor(rows, 0.0) == pytest.approx(140 / 40)


# ---- missing evidence never passes ------------------------------------------------------


def test_missing_evidence_is_not_assessed_and_never_validated():
    v = ps.validation_status(
        **_kw(
            net_pf=None, net_expectancy=None, edge_verdict=None, concentration_stats=None,
            drawdown=None, stressed_pf=None, slippage_coverage_share=None,
        )
    )  # fmt: skip
    c = _by_id(v)
    for cid in ("net_pf_above_1", "net_expectancy_positive", "net_pf_validated", "edge_supported",
                "best_day_share", "positive_ex_best_day", "drawdown_recovered", "cost_stress",
                "slippage_coverage"):  # fmt: skip
        assert c[cid]["status"] == "not_assessed", cid
    assert v["status"] == "not_enough_data"  # unavailable figures are not judged, never passed
    assert "not available" in v["summary"]
    assert v["failed"] == []


def test_a_single_unavailable_metric_blocks_validated():
    v = ps.validation_status(**_kw(drawdown=None))
    assert v["status"] == "under_validation"
    assert _by_id(v)["drawdown_recovered"]["status"] == "not_assessed"
    assert "drawdown_recovered" in v["waiting"] and "drawdown_recovered" not in v["failed"]


def test_best_five_days_needs_more_than_five_traded_days():
    conc = dict(_kw()["concentration_stats"], net_ex_best_five_days=None)
    v = ps.validation_status(**_kw(concentration_stats=conc))
    assert _by_id(v)["positive_ex_best_five"]["status"] == "pending"
    assert v["status"] == "under_validation"


def test_no_net_profit_to_spread_fails_the_best_day_check():
    conc = dict(_kw()["concentration_stats"], top1_days_share_pct=None, net=-50.0)
    assert (
        _by_id(ps.validation_status(**_kw(concentration_stats=conc)))["best_day_share"]["status"]
        == "fail"
    )


def test_drawdown_states():
    flat = ps.validation_status(**_kw(drawdown={"max_drawdown": 0.0}))
    assert _by_id(flat)["drawdown_recovered"]["status"] == "pass"
    deep = ps.validation_status(
        **_kw(drawdown={"max_drawdown": -900.0, "drawdown_recovered": False})
    )
    assert _by_id(deep)["drawdown_recovered"]["status"] == "fail"


def test_failed_pending_and_not_assessed_are_listed_separately_with_plain_reasons():
    v = ps.validation_status(
        **_kw(mode="analyze", edge_verdict=ps.VERDICT_POSITIVE_INCONCLUSIVE, last_exp20=-1.0)
    )
    assert v["failed"] == ["recent_expectancy"]
    assert set(v["waiting"]) == {"edge_supported", "live_fills"}
    for c in v["checks"]:
        assert c["detail"] and "_" not in c["detail"] and "HTTP" not in c["detail"]


def test_detail_text_has_no_rupee_amounts_so_capital_scaling_cannot_make_it_stale():
    v = ps.validation_status(**_kw())
    assert not any("Rs" in c["detail"] or "\u20b9" in c["detail"] for c in v["checks"])


# ---- combined portfolio versus an individual strategy ------------------------------------

SESSIONS = ps.weekday_sessions("2026-01-05", "2026-04-10")  # 70 weekdays


def _trade(strategy, day, pnl, i):
    return {
        "strategy": strategy, "symbol": "AAA", "trade_date": day,
        "opened_at": f"{day}T09:30:00", "closed_at": f"{day}T10:{i % 60:02d}:00",
        "direction": "LONG", "entry_price": 100.0, "closed_quantity": 10.0,
        "realized_pnl": pnl, "cost": 5.0, "net_r_multiple": None,
        "entry_slippage": 1.0, "data_flag": None,
    }  # fmt: skip


def _steady(name):
    out = []
    for i, day in enumerate(SESSIONS):
        out.append(_trade(name, day, 100.0 + (i % 5) * 4, 0))
        out.append(_trade(name, day, -20.0 - (i % 3), 1))
    return out


def _one_big_day(name):
    out = [_trade(name, day, -30.0, i) for i, day in enumerate(SESSIONS)]
    out[10] = _trade(name, SESSIONS[10], 100000.0, 10)
    return out


def _edge(trades):
    nets = [t["realized_pnl"] - t["cost"] for t in trades]
    return ps.edge_stats(nets, trade_dates=[t["trade_date"] for t in trades])


def test_portfolio_and_each_strategy_are_assessed_by_the_same_rules_and_can_differ():
    a, b = _steady("STEADY"), _one_big_day("LUMPY")
    both = a + b
    x = comparison_extras(
        {"STEADY": a, "LUMPY": b},
        SESSIONS,
        {},
        {"STEADY": _edge(a), "LUMPY": _edge(b), "__portfolio__": _edge(both)},
        "live",
    )
    steady, lumpy, port = x["per_strategy"]["STEADY"], x["per_strategy"]["LUMPY"], x["portfolio"]
    assert steady["validation_status"] == "validated"
    assert lumpy["validation_status"] == "under_validation"
    assert port["validation_status"] == "under_validation"  # LUMPY's one day dominates the whole
    for row in (steady, lumpy, port):
        assert row["validation_label"] and row["validation_summary"]


def test_single_strategy_view_matches_its_comparison_row_and_sandbox_caps_it():
    a = _steady("STEADY")
    live = build_insights(a, SESSIONS, None, None, _edge(a), "live")["validation"]
    row_live = comparison_extras({"STEADY": a}, SESSIONS, {}, {"STEADY": _edge(a)}, "live")
    assert live["status"] == "validated"
    assert row_live["per_strategy"]["STEADY"]["validation_status"] == live["status"]
    sandbox = build_insights(a, SESSIONS, None, None, _edge(a), "analyze")["validation"]
    assert sandbox["status"] == "under_validation" and sandbox["blocking"] == ["live_fills"]


def test_build_insights_without_an_edge_result_does_not_pass_the_statistical_checks():
    a = _steady("STEADY")
    v = build_insights(a, SESSIONS, None, None, None, "live")["validation"]
    assert _by_id(v)["edge_supported"]["status"] == "not_assessed"
    assert v["status"] != "validated"


def test_too_few_trades_in_a_selection_gives_not_enough_data():
    a = _steady("STEADY")[:20]
    v = build_insights(a, SESSIONS[:10], None, None, _edge(a), "live")["validation"]
    assert v["status"] == "not_enough_data"


# ---- the service: filters and mode --------------------------------------------------------


def _ledger_trade(i, strategy, pnl, day):
    return {
        "strategy": strategy, "symbol": f"S{i % 4}", "exchange": "NSE", "product": "MIS",
        "mode": "analyze", "direction": "LONG", "closed_quantity": 10.0, "entry_price": 100.0,
        "exit_price": 100.0 + pnl / 10, "realized_pnl": pnl, "trade_date": day,
        "opened_at": f"{day}T09:30:00", "closed_at": f"{day}T11:{i % 60:02d}:00",
    }  # fmt: skip


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    trades = [
        _ledger_trade(
            i, "ORB" if i % 2 else "VWAP", 80.0 if i % 3 else -60.0, f"2026-10-{i % 12 + 1:02d}"
        )
        for i in range(48)
    ]
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: [dict(t) for t in trades])
    monkeypatch.setattr(svc, "_current_mode", lambda: "analyze")
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 10, 30))
    monkeypatch.setattr(enr, "SIGNAL_TRADES_DB", tmp_path / "absent.db")
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    return trades


def test_daily_response_carries_validation_and_follows_the_strategy_filter(ledger):
    _, both, _ = svc.get_daily_performance(None, "all")
    _, orb, _ = svc.get_daily_performance("ORB", "all")
    for r in (both, orb):
        v = r["insights"]["validation"]
        assert v["status"] in ps.VALIDATION_LABELS and v["mode"] == "analyze"
        assert _by_id(v)["live_fills"]["status"] == "not_assessed"  # Sandbox ledger
    # the filter reaches the assessment: ORB's own 24 trades, not the combined 48
    assert "has 24 trades" in orb["insights"]["validation"]["checks"][0]["detail"]
    assert "has 48 trades" in both["insights"]["validation"]["checks"][0]["detail"]


@pytest.fixture
def big_ledger(monkeypatch, tmp_path):
    """60 trades per strategy over 20 days, so every selection is past the sample minimum and the
    assessment really judges (rather than returning Not enough data yet)."""
    trades = [
        _ledger_trade(
            i, "ORB" if i % 2 else "VWAP", 80.0 if i % 3 else -60.0, f"2026-10-{i % 20 + 1:02d}"
        )
        for i in range(120)
    ]
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: [dict(t) for t in trades])
    monkeypatch.setattr(svc, "_current_mode", lambda: "analyze")
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 10, 30))
    monkeypatch.setattr(enr, "SIGNAL_TRADES_DB", tmp_path / "absent.db")
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    return trades


def test_comparison_rows_and_the_single_strategy_view_agree_on_a_judged_status(big_ledger):
    _, cmp_, _ = svc.get_strategy_comparison("all")
    rows = {r["strategy"]: r for r in cmp_["strategies"]}
    for name in ("ORB", "VWAP"):
        _, single, _ = svc.get_daily_performance(name, "all")
        v = single["insights"]["validation"]
        assert v["status"] != "not_enough_data"  # judged, so the comparison is not vacuous
        assert rows[name]["validation_status"] == v["status"]
        assert rows[name]["validation_summary"] == v["summary"]
    _, both, _ = svc.get_daily_performance(None, "all")
    port = cmp_["portfolio"]
    assert port["validation_status"] == both["insights"]["validation"]["status"]
    assert port["validation_summary"] == both["insights"]["validation"]["summary"]


def test_capital_does_not_change_the_validation(ledger):
    _, plain, _ = svc.get_daily_performance(None, "all")
    _, with_cap, _ = svc.get_daily_performance(None, "all", capital=250000.0)
    assert plain["insights"]["validation"] == with_cap["insights"]["validation"]


# ---- boundaries and bad inputs (from the final review) ------------------------------------


def test_net_profit_factor_of_exactly_one_is_not_above_one():
    c = _by_id(ps.validation_status(**_kw(net_pf=1.0)))
    assert c["net_pf_above_1"]["status"] == "fail"
    assert _by_id(ps.validation_status(**_kw(net_pf=1.0001)))["net_pf_above_1"]["status"] == "pass"
    # validated level: more than 1.3
    assert _by_id(ps.validation_status(**_kw(net_pf=1.3)))["net_pf_validated"]["status"] == "fail"
    assert (
        _by_id(ps.validation_status(**_kw(net_pf=1.3001)))["net_pf_validated"]["status"] == "pass"
    )


def test_a_net_profit_factor_of_one_is_not_encouraging():
    assert ps.validation_status(**_kw(net_pf=1.0))["status"] == "not_encouraging"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_unavailable_evidence_not_a_pass_or_a_failure(bad):
    v = ps.validation_status(
        **_kw(net_pf=bad, net_expectancy=bad, last_exp20=bad, stressed_pf=bad,
              slippage_coverage_share=bad)
    )  # fmt: skip
    c = _by_id(v)
    for cid in ("net_pf_above_1", "net_expectancy_positive", "net_pf_validated", "cost_stress",
                "slippage_coverage"):  # fmt: skip
        assert c[cid]["status"] == "not_assessed", cid
    assert c["recent_expectancy"]["status"] == "pending"
    assert v["failed"] == [] and v["status"] == "not_enough_data"
    assert "nan" not in " ".join(x["detail"] for x in v["checks"]).lower()


def test_detail_text_shows_enough_precision_to_explain_a_boundary_failure():
    just_below = _by_id(ps.validation_status(**_kw(net_pf=1.2999)))["net_pf_validated"]
    assert just_below["status"] == "fail" and "1.2999" in just_below["detail"]
    low_cov = _by_id(ps.validation_status(**_kw(slippage_coverage_share=0.796)))[
        "slippage_coverage"
    ]
    assert low_cov["status"] == "fail" and "79.6%" in low_cov["detail"]


def test_every_trade_the_same_positive_result_stays_not_encouraging_with_an_honest_reason():
    """No spread: the edge check returns inconclusive on a POSITIVE average. It still cannot
    show a positive edge, so the status is Not encouraging, but the text must not say the
    average is not positive."""
    v = ps.validation_status(**_kw(edge_verdict=ps.VERDICT_INCONCLUSIVE, net_expectancy=10.0))
    assert v["status"] == "not_encouraging" and "edge_not_negative" in v["failed"]
    detail = _by_id(v)["edge_not_negative"]["detail"]
    assert "same positive result" in detail and "not positive" not in detail
    flat = ps.edge_stats(
        [10.0] * 40, trade_dates=[f"2026-10-{d + 1:02d}" for d in range(20) for _ in (0, 1)]
    )
    assert flat["edge_verdict"] == ps.VERDICT_INCONCLUSIVE  # the real edge check returns this
    # a non-positive average with the same verdict gets the other wording
    bad = ps.validation_status(**_kw(edge_verdict=ps.VERDICT_INCONCLUSIVE, net_expectancy=-1.0))
    assert bad["status"] == "not_encouraging"
    assert "not positive" in _by_id(bad)["edge_not_negative"]["detail"]
