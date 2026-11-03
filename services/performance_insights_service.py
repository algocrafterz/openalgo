"""
Assembles the P1 insights for the Strategy Performance page from enriched closed trades.

Every formula lives in `services/performance_stats.py`; this module only turns enriched
trade dicts into the plain rows those functions take and puts the results together. No I/O.

Percent-of-capital figures use the reference capital per strategy times the strategies
selected (the same basis as `compute_capital_view`), so they do not change with the
capital control, exactly like the recorded P&L they are computed from.
"""

from __future__ import annotations

import numpy as np

from services import performance_stats as ps
from services.strategy_metrics_service import REFERENCE_CAPITAL_PER_STRATEGY


def trade_rows(enriched: list[dict]) -> list[dict]:
    """Trade rows for `performance_stats`: net = realized minus estimated charges
    (an unknown charge counts as 0, as everywhere else on the page)."""
    return [
        {
            "strategy": t["strategy"],
            "symbol": t.get("symbol"),
            "trade_date": t["trade_date"],
            "opened_at": t.get("opened_at"),
            "closed_at": t.get("closed_at"),
            "direction": t.get("direction"),
            "entry_price": t.get("entry_price"),
            "closed_quantity": t.get("closed_quantity"),
            "net": float(t["realized_pnl"]) - float(t.get("cost") or 0.0),
            "net_r": t.get("net_r_multiple"),
            "cost": float(t.get("cost") or 0.0),
            "entry_slippage": t.get("entry_slippage"),
            "data_flag": t.get("data_flag"),
        }
        for t in enriched
    ]


def _session_values(rows: list[dict], sessions: list[str]) -> np.ndarray:
    return ps.daily_pnl_values(ps.daily_totals(rows), sessions)


def _core(
    rows: list[dict],
    enriched: list[dict],
    sessions: list[str],
    strategies: int,
    sharpe: float | None,
    sortino: float | None,
    edge: dict | None = None,
    mode: str | None = None,
) -> dict:
    """Concentration, net drawdown, streaks, the grade and the validation status for one set
    of trades. `edge` is the edge-check result for the same trades (the caller already has it)."""
    base = float(REFERENCE_CAPITAL_PER_STRATEGY * max(1, strategies))
    values = _session_values(rows, sessions)
    ordered = [r["net"] for r in ps.ordered_trades(rows)]
    drawdown = ps.net_drawdown(values, sessions, base, ordered)
    conc = ps.concentration(rows)
    nets = [r["net"] for r in rows]
    gross = sum(float(t["realized_pnl"]) for t in enriched)
    costs = sum(float(t.get("cost") or 0.0) for t in enriched)
    r_values = [r["net_r"] for r in rows if r["net_r"] is not None]
    pf = ps.profit_factor(nets)
    grade = ps.grade_report(
        {
            "net_profit_factor": pf,
            "win_rate_pct": ps.pct(sum(1 for v in nets if v > 0), len(nets)),
            "expectancy_r": (
                sum(r_values) / len(r_values) if len(r_values) >= ps.MIN_TRADES_FOR_GRADE else None
            ),
            "sharpe": sharpe,
            "sortino": sortino,
            "recovery_factor": drawdown["recovery_factor"],
            "cost_pct_of_gross": ps.pct(costs, gross) if gross > 0 else None,
        },
        trades=len(rows),
        sessions=len(sessions),
    )
    rolling = ps.rolling_series(rows)
    last_exp20 = next((p["exp20"] for p in reversed(rolling) if p["exp20"] is not None), None)
    validation = ps.validation_status(
        trades=len(rows),
        sessions=len(sessions),
        net_pf=pf,
        net_expectancy=sum(nets) / len(nets) if nets else None,
        edge_verdict=edge["edge_verdict"] if edge else None,
        concentration_stats=conc,
        drawdown=drawdown,
        last_exp20=last_exp20,
        stressed_pf=ps.stressed_profit_factor(rows),
        slippage_coverage_share=ps.slippage_coverage(rows),
        mode=mode,
    )
    return {
        "_rolling_combined": rolling,
        "validation": validation,
        "concentration": conc,
        "drawdown": drawdown,
        "streaks": ps.losing_streaks(rows),
        "gross_profit_factor": _round(ps.profit_factor(float(t["realized_pnl"]) for t in enriched)),
        "net_profit_factor": _round(pf),
        "grading": grade,
    }


def _validation_brief(v: dict) -> dict:
    """The compact form shown in the comparison table: status, its label and the sentence."""
    return {
        "validation_status": v["status"],
        "validation_label": v["label"],
        "validation_summary": v["summary"],
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def build_insights(
    enriched: list[dict],
    sessions: list[str],
    sharpe: float | None,
    sortino: float | None,
    edge: dict | None = None,
    mode: str | None = None,
) -> dict:
    """Everything the Insights panel shows for the selected trades."""
    rows = trade_rows(enriched)
    names = sorted({r["strategy"] for r in rows})
    out = _core(rows, enriched, sessions, len(names), sharpe, sortino, edge, mode)
    rolling_combined = out.pop("_rolling_combined")
    out.update(
        {
            "deployment": ps.deployment(rows),
            "rolling": {
                "combined": rolling_combined,
                "by_strategy": {
                    n: ps.rolling_series([r for r in rows if r["strategy"] == n]) for n in names
                },
                "expectancy_window": ps.ROLLING_EXPECTANCY_WINDOW,
                "pf_window": ps.ROLLING_PF_WINDOW,
            },
            "monthly": ps.monthly_table(rows),
            "entry_quality": ps.entry_quality(rows),
            "slippage": ps.slippage_summary(enriched),
        }
    )
    return out


def comparison_extras(
    enriched_by_strategy: dict[str, list[dict]],
    sessions: list[str],
    ratios: dict[str, tuple[float | None, float | None]],
    edges: dict[str, dict] | None = None,
    mode: str | None = None,
) -> dict:
    """Extra columns per strategy, plus the combined portfolio, correlation and contribution.

    `ratios` maps a strategy name (and "__portfolio__") to its (sharpe, sortino); `edges` maps
    the same names to their edge-check results. Every status comes from the same `_core`.
    """
    edges = edges or {}
    per_strategy = {}
    all_enriched = [t for items in enriched_by_strategy.values() for t in items]
    for name, items in enriched_by_strategy.items():
        rows = trade_rows(items)
        sharpe, sortino = ratios.get(name, (None, None))
        core = _core(rows, items, sessions, 1, sharpe, sortino, edges.get(name), mode)
        r_values = [r["net_r"] for r in rows if r["net_r"] is not None]
        per_strategy[name] = {
            "gross_profit_factor": core["gross_profit_factor"],
            "net_profit_factor": core["net_profit_factor"],
            "expectancy_net_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
            "net_r_covered": len(r_values),
            "return_on_capital_pct": core["drawdown"]["return_on_capital_pct"],
            "max_drawdown_pct": core["drawdown"]["max_drawdown_pct"],
            "recovery_factor": core["drawdown"]["recovery_factor"],
            "top1_day_share_pct": core["concentration"].get("top1_days_share_pct"),
            "top5_days_share_pct": core["concentration"].get("top5_days_share_pct"),
            "overall_grade": core["grading"]["overall"],
            "preliminary_grade": core["grading"]["preliminary"],
            "overall_note": core["grading"]["overall_note"],
            **_validation_brief(core["validation"]),
        }
    portfolio = None
    series = {}
    if all_enriched:
        sharpe, sortino = ratios.get("__portfolio__", (None, None))
        rows = trade_rows(all_enriched)
        core = _core(
            rows,
            all_enriched,
            sessions,
            len(enriched_by_strategy),
            sharpe,
            sortino,
            edges.get("__portfolio__"),
            mode,
        )
        portfolio = {
            "net": core["drawdown"]["net"],
            "net_profit_factor": core["net_profit_factor"],
            "return_on_capital_pct": core["drawdown"]["return_on_capital_pct"],
            "max_drawdown": core["drawdown"]["max_drawdown"],
            "max_drawdown_pct": core["drawdown"]["max_drawdown_pct"],
            "recovery_factor": core["drawdown"]["recovery_factor"],
            "overall_grade": core["grading"]["overall"],
            "preliminary_grade": core["grading"]["preliminary"],
            "overall_note": core["grading"]["overall_note"],
            **_validation_brief(core["validation"]),
        }
        series = {
            name: _session_values(trade_rows(items), sessions)
            for name, items in enriched_by_strategy.items()
        }
    return {
        "per_strategy": per_strategy,
        "portfolio": portfolio,
        "correlation": ps.daily_correlation(series) if len(series) > 1 else None,
        "contribution": ps.portfolio_contribution(series) if len(series) > 1 else [],
    }


def excursion_report(enriched_with_excursions: list[dict]) -> dict:
    """MAE/MFE quality for trades that already carry rupee `mfe`/`mae`."""
    rows = []
    for t in enriched_with_excursions:
        rows.append(
            {
                "strategy": t["strategy"],
                "mfe": t.get("mfe"),
                "mae": t.get("mae"),
                "risk_amount": t.get("risk_amount"),
                "realized_pnl": t["realized_pnl"],
                "net": float(t["realized_pnl"]) - float(t.get("cost") or 0.0),
                "data_flag": t.get("data_flag"),
            }
        )
    return ps.excursion_stats(rows)
