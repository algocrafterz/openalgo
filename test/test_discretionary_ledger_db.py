"""Discretionary ledger tables: unique key, idempotent insert, rows survive re-init.

All tests use a temp SQLite file; nothing touches a real database or broker.
Run with: uv run pytest test/test_discretionary_ledger_db.py -q
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from database import discretionary_ledger_db as ledger


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'ledger.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    original = ledger.engine
    ledger.db_session.remove()
    ledger.db_session.configure(bind=eng)
    monkeypatch.setattr(ledger, "engine", eng)
    ledger.init_discretionary_ledger_db()
    yield eng
    ledger.db_session.remove()
    ledger.db_session.configure(bind=original)


def _fill(key="flattrade|A1|F1", **kw):
    row = {
        "broker": "flattrade",
        "account": "A1",
        "order_id": "O1",
        "fill_id": "F1",
        "symbol": "SBIN-EQ",
        "exchange": "NSE",
        "product": "I",
        "side": "BUY",
        "qty": 10.0,
        "price": 100.5,
        "fill_time": datetime(2026, 11, 17, 10, 0, 0),
        "occurrence_idx": 0,
        "dedup_key": key,
        "attribution_state": "unresolved",
        "strategy": None,
        "synced_at": datetime(2026, 11, 17, 10, 5, 0),
        "raw_hash": "h",
    }
    row.update(kw)
    return row


def _count(eng, table="ledger_fills"):
    with eng.connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def test_tables_and_indexes_exist(env):
    with env.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master"))}
    assert {"ledger_fills", "ledger_sync_runs", "ledger_attribution_audit"} <= names
    assert "ix_ledger_fills_broker_account_time" in names
    assert "ix_ledger_fills_attribution_state" in names
    assert "ix_ledger_fills_order_id" in names


def test_dedup_key_is_unique_at_the_database(env):
    ledger.db_session.add(ledger.LedgerFill(**_fill()))
    ledger.db_session.commit()
    ledger.db_session.add(ledger.LedgerFill(**_fill()))
    with pytest.raises(IntegrityError):
        ledger.db_session.commit()
    ledger.db_session.rollback()
    ledger.db_session.remove()


def test_insert_fills_is_idempotent(env):
    batch = [_fill("k1"), _fill("k2"), _fill("k2")]  # duplicate inside the batch
    assert ledger.insert_fills(batch) == 2
    assert ledger.insert_fills(batch) == 0
    assert _count(env) == 2


def test_insert_survives_a_lost_race(env, monkeypatch):
    """A key inserted by another writer between the check and the insert is ignored."""
    ledger.insert_fills([_fill("k1")])
    original = ledger.db_session.execute

    calls = {"n": 0}

    def blind_first_select(stmt, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:  # pretend the existing-key lookup saw nothing

            class _Empty:
                def all(self):
                    return []

            return _Empty()
        return original(stmt, *a, **kw)

    monkeypatch.setattr(ledger.db_session, "execute", blind_first_select)
    assert ledger.insert_fills([_fill("k1"), _fill("k3")]) == 1
    monkeypatch.undo()
    assert _count(env) == 2


def test_rows_survive_reinit(env):
    ledger.insert_fills([_fill("k1")])
    ledger.init_discretionary_ledger_db()
    ledger.init_discretionary_ledger_db()
    assert _count(env) == 1


def test_set_attribution_changes_only_state_and_strategy_and_writes_audit(env):
    ledger.insert_fills([_fill("k1")])
    assert ledger.set_attribution(1, "strategy_tag", "S1", "live order tag", "tag")
    with env.connect() as c:
        row = c.execute(
            text("SELECT attribution_state, strategy, qty, price, symbol FROM ledger_fills")
        ).one()
        audit = c.execute(
            text("SELECT fill_ref, from_state, to_state, source FROM ledger_attribution_audit")
        ).all()
    assert tuple(row) == ("strategy_tag", "S1", 10.0, 100.5, "SBIN-EQ")
    assert [tuple(a) for a in audit] == [(1, "unresolved", "strategy_tag", "tag")]


def test_state_never_downgrades(env):
    ledger.insert_fills([_fill("k1")])
    assert ledger.set_attribution(1, "ui_manual", "UI Exit Position", "r", "tag")
    for target in ("unresolved", "manual", "strategy_tag", "strategy_engine"):
        assert not ledger.set_attribution(1, target, None, "r", "none")
    assert _count(env, "ledger_attribution_audit") == 1


def test_unknown_state_rejected(env):
    ledger.insert_fills([_fill("k1")])
    with pytest.raises(ValueError):
        ledger.set_attribution(1, "bogus", None, "r", "none")


def test_record_sync_run(env):
    ledger.record_sync_run(
        broker="flattrade",
        account="A1",
        started_at=datetime(2026, 11, 17, 10, 0),
        finished_at=datetime(2026, 11, 17, 10, 1),
        status="ok",
        fetched=3,
        inserted=2,
        error=None,
        tag_min_created_at=None,
    )
    assert _count(env, "ledger_sync_runs") == 1


# ---------------------------------------------------------------- account identity


def test_identity_table_is_unique_per_broker_and_login(env):
    assert ledger.remember_identity("flattrade", "alice", "FZ01") is True
    assert ledger.remember_identity("flattrade", "alice", "FZ01") is False  # unchanged
    assert ledger.remember_identity("zerodha", "alice", "ZR01") is True
    assert ledger.get_identity("flattrade", "alice") == "FZ01"
    assert ledger.auth_names_for_account("flattrade", "FZ01") == ["alice"]
    assert ledger.remember_identity("flattrade", "alice", "") is False
    with pytest.raises(IntegrityError):
        with env.begin() as c:
            c.execute(
                text(
                    "INSERT INTO ledger_account_identity (broker, auth_name, account, first_seen)"
                    " VALUES ('flattrade', 'alice', 'X', '2026-11-18 09:00:00')"
                )
            )
    assert _count(env, "ledger_account_identity") == 2


def test_relabel_sync_runs_touches_runs_and_identity_only(env):
    for acct in ("alice", "alice", "FZ01"):
        ledger.record_sync_run(
            broker="flattrade",
            account=acct,
            started_at=datetime(2026, 11, 18, 9, 0),
            status="ok",
            fetched=0,
            inserted=0,
        )
    ledger.insert_fills([_fill(account="alice")])
    assert ledger.relabel_sync_runs("flattrade", "alice", "FZ01") == 2
    with env.connect() as c:
        labels = [r[0] for r in c.execute(text("SELECT account FROM ledger_sync_runs"))]
        fill_acct = c.execute(text("SELECT account FROM ledger_fills")).scalar()
    assert labels == ["FZ01"] * 3 and fill_acct == "alice"
    assert ledger.get_identity("flattrade", "alice") == "FZ01"
    assert ledger.relabel_sync_runs("flattrade", "alice", "FZ01") == 0


def _schema(path):
    import sqlite3

    con = sqlite3.connect(path)
    try:
        return sorted(
            (r[0], r[1], r[2] or "")
            for r in con.execute("SELECT type, name, sql FROM sqlite_master")
        )
    finally:
        con.close()


def test_ledger_migration_creates_identity_table_and_is_idempotent(tmp_path, monkeypatch):
    import importlib.util
    import pathlib

    path = pathlib.Path(ledger.__file__).resolve().parent.parent / "upgrade"
    monkeypatch.syspath_prepend(str(path))  # the script imports its sibling _pragmas
    spec = importlib.util.spec_from_file_location(
        "migrate_ledger", path / "migrate_discretionary_ledger.py"
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    db = str(tmp_path / "m.db")
    assert mig.run(db) == 0
    first = _schema(db)
    assert mig.run(db) == 0
    assert _schema(db) == first
    assert "ledger_account_identity" in {name for _t, name, _s in first}
    # the ORM create_all on the migrated DB adds nothing either
    eng = create_engine(f"sqlite:///{db}")
    ledger.Base.metadata.create_all(eng)
    eng.dispose()
    assert _schema(db) == first
