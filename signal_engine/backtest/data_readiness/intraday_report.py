"""Intraday build step 11: the validation report and the dataset readiness (plan Parts A, B; 12).

Reads only the build folder (and the corporate-action events of the daily version) and writes
`report.json` and `report.md` there. Thresholds come from the versioned data-quality policy. No
strategy result is an input to anything here, and the report makes no claim about any strategy:
it states the quality properties of each field family.

Alerts need a written assessment: `<build>/assessment.yaml`, one entry per alert id with the
seven questions of Part A7 and a disposition. The report merges it; an alert without one keeps
the dataset NOT_READY.

    uv run python -m signal_engine.backtest.data_readiness.intraday_report \
        --build db/staging/intraday/build-<root12> --daily-version nse-daily-20261005-002
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
import yaml

from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import intraday_recovery as ir
from signal_engine.backtest.data_readiness import snapshot as _snapshot
from signal_engine.backtest.data_readiness import state as st

POLICY = ic.POLICY
PROVENANCE_PASS = POLICY["provenance"]["exact_share"]
WILSON_Z = 1.96
FALLBACK_PERIOD = ("2023-06-01", "2024-03-31")
BASIS_TOL = 0.001
FAMILIES = ic.FIELD_FAMILIES
QUESTIONS = ("material", "concentrated", "definition_or_defect", "field_families",
             "safely_excludable", "other_source", "remove_period")


def wilson_lower(k: int, n: int, z: float = WILSON_Z) -> float:
    """95% Wilson score lower bound of a proportion k/n."""
    if n == 0:
        return float("nan")
    p = k / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


# ---------------------------------------------------------------- conversion statistics

def outcome(sessions: pd.DataFrame) -> pd.Series:
    """primary | fallback | excluded:<reason> | unresolved | missing per requested session."""
    s = sessions.price_status.astype(str)
    return s.mask(s.eq(ic.CONVERTED), "primary").mask(s.eq(ic.CONVERTED_FALLBACK), "fallback")


def conversion_table(sessions: pd.DataFrame, by: pd.Series | None) -> pd.DataFrame:
    """Session count and the % of each price outcome, per group (never one combined share)."""
    keys = by if by is not None else pd.Series("all", index=sessions.index)
    t = pd.crosstab(keys, outcome(sessions))
    total = t.sum(axis=1)
    pct = t.div(total, axis=0).mul(100).round(3)
    pct.insert(0, "sessions", total)
    return pct


def basis_class(sessions: pd.DataFrame) -> pd.Series:
    k, pf = sessions.k, sessions.price_factor
    cls = pd.Series("other k", index=sessions.index)
    cls[(k / pf - 1).abs() <= BASIS_TOL] = "k = NSE factor"
    cls[(k - 1).abs() <= BASIS_TOL] = "k = 1"
    cls[k.isna()] = "no k"
    return cls


def event_sessions(sessions: pd.DataFrame, events: pd.DataFrame) -> pd.Series:
    """'ex-date', 'session before' or 'no event' for each session (corporate-action events)."""
    tag = pd.Series("no event", index=sessions.index, dtype=object)
    ex = events.assign(ex_date=pd.to_datetime(events.ex_date))[["symbol", "ex_date"]]
    for sym, part in sessions.groupby("symbol"):
        dates = part.session.sort_values()
        for e in ex[ex.symbol == sym].ex_date:
            before = dates[dates < e]
            if len(before):
                tag[part.index[part.session == before.iloc[-1]]] = "session before"
            tag[part.index[part.session == e]] = "ex-date"
    return tag


def fallback_period_evidence(sessions: pd.DataFrame) -> dict:
    """The one-sided extreme-gap evidence for the fallback-heavy period (plan 6.2)."""
    a, b = (pd.Timestamp(x) for x in FALLBACK_PERIOD)
    inside = sessions.session.between(a, b)
    fb = sessions[inside & sessions.price_valid & (sessions.status == ic.CONVERTED_FALLBACK)]
    h_gap = fb.H / fb.k / fb.raw_high - 1
    l_gap = fb.L / fb.k / fb.raw_low - 1
    published = sessions[inside & sessions.price_valid]
    return {"period": list(FALLBACK_PERIOD), "fallback_sessions": int(len(fb)),
            "fallback_share_of_published": round(len(fb) / max(len(published), 1), 4),
            "minute_high_at_or_below_nse_high": round(float((h_gap <= 1e-9).mean()), 4),
            "minute_low_at_or_above_nse_low": round(float((l_gap >= -1e-9).mean()), 4),
            "median_high_gap_pct": round(float(-h_gap.median() * 100), 4),
            "median_low_gap_pct": round(float(l_gap.median() * 100), 4)}


# ---------------------------------------------------------------- field-family rates (A7)

def rates(requested: pd.DataFrame, family: str, by: pd.Series) -> pd.DataFrame:
    """Excluded requested symbol-sessions / requested symbol-sessions for one field family."""
    bad = ~requested[f"{family}_valid"].astype(bool)
    t = pd.DataFrame({"requested": requested.groupby(by).size(), "excluded": bad.groupby(by).sum()})
    t["rate"] = (t.excluded / t.requested).round(5)
    return t


def alerts_and_guardrails(requested: pd.DataFrame) -> tuple[list[dict], list[dict], dict]:
    """(alerts, guardrail removals, rate tables) per field family, from the policy thresholds."""
    al, gr = POLICY["alerts"], POLICY["guardrails"]
    alerts, removals, tables = [], [], {}
    year = requested.session.dt.year
    month = requested.session.dt.to_period("M").astype(str)
    for fam in FAMILIES:
        overall = float((~requested[f"{fam}_valid"].astype(bool)).mean())
        by_year, by_month = rates(requested, fam, year), rates(requested, fam, month)
        by_symbol = rates(requested, fam, requested.symbol)
        tables[fam] = {"overall": round(overall, 5),
                       "by_year": json.loads(by_year.to_json(orient="index")),
                       "worst_months": json.loads(by_month.nlargest(10, "rate")
                                                  .to_json(orient="index")),
                       "worst_symbols": json.loads(by_symbol.nlargest(10, "rate")
                                                   .to_json(orient="index"))}
        if overall > al["overall"]:
            alerts.append({"id": f"{fam}:overall", "rate": round(overall, 5)})
        for scope, table, limit in (("year", by_year, al["year"]),
                                    ("month", by_month, al["month"]),
                                    ("symbol", by_symbol, al["symbol"])):
            for key, row in table[table.rate > limit].iterrows():
                alerts.append({"id": f"{fam}:{scope}:{key}", "rate": float(row.rate),
                               "excluded": int(row.excluded), "requested": int(row.requested)})
        for scope, table, limit in (("year", by_year, gr["year"]),
                                    ("symbol", by_symbol, gr["symbol"])):
            for key, row in table[table.rate > limit].iterrows():
                removals.append({"family": fam, scope: str(key), "rate": float(row.rate)})
    return alerts, removals, tables


def alert_facts(requested: pd.DataFrame, alert_id: str) -> dict:
    """Facts that the written assessment of one alert starts from."""
    fam, scope, *key = alert_id.split(":")
    bad = requested[~requested[f"{fam}_valid"].astype(bool)]
    if scope == "year":
        bad = bad[bad.session.dt.year == int(key[0])]
    elif scope == "month":
        bad = bad[bad.session.dt.to_period("M").astype(str) == key[0]]
    elif scope == "symbol":
        bad = bad[bad.symbol == key[0]]
    return {"defects": bad.final_defect.fillna("none").value_counts().head(6).to_dict(),
            "treatment": bad.treatment.value_counts().to_dict(),
            "symbols_affected": int(bad.symbol.nunique()),
            "top_symbols": bad.symbol.value_counts().head(5).to_dict()}


def merge_assessments(alerts: list[dict], path: Path) -> list[dict]:
    """Attach the written assessment of each alert; an incomplete one counts as missing."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    given = {a["id"]: a for a in (doc or {}).get("assessments", [])}
    for a in alerts:
        g = given.get(a["id"])
        complete = bool(g) and all(q in g for q in QUESTIONS) and bool(g.get("disposition"))
        a["assessment"] = g if complete else None
    return alerts


# ---------------------------------------------------------------- provenance

def provenance_summary(prov: pd.DataFrame, sessions: pd.DataFrame) -> dict:
    p = prov.drop_duplicates(["symbol", "session"])
    exact = p.result.eq("exact")
    legacy = sessions[sessions.origin == "legacy"]
    year = p.session.dt.year
    by_year = pd.DataFrame({"sampled": p.groupby(year).size(), "exact": exact.groupby(year).sum(),
                            "legacy_sessions": legacy.groupby(legacy.session.dt.year).size()})
    by_year["exact_share"] = (by_year.exact / by_year.sampled).round(4)
    by_year["wilson_lower_95"] = [round(wilson_lower(int(k), int(n)), 4)
                                  for k, n in zip(by_year.exact, by_year.sampled, strict=True)]
    by_year["coverage"] = (by_year.sampled / by_year.legacy_sessions).round(4)
    strata = prov.assign(exact=prov.result.eq("exact"),
                         stratum=prov.stratum.str.split(";")).explode("stratum")
    by_stratum = strata.groupby("stratum").agg(sampled=("exact", "size"), exact=("exact", "sum"))
    by_stratum["exact_share"] = (by_stratum.exact / by_stratum.sampled).round(4)
    lowest = p.assign(exact=exact).groupby("symbol").exact.mean().sort_values().head(10)
    n, k = len(p), int(exact.sum())
    return {"definition": "a sampled legacy session matches exactly when a fresh AngelOne fetch "
                          "with the same request parameters has the same minute timestamps and "
                          "identical open, high, low, close and volume on every bar",
            "sampled_sessions": n, "exact": k, "results": p.result.value_counts().to_dict(),
            "exact_share": round(k / n, 5) if n else None,
            "wilson_lower_95": round(wilson_lower(k, n), 5) if n else None,
            "coverage": round(n / max(len(legacy), 1), 4),
            "pass_mark": PROVENANCE_PASS, "passed": bool(n and k / n >= PROVENANCE_PASS),
            "by_year": json.loads(by_year.to_json(orient="index")),
            "by_stratum": json.loads(by_stratum.to_json(orient="index")),
            "lowest_symbols": lowest.round(4).to_dict(),
            "statement": "legacy Angel store + stratified verification + targeted Angel recovery"}


# ---------------------------------------------------------------- report

def repair_impact(build: Path, report_families: dict, requested: pd.DataFrame) -> dict | None:
    """Before/after of the timestamp-alignment repair, against the report saved before it."""
    pre_path = build / "report_pre_alignment.json"
    if not pre_path.exists():
        return None
    pre = json.loads(pre_path.read_text())
    upl = requested[requested.symbol.isin(ic.ALIGNMENT["symbols"])]
    pre_valid = dict(pre["sessions"]["valid"])
    return {
        "overall_rate": {f: {"before": pre["field_families"][f]["overall"],
                             "after": report_families[f]["overall"]} for f in FAMILIES},
        "valid_sessions_all_symbols": {f: {"before": pre_valid[f],
                                           "after": int(requested[f"{f}_valid"].sum())}
                                       for f in FAMILIES},
        "upl_valid_sessions_after": {f: int(upl[f"{f}_valid"].sum()) for f in FAMILIES},
        "upl_requested": int(len(upl)),
        "sessions_restored": int((requested.treatment == ic.ALIGNED_TREATMENT).sum()),
        "aligned_but_failing": int((requested.timestamp_aligned.fillna(False).astype(bool)
                                    & ~requested.price_valid).sum()),
    }


def cleanup_findings(build: Path) -> dict:
    """What cleanup rule C1 actually removed from the legacy store, by symbol (correction of the
    earlier reading that the off-minute bars were Flattrade contamination)."""
    led = pd.read_parquet(build / "cleanup_ledger.parquet")
    c1 = led[led.rule == "C1_off_minute"]
    by_symbol = c1.symbol.value_counts()
    return {"c1_off_minute_bars": int(len(c1)),
            "c1_by_symbol_top": by_symbol.head(5).to_dict(),
            "c1_share_upl": round(float(by_symbol.get("UPL", 0) / max(len(c1), 1)), 4),
            "c2_pre_open_bars": int((led.rule == "C2_pre_open").sum()),
            "reading": "C1 off-minute bars are predominantly UPL's own AngelOne bars stamped "
                       "1-6 s before the minute (a source timestamp convention, repaired by "
                       "timestamp_alignment), not Flattrade contamination. Genuine vendor mixing "
                       "is the 09:14 pre-open bars (C2) and the negative volumes (C3)."}


def _frame_json(df: pd.DataFrame) -> dict:
    return json.loads(df.to_json(orient="index"))


def _state(criteria: dict, metadata: dict, warnings: bool) -> str:
    if not all(criteria.values()) or not all(metadata.values()):
        return st.NOT_READY
    return st.READY_WITH_WARNINGS if warnings else st.READY


def build_report(build: Path, daily_version: str) -> dict:
    plan = json.loads((build / "plan.json").read_text())
    built = json.loads((build / "build.json").read_text())
    policy = json.loads((build / "policy.json").read_text())
    sessions = pd.read_parquet(build / "session_ledger.parquet")
    sessions["session"] = pd.to_datetime(sessions.session)
    prov = pd.read_parquet(build / "provenance.parquet")
    prov["session"] = pd.to_datetime(prov.session)
    checks = pd.read_parquet(build / "checks_5m.parquet")
    irrec_path = build / "irrecoverability.json"
    irrec = json.loads(irrec_path.read_text()) if irrec_path.exists() else None
    m = _snapshot.resolve_version(daily_version)
    events = pd.read_parquet(_snapshot._object_path(_snapshot.DEFAULT_ROOT, m["events_sha256"]))

    req = sessions[~sessions.cls.str.startswith("out_of_scope")].copy()
    pub = req[req.price_valid]
    alerts, removals, tables = alerts_and_guardrails(req)
    for a in alerts:
        a["facts"] = alert_facts(req, a["id"])
    alerts = merge_assessments(alerts, build / "assessment.yaml")
    recovery_errors = ir.verify(build)
    five = {c: int(checks[c].sum()) for c in checks.columns if c[0].isdigit()}
    provenance = provenance_summary(prov, sessions)
    by_year = conversion_table(req, req.session.dt.year)
    valid = req[[f"{f}_valid" for f in FAMILIES]]
    invalid_any = ~valid.all(axis=1)

    criteria = {
        "1_snapshot_hashed": plan["snapshot_root_hash"] == built["snapshot_root_hash"],
        "2_recovery_completed": not any("no final outcome" in e for e in recovery_errors),
        "3_responses_hashed": not any("missing or changed" in e for e in recovery_errors),
        "4_mixed_vendor_handled": bool(pub.structure_ok.astype(bool).all()),
        "5_missing_sessions_handled": bool(req.price_status.notna().all()),
        "6_normalisation_completed": bool(pub.k.notna().all()),
        "7_conversion_statistics": len(by_year) > 0,
        "8_alerts_assessed": all(a["assessment"] for a in alerts),
        "9_nse_crosscheck": bool(pub.contained.astype(bool).all()
                                 and req[req.volume_valid].volume_ok.astype(bool).all()
                                 and pub.final_bar_convention.isin(ic.CONVENTIONS).all()),
        "10_missing_minute_policy": bool((pub.missing_minutes <= policy["N"]).all()),
        "11_1m_validation": bool(pub.structure_ok.astype(bool).all()),
        "12_5m_aggregation_validation": all(v == 0 for k, v in five.items()
                                            if not k.startswith("10_")),
        "13_5m_validation": five.get("10_daily_rollup", 1) == 0,
        "14_calendar_session_rules": five.get("8_unexpected_session", 1) == 0,
        "15_quality_table_complete": bool(valid.notna().all().all()),
        "16_provenance_passed": provenance["passed"],
    }
    metadata = {
        "21_defects_classified": bool(req[invalid_any].final_defect.notna().all()),
        "22_policy_version_recorded": built.get("data_policy_version") == ic.policy_version(),
        "23_irrecoverability_recorded": irrec is not None,
    }
    pending = ["17_fingerprint", "18_root_hash", "19_immutable_publication_verified",
               "20_published_source_applies_contract"]
    state = _state(criteria, metadata, bool(alerts or removals or invalid_any.any()))
    return {
        "kind": "intraday_validation_report",
        "claim": "quality properties of each field family; no claim about any strategy",
        "snapshot_root_hash": built["snapshot_root_hash"],
        "template_sha256": built["template_sha256"],
        "conversion_spec_version": built["conversion_spec_version"],
        "data_policy_version": ic.policy_version(),
        "daily_version": daily_version,
        "readiness_state": state,
        "criteria": criteria, "metadata": metadata, "criteria_pending_publication": pending,
        "sessions": {"expected": len(sessions),
                     "out_of_scope_muhurat": int(len(sessions) - len(req)),
                     "requested": len(req),
                     "valid": {f: int(req[f"{f}_valid"].sum()) for f in FAMILIES},
                     "by_origin": req.origin.fillna("none").value_counts().to_dict(),
                     "by_price_status": req.price_status.value_counts().to_dict(),
                     "by_treatment": req.treatment.value_counts().to_dict(),
                     "by_defect": req.final_defect.fillna("none").value_counts().to_dict(),
                     "final_bar_convention": pub.final_bar_convention.value_counts().to_dict()},
        "field_families": tables,
        "alerts": alerts,
        "usable_range_removals": removals,
        "conversion": {
            "overall": _frame_json(conversion_table(req, None)),
            "by_year": _frame_json(by_year),
            "by_basis_class": _frame_json(conversion_table(req, basis_class(req))),
            "by_event": _frame_json(conversion_table(req, event_sessions(req, events))),
            "by_symbol": _frame_json(conversion_table(req, req.symbol)),
            "by_fallback_pair": pub[pub.status == ic.CONVERTED_FALLBACK].method
            .value_counts().to_dict(),
            "fallback_period": fallback_period_evidence(req),
            "open_field_outside_tolerance": int((~pub.open_ok.astype(bool)).sum()),
        },
        "missing_minute_policy": policy,
        "missing_minute_note": "N is permissive because 2017-2020 minute data is sparse "
                               "(P99 of missing minutes 212-254 in those years); every 5m bar "
                               "carries n_minutes so a strategy contract can set its own minimum",
        "timestamp_alignment": repair_impact(build, tables, req),
        "cleanup_findings": cleanup_findings(build),
        "irrecoverability": irrec,
        "provenance": provenance,
        "recovery": {"requests_planned": int(plan["requests_total"]),
                     "errors": recovery_errors[:20]},
        "checks_5m": five,
        "bars": {"one_minute": int(checks.bars_1m.sum()),
                 "five_minute": int(checks.bars_5m.sum())},
    }


def _table(rows: dict) -> list[str]:
    df = pd.DataFrame(rows).T
    head = ["key", *map(str, df.columns)]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for idx, row in df.iterrows():
        out.append("| " + " | ".join([str(idx), *(str(v) for v in row)]) + " |")
    return out


def _alert_line(a: dict) -> str:
    done = a["assessment"]
    return (f"- `{a['id']}` rate {a['rate']}: "
            + (f"assessed, disposition: {done['disposition']}" if done else "ASSESSMENT MISSING"))


def to_markdown(r: dict) -> str:
    lines = ["# Intraday 5m dataset validation report", "",
             f"Readiness: **{r['readiness_state']}** ({r['claim']})", "",
             f"Snapshot root: `{r['snapshot_root_hash']}`  ",
             f"Data policy version: `{r['data_policy_version']}`", "",
             "## Criteria", "", "| Criterion | Result |", "|---|---|"]
    lines += [f"| {k} | {'PASS' if v else 'FAIL'} |"
              for k, v in {**r["criteria"], **r["metadata"]}.items()]
    lines += [f"| {k} | pending publication |" for k in r["criteria_pending_publication"]]
    lines += ["", "## Sessions", "", "```", json.dumps(r["sessions"], indent=1), "```", ""]
    for fam, t in r["field_families"].items():
        lines += [f"## Field family `{fam}`: exclusion by year (overall {t['overall']})", "",
                  *_table(t["by_year"]), ""]
    lines += ["## Alerts", "", *([_alert_line(a) for a in r["alerts"]] or ["none"]), "",
              "## Usable-range removals (guardrails)", "", "```",
              json.dumps(r["usable_range_removals"], indent=1), "```", "",
              "## Price conversion by year (% of requested sessions)", "",
              *_table(r["conversion"]["by_year"]), "", "## Fallback period evidence", "", "```",
              json.dumps(r["conversion"]["fallback_period"], indent=1), "```", "",
              "## Missing-minute policy", "",
              f"N = {r['missing_minute_policy']['N']} ({r['missing_minute_policy']['method']})",
              "", "## UPL timestamp alignment: before and after", "", "```",
              json.dumps(r["timestamp_alignment"], indent=1), "```", "",
              "## Cleanup rule C1: what it removed", "", "```",
              json.dumps(r["cleanup_findings"], indent=1), "```",
              "", "## Irrecoverability decisions", "", "```",
              json.dumps((r["irrecoverability"] or {}).get("cells"), indent=1), "```", "",
              "## Provenance", "", "```",
              json.dumps({k: r["provenance"][k] for k in (
                  "sampled_sessions", "exact", "results", "exact_share", "wilson_lower_95",
                  "coverage", "passed", "statement")}, indent=1), "```", "",
              "## 5m validation violations", "", "```", json.dumps(r["checks_5m"], indent=1),
              "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.intraday_report")
    ap.add_argument("--build", required=True, type=Path)
    ap.add_argument("--daily-version", required=True)
    args = ap.parse_args(argv)
    report = build_report(args.build, args.daily_version)
    (args.build / "report.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    (args.build / "report.md").write_text(to_markdown(report))
    failed = [k for k, v in {**report["criteria"], **report["metadata"]}.items() if not v]
    print(report["readiness_state"], failed, f"{len(report['alerts'])} alerts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
