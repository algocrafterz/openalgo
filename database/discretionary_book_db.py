# database/discretionary_book_db.py
"""
Derived tables of the Discretionary Trading Performance module (Stage 2).

Everything here is DERIVED from ``ledger_fills`` and can be thrown away and rebuilt:

* ``disc_positions``   one row per position episode (identity + position_seq), OPEN or FLAT
* ``disc_trades``      one row per closed trade (a partial exit is its own row)
* ``disc_trade_fills`` which ledger fill (and how much of it) each trade / open position used
* ``disc_orphans``     fills the engine refused (invalid, or reducing an unknown opening)
* ``disc_build_meta``  one row: the build that the rows above belong to, with the ledger
                       fingerprint it was built from

``replace_build`` swaps the whole derived set in ONE transaction (delete everything, insert
the new build), so a reader never sees a half-built state and a failed build leaves the
previous one untouched. The ledger tables are never written here.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    delete,
    select,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)

_write_lock = threading.Lock()

engine = create_db_engine()
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()

TABLES = ("disc_positions", "disc_trades", "disc_trade_fills", "disc_orphans", "disc_build_meta")


class DiscPosition(Base):
    __tablename__ = "disc_positions"

    id = Column(Integer, primary_key=True)
    build_id = Column(String(40), nullable=False)
    broker = Column(String(40), nullable=False)
    account = Column(String(64), nullable=False, default="")
    exchange = Column(String(20), nullable=False, default="")
    symbol = Column(String(80), nullable=False)
    product = Column(String(20), nullable=False, default="")
    position_seq = Column(Integer, nullable=False)
    direction = Column(String(5), nullable=False)  # LONG / SHORT
    open_qty = Column(Float, nullable=False)
    avg_entry = Column(Float, nullable=False)
    opened_at = Column(DateTime, nullable=False)
    opened_session = Column(String(10), nullable=False)
    realized_gross = Column(Float, nullable=False, default=0.0)
    entry_charges_carried = Column(Float, nullable=True)
    opening_unknown = Column(Boolean, nullable=False, default=False)
    status = Column(String(5), nullable=False)  # OPEN / FLAT

    __table_args__ = (
        Index("ix_disc_positions_identity", "broker", "account", "exchange", "symbol", "product"),
        Index("ix_disc_positions_status", "status"),
    )


class DiscTrade(Base):
    __tablename__ = "disc_trades"

    id = Column(Integer, primary_key=True)
    build_id = Column(String(40), nullable=False)
    broker = Column(String(40), nullable=False)
    account = Column(String(64), nullable=False, default="")
    exchange = Column(String(20), nullable=False, default="")
    symbol = Column(String(80), nullable=False)
    product = Column(String(20), nullable=False, default="")
    position_seq = Column(Integer, nullable=False)
    direction = Column(String(5), nullable=False)
    closed_quantity = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, nullable=False)
    trade_date = Column(String(10), nullable=False)
    overnight = Column(Boolean, nullable=False, default=False)
    hold_minutes = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=False)
    charges = Column(Float, nullable=True)
    charges_status = Column(String(12), nullable=False)  # ok / unavailable
    net_pnl = Column(Float, nullable=True)
    charges_breakdown_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_disc_trades_identity", "broker", "account", "exchange", "symbol", "product"),
        Index("ix_disc_trades_trade_date", "trade_date"),
    )


class DiscTradeFill(Base):
    __tablename__ = "disc_trade_fills"

    id = Column(Integer, primary_key=True)
    build_id = Column(String(40), nullable=False)
    trade_id = Column(Integer, nullable=True)
    position_id = Column(Integer, nullable=True)
    fill_ref = Column(Integer, nullable=False)  # ledger_fills.id
    role = Column(String(5), nullable=False)  # ENTRY / EXIT
    qty_used = Column(Float, nullable=False)
    charge_allocated = Column(Float, nullable=True)

    __table_args__ = (
        Index("ix_disc_trade_fills_trade", "trade_id"),
        Index("ix_disc_trade_fills_position", "position_id"),
        Index("ix_disc_trade_fills_fill", "fill_ref"),
    )


class DiscOrphan(Base):
    __tablename__ = "disc_orphans"

    id = Column(Integer, primary_key=True)
    build_id = Column(String(40), nullable=False)
    fill_ref = Column(Integer, nullable=True)  # ledger_fills.id
    reason = Column(String(60), nullable=False)
    qty = Column(Float, nullable=False, default=0.0)
    broker = Column(String(40), nullable=True)
    account = Column(String(64), nullable=True)
    exchange = Column(String(20), nullable=True)
    symbol = Column(String(80), nullable=True)
    product = Column(String(20), nullable=True)
    side = Column(String(4), nullable=True)
    price = Column(Float, nullable=True)
    fill_time = Column(DateTime, nullable=True)


class DiscBuildMeta(Base):
    __tablename__ = "disc_build_meta"

    build_id = Column(String(40), primary_key=True)
    built_at = Column(DateTime, nullable=False)
    fingerprint = Column(String(64), nullable=False)
    fill_count = Column(Integer, nullable=False)
    engine_version = Column(String(20), nullable=False)


_POSITION_COLS = {
    "broker",
    "account",
    "exchange",
    "symbol",
    "product",
    "position_seq",
    "direction",
    "open_qty",
    "avg_entry",
    "opened_at",
    "opened_session",
    "realized_gross",
    "entry_charges_carried",
    "opening_unknown",
    "status",
}
_TRADE_COLS = {
    "broker",
    "account",
    "exchange",
    "symbol",
    "product",
    "position_seq",
    "direction",
    "closed_quantity",
    "entry_price",
    "exit_price",
    "opened_at",
    "closed_at",
    "trade_date",
    "overnight",
    "hold_minutes",
    "realized_pnl",
    "charges",
    "charges_status",
    "net_pnl",
}
_ORPHAN_COLS = {
    "fill_ref",
    "reason",
    "qty",
    "broker",
    "account",
    "exchange",
    "symbol",
    "product",
    "side",
    "price",
    "fill_time",
}


def init_discretionary_book_db() -> None:
    """Create the derived tables if absent. Idempotent; never drops or alters rows."""
    Base.metadata.create_all(bind=engine)
    logger.debug("Discretionary book DB initialized")


def replace_build(
    meta: dict,
    positions: list[dict],
    trades: list[dict],
    links: list[dict],
    orphans: list[dict],
) -> None:
    """Atomically replace the whole derived set with a new build.

    ``links`` use ``trade_idx`` / ``pos_idx`` (indexes into ``trades`` / ``positions``); the
    real row ids are assigned here. One transaction: on any error nothing changes.
    """
    build_id = meta["build_id"]
    with _write_lock:
        try:
            for table in (DiscTradeFill, DiscTrade, DiscPosition, DiscOrphan, DiscBuildMeta):
                db_session.execute(delete(table))
            pos_rows = [
                DiscPosition(
                    build_id=build_id,
                    **{k: v for k, v in p.items() if k in _POSITION_COLS},
                )
                for p in positions
            ]
            trade_rows = []
            for t in trades:
                values = {k: v for k, v in t.items() if k in _TRADE_COLS}
                values["charges_breakdown_json"] = json.dumps(
                    t.get("charges_breakdown") or {}, sort_keys=True
                )
                trade_rows.append(DiscTrade(build_id=build_id, **values))
            db_session.add_all(pos_rows)
            db_session.add_all(trade_rows)
            db_session.flush()  # assigns ids
            for link in links:
                db_session.add(
                    DiscTradeFill(
                        build_id=build_id,
                        trade_id=trade_rows[link["trade_idx"]].id if "trade_idx" in link else None,
                        position_id=pos_rows[link["pos_idx"]].id if "pos_idx" in link else None,
                        fill_ref=link["fill_ref"],
                        role=link["role"],
                        qty_used=link["qty_used"],
                        charge_allocated=link.get("charge_allocated"),
                    )
                )
            for o in orphans:
                db_session.add(
                    DiscOrphan(
                        build_id=build_id,
                        **{k: v for k, v in o.items() if k in _ORPHAN_COLS},
                    )
                )
            db_session.add(DiscBuildMeta(**meta))
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.remove()


def _iso(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _as_dict(row) -> dict:
    return {c.name: _iso(getattr(row, c.name)) for c in row.__table__.columns}


def _filtered(model, filters: dict, build_id: str | None = None):
    stmt = select(model)
    if build_id:
        stmt = stmt.where(model.build_id == build_id)
    for name in ("broker", "account", "symbol", "exchange", "product"):
        if filters.get(name):
            stmt = stmt.where(getattr(model, name) == filters[name])
    return stmt


def get_meta() -> dict | None:
    try:
        row = db_session.execute(select(DiscBuildMeta)).scalars().first()
        if row is None:
            return None
        return {
            "build_id": row.build_id,
            "built_at": row.built_at,
            "fingerprint": row.fingerprint,
            "fill_count": row.fill_count,
            "engine_version": row.engine_version,
        }
    finally:
        db_session.remove()


def load_trades(
    filters: dict | None = None,
    start: str | None = None,
    end: str | None = None,
    build_id: str | None = None,
):
    """Trade rows (dicts) in close order, optionally filtered; dates are ISO strings.

    ``build_id`` restricts the read to one build (an empty result when it was replaced).
    """
    filters = filters or {}
    try:
        stmt = _filtered(DiscTrade, filters, build_id)
        if start:
            stmt = stmt.where(DiscTrade.trade_date >= start)
        if end:
            stmt = stmt.where(DiscTrade.trade_date <= end)
        stmt = stmt.order_by(DiscTrade.closed_at, DiscTrade.id)
        out = []
        for row in db_session.execute(stmt).scalars():
            d = _as_dict(row)
            raw = d.pop("charges_breakdown_json", None)
            d["charges_breakdown"] = json.loads(raw) if raw else {}
            out.append(d)
        return out
    finally:
        db_session.remove()


def load_positions(
    filters: dict | None = None, status: str | None = None, build_id: str | None = None
) -> list[dict]:
    filters = filters or {}
    try:
        stmt = _filtered(DiscPosition, filters, build_id)
        if status:
            stmt = stmt.where(DiscPosition.status == status)
        stmt = stmt.order_by(
            DiscPosition.broker,
            DiscPosition.account,
            DiscPosition.exchange,
            DiscPosition.symbol,
            DiscPosition.product,
            DiscPosition.position_seq,
        )
        return [_as_dict(r) for r in db_session.execute(stmt).scalars()]
    finally:
        db_session.remove()


def load_orphans(filters: dict | None = None, build_id: str | None = None) -> list[dict]:
    filters = filters or {}
    try:
        stmt = _filtered(DiscOrphan, filters, build_id).order_by(
            DiscOrphan.fill_time, DiscOrphan.id
        )
        return [_as_dict(r) for r in db_session.execute(stmt).scalars()]
    finally:
        db_session.remove()


def load_links() -> list[dict]:
    try:
        rows = db_session.execute(select(DiscTradeFill).order_by(DiscTradeFill.id)).scalars()
        return [_as_dict(r) for r in rows]
    finally:
        db_session.remove()


def load_trade(trade_id: int, build_id: str | None = None) -> dict | None:
    """One trade row (dict) by id, scoped to ``build_id`` when given; None when absent."""
    try:
        stmt = select(DiscTrade).where(DiscTrade.id == trade_id)
        if build_id:
            stmt = stmt.where(DiscTrade.build_id == build_id)
        row = db_session.execute(stmt).scalars().first()
        if row is None:
            return None
        d = _as_dict(row)
        raw = d.pop("charges_breakdown_json", None)
        d["charges_breakdown"] = json.loads(raw) if raw else {}
        return d
    finally:
        db_session.remove()


def load_trade_fills(trade_id: int, build_id: str) -> list[dict]:
    """The link rows (ENTRY / EXIT, qty_used, charge_allocated, fill_ref) of ONE trade in ONE
    build, oldest link first. Scoped by both, so a link of a replaced build is never returned."""
    try:
        stmt = (
            select(DiscTradeFill)
            .where(DiscTradeFill.trade_id == trade_id, DiscTradeFill.build_id == build_id)
            .order_by(DiscTradeFill.id)
        )
        return [_as_dict(r) for r in db_session.execute(stmt).scalars()]
    finally:
        db_session.remove()
