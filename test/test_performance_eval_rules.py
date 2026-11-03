"""The rule-based report: closed kinds, no judgement words, no verdict for tiny samples,
underpowered buckets never ranked, deterministic, built from the dataset only.

Run with: PYTHONPATH=. uv run pytest test/test_performance_eval_rules.py -v
"""

import copy
import json
import re
from datetime import date, timedelta

import pytest

import services.performance_eval.dataset as ds
import services.performance_eval.rules as rules
import services.strategy_daily_performance_service as svc

TODAY = date.today()
JUDGEMENT = re.compile(
    r"\b(good|bad|poor|average|profitable|unprofitable|excellent|weak|strong)\b", re.I
)


def _trade(strategy, i, pnl, mode="analyze", days_back=None, hour=None):
    day = TODAY - timedelta(days=(i % 20) if days_back is None else days_back)
    hour = 9 + (i % 3) if hour is None else hour
    return {
        "strategy": strategy,
        "symbol": f"SYM{i % 4}",
        "exchange": "NSE",
        "product": "MIS",
        "mode": mode,
        "direction": "LONG",
        "closed_quantity": 10.0,
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl / 10,
        "realized_pnl": pnl,
        "trade_date": day.isoformat(),
        "opened_at": f"{day.isoformat()}T{hour:02d}:00:00",
        "closed_at": f"{day.isoformat()}T{hour:02d}:40:00",
    }


def _series(strategy, n, kind="mixed"):
    out = []
    for i in range(n):
        if kind == "up":
            pnl = 100.0 + (i * 7 % 13)
        elif kind == "down":
            pnl = -100.0 - (i * 7 % 13)
        elif kind == "flat":
            pnl = 10.0 if i % 2 == 0 else -10.0
        elif kind == "same":
            pnl = 50.0
        else:
            pnl = 40.0 + (i * 7 % 13) if i % 3 else -35.0 - (i % 5)
        out.append(_trade(strategy, i, pnl))
    return out


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    state = {"trades": []}

    def get_closed_trades(mode=None, **_kw):
        return [copy.deepcopy(t) for t in state["trades"] if t["mode"] == mode]

    monkeypatch.setattr(svc, "get_closed_trades", get_closed_trades)
    monkeypatch.setattr(svc, "_current_mode", lambda: "analyze")
    monkeypatch.setattr(
        "services.strategy_trade_enrichment.SIGNAL_TRADES_DB", tmp_path / "absent.db"
    )
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))

    def setter(trades):
        state["trades"] = trades

    return setter


def _dataset(**kw):
    ok, data, _ = ds.build_evaluation_dataset(
        kw.get("include") or [],
        kw.get("exclude") or [],
        kw.get("strategy"),
        "90d",
        None,
        None,
        None,
    )
    assert ok, data
    return data


def _findings(report):
    return [f for s in report["sections"] for f in s["findings"]]


def _section(report, sid):
    return next(s for s in report["sections"] if s["id"] == sid)


def test_section_ids_are_the_planned_ones(ledger):
    ledger(_series("ALPHA", 40))
    report = rules.build_report(_dataset())
    assert [s["id"] for s in report["sections"]] == [
        "profitability",
        "expectancy_edge",
        "profit_factor_win_rate",
        "drawdown_recovery",
        "consistency",
        "entry_hour",
        "holding_duration",
        "market_conditions",
        "sample_size",
        "weaknesses",
    ]
    assert report["schema_version"] == 1
    assert set(report["legend"]) == {"math", "heuristic", "not_available"}


@pytest.mark.parametrize(
    "n,kind",
    [
        (1, "up"),
        (5, "up"),
        (20, "mixed"),
        (29, "up"),
        (30, "flat"),
        (100, "up"),
        (100, "down"),
        (40, "same"),
        (40, "flat"),
    ],
)
def test_every_finding_has_a_closed_kind_and_n(ledger, n, kind):
    ledger(_series("ALPHA", n, kind))
    report = rules.build_report(_dataset())
    assert report["scope"]["n_trades"] == n
    for finding in _findings(report):
        assert finding["kind"] in rules.KINDS
        assert isinstance(finding["n"], int) and finding["n"] >= 0
        assert finding["statement"].strip()
        assert isinstance(finding["evidence"], list)


@pytest.mark.parametrize(
    "n,kind", [(5, "up"), (29, "up"), (30, "flat"), (100, "up"), (100, "down")]
)
def test_math_findings_use_no_judgement_words(ledger, n, kind):
    ledger(_series("ALPHA", n, kind))
    for finding in _findings(rules.build_report(_dataset())):
        if finding["kind"] == rules.KIND_MATH:
            assert not JUDGEMENT.search(finding["statement"]), finding["statement"]


def test_no_rating_scale_anywhere(ledger):
    ledger(_series("ALPHA", 100, "up"))
    text = json.dumps(rules.build_report(_dataset()))
    for word in ("Good", "Average", "Poor"):
        assert word not in text
    assert "profitable" not in text.lower()


@pytest.mark.parametrize("n", [1, 5, 20, 29])
def test_insufficient_strategy_gets_numbers_and_one_sentence_only(ledger, n):
    ledger(_series("ALPHA", n, "up"))
    report = rules.build_report(_dataset())
    assert report["scope"]["labels"]["pooled"] == ds.LABEL_INSUFFICIENT
    assert report["scope"]["labels"]["per_strategy"] == {"ALPHA": ds.LABEL_INSUFFICIENT}
    for finding in _findings(report):
        if finding["id"].startswith("weaknesses"):
            continue
        # No heuristic flag may fire for a sample this small.
        assert not finding.get("flagged"), finding
    weaknesses = _section(report, "weaknesses")["findings"]
    assert [f["id"] for f in weaknesses] == ["weaknesses.none"]
    assert rules.NOTHING_FLAGGED in weaknesses[0]["statement"]
    assert any(rules.TOO_FEW in f["statement"] for f in _findings(report))


def test_insufficient_strategy_is_silent_in_a_mixed_selection(ledger):
    ledger(_series("BIG", 100, "up") + _series("TINY", 10, "down"))
    report = rules.build_report(_dataset())
    labels = report["scope"]["labels"]["per_strategy"]
    assert labels["TINY"] == ds.LABEL_INSUFFICIENT
    for finding in _findings(report):
        if finding.get("scope") == "TINY":
            assert not finding.get("flagged")
            assert finding["kind"] != rules.KIND_HEURISTIC or not finding.get("flagged")
    assert not any(f.get("scope") == "TINY" for f in _section(report, "weaknesses")["findings"])


def test_underpowered_buckets_are_never_ranked(ledger):
    ledger(_series("ALPHA", 100, "mixed"))
    data = _dataset()
    report = rules.build_report(data)
    # 100 trades spread over 20 days and 5 weekdays: every weekday bucket is under 30.
    assert all(r["underpowered"] for r in data["buckets"]["by_weekday"]["rows"])
    spread = next(f for f in _findings(report) if f["id"] == "bucket_spread.by_weekday")
    assert "highest gross P&L" not in spread["statement"]
    assert "lowest" not in spread["statement"]
    assert not any(f["id"] == "weaknesses.bucket.by_weekday" for f in _findings(report))


def test_no_bucket_reaches_the_minimum_states_so(ledger):
    ledger(_series("ALPHA", 60, "mixed"))
    data = _dataset()
    assert all(r["n"] < 30 for r in data["buckets"]["by_weekday"]["rows"])
    report = rules.build_report(data)
    spread = next(f for f in _findings(report) if f["id"] == "bucket_spread.by_weekday")
    assert "No bucket has enough trades to compare (need 30 each)" in spread["statement"]
    assert "treat every slice as exploratory" in spread["statement"]
    assert spread["kind"] == rules.KIND_HEURISTIC
    assert spread["rule"]["name"] == "bucket_spread"
    assert spread["rule"]["parameters"]["min_trades_per_bucket"] == 30
    assert spread["note"] == rules.MULTIPLE_COMPARISON


def test_a_bucket_with_enough_trades_is_ranked_and_the_small_one_is_exploratory(ledger):
    trades = [_trade("ALPHA", i, 20.0 if i % 2 else -5.0, hour=10) for i in range(40)]
    trades += [_trade("ALPHA", 100 + i, -50.0, hour=14) for i in range(3)]
    ledger(trades)
    data = _dataset()
    report = rules.build_report(data)
    spread = next(f for f in _findings(report) if f["id"] == "bucket_spread.by_entry_hour")
    assert "only one bucket has at least 30 trades" in spread["statement"]
    small = next(
        f for f in _findings(report) if f["id"] == "bucket_spread.by_entry_hour.exploratory"
    )
    assert "14 (n=3)" in small["statement"]
    assert small["note"] == "Not ranked and not a finding."
    assert small["n"] == 3


def test_a_bucket_of_exactly_29_is_exploratory_and_30_is_ranked(ledger):
    for count, ranked in ((29, False), (30, True)):
        ledger(
            [_trade("ALPHA", i, 10.0, hour=10) for i in range(count)]
            + [_trade("ALPHA", 200 + i, 10.0, hour=11) for i in range(60)]
        )
        report = rules.build_report(_dataset())
        findings = {f["id"]: f for f in _findings(report)}
        exploratory = findings.get("bucket_spread.by_entry_hour.exploratory")
        assert (exploratory is None) == ranked
        if not ranked:
            assert "10 (n=29)" in exploratory["statement"]


def test_market_conditions_is_not_available(ledger):
    ledger(_series("ALPHA", 100, "up"))
    findings = _section(rules.build_report(_dataset()), "market_conditions")["findings"]
    assert len(findings) == 1
    assert findings[0]["kind"] == rules.KIND_NOT_AVAILABLE
    assert "no volatility, trend or market-regime" in findings[0]["statement"]
    assert "weekday and hour" in findings[0]["statement"]


def test_heuristic_findings_carry_rule_parameters_and_n(ledger):
    ledger(_series("ALPHA", 100, "mixed") + _series("BETA", 60, "down"))
    report = rules.build_report(_dataset())
    heuristics = [f for f in _findings(report) if f["kind"] == rules.KIND_HEURISTIC]
    assert heuristics
    for finding in heuristics:
        if finding["id"] == "weaknesses.none":
            continue
        assert finding["rule"]["name"]
        assert isinstance(finding["rule"]["parameters"], dict)
        assert finding["note"]
        assert isinstance(finding["n"], int)


def test_drawdown_larger_than_profit_is_flagged_as_a_plain_comparison(ledger):
    trades = [_trade("ALPHA", i, 30.0, days_back=i % 20) for i in range(40)]
    trades += [_trade("ALPHA", 100 + i, -200.0, days_back=10) for i in range(4)]
    ledger(trades)
    data = _dataset()
    pooled = data["pooled"]
    report = rules.build_report(data)
    item = next(f for f in _findings(report) if f["id"] == "drawdown_recovery.rule.0")
    assert pooled["net_profit"] > 0
    assert item["rule"]["name"] == "drawdown_vs_profit"
    assert item["flagged"] == (abs(pooled["max_drawdown"]) > pooled["net_profit"])
    assert item["flagged"] is True
    assert item["kind"] == rules.KIND_HEURISTIC


def _scope(values, label=ds.LABEL_NO_EDGE, n=60):
    return rules._Scope(
        "pooled", label, n, {key: (value, f"pooled.{key}") for key, value in values.items()}
    )


@pytest.mark.parametrize(
    ("dd", "net", "flagged"),
    [(-500.0, 100.0, True), (-50.0, 100.0, False), (0.0, 100.0, False)],
)
def test_drawdown_rule_runs_only_for_positive_net_profit(dd, net, flagged):
    item = rules._drawdown_vs_profit(_scope({"max_drawdown": dd, "net_profit": net}), "x")
    assert item["kind"] == rules.KIND_HEURISTIC
    assert item["flagged"] is flagged


@pytest.mark.parametrize("net", [0.0, -100.0])
def test_drawdown_with_no_profit_is_a_math_statement(net):
    item = rules._drawdown_vs_profit(_scope({"max_drawdown": -500.0, "net_profit": net}), "x")
    assert item["kind"] == rules.KIND_MATH
    assert "flagged" not in item and "rule" not in item
    assert "Net profit is zero or negative, so there is no profit to compare" in item["statement"]
    assert "500" in item["statement"]
    assert item["evidence"] == ["pooled.max_drawdown", "pooled.net_profit"]


def test_a_loss_making_selection_adds_no_drawdown_weakness(ledger):
    ledger([_trade("ALPHA", i, -30.0) for i in range(40)])
    report = rules.build_report(_dataset())
    assert not any("drawdown" in f["id"] for f in _section(report, "weaknesses")["findings"])


def test_concentration_states_its_definition():
    scope = _scope({"best_day": 600.0, "gross_profit": 1000.0})
    item = rules._concentration(scope, "c")
    assert "the best trading day is 0.60 of gross profit" in item["statement"]
    assert "best 1 trading day" not in item["statement"]
    assert rules.CONCENTRATION_DEFINITION in item["statement"]
    assert item["rule"]["parameters"]["formula"] == rules.CONCENTRATION_DEFINITION
    assert item["rule"]["parameters"]["top_days"] == 1
    assert "the sum of winning trades" in rules.CONCENTRATION_DEFINITION


def test_cost_drag_without_a_percentage_omits_that_clause():
    scope = _scope({"est_costs": 120.0, "net_profit": 500.0, "net_after_costs": 380.0})
    first = rules._cost_drag(scope, "k")[0]
    assert "percent of gross" not in first["statement"]
    assert "None" not in first["statement"]
    assert first["evidence"] == ["pooled.est_costs"]


def test_no_finding_carries_an_empty_evidence_path(ledger):
    ledger(_series("ALPHA", 60))
    report = rules.build_report(_dataset())
    for finding in _findings(report):
        assert all(path for path in finding["evidence"]), finding["id"]


def test_the_nothing_flagged_finding_is_math():
    item = rules._finding("weaknesses.none", rules.KIND_MATH, rules.NOTHING_FLAGGED, 3, [])
    assert item["kind"] == rules.KIND_MATH and item["evidence"] == []


def test_profit_concentration_flag_follows_the_named_constant(ledger):
    spike = [_trade("ALPHA", i, 1.0, days_back=1 + i % 10) for i in range(40)]
    spike += [_trade("ALPHA", 100 + i, 500.0, days_back=0) for i in range(2)]
    ledger(spike)
    report = rules.build_report(_dataset())
    item = next(f for f in _findings(report) if f["id"] == "consistency.concentration")
    assert item["rule"]["parameters"]["flag_at_share"] == rules.CONCENTRATION_FLAG
    assert item["flagged"] is True
    assert any(f["id"] == "weaknesses.consistency.concentration" for f in _findings(report))

    ledger([_trade("ALPHA", i, 10.0, days_back=i % 20) for i in range(60)])
    flat = next(
        f
        for f in _findings(rules.build_report(_dataset()))
        if f["id"] == "consistency.concentration"
    )
    assert flat["flagged"] is False


def test_loss_streak_flag_follows_the_named_constant(ledger):
    trades = [_trade("ALPHA", i, 20.0, days_back=10 + i % 20) for i in range(40)]
    trades += [_trade("ALPHA", 100 + d, -10.0, days_back=d) for d in range(rules.LOSS_STREAK_FLAG)]
    ledger(trades)
    report = rules.build_report(_dataset())
    item = next(f for f in _findings(report) if f["id"] == "consistency.loss_streak.0")
    assert item["rule"]["parameters"]["flag_at_days"] == rules.LOSS_STREAK_FLAG
    assert item["flagged"] is True


def test_heuristic_constants_are_named_and_plain():
    assert rules.CONCENTRATION_FLAG == 0.5
    assert rules.LOSS_STREAK_FLAG == 4
    assert rules.BUCKET_MIN_TRADES == 30 == ds.BUCKET_MIN_TRADES
    assert rules.CONCENTRATION_TOP_DAYS == 1
    assert set(rules.HEURISTIC_CONSTANTS) == {
        "CONCENTRATION_TOP_DAYS",
        "CONCENTRATION_FLAG",
        "LOSS_STREAK_FLAG",
        "BUCKET_MIN_TRADES",
    }


def test_output_is_deterministic(ledger):
    ledger(_series("ALPHA", 80) + _series("BETA", 33, "up"))
    data = _dataset()
    first = json.dumps(rules.build_report(data), sort_keys=True)
    second = json.dumps(rules.build_report(copy.deepcopy(data)), sort_keys=True)
    assert first == second


def test_report_does_not_change_the_dataset(ledger):
    ledger(_series("ALPHA", 80))
    data = _dataset()
    before = copy.deepcopy(data)
    rules.build_report(data)
    assert data == before


def test_excluded_strategies_are_absent(ledger):
    ledger(_series("KEEP", 40) + _series("HIDDEN-EXCLUDED-XYZ", 40, "down"))
    report = rules.build_report(_dataset(exclude=["HIDDEN-EXCLUDED-XYZ"]))
    assert "HIDDEN-EXCLUDED-XYZ" not in json.dumps(report)
    assert report["scope"]["strategies"] == 1


def test_report_is_built_from_the_dataset_only():
    data = {
        "pooled": {"trades_count": 0, "evidence_label": ds.LABEL_INSUFFICIENT},
        "strategies": [],
        "buckets": {},
    }
    report = rules.build_report(data)
    assert report["scope"]["n_trades"] == 0
    assert all(f["n"] >= 0 for f in _findings(report))
    assert _section(report, "weaknesses")["findings"][0]["id"] == "weaknesses.none"


def test_zero_variance_and_all_wins_and_all_losses_do_not_break(ledger):
    for kind in ("same", "up", "down"):
        ledger(_series("ALPHA", 50, kind))
        report = rules.build_report(_dataset())
        json.dumps(report, allow_nan=False)
        assert report["scope"]["n_trades"] == 50


def test_the_report_names_only_the_four_labels(ledger):
    ledger(_series("ALPHA", 100, "up"))
    report = rules.build_report(_dataset())
    labels = {
        report["scope"]["labels"]["pooled"],
        *report["scope"]["labels"]["per_strategy"].values(),
    }
    assert labels <= {
        ds.LABEL_INSUFFICIENT,
        ds.LABEL_NO_EDGE,
        ds.LABEL_POSSIBLE_EDGE,
        ds.LABEL_NEGATIVE,
    }


def test_weaknesses_are_tests_not_instructions(ledger):
    spike = [_trade("ALPHA", i, 1.0, days_back=1 + i % 10) for i in range(40)]
    spike += [_trade("ALPHA", 100 + i, 500.0, days_back=0) for i in range(2)]
    ledger(spike)
    for finding in _section(rules.build_report(_dataset()), "weaknesses")["findings"]:
        if finding["id"] != "weaknesses.none":
            assert "Test:" in finding["statement"] or "Test " in finding["statement"]
            assert not re.search(
                r"\b(should|must|stop trading|disable)\b", finding["statement"], re.I
            )
