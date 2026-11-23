# services/discretionary_pnl_service.py
"""
Accounting and P&L for the Discretionary Trading Performance module (Stage 2, backend only).

Reads the Stage 1 ledger (``ledger_fills``), rebuilds the derived book
(``database/discretionary_book_db``) and answers: trades, open positions with unrealized
P&L, unresolved fills, orphans, a summary and a PositionBook reconciliation. No UI, no HTTP
route. Nothing here places, modifies or cancels an order; the only broker reads are quotes
(``get_multiquotes_with_auth``) and the position book (``get_positionbook_with_auth``), both
through helpers that tests replace.

Which fills count
-----------------
Discretionary = attribution_state in (manual, ui_manual). ``unresolved`` fills are listed
(``get_unresolved``) and excluded from P&L; ``strategy_*`` fills are excluded. The summary's
``executions_collected`` / ``executions_unresolved`` count the manual + unresolved fills of the
period and filters (see ``execution_counts``); they are informational and change no figure.

Rebuild
-------
FULL, atomic and deterministic. The fingerprint is a sha256 over the sorted
(id, attribution_state, qty, price, fill_time, side, symbol, exchange, product, account,
broker, order_id) of ALL ledger fills PLUS everything that
changes the derived numbers: ENGINE_VERSION, SESSION_EXPIRY_TIME and a hash of the cost
model (the schedules and broker fee settings of the brokers present), so a re-attribution, a
new fill, a cost-model change or an expiry-time change all force a rebuild. Every read
loads the ledger and computes the fingerprint ONCE, rebuilds at most once (under a lock) and
reads trades, positions, unresolved fills and orphans for that SAME build_id (a snapshot).
The derived tables are created once per process, not per read. The sync is untouched.

Unrealized P&L
--------------
Never stored and never fed to ``compute_metrics``. One batched quote call per broker covers
all distinct open (symbol, exchange) pairs. ``fetched_at`` is our own clock read just before
the call (Flattrade quotes carry no timestamp). Price status:

  live         quote taken within DISCRETIONARY_STALE_AFTER_S (default 60) in market hours
  stale        older than that in market hours (a fresh call that returned an old quote)
  last_close   market closed: the price is the last traded one, not a live quote
  cached       the whole quote call failed and this is the last good quote we cached for the
               symbol; unrealized is computed from it but flagged, ``age_s`` says how old
  unavailable  ltp <= 0, a per-symbol error, or a failed call with nothing cached: no number
  not_requested  with_quotes=False

A per-symbol error or ltp <= 0 never falls back to the cache. Every price carries ``age_s``
(seconds since ``fetched_at``, None when there is no price).

Market hours: ``database.market_calendar_db.get_effective_session_window`` (holidays,
special sessions, per-exchange timings; read-only, cached per day and exchange). It returns
None for both "closed" and an internal failure. Only non-None windows are cached; a None
answer counts as closed only when ``is_market_holiday`` (holiday or weekend) confirms it,
otherwise it is a calendar failure and the clock windows below are used. ``market_hours_source`` says which
was used: 'calendar', 'clock' or 'mixed'. The clock windows are weekdays only (no holidays);
the MCX close is DISCRETIONARY_MCX_CLOSE (default 23:55).

Naming of the summary: top-level ``net`` and ``realized_net_of_charges`` are realized gross
minus ESTIMATED charges, ``realized_gross`` and ``metrics.net_profit`` are gross (before
charges).

Known limit (equity only): the user trades equity (swing = CNC, intraday = MIS), not
derivatives. There is NO settlement close and NO expiry handling: an open derivative position
is never auto-settled at expiry, stays OPEN, and shows as missing_in_broker in reconcile once
the broker drops it. Do not rely on derivative P&L from this module.

Reconcile: a carried CNC row (scope 'carried') overlaps the session row of the same identity
when both exist, because the broker reports one net quantity for the identity. To avoid
repeating it, ``broker_qty`` is None on carried rows (the session row carries it).

Not verified without live data: Flattrade PositionBook average-price semantics (whether
``average_price`` is the net or the day-buy average) and MCX lot units (quantity in lots or
units). Reconcile reports what the broker returns.

Completed trades and legs
-------------------------
The book stores one row per CLOSING LEG (``disc_trades``: every partial exit is a leg) and
one row per position (``disc_positions``). A COMPLETED trade is one position from flat to
flat (a reversal ends it and opens a new position); it is built at read time by
``discretionary_analytics.group_completed`` from a FLAT position and the legs sharing its
identity and ``position_seq`` (no schema change). Its id is the position row id (stable
within a build), quantity the total closed, entry / exit the quantity-weighted averages,
P&L and charges the sums of the legs, hold from the position's opening to the final close,
and ``trade_date`` the session date of the FINAL close.

Money follows the legs: realized gross, charges, net, the equity curve, daily P&L and the
daily / weekly / monthly rows sum EVERY leg on its own close date, so a partial exit counts on
its day while the position is still open. Counts follow completed trades: trade counts,
wins / losses, win rate, profit factor, average win / loss, expectancy, hold times and every
by_* trades / wins / losses count. In a period, a completed trade is counted when its final
close is in the period (its P&L, used by the ratios, is the whole position's). An open
position carries ``realized_so_far`` (its closed legs' gross P&L, already in realized).
``charges_unavailable_trades`` counts closing legs without a charge estimate (it qualifies
the money figures).

Metrics
-------
Closing legs go to ``strategy_metrics_service.compute_metrics`` with GROSS realized P&L by
close date (no unrealized, no charges in the equity curve or drawdown); the trade-count
figures (``COUNT_METRICS``) are then replaced by those of a second call on the completed
trades, so money and counts each use their own basis. The completed-trade call is on NET P&L
after estimated charges (a win is net > 0); a trade without a net is left out of those ratios
and counted in ``net_unavailable_trades`` (``trades_count`` stays the total).

Phase 2A additions: a ``category`` filter (all / intraday / swing, see ``_scope``), the
blocks ``points_by_instrument``, ``risk``, ``monthly``, ``weekday``, ``trade_management``,
``costs``, ``classification``, ``unresolved_in_range`` and ``reconciliation`` in
``get_performance``, the lazy MAE / MFE (``get_excursions``) and the CSV rows
(``get_export``). Formulas live in ``discretionary_analytics``. Discretionary has no
capital basis, so Sharpe, Sortino, the capital they assume and any return-on-capital are
withheld with the reason ``no capital basis`` (compute_metrics would otherwise quietly use
its 100,000 reference capital). Its start date is DISCRETIONARY_RELIABLE_SINCE, else the
session date of the first synced fill.

Today (current session)
-----------------------
``summary["today"]`` (``today_block``) is a read-only view of the SAME build rows for the
session ``session_date(now)`` (IST; the previous day before SESSION_EXPIRY_TIME): realized
gross / charges / net of the exit legs of the session (partial exits on their own day, so it
equals the daily row of the session), completed positions whose FINAL close is in the session
(with compute_metrics counts and ratios on them only), every open position (partially closed
= open with at least one closing leg) and ``pending_attribution`` (unresolved fills, those
held by the manual grace window with the earliest time they can be classified). It ignores
the page period, honours the broker / account / symbol filters and changes no attribution.

Sync health
-----------
``get_summary`` reads ``ledger_sync_runs`` (read-only) and adds ``sync_health`` plus warnings:
the last successful (ok/partial) sync, weekdays since the first sync with no successful run
(fills of such a day may be lost: the broker TradeBook is a current-day book), and the latest
run per broker/account when it ended partial/error/no_auth/broker_error. Runs with account ""
(no canonical account known yet) count for the successful-sync checks but never produce a
per-account warning. A failure to read the runs or the calendar never breaks the summary.

Known limits (documented, not fixed)
------------------------------------
* MIS-to-CNC conversion is not modelled: it leaves a stale open MIS position and a CNC orphan
  (a CNC sell without an opening buy).
* Budget Saturday and Muhurat / special sessions are not scheduled (the sync scheduler runs
  on weekdays only), so fills made in them are not collected.
* StrategyOrderTag rows are pruned after 30 days. A fill left unresolved long enough can
  therefore flip later (for example to manual once its tag is gone).
* If the strategy book failed to initialise at boot, untagged automated orders may be
  classified manual.
* Broker auto square-off and the UI "close all" on AUTOMATED positions are classified as
  discretionary (their orders carry no strategy tag). RESIDUAL RISK: the same position is then
  also counted by the strategy side, so its P&L can be double counted.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from database import discretionary_book_db as book
from database import discretionary_ledger_db as ledger
from services import discretionary_analytics as an
from services import discretionary_ledger_service as ledger_svc
from services import strategy_metrics_service as metrics_svc
from services.discretionary_charges import allocate_charges
from services.discretionary_reconstruction import (
    ENGINE_VERSION,
    reconstruct,
    session_date,
    session_expiry,
)
from utils.env_config import env_float
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

DISCRETIONARY_STATES = (ledger.STATE_MANUAL, ledger.STATE_UI_MANUAL)
NO_CAPITAL = "no capital basis"
AVG_TOLERANCE = 0.0005  # 0.05 %
QTY_EPS = 1e-9
# compute_metrics figures that describe trades rather than money: taken from the completed
# trades (position flat to flat), not from the closing legs.
COUNT_METRICS = (
    "trades_count",
    "winning_trades",
    "losing_trades",
    "win_rate",
    "gross_profit",
    "gross_loss",
    "profit_factor",
    "expectancy",
    "avg_win",
    "avg_loss",
    "payoff_ratio",
)

_rebuild_lock = threading.RLock()
_tables_ready = False
_tables_lock = threading.Lock()
_quote_cache: dict[tuple[str, str, str], tuple[float, datetime]] = {}
_calendar_cache: dict[tuple[date, str], tuple[int, int] | None] = {}
_CALENDAR_EXCHANGES = frozenset({"NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX", "NCO"})
_IST_OFFSET_MS = 19_800_000
_IST = timezone(timedelta(hours=5, minutes=30))


class ReconcileError(Exception):
    """No comparison is possible: no session for the account, or the PositionBook failed."""


# ---------------------------------------------------------------- clock / env / helpers


def _now() -> datetime:
    """Naive IST wall clock; tests replace this."""
    return ledger_svc.now_ist()


def stale_after_s() -> float:
    return env_float("DISCRETIONARY_STALE_AFTER_S", 60.0, minimum=0.0)


def _clock_time(name: str, default: tuple[int, int]) -> tuple[int, int]:
    raw = os.getenv(name, "").strip()
    try:
        hour, minute = map(int, raw.split(":"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    return default


def _clock_open(exchange: str, now: datetime) -> bool:
    """Weekday clock window of the exchange (holidays are not modelled)."""
    if now.weekday() >= 5:
        return False
    ex = (exchange or "").upper()
    if ex in ("MCX", "NCO"):
        window = ((9, 0), _clock_time("DISCRETIONARY_MCX_CLOSE", (23, 55)))
    elif ex in ("CDS", "BCD"):
        window = ((9, 0), (17, 0))
    elif ex in ("NFO", "BFO"):
        window = ((9, 15), (15, 40))
    else:
        window = ((9, 15), (15, 30))
    return window[0] <= (now.hour, now.minute) < window[1]


def _calendar_window(day: date, exchange: str):
    """(start_ms, end_ms) of the trading window from the OpenAlgo market calendar, or None.
    Read only. Tests replace this.

    ``get_effective_session_window`` swallows its own errors and returns None, so None means
    "closed OR failure"; ``market_status`` decides which (see ``_confirmed_closed``).
    """
    from database import market_calendar_db as cal

    window = cal.get_effective_session_window(day, exchange)
    if window is None:
        return None
    return int(window["start_ms"]), int(window["end_ms"])


def _calendar_window_info(day: date, exchange: str) -> dict | None:
    """The calendar's session window dict ({start_ms, end_ms, is_special}) or None. Read only.
    Tests replace this. None means "closed OR failure" (see ``_calendar_window``)."""
    from database import market_calendar_db as cal

    return cal.get_effective_session_window(day, exchange)


def _confirmed_closed(day: date, exchange: str) -> bool:
    """True when the calendar says the exchange is closed that day (holiday or weekend).
    Tests replace this."""
    from database import market_calendar_db as cal

    return bool(cal.is_market_holiday(day, exchange))


def market_status(exchange: str, now: datetime) -> tuple[bool, str]:
    """(is_open, source): the market calendar when it answers, else the clock windows.

    Never raises. Only non-None calendar windows are cached (per day and exchange). A None
    answer is closed only when ``_confirmed_closed`` agrees; otherwise it may be a swallowed
    calendar failure and the clock windows are used (source 'clock'), uncached.
    """
    ex = (exchange or "").upper()
    if ex in _CALENDAR_EXCHANGES:
        try:
            day = now.date()
            key = (day, ex)
            for old in [k for k in _calendar_cache if k[0] != day]:
                _calendar_cache.pop(old, None)
            window = _calendar_cache.get(key)
            if window is None:
                window = _calendar_window(day, ex)
                if window is not None:
                    _calendar_cache[key] = window
                elif _confirmed_closed(day, ex):
                    return False, "calendar"
                else:
                    raise RuntimeError("calendar returned no window on a trading day")
            now_ms = int((now - datetime(1970, 1, 1)).total_seconds() * 1000) - _IST_OFFSET_MS
            return window[0] <= now_ms < window[1], "calendar"
        except Exception as e:
            logger.warning("Discretionary market calendar unavailable: %s", type(e).__name__)
    return _clock_open(ex, now), "clock"


def _ms_to_naive_ist(ms: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(milliseconds=int(ms) + _IST_OFFSET_MS)


def session_provider():
    """A memoising ``SessionProvider`` (day, exchange) -> an.SessionWindow | None for the
    pure classifier and excursion code, over the existing market calendar
    (``get_effective_session_window``, special sessions and per-exchange timings included).

    The calendar returns None for both "closed" and an internal failure, so a None answer is
    a CONFIRMED closed day only when ``_confirmed_closed`` agrees (holiday or weekend);
    otherwise, and for an exchange the calendar does not model, or when any call raises, it
    raises ``an.SessionCalendarUnavailable``. The result is memoised per provider instance
    only (one request), never across requests. Nothing is written.
    """
    memo: dict[tuple[date, str], an.SessionWindow | None] = {}

    def provider(day: date, exchange: str):
        ex = (exchange or "").upper()
        key = (day, ex)
        if key in memo:
            return memo[key]
        if ex not in _CALENDAR_EXCHANGES:
            raise an.SessionCalendarUnavailable(ex)
        try:
            info = _calendar_window_info(day, ex)
            if info is None:
                if not _confirmed_closed(day, ex):
                    raise an.SessionCalendarUnavailable(ex)
                window = None
            else:
                window = an.SessionWindow(
                    _ms_to_naive_ist(info["start_ms"]),
                    _ms_to_naive_ist(info["end_ms"]),
                    bool(info.get("is_special")),
                )
        except an.SessionCalendarUnavailable:
            raise
        except Exception as e:
            logger.warning("Discretionary session calendar failed: %s", type(e).__name__)
            raise an.SessionCalendarUnavailable(ex) from e
        memo[key] = window
        return window

    return provider


def market_open(exchange: str, now: datetime) -> bool:
    """True when the exchange is open at ``now`` (naive IST); see ``market_status``."""
    return market_status(exchange, now)[0]


def _combine_sources(sources) -> str:
    sources = set(sources)
    if not sources:
        return "clock"
    return sources.pop() if len(sources) == 1 else "mixed"


def _clean_filters(broker=None, account=None, symbol=None) -> dict:
    return {k: v for k, v in (("broker", broker), ("account", account), ("symbol", symbol)) if v}


def _round(value, places: int = 4):
    return None if value is None else round(float(value), places)


# ---------------------------------------------------------------- ledger access (read only)


def _load_ledger_fills() -> list[dict]:
    """Every ledger fill as a plain dict. Reads only."""
    try:
        rows = ledger.db_session.execute(select(ledger.LedgerFill).order_by(ledger.LedgerFill.id))
        return [
            {c.name: getattr(r, c.name) for c in ledger.LedgerFill.__table__.columns}
            for r in rows.scalars()
        ]
    finally:
        ledger.db_session.remove()


def _cost_model_digest(brokers) -> str:
    """Hash of the cost schedules and broker fee settings the charges of ``brokers`` use."""
    from portfolio.broker_fees import fees_for
    from services.discretionary_charges import schedule_for

    probes = ((1000.0, 0.0), (0.0, 1000.0), (1_000_000.0, 0.0), (0.0, 1_000_000.0))
    model: dict = {}
    for broker in sorted(brokers):
        fees = asdict(fees_for(broker))
        for noise in ("source", "verified_on", "label"):
            fees.pop(noise, None)
        entry: dict = {"fees": fees, "schedules": {}}
        for exchange in ("NSE", "BSE"):
            for product in ("MIS", "CNC"):
                schedule = schedule_for(exchange, product, broker)
                if schedule is None:
                    entry["schedules"][f"{exchange}/{product}"] = None
                    continue
                rows = [
                    {
                        k: repr(float(v))
                        for k, v in sorted(schedule.breakdown(b, s, orders=2).items())
                    }
                    for b, s in probes
                ]
                entry["schedules"][f"{exchange}/{product}"] = {
                    "tax_rate": repr(float(schedule.tax_rate)),
                    "probes": rows,
                }
        model[str(broker)] = entry
    return hashlib.sha256(json.dumps(model, sort_keys=True).encode("utf-8")).hexdigest()


def fingerprint(fills: list[dict]) -> str:
    """sha256 over the sorted (id, attribution_state, qty, price, fill_time, side, symbol,
    exchange, product, account, broker, order_id) of ALL fills, the
    engine version, SESSION_EXPIRY_TIME and the cost model of the brokers present."""
    digest = hashlib.sha256()
    for f in sorted(fills, key=lambda f: f["id"]):
        line = (
            f"{f['id']}|{f['attribution_state']}|{float(f['qty'])!r}|"
            f"{float(f['price'])!r}|{f['fill_time'].isoformat()}|"
            f"{f['side']}|{f['symbol']}|{f['exchange']}|{f['product']}|"
            f"{f['account']}|{f['broker']}|{f['order_id']}\n"
        )
        digest.update(line.encode("utf-8"))
    hour, minute = session_expiry()
    brokers = {f["broker"] for f in fills}
    digest.update(
        (
            f"engine={ENGINE_VERSION}|expiry={hour:02d}:{minute:02d}|"
            f"cost={_cost_model_digest(brokers)}\n"
        ).encode()
    )
    return digest.hexdigest()


def reliable_since(fills: list[dict] | None = None) -> date:
    """Start date of Discretionary history: env DISCRETIONARY_RELIABLE_SINCE, else the session
    date of the first synced fill, else today."""
    raw = os.getenv("DISCRETIONARY_RELIABLE_SINCE", "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            logger.warning("DISCRETIONARY_RELIABLE_SINCE is not YYYY-MM-DD; using first fill")
    fills = _load_ledger_fills() if fills is None else fills
    if fills:
        return min(session_date(f["fill_time"]) for f in fills)
    return session_date(_now())


# ---------------------------------------------------------------- rebuild


def _ensure_tables() -> None:
    """Create the derived tables once per process (not on every read)."""
    global _tables_ready
    if _tables_ready:
        return
    # Claim under the lock that checks: two first requests on pooled threads would both run
    # create_all on a fresh database, and the second CREATE TABLE fails as "already exists".
    with _tables_lock:
        if not _tables_ready:
            book.init_discretionary_book_db()
            _tables_ready = True


_last_mismatch: tuple | None = None


def _note_fingerprint_mismatch(meta, fp: str, fills: list[dict], force: bool) -> None:
    """Warn once when the SAME stored-vs-computed mismatch recurs on the next read with
    unchanged fills: another process with different settings keeps rebuilding the book."""
    global _last_mismatch
    if force or not meta or meta["fingerprint"] == fp:
        return
    inputs = hashlib.sha256(
        "|".join(f"{f['id']}:{f['attribution_state']}" for f in fills).encode()
    ).hexdigest()
    state = (meta["fingerprint"], fp, inputs)
    if state == _last_mismatch:
        logger.warning(
            "Discretionary fingerprint flip-flop: stored book keeps differing with no input "
            "change; another process may run with different settings"
        )
    _last_mismatch = state


def _rebuild_locked(fills: list[dict], force: bool = False) -> dict:
    """Rebuild when the fingerprint of ``fills`` differs from the stored one. Caller holds the
    lock and has loaded ``fills`` (the ledger is read once per operation)."""
    _ensure_tables()
    fp = fingerprint(fills)
    meta = book.get_meta()
    _note_fingerprint_mismatch(meta, fp, fills, force)
    if (
        meta
        and not force
        and meta["fingerprint"] == fp
        and meta["engine_version"] == ENGINE_VERSION
    ):
        return {
            "build_id": meta["build_id"],
            "fingerprint": fp,
            "rebuilt": False,
            "fills": meta["fill_count"],
            "trades": len(book.load_trades(build_id=meta["build_id"])),
            "positions": len(book.load_positions(build_id=meta["build_id"])),
            "completed_trades": len(book.load_positions(status="FLAT", build_id=meta["build_id"])),
        }
    disc = [f for f in fills if f["attribution_state"] in DISCRETIONARY_STATES]
    result = reconstruct(disc)
    allocate_charges(result, {f["id"]: f for f in disc})
    build_id = f"{_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    book.replace_build(
        {
            "build_id": build_id,
            "built_at": _now(),
            "fingerprint": fp,
            "fill_count": len(fills),
            "engine_version": ENGINE_VERSION,
        },
        result["positions"],
        result["trades"],
        result["links"],
        result["orphans"],
    )
    logger.info(
        "Discretionary book rebuilt: build=%s fills=%d trades=%d positions=%d orphans=%d",
        build_id,
        len(fills),
        len(result["trades"]),
        len(result["positions"]),
        len(result["orphans"]),
    )
    return {
        "build_id": build_id,
        "fingerprint": fp,
        "rebuilt": True,
        "fills": len(fills),
        "trades": len(result["trades"]),
        "positions": len(result["positions"]),
        "completed_trades": sum(1 for p in result["positions"] if p["status"] == "FLAT"),
    }


def rebuild(force: bool = False) -> dict:
    """Rebuild the derived book when the ledger fingerprint changed (or ``force``).

    ``trades`` in the answer counts closing legs (rows of disc_trades); ``completed_trades``
    counts positions that went back to flat."""
    with _rebuild_lock:
        return _rebuild_locked(_load_ledger_fills(), force)


def _snapshot(
    filters: dict,
    *,
    trades=False,
    positions=False,
    orphans=False,
    start=None,
    end=None,
    trade_id: int | None = None,
    links=False,
) -> dict:
    """One consistent read: load the ledger and fingerprint once, rebuild at most once, then
    read the requested tables for that SAME build_id. Retries if another process replaced the
    build in between (the in-process lock already excludes our own rebuilds)."""
    for _ in range(3):
        with _rebuild_lock:
            fills = _load_ledger_fills()
            info = _rebuild_locked(fills)
            bid = info["build_id"]
            snap = {"build_id": bid, "fills": fills, "info": info}
            if trades:
                snap["trades"] = book.load_trades(filters, start, end, build_id=bid)
                snap["flat_positions"] = book.load_positions(filters, status="FLAT", build_id=bid)
            if positions:
                snap["positions"] = book.load_positions(filters, status="OPEN", build_id=bid)
            if orphans:
                snap["orphans"] = book.load_orphans(filters, build_id=bid)
            if links:  # every link of the build (data-quality states); unscoped read, so filter
                snap["links"] = [lk for lk in book.load_links() if lk.get("build_id") == bid]
            if trade_id is not None:
                snap.update(_completed_trade_read(trade_id, bid))
            meta = book.get_meta()
        if meta and meta["build_id"] == bid:
            return snap
    raise RuntimeError("discretionary book kept changing during a read")


def _completed_trade_read(position_id: int, bid: str) -> dict:
    """{"trade": the completed trade with id ``position_id`` in build ``bid`` (None when that
    position does not exist or is still open), "trade_links": the fill links of ALL its legs}.
    Caller holds the rebuild lock."""
    pos = book.load_position(position_id, bid)
    if pos is None or pos["status"] != "FLAT":
        return {"trade": None, "trade_links": []}
    ident = {k: pos[k] for k in ("broker", "account", "exchange", "symbol", "product")}
    legs = [
        t
        for t in book.load_trades(ident, build_id=bid)
        if int(t["position_seq"]) == int(pos["position_seq"])
        # _filtered skips empty filter values: re-check the identity exactly
        and all(t[k] == v for k, v in ident.items())
    ]
    completed = an.group_completed(legs, [pos])
    if not completed:
        return {"trade": None, "trade_links": []}
    links = []
    for leg in legs:
        links.extend(book.load_trade_fills(leg["id"], bid))
    return {"trade": completed[0], "trade_links": links}


# ---------------------------------------------------------------- read API


def _source_fields(f: dict) -> dict:
    """Read-time source label (SIGNAL_LIVE / MANUAL / UNCLASSIFIED) and evidence label of a
    ledger fill. Display only: no figure, state or accounting reads them."""
    evidence = ledger_svc.parse_evidence(f.get("source_evidence"))
    return {
        "source_label": ledger_svc.source_label(f.get("attribution_state")),
        "source_evidence": ledger_svc.evidence_label(evidence) if evidence else None,
    }


def _unresolved_of(fills: list[dict], filters: dict) -> list[dict]:
    out = []
    for f in fills:
        if f["attribution_state"] != ledger.STATE_UNRESOLVED:
            continue
        if any(f.get(k) != v for k, v in filters.items()):
            continue
        row = {
            k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v)
            for k, v in f.items()
            if k not in ("raw_hash", "source_evidence")
        }
        row.update(_source_fields(f))
        out.append(row)
    return out


def source_label_counts(fills: list[dict], filters: dict, start: str, end: str) -> dict:
    """Read-only count of ALL ledger fills of the period and filters per source label.

    Every state counts here (strategy fills included, as SIGNAL_LIVE), so it describes source
    identification only; it feeds no P&L, metric or attribution.
    """
    counts = {ledger_svc.SIGNAL_LIVE: 0, ledger_svc.MANUAL: 0, ledger_svc.UNCLASSIFIED: 0}
    for f in fills:
        if any(f.get(k) != v for k, v in filters.items()):
            continue
        if f.get("fill_time") is None:
            continue
        day = session_date(f["fill_time"]).isoformat()
        if not (start <= day <= end):
            continue
        counts[ledger_svc.source_label(f.get("attribution_state"))] += 1
    return counts


def execution_counts(fills: list[dict], filters: dict, start: str, end: str) -> dict:
    """Read-only counts of discretionary CANDIDATE executions for the period and filters.

    ``executions_collected``: ledger fills whose state is manual, ui_manual or unresolved
    (the fills that are, or may become, discretionary) whose session date of ``fill_time`` is
    in [``start``, ``end``] and that match the broker / account / symbol ``filters``.
    strategy_* fills are NOT counted: they belong to the strategy side, never to this view.
    ``executions_unresolved``: the unresolved share of that same set (awaiting attribution,
    excluded from P&L). These counts only describe the ledger; they feed no P&L, metric or
    attribution, and the unresolved list (``get_unresolved``) stays unbounded by period.
    """
    collected = unresolved = 0
    for f in fills:
        state = f["attribution_state"]
        if state not in DISCRETIONARY_STATES and state != ledger.STATE_UNRESOLVED:
            continue
        if any(f.get(k) != v for k, v in filters.items()):
            continue
        if f.get("fill_time") is None:
            continue
        day = session_date(f["fill_time"]).isoformat()
        if not (start <= day <= end):
            continue
        collected += 1
        if state == ledger.STATE_UNRESOLVED:
            unresolved += 1
    return {"executions_collected": collected, "executions_unresolved": unresolved}


def get_trades(broker=None, account=None, symbol=None, start=None, end=None) -> list[dict]:
    """Closing LEGS (one row per closing fill, partial exits included; close order),
    ``start``/``end`` on the leg's trade_date (inclusive). For completed trades (position flat
    to flat) use ``get_completed_trades``."""
    filters = _clean_filters(broker, account, symbol)
    return _snapshot(filters, trades=True, start=start, end=end)["trades"]


def get_completed_trades(
    broker=None, account=None, symbol=None, start=None, end=None
) -> list[dict]:
    """Completed trades (one per position from flat to flat; close order), ``start``/``end``
    on the trade_date of the FINAL close (inclusive)."""
    snap = _snapshot(_clean_filters(broker, account, symbol), trades=True)
    done = an.group_completed(snap["trades"], snap["flat_positions"])
    return [
        t
        for t in done
        if (not start or t["trade_date"] >= start) and (not end or t["trade_date"] <= end)
    ]


def get_unresolved(broker=None, account=None, symbol=None) -> list[dict]:
    """Ledger fills still unresolved: listed, never counted in P&L."""
    return _unresolved_of(_load_ledger_fills(), _clean_filters(broker, account, symbol))


def get_orphans(broker=None, account=None, symbol=None) -> list[dict]:
    """Discretionary fills the engine refused (invalid, or reducing an unknown opening)."""
    return _snapshot(_clean_filters(broker, account, symbol), orphans=True)["orphans"]


# ---------------------------------------------------------------- quotes / unrealized


def _auth_rows(broker: str) -> list[tuple[str, str]]:
    """(name, user_id) of every non-revoked Auth row of ``broker``. Read only."""
    from database.auth_db import Auth
    from database.auth_db import db_session as auth_session

    try:
        return [
            (a.name, a.user_id)
            for a in Auth.query.filter_by(is_revoked=False).all()
            if a.name and (a.broker or "").strip().lower() == broker
        ]
    finally:
        auth_session.remove()


def _tokens_for(name: str):
    from database.auth_db import get_auth_token, get_feed_token

    token = get_auth_token(name)
    return (token, get_feed_token(name)) if token else None


def _default_auth(broker: str, account: str | None = None):
    """(auth_token, feed_token) for QUOTES; None when there is none.

    Prefers the session of ``account``, else falls back to any logged-in user of the broker.
    That fallback is only safe for quotes: an LTP for (symbol, exchange) comes from the same
    broker feed whichever user asks, so it does not depend on the account. Never use it for
    account-specific data (see ``_default_account_auth``).
    """
    try:
        rows = _auth_rows(broker)
        rows.sort(key=lambda r: 0 if account and account in (r[0], r[1]) else 1)
        for name, _uid in rows:
            auth = _tokens_for(name)
            if auth:
                return auth
    except Exception as e:
        logger.error("Discretionary auth lookup failed: %s", type(e).__name__)
    return None


def _identity_names(broker: str, account: str) -> list[str]:
    """Auth names whose canonical ledger account on ``broker`` is ``account``; [] on error."""
    try:
        return ledger.auth_names_for_account(broker, account)
    except Exception as e:
        logger.warning("Discretionary identity lookup failed: %s", type(e).__name__)
        return []


def _default_account_auth(broker: str, account: str):
    """(auth_token, feed_token) of exactly ``account`` on ``broker``, else None.

    For account-specific reads (the PositionBook): no fallback to another user of the broker.
    ``account`` must equal, exactly, the Auth row's broker user_id (preferred), else the
    canonical account remembered for that login in ledger_account_identity, else its name.
    """
    try:
        rows = _auth_rows(broker) if account else []
        known = set(_identity_names(broker, account)) if rows else set()
        # exact broker user_id first, then identity, then exact name; case-sensitive
        for matches in (
            [r for r in rows if r[1] == account],
            [r for r in rows if r[0] in known],
            [r for r in rows if r[0] == account],
        ):
            for name, _uid in matches:
                auth = _tokens_for(name)
                if auth:
                    return auth
            if matches:
                break
    except Exception as e:
        logger.error("Discretionary account auth lookup failed: %s", type(e).__name__)
    return None


_auth_provider = _default_auth
_account_auth_provider = _default_account_auth


def _get_multiquotes(auth_token, feed_token, broker, symbols):
    from services.quotes_service import get_multiquotes_with_auth

    return get_multiquotes_with_auth(auth_token, feed_token, broker, symbols)


def _get_positionbook(auth_token, broker):
    from services.positionbook_service import get_positionbook_with_auth

    return get_positionbook_with_auth(auth_token, broker)


def _fetch_broker_quotes(broker: str, pairs: list[tuple[str, str]], fetched_at: datetime):
    """One batched call. Returns ({(symbol, exchange): ltp or None}, call_ok)."""
    auth = _auth_provider(broker)
    if not auth:
        return {}, False
    token, feed = auth
    symbols = [{"symbol": s, "exchange": e} for s, e in pairs]
    try:
        ok, resp, _status = _get_multiquotes(token, feed, broker, symbols)
    except Exception as e:
        logger.warning("Discretionary quote call failed: broker=%s %s", broker, type(e).__name__)
        return {}, False
    if not ok or not isinstance(resp, dict):
        return {}, False
    out: dict[tuple[str, str], float | None] = {}
    for item in resp.get("results") or []:
        key = (item.get("symbol"), item.get("exchange"))
        data = item.get("data")
        ltp = None
        if not item.get("error") and isinstance(data, dict):
            try:
                value = float(data.get("ltp"))
                ltp = value if value > 0 and value == value else None
            except (TypeError, ValueError):
                ltp = None
        out[key] = ltp
    return out, True


def _position_view(pos: dict, ltp, ts: datetime | None, status: str, now: datetime | None = None):
    sign = 1.0 if pos["direction"] == "LONG" else -1.0
    unrealized = None
    if ltp is not None:
        unrealized = sign * pos["open_qty"] * (ltp - pos["avg_entry"])
    age = None
    if ts is not None and now is not None:
        age = max(0.0, (now - ts).total_seconds())
    view = dict(pos)
    view.update(
        realized_so_far=_round(pos.get("realized_gross") or 0.0),
        ltp=ltp,
        fetched_at=ts.strftime("%Y-%m-%d %H:%M:%S.%f") if ts else None,
        age_s=_round(age, 3),
        price_status=status,
        unrealized=_round(unrealized),
    )
    return view


def _price_positions(opens: list[dict], with_quotes: bool) -> tuple[list[dict], str]:
    """Attach ltp, fetched_at, age_s, price_status and unrealized to open positions.

    Returns (views, market_hours_source).
    """
    now = _now()
    if not with_quotes:
        return [_position_view(p, None, None, "not_requested", now) for p in opens], (
            market_status("NSE", now)[1]
        )
    by_broker: dict[str, list[tuple[str, str]]] = {}
    for p in opens:
        pair = (p["symbol"], p["exchange"])
        pairs = by_broker.setdefault(p["broker"], [])
        if pair not in pairs:
            pairs.append(pair)
    fresh: dict[tuple[str, str, str], tuple[float, datetime]] = {}
    failed_brokers: set[str] = set()
    for b, pairs in by_broker.items():
        fetched_at = _now()  # our clock, just before the call
        got, call_ok = _fetch_broker_quotes(b, pairs, fetched_at)
        if not call_ok:
            failed_brokers.add(b)
        for (s, e), ltp in got.items():
            if ltp is not None:
                fresh[(b, s, e)] = (ltp, fetched_at)
                _quote_cache[(b, s, e)] = (ltp, fetched_at)
    now = _now()
    views = []
    sources = []
    for p in opens:
        key = (p["broker"], p["symbol"], p["exchange"])
        ltp, ts, cached = None, None, False
        if key in fresh:
            ltp, ts = fresh[key]
        elif p["broker"] in failed_brokers and key in _quote_cache:
            ltp, ts = _quote_cache[key]
            cached = True
        is_open, source = market_status(p["exchange"], now)
        sources.append(source)
        if ltp is None:
            views.append(_position_view(p, None, None, "unavailable", now))
            continue
        if cached:
            status = "cached"
        elif not is_open:
            status = "last_close"
        elif (now - ts).total_seconds() > stale_after_s():
            status = "stale"
        else:
            status = "live"
        views.append(_position_view(p, ltp, ts, status, now))
    return views, _combine_sources(sources) if sources else market_status("NSE", now)[1]


def get_positions(broker=None, account=None, symbol=None, with_quotes: bool = True) -> dict:
    """Open discretionary positions, each with ltp, fetched_at, age_s, price_status, unrealized."""
    snap = _snapshot(_clean_filters(broker, account, symbol), positions=True)
    views, source = _price_positions(snap["positions"], with_quotes)
    return {"build_id": snap["build_id"], "positions": views, "market_hours_source": source}


# ---------------------------------------------------------------- metrics adapter


def to_metric_trades(trades: list[dict]) -> list[dict]:
    """Shape discretionary trades for ``compute_metrics`` (gross realized P&L)."""
    return [
        {
            "trade_date": t["trade_date"],
            "realized_pnl": float(t["realized_pnl"]),
            "strategy": "discretionary",
            "symbol": t["symbol"],
            "exchange": t["exchange"],
            "product": t["product"],
            "mode": "live",
            "direction": t["direction"],
            "closed_quantity": t["closed_quantity"],
            "entry_price": t["entry_price"],
            "exit_price": t["exit_price"],
            "broker": t["broker"],
            "closed_at": t["closed_at"],
            "opened_at": t["opened_at"],
            "account": t["account"],
            "charges_status": t["charges_status"],
        }
        for t in trades
    ]


def leg_and_trade_metrics(legs: list[dict], completed: list[dict], s: str, e: str) -> dict:
    """compute_metrics on the closing ``legs`` (money: P&L, equity, drawdown, daily P&L, best /
    worst day, streaks) with the COUNT_METRICS replaced by compute_metrics on the
    ``completed`` trades (counts, wins / losses, ratios)."""
    metrics = metrics_svc.compute_metrics(
        to_metric_trades(legs), s, e, costs=[t["charges"] for t in legs], capital=None
    )
    # Win / loss basis is NET of estimated charges: the completed trades go to compute_metrics
    # with their net P&L as ``realized_pnl``. A trade without a net is never measured on gross:
    # it is left out of these figures and counted in ``net_unavailable_trades``.
    net_rows = [
        dict(t, realized_pnl=an.net_value(t)) for t in completed if an.net_value(t) is not None
    ]
    counted = metrics_svc.compute_metrics(
        to_metric_trades(net_rows), s, e, costs=[t["charges"] for t in net_rows], capital=None
    )
    for name in COUNT_METRICS:
        metrics[name] = counted[name]
    metrics["count_unavailable_reason"] = None
    if not net_rows:
        # No completed trade has a net P&L: the ratios are unknown, not 0.
        for name in (
            "win_rate",
            "profit_factor",
            "expectancy",
            "avg_win",
            "avg_loss",
            "payoff_ratio",
        ):
            metrics[name] = None
        metrics["count_unavailable_reason"] = "no completed trades with a net P&L"
    metrics["trades_count"] = len(completed)
    metrics["net_basis_trades"] = len(net_rows)
    metrics["net_unavailable_trades"] = len(completed) - len(net_rows)
    metrics["count_basis"] = (
        "completed trades (a position from flat to flat); wins, losses, win rate, profit "
        "factor, expectancy and average win / loss use NET P&L after estimated charges "
        "(gross_profit / gross_loss here are sums of net wins / losses)"
    )
    return metrics


def withhold_capital_metrics(metrics: dict) -> dict:
    """Null every figure that compute_metrics derives from an assumed capital."""
    out = dict(metrics)
    withheld = {}
    for name in ("sharpe_ratio", "sortino_ratio", "ratio_capital", "return_pct"):
        out[name] = None
        withheld[name] = NO_CAPITAL
    out["ratio_basis"] = "withheld: " + NO_CAPITAL
    out["ratio_warning"] = (
        "Sharpe, Sortino and return on capital are not shown: discretionary trading has no "
        "capital basis."
    )
    out["withheld"] = withheld
    out["equity_basis"] = "gross realized P&L by close date; excludes unrealized P&L and charges"
    return out


# ---------------------------------------------------------------- summary


SYNC_OK_STATUSES = ("ok", "partial")
SYNC_BAD_LATEST = ("partial", "error", "no_auth", "broker_error")
SYNC_STALE_DAYS = 2
SYNC_LIST_CAP = 10


def _load_sync_runs() -> list[tuple]:
    """(broker, account, started_at, finished_at, status) of every sync run, oldest first."""
    try:
        rows = ledger.db_session.execute(
            select(
                ledger.LedgerSyncRun.broker,
                ledger.LedgerSyncRun.account,
                ledger.LedgerSyncRun.started_at,
                ledger.LedgerSyncRun.finished_at,
                ledger.LedgerSyncRun.status,
            ).order_by(ledger.LedgerSyncRun.started_at, ledger.LedgerSyncRun.id)
        ).all()
        return [tuple(r) for r in rows]
    finally:
        ledger.db_session.remove()


def _sync_health(now: datetime, period_start) -> tuple[dict, list[str]]:
    """Sync-run health for the summary: (sync_health dict, warnings). Never raises."""
    health = {"last_successful_sync": None, "runs_by_day": {}, "days_without_sync": []}
    try:
        runs = _load_sync_runs()
    except Exception as e:
        logger.warning("Could not read ledger sync runs for the summary: %s", type(e).__name__)
        return health, []
    warnings: list[str] = []
    today = now.date()
    ok_runs = [r for r in runs if r[4] in SYNC_OK_STATUSES]
    if not ok_runs:
        warnings.append(
            "no successful ledger sync yet: fills are not being collected; check the broker login."
        )
    else:
        last = max((r[3] or r[2]) for r in ok_runs)
        health["last_successful_sync"] = last.strftime("%Y-%m-%d %H:%M")
        warnings.append(f"last successful sync: {last.strftime('%Y-%m-%d %H:%M')} IST")
        if today.weekday() < 5 and (today - last.date()).days > SYNC_STALE_DAYS:
            warnings.append(
                f"the last successful sync is older than {SYNC_STALE_DAYS} days; "
                "fills since then may be missing."
            )
    if not runs:
        return health, warnings

    by_day: dict[str, dict[str, int]] = {}
    for _b, _a, started, _f, status in runs:
        if (today - started.date()).days < 7:
            day = by_day.setdefault(started.date().isoformat(), {})
            day[status] = day.get(status, 0) + 1
    health["runs_by_day"] = {k: by_day[k] for k in sorted(by_day)}

    ok_days = {r[2].date() for r in ok_runs}
    try:  # period_start None ("all"): the first ledger sync date is the start
        floor_day = date.fromisoformat(str(period_start)[:10])
    except ValueError:
        floor_day = runs[0][2].date()
    day = max(floor_day, runs[0][2].date())
    missing: list[date] = []
    calendar_used = True
    while day < today:  # today is still in progress
        if day.weekday() < 5 and day not in ok_days:
            closed = False
            try:
                closed = bool(_confirmed_closed(day, "NSE"))
            except Exception:
                calendar_used = False
            if not closed:
                missing.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    health["days_without_sync"] = [d.isoformat() for d in missing]
    if missing:
        shown = ", ".join(d.isoformat() for d in missing[:SYNC_LIST_CAP])
        more = len(missing) - SYNC_LIST_CAP
        tail = f" and {more} more" if more > 0 else ""
        hol = (
            "exchange holidays are excluded per the market calendar"
            if calendar_used
            else "exchange holidays are not excluded (the market calendar is unavailable)"
        )
        warnings.append(
            f"no successful ledger sync on weekday(s): {shown}{tail}. Fills of those days may "
            f"be missing because the broker TradeBook is a current-day book; {hol}."
        )

    latest: dict[tuple, tuple] = {}
    for r in runs:
        if r[1]:  # account "" (no account known yet) is account-agnostic: no per-account warning
            latest[(r[0], r[1])] = r
    for (broker, account), r in sorted(latest.items()):
        if r[4] in SYNC_BAD_LATEST:
            warnings.append(
                f"latest ledger sync for broker {broker or '?'} account {account or '?'} "
                f"ended {r[4]} at {r[2].strftime('%Y-%m-%d %H:%M')}."
            )
    # Runs labelled account "" (no_auth, broker_error, error, an early no_auth with broker "")
    # never enter the per-account map. The latest run of each broker across ALL labels
    # (broker "" is its own group) warns on its own when it is bad and the per-account loop
    # above has not already warned about that very run. A later ok run clears it.
    latest_broker: dict[str, tuple] = {}
    for r in runs:
        latest_broker[r[0]] = r
    for broker, r in sorted(latest_broker.items()):
        if r[4] in SYNC_BAD_LATEST and not (r[1] and latest.get((r[0], r[1])) is r):
            warnings.append(
                f"latest ledger sync for broker {broker or '?'} (no account known) "
                f"ended {r[4]} at {r[2].strftime('%Y-%m-%d %H:%M')}."
            )
    return health, warnings


def get_summary(
    period: str = "all",
    start: str | None = None,
    end: str | None = None,
    filters: dict | None = None,
    with_quotes: bool = True,
    category: str = "all",
) -> dict:
    """Metrics, realized/charges/net and unrealized for the period (see ``_summary_parts``)."""
    check_category(category)
    filters = _clean_filters(**{k: (filters or {}).get(k) for k in ("broker", "account", "symbol")})
    snap = _snapshot(filters, trades=True, positions=True, orphans=True, links=True)
    return _summary_parts(snap, period, start, end, filters, with_quotes, category)["summary"]


def _safe_block(name: str, build, warnings: list[str]):
    """Build one OPTIONAL summary block. A failure never fails the summary: it is logged
    (exception type only, no ids or values), the block becomes None and a warning names it.
    The page renders a short 'unavailable' note for a null block."""
    try:
        return build()
    except Exception as e:
        logger.error("Discretionary summary block failed: block=%s %s", name, type(e).__name__)
        warnings.append(f"{name} block unavailable (computation failed); totals are unaffected.")
        return None


class FilterError(ValueError):
    """A request filter / parameter (category, period, custom range) is invalid. The ONLY
    exception the routes turn into a 400 with its text; any other error is a 500 with a
    generic message, so an internal ValueError never leaks its text."""


def check_category(category) -> str:
    """The category filter value; ValueError (a 400 for the route) when it is not one of
    all / intraday / swing. ``unclassified`` is not a filter: it is in ``all`` only."""
    if category not in an.CATEGORY_FILTERS:
        raise FilterError(f"category must be one of {', '.join(an.CATEGORY_FILTERS)}")
    return category


CLASSIFICATION_FAILED = "classification_failed"


def classify_positions(snap: dict, floor: date, now: datetime, completed: list[dict]) -> dict:
    """{position_key: (category, reason)} for every completed position (``completed``) and
    every open position of the snapshot, by the exchange session windows (see
    ``an.classify_with_reason``; the calendar is the service's ``session_provider``)."""
    provider = session_provider()
    opens = snap.get("positions") or []
    open_orphans = an.orphan_fill_counts(snap.get("orphans"), opens) if opens else {}
    out: dict[tuple, tuple[str, str | None]] = {}
    for t in completed:
        out[an.position_key(t)] = an.classify_with_reason(
            t, provider, reliable_since=floor, now=now
        )
    for p in opens:
        key = an.position_key(p)
        row = dict(p, closed_at=None, orphan_fills=open_orphans.get(key, 0))
        out[key] = an.classify_with_reason(row, provider, reliable_since=floor, now=now)
    return out


def _scope(snap: dict, period: str, start, end, category: str) -> dict:
    """The rows of one reporting scope from ONE snapshot: the period, the classification of
    every position and the closing legs / completed positions selected by ``category``.

    Period basis. The period is resolved on ALL closing legs (so it does not move with the
    category). A completed position is in the period when its FINAL close (``trade_date``)
    is in it; a leg when its OWN close date is. Headline money sums the legs, position
    statistics the whole positions, so they differ only for positions whose legs straddle
    the period or that are still open (see ``reconciliation_block``); for intraday
    positions (one session) they are identical.

    Category. Positions are classified once (``classify_positions``); ``all`` keeps
    everything, including unclassified positions and the legs of open positions, while
    intraday / swing keep only positions of that category, their closing legs and (open)
    positions by the position key. Unclassified rows are in ``all`` only.
    """
    check_category(category)
    all_trades = snap["trades"]
    floor = reliable_since(snap["fills"])
    now = _now()
    today = session_date(now)
    try:
        s, e = metrics_svc.resolve_period(
            period, to_metric_trades(all_trades), today, start, end, floor=floor
        )
    except ValueError as exc:  # resolve_period validates only the period / custom range
        raise FilterError(str(exc)) from None
    completed_all = an.group_completed(
        all_trades, snap.get("flat_positions") or [], snap.get("orphans")
    )
    classification_failed = False
    try:
        classes = classify_positions(snap, floor, now, completed_all)
    except Exception as exc:
        # Never guess: every position becomes unclassified (in the 'all' view only).
        logger.error("Discretionary classification failed: %s", type(exc).__name__)
        classification_failed = True
        classes = {
            an.position_key(row): ("unclassified", CLASSIFICATION_FAILED)
            for row in list(completed_all) + list(snap.get("positions") or [])
        }
    for t in completed_all:
        t["category"], t["category_reason"] = classes[an.position_key(t)]

    def keep(row: dict) -> bool:
        return (
            category == "all" or classes.get(an.position_key(row), ("unclassified",))[0] == category
        )

    legs_period = [t for t in all_trades if s <= t["trade_date"] <= e]
    trades_period = [t for t in completed_all if s <= t["trade_date"] <= e]
    return {
        "s": s,
        "e": e,
        "floor": floor,
        "now": now,
        "today": today,
        "all_trades": all_trades,
        "completed_all": completed_all,
        "classes": classes,
        "keep": keep,
        "legs_period": legs_period,
        "trades_period": trades_period,
        "legs": [t for t in legs_period if keep(t)],
        "trades": [t for t in trades_period if keep(t)],
        "category": category,
        "classification_failed": classification_failed,
    }


def classification_block(scope: dict) -> dict:
    """Classification counts of the closed positions in the period (every category, whatever
    the filter) and of the open positions, with the unclassified reasons."""
    closed = [(t["category"], t["category_reason"]) for t in scope["trades_period"]]
    closed_keys = {an.position_key(t) for t in scope["completed_all"]}
    return {
        "selected": scope["category"],
        "closed": an.category_counts(closed),
        "open": an.category_counts(
            [v for k, v in scope["classes"].items() if k not in closed_keys]
        ),
        "note": (
            "intraday = opened and closed in the same exchange session; swing = still open after "
            "the opening session ended; unclassified (reason shown) is never guessed and is in "
            "the 'all' view only. Product (MIS / CNC) is not used."
        ),
    }


def unresolved_in_range(snap: dict, filters: dict, start: str, end: str) -> dict:
    """Unresolved ledger fills and orphan fills (fills the engine could not place) whose
    session date lies in [start, end], for the broker / account / symbol filters. Both are
    excluded from every figure. ``/unresolved`` lists them; this is the count only."""
    unresolved = execution_counts(snap["fills"], filters, start, end)["executions_unresolved"]
    orphans = 0
    for o in snap.get("orphans") or []:
        moment = an.parse_time(o.get("fill_time"))  # the book returns ISO strings
        if moment is not None and start <= session_date(moment).isoformat() <= end:
            orphans += 1
    return {
        "unresolved_fills": unresolved,
        "orphan_fills": orphans,
        "total": unresolved + orphans,
        "start": start,
        "end": end,
    }


def _leg_net0(leg: dict) -> float:
    """Net of one closing leg on the headline basis: an unavailable charge counts as 0."""
    return float(leg["realized_pnl"]) - float(leg["charges"] or 0.0)


def reconciliation_block(scope: dict) -> dict:
    """How the headline (closing LEGS by their own close date) reconciles to the closed
    position ledger (whole POSITIONS by final close date) for the same filters.

    ``headline_net`` = sum of (realized - charges) of the selected legs in the period
    (an unavailable charge as 0) = the summary ``net``. ``closed_positions_net`` = sum of
    ``net_pnl`` of the selected closed positions in the period that have one. The difference
    is exactly the sum of its parts:

    * ``open_position_partial_exits``: legs in the period of positions still open;
    * ``later_closed_partial_exits``: legs in the period of positions whose final close is
      after the period;
    * ``earlier_legs_of_closed_positions``: legs BEFORE the period of positions closed in
      it (subtracted: they belong to the position, not to the period's headline);
    * ``net_unavailable_positions_legs``: legs in the period of closed positions without a
      net (not in ``closed_positions_net``).

    ``reconciled`` is True when headline - closed_positions_net equals those parts.
    """
    s, e = scope["s"], scope["e"]
    final = {an.position_key(t): t for t in scope["completed_all"]}
    selected = {an.position_key(t): t for t in scope["trades"]}
    headline = sum(_leg_net0(t) for t in scope["legs"])
    positions_net = sum(float(t["net_pnl"]) for t in scope["trades"] if t["net_pnl"] is not None)
    open_partial = later = earlier = unavailable = 0.0
    for leg in scope["all_trades"]:
        if not scope["keep"](leg):
            continue
        key = an.position_key(leg)
        pos = final.get(key)
        in_range = s <= leg["trade_date"] <= e
        if in_range:
            if pos is None:
                open_partial += _leg_net0(leg)
            elif pos["trade_date"] > e:
                later += _leg_net0(leg)
            elif key in selected and selected[key]["net_pnl"] is None:
                unavailable += _leg_net0(leg)
        elif key in selected and leg["trade_date"] < s and selected[key]["net_pnl"] is not None:
            earlier += _leg_net0(leg)
    parts = open_partial + later - earlier + unavailable
    return {
        "basis": (
            "headline = closing legs by their own close date; positions = whole positions by "
            "final close date; both after estimated charges"
        ),
        "headline_net": _round(headline),
        "closed_positions_net": _round(positions_net),
        "closed_positions": len(scope["trades"]),
        "net_unavailable_positions": sum(1 for t in scope["trades"] if t["net_pnl"] is None),
        "open_position_partial_exits": _round(open_partial),
        "later_closed_partial_exits": _round(later),
        "earlier_legs_of_closed_positions": _round(earlier),
        "net_unavailable_positions_legs": _round(unavailable),
        "difference": _round(headline - positions_net),
        "reconciled": abs((headline - positions_net) - parts) < 1e-6,
    }


def _summary_parts(
    snap: dict,
    period: str,
    start: str | None,
    end: str | None,
    filters: dict,
    with_quotes: bool,
    category: str = "all",
) -> dict:
    """Build the summary from ONE snapshot. Returns {"summary", "trades" (in the period),
    "positions" (priced views), "orphans", "today"} so a caller that needs more than the
    summary does not read the book again.

    Metrics, realized/charges/net and unrealized for the period; filters are
    broker / account / symbol. Totals are sums of the per-identity figures. Money sums the
    closing legs closed in the period; ``trades`` counts (per identity and in the metrics)
    are completed trades whose final close is in the period. ``trades`` in the answer are
    those completed trades, ``legs`` the closing legs of the period.

    Naming: ``realized_gross`` is realized P&L before charges (the same basis as
    ``metrics.net_profit``, which is therefore gross despite its name); ``charges`` are
    estimated; top-level ``net`` == ``realized_net_of_charges`` == realized_gross - charges.
    One snapshot (one ledger load, one fingerprint, one build_id) feeds every figure.
    """
    scope = _scope(snap, period, start, end, category)
    all_trades, floor, today = scope["all_trades"], scope["floor"], scope["today"]
    s, e = scope["s"], scope["e"]
    legs, trades = scope["legs"], scope["trades"]
    keep = scope["keep"]

    def _blank(t: dict) -> dict:
        return {
            "broker": t["broker"],
            "account": t["account"],
            "exchange": t["exchange"],
            "symbol": t["symbol"],
            "product": t["product"],
            "trades": 0,
            "realized_gross": 0.0,
            "charges": 0.0,
            "charges_unavailable_trades": 0,
            "unrealized": 0.0,
            "unrealized_unavailable_positions": 0,
        }

    per: dict[tuple, dict] = {}
    for t in trades:  # completed trades: the count
        key = (t["broker"], t["account"], t["exchange"], t["symbol"], t["product"])
        per.setdefault(key, _blank(t))["trades"] += 1
    for t in legs:  # closing legs: the money
        row = per.setdefault(
            (t["broker"], t["account"], t["exchange"], t["symbol"], t["product"]), _blank(t)
        )
        row["realized_gross"] += t["realized_pnl"]
        if t["charges"] is None:
            row["charges_unavailable_trades"] += 1
        else:
            row["charges"] += t["charges"]

    pos_view_all, hours_source = _price_positions(snap["positions"], with_quotes)
    pos_view = [p for p in pos_view_all if keep(p)]
    for p in pos_view:
        row = per.setdefault(
            (p["broker"], p["account"], p["exchange"], p["symbol"], p["product"]), _blank(p)
        )
        if p["unrealized"] is None:
            row["unrealized_unavailable_positions"] += 1
        else:
            row["unrealized"] += p["unrealized"]

    per_identity = []
    for row in per.values():
        row["net"] = row["realized_gross"] - row["charges"]
        per_identity.append({k: (_round(v) if isinstance(v, float) else v) for k, v in row.items()})
    realized = sum(r["realized_gross"] for r in per.values())
    charges = sum(r["charges"] for r in per.values())
    unavailable = sum(r["charges_unavailable_trades"] for r in per.values())
    unrealized_total = sum(r["unrealized"] for r in per.values())
    unrealized_missing = sum(r["unrealized_unavailable_positions"] for r in per.values())

    metrics = withhold_capital_metrics(leg_and_trade_metrics(legs, trades, s, e))
    metrics["charges_unavailable_trades"] = unavailable
    # Discretionary path only, after compute_metrics (shared with Live/Paper, unchanged): its
    # drawdown seeds the equity peak from the FIRST day, so a first-day loss is invisible.
    # Here the peak starts at 0 (see an.max_drawdown_from_zero); same gross-realized basis.
    daily_for_dd = an.period_breakdown(trades, "daily", legs)
    if len(daily_for_dd) >= an.MIN_DAYS_FOR_DAILY:
        metrics["max_drawdown"] = an.max_drawdown_from_zero(daily_for_dd)
        metrics["max_drawdown_note"] = (
            "equity peak starts at 0 (a first-day loss counts); gross realized by close date"
        )

    warnings = []
    if unavailable:
        warnings.append(
            f"{unavailable} trade(s) have no charge estimate (instrument not modelled); "
            "net P&L counts their charges as 0 and is not complete."
        )
    unresolved = len(_unresolved_of(snap["fills"], filters))
    if unresolved:
        warnings.append(f"{unresolved} unresolved fill(s) are excluded from P&L until attributed.")
    orphans = snap["orphans"]
    if orphans:
        warnings.append(
            f"{len(orphans)} fill(s) could not be placed (opening not in the ledger or invalid) "
            "and are excluded."
        )
    if unrealized_missing:
        warnings.append(f"{unrealized_missing} open position(s) have no usable price.")
    stale = [p for p in pos_view if p["price_status"] in ("stale", "last_close")]
    if stale:
        warnings.append(f"{len(stale)} open position(s) priced from a stale or last-close quote.")
    cached = [p for p in pos_view if p["price_status"] == "cached"]
    if cached:
        warnings.append(
            f"{len(cached)} open position(s) priced from a cached quote (the quote call failed); "
            "see age_s."
        )
    mis_old = [
        p for p in pos_view if p["product"] == "MIS" and p["opened_session"] < today.isoformat()
    ]
    if mis_old:
        warnings.append(
            f"{len(mis_old)} open MIS position(s) were opened in an earlier session: intraday "
            "positions are squared off, so a fill is probably missing from the ledger."
        )
    if any(p["opening_unknown"] for p in pos_view):
        warnings.append("some open positions follow a fill with an unknown opening.")
    if with_quotes is False:
        warnings.append("unrealized P&L not requested.")
    sync_health, sync_warnings = _sync_health(_now(), None if period == "all" else s)
    warnings.extend(sync_warnings)
    scope_start = floor.isoformat() if period == "all" else s
    if scope.get("classification_failed"):
        warnings.append(
            "classification failed: every position is shown as unclassified (in the 'all' view "
            "only); totals are unaffected."
        )

    summary = {
        "period": {"period": period, "start": s, "end": e, "reliable_since": floor.isoformat()},
        "metrics": metrics,
        "build_id": snap["build_id"],
        "market_hours_source": hours_source,
        # category filter (all | intraday | swing) and the classification of the period; the
        # data_quality, today, execution and source-label blocks ignore it (ledger-level)
        "category": category,
        "classification": _safe_block(
            "classification", lambda: classification_block(scope), warnings
        ),
        "unresolved_in_range": _safe_block(
            "unresolved_in_range",
            lambda: unresolved_in_range(snap, filters, scope_start, e),
            warnings,
        ),
        "reconciliation": _safe_block(
            "reconciliation", lambda: reconciliation_block(scope), warnings
        ),
        "realized_gross": _round(realized),
        "charges": _round(charges),
        "net": _round(realized - charges),
        "realized_net_of_charges": _round(realized - charges),
        "net_definition": (
            "net and realized_net_of_charges = realized_gross minus estimated charges; "
            "realized_gross and metrics.net_profit are before charges"
        ),
        "charges_unavailable_trades": unavailable,
        "unrealized_total": _round(unrealized_total),
        "open_positions": len(pos_view),
        "per_identity": per_identity,
        "sync_health": sync_health,
        "warnings": warnings,
        # read-only ledger counts for the page's empty state (see execution_counts). "all"
        # resolves to the first closed trade (today when there is none), so its counts start
        # at the reliable_since floor instead: older unresolved fills still count.
        **execution_counts(snap["fills"], filters, scope_start, e),
        # read-time source labels of every ledger fill of the period (display only)
        "source_labels": source_label_counts(snap["fills"], filters, scope_start, e),
        # sample-size context of the ratios (the existing edge-check rule; see sample_context)
        "sample": sample_context(len(trades), len({t["trade_date"] for t in trades})),
        # four-state count of the candidate executions of the period (see data_quality)
        "data_quality": _safe_block(
            "data_quality",
            lambda: data_quality(
                snap,
                filters,
                scope_start,
                e,
                scope["legs_period"],
                scope["trades_period"],
                pos_view_all,
                with_quotes,
                sync_health,
                sync_warnings,
            ),
            warnings,
        ),
        # unresolved fills replayed in memory; in NO total (see provisional_block)
        "provisional": _safe_block(
            "provisional",
            lambda: provisional_block(
                snap["fills"],
                filters,
                {
                    (p["broker"], p["account"], p["exchange"], p["symbol"], p["product"])
                    for p in pos_view
                },
            ),
            warnings,
        ),
        # the current session, from the same build rows (ignores the period; see today_block)
        "today": _safe_block(
            "today",
            lambda: today_block(
                all_trades,
                snap.get("flat_positions") or [],
                pos_view_all,
                snap["fills"],
                filters,
                today,
                with_quotes,
            ),
            warnings,
        ),
    }
    return {
        "summary": summary,
        "trades": trades,
        "legs": legs,
        "positions": pos_view,
        "orphans": orphans,
        "today": today,
        "scope": scope,
    }


# ---------------------------------------------------------------- today (current session)

TODAY_METRICS = (
    "trades_count",
    "winning_trades",
    "losing_trades",
    "win_rate",
    "gross_profit",
    "gross_loss",
    "profit_factor",
    "expectancy",
    "avg_win",
    "avg_loss",
    "count_unavailable_reason",
)


def _ident_seq(row: dict) -> tuple:
    return (
        row["broker"],
        row["account"],
        row["exchange"],
        row["symbol"],
        row["product"],
        int(row["position_seq"]),
    )


def _last_attribution_run() -> tuple[datetime, datetime | None] | None:
    """(started_at, finished_at) of the newest ok/partial ledger sync run (any broker /
    account), or None.

    A successful sync re-attributes EVERY unresolved fill of every broker and account with
    ``now`` = its run start (ledger_svc.sync_once) and records the run after that, so this
    run's start is when the stored attribution states were last evaluated, for every fill
    inserted before it finished. Read only; None when the runs cannot be read."""
    try:
        runs = _load_sync_runs()
    except Exception as e:
        logger.warning(
            "Could not read ledger sync runs for pending attribution: %s", type(e).__name__
        )
        return None
    ok = [r for r in runs if r[4] in SYNC_OK_STATUSES and r[2] is not None]
    if not ok:
        return None
    last = max(ok, key=lambda r: r[2])
    return last[2], last[3]


def _session_of(value) -> str | None:
    """Session date (ISO) of a datetime or a 'YYYY-MM-DD HH:MM:SS' string; None otherwise."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return session_date(value).isoformat() if isinstance(value, datetime) else None


def pending_attribution(
    fills: list[dict],
    filters: dict,
    now: datetime,
    day: str,
    open_keys: set,
    last_run: tuple[datetime, datetime | None] | None,
) -> dict:
    """Read-only view of the unresolved fills (never counted in P&L), split by WHY they may
    still be unresolved. Changes no attribution.

    ``grace``: unresolved fills that were inside the manual grace window
    (DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN, G) when the attribution was last evaluated
    (run start - fill_time < G, where ``last_run`` is the (start, finish) of the newest
    successful sync; or the fill was inserted after that run finished, so it was never
    evaluated). The grace
    window lets a strategy tag written just after the broker returned the order id still
    arrive. ``attributable_from`` = fill_time + G is the EARLIEST time the fill can be
    classified; it is classified at the first successful sync that STARTS at or after it (and
    only then if no tag / engine order appears and the coverage rule holds, so it may still
    become strategy_* or stay unresolved). ``due`` is True when that time has passed and the
    fill only waits for the next sync. ``affects_open_position``: the fill's identity has an
    open discretionary position (its quantity may be about to change).

    ``other``: unresolved fills that were already past the grace window at the last
    evaluation, so something else holds them (no coverage, a tag of unknown mode, no usable
    order id, signal_engine trades.db unreadable). ``other_today`` counts those of session
    ``day``; ``other_total`` all of them (the unresolved list is unbounded by period).
    """
    from datetime import timedelta

    grace_min = ledger_svc.manual_grace_minutes()
    grace = timedelta(minutes=grace_min)
    held: list[dict] = []
    other_today = other_total = 0
    for f in fills:
        if f["attribution_state"] != ledger.STATE_UNRESOLVED:
            continue
        if any(f.get(k) != v for k, v in filters.items()):
            continue
        when = f.get("fill_time")
        if not isinstance(when, datetime):
            other_total += 1
            continue
        evaluated = None
        if last_run is not None:
            started, finished = last_run
            synced = f.get("synced_at")
            if finished is None or not isinstance(synced, datetime) or finished >= synced:
                evaluated = started
        in_grace = grace_min > 0 and (evaluated is None or evaluated - when < grace)
        if not in_grace:
            other_total += 1
            if session_date(when).isoformat() == day:
                other_today += 1
            continue
        attributable = when + grace
        key = (f["broker"], f["account"], f["exchange"], f["symbol"], f["product"])
        held.append(
            {
                "id": f["id"],
                "broker": f["broker"],
                "account": f["account"],
                "symbol": f["symbol"],
                "exchange": f["exchange"],
                "product": f["product"],
                "side": f["side"],
                "qty": f["qty"],
                "price": f["price"],
                "fill_time": _fill_time(when),
                "attributable_from": _fill_time(attributable),
                "due": now >= attributable,
                "affects_open_position": key in open_keys,
            }
        )
    held.sort(key=lambda r: (r["attributable_from"], r["id"]))
    return {
        "grace_minutes": grace_min,
        "last_evaluated_at": _fill_time(last_run[0]) if last_run else None,
        "grace_count": len(held),
        "earliest_attributable_at": held[0]["attributable_from"] if held else None,
        "grace_fills": held,
        "other_today": other_today,
        "other_total": other_total,
    }


def today_block(
    all_trades: list[dict],
    flat_positions: list[dict],
    pos_view: list[dict],
    fills: list[dict],
    filters: dict,
    today: date,
    with_quotes: bool,
) -> dict:
    """The current trading session, from the SAME build rows as the rest of the summary.

    Read only: no second accounting path. ``all_trades`` are the closing legs of the build
    (already filtered by broker / account / symbol), ``flat_positions`` its FLAT positions,
    ``pos_view`` the priced OPEN positions. The page period is ignored.

    Session date: ``session_date(now)`` (naive IST, the previous calendar day before
    SESSION_EXPIRY_TIME, default 03:00), the same "today" as the period resolver and the
    daily table. It is not calendar-aware: on a weekend or holiday it is that date and the
    block shows no closes.

    * realized (gross, charges, net): the closing legs whose ``trade_date`` is the session,
      i.e. every exit fill of the session, partial exits included, on the day of the exit.
      It therefore equals the daily row of the session (``by_period.daily``).
    * completed positions: positions that went flat with their FINAL close in the session
      (a position opened on an earlier day and closed today counts today; its entry date is
      shown). Ratios / counts come from compute_metrics on those completed positions only
      (``leg_and_trade_metrics``); a partial exit adds money, never a completed trade.
    * open positions: every position open now (whatever day it opened). Partially closed:
      an open position with at least one closing leg (any day).
    * unrealized: the open positions marked to the price, only with quotes (else
      ``not_requested``); never part of realized or net.
    * pending attribution: see ``pending_attribution``; excluded from every figure.
    """
    day = today.isoformat()
    legs_today = [t for t in all_trades if t["trade_date"] == day]
    completed = [
        t for t in an.group_completed(all_trades, flat_positions) if t["trade_date"] == day
    ]
    metrics = leg_and_trade_metrics(legs_today, completed, day, day)
    gross = sum(float(t["realized_pnl"]) for t in legs_today)
    charges = sum(float(t["charges"]) for t in legs_today if t["charges"] is not None)
    unavailable = sum(1 for t in legs_today if t["charges"] is None)

    legs_by_pos: dict[tuple, list[dict]] = {}
    for t in all_trades:
        legs_by_pos.setdefault(_ident_seq(t), []).append(t)

    closed_rows = []
    for t in sorted(completed, key=lambda t: (t["closed_at"], t["id"]), reverse=True):
        legs = legs_by_pos.get(_ident_seq(t), [])
        closed_rows.append(
            {
                "id": t["id"],
                "build_id": t.get("build_id"),
                "broker": t["broker"],
                "account": t["account"],
                "symbol": t["symbol"],
                "exchange": t["exchange"],
                "product": t["product"],
                "direction": t["direction"],
                "total_quantity": _round(t["closed_quantity"]),
                "entry_price": _round(t["entry_price"]),
                "exit_price": _round(t["exit_price"]),
                "opened_at": t["opened_at"],
                "opened_session": _session_of(t["opened_at"]),
                "closed_at": t["closed_at"],
                "gross_pnl": _round(t["realized_pnl"]),
                "realized_today": _round(
                    sum(float(x["realized_pnl"]) for x in legs if x["trade_date"] == day)
                ),
                "charges": _round(t["charges"]),
                "charges_status": t["charges_status"],
                "net_pnl": _round(t["net_pnl"]),
                "legs": t["legs"],
                "status": "Closed",
            }
        )

    open_rows = []
    partial = 0
    unrealized = 0.0
    unpriced = 0
    statuses: dict[str, int] = {}
    for p in pos_view:
        legs = legs_by_pos.get(_ident_seq(p), [])
        closed_qty = sum(float(x["closed_quantity"]) for x in legs)
        is_partial = bool(legs)
        partial += int(is_partial)
        statuses[p["price_status"]] = statuses.get(p["price_status"], 0) + 1
        if p["unrealized"] is None:
            unpriced += 1
        else:
            unrealized += p["unrealized"]
        open_rows.append(
            {
                "id": p.get("id"),
                "broker": p["broker"],
                "account": p["account"],
                "symbol": p["symbol"],
                "exchange": p["exchange"],
                "product": p["product"],
                "direction": p["direction"],
                "open_qty": _round(p["open_qty"]),
                "closed_qty": _round(closed_qty),
                "total_quantity": _round(float(p["open_qty"]) + closed_qty),
                "avg_entry": _round(p["avg_entry"]),
                "opened_at": p["opened_at"],
                "opened_session": p.get("opened_session"),
                "realized_so_far": p.get("realized_so_far"),
                "realized_today": _round(
                    sum(float(x["realized_pnl"]) for x in legs if x["trade_date"] == day)
                ),
                "exit_legs": len(legs),
                "ltp": p.get("ltp"),
                "price_status": p["price_status"],
                "unrealized": p["unrealized"],
                "status": "Partially closed" if is_partial else "Open",
            }
        )
    open_rows.sort(key=lambda r: (str(r["opened_at"]), r["symbol"]), reverse=True)
    open_keys = {
        (p["broker"], p["account"], p["exchange"], p["symbol"], p["product"]) for p in pos_view
    }
    pending = pending_attribution(fills, filters, _now(), day, open_keys, _last_attribution_run())
    priced = len(pos_view) - unpriced
    return {
        "session_date": day,
        "session_definition": (
            "session date of the current IST time (before SESSION_EXPIRY_TIME it is the "
            "previous day); realized counts every exit fill of the session, partial exits "
            "included; completed positions are those whose final close is in the session"
        ),
        "realized_gross": _round(gross),
        "charges": _round(charges),
        "charges_unavailable_legs": unavailable,
        "charges_complete": unavailable == 0,
        "net": _round(gross - charges),
        "realized_net_of_charges": _round(gross - charges),
        "exit_legs": len(legs_today),
        "unrealized": _round(unrealized) if (with_quotes and priced) else None,
        "unrealized_unavailable_positions": unpriced if with_quotes else 0,
        "price_statuses": statuses,
        "quotes_requested": bool(with_quotes),
        "completed_positions": len(completed),
        "open_positions": len(pos_view),
        "partially_closed_positions": partial,
        "metrics": {name: metrics.get(name) for name in TODAY_METRICS},
        "closed_positions": closed_rows,
        "open_position_rows": open_rows,
        "pending_attribution": pending,
    }


# ---------------------------------------------------------------- sample size / data quality

SAMPLE_MIN_TRADES = metrics_svc.EDGE_MIN_TRADES
SAMPLE_MIN_DAYS = metrics_svc.EDGE_MIN_DAYS


def sample_context(trades: int, days: int) -> dict:
    """Sample-size context of a set of completed trades, by the EXISTING edge-check rule
    (``performance_stats.edge_stats``: at least EDGE_MIN_TRADES trades over EDGE_MIN_DAYS
    trading days). ``days`` is the number of distinct session dates of the trades' final
    closes. Nothing is withheld by it here: the page shows the ratios with this context, and
    hides a per-row profit factor when ``sufficient`` is False."""
    return {
        "completed_trades": int(trades),
        "trading_days": int(days),
        "min_trades": SAMPLE_MIN_TRADES,
        "min_days": SAMPLE_MIN_DAYS,
        "sufficient": trades >= SAMPLE_MIN_TRADES and days >= SAMPLE_MIN_DAYS,
        "rule": (
            f"at least {SAMPLE_MIN_TRADES} completed trades over {SAMPLE_MIN_DAYS} trading days "
            "(the edge-check sample rule)"
        ),
    }


def _with_sample(rows: list[dict]) -> list[dict]:
    for row in rows:
        row["sample_ok"] = sample_context(row["trades"], row.get("trading_days", 0))["sufficient"]
    return rows


STATE_COLLECTED = "collected"


def data_quality(
    snap: dict,
    filters: dict,
    start: str,
    end: str,
    legs: list[dict],
    trades: list[dict],
    pos_view: list[dict],
    with_quotes: bool,
    sync_health: dict,
    sync_warnings: list[str],
) -> dict:
    """Read-only data-quality panel for the period and filters. Changes no figure.

    Scope: the discretionary CANDIDATE fills of ``execution_counts`` (manual, ui_manual or
    unresolved; session date of fill_time in [start, end]; broker / account / symbol
    filters). Each is in exactly ONE state, by what it is NOW:

    * ``awaiting_attribution`` (2): unresolved; split into ``awaiting_grace`` (held by the
      manual grace window, ``pending_attribution``) and ``awaiting_other``.
    * ``attributed_open`` (3): manual with at least one link to a position still OPEN (its
      open remainder, or a leg of a partially closed position). A reversing fill that closed
      one position and opened the next counts here.
    * ``included`` (4): manual, every link belongs to a completed trade (a FLAT position).
    * ``not_placed`` (5): manual but refused by the engine (orphan / invalid, no link).

    ``collected`` (1) == 2 + 3 + 4 + 5 always (``reconciles``). Also: closing legs / completed
    trades of the period without a charge estimate, the open-position price states (only when
    quotes were requested), and the sync status (``sync_health`` plus its problem lines). No
    reconciliation figure is shown: the summary has none.
    """
    scope = []
    for f in snap["fills"]:
        state = f["attribution_state"]
        if state not in DISCRETIONARY_STATES and state != ledger.STATE_UNRESOLVED:
            continue
        if any(f.get(k) != v for k, v in filters.items()) or f.get("fill_time") is None:
            continue
        if start <= session_date(f["fill_time"]).isoformat() <= end:
            scope.append(f)
    unresolved = [f for f in scope if f["attribution_state"] == ledger.STATE_UNRESOLVED]
    pending = pending_attribution(unresolved, {}, _now(), end, set(), _last_attribution_run())

    leg_pos = {t["id"]: _ident_seq(t) for t in snap.get("trades") or []}
    flat = {_ident_seq(p) for p in snap.get("flat_positions") or []}
    by_fill: dict[int, list[dict]] = {}
    for link in snap.get("links") or []:
        by_fill.setdefault(link["fill_ref"], []).append(link)
    attributed_open = included = not_placed = 0
    for f in scope:
        if f["attribution_state"] == ledger.STATE_UNRESOLVED:
            continue
        mine = by_fill.get(f["id"], [])
        if not mine:
            not_placed += 1
        elif any(
            lk.get("position_id") is not None or leg_pos.get(lk.get("trade_id")) not in flat
            for lk in mine
        ):
            attributed_open += 1
        else:
            included += 1
    collected = len(scope)

    statuses: dict[str, int] = {}
    for p in pos_view:
        statuses[p["price_status"]] = statuses.get(p["price_status"], 0) + 1
    prices = {
        "requested": bool(with_quotes),
        "open_positions": len(pos_view),
        "statuses": statuses if with_quotes else {},
        "missing": sum(1 for p in pos_view if p["unrealized"] is None) if with_quotes else 0,
        "stale": (
            sum(1 for p in pos_view if p["price_status"] in ("stale", "last_close", "cached"))
            if with_quotes
            else 0
        ),
    }
    return {
        "scope": (
            "manual and unresolved ledger fills whose session date is in the period, with the "
            "filters; each in one state by what it is now"
        ),
        STATE_COLLECTED: collected,
        "awaiting_attribution": len(unresolved),
        "awaiting_grace": pending["grace_count"],
        "awaiting_other": pending["other_total"],
        "grace_minutes": pending["grace_minutes"],
        "earliest_attributable_at": pending["earliest_attributable_at"],
        "attributed_open": attributed_open,
        "included": included,
        "not_placed": not_placed,
        "reconciles": collected == len(unresolved) + attributed_open + included + not_placed,
        "charges_incomplete_legs": sum(1 for t in legs if t["charges"] is None),
        "charges_incomplete_trades": sum(1 for t in trades if t["charges"] is None),
        "prices": prices,
        "sync": {
            "last_successful_sync": (sync_health or {}).get("last_successful_sync"),
            "days_without_sync": list((sync_health or {}).get("days_without_sync") or []),
            "problems": [w for w in sync_warnings if not w.startswith("last successful sync:")],
        },
    }


# ---------------------------------------------------------------- provisional (unresolved)


def _iso_time(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value


def _evidence_counts(fills: list[dict]) -> dict:
    """{evidence label: fills} of ``fills`` (no evidence -> "no source evidence")."""
    out: dict[str, int] = {}
    for f in fills:
        label = _source_fields(f)["source_evidence"] or "no source evidence"
        out[label] = out.get(label, 0) + 1
    return dict(sorted(out.items()))


def provisional_block(
    fills: list[dict], filters: dict, book_open_idents: set | frozenset | None = None
) -> dict:
    """What the UNRESOLVED fills would show if they were yours. Read only and in memory.

    The unresolved ledger fills matching the broker / account / symbol filters (all periods:
    an opening may predate the page period, and the unresolved list is unbounded too) go
    through the SAME engine as the book: ``reconstruct`` (per broker / account / exchange /
    symbol / product identity, accounts never merge), ``allocate_charges`` (estimated, per
    order) and ``group_completed`` / ``summary_stats``. Nothing is written to the book or the
    ledger, no attribution changes, and nothing here enters any total, metric, breakdown,
    daily row, equity curve or the today block. Ids are local to this block (no build id):
    the fills drill-down does not take them; each trade carries its fills inline instead.

    ``opening_unknown`` marks a trade or open position of an identity where the engine had
    to refuse a fill (an exit with no known opening); ``orphans`` lists those fills.
    ``book_open_idents`` is the set of (broker, account, exchange, symbol, product) with an
    open position in the BOOK; an orphan of such an identity gets ``book_position_open``
    True (its opening is probably that book position). Display only, no figure changes.
    """
    book_open = set(book_open_idents or ())
    unresolved = [
        f
        for f in fills
        if f["attribution_state"] == ledger.STATE_UNRESOLVED
        and not any(f.get(k) != v for k, v in filters.items())
        and isinstance(f.get("fill_time"), datetime)  # the engine sorts by fill_time
    ]
    by_id = {f["id"]: f for f in unresolved}
    result = reconstruct(unresolved)
    allocate_charges(result, by_id)
    legs = []
    for i, t in enumerate(result["trades"]):
        leg = dict(t)
        leg.update(
            id=i + 1,
            build_id=None,
            opened_at=_iso_time(t["opened_at"]),
            closed_at=_iso_time(t["closed_at"]),
        )
        legs.append(leg)
    positions = []
    for i, p in enumerate(result["positions"]):
        pos = dict(p)
        pos.update(id=i + 1, build_id=None, opened_at=_iso_time(p["opened_at"]))
        positions.append(pos)
    pos_of = {_ident_seq(p): p for p in positions}
    links_by_trade: dict[int, list[dict]] = {}
    for link in result["links"]:
        if "trade_idx" in link:
            links_by_trade.setdefault(link["trade_idx"], []).append(link)

    completed = an.group_completed(legs, positions)
    rows = []
    for t in sorted(completed, key=lambda t: (t["closed_at"], t["id"]), reverse=True):
        merged: dict[tuple, dict] = {}
        for leg_id in t["leg_ids"]:
            for link in links_by_trade.get(leg_id - 1, []):
                key = (link["fill_ref"], link["role"])
                row = merged.setdefault(
                    key,
                    {
                        "fill_ref": link["fill_ref"],
                        "role": link["role"],
                        "qty_used": 0.0,
                        "charge_allocated": 0.0,
                    },
                )
                row["qty_used"] += link["qty_used"]
                if row["charge_allocated"] is None or link.get("charge_allocated") is None:
                    row["charge_allocated"] = None
                else:
                    row["charge_allocated"] += link["charge_allocated"]
        trade_fills = []
        for row in merged.values():
            f = by_id[row["fill_ref"]]
            trade_fills.append(
                {
                    "fill_ref": row["fill_ref"],
                    "role": row["role"],
                    "side": f["side"],
                    "qty_used": _round(row["qty_used"]),
                    "price": f["price"],
                    "fill_time": _iso_time(f["fill_time"]),
                    # raw, like get_trade_fills: the page rounds once
                    "charge_allocated": row["charge_allocated"],
                    **_source_fields(f),
                }
            )
        trade_fills.sort(
            key=lambda x: (_ROLE_ORDER.get(x["role"], 9), x["fill_time"] or "", x["fill_ref"])
        )
        pos = pos_of.get(_ident_seq(t)) or {}
        rows.append(
            {
                "id": t["id"],
                "provisional": True,
                "broker": t["broker"],
                "account": t["account"],
                "symbol": t["symbol"],
                "exchange": t["exchange"],
                "product": t["product"],
                "direction": t["direction"],
                "closed_quantity": _round(t["closed_quantity"]),
                "entry_price": _round(t["entry_price"]),
                "exit_price": _round(t["exit_price"]),
                "opened_at": t["opened_at"],
                "closed_at": t["closed_at"],
                "trade_date": t["trade_date"],
                "overnight": t["overnight"],
                "hold_minutes": t["hold_minutes"],
                "realized_pnl": _round(t["realized_pnl"]),
                "charges": _round(t["charges"]),
                "charges_status": t["charges_status"],
                "net_pnl": _round(t["net_pnl"]),
                "legs": t["legs"],
                "opening_unknown": bool(pos.get("opening_unknown")),
                "fills": trade_fills,
            }
        )
    open_rows = [
        {
            "broker": p["broker"],
            "account": p["account"],
            "symbol": p["symbol"],
            "exchange": p["exchange"],
            "product": p["product"],
            "direction": p["direction"],
            "open_qty": _round(p["open_qty"]),
            "avg_entry": _round(p["avg_entry"]),
            "opened_at": p["opened_at"],
            "realized_so_far": _round(p["realized_gross"]),
            "opening_unknown": bool(p["opening_unknown"]),
        }
        for p in positions
        if p["status"] == "OPEN"
    ]
    orphans = [
        {
            **{k: v for k, v in o.items() if k != "fill_time"},
            "fill_time": _iso_time(o["fill_time"]),
            "book_position_open": (
                o["broker"],
                o["account"],
                o["exchange"],
                o["symbol"],
                o["product"],
            )
            in book_open,
        }
        for o in result["orphans"]
    ]
    stats = an.summary_stats(completed, legs)
    charges_missing = sum(1 for t in completed if t["charges"] is None)
    return {
        "definition": (
            "unresolved fills replayed with the same engine as if they were manual; estimated "
            "charges; excluded from every total until classified"
        ),
        "fills": len(unresolved),
        # every fill here is unresolved: source UNCLASSIFIED (see _source_fields)
        "source_label": ledger_svc.UNCLASSIFIED,
        "source_evidence": _evidence_counts(unresolved),
        "trades": rows,
        "open_positions": open_rows,
        "orphans": orphans,
        "metrics": {
            "trades": stats["trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "expectancy": stats["expectancy"],
            "avg_win": stats["avg_win"],
            "avg_loss": stats["avg_loss"],
            "realized_gross": stats["realized_gross"],
            "charges": stats["charges"],
            "net_of_charges": stats["net_of_charges"],
            "charges_unavailable_trades": stats["charges_unavailable_trades"],
            "net_charges_complete": stats["net_charges_complete"],
            "charges_missing_completed": charges_missing,
        },
        "sample": sample_context(len(completed), len({t["trade_date"] for t in completed})),
    }


# ---------------------------------------------------------------- performance page (Stage 3a)

TABLE_LIMIT = 500
_ROLE_ORDER = {"ENTRY": 0, "EXIT": 1}

LABELS = {
    "realized_gross": "Realized P&L before charges (gross). metrics.net_profit is the same gross figure.",
    "charges": "Estimated charges (brokerage, taxes, exchange fees); not a broker statement.",
    "realized_net_of_charges": (
        "realized_gross minus estimated charges. A trade without a charge estimate counts as 0 "
        "charges, so the figure is complete only when charges_unavailable_trades is 0."
    ),
    "net": "Same as realized_net_of_charges.",
    "unrealized_total": "Open positions marked to the last price; only with quotes=1.",
    "buckets": (
        "Daily, weekly (ISO, Monday start) and monthly buckets use the session date of the close: "
        "money on the date of each closing leg (a partial exit counts on its day), trade counts "
        "on the date of the final close."
    ),
    "trades": (
        "A completed trade is one position from flat to flat (a reversal ends it); partial "
        "exits are legs of it. Open positions are not trades."
    ),
    "ratios": (
        "Win rate, profit factor, expectancy and average win / loss are computed per set of "
        "completed trades on NET P&L after estimated charges (a win is net > 0), never summed. "
        "A trade with no charge estimate has no net: it is left out and counted."
    ),
    "today": (
        "The current session (session date of now, IST; before SESSION_EXPIRY_TIME the previous "
        "day). Realized counts every exit of the session, partial exits included; completed "
        "positions are those whose final close is in the session; open positions are open now. "
        "Unresolved fills, including those inside the manual grace window, are excluded."
    ),
    "sample": (
        "Ratios come with the number of completed trades and trading days behind them; below "
        "the edge-check rule (30 completed trades over 10 trading days) they are a low sample. "
        "A breakdown row shows its profit factor only when it meets that rule."
    ),
    "consistency": (
        "Largest win / loss and streaks are net of estimated charges; daily figures and this "
        "max drawdown are gross realized (before charges). Streaks run over completed trades "
        "in close order; a break-even trade ends a streak. Average daily P&L and max drawdown "
        "need at least 2 trading days."
    ),
    "risk": (
        "Drawdown is the net realised equity (after estimated charges) by position close time, "
        "peak starting at 0, in rupees; current drawdown is peak minus latest equity. No "
        "percentage: there is no capital basis."
    ),
    "points": (
        "Points are per unit of one instrument (symbol + exchange), long: exit - entry, short: "
        "entry - exit, never summed across instruments. A winner is net P&L > 0. Rupee net P&L "
        "is the authoritative result."
    ),
    "category": (
        "intraday = opened and closed in the same exchange session; swing = still open after the "
        "opening session ended; unclassified (reason shown) is in 'all' only. Headline money "
        "follows the closing legs by their own close date, position tables the whole position by "
        "final close date; see reconciliation."
    ),
    "excursions": (
        "MAE / MFE: NSE equity only, from one-minute bars along the real fills; a minute-bar "
        "lower bound, not tick accurate; available after market close once price data is complete."
    ),
    "breakdowns": (
        "Time of day and weekday use the ENTRY time (IST) of the position; holding time the "
        "trade's own hold. Buckets: 09:15-10:00, 10:00-11:30, 11:30-13:30, 13:30-15:00, "
        "15:00-15:30 (start included, end excluded), other outside them."
    ),
    "data_quality": (
        "Candidate executions of the period in one state each: awaiting attribution, "
        "attributed but in an open position, included in a completed trade, or not placed. "
        "collected = the sum of the four."
    ),
    "provisional": (
        "Unresolved executions replayed as if they were manual, with estimated charges. They "
        "are not in any total, metric, breakdown, chart or the today block."
    ),
    "source_labels": (
        "Source identification of every ledger fill of the period: SIGNAL_LIVE (an OpenAlgo "
        "strategy order: order tag, remarks tag or engine order), MANUAL (manual or OpenAlgo UI) "
        "or UNCLASSIFIED (unresolved, excluded from P&L). Display only; accounting ignores it."
    ),
    "executions": (
        "executions_collected counts the ledger fills of the period and filters that are manual "
        "or unresolved (strategy fills excluded); executions_unresolved is the unresolved part, "
        "awaiting attribution and excluded from P&L."
    ),
}


def _fill_time(value) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value


class BuildChanged(Exception):
    """The caller's build_id is no longer the current build (trade ids are per build)."""

    def __init__(self, current_build_id: str):
        super().__init__("discretionary build changed")
        self.current_build_id = current_build_id


def get_trade_fills(trade_id: int, build_id: str | None = None) -> dict | None:
    """The ledger fills behind one COMPLETED trade (position flat to flat) of the CURRENT
    build, or None when it does not exist in it (an open position is not a trade). Ids are
    reassigned by every rebuild, so when ``build_id`` is given and is not the current build
    this raises BuildChanged rather than return a different trade that reuses the id.

    Every leg's links are returned, merged per (fill, role): an entry fill consumed pro rata
    by several legs is ONE row with the summed ``qty_used`` / ``charge_allocated``; a
    reversing fill shows only its closing (EXIT) part. Entry fills first, then exits, each
    oldest first. ``linked_fills`` counts the link rows before the merge. Reads only; the
    ledger rows are returned as stored (no raw hash, no tokens: the ledger holds none)."""
    snap = _snapshot({}, trade_id=trade_id)
    if build_id is not None and snap["build_id"] != build_id:
        raise BuildChanged(snap["build_id"])
    trade = snap["trade"]
    if trade is None:
        return None
    by_id = {f["id"]: f for f in snap["fills"]}
    merged: dict[tuple, dict] = {}
    for link in snap["trade_links"]:
        key = (link["fill_ref"], link["role"])
        row = merged.get(key)
        if row is None:
            merged[key] = dict(link)
            continue
        row["qty_used"] += link["qty_used"]
        if row["charge_allocated"] is None or link["charge_allocated"] is None:
            row["charge_allocated"] = None
        else:
            row["charge_allocated"] += link["charge_allocated"]
    fills = []
    for link in merged.values():
        f = by_id.get(link["fill_ref"])
        if f is None:
            continue
        fills.append(
            {
                "fill_ref": link["fill_ref"],
                "role": link["role"],
                "qty_used": link["qty_used"],
                "charge_allocated": link["charge_allocated"],
                "price": f["price"],
                "fill_time": _fill_time(f["fill_time"]),
                "order_id": f["order_id"],
                "fill_id": f["fill_id"],
                "broker": f["broker"],
                "account": f["account"],
                "symbol": f["symbol"],
                "exchange": f["exchange"],
                "product": f["product"],
                "side": f["side"],
                "attribution_state": f["attribution_state"],
                **_source_fields(f),
            }
        )
    fills.sort(key=lambda x: (_ROLE_ORDER.get(x["role"], 9), x["fill_time"] or "", x["fill_ref"]))
    return {
        "build_id": snap["build_id"],
        "trade": trade,
        "fills": fills,
        "linked_fills": len(snap["trade_links"]),
    }


def get_performance(
    period: str = "30d",
    start: str | None = None,
    end: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    account: str | None = None,
    with_quotes: bool = False,
    category: str = "all",
) -> dict:
    """Everything the Discretionary Performance page needs from ONE snapshot (one build_id).

    ``category`` (all | intraday | swing, else ValueError) filters the closed positions, their
    closing legs and the open positions by position key; every figure, table and the headline
    then describe that category (see ``_scope``). Additive blocks: ``points_by_instrument``,
    ``risk`` (net-realised drawdown, streaks), ``monthly`` / ``weekday`` (by position close),
    ``trade_management``, ``costs``, ``classification``, ``unresolved_in_range``,
    ``reconciliation``; the closed-position rows carry ``category`` and ``points_per_unit``.
    Win / loss figures are on the NET basis. MAE / MFE are NOT here (lazy ``get_excursions``).

    ``get_summary`` plus: by_period {daily, weekly, monthly}, by_symbol, by_side, by_product,
    by_holding {intraday, overnight}, hold, trades (COMPLETED trades, each with ``legs``) /
    positions (newest first, at most TABLE_LIMIT each, with ``*_total`` and ``*_truncated``;
    every total is computed before the cut), withheld and labels. Breakdown money sums the
    legs, breakdown counts the completed trades (see the module docstring). The
    broker / account / symbol filters apply to every figure, table and chart; pooled ratios
    are computed on the filtered set. ``with_quotes`` False (default) makes no broker call.
    Raises ValueError for a bad period or custom range.
    """
    check_category(category)
    filters = _clean_filters(broker, account, symbol)
    snap = _snapshot(filters, trades=True, positions=True, orphans=True, links=True)
    parts = _summary_parts(snap, period, start, end, filters, with_quotes, category)
    out = dict(parts["summary"])
    trades, legs = parts["trades"], parts["legs"]  # completed trades / closing legs
    out["by_period"] = {g: an.period_breakdown(trades, g, legs) for g in an.GRANULARITIES}
    out["by_symbol"] = an.group_breakdown(trades, "symbol", legs)
    out["by_side"] = an.group_breakdown(trades, "direction", legs)
    out["by_product"] = an.group_breakdown(trades, "product", legs)
    out["by_holding"] = an.holding_breakdown(trades, legs)
    out["hold"] = an.hold_summary(trades)
    # "How you trade": the same row builder over entry time of day, entry weekday and
    # holding time; sample_ok says whether the row's profit factor may be shown
    out["breakdowns"] = {
        name: _with_sample(rows) for name, rows in an.trading_breakdowns(trades, legs).items()
    }
    out["consistency"] = an.consistency(
        trades, out["by_period"]["daily"], out["hold"], out["metrics"].get("max_drawdown")
    )

    # Additive blocks; every one is computed from ALL closed positions of the scope (the
    # table limit below only cuts what is DISPLAYED).
    out["points_by_instrument"] = _safe_block(
        "points_by_instrument", lambda: an.points_by_instrument(trades), out["warnings"]
    )
    out["risk"] = _safe_block("risk", lambda: an.risk_block(trades), out["warnings"])
    out["monthly"] = _safe_block("monthly", lambda: an.monthly_table(trades), out["warnings"])
    out["weekday"] = _safe_block("weekday", lambda: an.weekday_table(trades), out["warnings"])
    out["trade_management"] = _safe_block(
        "trade_management", lambda: an.trade_management(trades), out["warnings"]
    )
    out["costs"] = _safe_block("costs", lambda: an.costs_summary(trades), out["warnings"])

    newest = sorted(trades, key=lambda t: (t["closed_at"], t["id"]), reverse=True)
    out["trades"] = newest[:TABLE_LIMIT]
    out["trades_total"] = len(newest)
    out["trades_shown"] = len(out["trades"])
    out["trades_truncated"] = max(0, len(newest) - TABLE_LIMIT)
    positions = parts["positions"]
    out["positions"] = positions[:TABLE_LIMIT]
    out["positions_total"] = len(positions)
    out["positions_truncated"] = max(0, len(positions) - TABLE_LIMIT)
    out["withheld"] = dict(out["metrics"].get("withheld") or {})
    out["labels"] = dict(LABELS)
    out["quotes_requested"] = bool(with_quotes)
    return out


# ---------------------------------------------------------------- MAE / MFE (lazy)

# Positions whose bars are read per request (the most recent ones); the rest of a larger scope
# is reported as ``not_computed_cap``, never estimated. Each read is one short Historify
# SELECT; every read of one computation shares ONE DuckDB connection (``_shared_connection``),
# closed when the computation ends, on every path.
EXCURSION_MAX_POSITIONS = 300
_excursion_cache = LockedTTLCache(maxsize=5000, ttl=6 * 3600)


_BARS_SQL = (
    "SELECT timestamp, open, high, low, close, volume, oi FROM market_data "
    "WHERE symbol = ? AND exchange = ? AND interval = '1m' "
    "AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC"
)


class _LazyConnection:
    """One Historify connection per computation, opened on the first read (a computation
    that never reads, e.g. all cached, opens none), closed by ``close``. A failed open is
    remembered and re-raised, so a broken database is not retried (with back-off sleeps) for
    every position."""

    def __init__(self):
        self._stack = contextlib.ExitStack()
        self._conn = None
        self._error: Exception | None = None

    def get(self):
        if self._error is not None:
            raise self._error
        if self._conn is None:
            from database.historify_db import get_connection

            try:
                self._conn = self._stack.enter_context(get_connection())
            except Exception as exc:
                self._error = exc
                raise
        return self._conn

    def close(self) -> None:
        self._stack.close()


_active_connection: contextvars.ContextVar[_LazyConnection | None] = contextvars.ContextVar(
    "discretionary_excursion_connection", default=None
)


@contextlib.contextmanager
def _shared_connection():
    """All ``_read_bars`` calls inside the block use one lazily opened connection, closed in
    the ``finally`` (also when a read raises)."""
    lazy = _LazyConnection()
    token = _active_connection.set(lazy)
    try:
        yield lazy
    finally:
        _active_connection.reset(token)
        lazy.close()


def _read_bars(symbol: str, exchange: str, start_ts: int, end_ts: int):
    """One-minute Historify bars (epoch seconds, inclusive) as a DataFrame with ``timestamp``
    / ``high`` / ``low`` columns. Read only; tests replace this.

    Runs the exact-window parameterised SELECT through ``get_connection`` so that a real
    read error RAISES (``database.historify_db.get_ohlcv`` swallows errors and returns an
    empty frame, which would be reported as missing data). Uses the shared connection of
    the running computation, or opens (and closes) its own."""
    params = [symbol.upper(), exchange.upper(), int(start_ts), int(end_ts)]
    lazy = _active_connection.get()
    if lazy is not None:
        return lazy.get().execute(_BARS_SQL, params).fetchdf()
    from database.historify_db import get_connection

    with get_connection() as conn:
        return conn.execute(_BARS_SQL, params).fetchdf()


def _bars_by_minute(frame) -> dict[datetime, tuple[float, float]]:
    """{naive IST minute: (high, low)} of a bar frame; empty for None / no rows."""
    if frame is None or len(frame) == 0:
        return {}
    out = {}
    for ts, high, low in zip(
        frame["timestamp"].tolist(), frame["high"].tolist(), frame["low"].tolist(), strict=True
    ):
        moment = datetime.fromtimestamp(int(ts), tz=_IST).replace(tzinfo=None)
        out[moment.replace(second=0, microsecond=0)] = (high, low)
    return out


def _epoch(moment: datetime) -> int:
    return int(moment.replace(tzinfo=_IST).timestamp())


def _position_fills(snap: dict, trade: dict, links_by_leg: dict, fills_by_id: dict) -> list[dict]:
    """The fills of one completed position in processing order (an.excursion fill dicts).

    Every leg's links are merged per (fill, role) (an entry consumed pro rata by several
    legs is one fill with the summed quantity; a reversing fill contributes only its closing
    part), priced and timed from the ledger fill, ordered by the engine's own rule
    (fill_time, order_id, fill_id, id)."""
    merged: dict[tuple, float] = {}
    for leg_id in trade["leg_ids"]:
        for link in links_by_leg.get(leg_id, []):
            key = (link["fill_ref"], link["role"])
            merged[key] = merged.get(key, 0.0) + float(link["qty_used"])
    rows = []
    for (ref, role), qty in merged.items():
        f = fills_by_id.get(ref)
        if f is None:
            return []
        rows.append(
            {
                "time": f["fill_time"],
                "role": role,
                "qty": qty,
                "price": f["price"],
                "_sort": (
                    f["fill_time"],
                    str(f.get("order_id") or ""),
                    str(f.get("fill_id") or ""),
                    f["id"],
                    role,
                ),
            }
        )
    rows.sort(key=lambda r: r["_sort"])
    for r in rows:
        del r["_sort"]
    return rows


def position_excursion_result(
    trade: dict, fills: list[dict], provider, now: datetime, reader=None
) -> dict:
    """MAE / MFE of one completed position (see ``an.position_excursion``) with the checks
    that need the clock and Historify:

    * unsupported segment (anything but NSE equity) => unavailable, no read;
    * closed today and the exchange session has not ended yet => ``after_market_close``;
    * the fills are validated BEFORE any read (``an.position_excursion`` with no bars);
    * bars are read for [first fill minute, last fill minute] only; a missing minute makes
      it unavailable (``missing_bars``, or ``historify_incomplete`` when the position closed
      today: the day's data is still being processed). Nothing is estimated.
    A past day's available result is cached (its bars are final); unavailable ones are not.
    """
    reader = reader or _read_bars
    if not an.excursion_segment_supported(trade["exchange"], trade["symbol"]):
        return an.excursion_unavailable("unsupported_segment")
    if not fills:
        return an.excursion_unavailable("no_fills")
    closed = an.parse_time(trade["closed_at"])
    if closed is None:
        return an.excursion_unavailable("fill_times_unreliable")
    if closed.date() >= now.date():
        try:
            window = provider(closed.date(), trade["exchange"])
        except an.SessionCalendarUnavailable:
            return an.excursion_unavailable("session_calendar_unavailable")
        if window is not None and now < window.end:
            return an.excursion_unavailable("after_market_close")
    pre = an.position_excursion(trade["direction"], fills, {}, trade["exchange"], provider)
    if pre["reason"] != "missing_bars":
        return pre  # unavailable for a reason that needs no bars (or cannot be, with none)
    key = (
        trade["symbol"],
        trade["exchange"],
        trade["direction"],
        tuple((f["time"].isoformat(), f["role"], f["qty"], f["price"]) for f in fills),
    )
    cached = _excursion_cache.get(key)
    if cached is not None:
        return dict(cached)
    first = fills[0]["time"].replace(second=0, microsecond=0)
    last = fills[-1]["time"].replace(second=0, microsecond=0)
    try:
        bars = _bars_by_minute(
            reader(trade["symbol"], trade["exchange"], _epoch(first), _epoch(last))
        )
    except Exception as e:
        logger.warning("Discretionary excursion bar read failed: %s", type(e).__name__)
        return an.excursion_unavailable("bar_read_failed")
    result = an.position_excursion(trade["direction"], fills, bars, trade["exchange"], provider)
    if result["status"] != "available":
        if result["reason"] == "missing_bars" and closed.date() >= now.date():
            return an.excursion_unavailable("historify_incomplete")
        return result
    if closed.date() < now.date():
        _excursion_cache[key] = dict(result)
    return result


def excursion_rows(snap: dict, scope: dict, reader=None) -> tuple[list[dict], int]:
    """(rows, computed): one row per closed position of the scope, newest close first, with
    its excursion status. At most ``EXCURSION_MAX_POSITIONS`` NSE-equity positions are
    read (the newest); the others are ``not_computed_cap``. ``computed`` counts the reads."""
    provider = session_provider()
    links_by_leg: dict[int, list[dict]] = {}
    for link in snap.get("links") or []:
        if link.get("trade_id") is not None:
            links_by_leg.setdefault(link["trade_id"], []).append(link)
    fills_by_id = {f["id"]: f for f in snap["fills"]}
    with _shared_connection():
        return _excursion_rows_in(snap, scope, reader, provider, links_by_leg, fills_by_id)


def _excursion_rows_in(snap, scope, reader, provider, links_by_leg, fills_by_id):
    rows, reads = [], 0
    for t in sorted(scope["trades"], key=lambda t: (t["closed_at"], t["id"]), reverse=True):
        supported = an.excursion_segment_supported(t["exchange"], t["symbol"])
        if supported and reads >= EXCURSION_MAX_POSITIONS:
            result = an.excursion_unavailable("not_computed_cap")
        else:
            fills = _position_fills(snap, t, links_by_leg, fills_by_id) if supported else []
            reads += 1 if supported else 0
            result = position_excursion_result(t, fills, provider, scope["now"], reader)
        net = t["net_pnl"]
        rows.append(
            {
                "id": t["id"],
                "symbol": t["symbol"],
                "exchange": t["exchange"],
                "direction": t["direction"],
                "category": t["category"],
                "opened_at": t["opened_at"],
                "closed_at": t["closed_at"],
                "closed_quantity": t["closed_quantity"],
                "points_per_unit": _round(t["points_per_unit"]),
                "net_pnl": _round(net),
                **{
                    k: result[k]
                    for k in (
                        "status",
                        "reason",
                        "reason_text",
                        "mae_points",
                        "mfe_points",
                        "mae_amount",
                        "mfe_amount",
                        "minute_bar_precision",
                    )
                },
                "profit_capture": _round(
                    an.profit_capture(net, t["points_per_unit"], result["mfe_points"]), 4
                ),
            }
        )
    return rows, reads


def get_excursions(
    period: str = "30d",
    start: str | None = None,
    end: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    account: str | None = None,
    category: str = "all",
    reader=None,
) -> dict:
    """Lazy MAE / MFE of the closed positions of a scope (same filters as the summary, incl.
    ``category``). Kept OUT of the summary so that stays fast. Per position: ``status``
    available / unavailable with a ``reason`` code; plus ``aggregates`` (average / median
    MAE and MFE of winners and losers, profit capture of winners, sample counts, ``N of M
    positions available``), ``note`` and the cap. Raises ValueError for a bad period or
    category. Read only: Historify SELECTs, no broker call."""
    check_category(category)
    filters = _clean_filters(broker, account, symbol)
    snap = _snapshot(filters, trades=True, positions=True, orphans=True, links=True)
    scope = _scope(snap, period, start, end, category)
    rows, computed = excursion_rows(snap, scope, reader)
    return {
        "build_id": snap["build_id"],
        "period": {"period": period, "start": scope["s"], "end": scope["e"]},
        "category": category,
        "positions": rows,
        "positions_total": len(rows),
        "computed": computed,
        "cap": EXCURSION_MAX_POSITIONS,
        "aggregates": an.excursion_aggregates(rows),
        "note": an.EXCURSION_NOTE,
    }


EXPORT_COLUMNS = (
    "trade_date",
    "symbol",
    "exchange",
    "category",
    "category_reason",
    "direction",
    "product",
    "broker",
    "opened_at",
    "closed_at",
    "holding_minutes",
    "closed_quantity",
    "avg_entry_price",
    "avg_exit_price",
    "points_per_unit",
    "gross_pnl",
    "charges",
    "net_pnl",
    "mae_points",
    "mfe_points",
    "mae_amount",
    "mfe_amount",
    "profit_capture",
    "excursion_status",
    "excursion_reason",
)
EXPORT_NUMERIC = frozenset(
    {
        "holding_minutes",
        "closed_quantity",
        "avg_entry_price",
        "avg_exit_price",
        "points_per_unit",
        "gross_pnl",
        "charges",
        "net_pnl",
        "mae_points",
        "mfe_points",
        "mae_amount",
        "mfe_amount",
        "profit_capture",
    }
)


def get_export(
    period: str = "30d",
    start: str | None = None,
    end: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    account: str | None = None,
    category: str = "all",
    reader=None,
) -> dict:
    """{"start", "end", "category", "columns", "rows"}: EVERY closed position of the scope
    (the summary's table limit does not apply; ``len(rows)`` equals the summary's closed
    positions of the same filters), newest close first. MAE / MFE come from the same
    computation as ``get_excursions`` (``excursion_rows``); a figure that is unavailable is
    None (an empty CSV cell), never 0. Unresolved and orphan fills are in no row."""
    check_category(category)
    filters = _clean_filters(broker, account, symbol)
    snap = _snapshot(filters, trades=True, positions=True, orphans=True, links=True)
    scope = _scope(snap, period, start, end, category)
    excursions, _computed = excursion_rows(snap, scope, reader)
    by_id = {r["id"]: r for r in excursions}
    rows = []
    for t in sorted(scope["trades"], key=lambda t: (t["closed_at"], t["id"]), reverse=True):
        ex = by_id[t["id"]]
        rows.append(
            {
                "trade_date": t["trade_date"],
                "symbol": t["symbol"],
                "exchange": t["exchange"],
                "category": t["category"],
                "category_reason": t["category_reason"],
                "direction": t["direction"],
                "product": t["product"],
                "broker": t["broker"],
                "opened_at": _iso_ist(t["opened_at"]),
                "closed_at": _iso_ist(t["closed_at"]),
                "holding_minutes": t["hold_minutes"],
                "closed_quantity": t["closed_quantity"],
                "avg_entry_price": t["entry_price"],
                "avg_exit_price": t["exit_price"],
                "points_per_unit": _round(t["points_per_unit"]),
                "gross_pnl": _round(t["realized_pnl"]),
                "charges": _round(t["charges"]),
                "net_pnl": _round(t["net_pnl"]),
                "mae_points": ex["mae_points"],
                "mfe_points": ex["mfe_points"],
                "mae_amount": ex["mae_amount"],
                "mfe_amount": ex["mfe_amount"],
                "profit_capture": ex["profit_capture"],
                "excursion_status": ex["status"],
                "excursion_reason": ex["reason"],
            }
        )
    return {
        "start": scope["s"],
        "end": scope["e"],
        "category": category,
        "columns": list(EXPORT_COLUMNS),
        "rows": rows,
    }


def _iso_ist(value) -> str | None:
    """ISO 8601 with the IST offset of a naive IST time (a datetime or the book's ISO text)."""
    moment = an.parse_time(value)
    return None if moment is None else moment.isoformat(sep=" ") + "+05:30"


# ---------------------------------------------------------------- PositionBook reconciliation


def _signed_open(result: dict) -> dict[tuple, tuple[float, float]]:
    """{(exchange, symbol, product): (signed qty, avg)} of the open positions of ``result``."""
    out = {}
    for p in result["positions"]:
        if p["status"] != "OPEN":
            continue
        sign = 1.0 if p["direction"] == "LONG" else -1.0
        out[(p["exchange"], p["symbol"], p["product"])] = (sign * p["open_qty"], p["avg_entry"])
    return out


def reconcile(broker: str, account: str) -> list[dict]:
    """Compare the ledger with the broker's PositionBook. Read-only: writes nothing.

    Needs the broker session of EXACTLY ``account``; otherwise ``ReconcileError`` (another
    account's PositionBook is never compared with this ledger).

    Ledger quantity nets ALL attribution states (the broker nets everything). MIS and CNC
    count only the CURRENT session (intraday positions do not carry; delivered CNC moves to
    holdings and drops out of the PositionBook after T+1); NRML uses the whole ledger. CNC
    still open from an earlier session is listed in separate rows (``scope='carried'``) with
    status ``carried_not_comparable`` (never a mismatch). ``discretionary_qty`` is the
    manual / ui_manual share, from an in-memory replay of those fills alone
    (``mixed_attribution`` says when other fills share the identity). Quantity must match
    exactly; average price within 0.05 %.

    Not verified without live data: Flattrade PositionBook ``average_price`` semantics (net
    vs day-buy average) and MCX quantity units (lots vs units).
    """
    auth = _account_auth_provider(broker, account)
    if not auth:
        raise ReconcileError(
            f"no broker session for account {account} on {broker}; refusing to compare with "
            "another account's PositionBook"
        )
    session = session_date(_now())
    all_fills = [
        f for f in _load_ledger_fills() if f["broker"] == broker and f["account"] == account
    ]

    def in_session(f):
        return session_date(f["fill_time"]) == session

    # position comparison scope: whole ledger for NRML (and other products), current session
    # for MIS and CNC
    scoped = [f for f in all_fills if f["product"] not in ("MIS", "CNC") or in_session(f)]
    carried_fills = [f for f in all_fills if f["product"] == "CNC" and not in_session(f)]
    ledger_pos = _signed_open(reconstruct(scoped, strict_opening=False))
    disc_pos = _signed_open(
        reconstruct([f for f in scoped if f["attribution_state"] in DISCRETIONARY_STATES])
    )
    carried_pos = _signed_open(reconstruct(carried_fills, strict_opening=False))
    carried_disc = _signed_open(
        reconstruct([f for f in carried_fills if f["attribution_state"] in DISCRETIONARY_STATES])
    )
    seen = {(f["exchange"], f["symbol"], f["product"]) for f in scoped}
    mixed = {
        (f["exchange"], f["symbol"], f["product"])
        for f in scoped
        if f["attribution_state"] not in DISCRETIONARY_STATES
    }
    carried_keys = {k for k, (q, _a) in carried_pos.items() if abs(q) > QTY_EPS}

    try:
        ok, resp, _status = _get_positionbook(auth[0], broker)
    except Exception as e:
        raise ReconcileError(f"PositionBook call failed: {type(e).__name__}") from None
    if not ok or not isinstance(resp, dict):
        raise ReconcileError("PositionBook call failed")
    broker_pos: dict[tuple, tuple[float, float]] = {}
    for row in resp.get("data") or []:
        key = (
            str(row.get("exchange", "")).upper(),
            str(row.get("symbol", "")),
            str(row.get("product", "")).upper(),
        )
        try:
            broker_pos[key] = (
                float(row.get("quantity") or 0),
                float(row.get("average_price") or 0),
            )
        except (TypeError, ValueError):
            continue

    report = []
    for key in sorted(seen | set(broker_pos)):
        if key in carried_keys and key not in seen:
            continue  # only carried CNC here: reported below, not comparable
        l_qty, l_avg = ledger_pos.get(key, (0.0, None))
        b_qty, b_avg = broker_pos.get(key, (None, None))
        in_ledger = key in seen
        if not in_ledger:
            if not b_qty:
                continue
            status = "missing_in_ledger"
        elif b_qty is None:
            status = "missing_in_broker" if abs(l_qty) > QTY_EPS else "match"
        elif abs(l_qty - b_qty) > QTY_EPS:
            status = "qty_mismatch"
        elif abs(b_qty) > QTY_EPS and abs(l_avg - b_avg) > AVG_TOLERANCE * abs(b_avg):
            status = "avg_mismatch"
        else:
            status = "match"
        report.append(
            {
                "identity": {
                    "broker": broker,
                    "account": account,
                    "exchange": key[0],
                    "symbol": key[1],
                    "product": key[2],
                },
                "ledger_qty": l_qty if in_ledger else None,
                "broker_qty": b_qty,
                "ledger_avg": l_avg,
                "broker_avg": b_avg,
                "discretionary_qty": disc_pos.get(key, (0.0, None))[0] if in_ledger else None,
                # True when non-discretionary fills share the identity: the discretionary-only
                # quantity is then a replay of its own fills, not something the broker reports.
                "mixed_attribution": key in mixed,
                "scope": "whole_ledger" if key[2] not in ("MIS", "CNC") else "session",
                "status": status,
            }
        )
    for key in sorted(carried_keys):
        c_qty, c_avg = carried_pos[key]
        _b_qty, b_avg = broker_pos.get(key, (None, None))
        report.append(
            {
                "identity": {
                    "broker": broker,
                    "account": account,
                    "exchange": key[0],
                    "symbol": key[1],
                    "product": key[2],
                },
                "ledger_qty": c_qty,
                "broker_qty": None,  # the session row of the identity carries the broker qty
                "ledger_avg": c_avg,
                "broker_avg": b_avg,
                "discretionary_qty": carried_disc.get(key, (0.0, None))[0],
                "mixed_attribution": any(
                    f["attribution_state"] not in DISCRETIONARY_STATES
                    for f in carried_fills
                    if (f["exchange"], f["symbol"], f["product"]) == key
                ),
                "scope": "carried",
                "status": "carried_not_comparable",
            }
        )
    return report


__all__ = [
    "ReconcileError",
    "fingerprint",
    "get_orphans",
    "get_positions",
    "get_completed_trades",
    "get_summary",
    "get_trades",
    "get_unresolved",
    "rebuild",
    "reconcile",
]
