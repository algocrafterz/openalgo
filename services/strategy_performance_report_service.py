"""Simple strategy performance report (paper, live or both) as one self-contained HTML file.

A monitoring report for the owner, not a promotion system. It reuses the Performance page
calculations (strategy_metrics_service.compute_metrics, strategy_trade_enrichment) and adds no
new thresholds: the sample-size and trend rules are the ones in the strategy-performance-analyst
skill (windows under 10 trades are not assessed, 10 to 19 are low confidence, trends need 20+).

Used by the skill, the Friday close-day step and the Strategy Reports routes:
    generate_report("paper" | "live" | "both")  -> Path of the saved file
    list_reports() / report_path(filename)       -> saved files (name whitelist, no traversal)

Command line (from the repo root):
    PYTHONPATH=. uv run python -m services.strategy_performance_report_service --mode both

Read only: every SQLite file is opened with mode=ro. Paper = ledger rows stored as 'analyze'.
Paper and live are never mixed and never summed. Charges are estimates (no actual-charge source).
On any failure ReportError is raised and no file is written.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO_ROOT / "reports" / "strategy-performance"
LEDGER_DB = REPO_ROOT / "db" / "openalgo.db"
SIGNAL_DB = REPO_ROOT / "signal_engine" / "data" / "trades.db"

MODES = ("paper", "live", "both")
DB_MODE = {"paper": "analyze", "live": "live"}
FILENAME_RE = re.compile(r"^(paper|live|both)-(\d{4})-(\d{2})-(\d{2})\.html$")
WINDOW_SESSIONS = 5  # recent / previous window size (skill step 2)
ACTIVE_SESSIONS = 10  # a strategy is active when it traded in the last 10 sessions (skill step 1)
NA = "insufficient data"
IST = timezone(timedelta(hours=5, minutes=30))

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"

_patch_lock = threading.Lock()


class ReportError(Exception):
    """The report could not be produced. Nothing was written."""


# ---------------------------------------------------------------- data access (read only)


def _ro(path: Path) -> sqlite3.Connection:
    if not Path(path).exists():
        raise ReportError(f"Database not found: {Path(path).name}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def _readonly_signal_engine():
    """The enrichment helper opens trades.db with a plain engine. For this process only, make it
    read-only for the duration of the report. No repo file changes."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    import services.strategy_trade_enrichment as ste

    def _ro_engine(path):
        key = Path(path).resolve()
        return create_engine(f"sqlite:///file:{key}?mode=ro&uri=true", poolclass=NullPool)

    with _patch_lock:
        original = ste._engine_for
        ste._engine_for = _ro_engine
        try:
            yield
        finally:
            ste._engine_for = original


def _parse_names(value) -> list[str]:
    """Comma-separated exact strategy names (or an iterable of names): strip, drop empties."""
    if value is None:
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)
    return [p.strip() for p in parts if isinstance(p, str) and p.strip()]


def _parse_date(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ReportError(f"{label} must be YYYY-MM-DD.")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ReportError(f"{label} must be a valid YYYY-MM-DD date.") from exc
    return value


def _load_closed(ledger_db: Path, db_mode: str, floor: str, cutoff: str | None) -> list[dict]:
    conn = _ro(ledger_db)
    try:
        sql = "select * from strategy_closed_trades where mode=? and trade_date>=?"
        args: list = [db_mode, floor]
        if cutoff:
            sql += " and trade_date<=?"
            args.append(cutoff)
        return [dict(r) for r in conn.execute(sql + " order by trade_date, id", args)]
    except sqlite3.Error as exc:
        raise ReportError(f"Could not read closed trades: {type(exc).__name__}") from exc
    finally:
        conn.close()


def _load_exits(signal_db: Path, db_mode: str, floor: str, cutoff: str | None) -> list[dict]:
    """EXIT rows with their exit types. Missing or unreadable file: no exit data."""
    if not Path(signal_db).exists():
        return []
    try:
        conn = _ro(signal_db)
        try:
            rows = conn.execute(
                "select strategy, substr(coalesce(executed_at, received_at), 1, 10) d, context "
                "from trades where trade_mode=? and direction='EXIT' and status='SUCCESS'",
                (db_mode,),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, ReportError):
        return []
    out = []
    for r in rows:
        day = r["d"] or ""
        if day < floor or (cutoff and day > cutoff):
            continue
        try:
            types = json.loads(r["context"]).get("exit_types") or ["unknown"]
        except (TypeError, ValueError, AttributeError):
            types = ["unknown"]
        out.append(
            {"strategy": r["strategy"], "date": day, "type": "+".join(str(t) for t in types)}
        )
    return out


# ---------------------------------------------------------------- numbers


def _confidence(n: int) -> str:
    if n < 10:
        return "not assessed (fewer than 10 trades)"
    if n < 20:
        return "low confidence (10 to 19 trades)"
    return "ok (20 or more trades)"


def _window_stats(trades: list[dict], days: list[str], signal_db: Path) -> dict:
    """Metrics of one strategy over one window of sessions."""
    from services.strategy_metrics_service import compute_metrics
    from services.strategy_trade_enrichment import enrich_trades, summarize_enrichment

    out = {
        "from": days[0] if days else None,
        "to": days[-1] if days else None,
        "trades": len(trades),
    }
    out["confidence"] = _confidence(len(trades))
    if not trades:
        return out
    m = compute_metrics(trades, days[0], days[-1])
    enriched = enrich_trades(trades, days[0], days[-1], db_path=signal_db)
    summary = summarize_enrichment(enriched)
    costs = [e["cost"] for e in enriched if e["cost"] is not None]
    gross = round(sum(float(t["realized_pnl"]) for t in trades), 2)
    charges = round(sum(costs), 2)
    n = len(trades)
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t["trade_date"]] = by_day.get(t["trade_date"], 0.0) + float(t["realized_pnl"])
    out.update(
        gross=gross,
        charges=charges,
        charges_covered=len(costs),
        net=round(gross - charges, 2),
        win_rate=m["win_rate"],
        profit_factor=m["profit_factor"],
        gross_loss=m["gross_loss"],
        expectancy_net=round((gross - charges) / n, 2),
        avg_win=m["avg_win"],
        avg_loss=m["avg_loss"],
        max_drawdown=m["max_drawdown"],
        exp_net_r=summary.get("expectancy_net_r"),
        r_covered=summary.get("net_r_covered"),
        own_days=len(by_day),
        by_day=by_day,
    )
    if len(by_day) >= 3:
        vals = sorted(by_day.values())
        out["gross_without_best_worst_day"] = round(sum(vals[1:-1]), 2)
    return out


def _trend(recent: dict, prev: dict) -> tuple[str, list[str]]:
    """Recent vs previous with the skill's existing rules. Returns (label, detail lines)."""
    nr, npv = recent["trades"], prev["trades"]
    if nr < 10 or npv < 10:
        return (
            f"{NA}: a window has fewer than 10 trades (recent {nr}, previous {npv}); not assessed",
            [],
        )
    if nr < 20 or npv < 20:
        direction = (
            "higher"
            if recent["expectancy_net"] > prev["expectancy_net"]
            else ("lower" if recent["expectancy_net"] < prev["expectancy_net"] else "the same")
        )
        return (
            f"low confidence (recent {nr}, previous {npv} trades): net result per trade is "
            f"{direction} than before; direction only, not a trend",
            [],
        )
    up = down = 0
    lines = []
    d = recent["win_rate"] - prev["win_rate"]
    if d >= 10:
        up += 1
    elif d <= -10:
        down += 1
    lines.append(f"win rate {prev['win_rate']:.0f}% to {recent['win_rate']:.0f}%")
    rp, pp = recent["profit_factor"], prev["profit_factor"]
    if rp is not None and pp:
        change = (rp - pp) / pp
        if change >= 0.25:
            up += 1
        elif change <= -0.25:
            down += 1
        lines.append(f"profit factor {pp:.2f} to {rp:.2f}")
    re_, pe = recent.get("exp_net_r"), prev.get("exp_net_r")
    if re_ is not None and pe is not None:
        if re_ - pe >= 0.10:
            up += 1
        elif re_ - pe <= -0.10:
            down += 1
        lines.append(f"net expectancy {pe:+.2f}R to {re_:+.2f}R")
    if up >= 2 and down == 0:
        return "improving", lines
    if down >= 2 and up == 0:
        return "deteriorating", lines
    return "stable or mixed", lines


def _analyse_mode(
    mode: str,
    ledger_db: Path,
    signal_db: Path,
    cutoff: str | None,
    date_from: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    window_sessions: int | None = None,
) -> dict:
    from services.strategy_metrics_service import LEDGER_RELIABLE_SINCE

    floor = LEDGER_RELIABLE_SINCE.isoformat()
    if date_from and date_from > floor:
        floor = date_from
    db_mode = DB_MODE[mode]
    res: dict = {"mode": mode, "floor": floor}
    closed = _load_closed(ledger_db, db_mode, floor, cutoff)
    res["available_names"] = sorted({t["strategy"] for t in closed})
    if include:
        keep = set(include)
        closed = [t for t in closed if t["strategy"] in keep]
    if exclude:
        drop = set(exclude)
        closed = [t for t in closed if t["strategy"] not in drop]
    res["filtered_empty"] = bool(res["available_names"]) and not closed
    sessions = sorted({t["trade_date"] for t in closed})
    if not sessions:
        res["status"] = "no_data"
        return res
    n = window_sessions or WINDOW_SESSIONS
    if len(sessions) >= 2 * n:
        recent, prev = sessions[-n:], sessions[-2 * n : -n]
        res["window_note"] = ""
    else:
        h = len(sessions) // 2
        recent, prev = sessions[h:], sessions[:h]
        res["window_note"] = (
            f"Fewer than {2 * n} sessions have data, so recent is the last half and previous is the first half."
        )
    cut = sessions[-1]
    res.update(
        status="ok", cutoff=cut, sessions=len(sessions), recent_days=recent, previous_days=prev
    )
    active_set = set(sessions[-ACTIVE_SESSIONS:])
    names = sorted({t["strategy"] for t in closed})
    active = [
        s
        for s in names
        if any(t["strategy"] == s and t["trade_date"] in active_set for t in closed)
    ]
    res["inactive"] = [
        {"strategy": s, "last_seen": max(t["trade_date"] for t in closed if t["strategy"] == s)}
        for s in names
        if s not in active
    ]
    exit_names = {t["strategy"] for t in closed}
    exits = [e for e in _load_exits(signal_db, db_mode, floor, cutoff) if e["strategy"] in exit_names]
    strategies = {}
    with _readonly_signal_engine():
        for s in active:
            mine = [t for t in closed if t["strategy"] == s]
            windows = {}
            for label, days in (("recent", recent), ("previous", prev), ("all", sessions)):
                ds = set(days)
                windows[label] = _window_stats(
                    [t for t in mine if t["trade_date"] in ds], days, signal_db
                )
            own_sessions = len({t["trade_date"] for t in mine})
            label, lines = _trend(windows["recent"], windows["previous"])
            rec_days = set(recent)
            strategies[s] = {
                "windows": windows,
                "own_sessions": own_sessions,
                "trend": label,
                "trend_detail": lines,
                "recent_trades": [t for t in mine if t["trade_date"] in rec_days],
                "exits": [e for e in exits if e["strategy"] == s and e["date"] in rec_days],
            }
    res["strategies"] = strategies
    return res


# ---------------------------------------------------------------- text helpers


def _rs(v) -> str:
    if v is None:
        return "n/a"
    return f"{'-' if v < 0 else ''}Rs {abs(v):,.0f}"


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0f}%"


def _pf(w: dict) -> str:
    if not w.get("trades"):
        return "n/a"
    if w["profit_factor"] is None:
        return "no losing trades" if w.get("gross_loss", 0) == 0 else "n/a"
    return f"{w['profit_factor']:.2f}"


def _e(text) -> str:
    return escape(str(text), quote=True)


def _table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _ul(items: list[str]) -> str:
    if not items:
        return "<p>None.</p>"
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def _cell_na(w: dict, fn) -> str:
    return _e(NA) if not w.get("trades") else _e(fn(w))


# ---------------------------------------------------------------- sections per mode


def _no_live_text(mode: str, floor: str) -> str:
    label = "live" if mode == "live" else "paper"
    if mode == "live":
        return (
            "<p><b>No live performance report can yet be produced.</b> There are no live closed "
            f"trades in the trade ledger since {_e(floor)}. No broker snapshot or estimate is used in "
            "place of live trades. Nothing is invented. Paper results do not stand in for live results.</p>"
        )
    return f"<p><b>No {label} closed trades</b> exist since {_e(floor)}, so there is nothing to report.</p>"


def _summary(d: dict) -> str:
    if d["status"] != "ok":
        return _no_live_text(d["mode"], d["floor"])
    strategies = d["strategies"]
    nets = {
        s: v["windows"]["recent"].get("net")
        for s, v in strategies.items()
        if v["windows"]["recent"]["trades"]
    }
    total_trades = sum(v["windows"]["recent"]["trades"] for v in strategies.values())
    r_days = d["recent_days"]
    lines = [
        f"Reporting period (same for every strategy): the last {len(r_days)} sessions with data, "
        f"{_e(r_days[0])} to {_e(r_days[-1])}.",
        f"{len(strategies)} active strategies traded in the last {ACTIVE_SESSIONS} sessions; "
        f"{total_trades} closed trades in the reporting period.",
    ]
    if nets:
        lines.append(
            f"Total net P&amp;L after estimated charges for the {_e(d['mode'])} strategies in the period: "
            f"{_e(_rs(sum(nets.values())))} (this mode only; paper and live are never added together)."
        )
        ranked = sorted(nets.items(), key=lambda kv: kv[1], reverse=True)
        pos = [s for s, v in ranked if v > 0]
        neg = [s for s, v in ranked if v < 0]
        lines.append(
            f"{len(pos)} strategies made money after estimated charges and {len(neg)} lost money "
            f"in the period. Best: {_e(ranked[0][0])} ({_rs(ranked[0][1])}). "
            f"Weakest: {_e(ranked[-1][0])} ({_rs(ranked[-1][1])})."
        )
    for s, v in strategies.items():
        if v["windows"]["recent"]["trades"] == 0:
            lines.append(
                f"{_e(s)} had no closed trades in the period (a quiet period, not a failure)."
            )
    lines.append(
        f"Data covers sessions from {_e(d['floor'])} (the date the trade ledger is considered reliable) "
        f"up to {_e(d['cutoff'])}. Charges are estimates."
    )
    return _ul(lines)


def _comparison(d: dict) -> str:
    if d["status"] != "ok":
        return _no_live_text(d["mode"], d["floor"])
    rows = []
    for s, v in d["strategies"].items():
        w = v["windows"]["recent"]
        rows.append(
            [
                f"<b>{_e(s)}</b>",
                _e(w["trades"]),
                _cell_na(w, lambda w: _rs(w["net"])),
                _cell_na(w, lambda w: _pct(w["win_rate"])),
                _e(_pf(w)),
                _cell_na(w, lambda w: _rs(w["expectancy_net"])),
                _cell_na(w, lambda w: _rs(w["avg_win"])),
                _cell_na(w, lambda w: _rs(-w["avg_loss"] if w["avg_loss"] else 0)),
                _cell_na(w, lambda w: _rs(w["max_drawdown"])),
                _e(w["confidence"]),
            ]
        )
    tbl = _table(
        [
            "Strategy",
            "Closed trades",
            "Net P&L after est. charges",
            "Win rate",
            "Profit factor",
            "Expectancy per trade (net)",
            "Avg win",
            "Avg loss",
            "Max drawdown",
            "Sample",
        ],
        rows,
    )
    note = (
        "<p class='small'>Win rate, profit factor, average win and loss, and drawdown are before charges. "
        "Net P&L and expectancy are after estimated charges. Max drawdown is the largest fall of the "
        "running daily P&amp;L inside the period.</p>"
    )
    inactive = ""
    if d["inactive"]:
        inactive = (
            "<p class='small'>Not active in the last 10 sessions: "
            + ", ".join(
                f"{_e(i['strategy'])} (last traded {_e(i['last_seen'])})" for i in d["inactive"]
            )
            + ".</p>"
        )
    return tbl + note + inactive


def _trends(d: dict) -> str:
    if d["status"] != "ok":
        return _no_live_text(d["mode"], d["floor"])
    rows = []
    for s, v in d["strategies"].items():
        cells = [f"<b>{_e(s)}</b>"]
        for label in ("recent", "previous", "all"):
            w = v["windows"][label]
            if not w["trades"]:
                cells.append(_e(NA))
            else:
                cells.append(
                    f"{_e(_rs(w['net']))} net, {_e(w['trades'])} trades<br>"
                    f"<span class='small'>profit factor {_e(_pf(w))}, {_e(_pct(w['win_rate']))} win</span>"
                )
        detail = ("; " + "; ".join(v["trend_detail"])) if v["trend_detail"] else ""
        cells.append(_e(v["trend"] + detail))
        rows.append(cells)
    r, p = d["recent_days"], d["previous_days"]
    cap = (
        f"<p class='small'>Recent: {_e(r[0])} to {_e(r[-1])} ({len(r)} sessions). "
        + (
            f"Previous: {_e(p[0])} to {_e(p[-1])} ({len(p)} sessions). "
            if p
            else "Previous: none. "
        )
        + f"All: {_e(d['floor'])} to {_e(d['cutoff'])}. {_e(d['window_note'])}</p>"
    )
    return cap + _table(
        ["Strategy", "Recent", "Previous", "All history", "Trend (recent vs previous)"], rows
    )


def _observations(d: dict) -> tuple[list[str], list[str]]:
    from services.strategy_trade_enrichment import entry_hour_breakdown, symbol_breakdown

    confirmed: list[str] = []
    possible: list[str] = []
    if d["status"] != "ok":
        return confirmed, possible
    traded = {s: v for s, v in d["strategies"].items() if v["windows"]["recent"]["trades"]}
    ranked = sorted(traded.items(), key=lambda kv: kv[1]["windows"]["recent"]["net"], reverse=True)
    if ranked:
        top, bottom = ranked[0], ranked[-1]
        confirmed.append(
            f"Biggest profit contributor in the period: {_e(top[0])} at {_e(_rs(top[1]['windows']['recent']['net']))}. "
            f"Biggest loss contributor: {_e(bottom[0])} at {_e(_rs(bottom[1]['windows']['recent']['net']))}."
        )
    for s, v in traded.items():
        w = v["windows"]["recent"]
        if w["gross"] > 0 and w["net"] < 0:
            confirmed.append(
                f"{_e(s)}: gross profit of {_e(_rs(w['gross']))} becomes a loss of {_e(_rs(w['net']))} after estimated charges."
            )
        if "gross_without_best_worst_day" in w and (w["gross_without_best_worst_day"] > 0) != (
            w["gross"] > 0
        ):
            confirmed.append(
                f"{_e(s)}: the result is driven by one day. Without the best and worst day the gross "
                f"P&amp;L is {_e(_rs(w['gross_without_best_worst_day']))} instead of {_e(_rs(w['gross']))}."
            )
        sb = symbol_breakdown(v["recent_trades"])
        if len(sb) >= 2:
            best, worst = sb[0], sb[-1]

            def sym(r):
                one = " (one trade only, treat as noise)" if r["trades"] == 1 else ""
                word = "trade" if r["trades"] == 1 else "trades"
                return f"{_e(r['symbol'])} {_e(_rs(r['net_pnl']))} on {_e(r['trades'])} {word}{one}"

            confirmed.append(f"{_e(s)} by symbol: best {sym(best)}; worst {sym(worst)}.")
        hours = [h for h in entry_hour_breakdown(v["recent_trades"]) if h["net_pnl"] < 0]
        if hours:
            h = min(hours, key=lambda x: x["net_pnl"])
            small = (
                " Small sample (fewer than 10 trades): a possible pattern only."
                if h["trades"] < 10
                else ""
            )
            confirmed.append(
                f"{_e(s)}: weakest entry hour (IST) is {_e(h['hour'])}:00 with {_e(_rs(h['net_pnl']))} "
                f"on {_e(h['trades'])} trades.{small}"
            )
        if v["exits"]:
            mix: dict[str, int] = {}
            for e in v["exits"]:
                mix[e["type"]] = mix.get(e["type"], 0) + 1
            parts = ", ".join(
                f"{_e(k)} {_e(c)}" for k, c in sorted(mix.items(), key=lambda kv: -kv[1])
            )
            confirmed.append(
                f"{_e(s)} exit types in the period ({len(v['exits'])} exits): {parts}."
            )
        if v["trend"] == "deteriorating":
            possible.append(
                f"{_e(s)} is deteriorating. Possible explanations, not tested: a change in market conditions "
                "(no market regime data is stored, so this cannot be confirmed), or a change in fills or exits."
            )
        elif v["trend"] == "improving":
            possible.append(
                f"{_e(s)} is improving. Possible explanation, not tested: market conditions suited it in "
                "the recent sessions. This cannot be confirmed from stored data."
            )
        allw, recw = v["windows"]["all"], w
        if (
            allw["trades"]
            and recw["trades"]
            and recw["expectancy_net"] > allw["expectancy_net"]
            and allw["net"] < 0
        ):
            confirmed.append(
                f"{_e(s)}: recent results are better than its full history, but the full history is still "
                "negative. This is recovering, not proven."
            )
    if not confirmed:
        confirmed.append(
            "No closed trades in the reporting period, so there is nothing to observe."
        )
    if d["mode"] == "paper":
        possible.append(
            "Paper fills come from the sandbox price feed and charges are estimates, so live results may "
            "differ in fill quality, rejections and actual charges."
        )
    return confirmed, possible


def _issues(d: dict) -> list[str]:
    out: list[str] = []
    if d["status"] != "ok":
        return out
    for s, v in d["strategies"].items():
        w = v["windows"]["recent"]
        if w["trades"] == 0:
            continue
        if w["trades"] < 10:
            out.append(
                f"{_e(s)}: only {_e(w['trades'])} closed trades in the period, so it is not assessed."
            )
        if w["net"] < 0:
            out.append(
                f"{_e(s)}: net loss of {_e(_rs(-w['net']))} after estimated charges in the period."
            )
        if v["trend"] == "deteriorating":
            out.append(f"{_e(s)}: deteriorating against the previous period.")
        if w["charges_covered"] < w["trades"]:
            out.append(
                f"{_e(s)}: charges could not be estimated for {_e(w['trades'] - w['charges_covered'])} "
                "trade(s) (instrument not in the cost schedule), so net P&amp;L may be overstated."
            )
        if v["own_sessions"] < 20:
            out.append(f"{_e(s)}: only {_e(v['own_sessions'])} trading sessions so far (under 20).")
    return out


def _suggestions(d: dict) -> list[str]:
    out: list[str] = []
    if d["status"] != "ok":
        return out
    for s, v in d["strategies"].items():
        w = v["windows"]["recent"]
        if w["trades"] == 0:
            continue
        if w["trades"] < 10:
            out.append(
                f"{_e(s)}: wait for more trades before drawing any conclusion. For the owner to decide."
            )
            continue
        base = ""
        if v["own_sessions"] < 20:
            base = f" It has {_e(v['own_sessions'])} sessions (under 20), so no change to its settings should be based on this report."
        if w["net"] < 0 or v["trend"] == "deteriorating":
            out.append(
                f"{_e(s)}: review the losing trades of the period (symbols, entry hours, exit types listed above)."
                f"{base} For the owner to decide."
            )
    if not out:
        out.append("The data does not point to any action this period. For the owner to decide.")
    return out


def _limitations(d: dict) -> list[str]:
    out = [
        "Charges are estimates from the Performance page schedule. No actual-charge source exists in the repository.",
        "Samples are small. Windows under 10 trades are not assessed and 10 to 19 trades are low confidence. "
        "A strategy with under 20 sessions gets no tuning suggestion.",
        "Paper fills come from the sandbox price feed. Live fills differ in slippage, queueing and rejections.",
        "No market regime data is stored, so market conditions cannot be confirmed as a cause.",
        "This report does not check missing sessions, engine incidents or frozen-strategy status. Use the "
        "strategy-performance-analyst skill for the full analysis.",
    ]
    if d["mode"] == "live":
        out.append(
            "Live reliability of the ledger is not yet verified. Broker rejections and actual charges are not stored."
        )
    return out


# ---------------------------------------------------------------- html


CSS = """
body{font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;margin:24px auto;max-width:1000px;padding:0 16px;font-size:13px;line-height:1.45}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:20px 0 6px;border-bottom:2px solid #333;padding-bottom:2px}
h3{font-size:13px;margin:12px 0 4px;color:#333}
table{border-collapse:collapse;width:100%;margin:6px 0}th,td{border:1px solid #bbb;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#eee}ul{margin:4px 0 4px 18px;padding:0}p{margin:4px 0}.small{font-size:11px;color:#444}
.meta{color:#444;margin-bottom:8px}
@media print{body{margin:0;font-size:11px}h2{break-after:avoid}}
"""


def _render(
    modes: list[str],
    data: dict[str, dict],
    generated: datetime,
    filters_note: str = "",
) -> str:
    title = {"paper": "Paper", "live": "Live", "both": "Paper and live (shown separately)"}
    mode_key = modes[0] if len(modes) == 1 else "both"
    sections = []

    def block(name: str, fn) -> str:
        parts = []
        for m in modes:
            body = fn(data[m])
            if len(modes) > 1:
                parts.append(f"<h3>{_e(m.capitalize())}</h3>{body}")
            else:
                parts.append(body)
        return f"<h2>{_e(name)}</h2>" + "".join(parts)

    sections.append(block("1. Overall summary", _summary))
    sections.append(block("2. Strategy-wise comparison", _comparison))
    sections.append(block("3. Trends", _trends))

    obs_parts = []
    for m in modes:
        confirmed, possible = _observations(data[m])
        head = f"<h3>{_e(m.capitalize())}</h3>" if len(modes) > 1 else ""
        if data[m]["status"] != "ok":
            obs_parts.append(head + _no_live_text(m, data[m]["floor"]))
            continue
        obs_parts.append(
            head
            + "<p><b>Confirmed by the data</b></p>"
            + _ul(confirmed)
            + "<p><b>Possible explanations (not proven)</b></p>"
            + _ul(possible)
        )
    sections.append("<h2>4. Key observations</h2>" + "".join(obs_parts))

    def issues_block(name, fn):
        parts = []
        for m in modes:
            head = f"<h3>{_e(m.capitalize())}</h3>" if len(modes) > 1 else ""
            body = (
                _no_live_text(m, data[m]["floor"])
                if data[m]["status"] != "ok"
                else _ul(fn(data[m]))
            )
            parts.append(head + body)
        return f"<h2>{_e(name)}</h2>" + "".join(parts)

    sections.append(issues_block("5. Issues needing attention", _issues))
    sections.append(
        issues_block("6. Practical suggestions (for the owner to decide)", _suggestions)
    )
    sections.append(issues_block("7. Limitations", _limitations))

    ts = generated.strftime("%Y-%m-%d %H:%M IST")
    covered = ", ".join(
        f"{m}: " + (f"data up to {data[m]['cutoff']}" if data[m]["status"] == "ok" else "no data")
        for m in modes
    )
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{_e(CSP)}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Strategy Performance Report - {_e(title[mode_key])}</title><style>{CSS}</style></head><body>"
        f"<h1>Strategy Performance Report - {_e(title[mode_key])}</h1>"
        f'<div class="meta">Generated {_e(ts)}. {_e(covered)}. Charges are estimated. '
        f"Paper and live are never combined.{_e(filters_note)}</div>" + "".join(sections) + "</body></html>\n"
    )


# ---------------------------------------------------------------- public api


def _filters_note(date_from, date_to, inc, exc, window_sessions) -> str:
    parts = []
    if date_from:
        parts.append(f"from {date_from}")
    if date_to:
        parts.append(f"to {date_to}")
    if inc:
        parts.append("include " + ", ".join(inc))
    if exc:
        parts.append("exclude " + ", ".join(exc))
    if window_sessions:
        parts.append(f"window {window_sessions} sessions")
    return (" Filters: " + "; ".join(parts) + ".") if parts else ""


def generate_report(
    mode: str,
    *,
    out_dir: Path | str | None = None,
    ledger_db: Path | str | None = None,
    signal_db: Path | str | None = None,
    cutoff: str | None = None,
    now: datetime | None = None,
    date_to: str | None = None,
    date_from: str | None = None,
    include: str | list[str] | None = None,
    exclude: str | list[str] | None = None,
    window_sessions: int | None = None,
) -> Path:
    """Build the report and save it as <out_dir>/<mode>-YYYY-MM-DD.html (the run date, IST).

    A second run on the same day replaces that day's file. Another day's file is never touched.
    Raises ReportError and writes nothing when anything fails.
    """
    if mode not in MODES:
        raise ReportError(f"Unknown mode {mode!r}. Use paper, live or both.")
    if date_to is not None and cutoff is not None and date_to != cutoff:
        raise ReportError("Give either cutoff or date_to, not two different values.")
    cutoff = _parse_date(date_to if date_to is not None else cutoff, "End date (--to)")
    date_from = _parse_date(date_from, "Start date (--from)")
    if date_from and cutoff and date_from > cutoff:
        raise ReportError(f"Start date {date_from} is after end date {cutoff}.")
    if date_from:
        from services.strategy_metrics_service import LEDGER_RELIABLE_SINCE

        reliable = LEDGER_RELIABLE_SINCE.isoformat()
        if date_from < reliable:
            print(
                f"Notice: --from {date_from} is before the reliable ledger start {reliable}; using {reliable}.",
                file=sys.stderr,
            )
            date_from = reliable
    if window_sessions is not None and (
        isinstance(window_sessions, bool) or not isinstance(window_sessions, int) or window_sessions < 1
    ):
        raise ReportError("Window sessions must be an integer of 1 or more.")
    inc = _parse_names(include)
    exc_names = _parse_names(exclude)
    out_dir = Path(out_dir) if out_dir else REPORT_DIR
    ledger = Path(ledger_db) if ledger_db else LEDGER_DB
    signals = Path(signal_db) if signal_db else SIGNAL_DB
    generated = now or datetime.now(IST)
    modes = ["paper", "live"] if mode == "both" else [mode]
    try:
        data = {
            m: _analyse_mode(
                m, ledger, signals, cutoff, date_from, inc, exc_names, window_sessions
            )
            for m in modes
        }
        if inc or exc_names:
            seen = {n for m in modes for n in data[m]["available_names"]}
            unknown = [n for n in dict.fromkeys(inc + exc_names) if n not in seen]
            if unknown:
                print(
                    "Warning: no strategy named in the data: " + ", ".join(unknown),
                    file=sys.stderr,
                )
            if any(data[m]["filtered_empty"] for m in modes) and not any(
                data[m]["status"] == "ok" for m in modes
            ):
                raise ReportError("The include/exclude filters leave no strategies to report.")
        html = _render(modes, data, generated, _filters_note(date_from, cutoff, inc, exc_names, window_sessions))
    except ReportError:
        raise
    except Exception as exc:  # any failure: no file
        raise ReportError(f"Report could not be generated: {type(exc).__name__}: {exc}") from exc

    final = out_dir / f"{mode}-{generated.date().isoformat()}.html"
    tmp_name = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=out_dir)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
        os.replace(tmp_name, final)
        tmp_name = None
    except OSError as exc:
        raise ReportError(f"Report could not be saved: {type(exc).__name__}") from exc
    finally:
        if tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    return final


def list_reports(out_dir: Path | str | None = None) -> list[dict]:
    """Saved reports, newest first. Only files whose names match the whitelist."""
    base = Path(out_dir) if out_dir else REPORT_DIR
    if not base.is_dir():
        return []
    out = []
    for p in base.iterdir():
        m = FILENAME_RE.match(p.name)
        if m and p.is_file() and not p.is_symlink():
            out.append(
                {
                    "filename": p.name,
                    "mode": m.group(1),
                    "date": f"{m.group(2)}-{m.group(3)}-{m.group(4)}",
                    "size": p.stat().st_size,
                }
            )
    out.sort(key=lambda r: (r["date"], r["mode"]), reverse=True)
    return out


def report_path(filename: str, out_dir: Path | str | None = None) -> Path | None:
    """The real path of a saved report, or None. Whitelisted name, inside the folder, not a link."""
    if not isinstance(filename, str) or not FILENAME_RE.fullmatch(filename):
        return None
    base = (Path(out_dir) if out_dir else REPORT_DIR).resolve()
    candidate = base / filename
    if candidate.is_symlink() or not candidate.is_file():
        return None
    if candidate.resolve().parent != base:
        return None
    return candidate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate the strategy performance report (read only)."
    )
    ap.add_argument("--mode", choices=MODES, default="paper")
    ap.add_argument(
        "--out", default=None, help="output folder (default reports/strategy-performance)"
    )
    ap.add_argument(
        "--to",
        "--cutoff",
        dest="date_to",
        default=None,
        help="YYYY-MM-DD upper bound, default the last session with data (--cutoff is an alias)",
    )
    ap.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="YYYY-MM-DD lower bound (not earlier than the reliable ledger start)",
    )
    ap.add_argument("--include", default=None, help="comma-separated exact strategy names to report")
    ap.add_argument("--exclude", default=None, help="comma-separated exact strategy names to drop")
    ap.add_argument(
        "--window-sessions",
        type=int,
        default=None,
        help=f"sessions per recent/previous window (default {WINDOW_SESSIONS})",
    )
    args = ap.parse_args(argv)
    try:
        path = generate_report(
            args.mode,
            out_dir=args.out,
            date_to=args.date_to,
            date_from=args.date_from,
            include=args.include,
            exclude=args.exclude,
            window_sessions=args.window_sessions,
        )
    except ReportError as exc:
        print(f"REPORT FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"report -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
