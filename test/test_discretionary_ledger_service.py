"""Discretionary ledger sync + attribution, with every broker/network call mocked.

Flattrade TradeBook fields: see test_flattrade_tradebook_live_shape.py (real-shaped,
masked sample) and test_flattrade_tradebook_raw.py. Fills here are fed in already normalized, or via a
mocked get_trade_book. Temp SQLite files only.

Run with: uv run pytest test/test_discretionary_ledger_service.py -q
"""

import ast
import copy
import pathlib
import sqlite3
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from database import discretionary_ledger_db as ledger
from database import strategy_book_db as sb
from services import discretionary_ledger_service as svc
from utils.broker_backpressure import BrokerBusyError

NOW = datetime(2026, 11, 17, 11, 0, 0)
TODAY = datetime(2026, 11, 17, 10, 30, 0)
TAG_START = datetime(2026, 11, 10, 9, 30, 0)  # coverage starts 2026-11-11


def now_fn():
    return NOW


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'db.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    orig_l, orig_s = ledger.engine, sb.engine
    for mod in (ledger, sb):
        mod.db_session.remove()
        mod.db_session.configure(bind=eng)
        mod.Base.metadata.create_all(eng)
    monkeypatch.setattr(ledger, "engine", eng)
    # signal_engine trades.db stand-in
    se = tmp_path / "trades.db"
    con = sqlite3.connect(se)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, order_id TEXT, strategy TEXT)")
    con.commit()
    con.close()
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", str(se))
    yield SimpleNamespace(engine=eng, se_path=str(se))
    for mod, o in ((ledger, orig_l), (sb, orig_s)):
        mod.db_session.remove()
        mod.db_session.configure(bind=o)


def add_tag(env, orderid, strategy="S1", mode="live", broker="flattrade", created=TAG_START):
    with env.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO strategy_order_tags (orderid,user_id,strategy,symbol,exchange,product,"
                "mode,broker,applied_quantity,applied_notional,created_at) VALUES "
                "(:o,'u',:s,'SBIN','NSE','MIS',:m,:b,0,0,:c)"
            ),
            {"o": orderid, "s": strategy, "m": mode, "b": broker, "c": created},
        )


def add_engine_trade(env, order_id):
    con = sqlite3.connect(env.se_path)
    con.execute("INSERT INTO trades (order_id, strategy) VALUES (?, 's')", (order_id,))
    con.commit()
    con.close()


def mk(
    order_id="O1",
    fill_id=None,
    broker="flattrade",
    account="A1",
    qty=10.0,
    price=100.0,
    when=TODAY,
    side="BUY",
    symbol="SBIN-EQ",
    exchange="NSE",
):
    return {
        "broker": broker,
        "account": account,
        "order_id": order_id,
        "fill_id": fill_id,
        "symbol": symbol,
        "exchange": exchange,
        "product": "I",
        "side": side,
        "qty": qty,
        "price": price,
        "fill_time": when,
        "raw_hash": None,
    }


def rows(env, sql="SELECT * FROM ledger_fills ORDER BY id"):
    with env.engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def sync(fills, user="alice", broker="flattrade", token="TOKEN-MASKED", now=now_fn):
    """Run sync_once for one mocked account whose broker returns `fills`."""
    auth = SimpleNamespace(name=user, user_id=f"id-{user}", broker=broker, is_revoked=False)
    with (
        patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
        patch("database.auth_db.get_auth_token", return_value=token),
        patch("utils.session.has_login_this_trading_session", return_value=True),
        patch.object(svc, "fetch_fills", side_effect=lambda *a, **k: copy.deepcopy(fills)),
    ):
        return svc.sync_once(user, now)


def sync_real(broker, user="alice", now=now_fn, patches=()):
    """Like sync() but fetch_fills is NOT mocked (real reader / fallback selection)."""
    auth = SimpleNamespace(name=user, user_id=f"id-{user}", broker=broker, is_revoked=False)
    with ExitStack() as stack:
        stack.enter_context(patch("database.auth_db.get_auth_token_dbquery", return_value=auth))
        stack.enter_context(patch("database.auth_db.get_auth_token", return_value="TOKEN-MASKED"))
        stack.enter_context(
            patch("utils.session.has_login_this_trading_session", return_value=True)
        )
        for p in patches:
            stack.enter_context(p)
        return svc.sync_once(user, now)


def fl_row(flid="F1", flqty="5", flprc="101.25", fltm="17-11-2026 10:15:30", **kw):
    r = {
        "flid": flid,
        "fltm": fltm,
        "flqty": flqty,
        "flprc": flprc,
        "norenordno": "ORD1",
        "actid": "ACT",
        "exch": "NSE",
        "tsym": "SBIN-EQ",
        "prd": "I",
        "trantype": "B",
        "qty": "100",
        "avgprc": "100.0",
        "norentm": "10:00:00 17-11-2026",
    }
    r.update(kw)
    return {k: v for k, v in r.items() if v is not None}


def sync_flattrade(raw_rows, now=now_fn):
    return sync_real(
        "flattrade",
        now=now,
        patches=[
            patch(
                "broker.flattrade.api.order_api.get_trade_book",
                side_effect=lambda auth: copy.deepcopy(raw_rows),
            ),
            patch(
                "broker.flattrade.mapping.order_data.get_oa_symbol",
                side_effect=lambda sym, exch: {"SBIN-EQ": "SBIN"}.get(sym),
            ),
        ],
    )


# ------------------------------------------------------------------ dedup


def test_dedup_with_fill_id(env):
    f = [mk(fill_id="F1"), mk(fill_id="F2")]
    assert sync(f)["inserted"] == 2
    assert sync(f)["inserted"] == 0
    assert [r["dedup_key"] for r in rows(env)] == [
        "flattrade|A1|NSE|2026-11-17|O1|F1",
        "flattrade|A1|NSE|2026-11-17|O1|F2",
    ]


def test_dedup_without_fill_id_counts_identical_tuples(env):
    f = [mk(), mk(), mk(qty=5.0)]
    assert sync(f)["inserted"] == 3
    got = rows(env)
    assert sorted(r["occurrence_idx"] for r in got if r["qty"] == 10.0) == [0, 1]
    assert sync(f)["inserted"] == 0
    assert len(rows(env)) == 3


def test_partial_fills_of_one_order_are_separate_rows(env):
    f = [mk(fill_id="F1", qty=3), mk(fill_id="F2", qty=3), mk(fill_id="F3", qty=4)]
    sync(f)
    got = rows(env)
    assert len(got) == 3 and {r["order_id"] for r in got} == {"O1"}
    assert sum(r["qty"] for r in got) == 10


def test_repeated_sync_creates_no_duplicates_and_records_runs(env):
    f = [mk(fill_id="F1")]
    sync(f)
    sync(f)
    sync(f)
    assert len(rows(env)) == 1
    runs = rows(env, "SELECT status, fetched, inserted FROM ledger_sync_runs ORDER BY id")
    assert [(r["status"], r["fetched"], r["inserted"]) for r in runs] == [
        ("ok", 1, 1),
        ("ok", 1, 0),
        ("ok", 1, 0),
    ]


def test_recovery_after_restart_inserts_nothing(env):
    f = [mk(fill_id="F1"), mk(order_id="O2")]
    sync(f)
    ledger.init_discretionary_ledger_db()  # simulated restart
    assert sync(f)["inserted"] == 0
    assert len(rows(env)) == 2


def test_same_order_id_across_accounts_and_brokers_stays_separate(env):
    f = [
        mk(order_id="SAME", fill_id="F1", account="A1"),
        mk(order_id="SAME", fill_id="F1", account="A2"),
        mk(order_id="SAME", fill_id="F1", account="A1", broker="zerodha"),
    ]
    sync(f)
    assert len(rows(env)) == 3
    assert len({r["dedup_key"] for r in rows(env)}) == 3


# ------------------------------------------------------------------ attribution


def test_attribution_matrix(env):
    add_tag(env, "OTAG", "Alpha")
    add_tag(env, "OUI", "UI Exit Position")
    add_engine_trade(env, "OENG")
    f = [
        mk(order_id="OTAG", fill_id="1"),
        mk(order_id="OUI", fill_id="2"),
        mk(order_id="OENG", fill_id="3"),
        mk(order_id="OMAN", fill_id="4"),
        mk(order_id="OOLD", fill_id="5", when=datetime(2026, 11, 10, 11, 0)),  # before coverage
        mk(order_id="ONEXT", fill_id="6", when=datetime(2026, 11, 11, 9, 20)),  # first covered day
        mk(order_id="", fill_id="7"),
    ]
    sync(f)
    st = {r["fill_id"]: (r["attribution_state"], r["strategy"]) for r in rows(env)}
    assert st["1"] == ("strategy_tag", "Alpha")
    assert st["2"] == ("ui_manual", "UI Exit Position")
    assert st["3"] == ("strategy_engine", "signal_engine")
    assert st["4"] == ("manual", None)
    assert st["5"] == ("unresolved", None)
    assert st["6"] == ("manual", None)
    assert st["7"] == ("unresolved", None)
    run = rows(env, "SELECT tag_min_created_at FROM ledger_sync_runs")[0]
    assert str(run["tag_min_created_at"]).startswith("2026-11-10")


def test_no_tags_coverage_starts_day_after_first_sync(env):
    # NOW = 2026-11-17 is the first successful sync: that day is not trusted
    sync([mk(order_id="OX", fill_id="1")])
    assert rows(env)[0]["attribution_state"] == "unresolved"
    assert (
        rows(env, "SELECT tag_min_created_at FROM ledger_sync_runs")[0]["tag_min_created_at"]
        is None
    )
    nxt = datetime(2026, 11, 18, 12, 0)
    sync(
        [mk(order_id="OY", fill_id="2", when=datetime(2026, 11, 18, 9, 30))],
        now=lambda: nxt,
    )
    st = {r["fill_id"]: r["attribution_state"] for r in rows(env)}
    assert st == {"1": "unresolved", "2": "manual"}  # day-1 fill stays unresolved forever


def test_fill_before_first_sync_date_is_unresolved(env):
    nxt = datetime(2026, 11, 18, 12, 0)
    sync([mk(order_id="OX", fill_id="1")])
    sync([mk(order_id="OOLD", fill_id="2", when=datetime(2026, 11, 16, 10, 0))], now=lambda: nxt)
    assert {r["fill_id"]: r["attribution_state"] for r in rows(env)}["2"] == "unresolved"


def test_manual_needs_grace_even_when_first_sync_coverage(env):
    sync([mk(order_id="OX", fill_id="1")])
    nxt = datetime(2026, 11, 18, 12, 0)
    sync([mk(order_id="OY", fill_id="2", when=datetime(2026, 11, 18, 11, 50))], now=lambda: nxt)
    assert {r["fill_id"]: r["attribution_state"] for r in rows(env)}["2"] == "unresolved"


def test_earliest_of_tag_date_and_first_sync_date_wins(env):
    # first sync 11-17, but a live tag from 11-10 exists -> coverage from 11-11
    add_tag(env, "OTHER")
    sync([mk(order_id="OA", fill_id="1", when=datetime(2026, 11, 11, 10, 0))])
    assert rows(env)[0]["attribution_state"] == "manual"
    # converse: a tag created after the first successful sync does not delay coverage
    with env.engine.begin() as c:
        c.execute(text("DELETE FROM strategy_order_tags"))
        c.execute(text("DELETE FROM ledger_fills"))
        c.execute(text("DELETE FROM ledger_sync_runs"))
    add_tag(env, "OLATE", created=datetime(2026, 11, 17, 9, 0))
    sync([mk(order_id="OB", fill_id="2", when=datetime(2026, 11, 17, 9, 30))])  # first sync day
    nxt = datetime(2026, 11, 18, 12, 0)
    sync([mk(order_id="OC", fill_id="3", when=datetime(2026, 11, 18, 9, 30))], now=lambda: nxt)
    st = {r["fill_id"]: r["attribution_state"] for r in rows(env)}
    assert st == {"2": "unresolved", "3": "manual"}


@pytest.mark.parametrize("status", ["no_auth", "skipped_window", "error"])
def test_non_successful_runs_do_not_start_coverage(env, status):
    ledger.record_sync_run(
        broker="flattrade",
        account="A1",
        started_at=datetime(2026, 11, 10, 9, 0),
        finished_at=datetime(2026, 11, 10, 9, 1),
        status=status,
    )
    assert svc.get_coverage_anchor(None, None) is None
    sync([mk(order_id="OX", fill_id="1", when=datetime(2026, 11, 12, 10, 0))])
    assert rows(env)[0]["attribution_state"] == "unresolved"


@pytest.mark.parametrize("status", ["ok", "partial"])
def test_ok_and_partial_runs_start_coverage_for_any_account(env, status):
    ledger.record_sync_run(
        broker="zerodha",
        account="OTHER",
        started_at=datetime(2026, 11, 10, 9, 0),
        finished_at=datetime(2026, 11, 10, 9, 1),
        status=status,
    )
    sync([mk(order_id="OX", fill_id="1", when=datetime(2026, 11, 11, 10, 0))])
    assert rows(env)[0]["attribution_state"] == "manual"


def test_tagged_and_engine_fills_keep_their_state_under_first_sync_coverage(env):
    add_tag(env, "OTAG", "Alpha", created=datetime(2026, 11, 17, 9, 0))
    add_engine_trade(env, "OENG")
    nxt = datetime(2026, 11, 18, 12, 0)
    w = datetime(2026, 11, 18, 9, 30)
    sync(
        [mk(order_id="OTAG", fill_id="1", when=w), mk(order_id="OENG", fill_id="2", when=w)],
        now=lambda: nxt,
    )
    st = {r["fill_id"]: r["attribution_state"] for r in rows(env)}
    assert st == {"1": "strategy_tag", "2": "strategy_engine"}


def test_sentinel_order_ids_do_not_count_as_engine_orders(env):
    add_tag(env, "OTHER")  # gives coverage
    for s in ("-TRACKER-", "-EXECUTION-", "RECONCILED_ORDER_ID"):
        add_engine_trade(env, s)
    sync([mk(order_id="-TRACKER-", fill_id="1"), mk(order_id="RECONCILED_ORDER_ID", fill_id="2")])
    assert {r["attribution_state"] for r in rows(env)} == {"unresolved"}
    assert svc._engine_known_order_ids(["-TRACKER-", "O9"]) == set()


def test_unreadable_engine_db_keeps_unresolved(env, monkeypatch, tmp_path):
    add_tag(env, "OTHER")
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is not a sqlite database" * 40)
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", str(bad))
    assert svc._engine_known_order_ids(["OMAN"]) is None
    sync([mk(order_id="OMAN", fill_id="1")])
    assert rows(env)[0]["attribution_state"] == "unresolved"


def test_existing_db_without_trades_table_keeps_unresolved(env, monkeypatch, tmp_path):
    add_tag(env, "OTHER")
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", str(empty))
    assert svc._engine_known_order_ids(["OMAN"]) is None
    sync([mk(order_id="OMAN", fill_id="1")])
    assert rows(env)[0]["attribution_state"] == "unresolved"


def test_missing_engine_db_file_is_an_empty_set_and_manual_works(env, monkeypatch):
    add_tag(env, "OTHER")  # gives coverage
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", env.se_path + ".missing")
    assert svc._engine_known_order_ids(["OMAN"]) == set()
    old = datetime(2026, 11, 17, 9, 30, 0)
    sync([mk(order_id="OMAN", fill_id="1", when=old)])
    assert rows(env)[0]["attribution_state"] == "manual"


def test_unresolved_is_reevaluated_and_audited(env):
    f = [mk(order_id="OLATE", fill_id="1")]
    sync(f)
    assert rows(env)[0]["attribution_state"] == "unresolved"
    add_tag(env, "OLATE", "Beta")
    sync(f)
    assert rows(env)[0]["attribution_state"] == "strategy_tag"
    audit = rows(env, "SELECT from_state,to_state,source FROM ledger_attribution_audit")
    assert [(a["from_state"], a["to_state"], a["source"]) for a in audit] == [
        ("unresolved", "strategy_tag", "tag")
    ]


def test_manual_then_audit_row_and_no_churn(env):
    add_tag(env, "OTHER")
    f = [mk(order_id="OMAN", fill_id="1")]
    sync(f)
    sync(f)
    audit = rows(env, "SELECT to_state, source FROM ledger_attribution_audit")
    assert [(a["to_state"], a["source"]) for a in audit] == [("manual", "none")]


def test_strategy_rows_are_never_downgraded(env):
    add_tag(env, "OTAG", "Alpha")
    f = [mk(order_id="OTAG", fill_id="1")]
    sync(f)
    with env.engine.begin() as c:  # tag pruned after retention
        c.execute(text("DELETE FROM strategy_order_tags"))
    sync(f)
    r = rows(env)[0]
    assert (r["attribution_state"], r["strategy"]) == ("strategy_tag", "Alpha")
    assert len(rows(env, "SELECT * FROM ledger_attribution_audit")) == 1


def test_analyze_mode_tags_are_ignored(env):
    add_tag(env, "OSANDBOX", "Paper1", mode="analyze")
    add_tag(env, "OLIVE", "Live1", mode="live")
    sync([mk(order_id="OSANDBOX", fill_id="1")])
    r = rows(env)[0]
    assert r["attribution_state"] != "strategy_tag" and r["strategy"] is None
    # coverage minimum also ignores analyze rows
    add_tag(env, "OEARLY", mode="analyze", created=datetime(2026, 1, 1))
    assert svc.get_tag_min_created_at() == TAG_START


def test_unknown_mode_tag_is_not_called_manual(env):
    add_tag(env, "OLIVE")
    add_tag(env, "OUNK", mode="unknown")
    sync([mk(order_id="OUNK", fill_id="1")])
    assert rows(env)[0]["attribution_state"] == "unresolved"


def test_tag_from_other_broker_does_not_attribute(env):
    add_tag(env, "SAME", "Z", broker="zerodha")
    sync([mk(order_id="SAME", fill_id="1", broker="flattrade")])
    assert rows(env)[0]["attribution_state"] != "strategy_tag"


def test_ui_label_set_is_the_named_constant():
    assert svc.UI_ORDER_LABELS == frozenset({"UI Exit Position"})


# ------------------------------------------------------------------ auth / errors / window


def test_no_auth_record_is_recorded_and_returns(env):
    with (
        patch("database.auth_db.get_auth_token_dbquery", return_value=None),
        patch.object(svc, "fetch_fills") as fetch,
    ):
        assert svc.sync_once("ghost", now_fn) == {"status": "no_auth"}
        fetch.assert_not_called()
    assert rows(env, "SELECT status FROM ledger_sync_runs")[0]["status"] == "no_auth"


def test_missing_token_and_stale_session_are_no_auth(env):
    assert sync([mk()], token=None)["status"] == "no_auth"
    auth = SimpleNamespace(name="a", user_id="a", broker="flattrade", is_revoked=False)
    with (
        patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
        patch("database.auth_db.get_auth_token", return_value="T"),
        patch("utils.session.has_login_this_trading_session", return_value=False),
        patch.object(svc, "fetch_fills") as fetch,
    ):
        assert svc.sync_once("a", now_fn)["status"] == "no_auth"
        fetch.assert_not_called()


def test_broker_rejecting_the_session_is_no_auth(env):
    auth = SimpleNamespace(name="a", user_id="a", broker="flattrade", is_revoked=False)
    with (
        patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
        patch("database.auth_db.get_auth_token", return_value="T"),
        patch("utils.session.has_login_this_trading_session", return_value=True),
        patch.object(svc, "fetch_fills", side_effect=svc.LedgerAuthError("expired")),
    ):
        assert svc.sync_once("a", now_fn)["status"] == "no_auth"


def test_broker_busy_and_errors_are_recorded_never_raised_and_token_redacted(env):
    auth = SimpleNamespace(name="a", user_id="a", broker="flattrade", is_revoked=False)
    for exc in (BrokerBusyError("busy"), RuntimeError("boom SECRET-TOKEN-XYZ")):
        with (
            patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
            patch("database.auth_db.get_auth_token", return_value="SECRET-TOKEN-XYZ"),
            patch("utils.session.has_login_this_trading_session", return_value=True),
            patch.object(svc, "fetch_fills", side_effect=exc),
        ):
            assert svc.sync_once("a", now_fn)["status"] == "broker_error"
    errs = [r["error"] for r in rows(env, "SELECT error FROM ledger_sync_runs")]
    assert all("SECRET-TOKEN-XYZ" not in e for e in errs)
    assert "BrokerBusyError" in errs[0]


def test_one_account_failing_does_not_stop_the_others(env):
    users = {
        "bad": SimpleNamespace(name="bad", user_id="b", broker="flattrade", is_revoked=False),
        "good": SimpleNamespace(name="good", user_id="g", broker="flattrade", is_revoked=False),
    }

    def fetch(broker, token, auth_obj, reader=None):
        if auth_obj.name == "bad":
            raise BrokerBusyError("busy")
        return [mk(fill_id="F1", account="g")]

    with (
        patch.object(svc, "list_sync_users", return_value=["bad", "good"]),
        patch("database.auth_db.get_auth_token_dbquery", side_effect=lambda n: users[n]),
        patch("database.auth_db.get_auth_token", return_value="T"),
        patch("utils.session.has_login_this_trading_session", return_value=True),
        patch.object(svc, "fetch_fills", side_effect=fetch),
    ):
        res = svc.sync_all(now_fn)
    assert [r["status"] for r in res] == ["broker_error", "ok"]
    assert len(rows(env)) == 1


def test_in_skip_window_only_for_a_declared_window():
    window = ((5, 0), (6, 0))
    for h, m, skipped in ((4, 59, False), (5, 0, True), (5, 59, True), (6, 0, False)):
        assert svc.in_skip_window(datetime(2026, 11, 17, h, m), window) is skipped
    assert svc.in_skip_window(datetime(2026, 11, 17, 5, 30), None) is False
    assert not hasattr(svc, "SKIP_WINDOW_START")


def test_skip_window_applies_to_flattrade_only(env):
    at_0530 = lambda: datetime(2026, 11, 17, 5, 30)  # noqa: E731
    out = sync_flattrade([fl_row("F1", fltm="17-11-2026 05:30:00")], now=at_0530)
    assert out == {"status": "skipped_window"}
    run = rows(env, "SELECT broker, account, status FROM ledger_sync_runs")[0]
    assert (run["broker"], run["status"]) == ("flattrade", "skipped_window")
    assert rows(env) == []


def test_other_broker_without_declared_window_syncs_at_0530(env):
    sync_fake(
        env_fills=[fake_fill("X1", when=datetime(2026, 11, 17, 5, 25))],
        now=lambda: datetime(2026, 11, 17, 5, 30),
    )
    assert [r["status"] for r in rows(env, "SELECT status FROM ledger_sync_runs")] == ["ok"]
    assert len(rows(env)) == 1


def test_fake_broker_declaring_a_window_is_skipped_inside_it(env):
    out = sync_fake(
        env_fills=[fake_fill("X1")],
        now=lambda: datetime(2026, 11, 17, 12, 30),
        window=((12, 0), (13, 0)),
    )
    assert out == {"status": "skipped_window"}
    assert rows(env) == []


FORBIDDEN_PATHS = (
    "services.place_order_service.place_order",
    "services.place_order_service.place_order_with_auth",
    "services.modify_order_service.modify_order",
    "services.modify_order_service.modify_order_with_auth",
    "services.cancel_order_service.cancel_order",
    "services.cancel_order_service.cancel_order_with_auth",
    "services.sandbox_service.sandbox_place_order",
    "services.sandbox_service.sandbox_modify_order",
    "services.sandbox_service.sandbox_cancel_order",
    "services.sandbox_service.sandbox_get_tradebook",
    "broker.flattrade.api.order_api.place_order_api",
    "broker.flattrade.api.order_api.modify_order",
    "broker.flattrade.api.order_api.cancel_order",
)


def _import_order_modules():
    # place_order_service has an import cycle that only resolves when restx_api loads first.
    import restx_api  # noqa: F401
    import services.cancel_order_service  # noqa: F401
    import services.modify_order_service  # noqa: F401
    import services.place_order_service  # noqa: F401
    import services.sandbox_service  # noqa: F401


def test_sync_never_calls_sandbox_or_order_paths(env):
    """Every order, modify, cancel and sandbox entry point is a mock and stays uncalled."""
    _import_order_modules()
    with ExitStack() as stack:
        mocks = [stack.enter_context(patch(p)) for p in FORBIDDEN_PATHS]
        out = sync_flattrade([fl_row("F1"), fl_row("F2", flqty="3")])
        for m, path in zip(mocks, FORBIDDEN_PATHS, strict=True):
            assert not m.called, path
    assert out["status"] == "ok" and out["inserted"] == 2


def test_flattrade_path_end_to_end_with_mocked_http(env):
    add_tag(env, "ORD1", "Alpha")
    raw_rows = [
        {
            "flid": "F1",
            "fltm": "17-11-2026 10:15:30",
            "flqty": "5",
            "flprc": "101.25",
            "norenordno": "ORD1",
            "actid": "ACT",
            "exch": "NSE",
            "tsym": "SBIN-EQ",
            "prd": "I",
            "trantype": "B",
            "qty": "10",
        },
        {
            "flid": "F2",
            "fltm": "01-01-1980 00:00:00",
            "flqty": "5",
            "flprc": "101.5",
            "norenordno": "ORD1",
            "actid": "ACT",
            "exch": "NSE",
            "tsym": "SBIN-EQ",
            "prd": "I",
            "trantype": "B",
            "qty": "10",
            "norentm": "10:16:00 17-11-2026",
        },
    ]
    auth = SimpleNamespace(name="a", user_id="a", broker="flattrade", is_revoked=False)
    with (
        patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
        patch("database.auth_db.get_auth_token", return_value="T"),
        patch("utils.session.has_login_this_trading_session", return_value=True),
        patch("broker.flattrade.api.order_api.get_trade_book", return_value=raw_rows) as m,
        patch(
            "broker.flattrade.mapping.order_data.get_oa_symbol",
            side_effect=lambda sym, exch: {"SBIN-EQ": "SBIN"}.get(sym),
        ),
    ):
        first = svc.sync_once("a", now_fn)
        assert (first["inserted"], first["skipped"]) == (1, 1)  # 1980 fltm: unverifiable
        assert svc.sync_once("a", now_fn)["inserted"] == 0
    assert m.call_count == 2
    got = rows(env)
    assert [r["attribution_state"] for r in got] == ["strategy_tag"]
    assert got[0]["fill_time"].startswith("2026-11-17 10:15:30")


# ------------------------------------------------------------------ M1: dedup key scope


def test_fill_id_reused_across_exchanges_keeps_both(env):
    f = [mk(fill_id="7", exchange="NSE"), mk(fill_id="7", exchange="NFO", symbol="SBIN26NOVFUT")]
    assert sync(f)["inserted"] == 2
    assert sync(f)["inserted"] == 0
    assert sorted(r["dedup_key"] for r in rows(env)) == [
        "flattrade|A1|NFO|2026-11-17|O1|7",
        "flattrade|A1|NSE|2026-11-17|O1|7",
    ]


def test_fill_id_reused_across_days_keeps_both(env):
    yesterday = datetime(2026, 11, 16, 10, 30, 0)
    f = [mk(fill_id="7", when=yesterday), mk(fill_id="7", when=TODAY)]
    assert sync(f)["inserted"] == 2
    assert sync(f)["inserted"] == 0
    assert len(rows(env)) == 2


def test_no_flid_key_includes_exchange(env):
    f = [mk(exchange="NSE"), mk(exchange="BSE")]
    assert sync(f)["inserted"] == 2
    assert len({r["dedup_key"] for r in rows(env)}) == 2


# ------------------------------------------------------------------ M2: per-fill data only


def test_missing_flqty_or_flprc_skipped_and_counted_in_the_run(env):
    raw_rows = [
        fl_row("F1"),
        fl_row("F2", flqty=None),
        fl_row("F3", flprc=None),
        fl_row("", flqty="1"),
    ]
    out = sync_flattrade(raw_rows)
    assert out["status"] == "ok" and out["inserted"] == 1 and out["skipped"] == 3
    assert [r["fill_id"] for r in rows(env)] == ["F1"]
    run = rows(env, "SELECT status, fetched, error FROM ledger_sync_runs")[0]
    assert (run["status"], run["fetched"], run["error"]) == ("ok", 1, "unverifiable_skipped=3")


def test_clean_run_has_no_skip_note(env):
    sync_flattrade([fl_row("F1")])
    assert rows(env, "SELECT error FROM ledger_sync_runs")[0]["error"] is None


def test_partial_fills_of_a_100_qty_order_keep_their_own_quantities(env):
    sync_flattrade(
        [fl_row("F1", flqty="40", flprc="100.0"), fl_row("F2", flqty="60", flprc="100.5")]
    )
    got = rows(env)
    assert sorted(r["qty"] for r in got) == [40.0, 60.0]
    assert sum(r["qty"] for r in got) == 100.0


def test_avgprc_drift_between_syncs_creates_no_duplicates(env):
    first = [fl_row("F1", flqty="40", avgprc="100.0"), fl_row("F2", flqty="60", avgprc="100.0")]
    second = [fl_row("F1", flqty="40", avgprc="100.4"), fl_row("F2", flqty="60", avgprc="100.4")]
    assert sync_flattrade(first)["inserted"] == 2
    assert sync_flattrade(second)["inserted"] == 0
    # rows lacking the per-fill price are never rescued from a drifting order average
    drift_a = [fl_row("F3", flprc=None, avgprc="100.0")]
    drift_b = [fl_row("F3", flprc=None, avgprc="100.7")]
    assert sync_flattrade(drift_a)["inserted"] == 0
    assert sync_flattrade(drift_b)["inserted"] == 0
    assert len(rows(env)) == 2


def test_fill_without_fltm_is_not_dated_from_the_order_time(env):
    out = sync_flattrade([fl_row("F1", fltm="01-01-1980 00:00:00")])
    assert out["inserted"] == 0 and out["skipped"] == 1


def test_broker_without_map_trade_fills_records_no_fill_data(env):
    with patch("services.tradebook_service.get_tradebook") as gt:
        out = sync_real("zerodha")
        gt.assert_not_called()  # no order-level fallback exists any more
    assert out == {"status": "no_fill_data"}
    assert rows(env) == []
    run = rows(env, "SELECT status, error FROM ledger_sync_runs")[0]
    assert run["status"] == "no_fill_data" and "no per-fill reader" in run["error"]


def test_unknown_broker_name_records_no_fill_data(env):
    assert sync_real("no_such_broker")["status"] == "no_fill_data"
    assert sync_real("../evil")["status"] == "no_fill_data"


def test_aggregated_fallback_and_its_env_flag_are_gone():
    src = pathlib.Path(svc.__file__).read_text()
    for name in ("ALLOW_AGGREGATED", "aggregated_brokers_allowed", "_fetch_normalized"):
        assert name not in src
    assert not hasattr(svc, "RAW_READERS")


def test_symbol_lookup_failure_skips_counts_and_stores_nothing_raw(env):
    out = sync_flattrade([fl_row("F1"), fl_row("F2", tsym="UNKNOWN-EQ")])
    assert out["inserted"] == 1 and out["skipped"] == 1
    assert out["status"] == "partial" and out["skipped_unmapped"] == 1
    assert [r["symbol"] for r in rows(env)] == ["SBIN"]
    run = rows(env, "SELECT status, error FROM ledger_sync_runs")[0]
    assert (run["status"], run["error"]) == ("partial", "skipped_unmapped=1")


def test_flattrade_rows_store_openalgo_symbol_and_product(env):
    sync_flattrade([fl_row("F1"), fl_row("F2", prd="C", exch="NSE")])
    got = {r["fill_id"]: r for r in rows(env)}
    assert (got["F1"]["symbol"], got["F1"]["product"]) == ("SBIN", "MIS")
    assert got["F2"]["product"] == "CNC"


# ------------------------------------------------------------------ fake broker, no common-code edit

FAKE_BROKER = "fakebroker"


def fake_fill(fill_id, when=TODAY, **kw):
    f = {
        "account": "FAKE-ACCT",
        "order_id": f"ORD-{fill_id}",
        "fill_id": fill_id,
        "symbol": "INFY",
        "exchange": "NSE",
        "product": "MIS",
        "side": "BUY",
        "qty": 3.0,
        "price": 1500.25,
        "fill_time": when,
        "raw_hash": None,
    }
    f.update(kw)
    return f


def install_fake_broker(monkeypatch, fills, window=None, raw="RAW", mapper=None):
    """Inject broker.fakebroker.{api.order_api,mapping.order_data} through sys.modules."""
    import sys
    import types

    mapping = types.ModuleType(f"broker.{FAKE_BROKER}.mapping.order_data")
    api = types.ModuleType(f"broker.{FAKE_BROKER}.api.order_api")
    calls = []

    def get_trade_book(auth):
        calls.append(auth)
        return raw

    mapping.map_trade_fills = mapper or (lambda raw_response, default_account="": (fills, 0))
    if window is not None:
        mapping.SYNC_SKIP_WINDOW = window
    api.get_trade_book = get_trade_book
    monkeypatch.setitem(sys.modules, f"broker.{FAKE_BROKER}.mapping.order_data", mapping)
    monkeypatch.setitem(sys.modules, f"broker.{FAKE_BROKER}.api.order_api", api)
    return calls


def sync_fake(env_fills, now=now_fn, window=None, user="alice"):
    import _pytest.monkeypatch as mp

    m = mp.MonkeyPatch()
    try:
        install_fake_broker(m, env_fills, window)
        return sync_real(FAKE_BROKER, user=user, now=now)
    finally:
        m.undo()


def test_fake_broker_end_to_end_inserts_three_then_zero(env, monkeypatch):
    fills = [fake_fill("A1"), fake_fill("A2"), fake_fill("A3", side="SELL")]
    calls = install_fake_broker(monkeypatch, fills)
    first = sync_real(FAKE_BROKER)
    second = sync_real(FAKE_BROKER)
    assert (first["status"], first["inserted"], first["fetched"]) == ("ok", 3, 3)
    assert (second["status"], second["inserted"]) == ("ok", 0)
    assert calls == ["TOKEN-MASKED", "TOKEN-MASKED"]
    got = rows(env)
    assert {r["broker"] for r in got} == {FAKE_BROKER}
    assert {r["dedup_key"] for r in got} == {
        f"{FAKE_BROKER}|FAKE-ACCT|NSE|2026-11-17|ORD-{i}|{i}" for i in ("A1", "A2", "A3")
    }


def test_sync_run_label_equals_ledger_account_label(env, monkeypatch):
    install_fake_broker(monkeypatch, [fake_fill("A1")])
    sync_real(FAKE_BROKER)
    run = rows(env, "SELECT account FROM ledger_sync_runs")[0]["account"]
    assert run == rows(env)[0]["account"] == "FAKE-ACCT"


def test_account_falls_back_to_auth_user_id_then_name(env, monkeypatch):
    install_fake_broker(monkeypatch, [fake_fill("A1", account="")])
    sync_real(FAKE_BROKER)  # sync_real auth: user_id = "id-alice"
    assert rows(env)[0]["account"] == "id-alice"
    assert rows(env, "SELECT account FROM ledger_sync_runs")[0]["account"] == "id-alice"
    # no user_id -> the Auth name
    auth = SimpleNamespace(name="nm", user_id="", broker=FAKE_BROKER, is_revoked=False)
    with (
        patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
        patch("database.auth_db.get_auth_token", return_value="T"),
        patch("utils.session.has_login_this_trading_session", return_value=True),
    ):
        svc.sync_once("nm", now_fn)
    assert "nm" in {r["account"] for r in rows(env)}


def test_flattrade_sync_run_label_equals_ledger_label(env):
    sync_flattrade([fl_row("F1")])  # actid "ACT" beats the Auth user_id "id-alice"
    assert rows(env)[0]["account"] == "ACT"
    assert rows(env, "SELECT account FROM ledger_sync_runs")[0]["account"] == "ACT"


def test_contract_violations_are_skipped_and_counted(env, monkeypatch):
    bad = [
        fake_fill("B1", qty=0.0),
        fake_fill("B2", price=float("nan")),
        fake_fill("B3", side="B"),
        fake_fill("B4", symbol=""),
        fake_fill("B5", fill_time="2026-11-17 10:00:00"),
        fake_fill("B6", qty="5"),
        fake_fill("B7", exchange=None),
        "not a dict",
    ]
    install_fake_broker(monkeypatch, [fake_fill("OK1")] + bad)
    out = sync_real(FAKE_BROKER)
    assert out["inserted"] == 1 and out["skipped"] == len(bad)
    assert rows(env, "SELECT error FROM ledger_sync_runs")[0]["error"] == (
        f"unverifiable_skipped={len(bad)}"
    )


def test_validate_fill_returns_clean_copy_and_ignores_foreign_broker_key():
    f = fake_fill(" A1 ", broker="spoofed", extra="x")
    clean = svc.validate_fill(f, "fakebroker", "dflt")
    assert clean["broker"] == "fakebroker" and clean["fill_id"] == "A1"
    assert set(clean) == set(svc.FILL_FIELDS) | {"broker"}
    assert svc.validate_fill(fake_fill("A1", account=""), "b", "dflt")["account"] == "dflt"
    assert svc.validate_fill(dict(fake_fill("A1"), fill_id=None), "b")["fill_id"] is None
    aware = datetime(2026, 11, 17, 10, 0, tzinfo=svc.IST)
    assert svc.validate_fill(fake_fill("A1", fill_time=aware), "b") is None
    assert svc.validate_fill(fake_fill("A1", qty=True), "b") is None


def test_broker_module_auth_marker_becomes_no_auth(env, monkeypatch):
    class Dead(Exception):
        is_auth_error = True

    def mapper(raw_response, default_account=""):
        raise Dead("session rejected")

    install_fake_broker(monkeypatch, [], mapper=mapper)
    assert sync_real(FAKE_BROKER) == {"status": "no_auth"}


def test_broker_module_other_error_is_broker_error(env, monkeypatch):
    def mapper(raw_response, default_account=""):
        raise RuntimeError("bad payload")

    install_fake_broker(monkeypatch, [], mapper=mapper)
    assert sync_real(FAKE_BROKER) == {"status": "broker_error"}


def test_common_modules_carry_no_broker_specific_strings():
    root = pathlib.Path(svc.__file__).resolve().parent.parent
    files = (
        root / "services" / "discretionary_ledger_service.py",
        root / "database" / "discretionary_ledger_db.py",
        root / "services" / "discretionary_ledger_scheduler.py",
    )
    banned = (
        "flattrade",
        "flid",
        "fltm",
        "flqty",
        "flprc",
        "norentm",
        "actid",
        "noren",
        "tsym",
        "trantype",
    )
    for f in files:
        text = f.read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in text, f"{word!r} found in {f.name}"


def test_no_flid_book_reordered_and_grown_has_no_duplicates_or_losses(env):
    def a():
        return mk(order_id="A")

    def b1():
        return mk(order_id="B", qty=7.0)

    def c():
        return mk(order_id="C")

    assert sync([a(), b1(), b1()])["inserted"] == 3
    assert sync([b1(), a(), b1(), c(), b1()])["inserted"] == 2  # only C and 3rd B are new
    assert sync([b1(), b1(), b1(), c(), a()])["inserted"] == 0
    got = rows(env)
    assert len(got) == 5
    assert sorted(r["occurrence_idx"] for r in got if r["order_id"] == "B") == [0, 1, 2]


# ------------------------------------------------------------------ M3: grace + manual upgrade

LATER = datetime(2026, 11, 17, 11, 30, 0)


def test_fill_inside_grace_stays_unresolved_then_becomes_manual(env):
    add_tag(env, "OTHER")  # coverage
    f = [mk(order_id="OFRESH", fill_id="1", when=datetime(2026, 11, 17, 10, 50, 0))]
    sync(f)  # now=11:00, fill is 10 min old
    assert rows(env)[0]["attribution_state"] == "unresolved"
    assert rows(env, "SELECT * FROM ledger_attribution_audit") == []
    sync(f, now=lambda: LATER)  # 40 min old
    assert rows(env)[0]["attribution_state"] == "manual"


def test_grace_minutes_env_is_honoured(env, monkeypatch):
    add_tag(env, "OTHER")
    f = [mk(order_id="OFRESH", fill_id="1", when=datetime(2026, 11, 17, 10, 50, 0))]
    monkeypatch.setenv("DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN", "5")
    sync(f)
    assert rows(env)[0]["attribution_state"] == "manual"


def test_fill_after_grace_with_no_source_becomes_manual(env):
    add_tag(env, "OTHER")
    sync([mk(order_id="OOLD", fill_id="1", when=datetime(2026, 11, 17, 9, 30, 0))])
    r = rows(env)[0]
    assert (r["attribution_state"], r["strategy"]) == ("manual", None)


def test_tag_arriving_after_first_sync_upgrades_unresolved_within_grace(env):
    f = [mk(order_id="OLATE", fill_id="1", when=datetime(2026, 11, 17, 10, 55, 0))]
    sync(f)
    assert rows(env)[0]["attribution_state"] == "unresolved"
    add_tag(env, "OLATE", "Beta")
    sync(f)
    assert rows(env)[0]["attribution_state"] == "strategy_tag"


def test_tag_arriving_after_manual_upgrades_and_audits(env):
    add_tag(env, "OTHER")
    f = [mk(order_id="OLATE", fill_id="1", when=datetime(2026, 11, 17, 9, 30, 0))]
    sync(f)
    assert rows(env)[0]["attribution_state"] == "manual"
    add_tag(env, "OLATE", "Beta")
    sync(f)
    r = rows(env)[0]
    assert (r["attribution_state"], r["strategy"]) == ("strategy_tag", "Beta")
    audit = rows(env, "SELECT from_state,to_state,reason,source FROM ledger_attribution_audit")
    assert [(a["from_state"], a["to_state"], a["source"]) for a in audit] == [
        ("unresolved", "manual", "none"),
        ("manual", "strategy_tag", "tag"),
    ]
    assert "upgraded from manual" in audit[1]["reason"]
    sync(f)  # stable afterwards
    assert len(rows(env, "SELECT * FROM ledger_attribution_audit")) == 2


def test_trades_db_row_arriving_after_manual_upgrades_to_strategy_engine(env):
    add_tag(env, "OTHER")
    f = [mk(order_id="OENG", fill_id="1", when=datetime(2026, 11, 17, 9, 30, 0))]
    sync(f)
    assert rows(env)[0]["attribution_state"] == "manual"
    add_engine_trade(env, "OENG")
    sync(f)
    r = rows(env)[0]
    assert (r["attribution_state"], r["strategy"]) == ("strategy_engine", "signal_engine")
    last = rows(env, "SELECT from_state,to_state,source FROM ledger_attribution_audit")[-1]
    assert (last["from_state"], last["to_state"], last["source"]) == (
        "manual",
        "strategy_engine",
        "signal_engine",
    )


def test_ui_tag_arriving_after_manual_upgrades_to_ui_manual(env):
    add_tag(env, "OTHER")
    f = [mk(order_id="OUI", fill_id="1", when=datetime(2026, 11, 17, 9, 30, 0))]
    sync(f)
    add_tag(env, "OUI", "UI Exit Position")
    sync(f)
    assert rows(env)[0]["attribution_state"] == "ui_manual"


def test_manual_never_downgrades_to_unresolved_and_strategy_never_changes(env):
    add_tag(env, "OTHER")
    f = [mk(order_id="OMAN", fill_id="1", when=datetime(2026, 11, 17, 9, 30, 0))]
    sync(f)
    with env.engine.begin() as c:  # all tags pruned: coverage disappears
        c.execute(text("DELETE FROM strategy_order_tags"))
    sync(f)
    assert rows(env)[0]["attribution_state"] == "manual"
    assert ledger.can_change_state("manual", "unresolved") is False
    assert ledger.can_change_state("manual", "manual") is False
    for to in ("strategy_tag", "strategy_engine", "ui_manual"):
        assert ledger.can_change_state("manual", to) is True
        assert ledger.can_change_state(to, "manual") is False


# ------------------------------------------------------------------ gate stays inert


def test_app_py_ledger_block_is_only_reachable_through_the_env_gate():
    """app.py is not imported (too heavy); its source is parsed instead."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    hits = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        and "discretionary" in (getattr(n, "module", "") or "")
        # Stage 3a: the read-only performance API blueprint is registered unconditionally
        # because every one of its routes is feature-gated per request (404 when off).
        and getattr(n, "module", "") != "blueprints.discretionary_pnl"
    ]
    assert hits, "ledger block not found in app.py"
    for n in hits:
        cur, gated = n, False
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.If) and "DISCRETIONARY_LEDGER_ENABLED" in ast.get_source_segment(
                src, cur.test
            ):
                gated = True
        assert gated, f"ungated ledger import at line {n.lineno}"
    assert 'os.getenv("DISCRETIONARY_LEDGER_ENABLED", "false")' in src  # default off


# ------------------------------------------------------------------ review round: partial, errors, imports

NO_DATA_REPLY = {"stat": "Not_Ok", "emsg": 'Error Occurred : 5 :"no data"'}
SESSION_REPLY = {"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"}


def runs(env):
    return rows(env, "SELECT status, fetched, inserted, error FROM ledger_sync_runs ORDER BY id")


def test_sync_statuses_include_partial():
    assert "partial" in ledger.SYNC_STATUSES


def test_flattrade_no_data_reply_is_ok_with_zero_fetched(env):
    out = sync_flattrade(NO_DATA_REPLY)
    assert (out["status"], out["fetched"], out["inserted"]) == ("ok", 0, 0)
    run = runs(env)[0]
    assert (run["status"], run["fetched"], run["error"]) == ("ok", 0, None)


def test_flattrade_session_expired_reply_is_no_auth(env):
    assert sync_flattrade(SESSION_REPLY) == {"status": "no_auth"}
    run = runs(env)[0]
    assert run["status"] == "no_auth" and "session rejected" in run["error"]
    assert rows(env) == []


def test_inside_skip_window_with_no_token_is_recorded_as_skipped_window(env):
    # Documents the code's order: the window is checked before the token, so a
    # missing token inside the window is skipped_window, not no_auth.
    auth = SimpleNamespace(name="a", user_id="a", broker="flattrade", is_revoked=False)
    with (
        patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
        patch("database.auth_db.get_auth_token", return_value=None),
        patch("utils.session.has_login_this_trading_session", return_value=False),
    ):
        out = svc.sync_once("a", lambda: datetime(2026, 11, 17, 5, 30))
    assert out == {"status": "skipped_window"}
    assert [r["status"] for r in runs(env)] == ["skipped_window"]


def test_outside_window_with_no_token_is_no_auth(env):
    assert sync([mk()], token=None)["status"] == "no_auth"


def _import_patch(exc_for):
    real = svc.importlib.import_module

    def fake(name, *a, **k):
        if name == exc_for[0]:
            raise exc_for[1]
        return real(name, *a, **k)

    return patch.object(svc.importlib, "import_module", side_effect=fake)


def test_broker_import_failing_with_other_exception_is_error_and_isolated(env):
    boom = _import_patch(("broker.boomer.mapping.order_data", RuntimeError("init failed")))
    with boom:
        out = sync_real("boomer")
    assert out == {"status": "error"}
    run = runs(env)[0]
    assert run["status"] == "error" and run["error"].startswith("RuntimeError")
    # the next account still syncs
    assert sync([mk()])["status"] == "ok"


def test_missing_dependency_inside_broker_module_is_error_not_no_fill_data(env):
    err = ModuleNotFoundError("No module named 'somedep'", name="somedep")
    with _import_patch(("broker.depbroken.mapping.order_data", err)):
        out = sync_real("depbroken")
    assert out == {"status": "error"}
    assert runs(env)[0]["error"].startswith("ModuleNotFoundError")


def test_absent_broker_module_is_still_no_fill_data(env):
    assert sync_real("nosuchbroker") == {"status": "no_fill_data"}


def test_broker_name_must_fully_match():
    assert svc.load_broker_reader("flattrade\n") is None
    assert svc.load_broker_reader("Flattrade") is None
    assert svc.load_broker_reader("") is None


def test_one_tradebook_with_two_actids_keeps_two_accounts(env):
    out = sync_flattrade([fl_row("F1", actid="ACT1"), fl_row("F2", actid="ACT2")])
    assert out["status"] == "ok" and out["inserted"] == 2
    got = {r["fill_id"]: r["account"] for r in rows(env)}
    assert got == {"F1": "ACT1", "F2": "ACT2"}


def test_lookup_failure_then_success_inserts_on_second_sync(env):
    state = {"up": False}

    def lookup(sym, exch):
        return "SBIN" if state["up"] and sym == "SBIN-EQ" else None

    def go():
        return sync_real(
            "flattrade",
            patches=[
                patch(
                    "broker.flattrade.api.order_api.get_trade_book",
                    side_effect=lambda auth: [fl_row("F1")],
                ),
                patch("broker.flattrade.mapping.order_data.get_oa_symbol", side_effect=lookup),
            ],
        )

    first = go()
    assert (first["status"], first["inserted"], first["skipped_unmapped"]) == ("partial", 0, 1)
    assert rows(env) == []
    state["up"] = True
    second = go()
    assert (second["status"], second["inserted"]) == ("ok", 1)
    assert [r["fill_id"] for r in rows(env)] == ["F1"]
    assert [r["status"] for r in runs(env)] == ["partial", "ok"]


def test_partial_status_counts_both_kinds_and_logs_one_warning(env, caplog):
    rows_in = [
        fl_row("F1"),
        fl_row("F2", tsym="UNKNOWN-EQ"),  # symbol unmapped
        fl_row("F3", exch="NSE", prd="M"),  # product unmapped
        fl_row("F4", flqty=None),  # broker data missing
    ]
    with caplog.at_level("WARNING", logger=svc.logger.name):
        out = sync_flattrade(rows_in)
    assert (out["status"], out["inserted"], out["skipped"]) == ("partial", 1, 3)
    run = runs(env)[0]
    assert run["status"] == "partial"
    assert run["error"] == "skipped_unmapped=2 unverifiable_skipped=1"
    warns = [
        r for r in caplog.records if r.levelname == "WARNING" and "skipped_unmapped" in r.message
    ]
    assert len(warns) == 1 and warns[0].message.isascii()
    assert "SBIN" not in warns[0].message


def test_plain_int_skip_count_from_a_broker_stays_ok(env, monkeypatch):
    install_fake_broker(
        monkeypatch,
        [],
        mapper=lambda raw_response, default_account="": ([fake_fill("A1")], 2),
    )
    out = sync_real(FAKE_BROKER)
    assert out["status"] == "ok" and out["skipped"] == 2
    assert runs(env)[0]["error"] == "unverifiable_skipped=2"


def test_unmapped_attribute_on_a_generic_broker_triggers_partial(env, monkeypatch):
    class Skips(int):
        unmapped = 1

    install_fake_broker(
        monkeypatch,
        [],
        mapper=lambda raw_response, default_account="": ([fake_fill("A1")], Skips(1)),
    )
    out = sync_real(FAKE_BROKER)
    assert out["status"] == "partial" and out["inserted"] == 1
    assert runs(env)[0]["error"] == "skipped_unmapped=1"


@pytest.mark.parametrize("product", ["MIS", "CNC", "NRML"])
def test_validate_fill_accepts_documented_products(product):
    assert svc.validate_fill(fake_fill("A1", product=product), "x")["product"] == product


@pytest.mark.parametrize("product", ["I", "C", "M", "mis", "BO", "CO"])
def test_validate_fill_rejects_other_products(product):
    assert svc.validate_fill(fake_fill("A1", product=product), "x") is None


# ------------------------------------------------------------------ review: dedup key with order id


def test_same_fill_id_different_symbol_and_order_are_both_kept(env):
    f = [
        mk(order_id="OA", fill_id="55", symbol="SBIN-EQ"),
        mk(order_id="OB", fill_id="55", symbol="INFY-EQ"),
    ]
    assert sync(f)["inserted"] == 2
    got = rows(env)
    assert len(got) == 2
    assert sorted(r["dedup_key"] for r in got) == [
        "flattrade|A1|NSE|2026-11-17|OA|55",
        "flattrade|A1|NSE|2026-11-17|OB|55",
    ]


def test_same_fill_resynced_is_not_duplicated_and_order_is_stable(env):
    f = [
        mk(order_id="OA", fill_id="55", symbol="SBIN-EQ"),
        mk(order_id="OB", fill_id="55", symbol="INFY-EQ"),
        mk(order_id="OA", fill_id="56", symbol="SBIN-EQ"),
    ]
    assert sync(f)["inserted"] == 3
    assert sync(list(reversed(f)))["inserted"] == 0
    assert sync([f[1], f[2], f[0]])["inserted"] == 0
    assert len(rows(env)) == 3


def test_no_fill_id_key_shape_is_unchanged():
    got = svc.assign_dedup([{**mk(order_id="OA"), "broker": "flattrade"}])
    assert got[0]["dedup_key"] == (
        "flattrade|A1|NSE|OA|SBIN-EQ|BUY|10.000000|100.000000|2026-11-17 10:30:00|0"
    )


# ------------------------------------------------------------------ review: re-attribute all rows


def test_row_of_broker_a_upgrades_during_a_sync_of_broker_b(env):
    assert sync([mk(order_id="XA", fill_id="1")], broker="flattrade")["status"] == "ok"
    assert rows(env)[0]["attribution_state"] == "unresolved"
    add_tag(env, "XA", "S9", broker="flattrade")
    assert sync([], broker="zerodha", user="bob")["status"] == "ok"
    got = rows(env)[0]
    assert got["attribution_state"] == "strategy_tag" and got["strategy"] == "S9"


def test_tag_of_another_broker_still_does_not_match_during_cross_sync(env):
    sync([mk(order_id="XB", fill_id="1")], broker="flattrade")
    add_tag(env, "XB", "S9", broker="zerodha")
    sync([], broker="zerodha", user="bob")
    assert rows(env)[0]["attribution_state"] != "strategy_tag"


# ------------------------------------------------------------------ same-day coverage

D18 = datetime(2026, 11, 18)  # a Wednesday


def t18(hh, mm=0, ss=0):
    return datetime(2026, 11, 18, hh, mm, ss)


REAL_SESSION_START = svc._session_start


def fake_session_start(day, exchange):
    """T = session start minus 15 min: NSE-like 09:00, MCX 08:45; weekend closed."""
    if day.weekday() >= 5:
        return None
    if (exchange or "").upper() == "MCX":
        return datetime(day.year, day.month, day.day, 8, 45)
    return datetime(day.year, day.month, day.day, 9, 0)


@pytest.fixture
def sd(env, monkeypatch):
    """Same-day tests: deterministic calendar, no live tags (date coverage starts 11-19)."""
    monkeypatch.setattr(svc, "_session_start", fake_session_start)
    return env


def add_run(env, start, status="ok", finished=None, account="A1", broker="flattrade"):
    ledger.record_sync_run(
        broker=broker,
        account=account,
        started_at=start,
        finished_at=finished or start + timedelta(seconds=2),
        status=status,
        fetched=0,
        inserted=0,
    )


def states(env):
    return {r["fill_id"]: r["attribution_state"] for r in rows(env)}


def audits(env):
    return rows(
        env,
        "SELECT a.reason, f.fill_id FROM ledger_attribution_audit a "
        "JOIN ledger_fills f ON f.id = a.fill_ref ORDER BY a.id",
    )


def at(hh, mm=0, ss=0):
    when = t18(hh, mm, ss)
    return lambda: when


def test_same_day_pre_session_sync_and_continuous_syncs_give_manual_after_grace(sd):
    for start in (t18(8, 30), t18(9, 10), t18(9, 25), t18(9, 40), t18(9, 55)):
        add_run(sd, start)
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 35))], now=at(10, 5))
    assert states(sd) == {"1": "manual"}
    assert audits(sd)[-1]["reason"] == "absent from tags and engine; same-day coverage ok"


def test_same_day_fill_inside_grace_stays_unresolved(sd):
    for start in (t18(8, 30), t18(9, 10), t18(9, 25), t18(9, 40), t18(9, 55)):
        add_run(sd, start)
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 50))], now=at(10, 5))
    assert states(sd) == {"1": "unresolved"}


def test_same_day_first_sync_after_session_open_stays_unresolved(sd):
    add_run(sd, t18(9, 20))
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 10))], now=at(9, 35))
    sync([], now=at(10, 30))
    assert states(sd) == {"1": "unresolved"}


def test_same_day_gap_over_limit_keeps_later_fills_unresolved_and_earlier_manual(sd):
    for start in (t18(8, 30), t18(9, 10), t18(9, 30), t18(10, 5), t18(10, 20), t18(10, 35)):
        add_run(sd, start)  # 09:30 -> 10:05 is a 35 minute gap
    fills = [
        mk(order_id="OA", fill_id="early", when=t18(9, 20)),
        mk(order_id="OB", fill_id="gap", when=t18(10, 1)),
        mk(order_id="OC", fill_id="late", when=t18(10, 25)),
    ]
    sync(fills, now=at(11, 0))
    assert states(sd) == {"early": "manual", "gap": "unresolved", "late": "unresolved"}


@pytest.mark.parametrize("status", ["no_auth", "broker_error", "error", "skipped_window"])
def test_same_day_failed_or_skipped_runs_are_not_heartbeats(sd, status):
    add_run(sd, t18(8, 30))
    add_run(sd, t18(9, 10))
    add_run(sd, t18(9, 30), status=status)
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 20))], now=at(9, 55))  # 45 min after 09:10
    sync([], now=at(10, 30))
    assert states(sd) == {"1": "unresolved"}


def test_same_day_ok_run_in_the_same_place_is_a_heartbeat(sd):
    add_run(sd, t18(8, 30))
    add_run(sd, t18(9, 10))
    add_run(sd, t18(9, 30))
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 20))], now=at(9, 55))
    assert states(sd) == {"1": "manual"}


def test_same_day_partial_runs_are_heartbeats(sd):
    add_run(sd, t18(8, 30), status="partial")  # a partial run is also the pre-session run
    add_run(sd, t18(9, 10), status="partial")
    add_run(sd, t18(9, 30), status="partial")
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 20))], now=at(9, 55))
    assert states(sd) == {"1": "manual"}  # fails if "partial" is excluded from heartbeats


def test_same_day_gap_exactly_at_limit_passes_and_finish_equal_to_start_fails():
    start = lambda day, ex: t18(9, 0)  # noqa: E731
    pre = (t18(8, 30), t18(8, 30, 2))
    fill = t18(9, 20)
    assert svc.same_day_covered(fill, "NSE", [pre, (t18(9, 30), None)], None, start)
    assert not svc.same_day_covered(fill, "NSE", [pre, (t18(9, 30, 1), None)], None, start)
    # a run finishing exactly at T does not qualify
    assert not svc.same_day_covered(
        fill, "NSE", [(t18(8, 59), t18(9, 0)), (t18(9, 25), None)], None, start
    )
    # the first point at/after the fill may be the fill time itself
    assert svc.same_day_covered(t18(9, 25), "NSE", [pre, (t18(9, 25), None)], None, start)
    # a fill before T is never same-day covered
    assert not svc.same_day_covered(t18(8, 55), "NSE", [pre, (t18(9, 5), None)], None, start)
    # the run in progress counts as a point
    assert svc.same_day_covered(fill, "NSE", [pre], t18(9, 25), start)
    assert not svc.same_day_covered(fill, "NSE", [pre], None, start)


def test_same_day_unreadable_engine_db_stays_unresolved(sd, monkeypatch, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file at all" * 100)
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", str(bad))
    for start in (t18(8, 30), t18(9, 10), t18(9, 25)):
        add_run(sd, start)
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 20))], now=at(10, 0))
    assert states(sd) == {"1": "unresolved"}


def test_same_day_tag_query_error_records_error_and_changes_nothing(sd, monkeypatch):
    for start in (t18(8, 30), t18(9, 10), t18(9, 25)):
        add_run(sd, start)

    def boom(order_ids):
        raise RuntimeError("tags down")

    monkeypatch.setattr(svc, "_load_live_tags", boom)
    out = sync([mk(order_id="OM", fill_id="1", when=t18(9, 20))], now=at(10, 0))
    assert out == {"status": "error"}
    assert states(sd) == {"1": "unresolved"} and audits(sd) == []
    assert rows(sd, "SELECT status FROM ledger_sync_runs ORDER BY id")[-1]["status"] == "error"


def test_same_day_live_tag_engine_and_ui_label_keep_their_states(sd):
    for start in (t18(8, 30), t18(9, 10), t18(9, 25)):
        add_run(sd, start)
    add_tag(sd, "OTAG", "Alpha", created=t18(9, 15))
    add_tag(sd, "OUI", "UI Exit Position", created=t18(9, 15))
    add_engine_trade(sd, "OENG")
    fills = [
        mk(order_id="OTAG", fill_id="1", when=t18(9, 20)),
        mk(order_id="OUI", fill_id="2", when=t18(9, 20)),
        mk(order_id="OENG", fill_id="3", when=t18(9, 20)),
        mk(order_id="OMAN", fill_id="4", when=t18(9, 20)),
    ]
    sync(fills, now=at(9, 55))
    assert states(sd) == {
        "1": "strategy_tag",
        "2": "ui_manual",
        "3": "strategy_engine",
        "4": "manual",
    }


def test_previous_day_fill_without_pre_session_sync_stays_unresolved(sd):
    for start in (t18(8, 30), t18(9, 10), t18(9, 25)):
        add_run(sd, start)
    sync([mk(order_id="OLD", fill_id="1", when=datetime(2026, 11, 17, 14, 0))], now=at(9, 40))
    assert states(sd) == {"1": "unresolved"}


def test_restart_mid_day_boot_sync_keeps_continuity(sd):
    for start in (t18(8, 30), t18(9, 10), t18(9, 25), t18(9, 50)):  # 09:50 = boot catch-up
        add_run(sd, start)
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 40))], now=at(10, 15))
    assert states(sd) == {"1": "manual"}


def test_restart_gap_over_limit_unresolved(sd):
    for start in (t18(8, 30), t18(9, 10), t18(9, 25)):
        add_run(sd, start)
    add_run(sd, t18(10, 0))  # restart after 35 minutes without a run
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 40))], now=at(10, 15))
    assert states(sd) == {"1": "unresolved"}


def test_closed_session_day_is_not_same_day_covered(sd):
    sat = datetime(2026, 11, 21)
    for hh, mm in ((8, 30), (9, 10), (9, 25)):
        add_run(sd, sat.replace(hour=hh, minute=mm))
    sync(
        [mk(order_id="OM", fill_id="1", when=sat.replace(hour=9, minute=20))],
        now=lambda: sat.replace(hour=10, minute=0),
    )
    assert states(sd) == {"1": "unresolved"}


def _calendar_module(monkeypatch, fn):
    import sys
    import types

    mod = types.ModuleType("database.market_calendar_db")
    mod.get_effective_session_window = fn
    monkeypatch.setitem(sys.modules, "database.market_calendar_db", mod)


def _window(hh, mm):
    def fn(day, exchange):
        start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=svc.IST)
        return {"start_ms": int(start.timestamp() * 1000), "end_ms": 0, "is_special": False}

    return fn


def test_session_window_lookup_error_fails_safe(monkeypatch):
    def boom(day, exchange):
        raise RuntimeError("calendar down")

    _calendar_module(monkeypatch, boom)
    assert REAL_SESSION_START(D18.date(), "NSE") is None
    _calendar_module(monkeypatch, lambda day, exchange: None)  # holiday / weekend
    assert REAL_SESSION_START(D18.date(), "NSE") is None
    _calendar_module(monkeypatch, _window(9, 15))
    assert REAL_SESSION_START(D18.date(), "NSE") == t18(9, 0)


def test_mcx_session_start_differs_from_nse(sd):
    runs_ = [(t18(8, 48), t18(8, 50)), (t18(9, 5), None)]
    fill_nse = t18(9, 3)
    assert svc.same_day_covered(fill_nse, "NSE", runs_, None, fake_session_start)
    assert not svc.same_day_covered(fill_nse, "MCX", runs_, None, fake_session_start)


def test_max_sync_gap_env_override(monkeypatch):
    monkeypatch.delenv("DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN", raising=False)
    assert svc.max_sync_gap_minutes() == 30
    monkeypatch.setenv("DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN", "45")
    assert svc.max_sync_gap_minutes() == 45
    start = lambda day, ex: t18(9, 0)  # noqa: E731
    runs_ = [(t18(8, 30), t18(8, 30, 2)), (t18(9, 40), None)]
    assert svc.same_day_covered(t18(9, 20), "NSE", runs_, None, start)
    monkeypatch.setenv("DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN", "30")
    assert not svc.same_day_covered(t18(9, 20), "NSE", runs_, None, start)


def test_existing_unresolved_rows_upgrade_on_next_sync_with_audit_reason_same_day(sd, monkeypatch):
    add_run(sd, t18(8, 30))
    add_run(sd, t18(9, 10))
    # an older build stored the fill unresolved (no same-day rule): simulate with a gap limit
    # of 1 minute for the first sync
    monkeypatch.setenv("DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN", "1")
    sync([mk(order_id="OM", fill_id="1", when=t18(9, 15))], now=at(9, 25))
    monkeypatch.delenv("DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN")
    assert states(sd) == {"1": "unresolved"} and audits(sd) == []
    sync([], now=at(9, 50))
    assert states(sd) == {"1": "manual"}
    assert [a["reason"] for a in audits(sd)] == [
        "absent from tags and engine; same-day coverage ok"
    ]


# Today's real data, masked: 7 ok runs and 5 TCS fills of one account.
REAL_RUNS = [
    (t18(8, 30, 30), t18(8, 30, 32)),
    (t18(9, 34, 38), None),
    (t18(9, 41, 21), None),
    (t18(9, 56, 38), None),
    (t18(10, 11, 58), None),
    (t18(10, 27, 22), None),
    (t18(10, 42, 46), None),
]
REAL_FILLS = [
    ("O1", "f1", "BUY", 4, 2392.6, t18(9, 46, 18)),
    ("O2", "f2", "BUY", 21, 2392.0, t18(9, 46, 18)),
    ("O3", "f3", "BUY", 20, 2401.8, t18(10, 7, 49)),
    ("O4", "f4", "SELL", 30, 2395.2, t18(10, 16, 38)),
    ("O5", "f5", "SELL", 15, 2395.2, t18(10, 16, 38)),
]


def _real_case(env, monkeypatch, gap, now, runs_=REAL_RUNS):
    """Real NSE window 09:15 through the real _session_start (T_open 09:00)."""
    monkeypatch.setattr(svc, "_session_start", REAL_SESSION_START)
    _calendar_module(monkeypatch, _window(9, 15))
    monkeypatch.setenv("DISCRETIONARY_LEDGER_MAX_SYNC_GAP_MIN", str(gap))
    for start, fin in runs_:
        add_run(env, start, finished=fin or start + timedelta(seconds=3), account="FZ0000")
    fills = [
        mk(order_id=o, fill_id=f, side=s, qty=q, price=p, when=w, symbol="TCS", account="FZ0000")
        for o, f, s, q, p, w in REAL_FILLS
    ]
    sync(fills, now=lambda: now)
    return states(env)


def test_real_case_2026_11_18_g30_all_five_stay_unresolved(env, monkeypatch):
    got = _real_case(env, monkeypatch, 30, t18(10, 58, 10))
    assert got == dict.fromkeys(("f1", "f2", "f3", "f4", "f5"), "unresolved")


def test_real_case_2026_11_18_g45_all_five_become_manual(env, monkeypatch):
    got = _real_case(env, monkeypatch, 45, t18(10, 58, 10))
    assert got == dict.fromkeys(("f1", "f2", "f3", "f4", "f5"), "manual")


def test_real_case_2026_11_18_g45_sells_wait_for_grace(env, monkeypatch):
    got = _real_case(env, monkeypatch, 45, t18(10, 42, 46), runs_=REAL_RUNS[:-1])
    assert got == {
        "f1": "manual",
        "f2": "manual",
        "f3": "manual",
        "f4": "unresolved",  # 10:16:38 + 30 min grace = 10:46:38
        "f5": "unresolved",
    }


def test_real_case_continuous_15_minute_syncs_give_manual_after_grace(env, monkeypatch):
    runs_ = [(t18(8, 30, 30), t18(8, 30, 32))]
    runs_ += [(t18(9, 0) + timedelta(minutes=15 * i), None) for i in range(1, 8)]  # 09:15..10:45
    within = _real_case(env, monkeypatch, 30, t18(10, 46, 0), runs_=runs_)
    assert within == {
        "f1": "manual",
        "f2": "manual",
        "f3": "manual",
        "f4": "unresolved",
        "f5": "unresolved",
    }
    sync([], now=at(11, 0))
    assert states(env) == dict.fromkeys(("f1", "f2", "f3", "f4", "f5"), "manual")
