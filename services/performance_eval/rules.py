"""Rule-based report for the evaluation package.

Pure and deterministic: it reads the dataset and nothing else (no I/O, no clock,
no model). Every finding is one of three kinds:

- "math": a number or fact computed directly from the trades, restated.
- "heuristic": a named rule-of-thumb check. Its parameters are shown with it and
  it is never presented as a statistical proof.
- "not_available": the dataset cannot say.

The only verdict words are the four labels the dataset already carries. A
strategy labelled "insufficient data" gets its numbers and one sentence, and no
strength or weakness verdict anywhere in the report.
"""

from __future__ import annotations

from typing import Any

from services.performance_eval import dataset as ds

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Heuristic constants. Every one is a rule of thumb chosen for this report. None
# is a statistical result.
# ---------------------------------------------------------------------------

#: profit_concentration: how many of the best trading days are measured. The
#: dataset carries the best single day only, so this is 1.
CONCENTRATION_TOP_DAYS = 1
#: profit_concentration: flagged when the best day is at least this share of gross profit.
CONCENTRATION_FLAG = 0.5
#: loss_streak_days: flagged when the longest run of losing trading days is at least this.
LOSS_STREAK_FLAG = 4
#: bucket_spread: a bucket is compared only with at least this many trades (same
#: figure as the dataset's underpowered flag).
BUCKET_MIN_TRADES = ds.BUCKET_MIN_TRADES

HEURISTIC_CONSTANTS = {
    "CONCENTRATION_TOP_DAYS": CONCENTRATION_TOP_DAYS,
    "CONCENTRATION_FLAG": CONCENTRATION_FLAG,
    "LOSS_STREAK_FLAG": LOSS_STREAK_FLAG,
    "BUCKET_MIN_TRADES": BUCKET_MIN_TRADES,
}

KIND_MATH = "math"
KIND_HEURISTIC = "heuristic"
KIND_NOT_AVAILABLE = "not_available"
KINDS = (KIND_MATH, KIND_HEURISTIC, KIND_NOT_AVAILABLE)

TOO_FEW = "Too few trades to say anything reliable."
NOTHING_FLAGGED = "Nothing flagged by these rules; this does not mean there is no weakness."
MULTIPLE_COMPARISON = "Many slices were tested at once, so some differences appear by chance."

LEGEND = {
    KIND_MATH: "A number or fact computed directly from your trades.",
    KIND_HEURISTIC: ("A rule-of-thumb check. The cut-off is a choice, not a statistical proof."),
    KIND_NOT_AVAILABLE: "Something this data cannot tell you. Nothing is guessed.",
}

_BUCKET_TABLES = (
    ("by_entry_hour", "hour", "entry hour"),
    ("by_exit_hour", "hour", "exit hour"),
    ("by_weekday", "weekday", "weekday"),
    ("by_hold_bucket", "bucket", "holding time"),
)

_FIXED_LIMITATIONS = (
    "This report is made by fixed rules from your closed trades. No model was used.",
    "The rules cannot see market conditions, news or your reasons for a trade.",
    "A heuristic flag is a prompt to investigate, not a conclusion.",
)


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "not available"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _finding(
    fid: str,
    kind: str,
    statement: str,
    n: int,
    evidence: list[str],
    *,
    scope: str = "pooled",
    rule: dict[str, Any] | None = None,
    note: str | None = None,
    flagged: bool | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": fid,
        "kind": kind,
        "scope": scope,
        "statement": statement,
        "n": n,
        "evidence": [path for path in evidence if path],
    }
    if rule is not None:
        out["rule"] = rule
    if note is not None:
        out["note"] = note
    if flagged is not None:
        out["flagged"] = flagged
    return out


class _Scope:
    """One thing the report talks about: the pooled result or one strategy."""

    def __init__(self, name: str, label: str, n: int, values: dict[str, tuple[Any, str]]):
        self.name = name
        self.label = label
        self.n = n
        self.values = values

    def get(self, *keys: str) -> Any:
        for key in keys:
            if key in self.values and self.values[key][0] is not None:
                return self.values[key][0]
        return None

    def path(self, *keys: str) -> str:
        for key in keys:
            if key in self.values and self.values[key][0] is not None:
                return self.values[key][1]
        return self.values[keys[0]][1] if keys[0] in self.values else ""

    @property
    def insufficient(self) -> bool:
        return self.label == ds.LABEL_INSUFFICIENT

    @property
    def tag(self) -> str:
        return "Pooled" if self.name == "pooled" else self.name


def _collect(prefix: str, source: dict[str, Any], into: dict[str, tuple[Any, str]]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and key == "day_streaks":
            for sub, subvalue in value.items():
                into[sub] = (subvalue, f"{prefix}.day_streaks.{sub}")
        elif not isinstance(value, (dict, list)):
            into[key] = (value, f"{prefix}.{key}")


def _scopes(dataset: dict[str, Any]) -> list[_Scope]:
    pooled = dataset.get("pooled") or {}
    values: dict[str, tuple[Any, str]] = {}
    _collect("pooled", pooled, values)
    out = [
        _Scope(
            "pooled",
            pooled.get("evidence_label") or ds.LABEL_INSUFFICIENT,
            _int(pooled.get("trades_count")),
            values,
        )
    ]
    for i, block in enumerate(dataset.get("strategies") or []):
        vals: dict[str, tuple[Any, str]] = {}
        compare = block.get("compare")
        detail = block.get("detail")
        if isinstance(compare, dict):
            _collect(f"strategies[{i}].compare", compare, vals)
        if isinstance(detail, dict):
            _collect(f"strategies[{i}].detail", detail, vals)
        out.append(
            _Scope(
                str(block.get("strategy")),
                block.get("evidence_label") or ds.LABEL_INSUFFICIENT,
                _int(block.get("trades_count")),
                vals,
            )
        )
    return out


def _numbers(scope: _Scope, fid: str, parts: list[tuple[str, tuple[str, ...]]]) -> dict[str, Any]:
    """A math finding restating the named values of one scope."""
    texts, paths = [], []
    for title, keys in parts:
        texts.append(f"{title} {_fmt(scope.get(*keys))}")
        paths.append(scope.path(*keys))
    statement = f"{scope.tag}: " + ", ".join(texts) + f" ({scope.n} trades)."
    if scope.insufficient:
        statement += f" {TOO_FEW}"
    return _finding(
        fid,
        KIND_MATH,
        statement,
        scope.n,
        [p for p in paths if p],
        scope=scope.name,
    )


def _section(sid: str, title: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": sid, "title": title, "findings": findings}


def _each(scopes: list[_Scope], fid: str, parts: list[tuple[str, tuple[str, ...]]]) -> list[dict]:
    return [_numbers(s, f"{fid}.{i}", parts) for i, s in enumerate(scopes)]


# -- heuristics -------------------------------------------------------------------------


def _loss_streak(scope: _Scope, fid: str) -> dict[str, Any] | None:
    streak = scope.get("best_loss_days", "best_loss_streak")
    if streak is None:
        return None
    rule = {"name": "loss_streak_days", "parameters": {"flag_at_days": LOSS_STREAK_FLAG}}
    path = scope.path("best_loss_days", "best_loss_streak")
    if scope.insufficient:
        return _finding(
            fid,
            KIND_MATH,
            f"{scope.tag}: longest run of losing trading days is {_fmt(streak)} "
            f"({scope.n} trades). {TOO_FEW}",
            scope.n,
            [path],
            scope=scope.name,
        )
    flagged = streak >= LOSS_STREAK_FLAG
    text = f"{scope.tag}: longest run of losing trading days is {_fmt(streak)} ({scope.n} trades)."
    if flagged:
        text += f" This is at least {LOSS_STREAK_FLAG} days, so the rule flags it."
    return _finding(
        fid,
        KIND_HEURISTIC,
        text,
        scope.n,
        [path],
        scope=scope.name,
        rule=rule,
        note="Rule of thumb, not statistically proven.",
        flagged=flagged,
    )


CONCENTRATION_DEFINITION = (
    "the best day's net P&L divided by gross profit (the sum of winning trades); "
    "an approximation of how concentrated profit is"
)


def _concentration(scope: _Scope, fid: str) -> dict[str, Any] | None:
    best, gross = scope.get("best_day"), scope.get("gross_profit")
    if best is None or gross is None or gross <= 0:
        return None
    share = max(best, 0.0) / gross
    rule = {
        "name": "profit_concentration",
        "parameters": {
            "top_days": CONCENTRATION_TOP_DAYS,
            "flag_at_share": CONCENTRATION_FLAG,
            "formula": CONCENTRATION_DEFINITION,
        },
    }
    paths = [scope.path("best_day"), scope.path("gross_profit")]
    if scope.insufficient:
        return _finding(
            fid,
            KIND_MATH,
            f"{scope.tag}: the best trading day is {share:.2f} of gross profit "
            f"({scope.n} trades). Definition: {CONCENTRATION_DEFINITION}. {TOO_FEW}",
            scope.n,
            paths,
            scope=scope.name,
        )
    flagged = share >= CONCENTRATION_FLAG
    text = (
        f"{scope.tag}: the best trading day is {share:.2f} of gross profit "
        f"({scope.n} trades). Definition: {CONCENTRATION_DEFINITION}."
    )
    if flagged:
        text += f" This is at least {CONCENTRATION_FLAG}, so the rule flags it as high."
    return _finding(
        fid,
        KIND_HEURISTIC,
        text,
        scope.n,
        paths,
        scope=scope.name,
        rule=rule,
        note="Rule of thumb, not statistically proven.",
        flagged=flagged,
    )


def _drawdown_vs_profit(scope: _Scope, fid: str) -> dict[str, Any] | None:
    dd, net = scope.get("max_drawdown"), scope.get("net_profit")
    if dd is None or net is None:
        return None
    paths = [scope.path("max_drawdown"), scope.path("net_profit")]
    base = (
        f"{scope.tag}: max drawdown is {_fmt(dd)} and net profit is {_fmt(net)} ({scope.n} trades)."
    )
    if scope.insufficient:
        return _finding(fid, KIND_MATH, f"{base} {TOO_FEW}", scope.n, paths, scope=scope.name)
    if net <= 0:
        return _finding(
            fid,
            KIND_MATH,
            f"{base} Net profit is zero or negative, so there is no profit to compare "
            "the drawdown with.",
            scope.n,
            paths,
            scope=scope.name,
        )
    flagged = abs(dd) > net
    if flagged:
        base += " The drawdown is larger than net profit."
    return _finding(
        fid,
        KIND_HEURISTIC,
        base,
        scope.n,
        paths,
        scope=scope.name,
        rule={
            "name": "drawdown_vs_profit",
            "parameters": {"compare": "abs(max_drawdown) > net_profit"},
        },
        note="A plain comparison, not statistically proven.",
        flagged=flagged,
    )


def _cost_drag(scope: _Scope, fid: str) -> list[dict[str, Any]]:
    pct = scope.get("charges_pct_of_gross")
    out = [
        _finding(
            f"{fid}.math",
            KIND_MATH,
            f"{scope.tag}: estimated charges are "
            + (f"{_fmt(pct)} percent of gross " if pct is not None else "")
            + f"({scope.n} trades). Estimated charges {_fmt(scope.get('est_costs'))}."
            + (f" {TOO_FEW}" if scope.insufficient else ""),
            scope.n,
            [scope.path("charges_pct_of_gross"), scope.path("est_costs")],
            scope=scope.name,
        )
    ]
    after = scope.get("net_after_costs")
    costs, net = scope.get("est_costs"), scope.get("net_profit")
    if scope.insufficient or after is None or costs is None or net is None:
        return out
    flagged = net > 0 and costs >= net
    text = (
        f"{scope.tag}: every trade could be costed; estimated charges are {_fmt(costs)} "
        f"against net profit of {_fmt(net)} ({scope.n} trades)."
    )
    if flagged:
        text += " Charges are at least as large as net profit."
    out.append(
        _finding(
            f"{fid}.rule",
            KIND_HEURISTIC,
            text,
            scope.n,
            [scope.path("est_costs"), scope.path("net_profit"), scope.path("net_after_costs")],
            scope=scope.name,
            rule={"name": "cost_drag", "parameters": {"compare": "est_costs >= net_profit"}},
            note="A plain comparison, not statistically proven.",
            flagged=flagged,
        )
    )
    return out


def _bucket_findings(
    dataset: dict[str, Any], pooled: _Scope, table: str, label_key: str, title: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Findings for one bucket table, and the weakest eligible bucket (for weaknesses)."""
    data = (dataset.get("buckets") or {}).get(table) or {}
    rows = data.get("rows") or []
    tested = _int(data.get("buckets_tested")) or len(rows)
    fid = f"bucket_spread.{table}"
    rule = {
        "name": "bucket_spread",
        "parameters": {"min_trades_per_bucket": BUCKET_MIN_TRADES, "ranked_by": "gross_pnl"},
    }
    base_path = f"buckets.{table}.rows"
    eligible = [r for r in rows if _int(r.get("n")) >= BUCKET_MIN_TRADES]
    weak = [r for r in rows if _int(r.get("n")) < BUCKET_MIN_TRADES]
    findings: list[dict[str, Any]] = []
    worst = None
    if pooled.insufficient:
        findings.append(
            _finding(
                fid,
                KIND_HEURISTIC,
                f"{title}: {tested} buckets tested ({pooled.n} trades in total). {TOO_FEW} "
                "Treat every slice as exploratory.",
                pooled.n,
                [base_path],
                rule=rule,
                note=MULTIPLE_COMPARISON,
                flagged=False,
            )
        )
    elif not eligible:
        findings.append(
            _finding(
                fid,
                KIND_HEURISTIC,
                f"{title}: No bucket has enough trades to compare (need {BUCKET_MIN_TRADES} "
                f"each); treat every slice as exploratory. Buckets tested: {tested}.",
                pooled.n,
                [base_path],
                rule=rule,
                note=MULTIPLE_COMPARISON,
                flagged=False,
            )
        )
    else:
        ordered = sorted(eligible, key=lambda r: (r.get("gross_pnl") or 0.0, str(r.get(label_key))))
        low, high = ordered[0], ordered[-1]
        worst = {**low, "_table": table, "_title": title, "_label_key": label_key}
        if len(ordered) == 1:
            text = (
                f"{title}: only one bucket has at least {BUCKET_MIN_TRADES} trades "
                f"({label_key} {low.get(label_key)}, {_fmt(low.get('gross_pnl'))} gross, "
                f"{low['n']} trades), so there is nothing to compare. Buckets tested: {tested}."
            )
        else:
            text = (
                f"{title}: among {len(ordered)} buckets with at least {BUCKET_MIN_TRADES} "
                f"trades, the highest gross P&L is {label_key} {high.get(label_key)} "
                f"({_fmt(high.get('gross_pnl'))}, {high['n']} trades) and the lowest is "
                f"{label_key} {low.get(label_key)} ({_fmt(low.get('gross_pnl'))}, "
                f"{low['n']} trades). Buckets tested: {tested}."
            )
        findings.append(
            _finding(
                fid,
                KIND_HEURISTIC,
                text,
                sum(_int(r["n"]) for r in ordered),
                [base_path],
                rule=rule,
                note=MULTIPLE_COMPARISON + " A difference here is not proof.",
                flagged=False,
            )
        )
    if weak:
        listing = ", ".join(f"{r.get(label_key)} (n={_int(r.get('n'))})" for r in weak)
        findings.append(
            _finding(
                f"{fid}.exploratory",
                KIND_HEURISTIC,
                f"{title}: exploratory only, too few trades to rank: {listing}.",
                sum(_int(r.get("n")) for r in weak),
                [base_path],
                rule=rule,
                note="Not ranked and not a finding.",
                flagged=False,
            )
        )
    return findings, worst


# -- the report ---------------------------------------------------------------------------


def build_report(dataset: dict[str, Any]) -> dict[str, Any]:
    """The rule-based report for a dataset. Deterministic; reads only the dataset."""
    scopes = _scopes(dataset)
    pooled, strategies = scopes[0], scopes[1:]
    screen = dataset.get("edge_screen") or {}
    flags: list[dict[str, Any]] = []
    worst_buckets: list[dict[str, Any]] = []

    # profitability
    profitability = _each(
        scopes,
        "profitability",
        [
            ("net profit before charges", ("net_profit",)),
            ("after estimated charges", ("net_after_costs",)),
        ],
    )
    profitability += _cost_drag(pooled, "profitability.cost")
    flags += [f for f in profitability if f["kind"] == KIND_HEURISTIC and f.get("flagged")]

    # expectancy and edge
    expectancy: list[dict[str, Any]] = []
    for i, s in enumerate(scopes):
        parts = [
            ("expectancy per trade", ("expectancy",)),
            ("after charges", ("net_expectancy",)),
        ]
        if s.insufficient:
            expectancy.append(_numbers(s, f"expectancy_edge.{i}", parts))
            continue
        text = (
            f"{s.tag}: expectancy per trade {_fmt(s.get('expectancy'))}, after charges "
            f"{_fmt(s.get('net_expectancy'))}, t statistic {_fmt(s.get('t_stat'))}, "
            f"day t statistic {_fmt(s.get('day_t_stat'))}, edge screen result: {s.label} "
            f"({s.n} trades)."
        )
        expectancy.append(
            _finding(
                f"expectancy_edge.{i}",
                KIND_MATH,
                text,
                s.n,
                [s.path("expectancy"), s.path("t_stat"), s.path("edge_verdict")],
                scope=s.name,
            )
        )
    ci_raw = (dataset.get("pooled") or {}).get("expectancy_ci95")
    if ci_raw and not pooled.insufficient:
        expectancy.append(
            _finding(
                "expectancy_edge.ci",
                KIND_MATH,
                f"Pooled: the 95 percent interval for expectancy per trade is "
                f"{_fmt(ci_raw[0])} to {_fmt(ci_raw[1])} ({pooled.n} trades). It uses a "
                "normal approximation, which is too narrow for small samples.",
                pooled.n,
                ["pooled.expectancy_ci95"],
            )
        )

    # profit factor, win rate
    pf = _each(
        scopes,
        "profit_factor_win_rate",
        [("profit factor", ("profit_factor",)), ("win rate percent", ("win_rate",))],
    )
    pf.append(
        _numbers(
            pooled,
            "profit_factor_win_rate.size",
            [
                ("mean win", ("avg_win",)),
                ("mean loss", ("avg_loss",)),
                ("payoff ratio", ("payoff_ratio",)),
            ],
        )
    )

    # drawdown and recovery
    drawdown: list[dict[str, Any]] = _each(
        scopes,
        "drawdown_recovery",
        [("max drawdown", ("max_drawdown",)), ("recovery factor", ("recovery_factor",))],
    )
    for i, s in enumerate(scopes):
        item = _drawdown_vs_profit(s, f"drawdown_recovery.rule.{i}")
        if item:
            drawdown.append(item)
            if item.get("flagged"):
                flags.append(item)

    # consistency
    consistency: list[dict[str, Any]] = [
        _numbers(
            pooled,
            "consistency.days",
            [
                ("trading days", ("trading_days",)),
                ("best day", ("best_day",)),
                ("worst day", ("worst_day",)),
                ("longest winning run of days", ("best_win_days",)),
            ],
        )
    ]
    distribution = (dataset.get("pooled") or {}).get("daily_pnl_distribution") or {}
    if distribution.get("n_days"):
        pieces = ", ".join(
            f"{k} {_fmt(distribution.get(k))}"
            for k in ("p05", "p25", "p50", "p75", "p95")
            if k in distribution
        )
        consistency.append(
            _finding(
                "consistency.distribution",
                KIND_MATH,
                f"Pooled: daily P&L over {distribution['n_days']} days with a result: {pieces}.",
                pooled.n,
                ["pooled.daily_pnl_distribution"],
            )
        )
    item = _concentration(pooled, "consistency.concentration")
    if item:
        consistency.append(item)
        if item.get("flagged"):
            flags.append(item)
    for i, s in enumerate(scopes):
        item = _loss_streak(s, f"consistency.loss_streak.{i}")
        if item:
            consistency.append(item)
            if item.get("flagged"):
                flags.append(item)

    # buckets
    hours: list[dict[str, Any]] = []
    for table, label_key, title in _BUCKET_TABLES:
        if table == "by_hold_bucket":
            continue
        found, worst = _bucket_findings(dataset, pooled, table, label_key, title)
        hours += found
        if worst:
            worst_buckets.append(worst)

    holding: list[dict[str, Any]] = []
    hold = (dataset.get("pooled") or {}).get("hold_stats") or {}
    if hold.get("covered"):
        holding.append(
            _finding(
                "holding_duration.stats",
                KIND_MATH,
                f"Pooled: holding time in minutes: mean {_fmt(hold.get('avg_min'))}, median "
                f"{_fmt(hold.get('median_min'))}, 90th percentile {_fmt(hold.get('p90_min'))}; "
                f"winners mean {_fmt(hold.get('winners_avg_min'))}, losers mean "
                f"{_fmt(hold.get('losers_avg_min'))} ({hold.get('covered')} of "
                f"{hold.get('total')} trades have an entry time).",
                _int(hold.get("covered")),
                ["pooled.hold_stats"],
            )
        )
    else:
        holding.append(
            _finding(
                "holding_duration.stats",
                KIND_NOT_AVAILABLE,
                "No trade has an entry time, so holding time is not available.",
                0,
                ["pooled.hold_stats"],
            )
        )
    found, worst = _bucket_findings(dataset, pooled, "by_hold_bucket", "bucket", "holding time")
    holding += found
    if worst:
        worst_buckets.append(worst)

    market = [
        _finding(
            "market_conditions.none",
            KIND_NOT_AVAILABLE,
            "This data has no volatility, trend or market-regime information, so the report "
            "cannot say how the strategies behave in different market conditions. The only "
            "calendar slices available are weekday and hour.",
            pooled.n,
            [],
        )
    ]

    # sample size
    sample = [
        _finding(
            "sample_size.screen",
            KIND_HEURISTIC,
            f"Pooled: {pooled.n} trades over {_fmt(pooled.get('trading_days'))} trading days. "
            f"The platform screens for at least {_fmt(screen.get('min_trades'))} trades and at "
            f"least {_fmt(screen.get('min_trading_days'))} trading days before it reports an "
            f"edge result. These are screening heuristics, not proof. Label: {pooled.label}."
            + (f" {TOO_FEW}" if pooled.insufficient else ""),
            pooled.n,
            ["pooled.trades_count", "pooled.trading_days", "edge_screen"],
            rule={
                "name": "edge_screen",
                "parameters": {
                    "min_trades": screen.get("min_trades"),
                    "min_trading_days": screen.get("min_trading_days"),
                },
            },
            note="Rule of thumb, not statistically proven.",
            flagged=False,
        )
    ]
    for i, s in enumerate(strategies):
        sample.append(
            _finding(
                f"sample_size.{i}",
                KIND_MATH,
                f"{s.tag}: {s.n} trades, {_fmt(s.get('trading_days'))} trading days, label "
                f"{s.label}." + (f" {TOO_FEW}" if s.insufficient else ""),
                s.n,
                [f"strategies[{i}].trades_count", f"strategies[{i}].evidence_label"],
                scope=s.name,
            )
        )

    # weaknesses
    weaknesses: list[dict[str, Any]] = []
    tests = {
        "profit_concentration": "Test: backtest the result with the best day removed and see whether it holds.",
        "loss_streak_days": "Test: backtest whether the losing days cluster on particular days or hours.",
        "drawdown_vs_profit": "Test: backtest over a longer window to see how often a drawdown this large occurs.",
        "cost_drag": "Test: backtest the effect of fewer trades or different order types on charges.",
    }
    for flag in flags:
        name = flag["rule"]["name"]
        weaknesses.append(
            _finding(
                f"weaknesses.{flag['id']}",
                KIND_HEURISTIC,
                f"{flag['statement']} {tests.get(name, '')}".strip(),
                flag["n"],
                list(flag["evidence"]),
                scope=flag["scope"],
                rule=flag["rule"],
                note="Area to investigate. Rule of thumb, not statistically proven.",
                flagged=True,
            )
        )
    for worst in worst_buckets:
        label_key = worst["_label_key"]
        weaknesses.append(
            _finding(
                f"weaknesses.bucket.{worst['_table']}",
                KIND_HEURISTIC,
                f"{worst['_title']}: {label_key} {worst.get(label_key)} has the lowest gross P&L "
                f"({_fmt(worst.get('gross_pnl'))}, {_int(worst.get('n'))} trades) among buckets "
                f"with enough trades. Test: backtest with this slice removed to see whether "
                "the difference holds. " + MULTIPLE_COMPARISON,
                _int(worst.get("n")),
                [f"buckets.{worst['_table']}.rows"],
                rule={
                    "name": "bucket_spread",
                    "parameters": {"min_trades_per_bucket": BUCKET_MIN_TRADES},
                },
                note="Exploratory. Not proof.",
                flagged=False,
            )
        )
    if not weaknesses:
        weaknesses.append(_finding("weaknesses.none", KIND_MATH, NOTHING_FLAGGED, pooled.n, []))

    sections = [
        _section("profitability", "Profitability", profitability),
        _section("expectancy_edge", "Expectancy and edge", expectancy),
        _section("profit_factor_win_rate", "Profit factor and win rate", pf),
        _section("drawdown_recovery", "Drawdown and recovery", drawdown),
        _section("consistency", "Consistency", consistency),
        _section("entry_hour", "Entry hour, exit hour and weekday", hours),
        _section("holding_duration", "Holding duration", holding),
        _section("market_conditions", "Market conditions", market),
        _section("sample_size", "Sample size", sample),
        _section("weaknesses", "Areas to investigate", weaknesses),
    ]
    caveats = [c for c in (dataset.get("caveats") or []) if isinstance(c, str)]
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": {
            "n_trades": pooled.n,
            "trading_days": pooled.get("trading_days"),
            "strategies": len(strategies),
            "labels": {
                "pooled": pooled.label,
                "per_strategy": {s.name: s.label for s in strategies},
            },
        },
        "sections": sections,
        "limitations": list(_FIXED_LIMITATIONS) + caveats,
        "legend": dict(LEGEND),
    }
