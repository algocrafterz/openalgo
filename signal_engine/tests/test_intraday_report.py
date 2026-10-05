"""Intraday validation report statistics (INTRADAY-5M-DATASET-PLAN.md section 10)."""

import pandas as pd
import pytest
import yaml

from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import intraday_report as rp


def test_wilson_lower_bound_known_values():
    assert rp.wilson_lower(99, 100) == pytest.approx(0.9455, abs=1e-4)
    assert rp.wilson_lower(0, 10) == pytest.approx(0.0, abs=1e-12)
    assert rp.wilson_lower(5, 5) < 1.0


def _sessions(statuses, year=2023):
    return pd.DataFrame({"symbol": "AAA", "price_status": statuses,
                         "session": pd.date_range(f"{year}-01-02", periods=len(statuses))})


def test_conversion_table_keeps_the_outcomes_apart():
    s = _sessions([ic.CONVERTED] * 6 + [ic.CONVERTED_FALLBACK] * 2 + ["excluded:volume",
                                                                      ic.UNRESOLVED])
    t = rp.conversion_table(s, s.session.dt.year).loc[2023]
    assert t.sessions == 10 and t.primary == 60 and t.fallback == 20
    assert t["excluded:volume"] == 10 and t[ic.UNRESOLVED] == 10


def _families(n, volume_bad, year):
    s = pd.DataFrame({"symbol": "AAA", "session": pd.date_range(f"{year}-01-02", periods=n),
                      "price_valid": True, "final_bar_valid": True})
    s["volume_valid"] = [i >= volume_bad for i in range(n)]
    return s


def test_rates_alerts_and_guardrails_are_per_field_family():
    s = pd.concat([_families(100, 1, 2022), _families(100, 4, 2023), _families(100, 12, 2024)],
                  ignore_index=True)
    alerts, removals, tables = rp.alerts_and_guardrails(s)
    ids = {a["id"] for a in alerts}
    assert "volume:year:2023" in ids and "volume:year:2024" in ids
    assert "volume:year:2022" not in ids and not any(i.startswith("price") for i in ids)
    assert removals == [{"family": "volume", "year": "2024", "rate": 0.12}]
    assert tables["volume"]["by_year"]["2023"]["rate"] == 0.04
    assert tables["price"]["overall"] == 0.0


def test_alert_needs_all_seven_answers_and_a_disposition(tmp_path):
    path = tmp_path / "assessment.yaml"
    answers = dict.fromkeys(rp.QUESTIONS, "x")
    path.write_text(yaml.safe_dump({"assessments": [
        {"id": "volume:year:2023", **answers, "disposition": "exclude volume"},
        {"id": "volume:year:2024", "material": "yes", "disposition": "x"}]}))
    alerts = rp.merge_assessments([{"id": "volume:year:2023"}, {"id": "volume:year:2024"},
                                   {"id": "price:overall"}], path)
    assert alerts[0]["assessment"]["disposition"] == "exclude volume"
    assert alerts[1]["assessment"] is None and alerts[2]["assessment"] is None


def test_event_sessions_tag_the_ex_date_and_the_session_before():
    s = _sessions([ic.CONVERTED] * 4)
    ev = pd.DataFrame({"symbol": ["AAA"], "ex_date": ["2023-01-04"]})
    assert list(rp.event_sessions(s, ev)) == ["no event", "session before", "ex-date",
                                              "no event"]


def test_basis_class():
    s = pd.DataFrame({"k": [1.0, 0.5, 0.3, None], "price_factor": [0.5, 0.5, 0.5, 0.5]})
    assert list(rp.basis_class(s)) == ["k = 1", "k = NSE factor", "other k", "no k"]
