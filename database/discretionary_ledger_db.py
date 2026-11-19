# database/discretionary_ledger_db.py
"""
Append-only ledger of broker fills for the Discretionary Trading Performance module.

Stage 1 only: it records what the broker reports and says who placed it. There is no
accounting, no P&L and no charges here.

Rules:

* Original broker values (symbol, qty, price, time ...) are never updated. The only
  mutable columns of ``ledger_fills`` are ``attribution_state`` and ``strategy``, and
  every change to them is mirrored by an append-only ``ledger_attribution_audit`` row, plus
  ``source_evidence``: the latest source evidence of the fill's broker order (derived, never a
  broker value; NULL until a sync with DISCRETIONARY_SOURCE_FROM_ORDERBOOK records some). It is
  not part of ``dedup_key`` or ``raw_hash``. Each audit row carries the evidence label behind
  that state change in ``evidence`` (NULL on rows written before the column existed).
* Rows are never deleted.
* ``ledger_account_identity`` maps an OpenAlgo login (broker, auth name) to the account id
  the broker reports on its fills; the only rewrite of ``ledger_sync_runs.account`` is the
  explicit repair (``relabel_sync_runs``) that replaces a user-name label with that id.
* Inserts are idempotent on ``dedup_key`` (UNIQUE). With a broker fill id the key is
  ``broker|account|exchange|YYYY-MM-DD|order_id|fill_id`` (the exchange trade number may
  be unique only per instrument, so the order id is part of the key); without one it is ``broker|account|exchange|order_id|symbol|side|
  qty|price|time|occurrence_idx``. Only the key shape matters here; it is built by the
  service, and the column stays a single UNIQUE String(400) (no schema change).
* The module is read-only toward the broker; nothing here talks to a broker.
"""

import threading
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)

# Attribution states. strategy_* and ui_manual are terminal; manual outranks unresolved
# and may still be upgraded to strategy_tag / strategy_engine / ui_manual (late tag).
STATE_STRATEGY_TAG = "strategy_tag"
STATE_STRATEGY_ENGINE = "strategy_engine"
STATE_UI_MANUAL = "ui_manual"
STATE_MANUAL = "manual"
STATE_UNRESOLVED = "unresolved"
ATTRIBUTION_STATES = (
    STATE_STRATEGY_TAG,
    STATE_STRATEGY_ENGINE,
    STATE_UI_MANUAL,
    STATE_MANUAL,
    STATE_UNRESOLVED,
)
_STATE_RANK = {
    STATE_UNRESOLVED: 0,
    STATE_MANUAL: 1,
    STATE_STRATEGY_TAG: 2,
    STATE_STRATEGY_ENGINE: 2,
    STATE_UI_MANUAL: 2,
}

# no_fill_data: the broker module has no map_trade_fills (per-fill reader).
# partial: the run stored what it could but the mapping layer lost >=1 row (symbol or
# product could not be mapped to OpenAlgo); the error text carries skipped_unmapped=N.
SYNC_STATUSES = (
    "ok",
    "no_auth",
    "broker_error",
    "skipped_window",
    "error",
    "no_fill_data",
    "partial",
)
AUDIT_SOURCES = ("tag", "signal_engine", "none", "user", "remarks", "ordersource")

# Nullable columns added after the first release: (table, column, SQL type). Added by
# init_discretionary_ledger_db and upgrade/migrate_discretionary_ledger.py (plain ADD COLUMN,
# existing rows stay NULL; running it twice changes nothing).
ADDED_COLUMNS = (
    ("ledger_fills", "source_evidence", "TEXT"),
    ("ledger_attribution_audit", "evidence", "VARCHAR(200)"),
)

_write_lock = threading.Lock()

engine = create_db_engine()
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class LedgerFill(Base):
    __tablename__ = "ledger_fills"

    id = Column(Integer, primary_key=True)
    broker = Column(String(40), nullable=False)
    account = Column(String(64), nullable=False, default="")
    order_id = Column(String(64), nullable=False, default="")
    fill_id = Column(String(64), nullable=True)
    symbol = Column(String(80), nullable=False)
    exchange = Column(String(20), nullable=False, default="")
    product = Column(String(20), nullable=False, default="")
    side = Column(String(4), nullable=False)  # BUY / SELL
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fill_time = Column(DateTime, nullable=False)  # IST, naive
    occurrence_idx = Column(Integer, nullable=False, default=0)
    dedup_key = Column(String(400), nullable=False, unique=True)
    attribution_state = Column(String(20), nullable=False, default=STATE_UNRESOLVED)
    strategy = Column(String(120), nullable=True)
    synced_at = Column(DateTime, nullable=False, default=datetime.now)
    raw_hash = Column(String(64), nullable=True)
    # Source evidence of the fill's broker order (compact JSON, see the ledger service); NULL
    # when none was recorded. Derived, mutable, outside dedup_key / raw_hash.
    source_evidence = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_ledger_fills_broker_account_time", "broker", "account", "fill_time"),
        Index("ix_ledger_fills_attribution_state", "attribution_state"),
        Index("ix_ledger_fills_order_id", "order_id"),
    )


class LedgerSyncRun(Base):
    __tablename__ = "ledger_sync_runs"

    id = Column(Integer, primary_key=True)
    broker = Column(String(40), nullable=False, default="")
    account = Column(String(64), nullable=False, default="")
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False)
    fetched = Column(Integer, nullable=False, default=0)
    inserted = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    # MIN(created_at) of live StrategyOrderTag rows at sync time (NULL if none). Informational;
    # NOT the coverage start: that is derived on the fly (see discretionary_ledger_service).
    tag_min_created_at = Column(DateTime, nullable=True)


class LedgerAttributionAudit(Base):
    """Append-only: one row per attribution change."""

    __tablename__ = "ledger_attribution_audit"

    id = Column(Integer, primary_key=True)
    fill_ref = Column(Integer, nullable=False, index=True)  # ledger_fills.id
    dedup_key = Column(String(400), nullable=False)
    from_state = Column(String(20), nullable=True)
    to_state = Column(String(20), nullable=False)
    reason = Column(String(200), nullable=False, default="")
    source = Column(String(20), nullable=False, default="none")
    at = Column(DateTime, nullable=False, default=datetime.now)
    evidence = Column(String(200), nullable=True)  # evidence label of this change


class LedgerAccountIdentity(Base):
    """Canonical broker account of an OpenAlgo login, learnt from the broker's own fills.

    One row per (broker, auth_name). ``account`` is the account id the broker reported
    (e.g. the broker's account id field), never the OpenAlgo user name.
    """

    __tablename__ = "ledger_account_identity"

    id = Column(Integer, primary_key=True)
    broker = Column(String(40), nullable=False)
    auth_name = Column(String(255), nullable=False)
    account = Column(String(64), nullable=False)
    first_seen = Column(DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("broker", "auth_name", name="uq_ledger_account_identity_broker_auth"),
    )


def get_identity(broker: str, auth_name: str) -> str | None:
    """Canonical account remembered for (broker, auth_name); None when unknown."""
    try:
        return db_session.execute(
            select(LedgerAccountIdentity.account).where(
                LedgerAccountIdentity.broker == broker,
                LedgerAccountIdentity.auth_name == auth_name,
            )
        ).scalar()
    finally:
        db_session.remove()


def auth_names_for_account(broker: str, account: str) -> list[str]:
    """Auth names whose remembered canonical account on ``broker`` is ``account``."""
    try:
        rows = db_session.execute(
            select(LedgerAccountIdentity.auth_name).where(
                LedgerAccountIdentity.broker == broker,
                LedgerAccountIdentity.account == account,
            )
        ).all()
        return [r[0] for r in rows]
    finally:
        db_session.remove()


def remember_identity(broker: str, auth_name: str, account: str) -> bool:
    """Store or update the canonical account of (broker, auth_name). True when written."""
    if not (broker and auth_name and account):
        return False
    with _write_lock:
        try:
            row = db_session.execute(
                select(LedgerAccountIdentity).where(
                    LedgerAccountIdentity.broker == broker,
                    LedgerAccountIdentity.auth_name == auth_name,
                )
            ).scalar()
            if row is not None and row.account == account:
                return False
            if row is None:
                db_session.add(
                    LedgerAccountIdentity(
                        broker=broker,
                        auth_name=auth_name,
                        account=account,
                        first_seen=datetime.now(),
                    )
                )
            else:
                logger.warning(
                    "Discretionary ledger: canonical account of a login changed on broker %s",
                    broker,
                )
                row.account = account
                row.first_seen = datetime.now()
            db_session.commit()
            return True
        except IntegrityError:
            db_session.rollback()  # a concurrent writer stored it first
            return False
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.remove()


def relabel_sync_runs(broker: str, auth_name: str, account: str) -> int:
    """Repair: runs of ``broker`` labelled ``auth_name`` get ``account``; identity row stored.

    One transaction. Never touches ledger_fills. Returns the number of runs relabelled.
    """
    if not (broker and auth_name and account) or auth_name == account:
        return 0
    with _write_lock:
        try:
            res = db_session.execute(
                update(LedgerSyncRun)
                .where(LedgerSyncRun.broker == broker, LedgerSyncRun.account == auth_name)
                .values(account=account)
            )
            row = db_session.execute(
                select(LedgerAccountIdentity).where(
                    LedgerAccountIdentity.broker == broker,
                    LedgerAccountIdentity.auth_name == auth_name,
                )
            ).scalar()
            if row is None:
                db_session.add(
                    LedgerAccountIdentity(
                        broker=broker,
                        auth_name=auth_name,
                        account=account,
                        first_seen=datetime.now(),
                    )
                )
            db_session.commit()
            return int(res.rowcount or 0)
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.remove()


def add_missing_columns(bind=None) -> list[str]:
    """ADD COLUMN for each ADDED_COLUMNS entry whose table exists without it. Idempotent.

    Returns "table.column" of the columns added. Existing rows get NULL; nothing is rewritten.
    """
    from sqlalchemy import inspect, text

    bind = bind or engine
    added = []
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table, column, sql_type in ADDED_COLUMNS:
        if table not in tables:
            continue
        if column in {c["name"] for c in inspector.get_columns(table)}:
            continue
        with bind.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        added.append(f"{table}.{column}")
        logger.info("Discretionary ledger DB: added column %s.%s", table, column)
    return added


def init_discretionary_ledger_db() -> None:
    """Create the ledger tables if absent and add later nullable columns. Idempotent; never
    drops or rewrites rows."""
    Base.metadata.create_all(bind=engine)
    add_missing_columns(engine)
    logger.debug("Discretionary ledger DB initialized")


def insert_fills(fills: list[dict]) -> int:
    """Insert fills not already present (by dedup_key). Returns the number inserted.

    Each dict needs the LedgerFill column names. Safe to call repeatedly with the same
    data and safe against a concurrent writer: a unique-key collision is swallowed.
    """
    if not fills:
        return 0
    with _write_lock:
        try:
            keys = [f["dedup_key"] for f in fills]
            existing: set[str] = set()
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                rows = db_session.execute(
                    select(LedgerFill.dedup_key).where(LedgerFill.dedup_key.in_(chunk))
                ).all()
                existing.update(r[0] for r in rows)
            new = []
            seen: set[str] = set()
            for f in fills:
                if f["dedup_key"] in existing or f["dedup_key"] in seen:
                    continue
                seen.add(f["dedup_key"])
                new.append(f)
            if not new:
                return 0
            try:
                for f in new:
                    db_session.add(LedgerFill(**f))
                db_session.commit()
                return len(new)
            except IntegrityError:
                db_session.rollback()
            # Lost a race on some key: fall back to row-by-row, ignoring collisions.
            inserted = 0
            for f in new:
                try:
                    db_session.add(LedgerFill(**f))
                    db_session.commit()
                    inserted += 1
                except IntegrityError:
                    db_session.rollback()
            return inserted
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.remove()


def can_change_state(from_state: str, to_state: str) -> bool:
    """Only upgrades (higher rank) are allowed; strategy_*/ui_manual are never changed."""
    return _STATE_RANK.get(to_state, -1) > _STATE_RANK.get(from_state, 99)


def set_attribution(
    fill_ref: int,
    to_state: str,
    strategy: str | None,
    reason: str,
    source: str,
    evidence: str | None = None,
) -> bool:
    """Move one fill to ``to_state`` and append an audit row. False if not allowed/changed.

    ``evidence``: the evidence label of the change, stored on the audit row (None: none).

    The UPDATE is conditional on the state still being what was read, so two writers
    cannot both apply a transition.
    """
    if to_state not in ATTRIBUTION_STATES:
        raise ValueError(f"unknown attribution state {to_state!r}")
    with _write_lock:
        try:
            row = db_session.execute(
                select(LedgerFill.attribution_state, LedgerFill.dedup_key).where(
                    LedgerFill.id == fill_ref
                )
            ).first()
            if row is None:
                return False
            from_state, dedup_key = row
            if not can_change_state(from_state, to_state):
                return False
            res = db_session.execute(
                update(LedgerFill)
                .where(LedgerFill.id == fill_ref, LedgerFill.attribution_state == from_state)
                .values(attribution_state=to_state, strategy=strategy)
            )
            if res.rowcount != 1:
                db_session.rollback()
                return False
            db_session.add(
                LedgerAttributionAudit(
                    fill_ref=fill_ref,
                    dedup_key=dedup_key,
                    from_state=from_state,
                    to_state=to_state,
                    reason=(reason or "")[:200],
                    source=source,
                    at=datetime.now(),
                    evidence=(evidence or None) and evidence[:200],
                )
            )
            db_session.commit()
            return True
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.remove()


def set_source_evidence(
    broker: str, order_id: str, evidence: str, only_if_null: bool = False
) -> int:
    """Store ``evidence`` on every fill of (broker, order_id) whose stored value differs.

    Touches only ``source_evidence``; returns the number of rows changed. Partial fills of one
    order share it, so a fill inserted later inherits the evidence at the next write.
    ``only_if_null``: write only rows that have no evidence yet.
    """
    if not (broker and order_id and evidence):
        return 0
    from sqlalchemy import or_

    if only_if_null:
        cond = LedgerFill.source_evidence.is_(None)
    else:
        cond = or_(LedgerFill.source_evidence.is_(None), LedgerFill.source_evidence != evidence)
    with _write_lock:
        try:
            res = db_session.execute(
                update(LedgerFill)
                .where(LedgerFill.broker == broker, LedgerFill.order_id == order_id, cond)
                .values(source_evidence=evidence)
            )
            db_session.commit()
            return int(res.rowcount or 0)
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.remove()


def record_sync_run(**fields) -> None:
    """Append one sync-run row (short transaction)."""
    with _write_lock:
        try:
            db_session.add(LedgerSyncRun(**fields))
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.remove()
