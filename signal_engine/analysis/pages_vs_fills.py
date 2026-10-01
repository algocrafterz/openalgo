"""Daily check: do the Strategy P&L / Performance pages agree with the broker's own fills?

The pages read OpenAlgo's strategy book (core `strategy_closed_trades` and `strategy_positions`),
which books P&L from fill events. This check rebuilds the same numbers independently from the
raw fills and compares them, per strategy and per symbol:

  analyze mode  fills = db/sandbox.db `sandbox_trades` (carries the strategy tag)
  live mode     fills = the day's broker tradebook snapshot, tagged via `strategy_order_tags`

Untagged fills (time exit, broker square-off) close whichever strategy still holds the symbol.

Realised P&L uses the same rule the book does (average-cost netting per strategy+symbol+product).

FAIL means a page number is not backed by a fill: a stray or duplicate order booked as a trade, a
phantom or missing open leg, a fill that never reached the book, or a booking bug. A MIS leg must
start the day flat; a CNC/NRML leg may be carried in, and is skipped (reported) rather than
compared, because the day's fills alone cannot price a position opened earlier.

2026-10-01 is why this exists: duplicate stop orders left two phantom open legs on the pages, and the
engine's own books had been off by thousands of rupees without anything noticing.

    PYTHONPATH=. uv run python -m signal_engine.analysis.pages_vs_fills            # today, current mode
    PYTHONPATH=. uv run python -m signal_engine.analysis.pages_vs_fills 2026-10-01 --mode analyze
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from signal_engine.timeutils import IST

REPO = Path(__file__).resolve().parents[2]
SANDBOX_DB = REPO / "db" / "sandbox.db"
CORE_DB = REPO / "db" / "openalgo.db"
TRADES_DB = REPO / "signal_engine" / "data" / "trades.db"
SNAP_DIR = REPO / "signal_engine" / "data" / "tradebook"
#: Reviewed, explained one-off artifacts: [{"day","mode","strategy","symbol","reason"}, ...].
#: An exempt leg is listed in the report but neither compared nor counted, so a known stray order
#: does not fail the check every day, while an UNEXPLAINED mismatch still does.
EXCEPTIONS_FILE = Path(__file__).with_name("pages_vs_fills_exceptions.json")

#: Rupees. Each fill is a float, so sums carry dust; a real defect is far larger.
TOLERANCE = 1.0
#: Products that can legitimately carry a position in from an earlier day.
CARRYING_PRODUCTS = frozenset({"CNC", "NRML"})

Key = tuple  # (strategy, symbol, product)


@dataclass(frozen=True)
class Fill:
    strategy: str
    symbol: str
    product: str
    action: str  # BUY | SELL
    quantity: float
    price: float


@dataclass
class LegState:
    realised: float = 0.0
    net_qty: float = 0.0


@dataclass
class Result:
    day: str
    mode: str
    failures: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    exempt: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    account: list[str] = field(default_factory=list)
    charges: list[str] = field(default_factory=list)
    charges_headline: str = ""
    by_strategy: dict[str, tuple[float, float]] = field(default_factory=dict)  # fills, page
    engine_gap: dict[str, tuple[float, float]] = field(default_factory=dict)  # engine, page

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        if not self.by_strategy and not self.failures:
            return f"no fills or page rows for {self.mode} on {self.day} - nothing to compare"
        if self.passed:
            total = sum(p for _, p in self.by_strategy.values())
            text = (
                f"pages match fills for {len(self.by_strategy)} strateg"
                f"{'y' if len(self.by_strategy) == 1 else 'ies'} (page total {total:,.1f})"
            )
            return f"{text}; {self.charges_headline}" if self.charges_headline else text
        return f"{len(self.failures)} page/fill mismatch(es): " + "; ".join(self.failures)

    def report(self) -> str:
        lines = [f"Pages vs fills, {self.day} ({self.mode})", ""]
        lines.append(f"{'strategy':28} {'fills':>11} {'page':>11} {'diff':>9}")
        for s in sorted(self.by_strategy):
            f, p = self.by_strategy[s]
            flag = "" if abs(f - p) <= TOLERANCE else "  <-- MISMATCH"
            lines.append(f"{s:28} {f:11.1f} {p:11.1f} {p - f:9.1f}{flag}")
        lines.extend(f"FAIL: {item}" for item in self.failures)
        lines.extend(f"skipped: {item}" for item in self.skipped)
        if self.exempt:
            lines.append("(totals above exclude the exempt legs below)")
        lines.extend(f"exempt: {item}" for item in self.exempt)
        lines.extend(f"note: {item}" for item in self.notes)
        for s in sorted(self.engine_gap):
            e, p = self.engine_gap[s]
            if abs(e - p) > TOLERANCE:
                lines.append(
                    f"info: engine books {e:,.1f} vs page {p:,.1f} for {s} "
                    "(engine records only; the pages are the fill-backed numbers)"
                )
        if self.account:
            lines.append("")
            lines.append(
                "Whole-account view (informational; why Positions / Dashboard can differ from the strategy pages)"
            )
            lines.extend(f"  {item}" for item in self.account)
        if self.charges:
            lines.append("")
            lines.append(
                "Estimated charges (same schedule as the Performance page; before slippage)"
            )
            lines.extend(f"  {item}" for item in self.charges)
        lines.append("")
        lines.append("RESULT: " + ("PASS" if self.passed else "FAIL"))
        return "\n".join(lines)


def realised_from_fills(
    fills: list[Fill], unattributed: list[Fill] | None = None
) -> dict[Key, LegState]:
    """Average-cost netting per (strategy, symbol, product), in the order given.

    A fill with no strategy tag (the 14:45 time exit and the sandbox's 15:15 square-off go
    through endpoints that carry none) closes whichever strategies still hold that symbol and
    product, each for its own share, at the fill's price. A fill that nothing holds is appended
    to `unattributed` when given.
    """
    out: dict[Key, LegState] = defaultdict(LegState)
    pos: dict[Key, float] = defaultdict(float)
    cost: dict[Key, float] = defaultdict(float)

    def apply(k: Key, action: str, quantity: float, price: float) -> None:
        sign = 1 if action.upper() == "BUY" else -1
        if pos[k] == 0 or (pos[k] > 0) == (sign > 0):
            total = abs(pos[k]) + quantity
            cost[k] = (cost[k] * abs(pos[k]) + price * quantity) / total
            pos[k] += sign * quantity
        else:
            closed = min(quantity, abs(pos[k]))
            out[k].realised += (price - cost[k]) * closed * (1 if pos[k] > 0 else -1)
            pos[k] += sign * closed
            remainder = quantity - closed
            if remainder > 0:
                pos[k] = sign * remainder
                cost[k] = price
        out[k].net_qty = pos[k]

    for f in fills:
        if f.strategy:
            apply((f.strategy, f.symbol, f.product), f.action, f.quantity, f.price)
            continue
        sign = 1 if f.action.upper() == "BUY" else -1
        holders = [
            k for k, q in pos.items() if k[1] == f.symbol and k[2] == f.product and q * sign < 0
        ]
        left = f.quantity
        for k in holders:
            take = min(left, abs(pos[k]))
            apply(k, f.action, take, f.price)
            left -= take
        if left > 1e-9 and unattributed is not None:
            unattributed.append(
                Fill("", f.symbol, f.product, f.action, left, f.price)
            )
    return dict(out)


def compare(
    day: str,
    mode: str,
    fill_state: dict[Key, LegState],
    page_realised: dict[Key, float],
    page_qty: dict[Key, float],
    tolerance: float = TOLERANCE,
    exempt: dict[tuple, str] | None = None,
) -> Result:
    """Compare fill-derived state with what the pages hold. Pure; no I/O."""
    res = Result(day=day, mode=mode)
    keys = set(fill_state) | set(page_realised) | {k for k, q in page_qty.items() if q}
    fills_by: dict[str, float] = defaultdict(float)
    page_by: dict[str, float] = defaultdict(float)
    for k in sorted(keys):
        strategy, symbol, product = k
        reason = (exempt or {}).get((strategy, symbol))
        if reason:
            res.exempt.append(f"{strategy} {symbol}: {reason}")
            continue
        st = fill_state.get(k, LegState())
        page_r = page_realised.get(k, 0.0)
        page_q = page_qty.get(k, 0.0)
        start_qty = page_q - st.net_qty  # what the leg held before today's fills
        if abs(start_qty) > 1e-6 and product.upper() in CARRYING_PRODUCTS:
            res.skipped.append(
                f"{strategy} {symbol} {product}: carried in {start_qty:g} from an earlier day"
            )
            continue
        fills_by[strategy] += st.realised
        page_by[strategy] += page_r
        if abs(page_r - st.realised) > tolerance:
            res.failures.append(
                f"{strategy} {symbol}: page realised {page_r:,.1f} vs fills {st.realised:,.1f}"
            )
        if abs(page_q - st.net_qty) > 1e-6:
            kind = "phantom open" if abs(page_q) > abs(st.net_qty) else "missing open"
            res.failures.append(
                f"{strategy} {symbol}: {kind} leg, page holds {page_q:g} but fills net to {st.net_qty:g}"
            )
    for s in set(fills_by) | set(page_by):
        res.by_strategy[s] = (fills_by[s], page_by[s])
    return res


def account_realised(fills: list[Fill]) -> dict[tuple, float]:
    """Realised P&L per (symbol, product) with every strategy netted together, as the broker
    and the sandbox's Positions page see it."""
    merged = [replace(f, strategy="ACCOUNT") for f in fills]
    return {
        (k[1], k[2]): st.realised for k, st in realised_from_fills(merged).items()
    }


def account_view(
    fills: list[Fill],
    page_realised: dict[Key, float],
    exempt: dict[tuple, str] | None = None,
    positions_total: float | None = None,
    funds_total: float | None = None,
    tolerance: float = TOLERANCE,
) -> list[str]:
    """Why the whole-account figure differs from the strategy pages, line by line.

    The strategy pages count only strategy-tagged trades; Positions and the Dashboard count
    everything the account did. A symbol whose account P&L differs from its strategy-page P&L
    is a stray (an order no strategy trade owns); it is explained when a reviewed exemption
    names it, and flagged UNEXPLAINED otherwise. `positions_total` / `funds_total` are the
    sandbox's own per-position sum and funds running total (analyze mode only).
    """
    acct = account_realised(fills)
    page_by_symbol: dict[tuple, float] = defaultdict(float)
    for (_strategy, symbol, product), pnl in page_realised.items():
        page_by_symbol[(symbol, product)] += pnl
    exempt_symbols = {sym: why for (_strat, sym), why in (exempt or {}).items()}

    pages_total = sum(page_realised.values())
    acct_total = sum(acct.values())
    lines = [
        f"strategy pages (P&L, Performance):   {pages_total:12,.1f}",
    ]
    explained = 0.0
    unexplained = 0.0
    for key in sorted(set(acct) | set(page_by_symbol)):
        gap = acct.get(key, 0.0) - page_by_symbol.get(key, 0.0)
        if abs(gap) <= tolerance:
            continue
        why = exempt_symbols.get(key[0])
        if why:
            explained += gap
            lines.append(f"  stray {key[0]:12} {gap:12,.1f}   {why}")
        else:
            unexplained += gap
            lines.append(f"  UNEXPLAINED {key[0]:12} {gap:12,.1f}   account and strategy pages disagree")
    lines.append(f"whole account, rebuilt from every fill: {acct_total:9,.1f}")
    if positions_total is not None:
        diff = positions_total - acct_total
        corrected = explained != 0 and abs(positions_total - (acct_total - explained)) <= 25.0
        if corrected:
            lines.append(
                f"Positions page (sandbox per-symbol sum): {positions_total:9,.1f}"
                "   (corrected for the reviewed strays above; the raw fills still show them)"
            )
        else:
            lines.append(
                f"Positions page (sandbox per-symbol sum): {positions_total:9,.1f}"
                f"   (differs from fills by {diff:,.1f}, sandbox rounding)"
            )
    if funds_total is not None:
        base = positions_total if positions_total is not None else acct_total
        drift = funds_total - base
        note = "" if abs(drift) <= tolerance else "   <-- sandbox funds running total drifts from its own positions"
        lines.append(f"Trading Dashboard (sandbox funds total): {funds_total:9,.1f}   (vs Positions {drift:,.1f}){note}")
    if unexplained:
        lines.append(f"UNEXPLAINED account-vs-strategy gap: {unexplained:,.1f}")
    elif explained:
        lines.append(f"all of the {explained:,.1f} gap is reviewed strays")
    return lines


# -- loaders -----------------------------------------------------------------------------------


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


#: Strategy values the sandbox stamps on fills that did not come from a tagged strategy order.
_UNTAGGED = frozenset({"", "AUTO_SQUARE_OFF"})


def _tag(raw) -> str:
    value = (raw or "").strip()
    return "" if value in _UNTAGGED else value


def load_fills_analyze(day: str, sandbox_db: Path = SANDBOX_DB) -> list[Fill]:
    con = _ro(sandbox_db)
    try:
        rows = con.execute(
            "select strategy, symbol, product, action, quantity, price from sandbox_trades "
            "where date(trade_timestamp)=? order by trade_timestamp, id",
            (day,),
        ).fetchall()
    finally:
        con.close()
    return [
        Fill(_tag(r[0]), r[1], r[2] or "MIS", r[3], float(r[4]), float(r[5])) for r in rows
    ]


def load_fills_live(day: str, snap_dir: Path = SNAP_DIR, core_db: Path = CORE_DB) -> list[Fill]:
    """Live fills: the tradebook snapshot, attributed to a strategy through its order tag."""
    snap = snap_dir / f"tradebook_{day}.json"
    if not snap.exists():
        raise FileNotFoundError(
            f"no tradebook snapshot for {day} ({snap}) - run analysis --snapshot first"
        )
    trades = json.loads(snap.read_text())
    con = _ro(core_db)
    try:
        tags = dict(
            con.execute(
                "select orderid, strategy from strategy_order_tags where mode='live'"
            ).fetchall()
        )
    finally:
        con.close()
    out = []
    for t in sorted(trades, key=lambda x: x.get("timestamp", "")):
        strategy = tags.get(str(t.get("orderid")), "")
        out.append(
            Fill(
                strategy,
                t["symbol"],
                t.get("product") or "MIS",
                t["action"],
                float(t["quantity"]),
                float(t.get("average_price") or t.get("price") or 0),
            )
        )
    return out


def load_closed_trades(day: str, mode: str, core_db: Path = CORE_DB) -> list[dict]:
    con = _ro(core_db)
    con.row_factory = sqlite3.Row
    try:
        has_broker = "broker" in {r[1] for r in con.execute("pragma table_info(strategy_closed_trades)")}
        broker_col = "broker" if has_broker else "'unknown' as broker"
        rows = con.execute(
            f"select strategy, symbol, exchange, product, mode, direction, closed_quantity, "
            f"entry_price, exit_price, realized_pnl, trade_date, closed_at, {broker_col} "
            "from strategy_closed_trades where trade_date=? and mode=?",
            (day, mode),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def charge_lines(trades: list[dict]) -> tuple[list[str], str]:
    """Per-strategy and total estimated charges and their share of gross profit.

    Uses the Performance page's own estimator, so the two always agree. Best effort: the
    estimator lives in OpenAlgo core and needs its dependencies, so any failure here yields
    one explanatory line instead of breaking the reconciliation.
    """
    try:
        from services.strategy_trade_enrichment import summarize_costs
    except Exception as exc:  # noqa: BLE001 - optional insight, never a reason to fail the check
        return [f"unavailable ({type(exc).__name__})"], ""

    def line(label: str, rows: list[dict]) -> str:
        c = summarize_costs(rows)
        gross = sum(float(t["realized_pnl"]) for t in rows)
        if c["est_costs"] is None:
            return f"{label:28} gross {gross:10,.1f}   charges unknown (an instrument is not modelled)"
        share = (
            f"{c['charges_pct_of_gross']:6.1f}% of profit"
            if c["charges_pct_of_gross"] is not None
            else "  no gross profit"
        )
        return (
            f"{label:28} gross {gross:10,.1f}   charges {c['est_costs']:9,.1f}  {share}"
            f"   after charges {c['net_after_costs']:10,.1f}"
        )

    by: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by[t["strategy"]].append(t)
    lines = [line(s, by[s]) for s in sorted(by)]
    lines.append(line("ALL STRATEGIES", trades))
    total = summarize_costs(trades)
    for b in total.get("by_broker", []):
        tag = " (assumed rates)" if b["assumed"] else ""
        lines.append(
            f"  broker {b['label']}{tag}: {b['trades']} trades, charges "
            + ("unknown" if b["est_costs"] is None else f"{b['est_costs']:,.1f}")
        )
        if b.get("attention"):
            lines.append(f"  ATTENTION: {b['attention']}")
    headline = ""
    if total["est_costs"] is not None:
        gross = sum(float(t["realized_pnl"]) for t in trades)
        headline = f"est. charges {total['est_costs']:,.0f}"
        if total["charges_pct_of_gross"] is not None:
            headline += f" = {total['charges_pct_of_gross']:.1f}% of gross profit {gross:,.0f}"
        if any(b.get("attention") for b in total.get("by_broker", [])):
            headline += " (FEE SCHEDULE NEEDS ATTENTION, see report)"
    return lines, headline


def load_sandbox_totals(sandbox_db: Path = SANDBOX_DB):
    """(sum of per-position today_realized_pnl, funds today_realized_pnl) - the Positions page
    and the Trading Dashboard figures. None where a table is empty."""
    con = _ro(sandbox_db)
    try:
        pos = con.execute("select sum(today_realized_pnl) from sandbox_positions").fetchone()[0]
        funds = con.execute("select sum(today_realized_pnl) from sandbox_funds").fetchone()[0]
    finally:
        con.close()
    return (None if pos is None else float(pos), None if funds is None else float(funds))


def load_pages(day: str, mode: str, core_db: Path = CORE_DB):
    con = _ro(core_db)
    try:
        realised = {
            (r[0], r[1], r[2]): float(r[3])
            for r in con.execute(
                "select strategy, symbol, product, sum(realized_pnl) from strategy_closed_trades "
                "where trade_date=? and mode=? group by 1,2,3",
                (day, mode),
            )
        }
        qty = {
            (r[0], r[1], r[2]): float(r[3])
            for r in con.execute(
                "select strategy, symbol, product, quantity from strategy_positions where mode=?",
                (mode,),
            )
        }
    finally:
        con.close()
    return realised, qty


def engine_totals(day: str, mode: str, trades_db: Path = TRADES_DB) -> dict[str, float]:
    """Per-strategy P&L as the engine booked it: the final cumulative row of each round trip."""
    if not trades_db.exists():
        return {}
    con = _ro(trades_db)
    try:
        rows = con.execute(
            "select strategy, symbol, direction, quantity, json_extract(context,'$.pnl') "
            "from trades where date(executed_at)=? and trade_mode=? "
            "and order_id not in ('','-DECLINED-') order by id",
            (day, mode),
        ).fetchall()
    finally:
        con.close()
    open_q: dict[tuple, list] = {}
    totals: dict[str, float] = defaultdict(float)
    for strategy, symbol, direction, qty, pnl in rows:
        k = (strategy, symbol)
        if direction in ("LONG", "SHORT"):
            open_q[k] = [qty or 0, 0, 0.0]
        elif k in open_q and pnl is not None:
            open_q[k][1] += qty or 0
            open_q[k][2] = float(pnl)
            if open_q[k][1] >= open_q[k][0]:
                totals[strategy] += open_q.pop(k)[2]
    return dict(totals)


def load_exceptions(day: str, mode: str, path: Path = EXCEPTIONS_FILE) -> dict:
    if not path.exists():
        return {}
    items = json.loads(path.read_text())
    return {
        (i["strategy"], i["symbol"]): i["reason"]
        for i in items
        if i.get("day") == day and i.get("mode") == mode
    }


def run(day: str, mode: str) -> Result:
    if mode not in ("analyze", "live"):
        raise ValueError(f"cannot compare mode {mode!r}: need 'analyze' or 'live'")
    fills = load_fills_analyze(day) if mode == "analyze" else load_fills_live(day)
    page_realised, page_qty = load_pages(day, mode)
    unattributed: list[Fill] = []
    res = compare(
        day,
        mode,
        realised_from_fills(fills, unattributed),
        page_realised,
        page_qty,
        exempt=load_exceptions(day, mode),
    )
    sandbox_totals = load_sandbox_totals() if mode == "analyze" else (None, None)
    res.account = account_view(
        fills,
        page_realised,
        exempt=load_exceptions(day, mode),
        positions_total=sandbox_totals[0],
        funds_total=sandbox_totals[1],
    )
    res.charges, res.charges_headline = charge_lines(load_closed_trades(day, mode))
    for f in unattributed:
        res.notes.append(
            f"untagged {f.action} {f.quantity:g} {f.symbol} @ {f.price} closed nothing a strategy held"
        )
    for strategy, engine in engine_totals(day, mode).items():
        if strategy in res.by_strategy:
            res.engine_gap[strategy] = (engine, res.by_strategy[strategy][1])
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD (default today, IST)")
    ap.add_argument("--mode", choices=("analyze", "live"), default=None)
    args = ap.parse_args(argv)

    day = args.day or datetime.now(IST).strftime("%Y-%m-%d")
    mode = args.mode
    if mode is None:
        from signal_engine.analysis.breakingtrade import review

        name, is_analyze = review.trading_mode()
        mode = "analyze" if is_analyze or name == "unknown" else "live"
    res = run(day, mode)
    print(res.report())
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
