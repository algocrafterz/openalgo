"""Build the evaluation dataset for the evaluation package of the Performance page.

Everything here is read-only. The numbers come from the same two service calls
that feed the Performance page (`get_daily_performance` and
`get_strategy_comparison`); nothing is recomputed, so the package carries exactly the
figures the trader sees. This module adds only structure: a verdict label taken
from the existing edge verdict, small-sample flags, a fixed caveats block and a
size guard.

Strategies that the filter excluded never enter the dataset: it is built from
the `included` list of the filtered response and never from `all_strategies`.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import services.strategy_metrics_service as metrics_service
from services.strategy_daily_performance_service import (
    get_daily_performance,
    get_strategy_comparison,
)
from utils.logging import get_logger

logger = get_logger(__name__)

#: Strategies that get their own edge block. The rest keep their comparison row.
MAX_STRATEGIES_DETAILED = 8
#: A bucket with fewer trades than this is flagged underpowered. Same figure as
#: the platform's own minimum for an edge verdict (EDGE_MIN_TRADES).
BUCKET_MIN_TRADES = 30
#: Approximate size limit of the dataset, in tokens (characters / 4).
TOKEN_BUDGET = 20_000
CHARS_PER_TOKEN = 4
#: Symbols shown at each end of the by-symbol table.
SYMBOL_EXTREMES = 5

DETAIL_DROPPED = "compare row only (size cap)"
NO_TRADES_MESSAGE = "There are no closed trades in this selection to evaluate."
MODE_CHANGED_MESSAGE = (
    "The trading mode changed while the evaluation was being prepared. Try again."
)

#: Edge verdict (from edge_stats) to the only labels an assessment may use.
LABEL_INSUFFICIENT = "insufficient data"
LABEL_NO_EDGE = "no distinguishable edge"
LABEL_POSSIBLE_EDGE = "possible edge (screening only)"
LABEL_NEGATIVE = "likely negative"
_VERDICT_LABELS = {
    "too few trades": LABEL_INSUFFICIENT,
    "too few days": LABEL_INSUFFICIENT,
    "not distinguishable from zero": LABEL_NO_EDGE,
    "likely real": LABEL_POSSIBLE_EDGE,
    "likely negative": LABEL_NEGATIVE,
}

_POOLED_SCALARS = (
    "trades_count",
    "winning_trades",
    "losing_trades",
    "win_rate",
    "gross_profit",
    "gross_loss",
    "net_profit",
    "est_costs",
    "net_after_costs",
    "charges_pct_of_gross",
    "profit_factor",
    "avg_win",
    "avg_loss",
    "payoff_ratio",
    "expectancy",
    "net_expectancy",
    "max_drawdown",
    "ulcer_index",
    "recovery_factor",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "trading_days",
    "calendar_days",
    "best_day",
    "worst_day",
    "expectancy_r",
    "total_r",
    "r_covered",
    "r_total",
    "entry_slippage_bps",
    "slippage_covered",
    "slippage_total",
    "fee_basis_assumed",
)
_EDGE_KEYS = (
    "edge_trades",
    "edge_cost_coverage",
    "net_pnl_std",
    "t_stat",
    "day_t_stat",
    "expectancy_ci95",
    "edge_days",
    "edge_verdict",
)
_DETAIL_SCALARS = (
    "net_profit",
    "net_after_costs",
    "win_rate",
    "profit_factor",
    "expectancy",
    "net_expectancy",
    "max_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "trading_days",
    "calendar_days",
    "best_day",
    "worst_day",
)

DEFINITIONS = {
    "net_profit": "Realised P&L before estimated charges (gross of charges), summed over the window.",
    "net_after_costs": "net_profit minus estimated charges; null when any trade could not be costed.",
    "net_expectancy": "Mean per-trade P&L after estimated charges (unknown charge counts as 0).",
    "win_rate": "Percent of closed trades with realised P&L above zero.",
    "day_streaks": "Longest run of winning or losing trading DAYS, not trades.",
    "buckets": "gross_pnl in bucket tables is realised P&L before estimated charges.",
}

CAVEATS = (
    "The trade ledger is reliable only from {since}; the history is short, so every statistic rests on a small sample.",
    "Charges are estimated, not taken from contract notes. In the edge statistics an unknown charge counts as 0.",
    "Slippage is not part of the charges; fills already include it.",
    "Drawdown and streaks are measured on daily totals, not on individual trades.",
    "Calendar days with no trade are counted as zero P&L days, which dilutes Sharpe and Sortino.",
    "Capital is one fixed value applied to the whole window; it is not a record of capital used at the time.",
    "The edge thresholds (at least {min_trades} trades, at least {min_days} trading days, |t| of {t_threshold} or more) are screening heuristics, not proof.",
    "The t statistic and the 95 percent interval use a normal approximation. It is too narrow below 30 trades (at 5 trades the true multiplier is about 2.78, not 1.96).",
    "Bucket tables (hour, weekday, holding time, symbol) are exploratory. Many buckets are tested at once, so some will look good or bad by chance; buckets_tested gives the count. The symbol table shows only the best and the worst symbols.",
    "Results are for one mode only (see meta.mode_display). Live and Sandbox are never mixed.",
    "R multiples exist only for trades whose signal stop could be matched (see r_covered and r_total).",
    "A per-trade P&L distribution is not available in this dataset.",
)


def evidence_label(edge_verdict: str | None) -> str:
    """The evidence label for an existing edge verdict.

    No thresholds of its own: the mapping only renames what `edge_stats`
    already decided. An unknown or missing verdict reads as insufficient data.
    """
    return _VERDICT_LABELS.get(edge_verdict or "", LABEL_INSUFFICIENT)


def _pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys}


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile of an ascending list (q in 0..1)."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    frac = pos - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


def _daily_distribution(daily_pnl: list[dict]) -> dict[str, Any]:
    """Percentiles of daily P&L over days with a nonzero result. DAILY, not per trade."""
    values = sorted(float(d["pnl"]) for d in daily_pnl if d["pnl"] != 0)
    out: dict[str, Any] = {
        "basis": "DAILY realised P&L over trading days with a nonzero result (not per trade)",
        "n_days": len(values),
    }
    if not values:
        return out
    for label, q in (("p05", 0.05), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p95", 0.95)):
        out[label] = round(_percentile(values, q), 2)
    out["min"] = round(values[0], 2)
    out["max"] = round(values[-1], 2)
    return out


def _bucket_table(rows: list[dict], label_key: str, *, name: str) -> dict[str, Any]:
    """Re-shape breakdown rows with an underpowered flag. Values are passed through."""
    shaped = []
    for row in rows:
        item = {
            label_key: row[label_key],
            "n": row["trades"],
            "gross_pnl": row["net_pnl"],
            "win_rate": row["win_rate"],
            "underpowered": row["trades"] < BUCKET_MIN_TRADES,
        }
        for extra in ("avg_hold_min", "avg_pnl"):
            if extra in row:
                item[extra] = row[extra]
        shaped.append(item)
    return {
        "name": name,
        "buckets_tested": len(rows),
        "min_trades_per_bucket": BUCKET_MIN_TRADES,
        "rows": shaped,
    }


def _symbol_table(rows: list[dict]) -> dict[str, Any]:
    """Best and worst symbols by P&L (rows arrive best first)."""
    top = rows[:SYMBOL_EXTREMES]
    bottom = rows[SYMBOL_EXTREMES:][-SYMBOL_EXTREMES:]
    table = _bucket_table(top + bottom, "symbol", name="by_symbol (best and worst only)")
    table["buckets_tested"] = len(rows)
    return table


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a == 0 or var_b == 0:
        return None
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    return round(cov / math.sqrt(var_a * var_b), 3)


def _overlap(series: dict[str, list[float]]) -> list[dict[str, Any]]:
    """Pairwise Pearson correlation and shared loss days of daily P&L. Exploratory."""
    names = sorted(series)
    pairs = []
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            a, b = series[first], series[second]
            if len(a) != len(b):
                continue
            pairs.append(
                {
                    "a": first,
                    "b": second,
                    "n_days": len(a),
                    "pearson_daily_pnl": _pearson(a, b),
                    "shared_loss_days": sum(
                        1 for x, y in zip(a, b, strict=True) if x < 0 and y < 0
                    ),
                }
            )
    return pairs


def _model_view(value: Any) -> Any:
    """A copy of the dataset for the package: non-finite numbers become None.

    JSON has no NaN or infinity, so one in a figure would fail serialisation.
    """
    if isinstance(value, dict):
        return {k: _model_view(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_view(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def dataset_to_text(dataset: dict[str, Any]) -> str:
    """The dataset as deterministic JSON, exactly as it is put in the package.

    The package shows the mode only as `mode_display` (Live or Sandbox); the
    internal `mode` key stays on the server. The size guard measures this text.
    """
    view = _model_view(dataset)
    meta = view.get("meta")
    if isinstance(meta, dict):
        meta.pop("mode", None)
    return json.dumps(view, sort_keys=True, ensure_ascii=False, indent=1, allow_nan=False)


#: Keys that carry a clock reading. They never enter the package hash, so the same
#: data hashes the same on every request.
_VOLATILE_KEYS = frozenset({"generated_at", "timestamp"})


def _without_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_without_volatile(v) for v in value]
    return value


def package_hash(dataset: dict[str, Any]) -> str:
    """SHA-256 (hex) of the canonical JSON of the dataset, without any timestamp field.

    Canonical: sorted keys, no indent, no NaN, no ASCII escaping. The internal
    `mode` key is left out, as in `dataset_to_text`.
    """
    view = _without_volatile(_model_view(dataset))
    meta = view.get("meta")
    if isinstance(meta, dict):
        meta.pop("mode", None)
    canonical = json.dumps(
        view, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def estimate_tokens(dataset: dict[str, Any]) -> int:
    """Rough token count of the package text: characters / 4."""
    return len(dataset_to_text(dataset)) // CHARS_PER_TOKEN


def _truncate(dataset: dict[str, Any]) -> None:
    """Shrink the dataset to TOKEN_BUDGET, deterministically, recording what went.

    Order: per-strategy detail (smallest strategies first), then bucket rows
    (least populated first), then comparison rows (smallest strategies first).
    """
    dropped: list[str] = []

    def over() -> bool:
        return estimate_tokens(dataset) > TOKEN_BUDGET

    blocks = sorted(dataset["strategies"], key=lambda b: (_trades_of(b), b["strategy"]))
    for block in blocks:
        if not over():
            break
        if isinstance(block["detail"], dict):
            block["detail"] = DETAIL_DROPPED
            dataset["overlap"] = [
                p for p in dataset["overlap"] if block["strategy"] not in (p["a"], p["b"])
            ]
            dropped.append(f"detail of strategy {block['strategy']}")

    while over():
        candidates = [
            (row["n"], key, idx)
            for key, table in sorted(dataset["buckets"].items())
            for idx, row in enumerate(table["rows"])
        ]
        if not candidates:
            break
        _n, key, idx = min(candidates)
        del dataset["buckets"][key]["rows"][idx]
        dropped.append(f"bucket row in {key}")

    for block in blocks:
        if not over():
            break
        if block["compare"] is not None:
            block["compare"] = None
            dropped.append(f"comparison row of strategy {block['strategy']}")

    dataset["meta"]["truncated"] = dropped
    dataset["meta"]["detailed_strategies"] = sorted(
        b["strategy"] for b in blocks if isinstance(b["detail"], dict)
    )


def _trades_of(block: dict[str, Any]) -> int:
    return int(block["trades_count"])


def _strategy_block(row: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    compare = {k: v for k, v in row.items() if k != "strategy"}
    block: dict[str, Any] = {
        "strategy": row["strategy"],
        "trades_count": row["trades_count"],
        "evidence_label": evidence_label(row.get("edge_verdict")),
        "compare": compare,
        "detail": DETAIL_DROPPED,
    }
    if detail is not None:
        block["detail"] = {
            **_pick(detail, _DETAIL_SCALARS),
            **_pick(detail, _EDGE_KEYS),
            "hold_stats": detail.get("hold_stats"),
            "day_streaks": {
                "best_win_days": detail.get("best_win_streak"),
                "best_loss_days": detail.get("best_loss_streak"),
            },
        }
        if "capital_view" in detail:
            block["detail"]["capital_view"] = detail["capital_view"]
    return block


def build_evaluation_dataset(
    include: list[str] | None,
    exclude: list[str] | None,
    strategy: str | None,
    period: str,
    start: str | None = None,
    end: str | None = None,
    capital: float | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Build the evaluation dataset for the selection the Performance page shows.

    Args:
        include: Further strategies to include; `strategy` is the first.
        exclude: Strategies to leave out (exclusion wins).
        strategy: First included strategy, or None for every strategy.
        period: A Performance page period keyword.
        start: Custom range start, YYYY-MM-DD.
        end: Custom range end, YYYY-MM-DD.
        capital: Optional fixed capital in rupees (already validated).

    Returns:
        (ok, body, status). On failure the body is {"status": "error",
        "message": <trader-facing text>}. On success it is the dataset, ready
        to serialise. Mode is resolved by the services, never by the caller.
    """
    names = ([strategy] if strategy else []) + list(include or [])
    extra = {} if capital is None else {"capital": capital}
    ok, pooled, status = get_daily_performance(
        strategy,
        period,
        start=start,
        end=end,
        include=list(include or []),
        exclude=exclude,
        **extra,
    )
    if not ok:
        return False, pooled, status
    if not pooled.get("trades_count") or not pooled.get("included"):
        return False, {"status": "error", "message": NO_TRADES_MESSAGE}, 400

    ok, comparison, status = get_strategy_comparison(
        period, start=start, end=end, include=names, exclude=exclude
    )
    if not ok:
        return False, comparison, status
    if comparison.get("mode") != pooled.get("mode"):
        return False, {"status": "error", "message": MODE_CHANGED_MESSAGE}, 409

    included = list(pooled["included"])
    allowed = set(included)
    rows = [r for r in comparison["strategies"] if r["strategy"] in allowed]
    rows.sort(key=lambda r: (-r["trades_count"], r["strategy"]))

    details: dict[str, dict[str, Any]] = {}
    for row in rows[:MAX_STRATEGIES_DETAILED]:
        ok, one, _status = get_daily_performance(
            row["strategy"], period, start=start, end=end, include=[], exclude=None, **extra
        )
        if ok and one.get("mode") != pooled["mode"]:
            return False, {"status": "error", "message": MODE_CHANGED_MESSAGE}, 409
        if ok and one.get("trades_count"):
            details[row["strategy"]] = one

    series = {name: [float(d["pnl"]) for d in one["daily_pnl"]] for name, one in details.items()}
    mode = pooled["mode"]
    trades_count = pooled["trades_count"]
    trading_days = pooled["trading_days"]
    calendar_days = pooled["calendar_days"]
    floor = metrics_service.LEDGER_RELIABLE_SINCE.isoformat()

    pooled_block = {
        **_pick(pooled, _POOLED_SCALARS),
        **_pick(pooled, _EDGE_KEYS),
        "hold_stats": pooled.get("hold_stats"),
        "day_streaks": {
            "best_win_days": pooled.get("best_win_streak"),
            "best_loss_days": pooled.get("best_loss_streak"),
        },
        "evidence_label": evidence_label(pooled.get("edge_verdict")),
        "trade_frequency": {
            "per_trading_day": round(trades_count / trading_days, 2) if trading_days else None,
            "per_calendar_day": round(trades_count / calendar_days, 2) if calendar_days else None,
            "formula": "trades_count / trading_days, and trades_count / calendar_days",
        },
        "daily_pnl_distribution": _daily_distribution(pooled["daily_pnl"]),
        "pnl_distribution": "not available in this dataset",
    }
    if "capital_view" in pooled:
        pooled_block["capital_view"] = pooled["capital_view"]

    dataset: dict[str, Any] = {
        "meta": {
            "mode": mode,
            "mode_display": "Live" if mode == "live" else "Sandbox",
            "period": period,
            "start_date": pooled["start_date"],
            "end_date": pooled["end_date"],
            "window_starts_at_ledger_floor": pooled["start_date"] == floor,
            "ledger_reliable_since": floor,
            "capital": capital,
            "strategies_included": sorted(included),
            "excluded_strategy_count": len(pooled.get("excluded") or []),
            "detailed_strategies": sorted(details),
            "truncated": [],
        },
        "edge_screen": {
            "min_trades": metrics_service.EDGE_MIN_TRADES,
            "min_trading_days": metrics_service.EDGE_MIN_DAYS,
            "t_threshold": metrics_service.EDGE_T_THRESHOLD,
            "note": "screening heuristics, not proof",
        },
        "definitions": DEFINITIONS,
        "pooled": pooled_block,
        "strategies": [_strategy_block(r, details.get(r["strategy"])) for r in rows],
        "overlap": _overlap(series) if len(series) > 1 else [],
        "overlap_note": "Exploratory. Pearson correlation and shared loss days of calendar-day P&L, with n_days.",
        "buckets": {
            "by_entry_hour": _bucket_table(
                pooled["by_entry_hour"], "hour", name="entry hour (IST)"
            ),
            "by_exit_hour": _bucket_table(pooled["by_hour"], "hour", name="exit hour (IST)"),
            "by_weekday": _bucket_table(pooled["by_weekday"], "weekday", name="weekday"),
            "by_hold_bucket": _bucket_table(
                pooled["by_hold_bucket"], "bucket", name="holding time"
            ),
            "by_symbol": _symbol_table(pooled["by_symbol"]),
        },
        "caveats": [
            text.format(
                since=floor,
                min_trades=metrics_service.EDGE_MIN_TRADES,
                min_days=metrics_service.EDGE_MIN_DAYS,
                t_threshold=metrics_service.EDGE_T_THRESHOLD,
            )
            for text in CAVEATS
        ],
    }
    _truncate(dataset)
    return True, dataset, 200
