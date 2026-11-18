# services/discretionary_ledger_service.py
"""
Broker-fill sync and attribution for the Discretionary Trading Performance ledger.

Stage 1: fetch fills (read-only), normalize, dedup-insert, attribute. No accounting.

Runs on a background thread with no Flask request context. It never places, modifies or
cancels anything and only ever reads the live broker path (never sandbox/analyze).

Attribution of a fill, evaluated in this order (first match wins):
  1. live StrategyOrderTag for the order id, label in UI_ORDER_LABELS -> ui_manual
  2. live StrategyOrderTag for the order id                           -> strategy_tag
  3. order id in signal_engine trades.db (sentinel ids excluded)      -> strategy_engine
  4. neither source knows it AND the fill is covered (date or same-day) -> manual
  5. otherwise                                                        -> unresolved
Coverage: a fill is covered only if its date >= coverage_anchor date + 1 day, where
coverage_anchor = the EARLIEST of (a) MIN(created_at) of live StrategyOrderTag rows and
(b) the started_at of the earliest ledger_sync_runs row with status ok/partial for ANY
broker/account (the run in progress counts, it will end ok/partial). Reason: orders placed
through OpenAlgo are tagged at placement, before their fills reach the broker TradeBook, and
the ledger only sees fills from the day it began syncing, so the first successful sync date
is a valid coverage start even when no strategy order was ever tagged (a purely manual
trader). Runs with status no_auth / skipped_window / error never start coverage. The +1 day
buffer stays for whole days: a day's TradeBook can hold fills from before the ledger ran, so
the anchor day is not trusted AS A DAY. Same-day coverage (an OR, per fill; see
same_day_covered) also covers a fill on its own date when (a) an ok/partial run started that
day FINISHED strictly before T = the effective session start of the fill's exchange that day
(market_calendar_db.get_effective_session_window: holidays, weekends, special sessions)
minus SESSION_LEAD_MIN (15) minutes, and the fill is at/after T; (b) counting from T, the
start times of ok/partial runs started that day (any broker/account; the run in progress
counts) up to the first one at/after the fill are never more than
DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN (default 30) minutes apart. Failed / skipped / no_auth
runs are not heartbeats, and an empty sync cycle (no non-revoked auth row) records no run, so
both are gaps: once a gap exceeds the limit, every later fill of that day stays unresolved
(the safe outcome). A closed or unknown session (calendar error) -> not same-day covered.
It is evaluated only for fills the date rule does not cover, i.e. in practice the anchor day.
The grace period still applies, and a tag-source error still aborts the run before any
attribution. The anchor is computed on the fly from
ledger_sync_runs plus the tag table; the stored ledger_sync_runs.tag_min_created_at column
keeps its meaning (MIN live tag created_at only, NULL when none), it is NOT the anchor. If
signal_engine trades.db cannot be read
the order cannot be proven absent, so it stays unresolved. strategy_* / ui_manual rows are
never downgraded. Unresolved rows and manual rows are re-evaluated on each sync: manual
may move only to strategy_tag / strategy_engine / ui_manual (a tag or trades.db row can
arrive after the first sync), and the audit reason says so. A fill is called manual only
once DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN (default 30) minutes have passed since its
fill_time; inside that window it stays unresolved.

Coverage rule: implications (see docs/discretionary_attribution.md for the long form)
-------------------------------------------------------------------------------------
* A fill dated BEFORE the coverage start (anchor date + 1 day) is ``unresolved``, never
  ``manual``: nothing can show that a strategy did not place it. Unresolved fills are listed
  and excluded from every P&L figure.
* First enablement: the anchor is the first ok/partial sync (or the oldest live tag, if
  earlier). Fills dated on or before that day stay unresolved unless same-day coverage holds
  (a run finished before T and no heartbeat gap above the limit up to the fill); fills from
  the next day on can be ``manual``. History from before the first sync is therefore never classed manual, and
  the broker TradeBook is a current-day book, so older fills usually are not in the ledger
  at all.
* The anchor is derived on every sync from persisted rows (no code prunes ledger_sync_runs),
  so a restart does not move it, and it is shared by every broker and account. Later syncs
  never move it. The one way it moves LATER: the oldest live tag predates the first sync and
  is then pruned (StrategyOrderTag retention, default 30 days); the anchor falls back to the
  first sync start. The date rule is a threshold, not a continuity check: a day with no
  successful sync is still "covered", and the fills of that day are simply absent if they
  were never fetched (the pnl service reports such weekdays as days_without_sync). Same-day
  coverage, in contrast, requires continuity.
* Same-day coverage does not remove one residual risk: an order placed earlier that day
  (after-market / AMO or pre-open) by a build or process WITHOUT tagging fills during the
  covered session and is then classed manual. The ledger stores no order-entry time.
* A failed tag write (database/strategy_book_db.py record_order_tag logs the exception and
  returns False) leaves that order without a tag, so its fill is classed manual. Neither the
  date rule nor the same-day rule protects against it, on covered days as on any other day.
* Order ids the broker creates itself (GTT triggers, bracket / cover-order legs, broker-side
  replacements) were never tagged by OpenAlgo and end up manual.
* Coupling of the same-day rule to the scheduler: the ticker wakes every 60 s and a periodic
  sync is due once DISCRETIONARY_LEDGER_INTERVAL_MIN (default 15) minutes have passed, so
  periodic syncs start about every 15m20s (tick drift included; observed 15m20s to 15m24s).
  With G = 30 one missed periodic sync makes a gap of about 30m40s > G, so G tolerates ZERO
  missed syncs: after one miss every later fill of that day stays unresolved (fails safe).
  An INTERVAL_MIN of G or more disables the same-day rule in practice (spacing is the
  interval plus drift). The scheduler runs no sync before the session opens (periodic starts
  at 09:15, the first heartbeat after T = 09:00): a run finished before T exists only after a
  boot before T (boot catch-up; not inside the 05:00-06:00 skip window, which records
  skipped_window, not a heartbeat). Do not relax G or the 15-minute lead to compensate.
* Known ways an automated order is still counted as discretionary (documented, not fixed):
  broker auto square-off of an automated position (no OpenAlgo tag, no engine row ->
  manual); the UI "close all" on an automated position (label in UI_ORDER_LABELS ->
  ui_manual, which counts as discretionary); strategy orders with an EMPTY strategy name
  (record_order_tag writes no tag when the name is empty -> manual); signal_engine trades
  whose order_id is a placeholder ("-..." or SENTINEL_ORDER_IDS) instead of the broker order
  id (never matched -> manual). Realized P&L is double counted (by the strategy side and by
  the discretionary side) exactly when such a fill closes or opens a position that the
  strategy side also counts.
* Not collected, so never classified: the MCX evening session (17:00-23:55) and anything
  outside the scheduler windows; Muhurat / special sessions (weekday scheduler only). When
  the TradeBook resets is NOT verified, so fills made outside the sync windows may be lost.
* Expired derivatives are unsupported: there is no settlement close, an expired position
  stays open.

Broker contract (brokers are discovered by name, no registry here)
------------------------------------------------------------------
A broker takes part by adding to ITS OWN module ``broker.<name>.mapping.order_data``:

  map_trade_fills(raw_response, default_account="") -> (fills, skipped)
      ``skipped`` is an int: rows skipped. It may be an int subclass carrying an optional
      ``unmapped`` attribute: how many of those rows were lost because their symbol or
      product could not be mapped to OpenAlgo (master contract missing or stale, unknown
      product code) rather than because broker data was missing. A plain int means
      ``unmapped = 0``. Nothing is stored for a skipped row, so the next sync retries it;
      but a broker whose book covers only the current day can lose it for good, hence the
      distinct "partial" run status below.
      ``raw_response`` is what ``broker.<name>.api.order_api.get_trade_book(auth)`` returns
      unchanged. Pure (no I/O, input not modified). For an error response it raises an
      exception; one carrying ``is_auth_error = True`` means the session was rejected
      (recorded as no_auth). A row that cannot be verified is skipped and counted, never
      filled in from order-level data.
  SYNC_SKIP_WINDOW = ((h, m), (h, m))    optional; naive IST, start inclusive, end exclusive;
      no sync is attempted inside it. Absent -> no window.

A broker without ``map_trade_fills`` has only order-level rows (no fill id, an order average
price); ledgering those would double count, so the sync records status "no_fill_data".

Each fill returned is a dict (see FILL_FIELDS) and is checked by validate_fill() before
insertion; a fill that fails is skipped and counted with the broker's own skips:

  account    str, optional ("" or missing -> the canonical account, see below; none known ->
             the fill is skipped and counted)
  order_id   str, optional ("" if unknown); used for attribution
  fill_id    str | None  broker execution id; None only if the broker has none (the dedup key
             then falls back to the order/symbol/side/qty/price/time tuple). The dedup key
             with a fill id is broker|account|exchange|YYYY-MM-DD|order_id|fill_id: the
             exchange trade number may be unique only per instrument, so the broker order id
             is part of the key and two symbols sharing a fill id are both kept. A broker that
             supplies a fill id SHOULD also supply the order id.
  symbol     str, non-empty, OpenAlgo symbol (never the raw broker symbol)
  exchange   str, non-empty
  product    str, one of PRODUCTS (MIS / CNC / NRML), the OpenAlgo product name
  side       "BUY" | "SELL"
  qty        float, finite, > 0      quantity of THIS fill
  price      float, finite, > 0      price of THIS fill, unrounded
  fill_time  naive datetime, IST     time of THIS fill
  raw_hash   str | None, optional    digest of the raw row
The service sets ``broker``; a value supplied by the broker module is ignored.

Rows that a broker module skips, and fills failing validation, are counted in the sync
run's error text. ``skipped_unmapped=N`` counts rows lost to the mapping layer (``unmapped``);
``unverifiable_skipped=M`` counts the rest (missing broker data, contract violations). When
N > 0 the run status is "partial" (not "ok") and one warning with the counts is logged.

Account label (canonical account identity): the account of a fill is the one the broker
reports; a fill without one gets the login's canonical account (resolve_account: the account
remembered in ledger_account_identity for (broker, Auth name), else the Auth row's broker
user_id); with neither the fill is skipped and counted. The OpenAlgo user name is never used
as an account label. When a fetch's fills report exactly one account, it is remembered as
the login's canonical account (remember_account). The sync-run record uses the same label as
ledger_fills when the fetch returned fills of one account; otherwise the canonical account,
or "" when none is known yet (an empty first sync). Runs with account "" are
account-agnostic: they count for the coverage anchor and same-day heartbeats (which ignore
accounts anyway) but never yield a per-account "latest sync bad" warning; the pnl service instead warns when the
latest run of a broker (any label) is bad.
"""

import importlib
import math
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from database import discretionary_ledger_db as ledger
from utils.env_config import env_int
from utils.logging import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Strategy labels that mark an order placed by hand from the OpenAlgo UI.
# Confirmed in code: blueprints/orders.py (two call sites) sends "UI Exit Position".
UI_ORDER_LABELS = frozenset({"UI Exit Position"})

# Order ids that signal_engine writes for non-orders; they never match a broker fill.
SENTINEL_ORDER_IDS = frozenset(
    {"-TRACKER-", "-EXECUTION-", "-DECLINED-", "-RECONCILED-", "RECONCILED_ORDER_ID"}
)

_CHUNK = 400

_SIGNAL_ENGINE_DB_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "signal_engine",
    "data",
    "trades.db",
)


class LedgerAuthError(Exception):
    """The broker rejected the session, or there is no usable token."""


class NoPerFillDataError(Exception):
    """The broker module offers no per-fill reader (map_trade_fills)."""


class FillBatch(list):
    """List of normalized fills plus how many broker rows were unverifiable and skipped."""

    skipped: int = 0  # all skipped rows (unverifiable + unmapped)
    unmapped: int = 0  # subset lost because symbol/product could not be mapped

    def __init__(self, *args):
        super().__init__(*args)
        self.reported_accounts: set[str] = set()  # accounts the broker itself reported


def manual_grace_minutes() -> int:
    return env_int("DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN", 30, minimum=0)


# Same-day coverage: fills can occur this long before the listed session start
# (NSE/BSE pre-open 09:00-09:08, opening auctions).
SESSION_LEAD_MIN = 15


def max_sync_gap_minutes() -> int:
    """Largest gap between successful sync starts allowed for same-day coverage."""
    return env_int("DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN", 30, minimum=1)


def now_ist() -> datetime:
    """Naive IST wall-clock time."""
    return datetime.now(IST).replace(tzinfo=None)


def in_skip_window(now: datetime, window=None) -> bool:
    """True inside a broker-declared ((h, m), (h, m)) window; no window -> never."""
    if not window:
        return False
    start, end = window
    return tuple(start) <= (now.hour, now.minute) < tuple(end)


def _signal_engine_db_path() -> str:
    return os.getenv("SIGNAL_ENGINE_TRADES_DB", _SIGNAL_ENGINE_DB_DEFAULT)


def _redact(text: str, secrets: tuple = ()) -> str:
    for s in secrets:
        if s:
            text = text.replace(str(s), "***")
    return text[:300]


# ---------------------------------------------------------------- fetching

FILL_FIELDS = (
    "account",
    "order_id",
    "fill_id",
    "symbol",
    "exchange",
    "product",
    "side",
    "qty",
    "price",
    "fill_time",
    "raw_hash",
)

_BROKER_NAME = re.compile(r"[a-z0-9_]+")
PRODUCTS = ("MIS", "CNC", "NRML")


def _is_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _opt_str(value) -> str | None:
    """str, or None when ``value`` is not a str / None."""
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else None


def validate_fill(fill, broker: str, default_account: str = "") -> dict | None:
    """Check one fill returned by a broker module against the contract.

    Returns a clean new dict (only FILL_FIELDS plus ``broker``) or None when the fill
    violates the contract. Never raises.
    """
    if not isinstance(fill, dict):
        return None
    required_text = {}
    for key in ("symbol", "exchange", "product"):
        v = fill.get(key)
        if not isinstance(v, str) or not v.strip():
            return None
        required_text[key] = v.strip()
    if required_text["product"] not in PRODUCTS:
        return None
    account = _opt_str(fill.get("account"))
    order_id = _opt_str(fill.get("order_id"))
    if account is None or order_id is None:
        return None
    fill_id = fill.get("fill_id")
    if fill_id is not None:
        if not isinstance(fill_id, str):
            return None
        fill_id = fill_id.strip() or None
    raw_hash = fill.get("raw_hash")
    if raw_hash is not None and not isinstance(raw_hash, str):
        return None
    when = fill.get("fill_time")
    if not isinstance(when, datetime) or when.tzinfo is not None:
        return None
    if fill.get("side") not in ("BUY", "SELL"):
        return None
    if not _is_number(fill.get("qty")) or not _is_number(fill.get("price")):
        return None
    account = account or (default_account or "").strip()
    if not account:
        return None  # no broker account and no canonical one: never guess a label
    return {
        "broker": broker,
        "account": account,
        "order_id": order_id,
        "fill_id": fill_id,
        "symbol": required_text["symbol"],
        "exchange": required_text["exchange"],
        "product": required_text["product"],
        "side": fill["side"],
        "qty": float(fill["qty"]),
        "price": float(fill["price"]),
        "fill_time": when,
        "raw_hash": raw_hash,
    }


def _import_broker_module(path: str):
    """Import ``path``; None only when that very module does not exist.

    A ModuleNotFoundError for another name (a dependency missing inside the broker module)
    or any other import-time failure propagates, so sync_once records it as an error with
    the exception type instead of calling the broker "no_fill_data".
    """
    try:
        return importlib.import_module(path)
    except ModuleNotFoundError as e:
        parts = path.split(".")
        absent = {".".join(parts[:i]) for i in range(1, len(parts) + 1)}
        if e.name in absent:
            return None
        raise


def load_broker_reader(broker: str):
    """Discover a broker's per-fill reader by name (same pattern as tradebook_service).

    Returns a namespace (get_trade_book, map_trade_fills, skip_window) or None when the
    broker has no ``map_trade_fills`` in broker.<name>.mapping.order_data. Read-only: only
    the broker's TradeBook fetch and mapper are looked up.
    """
    if not _BROKER_NAME.fullmatch(broker or ""):
        return None
    mapping = _import_broker_module(f"broker.{broker}.mapping.order_data")
    if mapping is None:
        return None
    map_fills = getattr(mapping, "map_trade_fills", None)
    if not callable(map_fills):
        return None
    api = _import_broker_module(f"broker.{broker}.api.order_api")
    if api is None:
        return None
    get_trade_book = getattr(api, "get_trade_book", None)
    if not callable(get_trade_book):
        return None
    return SimpleNamespace(
        get_trade_book=get_trade_book,
        map_trade_fills=map_fills,
        skip_window=getattr(mapping, "SYNC_SKIP_WINDOW", None),
    )


def fetch_fills(broker: str, auth_token: str, auth_obj, reader=None) -> FillBatch:
    """Fetch and validate the broker's per-fill rows (read-only).

    Raises NoPerFillDataError (no reader), LedgerAuthError (session rejected) or whatever
    the broker call raises. The returned FillBatch counts every unverifiable row.
    """
    reader = reader or load_broker_reader(broker)
    if reader is None:
        raise NoPerFillDataError(
            f"broker {broker or '?'} has no per-fill reader (map_trade_fills); not ledgered"
        )
    default_account = resolve_account(broker, getattr(auth_obj, "name", ""), auth_obj)
    raw_response = reader.get_trade_book(auth_token)
    try:
        mapped, skipped = reader.map_trade_fills(raw_response, default_account)
    except Exception as e:
        if getattr(e, "is_auth_error", False):
            raise LedgerAuthError(str(e)) from e
        raise
    fills = FillBatch()
    fills.skipped = max(int(skipped or 0), 0)
    fills.unmapped = min(max(int(getattr(skipped, "unmapped", 0) or 0), 0), fills.skipped)
    for item in mapped:
        clean = validate_fill(item, broker, default_account)
        if clean is None:
            fills.skipped += 1
        else:
            fills.append(clean)
            reported = item.get("account")
            if isinstance(reported, str) and reported.strip():
                fills.reported_accounts.add(reported.strip())
    return fills


# ---------------------------------------------------------------- account identity


def resolve_account(broker: str, user: str, auth_obj) -> str:
    """Canonical account label of an OpenAlgo login; "" when none is known.

    Order: the account remembered in ledger_account_identity (learnt from the broker's own
    fills), else the Auth row's broker user_id, else "". The OpenAlgo user name is never
    used as an account label.
    """
    if broker and user:
        try:
            known = ledger.get_identity(broker, user)
        except Exception as e:
            logger.warning("Could not read ledger account identity: %s", type(e).__name__)
            known = None
        if known:
            return known
    return (getattr(auth_obj, "user_id", None) or "").strip()


def remember_account(broker: str, user: str, account: str) -> bool:
    """Remember ``account`` as the canonical account of (broker, user). Never raises."""
    try:
        return ledger.remember_identity(broker, user, account)
    except Exception as e:
        logger.warning("Could not store ledger account identity: %s", type(e).__name__)
        return False


# ---------------------------------------------------------------- dedup


def _fmt(n: float) -> str:
    return f"{float(n):.6f}"


def assign_dedup(fills: list[dict]) -> list[dict]:
    """Add occurrence_idx and dedup_key to each fill (mutates and returns the list).

    With a real fill id: broker|account|exchange|YYYY-MM-DD|order_id|fill_id (the exchange
    trade number may be unique only per instrument, so the order id is part of the key).
    Otherwise
    broker|account|exchange|order_id|symbol|side|qty|price|time|occurrence_idx where the
    index counts identical tuples inside this batch in arrival order.
    """
    counts: dict[tuple, int] = defaultdict(int)
    for f in fills:
        if f.get("fill_id"):
            f["occurrence_idx"] = 0
            day = f["fill_time"].strftime("%Y-%m-%d")
            f["dedup_key"] = (
                f"{f['broker']}|{f['account']}|{f.get('exchange') or ''}|{day}"
                f"|{f.get('order_id') or ''}|{f['fill_id']}"
            )
            continue
        base = (
            f["broker"],
            f["account"],
            f.get("exchange") or "",
            f["order_id"],
            f["symbol"],
            f["side"],
            _fmt(f["qty"]),
            _fmt(f["price"]),
            f["fill_time"].strftime("%Y-%m-%d %H:%M:%S"),
        )
        idx = counts[base]
        counts[base] += 1
        f["occurrence_idx"] = idx
        f["dedup_key"] = "|".join(base) + f"|{idx}"
    return fills


# ---------------------------------------------------------------- attribution sources


def _load_live_tags(order_ids: list[str]) -> tuple[dict[str, list], set[str]]:
    """Return ({order_id: [(strategy, broker)]} for live tags, {order ids with unknown-mode tags})."""
    from database.strategy_book_db import StrategyOrderTag, db_session

    live: dict[str, list] = defaultdict(list)
    unknown: set[str] = set()
    try:
        for i in range(0, len(order_ids), _CHUNK):
            chunk = order_ids[i : i + _CHUNK]
            rows = (
                db_session.query(
                    StrategyOrderTag.orderid,
                    StrategyOrderTag.strategy,
                    StrategyOrderTag.broker,
                    StrategyOrderTag.mode,
                )
                .filter(StrategyOrderTag.orderid.in_(chunk))
                .all()
            )
            for oid, strategy, broker, mode in rows:
                m = (mode or "").strip().lower()
                if m == "live":
                    live[oid].append((strategy, (broker or "").strip().lower()))
                elif m != "analyze":
                    unknown.add(oid)  # pre-upgrade / unknown mode: cannot prove it is not live
    finally:
        db_session.remove()
    return live, unknown


def get_tag_min_created_at() -> datetime | None:
    """MIN(created_at) over live StrategyOrderTag rows; None when there are none."""
    from sqlalchemy import func

    from database.strategy_book_db import StrategyOrderTag, db_session

    try:
        return (
            db_session.query(func.min(StrategyOrderTag.created_at))
            .filter(StrategyOrderTag.mode == "live")
            .scalar()
        )
    finally:
        db_session.remove()


def get_coverage_anchor(
    tag_min: datetime | None, current_start: datetime | None = None
) -> datetime | None:
    """Earliest of: MIN live tag created_at, earliest ok/partial sync run start, current run.

    Coverage starts the day after this anchor's date. See the module docstring.
    """
    from sqlalchemy import func, select

    try:
        first_run = ledger.db_session.execute(
            select(func.min(ledger.LedgerSyncRun.started_at)).where(
                ledger.LedgerSyncRun.status.in_(("ok", "partial"))
            )
        ).scalar()
    except Exception as e:
        logger.warning("Could not read first sync run for coverage: %s", type(e).__name__)
        first_run = None
    finally:
        ledger.db_session.remove()
    candidates = [c for c in (tag_min, first_run, current_start) if c is not None]
    return min(candidates) if candidates else None


def _engine_known_order_ids(order_ids: list[str]) -> set[str] | None:
    """Order ids present in signal_engine trades.db (read-only).

    A trades.db file that does not exist (signal_engine not installed) is an empty set: no
    order can be an engine order. A file that exists but cannot be read or queried gives None
    (the fills stay unresolved).
    """
    path = _signal_engine_db_path()
    if not os.path.exists(path):
        return set()
    wanted = [o for o in order_ids if o and o not in SENTINEL_ORDER_IDS and not o.startswith("-")]
    found: set[str] = set()
    uri = "file:" + path.replace("\\", "/").replace("?", "%3f") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            for i in range(0, len(wanted), _CHUNK):
                chunk = wanted[i : i + _CHUNK]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT DISTINCT order_id FROM trades WHERE order_id IN ({marks})", chunk
                ).fetchall()
                found.update(r[0] for r in rows)
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("signal_engine trades.db unreadable for attribution: %s", type(e).__name__)
        return None
    return found


def is_covered(fill_time: datetime, tag_min: datetime | None) -> bool:
    """``tag_min`` is the coverage anchor (see get_coverage_anchor); covered from anchor day + 1."""
    # StrategyOrderTag.created_at is server-local time; this assumes the host runs in IST.
    if tag_min is None:
        return False
    return fill_time.date() >= (tag_min + timedelta(days=1)).date()


def _session_start(day: date, exchange: str) -> datetime | None:
    """Naive IST time from which fills of ``exchange`` can occur on ``day``.

    The calendar's effective session start (holidays, special sessions) minus
    SESSION_LEAD_MIN. None when the exchange is closed that day or the calendar fails.
    """
    try:
        from database.market_calendar_db import get_effective_session_window

        window = get_effective_session_window(day, exchange)
    except Exception as e:
        logger.warning("Session window unavailable for coverage: %s", type(e).__name__)
        return None
    if not window or window.get("start_ms") is None:
        return None
    start = datetime.fromtimestamp(window["start_ms"] / 1000, IST).replace(tzinfo=None)
    return start - timedelta(minutes=SESSION_LEAD_MIN)


def _load_ok_runs(days: set) -> dict:
    """{date: [(started_at, finished_at)]} of ok/partial runs (any broker/account) per start day."""
    from sqlalchemy import select

    out: dict = defaultdict(list)
    if not days:
        return out
    lo = datetime.combine(min(days), datetime.min.time())
    hi = datetime.combine(max(days) + timedelta(days=1), datetime.min.time())
    try:
        rows = ledger.db_session.execute(
            select(ledger.LedgerSyncRun.started_at, ledger.LedgerSyncRun.finished_at).where(
                ledger.LedgerSyncRun.status.in_(("ok", "partial")),
                ledger.LedgerSyncRun.started_at >= lo,
                ledger.LedgerSyncRun.started_at < hi,
            )
        ).all()
    finally:
        ledger.db_session.remove()
    for started, finished in rows:
        if started.date() in days:
            out[started.date()].append((started, finished))
    return out


def same_day_covered(
    fill_time: datetime,
    exchange: str,
    runs,
    current_start: datetime | None = None,
    start_fn: Callable | None = None,
) -> bool:
    """True when the ledger demonstrably ran from before the session up to the fill.

    ``runs``: (started_at, finished_at) of ok/partial runs started on the fill's date, any
    broker/account. T = session start of the fill's exchange that day minus SESSION_LEAD_MIN
    (``start_fn``, default _session_start; None -> not covered). Covered when fill_time >= T,
    a run FINISHED strictly before T, and the run starts >= T (plus ``current_start``, the run
    in progress) counted from T up to the first one at/after fill_time are never more than
    max_sync_gap_minutes() apart. Failed/skipped runs are not passed in, so they are gaps.
    """
    if not runs:
        return False
    start = (start_fn or _session_start)(fill_time.date(), exchange)
    if start is None or fill_time < start:
        return False
    if not any(fin is not None and fin < start for _s, fin in runs):
        return False
    points = {s for s, _f in runs if s >= start}
    if (
        current_start is not None
        and current_start.date() == fill_time.date()
        and current_start >= start
    ):
        points.add(current_start)
    limit = timedelta(minutes=max_sync_gap_minutes())
    prev = start
    for p in sorted(points):
        if p - prev > limit:
            return False
        if p >= fill_time:
            return True
        prev = p
    return False


def classify(
    fill: dict,
    live_tags,
    unknown_mode,
    engine_ids,
    tag_min,
    now: datetime | None = None,
    same_day: bool = False,
) -> tuple[str, str | None, str, str]:
    """Return (state, strategy, reason, source) for one fill row dict.

    ``now`` (IST) enables the manual grace period: a fill younger than
    DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN stays unresolved so late tags can still arrive.
    ``same_day`` (see same_day_covered) covers the fill even when the date rule does not.
    """
    oid = fill["order_id"]
    if not oid or oid in SENTINEL_ORDER_IDS:
        return ledger.STATE_UNRESOLVED, None, "no usable order id", "none"
    tags = [t for t in live_tags.get(oid, []) if t[1] in ("", "unknown", fill["broker"])]
    if tags:
        strategy = tags[0][0]
        if strategy in UI_ORDER_LABELS:
            return ledger.STATE_UI_MANUAL, strategy, "UI-placed order tag", "tag"
        return ledger.STATE_STRATEGY_TAG, strategy, "live order tag", "tag"
    if oid in unknown_mode:
        return ledger.STATE_UNRESOLVED, None, "tag of unknown mode", "none"
    if engine_ids is None:
        return ledger.STATE_UNRESOLVED, None, "signal_engine trades unreadable", "none"
    if oid in engine_ids:
        return (
            ledger.STATE_STRATEGY_ENGINE,
            "signal_engine",
            "order in signal_engine trades",
            "signal_engine",
        )
    by_date = is_covered(fill["fill_time"], tag_min)
    if by_date or same_day:
        if now is not None and now - fill["fill_time"] < timedelta(minutes=manual_grace_minutes()):
            return ledger.STATE_UNRESOLVED, None, "within manual grace period", "none"
        why = "tag coverage ok" if by_date else "same-day coverage ok"
        return ledger.STATE_MANUAL, None, f"absent from tags and engine; {why}", "none"
    return ledger.STATE_UNRESOLVED, None, "fill date before tag coverage", "none"


def attribute_unresolved(
    broker: str | None,
    account: str | None,
    tag_min: datetime | None,
    now: datetime | None = None,
    current_start: datetime | None = None,
) -> int:
    """Re-evaluate every unresolved and manual row. Returns changes.

    ``current_start``: start of the sync run in progress (it will end ok/partial); it counts
    as a heartbeat for same-day coverage. None outside a sync.

    ``broker`` / ``account`` None (the sync default) re-evaluates all rows of every broker and
    account; a value restricts to it. Account isolation holds either way: order ids match
    globally, a tag only matches a fill of its own broker (see classify).

    Manual rows are re-checked so a tag or trades.db row that arrived after the first
    sync upgrades them (and removes the double count). They can only move to
    strategy_tag / strategy_engine / ui_manual; db.set_attribution refuses anything else.
    """
    from sqlalchemy import select

    if now is None:
        now = now_ist()
    try:
        rows = ledger.db_session.execute(
            select(
                ledger.LedgerFill.id,
                ledger.LedgerFill.order_id,
                ledger.LedgerFill.broker,
                ledger.LedgerFill.fill_time,
                ledger.LedgerFill.attribution_state,
                ledger.LedgerFill.exchange,
            ).where(
                ledger.LedgerFill.attribution_state.in_(
                    (ledger.STATE_UNRESOLVED, ledger.STATE_MANUAL)
                ),
                *([ledger.LedgerFill.broker == broker] if broker is not None else []),
                *([ledger.LedgerFill.account == account] if account is not None else []),
            )
        ).all()
    finally:
        ledger.db_session.remove()
    if not rows:
        return 0
    fills = [
        {
            "id": r[0],
            "order_id": r[1] or "",
            "broker": r[2],
            "fill_time": r[3],
            "state": r[4],
            "exchange": r[5] or "",
        }
        for r in rows
    ]
    order_ids = sorted({f["order_id"] for f in fills if f["order_id"]})
    live, unknown = _load_live_tags(order_ids)
    engine_ids = _engine_known_order_ids(order_ids)
    # Same-day coverage only matters where the date rule does not already cover the fill.
    pending = {f["fill_time"].date() for f in fills if not is_covered(f["fill_time"], tag_min)}
    runs_by_day = _load_ok_runs(pending)
    starts: dict = {}

    def start_fn(day, exchange):
        if (day, exchange) not in starts:
            starts[(day, exchange)] = _session_start(day, exchange)
        return starts[(day, exchange)]

    changed = 0
    for f in fills:
        d = f["fill_time"].date()
        same_day = d in pending and same_day_covered(
            f["fill_time"], f["exchange"], runs_by_day.get(d, []), current_start, start_fn
        )
        state, strategy, reason, source = classify(
            f, live, unknown, engine_ids, tag_min, now, same_day=same_day
        )
        if state == f["state"] or state == ledger.STATE_UNRESOLVED:
            continue  # unchanged, or would be a downgrade
        if f["state"] == ledger.STATE_MANUAL:
            reason = f"upgraded from manual: {reason}"
        if ledger.set_attribution(f["id"], state, strategy, reason, source):
            changed += 1
    return changed


# ---------------------------------------------------------------- sync


def _record(broker, account, started, status, fetched=0, inserted=0, error=None, tag_min=None):
    try:
        ledger.record_sync_run(
            broker=broker or "",
            account=account or "",
            started_at=started,
            finished_at=now_ist(),
            status=status,
            fetched=fetched,
            inserted=inserted,
            error=error,
            tag_min_created_at=tag_min,
        )
    except Exception as e:  # recording must never raise into the caller
        logger.error("Could not record ledger sync run: %s", type(e).__name__)


def sync_once(user: str, now_fn: Callable[[], datetime] = now_ist) -> dict:
    """Sync one OpenAlgo user's broker account. Never raises; returns the run summary."""
    started = now_fn()
    broker = ""
    account = ""  # never the OpenAlgo user name (see resolve_account)
    token = None
    try:
        from database.auth_db import get_auth_token, get_auth_token_dbquery
        from utils.session import has_login_this_trading_session

        auth_obj = get_auth_token_dbquery(user)
        if auth_obj is None or getattr(auth_obj, "is_revoked", False):
            _record(broker, account, started, "no_auth", error="no active auth record")
            return {"status": "no_auth"}
        # Snapshot now: the ORM instance belongs to a scoped session released later.
        auth_obj = SimpleNamespace(
            name=auth_obj.name, user_id=auth_obj.user_id, broker=auth_obj.broker
        )
        broker = (auth_obj.broker or "").strip().lower()
        account = resolve_account(broker, user, auth_obj)
        reader = load_broker_reader(broker)
        if reader is not None and in_skip_window(started, reader.skip_window):
            _record(broker, account, started, "skipped_window")
            return {"status": "skipped_window"}
        token = get_auth_token(user)
        if not token or not has_login_this_trading_session(user):
            _record(broker, account, started, "no_auth", error="no valid token")
            return {"status": "no_auth"}

        try:
            fills = fetch_fills(broker, token, auth_obj, reader)
        except LedgerAuthError as e:
            _record(broker, account, started, "no_auth", error=_redact(str(e), (token,)))
            return {"status": "no_auth"}
        except NoPerFillDataError as e:
            _record(broker, account, started, "no_fill_data", error=_redact(str(e), (token,)))
            return {"status": "no_fill_data"}
        except Exception as e:  # BrokerBusyError and everything else
            _record(
                broker,
                account,
                started,
                "broker_error",
                error=_redact(f"{type(e).__name__}: {e}", (token,)),
            )
            return {"status": "broker_error"}

        skipped = int(getattr(fills, "skipped", 0) or 0)
        unmapped = min(int(getattr(fills, "unmapped", 0) or 0), skipped)
        unverifiable = skipped - unmapped
        parts = []
        if unmapped:
            parts.append(f"skipped_unmapped={unmapped}")
        if unverifiable:
            parts.append(f"unverifiable_skipped={unverifiable}")
        note = " ".join(parts) or None
        if skipped:
            logger.warning(
                "Discretionary ledger sync: broker=%s skipped_unmapped=%d unverifiable_skipped=%d",
                broker,
                unmapped,
                unverifiable,
            )
        reported = getattr(fills, "reported_accounts", None)
        if reported is None:  # a plain list: every account came from the broker
            reported = {f["account"] for f in fills if f.get("account")}
        if len(reported) == 1:
            remember_account(broker, user, next(iter(reported)))
        accounts = {f["account"] for f in fills}
        if len(accounts) == 1:
            account = next(iter(accounts))  # run record and ledger rows use one label
        assign_dedup(fills)
        inserted = ledger.insert_fills(fills)
        tag_min = get_tag_min_created_at()
        anchor = get_coverage_anchor(tag_min, started)
        # all brokers and accounts; the run in progress counts as a same-day heartbeat
        attribute_unresolved(None, None, anchor, started, current_start=started)
        status = "partial" if unmapped else "ok"
        _record(broker, account, started, status, len(fills), inserted, error=note, tag_min=tag_min)
        return {
            "status": status,
            "fetched": len(fills),
            "inserted": inserted,
            "skipped": skipped,
            "skipped_unmapped": unmapped,
        }
    except Exception as e:
        _record(
            broker, account, started, "error", error=_redact(f"{type(e).__name__}: {e}", (token,))
        )
        logger.error("Discretionary ledger sync failed: %s", type(e).__name__)
        return {"status": "error"}
    finally:
        try:
            from utils.db_sessions import remove_all_scoped_sessions

            remove_all_scoped_sessions()
        except Exception:
            pass


def list_sync_users() -> list[str]:
    """Names of non-revoked auth rows (candidates for sync)."""
    from database.auth_db import Auth, db_session

    try:
        return [a.name for a in Auth.query.filter_by(is_revoked=False).all() if a.name and a.broker]
    finally:
        db_session.remove()


def sync_all(now_fn: Callable[[], datetime] = now_ist) -> list[dict]:
    """Sync every account; one failure never stops the others."""
    results = []
    try:
        users = list_sync_users()
    except Exception as e:
        logger.error("Discretionary ledger could not list accounts: %s", type(e).__name__)
        return [{"status": "error"}]
    for u in users:
        try:
            results.append(sync_once(u, now_fn))
        except Exception as e:  # sync_once already swallows; belt and braces
            logger.error("Discretionary ledger sync crashed: %s", type(e).__name__)
            results.append({"status": "error"})
    return results
