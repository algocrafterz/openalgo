"""Strategy evolution verdict: improving, stagnating, regressing or exhausted."""

from signal_engine.research.evolution import assess_evolution


def _run(i, net, t=0.0, excess=None, trades=500):
    rec = {
        "run_id": f"R{i}",
        "date": f"2026-09-{i:02d}",
        "metrics": {"trades": trades, "net_bps": net, "t_stat": t, "gross_bps": net + 24},
    }
    if excess is not None:
        rec["baseline"] = {"excess_net_bps": excess}
    return rec


def test_too_few_runs_is_insufficient():
    out = assess_evolution([_run(1, -10), _run(2, -5)])
    assert out["state"] == "insufficient_history"
    assert out["action"] == "keep_iterating"


def test_clear_improvement():
    out = assess_evolution([_run(1, -40, 0.2), _run(2, -10, 0.9), _run(3, 25, 3.1)])
    assert out["state"] == "improving"
    assert out["action"] == "keep_iterating"


def test_regression_flags_revert():
    out = assess_evolution([_run(1, 30, 2.0), _run(2, 20, 1.5), _run(3, -5, 0.3)])
    assert out["state"] == "regressing"
    assert out["action"] == "revert_to_best"
    assert out["best_run"] == "R1"


def test_stagnating_but_positive_keeps_one_more_structural_change():
    out = assess_evolution([_run(1, 12, 1.0), _run(2, 13, 1.1), _run(3, 12.5, 1.0)])
    assert out["state"] == "stagnating"


def test_exhausted_when_many_runs_never_positive():
    runs = [_run(i, n, 0.1) for i, n in enumerate([-50, -40, -45, -38, -42], 1)]
    out = assess_evolution(runs)
    assert out["state"] == "exhausted"
    assert out["action"] == "stop_and_reject"


def test_baseline_excess_is_primary_metric_when_present():
    # net looks fine but entries are no better than random
    runs = [_run(i, 40, 2.0, excess=-e) for i, e in enumerate([30, 40, 50, 45], 1)]
    out = assess_evolution(runs)
    assert out["metric"] == "excess_net_bps"
    assert out["state"] == "exhausted"


def test_orders_by_date_not_input_order():
    out = assess_evolution([_run(3, 25, 3.1), _run(1, -40, 0.2), _run(2, -10, 0.9)])
    assert out["state"] == "improving"


def _with_t(runs, t):
    for r in runs:
        r["metrics"]["t_stat"] = t
        r["metrics"]["hurdle_t"] = 1.97
    return runs


def test_improving_below_the_bar_does_not_say_keep_iterating():
    runs = _with_t([_run(1, -40), _run(2, -10), _run(3, 25)], 0.9)
    out = assess_evolution(runs)
    assert out["state"] == "improving"
    assert out["action"] == "verify_before_tuning"
    assert "below the bar" in out["reason"]


def test_improving_and_clearing_the_bar_keeps_iterating():
    runs = _with_t([_run(1, -40), _run(2, -10), _run(3, 25)], 3.4)
    assert assess_evolution(runs)["action"] == "keep_iterating"


def test_no_edge_verdict_overrides_a_rising_trend():
    runs = _with_t([_run(1, -40), _run(2, -10), _run(3, 25)], 0.9)
    out = assess_evolution(runs, verdict="no-edge")
    assert out["state"] == "improving"
    assert out["action"] == "stop_and_reject"
    assert "no-edge" in out["reason"]
