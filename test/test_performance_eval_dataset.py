"""The evaluation dataset: server-computed, verdict labels from existing numbers,
excluded strategies absent, mode isolated, deterministic.

Run with: PYTHONPATH=. uv run pytest test/test_performance_eval_dataset.py -v
"""

import copy
import json
from datetime import date, timedelta

import pytest

import services.performance_eval.dataset as ds
import services.strategy_daily_performance_service as svc

TODAY = date.today()


def _trade(strategy, i, pnl, mode="analyze", symbol=None, days_back=None):
    day = TODAY - timedelta(days=(i % 20) if days_back is None else days_back)
    hour = 9 + (i % 3)
    return {
        "strategy": strategy,
        "symbol": symbol or f"SYM{i % 4}",
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


def _series(strategy, n, kind="mixed", mode="analyze"):
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
        out.append(_trade(strategy, i, pnl, mode=mode))
    return out


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """Install a fake ledger; returns a function to set its trades."""
    state = {"trades": [], "mode": "analyze"}

    def get_closed_trades(mode=None, **_kw):
        return [copy.deepcopy(t) for t in state["trades"] if t["mode"] == mode]

    monkeypatch.setattr(svc, "get_closed_trades", get_closed_trades)
    monkeypatch.setattr(svc, "_current_mode", lambda: state["mode"])
    monkeypatch.setattr(
        "services.strategy_trade_enrichment.SIGNAL_TRADES_DB", tmp_path / "absent.db"
    )
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))

    def setter(trades, mode="analyze"):
        state["trades"] = trades
        state["mode"] = mode
        return state

    return setter


def _build(include=None, exclude=None, strategy=None, period="90d", capital=None):
    return ds.build_evaluation_dataset(
        include or [], exclude or [], strategy, period, None, None, capital
    )


def test_empty_selection_is_refused_with_a_trader_message(ledger):
    ledger([])
    ok, body, status = _build()
    assert (ok, status) == (False, 400)
    assert body["message"] == "There are no closed trades in this selection to evaluate."


def test_selection_with_no_matching_strategy_is_empty(ledger):
    ledger(_series("ALPHA", 10))
    ok, body, status = _build(strategy="DOES-NOT-EXIST")
    assert (ok, status) == (False, 400)
    assert "no closed trades" in body["message"]


@pytest.mark.parametrize(
    "n,kind,expected",
    [
        (1, "up", ds.LABEL_INSUFFICIENT),
        (5, "up", ds.LABEL_INSUFFICIENT),
        (20, "up", ds.LABEL_INSUFFICIENT),
        (29, "up", ds.LABEL_INSUFFICIENT),
        (30, "flat", ds.LABEL_NO_EDGE),
        (100, "up", ds.LABEL_POSSIBLE_EDGE),
        (100, "down", ds.LABEL_NEGATIVE),
    ],
)
def test_label_follows_the_existing_edge_verdict(ledger, n, kind, expected):
    ledger(_series("ALPHA", n, kind))
    ok, data, _ = _build()
    assert ok
    block = data["strategies"][0]
    assert block["evidence_label"] == expected
    assert data["pooled"]["evidence_label"] == expected
    assert block["trades_count"] == n
    # The label is only a rename of the verdict the page already shows.
    assert ds.evidence_label(data["pooled"]["edge_verdict"]) == expected


def test_label_mapping_is_total_and_has_no_rating_words():
    assert ds.evidence_label("too few trades") == "insufficient data"
    assert ds.evidence_label("too few days") == "insufficient data"
    assert ds.evidence_label("not distinguishable from zero") == "no distinguishable edge"
    assert ds.evidence_label("likely real") == "possible edge (screening only)"
    assert ds.evidence_label("likely negative") == "likely negative"
    assert ds.evidence_label(None) == "insufficient data"
    assert ds.evidence_label("something new") == "insufficient data"
    labels = set(ds._VERDICT_LABELS.values())
    for word in ("good", "average", "poor", "profitable"):
        assert not any(word in label for label in labels)


def test_all_wins_all_losses_and_zero_variance(ledger):
    ledger(_series("WINS", 40, "up") + _series("LOSS", 40, "down") + _series("SAME", 40, "same"))
    ok, data, _ = _build()
    assert ok
    by = {b["strategy"]: b for b in data["strategies"]}
    assert by["WINS"]["compare"]["win_rate"] == 100.0
    assert by["LOSS"]["compare"]["win_rate"] == 0.0
    # Existing edge_stats behaviour, pinned: zero spread reads as "likely real".
    assert by["SAME"]["compare"]["edge_verdict"] == "likely real"
    assert by["SAME"]["evidence_label"] == ds.LABEL_POSSIBLE_EDGE
    json.dumps(data, allow_nan=False)  # no NaN or inf anywhere


def test_zero_spread_series_keeps_the_existing_verdict_and_valid_json(ledger):
    """The label is whatever edge_stats decided; the dataset adds no verdict of its own."""
    trades = [_trade("FLATLINE", i, 50.0, symbol="ONE") for i in range(40)]
    ledger(trades)
    ok, data, _ = _build()
    assert ok
    assert data["pooled"]["edge_verdict"] == "likely real"
    assert data["pooled"]["evidence_label"] == ds.LABEL_POSSIBLE_EDGE
    json.dumps(data, allow_nan=False)


def test_excluded_strategies_never_appear(ledger):
    ledger(
        _series("KEEP-ONE", 35, "up")
        + _series("KEEP-TWO", 35, "flat")
        + _series("HIDDEN-EXCLUDED-XYZ", 35, "down")
    )
    ok, data, _ = _build(exclude=["HIDDEN-EXCLUDED-XYZ"])
    assert ok
    text = json.dumps(data)
    assert "HIDDEN-EXCLUDED-XYZ" not in text
    assert data["meta"]["excluded_strategy_count"] == 1
    assert data["meta"]["strategies_included"] == ["KEEP-ONE", "KEEP-TWO"]
    from services.performance_eval.prompt import build_prompt

    assert "HIDDEN-EXCLUDED-XYZ" not in build_prompt(data, include_data=True)


def test_unselected_strategy_is_not_copied_from_all_strategies(ledger):
    ledger(_series("PICKED", 35, "up") + _series("NOT-PICKED-QQQ", 35, "down"))
    ok, data, _ = _build(strategy="PICKED")
    assert ok
    assert "NOT-PICKED-QQQ" not in json.dumps(data)
    assert [b["strategy"] for b in data["strategies"]] == ["PICKED"]


def test_capital_is_echoed_or_absent(ledger):
    ledger(_series("ALPHA", 35, "up"))
    ok, with_cap, _ = _build(capital=1_000_000.0)
    assert ok
    assert with_cap["meta"]["capital"] == 1_000_000.0
    assert with_cap["pooled"]["capital_view"]["capital"] == 1_000_000.0
    ok, without, _ = _build()
    assert without["meta"]["capital"] is None
    assert "capital_view" not in without["pooled"]


@pytest.mark.parametrize("mode,display", [("live", "Live"), ("analyze", "Sandbox")])
def test_mode_is_resolved_on_the_server_and_isolated(ledger, mode, display):
    ledger(
        _series("ALPHA", 31, "up", mode="live") + _series("ALPHA", 7, "down", mode="analyze"),
        mode=mode,
    )
    ok, data, _ = _build()
    assert ok
    assert data["meta"]["mode"] == mode
    assert data["meta"]["mode_display"] == display
    assert data["pooled"]["trades_count"] == (31 if mode == "live" else 7)


def test_mode_change_between_the_two_reads_is_refused(ledger, monkeypatch):
    ledger(_series("ALPHA", 35, "up"))
    modes = iter(["analyze", "live", "live", "live", "live", "live", "live"])
    monkeypatch.setattr(svc, "_current_mode", lambda: next(modes))
    ok, body, status = _build()
    assert (ok, status) == (False, 409)
    assert "mode changed" in body["message"]


def test_ledger_floor_is_noted(ledger, monkeypatch):
    ledger(_series("ALPHA", 35, "up"))
    ok, data, _ = _build(period="all")
    assert ok
    assert data["meta"]["ledger_reliable_since"] == "2020-01-01"
    assert data["meta"]["window_starts_at_ledger_floor"] is False
    floor = date.today() - timedelta(days=19)
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", floor)
    ok, data, _ = _build(period="90d")
    assert data["meta"]["window_starts_at_ledger_floor"] is True
    assert data["meta"]["start_date"] == floor.isoformat()


def test_buckets_are_flagged_underpowered_and_counted(ledger):
    ledger(_series("ALPHA", 20, "mixed") + _series("BETA", 100, "up"))
    ok, data, _ = _build()
    assert ok
    tables = data["buckets"]
    assert set(tables) == {
        "by_entry_hour",
        "by_exit_hour",
        "by_weekday",
        "by_hold_bucket",
        "by_symbol",
    }
    for table in tables.values():
        assert table["min_trades_per_bucket"] == 30
        assert table["buckets_tested"] >= len(table["rows"])
        for row in table["rows"]:
            assert row["underpowered"] == (row["n"] < 30)
    assert tables["by_entry_hour"]["buckets_tested"] == 3
    assert all(not r["underpowered"] for r in tables["by_entry_hour"]["rows"])
    assert any(r["underpowered"] for r in tables["by_weekday"]["rows"])


def test_symbol_table_shows_only_the_extremes_but_counts_all(ledger):
    trades = [_trade("ALPHA", i, float(i - 6) * 10, symbol=f"S{i:02d}") for i in range(14)]
    ledger(trades)
    ok, data, _ = _build()
    assert ok
    table = data["buckets"]["by_symbol"]
    assert table["buckets_tested"] == 14
    assert len(table["rows"]) == 10
    pnls = [r["gross_pnl"] for r in table["rows"]]
    assert pnls[:5] == sorted(pnls[:5], reverse=True)


def test_caveats_are_always_present(ledger):
    ledger(_series("ALPHA", 3, "up"))
    ok, data, _ = _build()
    assert ok
    text = " ".join(data["caveats"])
    for needle in (
        "reliable only from",
        "Charges are estimated",
        "Slippage is not part of the charges",
        "daily totals",
        "zero P&L days",
        "one fixed value",
        "screening heuristics",
        "too narrow below 30 trades",
        "exploratory",
        "Live and Sandbox",
        "R multiples",
        "distribution is not available",
    ):
        assert needle in text, needle
    assert data["edge_screen"]["note"] == "screening heuristics, not proof"
    assert data["pooled"]["pnl_distribution"] == "not available in this dataset"
    assert data["pooled"]["daily_pnl_distribution"]["basis"].startswith("DAILY")
    assert "DAYS" in data["definitions"]["day_streaks"]


def test_pooled_block_equals_the_daily_service(ledger):
    ledger(_series("ALPHA", 40, "mixed") + _series("BETA", 40, "up"))
    ok, data, _ = _build(capital=500_000.0)
    ok2, daily, _ = svc.get_daily_performance(None, "90d", capital=500_000.0)
    assert ok and ok2
    for key in (
        "trades_count",
        "net_profit",
        "net_after_costs",
        "est_costs",
        "win_rate",
        "profit_factor",
        "expectancy",
        "net_expectancy",
        "max_drawdown",
        "sharpe_ratio",
        "t_stat",
        "day_t_stat",
        "expectancy_ci95",
        "edge_days",
        "edge_verdict",
    ):
        assert data["pooled"][key] == daily[key], key
    assert data["pooled"]["capital_view"] == daily["capital_view"]


def test_strategy_rows_equal_the_compare_service(ledger):
    ledger(_series("ALPHA", 40, "mixed") + _series("BETA", 40, "up"))
    ok, data, _ = _build()
    ok2, comp, _ = svc.get_strategy_comparison("90d")
    assert ok and ok2
    rows = {r["strategy"]: r for r in comp["strategies"]}
    for block in data["strategies"]:
        expected = {k: v for k, v in rows[block["strategy"]].items() if k != "strategy"}
        assert block["compare"] == expected


def test_only_the_largest_strategies_get_detail_and_overlap(ledger, monkeypatch):
    trades = []
    for k in range(10):
        trades += _series(f"S{k:02d}", 12 + k, "mixed")
    ledger(trades)
    ok, data, _ = _build()
    assert ok
    detailed = [b for b in data["strategies"] if isinstance(b["detail"], dict)]
    assert len(detailed) == ds.MAX_STRATEGIES_DETAILED == 8
    assert [b["strategy"] for b in detailed] == [f"S{k:02d}" for k in range(9, 1, -1)]
    capped = [b for b in data["strategies"] if not isinstance(b["detail"], dict)]
    assert {b["detail"] for b in capped} == {"compare row only (size cap)"}
    assert len(data["overlap"]) == 28
    pair = data["overlap"][0]
    assert pair["n_days"] > 0 and "shared_loss_days" in pair
    assert -1.0 <= pair["pearson_daily_pnl"] <= 1.0


def test_overlap_is_omitted_for_a_single_strategy(ledger):
    ledger(_series("ALPHA", 40, "mixed"))
    ok, data, _ = _build()
    assert ok and data["overlap"] == []


def test_trade_frequency_names_its_formula(ledger):
    ledger(_series("ALPHA", 40, "mixed"))
    ok, data, _ = _build()
    freq = data["pooled"]["trade_frequency"]
    assert freq["per_trading_day"] == round(40 / data["pooled"]["trading_days"], 2)
    assert freq["per_calendar_day"] == round(40 / data["pooled"]["calendar_days"], 2)
    assert "trades_count / trading_days" in freq["formula"]


def test_size_guard_is_deterministic_and_recorded(ledger, monkeypatch):
    trades = []
    for k in range(10):
        trades += _series(f"S{k:02d}", 15 + k, "mixed")
    ledger(trades)
    ok, full, _ = _build()
    assert full["meta"]["truncated"] == []
    full_size = ds.estimate_tokens(full)
    monkeypatch.setattr(ds, "TOKEN_BUDGET", full_size // 2)
    ok1, first, _ = _build()
    ok2, second, _ = _build()
    assert ok1 and ok2
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["meta"]["truncated"]
    assert first["meta"]["truncated"][0].startswith("detail of strategy ")
    assert ds.estimate_tokens(first) < full_size
    # The smallest strategy that still has detail loses it first.
    assert first["meta"]["truncated"][0] == "detail of strategy S02"


def test_size_guard_drops_least_populated_buckets_before_comparison_rows(ledger, monkeypatch):
    ledger(_series("ALPHA", 40, "mixed") + _series("BETA", 40, "up"))
    ok, full, _ = _build()
    monkeypatch.setattr(ds, "TOKEN_BUDGET", ds.estimate_tokens(full) - 300)
    ok, small, _ = _build()
    dropped = small["meta"]["truncated"]
    assert dropped
    assert not any(d.startswith("comparison row") for d in dropped)


def test_output_is_deterministic_for_a_fixed_fixture(ledger):
    ledger(_series("ALPHA", 40, "mixed") + _series("BETA", 33, "up"))
    first = json.dumps(_build(capital=250_000.0)[1], sort_keys=True)
    second = json.dumps(_build(capital=250_000.0)[1], sort_keys=True)
    assert first == second
    data = json.loads(first)
    assert data["pooled"]["trades_count"] == 73
    assert data["meta"]["strategies_included"] == ["ALPHA", "BETA"]
    assert [b["strategy"] for b in data["strategies"]] == ["ALPHA", "BETA"]
    assert len(data["caveats"]) == len(ds.CAVEATS)


def test_building_the_dataset_does_not_change_the_ledger(ledger):
    trades = _series("ALPHA", 40, "mixed") + _series("BETA", 33, "up")
    state = ledger(trades)
    before = copy.deepcopy(state["trades"])
    _build(capital=100_000.0)
    _build(exclude=["BETA"])
    assert state["trades"] == before


def test_single_day_trade_list_is_not_copied(ledger):
    ledger([_trade("ALPHA", i, 10.0 + i, days_back=0) for i in range(5)])
    ok, data, _ = _build(period="1d")
    assert ok
    text = json.dumps(data)
    assert '"trades":' not in text.replace(" ", "")
    assert "closed_at" not in text


# -- fix round 1 ---------------------------------------------------------------------


def test_thirty_trades_on_five_days_is_insufficient_through_the_builder(ledger):
    ledger([_trade("F", i, 50.0 + (i % 7), days_back=i % 5) for i in range(30)])
    ok, data, _ = _build()
    assert ok
    assert data["pooled"]["trades_count"] == 30 and data["pooled"]["trading_days"] == 5
    assert data["pooled"]["edge_verdict"] == "too few days"
    assert data["pooled"]["evidence_label"] == ds.LABEL_INSUFFICIENT
    assert data["strategies"][0]["evidence_label"] == ds.LABEL_INSUFFICIENT


def test_the_same_series_at_29_and_at_30_trades_crosses_the_threshold(ledger):
    def series(n):
        return [_trade("F", i, 50.0 + (i % 7) * 3 - 6, days_back=i % 12) for i in range(n)]

    ledger(series(29))
    _ok, below, _ = _build()
    ledger(series(30))
    _ok, at, _ = _build()
    assert below["pooled"]["edge_verdict"] == "too few trades"
    assert below["pooled"]["evidence_label"] == ds.LABEL_INSUFFICIENT
    assert at["pooled"]["edge_verdict"] == "likely real"
    assert at["pooled"]["evidence_label"] == ds.LABEL_POSSIBLE_EDGE


def test_buckets_at_exactly_29_and_30_trades_in_one_table(ledger):
    trades = []
    for i in range(59):
        trade = _trade("F", i, 10.0 + (i % 5), days_back=i % 15)
        hour = 9 if i < 29 else 10
        day = trade["trade_date"]
        trade["opened_at"] = f"{day}T{hour:02d}:00:00"
        trade["closed_at"] = f"{day}T{hour:02d}:40:00"
        trades.append(trade)
    ledger(trades)
    ok, data, _ = _build()
    assert ok
    rows = {r["hour"]: r for r in data["buckets"]["by_entry_hour"]["rows"]}
    assert rows[9]["n"] == 29 and rows[9]["underpowered"] is True
    assert rows[10]["n"] == 30 and rows[10]["underpowered"] is False


def test_detail_read_in_another_mode_is_the_mode_changed_refusal(ledger, monkeypatch):
    ledger(_series("ALPHA", 35, "up"))
    real = svc.get_daily_performance

    def flipping(strategy, *a, **kw):
        ok, body, status = real(strategy, *a, **kw)
        if strategy == "ALPHA" and ok:
            body = {**body, "mode": "live"}
        return ok, body, status

    monkeypatch.setattr(ds, "get_daily_performance", flipping)
    # Pooled read (strategy None) keeps its mode; the per-strategy detail read reports another.
    ok, body, status = _build()
    assert (ok, status) == (False, 409)
    assert body["message"] == ds.MODE_CHANGED_MESSAGE


def test_detailed_strategies_lists_only_those_whose_detail_survived(ledger, monkeypatch):
    trades = []
    for k in range(6):
        trades += _series(f"S{k:02d}", 15 + k, "mixed")
    ledger(trades)
    _ok, full, _ = _build()
    assert full["meta"]["detailed_strategies"] == [f"S{k:02d}" for k in range(6)]
    monkeypatch.setattr(ds, "TOKEN_BUDGET", ds.estimate_tokens(full) - 200)
    _ok, small, _ = _build()
    survivors = sorted(b["strategy"] for b in small["strategies"] if isinstance(b["detail"], dict))
    assert survivors != sorted(full["meta"]["detailed_strategies"])
    assert small["meta"]["detailed_strategies"] == survivors


def test_estimate_is_the_length_of_the_text_sent(ledger):
    ledger(_series("ALPHA", 40, "mixed"))
    _ok, data, _ = _build()
    text = ds.dataset_to_text(data)
    assert ds.estimate_tokens(data) == len(text) // 4
    assert text == json.dumps(
        json.loads(text), allow_nan=False, sort_keys=True, ensure_ascii=False, indent=1
    )


def test_a_non_finite_number_is_sent_as_null(ledger):
    ledger(_series("ALPHA", 40, "mixed"))
    _ok, data, _ = _build()
    data["pooled"]["sharpe_ratio"] = float("nan")
    data["pooled"]["profit_factor"] = float("inf")
    text = ds.dataset_to_text(data)
    parsed = json.loads(text)
    assert parsed["pooled"]["sharpe_ratio"] is None
    assert parsed["pooled"]["profit_factor"] is None
    assert "NaN" not in text and "Infinity" not in text


def test_the_package_text_carries_mode_display_only(ledger):
    ledger(_series("ALPHA", 35, "up"), mode="analyze")
    _ok, data, _ = _build()
    parsed = json.loads(ds.dataset_to_text(data))
    assert "mode" not in parsed["meta"]
    assert parsed["meta"]["mode_display"] == "Sandbox"
    assert data["meta"]["mode"] == "analyze"  # the server keeps it


def test_pearson_known_values():
    assert ds._pearson([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0]) == 1.0
    assert ds._pearson([1.0, 2.0, 3.0, 4.0], [8.0, 6.0, 4.0, 2.0]) == -1.0
    assert ds._pearson([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None
    assert ds._pearson([1.0, 2.0, 3.0], [4.0, 4.0, 4.0]) is None
    assert ds._pearson([1.0, 2.0], [1.0, 2.0]) is None
    assert ds._pearson([], []) is None


def test_overlap_counts_shared_loss_days():
    pairs = ds._overlap(
        {"A": [-1.0, -2.0, 3.0, -4.0, 0.0], "B": [-1.0, 5.0, 3.0, -4.0, -9.0], "C": [1.0, 2.0]}
    )
    assert len(pairs) == 1  # C has a different number of days and is skipped
    pair = pairs[0]
    assert (pair["a"], pair["b"], pair["n_days"]) == ("A", "B", 5)
    assert pair["shared_loss_days"] == 2
    assert -1.0 <= pair["pearson_daily_pnl"] <= 1.0


def test_package_hash_is_deterministic_and_ignores_timestamps(ledger):
    ledger(_series("ALPHA", 40, "mixed"))
    _ok, data, _ = _build()
    first = ds.package_hash(data)
    assert len(first) == 64 and int(first, 16) >= 0
    assert ds.package_hash(copy.deepcopy(data)) == first
    stamped = copy.deepcopy(data)
    stamped["meta"]["generated_at"] = "2026-11-03T10:00:00+05:30"
    stamped["timestamp"] = "x"
    assert ds.package_hash(stamped) == first


def test_package_hash_changes_with_the_data(ledger):
    ledger(_series("ALPHA", 40, "mixed"))
    _ok, data, _ = _build()
    other = copy.deepcopy(data)
    other["pooled"]["net_profit"] = (other["pooled"]["net_profit"] or 0) + 1
    assert ds.package_hash(other) != ds.package_hash(data)


def test_package_hash_is_the_sha256_of_canonical_json(ledger):
    import hashlib

    ledger(_series("ALPHA", 40, "mixed"))
    _ok, data, _ = _build()
    view = json.loads(ds.dataset_to_text(data))
    canonical = json.dumps(
        view, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    assert ds.package_hash(data) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
