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
(``get_unresolved``) and excluded from P&L; ``strategy_*`` fills are excluded.

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

Metrics
-------
Closed trades go to ``strategy_metrics_service.compute_metrics`` with GROSS realized P&L by
close date (no unrealized, no charges in the equity curve or drawdown). Discretionary has no
capital basis, so Sharpe, Sortino, the capital they assume and any return-on-capital are
withheld with the reason ``no capital basis`` (compute_metrics would otherwise quietly use
its 100,000 reference capital). Its start date is DISCRETIONARY_RELIABLE_SINCE, else the
session date of the first synced fill.

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

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict
from datetime import date, datetime

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

logger = get_logger(__name__)

DISCRETIONARY_STATES = (ledger.STATE_MANUAL, ledger.STATE_UI_MANUAL)
NO_CAPITAL = "no capital basis"
AVG_TOLERANCE = 0.0005  # 0.05 %
QTY_EPS = 1e-9

_rebuild_lock = threading.RLock()
_tables_ready = False
_quote_cache: dict[tuple[str, str, str], tuple[float, datetime]] = {}
_calendar_cache: dict[tuple[date, str], tuple[int, int] | None] = {}
_CALENDAR_EXCHANGES = frozenset({"NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX", "NCO"})
_IST_OFFSET_MS = 19_800_000


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
    }


def rebuild(force: bool = False) -> dict:
    """Rebuild the derived book when the ledger fingerprint changed (or ``force``)."""
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
            if positions:
                snap["positions"] = book.load_positions(filters, status="OPEN", build_id=bid)
            if orphans:
                snap["orphans"] = book.load_orphans(filters, build_id=bid)
            if trade_id is not None:
                snap["trade"] = book.load_trade(trade_id, build_id=bid)
                snap["trade_links"] = book.load_trade_fills(trade_id, bid) if snap["trade"] else []
            meta = book.get_meta()
        if meta and meta["build_id"] == bid:
            return snap
    raise RuntimeError("discretionary book kept changing during a read")


# ---------------------------------------------------------------- read API


def _unresolved_of(fills: list[dict], filters: dict) -> list[dict]:
    out = []
    for f in fills:
        if f["attribution_state"] != ledger.STATE_UNRESOLVED:
            continue
        if any(f.get(k) != v for k, v in filters.items()):
            continue
        out.append(
            {
                k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v)
                for k, v in f.items()
                if k not in ("raw_hash",)
            }
        )
    return out


def get_trades(broker=None, account=None, symbol=None, start=None, end=None) -> list[dict]:
    """Closed discretionary trades (close order), ``start``/``end`` on trade_date (inclusive)."""
    filters = _clean_filters(broker, account, symbol)
    return _snapshot(filters, trades=True, start=start, end=end)["trades"]


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
) -> dict:
    """Metrics, realized/charges/net and unrealized for the period (see ``_summary_parts``)."""
    filters = _clean_filters(**{k: (filters or {}).get(k) for k in ("broker", "account", "symbol")})
    snap = _snapshot(filters, trades=True, positions=True, orphans=True)
    return _summary_parts(snap, period, start, end, filters, with_quotes)["summary"]


def _summary_parts(
    snap: dict,
    period: str,
    start: str | None,
    end: str | None,
    filters: dict,
    with_quotes: bool,
) -> dict:
    """Build the summary from ONE snapshot. Returns {"summary", "trades" (in the period),
    "positions" (priced views), "orphans", "today"} so a caller that needs more than the
    summary does not read the book again.

    Metrics, realized/charges/net and unrealized for the period; filters are
    broker / account / symbol. Totals are sums of the per-identity figures.

    Naming: ``realized_gross`` is realized P&L before charges (the same basis as
    ``metrics.net_profit``, which is therefore gross despite its name); ``charges`` are
    estimated; top-level ``net`` == ``realized_net_of_charges`` == realized_gross - charges.
    One snapshot (one ledger load, one fingerprint, one build_id) feeds every figure.
    """
    all_trades = snap["trades"]
    floor = reliable_since(snap["fills"])
    today = session_date(_now())
    s, e = metrics_svc.resolve_period(
        period, to_metric_trades(all_trades), today, start, end, floor=floor
    )
    trades = [t for t in all_trades if s <= t["trade_date"] <= e]

    per: dict[tuple, dict] = {}
    for t in trades:
        row = per.setdefault(
            (t["broker"], t["account"], t["exchange"], t["symbol"], t["product"]),
            {
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
            },
        )
        row["trades"] += 1
        row["realized_gross"] += t["realized_pnl"]
        if t["charges"] is None:
            row["charges_unavailable_trades"] += 1
        else:
            row["charges"] += t["charges"]

    pos_view, hours_source = _price_positions(snap["positions"], with_quotes)
    for p in pos_view:
        row = per.setdefault(
            (p["broker"], p["account"], p["exchange"], p["symbol"], p["product"]),
            {
                "broker": p["broker"],
                "account": p["account"],
                "exchange": p["exchange"],
                "symbol": p["symbol"],
                "product": p["product"],
                "trades": 0,
                "realized_gross": 0.0,
                "charges": 0.0,
                "charges_unavailable_trades": 0,
                "unrealized": 0.0,
                "unrealized_unavailable_positions": 0,
            },
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

    metric_trades = to_metric_trades(trades)
    costs = [t["charges"] for t in trades]
    metrics = withhold_capital_metrics(
        metrics_svc.compute_metrics(metric_trades, s, e, costs=costs, capital=None)
    )
    metrics["charges_unavailable_trades"] = unavailable

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

    summary = {
        "period": {"period": period, "start": s, "end": e, "reliable_since": floor.isoformat()},
        "metrics": metrics,
        "build_id": snap["build_id"],
        "market_hours_source": hours_source,
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
    }
    return {
        "summary": summary,
        "trades": trades,
        "positions": pos_view,
        "orphans": orphans,
        "today": today,
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
    "buckets": "Daily, weekly (ISO, Monday start) and monthly buckets use the session date of the close.",
    "ratios": "Win rate, profit factor and expectancy are computed per set of trades, never summed.",
}


def _fill_time(value) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value


class BuildChanged(Exception):
    """The caller's build_id is no longer the current build (trade ids are per build)."""

    def __init__(self, current_build_id: str):
        super().__init__("discretionary build changed")
        self.current_build_id = current_build_id


def get_trade_fills(trade_id: int, build_id: str | None = None) -> dict | None:
    """The ledger fills behind one closed trade of the CURRENT build, or None when the trade
    does not exist in it. Trade ids are reassigned by every rebuild, so when ``build_id`` is
    given and is not the current build this raises BuildChanged rather than return a
    different trade that reuses the id. Entry fills first, then exits, each oldest first. Reads only; the
    ledger rows are returned as stored (no raw hash, no tokens: the ledger holds none)."""
    snap = _snapshot({}, trade_id=trade_id)
    if build_id is not None and snap["build_id"] != build_id:
        raise BuildChanged(snap["build_id"])
    trade = snap["trade"]
    if trade is None:
        return None
    by_id = {f["id"]: f for f in snap["fills"]}
    fills = []
    for link in snap["trade_links"]:
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
) -> dict:
    """Everything the Discretionary Performance page needs from ONE snapshot (one build_id).

    ``get_summary`` plus: by_period {daily, weekly, monthly}, by_symbol, by_side, by_product,
    by_holding {intraday, overnight}, hold, trades / positions (newest first, at most
    TABLE_LIMIT each, with ``*_total`` and ``*_truncated``), withheld and labels. The
    broker / account / symbol filters apply to every figure, table and chart; pooled ratios
    are computed on the filtered set. ``with_quotes`` False (default) makes no broker call.
    Raises ValueError for a bad period or custom range.
    """
    filters = _clean_filters(broker, account, symbol)
    snap = _snapshot(filters, trades=True, positions=True, orphans=True)
    parts = _summary_parts(snap, period, start, end, filters, with_quotes)
    out = dict(parts["summary"])
    trades = parts["trades"]
    out["by_period"] = {g: an.period_breakdown(trades, g) for g in an.GRANULARITIES}
    out["by_symbol"] = an.group_breakdown(trades, "symbol")
    out["by_side"] = an.group_breakdown(trades, "direction")
    out["by_product"] = an.group_breakdown(trades, "product")
    out["by_holding"] = an.holding_breakdown(trades)
    out["hold"] = an.hold_summary(trades)

    newest = sorted(trades, key=lambda t: (t["closed_at"], t["id"]), reverse=True)
    out["trades"] = newest[:TABLE_LIMIT]
    out["trades_total"] = len(newest)
    out["trades_truncated"] = max(0, len(newest) - TABLE_LIMIT)
    positions = parts["positions"]
    out["positions"] = positions[:TABLE_LIMIT]
    out["positions_total"] = len(positions)
    out["positions_truncated"] = max(0, len(positions) - TABLE_LIMIT)
    out["withheld"] = dict(out["metrics"].get("withheld") or {})
    out["labels"] = dict(LABELS)
    out["quotes_requested"] = bool(with_quotes)
    return out


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
    "get_summary",
    "get_trades",
    "get_unresolved",
    "rebuild",
    "reconcile",
]
