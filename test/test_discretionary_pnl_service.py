"""Discretionary P&L service: rebuild, trades, positions, summary, reconcile.

Temp SQLite for both the ledger and the derived book; quotes, the position book and auth are
mocked, so there is no broker call and no HTTP. Run with:
    uv run pytest test/test_discretionary_pnl_service.py -q
"""

import itertools
import random
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from database import discretionary_book_db as book
from database import discretionary_ledger_db as ledger
from services import discretionary_pnl_service as pnl

DERIVED = ("disc_positions", "disc_trades", "disc_trade_fills", "disc_orphans", "disc_build_meta")
NOW = datetime(2026, 11, 17, 11, 0, 0)  # Tuesday, market open


class Env:
    def __init__(self, eng):
        self.eng = eng
        self.now = NOW
        self.quotes = {}  # (symbol, exchange) -> ltp, or an error string
        self.quote_calls = []
        self.quote_fail = False
        self.positionbook = []
        self.positionbook_calls = 0
        self._n = itertools.count(1)

    def fill(
        self,
        side,
        qty,
        price,
        when,
        *,
        state="manual",
        product="MIS",
        symbol="SBIN",
        exchange="NSE",
        broker="flattrade",
        account="A",
        order_id=None,
        fill_id=None,
    ):
        n = next(self._n)
        row = {
            "broker": broker,
            "account": account,
            "order_id": order_id if order_id is not None else f"O{n}",
            "fill_id": fill_id if fill_id is not None else f"F{n}",
            "symbol": symbol,
            "exchange": exchange,
            "product": product,
            "side": side,
            "qty": float(qty),
            "price": float(price),
            "fill_time": when,
            "occurrence_idx": 0,
            "dedup_key": f"k{n}",
            "attribution_state": state,
            "strategy": None,
            "synced_at": when,
            "raw_hash": None,
        }
        assert ledger.insert_fills([row]) == 1
        return n

    def count(self, table):
        with self.eng.connect() as c:
            return c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()

    def snapshot(self):
        with self.eng.connect() as c:
            return {
                t: c.execute(text(f"SELECT * FROM {t} ORDER BY 1")).fetchall()
                for t in ("ledger_fills", "ledger_attribution_audit", *DERIVED)
            }


_real_calendar_window = pnl._calendar_window
_real_confirmed_closed = pnl._confirmed_closed


def _calendar_down(day, exchange):
    raise RuntimeError("calendar unavailable in tests")


@pytest.fixture(autouse=True)
def _clock_only(monkeypatch):
    """No test reads the real market calendar: it is 'down' unless a test supplies a window."""
    monkeypatch.setattr(pnl, "_calendar_window", _calendar_down)
    monkeypatch.setattr(pnl, "_confirmed_closed", lambda day, ex: False)
    monkeypatch.setattr(pnl, "_calendar_cache", {})


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'disc.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    originals = (ledger.engine, book.engine)
    for mod in (ledger, book):
        mod.db_session.remove()
        mod.db_session.configure(bind=eng)
        monkeypatch.setattr(mod, "engine", eng)
    ledger.init_discretionary_ledger_db()
    book.init_discretionary_book_db()
    e = Env(eng)

    def fake_quotes(auth_token, feed_token, broker, symbols):
        e.quote_calls.append((broker, [(s["symbol"], s["exchange"]) for s in symbols]))
        if e.quote_fail:
            return False, {"status": "error", "message": "boom"}, 500
        results = []
        for s in symbols:
            v = e.quotes.get((s["symbol"], s["exchange"]))
            if isinstance(v, str):
                results.append({"symbol": s["symbol"], "exchange": s["exchange"], "error": v})
            elif v is None:
                results.append({"symbol": s["symbol"], "exchange": s["exchange"], "data": {}})
            else:
                results.append(
                    {"symbol": s["symbol"], "exchange": s["exchange"], "data": {"ltp": v}}
                )
        return True, {"status": "success", "results": results}, 200

    def fake_positionbook(auth_token, broker):
        e.positionbook_calls += 1
        return True, {"status": "success", "data": e.positionbook}, 200

    monkeypatch.setattr(pnl, "_now", lambda: e.now)
    monkeypatch.setattr(pnl, "_auth_provider", lambda broker, account=None: ("tok", "feed"))
    monkeypatch.setattr(pnl, "_account_auth_provider", lambda broker, account: ("tok", "feed"))
    # no real market calendar read in tests: the calendar "fails", so the clock windows apply
    monkeypatch.setattr(pnl, "_calendar_window", _calendar_down)
    monkeypatch.setattr(pnl, "_calendar_cache", {})
    monkeypatch.setattr(pnl, "_tables_ready", False)
    monkeypatch.delenv("DISCRETIONARY_MCX_CLOSE", raising=False)
    monkeypatch.setattr(pnl, "_get_multiquotes", fake_quotes)
    monkeypatch.setattr(pnl, "_get_positionbook", fake_positionbook)
    monkeypatch.setattr(pnl, "_quote_cache", {})
    monkeypatch.delenv("DISCRETIONARY_RELIABLE_SINCE", raising=False)
    monkeypatch.delenv("DISCRETIONARY_STALE_AFTER_S", raising=False)
    yield e
    for mod in (ledger, book):
        mod.db_session.remove()
        mod.db_session.configure(bind=originals[0] if mod is ledger else originals[1])


def d(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm)


# ---------------------------------------------------------------- golden cases through the DB


def test_golden_cases_through_the_service(env):
    # G1
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="G1")
    env.fill("BUY", 10, 110, d(17, 9, 25), symbol="G1")
    env.fill("SELL", 20, 120, d(17, 9, 30), symbol="G1")
    # G2
    env.fill("BUY", 30, 200, d(17, 9, 20), symbol="G2")
    env.fill("SELL", 10, 210, d(17, 9, 25), symbol="G2")
    env.fill("SELL", 20, 190, d(17, 9, 30), symbol="G2")
    # G3
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="G3")
    env.fill("SELL", 25, 90, d(17, 9, 25), symbol="G3")
    env.fill("BUY", 15, 80, d(17, 9, 30), symbol="G3")
    # G4
    env.fill("BUY", 10, 500, d(16, 14, 0), symbol="G4", product="CNC")
    env.fill("SELL", 10, 520, d(17, 10, 0), symbol="G4", product="CNC")
    # G5
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="G5", account="A")
    env.fill("SELL", 10, 110, d(17, 9, 30), symbol="G5", account="A")
    env.fill("BUY", 10, 120, d(17, 9, 20), symbol="G5", account="B")
    env.fill("SELL", 10, 115, d(17, 9, 30), symbol="G5", account="B")

    info = pnl.rebuild()
    assert info["rebuilt"] is True and info["trades"] == 8 and info["fills"] == 15

    def pnls(sym, **kw):
        return [round(t["realized_pnl"], 6) for t in pnl.get_trades(symbol=sym, **kw)]

    assert pnls("G1") == [300.0]
    assert pnl.get_trades(symbol="G1")[0]["entry_price"] == pytest.approx(105.0)
    assert pnls("G2") == [100.0, -200.0]
    assert pnls("G3") == [-100.0, 150.0]
    (g4,) = pnl.get_trades(symbol="G4")
    assert (g4["trade_date"], g4["overnight"], g4["hold_minutes"], g4["realized_pnl"]) == (
        "2026-11-17",
        True,
        1200.0,
        pytest.approx(200.0),
    )
    assert pnls("G5", account="A") == [100.0] and pnls("G5", account="B") == [-50.0]
    assert len(pnl.get_trades(symbol="G5")) == 2


# ---------------------------------------------------------------- rebuild / fingerprint


def test_rebuild_only_when_fingerprint_changes(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("SELL", 10, 101, d(17, 9, 30))
    first = pnl.rebuild()
    assert first["rebuilt"] is True
    second = pnl.rebuild()
    assert second["rebuilt"] is False
    assert second["build_id"] == first["build_id"] and second["fingerprint"] == first["fingerprint"]
    assert (second["trades"], second["positions"]) == (first["trades"], first["positions"])
    forced = pnl.rebuild(force=True)
    assert forced["rebuilt"] is True and forced["build_id"] != first["build_id"]
    assert forced["fingerprint"] == first["fingerprint"]
    assert env.count("disc_build_meta") == 1  # one build kept, never two


def test_reattribution_changes_fingerprint_and_forces_rebuild_on_next_read(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    sell_id = env.fill("SELL", 10, 110, d(17, 9, 30), state="unresolved")
    assert pnl.get_trades() == []  # the sell is unresolved: nothing closes
    before = pnl.rebuild()
    assert ledger.set_attribution(sell_id, "manual", None, "late", "none") is True
    trades = pnl.get_trades()  # a read triggers the rebuild
    assert [t["realized_pnl"] for t in trades] == [pytest.approx(100.0)]
    assert pnl.rebuild()["build_id"] != before["build_id"]


def test_new_fill_changes_fingerprint(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    a = pnl.rebuild()
    env.fill("SELL", 10, 100, d(17, 9, 30), state="strategy_tag")  # excluded, but a new row
    b = pnl.rebuild()
    assert b["rebuilt"] is True and b["fingerprint"] != a["fingerprint"]


def test_failed_build_leaves_previous_build_intact(env, monkeypatch):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("SELL", 10, 110, d(17, 9, 30))
    pnl.rebuild()
    snap = env.snapshot()
    env.fill("BUY", 1, 1, d(17, 9, 40))

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pnl, "allocate_charges", boom)
    with pytest.raises(RuntimeError):
        pnl.rebuild()
    after = env.snapshot()
    assert {k: v for k, v in after.items() if k in DERIVED} == {
        k: v for k, v in snap.items() if k in DERIVED
    }


def test_rebuild_is_deterministic_for_any_insertion_order(env):
    base = [
        ("BUY", 10, 100, d(17, 9, 20), "O1", "F1"),
        ("BUY", 5, 102, d(17, 9, 20), "O1", "F2"),
        ("SELL", 6, 105, d(17, 9, 40), "O2", "F1"),
        ("SELL", 9, 99, d(17, 9, 40), "O3", "F1"),
        ("BUY", 3, 98, d(17, 9, 40), "O0", "F9"),
    ]

    def shape(t):
        return (
            t["direction"],
            t["closed_quantity"],
            round(t["entry_price"], 9),
            t["exit_price"],
            round(t["realized_pnl"], 9),
            round(t["charges"], 9),
        )

    def build(order):
        with env.eng.begin() as c:
            for t in ("ledger_fills", *DERIVED):
                c.execute(text(f"DELETE FROM {t}"))
        for row in order:
            side, qty, price, when, oid, fid = row
            env.fill(side, qty, price, when, order_id=oid, fill_id=fid)
        pnl.rebuild(force=True)
        return [shape(t) for t in pnl.get_trades()], [
            (p["direction"], round(p["open_qty"], 9), round(p["avg_entry"], 9), p["status"])
            for p in pnl.get_positions(with_quotes=False)["positions"]
        ]

    reference = build(base)
    for seed in range(4):
        shuffled = base[:]
        random.Random(seed).shuffle(shuffled)
        assert build(shuffled) == reference


# ---------------------------------------------------------------- who counts


def test_unresolved_and_strategy_fills_are_excluded_and_unresolved_listed(env):
    env.fill("BUY", 10, 100, d(17, 9, 20), state="ui_manual")
    env.fill("SELL", 10, 110, d(17, 9, 30), state="manual")
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="UNR", state="unresolved")
    env.fill("SELL", 10, 150, d(17, 9, 30), symbol="UNR", state="unresolved")
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="STR", state="strategy_tag")
    env.fill("SELL", 10, 150, d(17, 9, 30), symbol="STR", state="strategy_engine")
    trades = pnl.get_trades()
    assert [t["symbol"] for t in trades] == ["SBIN"]
    assert {u["symbol"] for u in pnl.get_unresolved()} == {"UNR"}
    assert len(pnl.get_unresolved()) == 2
    assert pnl.get_unresolved(symbol="NOPE") == []
    summary = pnl.get_summary(with_quotes=False)
    assert any("unresolved" in w for w in summary["warnings"])
    assert summary["realized_gross"] == pytest.approx(100.0)


def test_opening_unknown_is_flagged_and_excluded(env):
    env.fill("SELL", 10, 520, d(17, 10, 0), product="CNC", symbol="HOLD")
    assert pnl.get_trades() == []
    orphans = pnl.get_orphans()
    assert [(o["symbol"], o["reason"], o["qty"]) for o in orphans] == [
        ("HOLD", "opening_unknown", 10.0)
    ]
    assert pnl.get_positions(with_quotes=False)["positions"] == []
    env.fill("BUY", 4, 500, d(17, 11, 0), product="CNC", symbol="HOLD")
    (pos,) = pnl.get_positions(with_quotes=False)["positions"]
    assert pos["opening_unknown"] is True and pos["open_qty"] == 4
    assert any("could not be placed" in w for w in pnl.get_summary(with_quotes=False)["warnings"])


def test_mis_short_is_a_genuine_position(env):
    env.fill("SELL", 10, 100, d(17, 9, 20))
    (pos,) = pnl.get_positions(with_quotes=False)["positions"]
    assert pos["direction"] == "SHORT" and pos["opening_unknown"] is False
    assert pnl.get_orphans() == []


def test_zero_or_negative_qty_or_price_rows_are_orphaned(env):
    for qty, price in ((0, 100), (5, 0), (-3, 10)):
        n = env.fill("BUY", 1, 1, d(17, 9, 20), symbol=f"BAD{qty}{price}")
        with env.eng.begin() as c:
            c.execute(
                text("UPDATE ledger_fills SET qty=:q, price=:p WHERE id=:i"),
                {"q": qty, "p": price, "i": n},
            )
    orphans = pnl.get_orphans()
    assert len(orphans) == 3 and {o["reason"] for o in orphans} == {"invalid_fill"}
    assert pnl.get_trades() == []


# ---------------------------------------------------------------- unrealized / quotes


def _open_long(env, **kw):
    env.fill("BUY", 10, 100, d(17, 9, 20), **kw)


def test_unrealized_live_and_never_stored(env):
    _open_long(env)
    env.quotes[("SBIN", "NSE")] = 106.0
    (p,) = pnl.get_positions()["positions"]
    assert (p["ltp"], p["price_status"], p["unrealized"]) == (106.0, "live", pytest.approx(60.0))
    assert p["fetched_at"] == "2026-11-17 11:00:00.000000"
    for table in ("disc_trades", "disc_positions"):
        with env.eng.connect() as c:
            cols = [r[1] for r in c.execute(text(f"PRAGMA table_info({table})"))]
        assert "unrealized" not in cols and "ltp" not in cols


def test_short_unrealized_sign(env):
    env.fill("SELL", 10, 100, d(17, 9, 20))
    env.quotes[("SBIN", "NSE")] = 94.0
    (p,) = pnl.get_positions()["positions"]
    assert p["unrealized"] == pytest.approx(60.0)


def test_one_batched_call_per_broker_for_distinct_pairs(env):
    env.fill("BUY", 10, 100, d(17, 9, 20), account="A")
    env.fill("BUY", 5, 100, d(17, 9, 21), account="B")  # same pair, other account
    env.fill("BUY", 5, 50, d(17, 9, 22), symbol="TCS")
    env.fill("BUY", 5, 70, d(17, 9, 23), symbol="INFY", broker="zerodha")
    env.quotes.update({("SBIN", "NSE"): 101.0, ("TCS", "NSE"): 51.0, ("INFY", "NSE"): 71.0})
    out = pnl.get_positions()["positions"]
    assert len(out) == 4
    assert len(env.quote_calls) == 2
    calls = {b: sorted(p) for b, p in env.quote_calls}
    assert calls == {"flattrade": [("SBIN", "NSE"), ("TCS", "NSE")], "zerodha": [("INFY", "NSE")]}


@pytest.mark.parametrize("bad", [0.0, -1.0, "no quote", None])
def test_bad_symbol_quote_is_unavailable(env, bad):
    _open_long(env)
    env.quotes[("SBIN", "NSE")] = bad
    (p,) = pnl.get_positions()["positions"]
    assert (p["price_status"], p["unrealized"], p["ltp"]) == ("unavailable", None, None)


def test_failed_call_without_cache_is_unavailable(env):
    _open_long(env)
    env.quote_fail = True
    (p,) = pnl.get_positions()["positions"]
    assert p["price_status"] == "unavailable" and p["unrealized"] is None


def test_cached_quote_after_whole_call_failure_is_labelled_cached_with_age(env, monkeypatch):
    _open_long(env)
    env.quotes[("SBIN", "NSE")] = 110.0
    (p,) = pnl.get_positions()["positions"]
    assert (p["price_status"], p["age_s"]) == ("live", pytest.approx(0.0))
    env.quote_fail = True
    env.now = datetime(2026, 11, 17, 11, 0, 30)
    (p,) = pnl.get_positions()["positions"]
    assert (p["price_status"], p["age_s"]) == ("cached", pytest.approx(30.0))
    assert p["ltp"] == 110.0 and p["unrealized"] == pytest.approx(100.0)
    env.now = datetime(2026, 11, 17, 11, 5, 0)
    (p,) = pnl.get_positions()["positions"]
    assert (p["price_status"], p["age_s"]) == ("cached", pytest.approx(300.0))
    assert p["unrealized"] == pytest.approx(100.0)
    summary = pnl.get_summary()
    assert any("cached quote" in w for w in summary["warnings"])
    assert not any("stale or last-close" in w for w in summary["warnings"])
    # a per-symbol failure on a successful call is unavailable, never the cache
    env.quote_fail = False
    env.quotes[("SBIN", "NSE")] = "no quote"
    (p,) = pnl.get_positions()["positions"]
    assert (p["price_status"], p["ltp"], p["age_s"]) == ("unavailable", None, None)


def test_fresh_but_old_quote_is_stale_and_every_price_has_age(env, monkeypatch):
    _open_long(env)
    env.quotes[("SBIN", "NSE")] = 110.0

    def slow_quotes(auth_token, feed_token, broker, symbols):
        out = (
            True,
            {"results": [{"symbol": "SBIN", "exchange": "NSE", "data": {"ltp": 110.0}}]},
            200,
        )
        env.now = datetime(2026, 11, 17, 11, 5, 0)  # the call itself took five minutes
        return out

    monkeypatch.setattr(pnl, "_get_multiquotes", slow_quotes)
    (p,) = pnl.get_positions()["positions"]
    assert (p["price_status"], p["age_s"]) == ("stale", pytest.approx(300.0))


def test_outside_market_hours_is_last_close_not_stale(env):
    _open_long(env)
    env.quotes[("SBIN", "NSE")] = 104.0
    env.now = datetime(2026, 11, 17, 18, 0, 0)
    (p,) = pnl.get_positions()["positions"]
    assert (p["price_status"], p["unrealized"]) == ("last_close", pytest.approx(40.0))
    env.now = datetime(2026, 11, 21, 11, 0, 0)  # Saturday
    assert pnl.get_positions()["positions"][0]["price_status"] == "last_close"


def test_with_quotes_false_makes_no_call(env):
    _open_long(env)
    (p,) = pnl.get_positions(with_quotes=False)["positions"]
    assert (
        env.quote_calls == [] and p["price_status"] == "not_requested" and p["unrealized"] is None
    )


def test_unrealized_never_reaches_metrics(env):
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="DONE")
    env.fill("SELL", 10, 110, d(17, 9, 30), symbol="DONE")
    env.fill("BUY", 100, 100, d(17, 9, 40), symbol="OPEN")
    env.quotes[("OPEN", "NSE")] = 200.0  # +10,000 unrealized
    s = pnl.get_summary()
    assert s["unrealized_total"] == pytest.approx(10_000.0)
    assert s["open_positions"] == 1
    m = s["metrics"]
    assert m["net_profit"] == pytest.approx(100.0) and m["trades_count"] == 1
    assert m["gross_profit"] == pytest.approx(100.0)
    assert m["equity_curve"][-1]["value"] == pytest.approx(100.0)
    assert "unrealized" in m["equity_basis"] and "excludes" in m["equity_basis"]


# ---------------------------------------------------------------- summary


def test_capital_based_metrics_are_withheld(env):
    for i in range(12):
        day = 2 + i
        env.fill("BUY", 10, 100, d(day, 9, 20), symbol=f"S{i}")
        env.fill("SELL", 10, 100 + (i % 3) - 1, d(day, 9, 40), symbol=f"S{i}")
    m = pnl.get_summary(with_quotes=False)["metrics"]
    for name in ("sharpe_ratio", "sortino_ratio", "ratio_capital", "return_pct"):
        assert m[name] is None, name
        assert m["withheld"][name] == "no capital basis"
    assert "no capital basis" in m["ratio_warning"]
    assert m["trades_count"] == 12


def test_summary_totals_equal_sum_of_parts_and_charges_flag(env):
    env.fill("BUY", 100, 1000, d(17, 9, 20), symbol="EQ")
    env.fill("SELL", 100, 1010, d(17, 9, 30), symbol="EQ")
    env.fill("BUY", 50, 100, d(17, 9, 20), symbol="OPT", exchange="NFO", product="NRML")
    env.fill("SELL", 50, 90, d(17, 9, 30), symbol="OPT", exchange="NFO", product="NRML")
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="OPENX", account="B")
    env.quotes[("OPENX", "NSE")] = 103.0
    s = pnl.get_summary()
    trades = pnl.get_trades()
    assert len(trades) == 2
    assert s["realized_gross"] == pytest.approx(1000.0 - 500.0)
    ok_charges = sum(t["charges"] for t in trades if t["charges"] is not None)
    assert s["charges"] == pytest.approx(ok_charges, abs=1e-3)
    assert round(ok_charges, 2) == 35.77  # flattrade: hand figure from the brief
    assert s["net"] == pytest.approx(s["realized_gross"] - s["charges"], abs=1e-3)
    assert s["charges_unavailable_trades"] == 1
    assert s["metrics"]["charges_unavailable_trades"] == 1
    assert any("no charge estimate" in w for w in s["warnings"])
    parts = s["per_identity"]
    assert s["realized_gross"] == pytest.approx(sum(p["realized_gross"] for p in parts), abs=1e-3)
    assert s["charges"] == pytest.approx(sum(p["charges"] for p in parts), abs=1e-3)
    assert s["unrealized_total"] == pytest.approx(sum(p["unrealized"] for p in parts), abs=1e-3)
    assert s["unrealized_total"] == pytest.approx(30.0)
    assert sum(p["trades"] for p in parts) == 2


def test_two_brokers_and_accounts_never_merge_in_summary(env):
    env.fill("BUY", 10, 100, d(17, 9, 20), broker="flattrade", account="A")
    env.fill("SELL", 10, 110, d(17, 9, 30), broker="flattrade", account="A")
    env.fill("BUY", 10, 100, d(17, 9, 20), broker="zerodha", account="Z")
    env.fill("SELL", 10, 95, d(17, 9, 30), broker="zerodha", account="Z")
    env.fill("BUY", 10, 100, d(17, 9, 20), broker="flattrade", account="B")
    env.fill("SELL", 10, 102, d(17, 9, 30), broker="flattrade", account="B")
    all_ = pnl.get_summary(with_quotes=False)
    assert len(all_["per_identity"]) == 3 and all_["metrics"]["trades_count"] == 3
    assert all_["realized_gross"] == pytest.approx(100 - 50 + 20)
    ft = pnl.get_summary(filters={"broker": "flattrade"}, with_quotes=False)
    assert ft["realized_gross"] == pytest.approx(120.0) and ft["metrics"]["trades_count"] == 2
    zr = pnl.get_summary(filters={"broker": "zerodha"}, with_quotes=False)
    assert zr["realized_gross"] == pytest.approx(-50.0)
    acct = pnl.get_summary(filters={"broker": "flattrade", "account": "B"}, with_quotes=False)
    assert acct["realized_gross"] == pytest.approx(20.0)
    assert {t["account"] for t in pnl.get_trades(broker="flattrade")} == {"A", "B"}


def test_period_range_and_discretionary_floor(env, monkeypatch):
    env.fill("BUY", 10, 100, d(3, 9, 20))
    env.fill("SELL", 10, 110, d(3, 9, 30))  # 2026-11-03 trade_date
    env.fill("BUY", 10, 100, d(10, 9, 20))
    env.fill("SELL", 10, 120, d(10, 9, 30))
    # default floor = session date of the first fill, far before LEDGER_RELIABLE_SINCE+... (2026-09-25)
    s = pnl.get_summary(period="all", with_quotes=False)
    assert s["period"]["reliable_since"] == "2026-11-03" and s["period"]["start"] == "2026-11-03"
    assert s["realized_gross"] == pytest.approx(300.0)
    assert pnl.get_summary(
        period="custom", start="2026-11-05", end="2026-11-17", with_quotes=False
    )["realized_gross"] == pytest.approx(200.0)
    monkeypatch.setenv("DISCRETIONARY_RELIABLE_SINCE", "2026-11-08")
    s = pnl.get_summary(period="all", with_quotes=False)
    assert s["period"]["start"] == "2026-11-08" and s["realized_gross"] == pytest.approx(200.0)
    # a floor earlier than the strategy ledger's own 2026-09-25 is honoured
    monkeypatch.setenv("DISCRETIONARY_RELIABLE_SINCE", "2026-01-01")
    custom = pnl.get_summary(
        period="custom", start="2026-02-01", end="2026-11-17", with_quotes=False
    )
    assert (
        custom["period"]["start"] == "2026-02-01"
    )  # the strategy ledger's own floor would give 2026-09-25
    # "1d" is the last traded session
    assert pnl.get_summary(period="1d", with_quotes=False)["period"]["start"] == "2026-11-10"


def test_summary_metrics_are_computed_from_gross_and_flag_costs(env):
    env.fill("BUY", 10, 100, d(17, 9, 20), exchange="NFO", product="NRML", symbol="OPT")
    env.fill("SELL", 10, 110, d(17, 9, 30), exchange="NFO", product="NRML", symbol="OPT")
    s = pnl.get_summary(with_quotes=False)
    assert s["metrics"]["net_profit"] == pytest.approx(100.0)  # gross, by close date
    assert s["charges"] == 0.0 and s["charges_unavailable_trades"] == 1


def test_metric_trade_shape():
    row = {
        "trade_date": "2026-11-17",
        "realized_pnl": 12.5,
        "symbol": "S",
        "exchange": "NSE",
        "product": "MIS",
        "direction": "LONG",
        "closed_quantity": 1,
        "entry_price": 1,
        "exit_price": 2,
        "broker": "b",
        "closed_at": "x",
        "opened_at": "y",
        "account": "A",
        "charges_status": "ok",
    }
    (m,) = pnl.to_metric_trades([row])
    assert m["strategy"] == "discretionary" and m["mode"] == "live" and m["realized_pnl"] == 12.5
    assert {"account", "charges_status", "opened_at", "closed_at", "broker"} <= set(m)


# ---------------------------------------------------------------- reconcile


def _bp(symbol, qty, avg, product="MIS", exchange="NSE"):
    return {
        "symbol": symbol,
        "exchange": exchange,
        "product": product,
        "quantity": qty,
        "average_price": avg,
    }


def test_reconcile_statuses_and_no_writes(env):
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="MATCH")
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="QTY")
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="AVG")
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="GONE")
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="FLAT")
    env.fill("SELL", 10, 101, d(17, 9, 30), symbol="FLAT")
    env.fill("BUY", 20, 100, d(17, 9, 20), symbol="MIXED", state="strategy_tag")
    env.fill("SELL", 5, 100, d(17, 9, 30), symbol="MIXED", state="manual")
    env.fill("BUY", 10, 100, d(17, 9, 20), symbol="OTHERACCT", account="B")
    env.positionbook = [
        _bp("MATCH", 10, 100.04),
        _bp("QTY", 7, 100),
        _bp("AVG", 10, 100.06),
        _bp("FLAT", 0, 0),
        _bp("MIXED", 15, 100),
        _bp("NEW", 3, 50),
        _bp("ZERO", 0, 0),
    ]
    pnl.rebuild()
    before = env.snapshot()
    report = pnl.reconcile("flattrade", "A")
    assert env.snapshot() == before  # nothing written, ledger or derived
    assert env.positionbook_calls == 1
    status = {r["identity"]["symbol"]: r["status"] for r in report}
    assert status == {
        "MATCH": "match",
        "QTY": "qty_mismatch",
        "AVG": "avg_mismatch",
        "GONE": "missing_in_broker",
        "FLAT": "match",
        "MIXED": "match",
        "NEW": "missing_in_ledger",
    }
    assert "OTHERACCT" not in status
    mixed = next(r for r in report if r["identity"]["symbol"] == "MIXED")
    assert (mixed["ledger_qty"], mixed["broker_qty"], mixed["discretionary_qty"]) == (
        15.0,
        15.0,
        -5.0,
    )
    assert mixed["mixed_attribution"] is True
    assert (
        next(r for r in report if r["identity"]["symbol"] == "MATCH")["mixed_attribution"] is False
    )
    qty = next(r for r in report if r["identity"]["symbol"] == "QTY")
    assert (qty["ledger_qty"], qty["broker_qty"], qty["ledger_avg"], qty["broker_avg"]) == (
        10.0,
        7.0,
        100.0,
        100.0,
    )
    assert qty["identity"] == {
        "broker": "flattrade",
        "account": "A",
        "exchange": "NSE",
        "symbol": "QTY",
        "product": "MIS",
    }


def test_reconcile_does_not_write_even_when_derived_book_is_stale(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    before = env.snapshot()  # derived tables empty: reconcile must not build them
    pnl.reconcile("flattrade", "A")
    assert env.snapshot() == before


def test_reconcile_mis_counts_only_the_current_session(env):
    env.fill("BUY", 10, 100, d(16, 9, 20))  # yesterday's MIS: squared off, not carried
    env.fill("BUY", 4, 100, d(17, 9, 20))
    env.fill("BUY", 6, 100, d(16, 9, 20), symbol="NR", product="NRML", exchange="NFO")  # carries
    env.positionbook = [_bp("SBIN", 4, 100), _bp("NR", 6, 100, "NRML", "NFO")]
    status = {r["identity"]["symbol"]: r["status"] for r in pnl.reconcile("flattrade", "A")}
    assert status == {"SBIN": "match", "NR": "match"}


def test_reconcile_failure_raises_and_writes_nothing(env, monkeypatch):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    monkeypatch.setattr(pnl, "_get_positionbook", lambda t, b: (False, {"status": "error"}, 500))
    before = env.snapshot()
    with pytest.raises(pnl.ReconcileError):
        pnl.reconcile("flattrade", "A")
    assert env.snapshot() == before
    monkeypatch.setattr(pnl, "_account_auth_provider", lambda broker, account: None)
    with pytest.raises(pnl.ReconcileError):
        pnl.reconcile("flattrade", "A")


def test_no_secret_in_results(env):
    _open_long(env)
    env.quotes[("SBIN", "NSE")] = 101.0
    blob = repr(pnl.get_summary()) + repr(pnl.get_positions())
    assert "tok" not in blob.replace("total", "").replace("token", "")


def test_market_open_windows():
    assert pnl.market_open("NSE", datetime(2026, 11, 17, 9, 15))
    assert not pnl.market_open("NSE", datetime(2026, 11, 17, 15, 30))
    assert pnl.market_open("MCX", datetime(2026, 11, 17, 20, 0))
    assert not pnl.market_open("MCX", datetime(2026, 11, 21, 12, 0))
    assert pnl.market_open("CDS", datetime(2026, 11, 17, 16, 59))


# ---------------------------------------------------------------- B1 through the service / reconcile


def test_nfo_nrml_option_round_trip_through_the_service(env):
    kw = {"product": "NRML", "exchange": "NFO", "symbol": "OPT"}
    env.fill("SELL", 75, 120, d(16, 10, 0), **kw)
    env.fill("BUY", 75, 80, d(17, 10, 0), **kw)
    (t,) = pnl.get_trades()
    assert (t["direction"], t["realized_pnl"]) == ("SHORT", pytest.approx(3000.0))
    assert pnl.get_orphans() == [] and pnl.get_positions()["positions"] == []
    env.positionbook = []
    (row,) = pnl.reconcile("flattrade", "A")
    assert row["ledger_qty"] == 0.0 and row["discretionary_qty"] == 0.0 and row["status"] == "match"


def test_reconcile_ledger_and_discretionary_qty_agree_for_an_open_nrml_short(env):
    kw = {"product": "NRML", "exchange": "NFO", "symbol": "OPT"}
    env.fill("SELL", 75, 120, d(16, 10, 0), **kw)
    env.positionbook = [_bp("OPT", -75, 120, "NRML", "NFO")]
    (row,) = pnl.reconcile("flattrade", "A")
    assert (row["ledger_qty"], row["discretionary_qty"], row["status"]) == (-75.0, -75.0, "match")
    (pos,) = pnl.get_positions(with_quotes=False)["positions"]
    assert pos["direction"] == "SHORT" and pos["open_qty"] == 75


def test_cash_nrml_sell_is_orphaned_and_visible_in_warnings(env):
    env.fill("SELL", 10, 100, d(17, 10, 0), product="NRML", symbol="HOLD")
    assert [o["reason"] for o in pnl.get_orphans()] == ["opening_unknown"]
    assert any("could not be placed" in w for w in pnl.get_summary(with_quotes=False)["warnings"])


# ---------------------------------------------------------------- M1 account match


def test_reconcile_requires_the_exact_account_session_and_writes_nothing(env, monkeypatch):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("BUY", 3, 100, d(17, 9, 20), account="B")
    env.positionbook = [_bp("SBIN", 10, 100)]
    pnl.rebuild()
    # only user B is logged in for the broker
    monkeypatch.setattr(pnl, "_auth_rows", lambda broker: [("userB", "B")])
    monkeypatch.setattr(pnl, "_tokens_for", lambda name: ("tokB", "feedB"))
    monkeypatch.setattr(pnl, "_account_auth_provider", pnl._default_account_auth)
    before = env.snapshot()
    with pytest.raises(pnl.ReconcileError) as err:
        pnl.reconcile("flattrade", "A")
    assert "A" in str(err.value) and "tokB" not in str(err.value)
    assert env.positionbook_calls == 0  # the other account's book was never fetched
    assert env.snapshot() == before
    # the exact account (by broker user id) is accepted
    assert pnl.reconcile("flattrade", "B")[0]["status"] in ("qty_mismatch", "match")
    assert env.positionbook_calls == 1


def test_quotes_may_fall_back_to_any_user_of_the_broker(monkeypatch):
    monkeypatch.setattr(pnl, "_auth_rows", lambda broker: [("userB", "B")])
    monkeypatch.setattr(pnl, "_tokens_for", lambda name: ("tokB", "feedB"))
    assert pnl._default_auth("flattrade", "A") == ("tokB", "feedB")  # LTP is account independent
    assert pnl._default_account_auth("flattrade", "A") is None
    assert pnl._default_account_auth("flattrade", "userB") == ("tokB", "feedB")


# ---------------------------------------------------------------- M2 carried CNC


def test_carried_cnc_is_reported_separately_and_never_a_mismatch(env):
    env.fill("BUY", 10, 500, d(13, 14, 0), product="CNC", symbol="HOLD")  # delivered, in holdings
    env.fill("BUY", 5, 100, d(17, 9, 30), product="CNC", symbol="TODAY")
    env.fill("BUY", 4, 100, d(13, 9, 30), product="CNC", symbol="BOTH")
    env.fill("BUY", 2, 110, d(17, 9, 30), product="CNC", symbol="BOTH")
    env.fill("BUY", 6, 100, d(13, 9, 20), product="NRML", symbol="NR", exchange="NFO")
    env.positionbook = [
        _bp("TODAY", 5, 100, "CNC"),
        _bp("BOTH", 2, 110, "CNC"),
        _bp("NR", 6, 100, "NRML", "NFO"),
    ]
    report = pnl.reconcile("flattrade", "A")
    by = {(r["identity"]["symbol"], r["scope"]): r for r in report}
    assert by[("HOLD", "carried")]["status"] == "carried_not_comparable"
    assert by[("HOLD", "carried")]["ledger_qty"] == 10.0
    assert by[("BOTH", "carried")]["status"] == "carried_not_comparable"
    assert by[("BOTH", "session")]["status"] == "match"
    assert by[("TODAY", "session")]["status"] == "match"
    assert by[("NR", "whole_ledger")]["status"] == "match"  # NRML keeps the whole ledger
    assert not [r for r in report if r["status"] in ("missing_in_broker", "qty_mismatch")]
    assert len(report) == 5


def test_carried_cnc_still_in_positionbook_is_not_missing_in_ledger(env):
    env.fill("BUY", 10, 500, d(16, 14, 0), product="CNC", symbol="HOLD")
    env.positionbook = [_bp("HOLD", 10, 500, "CNC")]
    (row,) = pnl.reconcile("flattrade", "A")
    assert (row["status"], row["scope"], row["broker_qty"]) == (
        "carried_not_comparable",
        "carried",
        None,  # the session row of the identity carries the broker qty
    )


# ---------------------------------------------------------------- M3 market hours


def _ms(day, hh, mm=0):
    return (
        int((datetime(2026, 11, day, hh, mm) - datetime(1970, 1, 1)).total_seconds() * 1000)
        - 19_800_000
    )


def test_calendar_holiday_on_a_weekday_is_closed(env, monkeypatch):
    calls = []

    def calendar(day, exchange):
        calls.append((day, exchange))
        return None  # exchange holiday

    monkeypatch.setattr(pnl, "_calendar_window", calendar)
    monkeypatch.setattr(pnl, "_confirmed_closed", lambda day, ex: True)
    assert pnl.market_status("NSE", datetime(2026, 11, 17, 11, 0)) == (False, "calendar")
    assert pnl.market_open("NSE", datetime(2026, 11, 17, 11, 5)) is False
    assert pnl._calendar_cache == {}  # a closed answer is never cached as a window
    _open_long(env)
    env.quotes[("SBIN", "NSE")] = 104.0
    (p,) = pnl.get_positions()["positions"]
    assert p["price_status"] == "last_close"
    assert pnl.get_positions()["market_hours_source"] == "calendar"
    assert pnl.get_summary()["market_hours_source"] == "calendar"


def test_calendar_open_window_special_hours_and_mcx_evening(env, monkeypatch):
    windows = {"NSE": (_ms(17, 9, 15), _ms(17, 15, 40)), "MCX": (_ms(17, 9, 0), _ms(17, 23, 55))}
    monkeypatch.setattr(pnl, "_calendar_window", lambda day, ex: windows.get(ex))
    assert pnl.market_status("NSE", datetime(2026, 11, 17, 15, 35)) == (True, "calendar")
    assert pnl.market_status("NSE", datetime(2026, 11, 17, 15, 40)) == (False, "calendar")
    assert pnl.market_status("MCX", datetime(2026, 11, 17, 23, 40)) == (True, "calendar")
    assert pnl.market_status("MCX", datetime(2026, 11, 17, 23, 55)) == (False, "calendar")
    assert pnl.market_status("MCX", datetime(2026, 11, 17, 8, 59)) == (False, "calendar")


def test_calendar_failure_falls_back_to_clock_and_is_not_cached(env, monkeypatch):
    state = {"down": True}

    def calendar(day, exchange):
        if state["down"]:
            raise RuntimeError("db down")
        return None

    monkeypatch.setattr(pnl, "_calendar_window", calendar)
    monkeypatch.setattr(pnl, "_confirmed_closed", lambda day, ex: True)
    assert pnl.market_status("NSE", datetime(2026, 11, 17, 11, 0)) == (True, "clock")
    assert pnl.market_status("NSE", datetime(2026, 11, 21, 11, 0)) == (False, "clock")
    state["down"] = False
    assert pnl.market_status("NSE", datetime(2026, 11, 17, 11, 0)) == (False, "calendar")
    _open_long(env)
    state["down"] = True
    pnl._calendar_cache.clear()  # a new day / a restart
    env.quotes[("SBIN", "NSE")] = 104.0
    assert pnl.get_summary()["market_hours_source"] == "clock"


def test_clock_fallback_mcx_close_is_2355_and_configurable(env, monkeypatch):
    assert pnl.market_open("MCX", datetime(2026, 11, 17, 23, 40))
    assert pnl.market_open("MCX", datetime(2026, 11, 17, 23, 54))
    assert not pnl.market_open("MCX", datetime(2026, 11, 17, 23, 55))
    monkeypatch.setenv("DISCRETIONARY_MCX_CLOSE", "23:30")
    assert not pnl.market_open("MCX", datetime(2026, 11, 17, 23, 40))
    monkeypatch.setenv("DISCRETIONARY_MCX_CLOSE", "junk")
    assert pnl.market_open("MCX", datetime(2026, 11, 17, 23, 40))
    assert pnl.market_open("NFO", datetime(2026, 11, 17, 15, 35))  # F&O runs to 15:40


# ---------------------------------------------------------------- m2 fingerprint


def test_fingerprint_changes_with_session_expiry_and_cost_model(env, monkeypatch):
    import dataclasses

    from portfolio import broker_fees

    env.fill("BUY", 10, 100, d(17, 9, 20))
    fills = pnl._load_ledger_fills()
    base = pnl.fingerprint(fills)
    assert pnl.fingerprint(fills) == base  # stable
    monkeypatch.setenv("SESSION_EXPIRY_TIME", "05:30")
    expiry_fp = pnl.fingerprint(fills)
    assert expiry_fp != base
    monkeypatch.delenv("SESSION_EXPIRY_TIME")
    changed = dataclasses.replace(broker_fees.BROKER_FEES["flattrade"], dp_charge=25.0)
    monkeypatch.setitem(broker_fees.BROKER_FEES, "flattrade", changed)
    assert pnl.fingerprint(fills) not in (base, expiry_fp)


def test_fingerprint_changes_with_cost_schedule_and_engine_version(env, monkeypatch):
    import dataclasses

    from services import discretionary_charges as charges

    env.fill("BUY", 10, 100, d(17, 9, 20))
    fills = pnl._load_ledger_fills()
    base = pnl.fingerprint(fills)
    real = charges.schedule_for

    def cheaper_tax(exchange, product, broker):
        sched = real(exchange, product, broker)
        return None if sched is None else dataclasses.replace(sched, tax_rate=0.5)

    monkeypatch.setattr(charges, "schedule_for", cheaper_tax)
    assert pnl.fingerprint(fills) != base
    monkeypatch.setattr(charges, "schedule_for", real)
    assert pnl.fingerprint(fills) == base
    monkeypatch.setattr(pnl, "ENGINE_VERSION", "99")
    assert pnl.fingerprint(fills) != base


def test_cost_model_change_forces_a_rebuild_on_next_read(env, monkeypatch):
    import dataclasses

    from portfolio import broker_fees

    env.fill("BUY", 10, 100, d(16, 10, 0), product="CNC")
    env.fill("SELL", 10, 110, d(17, 10, 0), product="CNC")
    first = pnl.rebuild()
    before = pnl.get_trades()[0]["charges"]
    changed = dataclasses.replace(broker_fees.BROKER_FEES["flattrade"], dp_charge=50.0)
    monkeypatch.setitem(broker_fees.BROKER_FEES, "flattrade", changed)
    after = pnl.get_trades()[0]["charges"]
    assert after > before
    assert pnl.rebuild()["build_id"] != first["build_id"]


# ---------------------------------------------------------------- m3 snapshot reads


def test_get_summary_reads_one_snapshot_for_everything(env, monkeypatch):
    env.fill("BUY", 10, 100, d(16, 10, 0))  # open MIS from a prior session
    env.fill("BUY", 5, 100, d(17, 10, 0), symbol="X")
    env.fill("SELL", 5, 110, d(17, 10, 30), symbol="X")
    env.fill("SELL", 5, 50, d(17, 10, 30), symbol="OR", product="CNC")  # orphan
    env.fill("BUY", 1, 50, d(17, 10, 30), symbol="U", state="unresolved")
    seen = {"ledger": 0, "fp": 0, "init": 0, "reads": []}
    real_load, real_fp, real_init = (
        pnl._load_ledger_fills,
        pnl.fingerprint,
        book.init_discretionary_book_db,
    )

    def load():
        seen["ledger"] += 1
        return real_load()

    def fp(fills):
        seen["fp"] += 1
        return real_fp(fills)

    def init():
        seen["init"] += 1
        real_init()

    monkeypatch.setattr(pnl, "_load_ledger_fills", load)
    monkeypatch.setattr(pnl, "fingerprint", fp)
    monkeypatch.setattr(book, "init_discretionary_book_db", init)
    for name in ("load_trades", "load_positions", "load_orphans"):
        real = getattr(book, name)

        def spy(*a, _real=real, _name=name, **k):
            seen["reads"].append((_name, k.get("build_id")))
            return _real(*a, **k)

        monkeypatch.setattr(book, name, spy)
    summary = pnl.get_summary(with_quotes=False)
    assert seen["ledger"] == 1 and seen["fp"] == 1
    ids = {bid for name, bid in seen["reads"] if name != "load_trades" or bid}
    assert ids == {summary["build_id"]}
    assert {n for n, _ in seen["reads"]} >= {"load_trades", "load_positions", "load_orphans"}
    assert all(
        bid == summary["build_id"]
        for n, bid in seen["reads"]
        if n in ("load_positions", "load_orphans")
    )
    assert summary["open_positions"] == 1 and any("unresolved" in w for w in summary["warnings"])
    # create_all once per process, not once per read
    pnl.get_summary(with_quotes=False)
    pnl.get_trades()
    pnl.get_orphans()
    assert seen["init"] == 1


def test_concurrent_get_summary_builds_once_with_one_build_id(env, monkeypatch):
    import threading

    for i in range(5):
        env.fill("BUY", 10, 100, d(17, 9, 20 + i), symbol=f"S{i}")
        env.fill("SELL", 10, 101, d(17, 10, 20 + i), symbol=f"S{i}")
    builds = []
    real = book.replace_build

    def counting(meta, *a, **k):
        builds.append(meta["build_id"])
        return real(meta, *a, **k)

    monkeypatch.setattr(book, "replace_build", counting)
    barrier = threading.Barrier(2)
    out, errors = [], []

    def work():
        try:
            barrier.wait(timeout=10)
            out.append(pnl.get_summary(with_quotes=False))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors and len(out) == 2
    assert len(builds) == 1
    assert out[0]["build_id"] == out[1]["build_id"] == builds[0]
    assert out[0]["realized_gross"] == out[1]["realized_gross"] == pytest.approx(50.0)


# ---------------------------------------------------------------- m4 / m5


def test_open_mis_from_a_prior_session_warns(env):
    env.fill("BUY", 10, 100, d(16, 10, 0))
    warnings = pnl.get_summary(with_quotes=False)["warnings"]
    assert any("MIS position" in w and "earlier session" in w for w in warnings)


def test_open_mis_from_the_current_session_does_not_warn(env):
    env.fill("BUY", 10, 100, d(17, 10, 0))
    assert not any("MIS" in w for w in pnl.get_summary(with_quotes=False)["warnings"])


def test_summary_names_net_and_gross_explicitly(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("SELL", 10, 110, d(17, 9, 30))
    out = pnl.get_summary(with_quotes=False)
    assert out["realized_gross"] == pytest.approx(100.0)
    assert out["charges"] > 0
    assert (
        out["realized_net_of_charges"]
        == out["net"]
        == pytest.approx(out["realized_gross"] - out["charges"], abs=1e-3)
    )
    assert out["metrics"]["net_profit"] == pytest.approx(out["realized_gross"])
    assert "before charges" in out["net_definition"]


def test_real_calendar_none_with_a_failing_holiday_read_uses_the_clock(monkeypatch):
    """get_effective_session_window returns None both for 'closed' and for its own failure."""
    from database import market_calendar_db as cal

    # scoped: only these names are patched; the env fixture's patches stay in place
    with monkeypatch.context() as m:
        m.setattr(pnl, "_calendar_window", _real_calendar_window)
        m.setattr(pnl, "_confirmed_closed", _real_confirmed_closed)
        m.setattr(cal, "get_effective_session_window", lambda day, ex: None)
        m.setattr(cal, "is_market_holiday", lambda day, ex=None: False)  # weekday, not a holiday
        assert pnl.market_status("NSE", datetime(2026, 11, 17, 11, 0)) == (True, "clock")
        assert pnl._calendar_cache == {}
        m.setattr(cal, "is_market_holiday", lambda day, ex=None: True)
        assert pnl.market_status("NSE", datetime(2026, 11, 17, 11, 0)) == (False, "calendar")
    assert pnl._calendar_window is not _real_calendar_window  # fixture patch intact


def test_transient_calendar_failure_is_not_cached_as_closed(monkeypatch):
    state = {"window": None, "holiday": False}
    monkeypatch.setattr(pnl, "_calendar_window", lambda day, ex: state["window"])
    monkeypatch.setattr(pnl, "_confirmed_closed", lambda day, ex: state["holiday"])
    t = datetime(2026, 11, 17, 11, 0)
    assert pnl.market_status("NSE", t) == (True, "clock")  # None, not confirmed closed
    assert pnl._calendar_cache == {}
    state["window"] = (_ms(17, 9, 15), _ms(17, 15, 30))  # the calendar recovers
    assert pnl.market_status("NSE", t) == (True, "calendar")
    assert list(pnl._calendar_cache.values()) == [state["window"]]
    old = (datetime(2026, 11, 16).date(), "NSE")
    pnl._calendar_cache[old] = state["window"]
    pnl.market_status("NSE", t)
    assert old not in pnl._calendar_cache  # per-day reset


# ---------------------------------------------------------------- review minors


def _row(**kw):
    base = {
        "id": 1,
        "attribution_state": "manual",
        "qty": 10.0,
        "price": 100.0,
        "fill_time": datetime(2026, 11, 17, 9, 20),
        "side": "BUY",
        "symbol": "SBIN",
        "exchange": "NSE",
        "product": "MIS",
        "account": "A",
        "broker": "flattrade",
        "order_id": "O1",
    }
    base.update(kw)
    return base


@pytest.mark.parametrize(
    "field,value",
    [
        ("side", "SELL"),
        ("symbol", "TCS"),
        ("exchange", "BSE"),
        ("product", "CNC"),
        ("account", "B"),
        ("broker", "zerodha"),
        ("order_id", "O2"),
    ],
)
def test_fingerprint_changes_with_every_identity_field(field, value):
    assert pnl.fingerprint([_row()]) != pnl.fingerprint([_row(**{field: value})])


def test_fingerprint_is_stable_for_identical_input():
    assert pnl.fingerprint([_row()]) == pnl.fingerprint([_row()])


def test_user_id_match_beats_a_name_match_on_another_row(monkeypatch):
    # row 1's NAME equals the account; row 2's USER ID equals it: the user id wins
    monkeypatch.setattr(pnl, "_auth_rows", lambda broker: [("A", "uidX"), ("nameY", "A")])
    monkeypatch.setattr(pnl, "_tokens_for", lambda name: (f"tok-{name}", "feed"))
    assert pnl._default_account_auth("flattrade", "A") == ("tok-nameY", "feed")


def test_account_match_is_exact_and_case_sensitive(monkeypatch):
    monkeypatch.setattr(pnl, "_auth_rows", lambda broker: [("Alice", "UID1")])
    monkeypatch.setattr(pnl, "_tokens_for", lambda name: ("tok", "feed"))
    assert pnl._default_account_auth("flattrade", "Alice") == ("tok", "feed")
    assert pnl._default_account_auth("flattrade", "UID1") == ("tok", "feed")
    for bad in ("alice", "uid1", " Alice", "Alice ", ""):
        assert pnl._default_account_auth("flattrade", bad) is None


def test_real_auth_rows_broker_match_is_case_insensitive(monkeypatch):
    import database.auth_db as auth_db

    class Row:
        def __init__(self, name, uid, broker, revoked=False):
            self.name, self.user_id, self.broker, self.is_revoked = name, uid, broker, revoked

    rows = [
        Row("n1", "u1", "FlatTrade"),
        Row("n2", "u2", " flattrade "),
        Row("n3", "u3", "zerodha"),
        Row("n4", "u4", "flattrade", revoked=True),
        Row("", "u5", "flattrade"),
    ]

    class Query:
        def filter_by(self, **kw):
            assert kw == {"is_revoked": False}
            return self

        def all(self):
            return [r for r in rows if not r.is_revoked]

    class Session:
        removed = 0

        def remove(self):
            Session.removed += 1

    monkeypatch.setattr(auth_db.Auth, "query", Query(), raising=False)
    monkeypatch.setattr(auth_db, "db_session", Session())
    assert pnl._auth_rows("flattrade") == [("n1", "u1"), ("n2", "u2")]
    assert Session.removed == 1


def test_snapshot_retry_exhaustion_raises_runtimeerror(env, monkeypatch):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    monkeypatch.setattr(
        pnl.book,
        "get_meta",
        lambda *a, **k: {"build_id": "someone-else", "fingerprint": "x", "engine_version": 0},
    )
    with pytest.raises(RuntimeError, match="kept changing"):
        pnl._snapshot({}, trades=True)


def test_fingerprint_flip_flop_logs_one_warning(env, monkeypatch, caplog):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    monkeypatch.setattr(pnl, "_last_mismatch", None)
    caplog.set_level("WARNING", logger=pnl.logger.name)

    def me():  # this process: SESSION_EXPIRY_TIME 03:00
        monkeypatch.setenv("SESSION_EXPIRY_TIME", "03:00")
        pnl.rebuild()

    def other_process():  # another process with SESSION_EXPIRY_TIME 04:00 rewrites the book
        mine = pnl._last_mismatch
        monkeypatch.setenv("SESSION_EXPIRY_TIME", "04:00")
        pnl.rebuild()
        pnl._last_mismatch = mine

    me()
    other_process()
    me()  # first mismatch: recorded, no warning
    assert not [r for r in caplog.records if "flip-flop" in r.getMessage()]
    other_process()
    me()  # the same mismatch again with unchanged fills
    flips = [r for r in caplog.records if "flip-flop" in r.getMessage()]
    assert len(flips) == 1
    assert flips[0].getMessage().isascii()


def test_carried_cnc_row_has_no_broker_qty_but_the_session_row_keeps_it(env):
    env.fill("BUY", 4, 100, d(13, 9, 30), product="CNC", symbol="BOTH")
    env.fill("BUY", 2, 110, d(17, 9, 30), product="CNC", symbol="BOTH")
    env.positionbook = [_bp("BOTH", 6, 103.0, "CNC")]
    by = {r["scope"]: r for r in pnl.reconcile("flattrade", "A")}
    assert by["carried"]["broker_qty"] is None
    assert by["session"]["broker_qty"] == 6.0


# ---------------------------------------------------------------- sync health (ledger_sync_runs)


def _run(env, status, when, broker="flattrade", account="A"):
    ledger.record_sync_run(
        broker=broker,
        account=account,
        started_at=when,
        finished_at=when,
        status=status,
        fetched=0,
        inserted=0,
    )


def _sync_warnings(out):
    keys = ("sync", "weekday", "TradeBook")
    return [w for w in out["warnings"] if any(k in w for k in keys)]


def test_empty_ledger_only_has_the_no_sync_yet_note(env):
    out = pnl.get_summary(with_quotes=False)
    assert [w for w in out["warnings"] if "not requested" not in w] == [
        "no successful ledger sync yet: fills are not being collected; check the broker login."
    ]
    assert out["sync_health"] == {
        "last_successful_sync": None,
        "runs_by_day": {},
        "days_without_sync": [],
    }


def test_last_successful_sync_is_reported(env):
    _run(env, "ok", d(17, 10, 30))
    out = pnl.get_summary(with_quotes=False)
    assert "last successful sync: 2026-11-17 10:30 IST" in out["warnings"]
    assert out["sync_health"]["last_successful_sync"] == "2026-11-17 10:30"
    assert out["sync_health"]["runs_by_day"] == {"2026-11-17": {"ok": 1}}
    assert out["sync_health"]["days_without_sync"] == []
    assert len([w for w in out["warnings"] if "not requested" not in w]) == 1


def test_no_ok_run_at_all_warns_even_with_failed_runs(env):
    _run(env, "no_auth", d(17, 9, 30))
    out = pnl.get_summary(with_quotes=False)
    assert any(w.startswith("no successful ledger sync yet") for w in out["warnings"])
    assert any("ended no_auth" in w for w in out["warnings"])


def test_stale_last_success_on_a_weekday_warns(env):
    _run(env, "ok", d(13, 10, 0))  # Friday; today is Tuesday the 17th: 4 days
    out = pnl.get_summary(with_quotes=False)
    assert any("older than 2 days" in w for w in out["warnings"])
    _run(env, "ok", d(16, 10, 0))
    fresh = pnl.get_summary(with_quotes=False)
    assert not any("older than 2 days" in w for w in fresh["warnings"])


def test_stale_check_is_skipped_on_a_weekend(env):
    _run(env, "ok", d(13, 10, 0))
    env.now = d(15, 11, 0)  # Sunday
    out = pnl.get_summary(with_quotes=False)
    assert not any("older than 2 days" in w for w in out["warnings"])


def test_weekdays_without_a_successful_run_are_listed(env):
    _run(env, "ok", d(10, 10, 0))  # Tue
    _run(env, "error", d(11, 10, 0))  # Wed, failed only
    _run(env, "ok", d(12, 10, 0))  # Thu
    # Fri 13 none; Sat 14 / Sun 15 skipped; Mon 16 none; today Tue 17 in progress
    out = pnl.get_summary(with_quotes=False, period="all")
    assert out["sync_health"]["days_without_sync"] == ["2026-11-11", "2026-11-13", "2026-11-16"]
    w = [x for x in out["warnings"] if x.startswith("no successful ledger sync on weekday")]
    assert len(w) == 1
    assert "2026-11-11, 2026-11-13, 2026-11-16" in w[0]
    assert "current-day book" in w[0] and "holidays" in w[0]


def test_missing_days_list_is_capped_at_ten(env):
    _run(env, "ok", datetime(2026, 9, 1, 10, 0))  # Tuesday
    out = pnl.get_summary(with_quotes=False, period="all")
    assert len(out["sync_health"]["days_without_sync"]) > 10
    w = next(x for x in out["warnings"] if x.startswith("no successful ledger sync on weekday"))
    n = len(out["sync_health"]["days_without_sync"]) - 10
    assert w.count("2026-") == 10 and f"and {n} more" in w


def test_calendar_holiday_is_excluded_and_failure_is_safe(env, monkeypatch):
    _run(env, "ok", d(13, 10, 0))
    monkeypatch.setattr(pnl, "_confirmed_closed", lambda day, ex: day.day == 16)
    out = pnl.get_summary(with_quotes=False, period="all")
    assert out["sync_health"]["days_without_sync"] == []

    def boom(day, ex):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(pnl, "_confirmed_closed", boom)
    out = pnl.get_summary(with_quotes=False, period="all")
    assert out["sync_health"]["days_without_sync"] == ["2026-11-16"]
    assert any("market calendar is unavailable" in w for w in out["warnings"])


def test_latest_run_per_account_in_a_bad_state_warns(env):
    _run(env, "ok", d(17, 9, 30), account="A")
    _run(env, "broker_error", d(17, 10, 30), account="A")
    _run(env, "ok", d(17, 9, 30), broker="zerodha", account="Z")
    _run(env, "partial", d(17, 10, 0), broker="zerodha", account="Z")
    _run(env, "error", d(17, 9, 0), broker="dhan", account="D")
    _run(env, "ok", d(17, 10, 0), broker="dhan", account="D")  # recovered: no warning
    out = pnl.get_summary(with_quotes=False)
    bad = [w for w in out["warnings"] if w.startswith("latest ledger sync")]
    assert len(bad) == 2
    assert any("flattrade account A ended broker_error" in w for w in bad)
    assert any("zerodha account Z ended partial" in w for w in bad)
    assert out["sync_health"]["runs_by_day"]["2026-11-17"]["ok"] == 3


def test_runs_by_day_covers_only_the_last_seven_days(env):
    _run(env, "ok", d(9, 10, 0))  # 8 days ago
    _run(env, "ok", d(11, 10, 0))
    _run(env, "error", d(11, 11, 0))
    out = pnl.get_summary(with_quotes=False, period="all")
    assert out["sync_health"]["runs_by_day"] == {"2026-11-11": {"ok": 1, "error": 1}}


def test_unreadable_sync_runs_never_break_the_summary(env, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(pnl, "_load_sync_runs", boom)
    out = pnl.get_summary(with_quotes=False)
    assert out["sync_health"]["last_successful_sync"] is None
    assert not any("sync" in w for w in out["warnings"])


def test_sync_health_makes_no_broker_call(env):
    _run(env, "ok", d(17, 10, 0))
    env.fill("BUY", 10, 100, d(17, 10, 0))
    pnl.get_summary(with_quotes=False)
    assert env.quote_calls == [] and env.positionbook_calls == 0


# ---------------------------------------------------------------- canonical account identity


def test_reconcile_session_lookup_through_the_identity_table(env, monkeypatch):
    # Flattrade: Auth.user_id is NULL, the fills carry the broker account id
    monkeypatch.setattr(pnl, "_auth_rows", lambda broker: [("alice", None), ("bob", None)])
    monkeypatch.setattr(pnl, "_tokens_for", lambda name: (f"tok-{name}", "feed"))
    assert pnl._default_account_auth("flattrade", "FZ74") is None
    ledger.remember_identity("flattrade", "alice", "FZ74")
    assert pnl._default_account_auth("flattrade", "FZ74") == ("tok-alice", "feed")
    assert pnl._default_account_auth("flattrade", "FZ75") is None


def test_identity_of_another_broker_does_not_match(env, monkeypatch):
    monkeypatch.setattr(pnl, "_auth_rows", lambda broker: [("alice", None)])
    monkeypatch.setattr(pnl, "_tokens_for", lambda name: (f"tok-{name}", "feed"))
    ledger.remember_identity("zerodha", "alice", "FZ74")
    assert pnl._default_account_auth("flattrade", "FZ74") is None


def _latest_warnings(out):
    return [w for w in out["warnings"] if w.startswith("latest ledger sync")]


def test_account_less_failed_runs_warn_at_broker_level(env):
    _run(env, "ok", d(17, 9, 30), account="FZ74")
    _run(env, "no_auth", d(17, 10, 30), account="")
    _run(env, "broker_error", d(17, 10, 45), broker="zerodha", account="")
    out = pnl.get_summary(with_quotes=False)
    bad = _latest_warnings(out)
    assert sorted(bad) == [
        "latest ledger sync for broker flattrade (no account known) ended no_auth at "
        "2026-11-17 10:30.",
        "latest ledger sync for broker zerodha (no account known) ended broker_error at "
        "2026-11-17 10:45.",
    ]
    assert out["sync_health"]["runs_by_day"]["2026-11-17"] == {
        "ok": 1,
        "no_auth": 1,
        "broker_error": 1,
    }


def test_account_less_error_with_broker_less_label_warns_globally(env):
    _run(env, "ok", d(17, 9, 30), account="FZ74")
    _run(env, "no_auth", d(17, 10, 30), broker="", account="")  # early no_auth path
    bad = _latest_warnings(pnl.get_summary(with_quotes=False))
    assert bad == [
        "latest ledger sync for broker ? (no account known) ended no_auth at 2026-11-17 10:30."
    ]


def test_ok_run_after_an_account_less_failure_clears_the_warning(env):
    _run(env, "no_auth", d(17, 9, 30), account="")
    assert len(_latest_warnings(pnl.get_summary(with_quotes=False))) == 1
    _run(env, "ok", d(17, 10, 0), account="")
    assert _latest_warnings(pnl.get_summary(with_quotes=False)) == []
    _run(env, "error", d(17, 10, 30), account="")
    assert len(_latest_warnings(pnl.get_summary(with_quotes=False))) == 1
    _run(env, "ok", d(17, 11, 0), account="FZ74")  # a later run of the broker, any label
    assert _latest_warnings(pnl.get_summary(with_quotes=False)) == []


def test_no_duplicate_when_the_account_run_already_warns(env):
    _run(env, "no_auth", d(17, 9, 30), account="")
    _run(env, "error", d(17, 11, 0), account="FZ74")
    bad = _latest_warnings(pnl.get_summary(with_quotes=False))
    assert len(bad) == 1 and "account FZ74 ended error" in bad[0]


def test_account_less_failure_after_an_account_run_warns_once_at_broker_level(env):
    _run(env, "error", d(17, 9, 30), account="FZ74")  # superseded by the later run below
    _run(env, "ok", d(17, 10, 0), account="FZ74")
    _run(env, "no_auth", d(17, 11, 0), account="")
    bad = _latest_warnings(pnl.get_summary(with_quotes=False))
    assert len(bad) == 1 and "(no account known) ended no_auth" in bad[0]


# ---------------------------------------------------------------- completed trades
# A completed trade is one position from flat to flat (a reversal ends it). Money (realized,
# charges, equity, daily P&L, period rows) follows the closing legs; counts and ratios follow
# completed trades. Every expected number below is computed by hand in the comment.


def _perf(env, ltp=105.0, symbol="SBIN", **kw):
    env.quotes[(symbol, "NSE")] = ltp
    return pnl.get_performance(period="all", with_quotes=True, **kw)


def _counts(perf):
    """Every trade count of the performance payload except the headline."""
    rows = [
        *perf["by_period"]["daily"],
        *perf["by_period"]["weekly"],
        *perf["by_period"]["monthly"],
        *perf["by_symbol"],
        *perf["by_side"],
        *perf["by_product"],
        *perf["by_holding"].values(),
        *perf["per_identity"],
    ]
    return [r["trades"] for r in rows]


def _assert_money_reconciles(perf):
    m = perf["metrics"]
    gross = perf["realized_gross"]
    for g in ("daily", "weekly", "monthly"):
        rows = perf["by_period"][g]
        assert sum(r["realized_gross"] for r in rows) == pytest.approx(gross, abs=1e-3)
        assert sum(r["charges"] for r in rows) == pytest.approx(perf["charges"], abs=1e-3)
        assert sum(r["trades"] for r in rows) == m["trades_count"]
    assert sum(r["realized_gross"] for r in perf["per_identity"]) == pytest.approx(gross, abs=1e-3)
    assert sum(r["trades"] for r in perf["per_identity"]) == m["trades_count"]
    for name in ("by_symbol", "by_side", "by_product"):
        assert sum(r["trades"] for r in perf[name]) == m["trades_count"]
        assert sum(r["realized_gross"] for r in perf[name]) == pytest.approx(gross, abs=1e-3)
    holding = perf["by_holding"].values()
    assert sum(r["trades"] for r in holding) == m["trades_count"]
    assert sum(r["realized_gross"] for r in holding) == pytest.approx(gross, abs=1e-3)
    last = m["equity_curve"][-1]["value"] if m["equity_curve"] else 0.0
    assert last == pytest.approx(round(gross, 2)) and m["net_profit"] == pytest.approx(gross)
    assert perf["trades_total"] == m["trades_count"]


def _scenario_a(env):
    env.fill("BUY", 45, 100, d(17, 9, 20))
    env.fill("SELL", 20, 110, d(17, 9, 50))


def test_scenario_a_partial_exit_is_realized_but_not_a_trade(env):
    _scenario_a(env)
    perf = _perf(env)
    # realized 20 * (110 - 100) = 200 on the day of the partial exit; unrealized
    # 25 * (105 - 100) = 125 on the open 25 @ 100
    assert perf["realized_gross"] == pytest.approx(200.0)
    assert perf["unrealized_total"] == pytest.approx(125.0)
    (pos,) = perf["positions"]
    assert (pos["direction"], pos["open_qty"], pos["avg_entry"]) == ("LONG", 25.0, 100.0)
    assert pos["realized_so_far"] == pytest.approx(200.0)
    m = perf["metrics"]
    assert m["trades_count"] == 0 and m["winning_trades"] == 0 and m["losing_trades"] == 0
    # no completed trade: nothing to rate (compute_metrics answers 0; the UI shows a dash)
    assert (m["win_rate"], m["profit_factor"], m["avg_win"], m["expectancy"]) == (0, 0, 0, 0)
    assert perf["trades"] == [] and perf["trades_total"] == 0
    assert set(_counts(perf)) == {0}
    assert perf["hold"]["overall"]["count"] == 0
    assert m["daily_pnl"][-1] == {"date": "2026-11-17", "pnl": 200.0}
    (day,) = perf["by_period"]["daily"]
    assert (day["trades"], day["realized_gross"]) == (0, 200.0)
    _assert_money_reconciles(perf)


def test_scenario_b_full_exit_makes_one_trade_of_all_legs(env):
    _scenario_a(env)
    env.fill("SELL", 25, 108, d(17, 10, 20))
    perf = _perf(env)
    # 200 + 25 * (108 - 100) = 400; average exit (20 * 110 + 25 * 108) / 45 = 108.8889
    assert perf["realized_gross"] == pytest.approx(400.0)
    assert perf["positions"] == [] and perf["unrealized_total"] == 0.0
    (t,) = perf["trades"]
    assert t["closed_quantity"] == 45.0 and t["legs"] == 2
    assert t["entry_price"] == pytest.approx(100.0)
    assert t["exit_price"] == pytest.approx(4900.0 / 45)
    assert t["realized_pnl"] == pytest.approx(400.0)
    assert t["hold_minutes"] == 60.0  # 09:20 first entry to 10:20 last exit
    assert t["opened_at"] == "2026-11-17 09:20:00" and t["closed_at"] == "2026-11-17 10:20:00"
    assert t["net_pnl"] == pytest.approx(400.0 - t["charges"])
    assert t["charges"] == pytest.approx(perf["charges"], abs=1e-4)  # one trade: all charges
    m = perf["metrics"]
    assert (m["trades_count"], m["win_rate"], m["profit_factor"]) == (1, 100.0, None)
    assert (m["avg_win"], m["expectancy"]) == (400.0, 400.0)
    assert perf["hold"]["overall"]["mean_minutes"] == 60.0
    _assert_money_reconciles(perf)


def test_scenario_c_a_winning_leg_inside_a_losing_trade_is_a_loss(env):
    _scenario_a(env)
    env.fill("SELL", 25, 90, d(17, 10, 20))
    perf = _perf(env)
    # 200 + 25 * (90 - 100) = -50: one losing trade (the +200 leg is not a win)
    assert perf["realized_gross"] == pytest.approx(-50.0)
    m = perf["metrics"]
    assert (m["trades_count"], m["winning_trades"], m["losing_trades"]) == (1, 0, 1)
    assert (m["win_rate"], m["profit_factor"]) == (0.0, 0.0)
    assert (m["avg_loss"], m["expectancy"]) == (50.0, -50.0)
    (row,) = perf["by_symbol"]
    assert (row["wins"], row["losses"], row["win_rate"]) == (0, 1, 0.0)
    _assert_money_reconciles(perf)


def test_scenario_d1_adding_after_a_partial_exit_keeps_one_open_position(env):
    _scenario_a(env)
    env.fill("BUY", 10, 104, d(17, 10, 20))
    perf = _perf(env)
    # open (25 * 100 + 10 * 104) / 35 = 101.142857; unrealized 35 * (105 - 101.142857) = 135
    (pos,) = perf["positions"]
    assert pos["open_qty"] == 35.0 and pos["avg_entry"] == pytest.approx(3540.0 / 35)
    assert perf["unrealized_total"] == pytest.approx(135.0)
    assert perf["realized_gross"] == pytest.approx(200.0) and pos["realized_so_far"] == 200.0
    assert perf["metrics"]["trades_count"] == 0 and set(_counts(perf)) == {0}
    _assert_money_reconciles(perf)


def test_scenario_d2_reversal_ends_the_trade_and_opens_a_new_position(env):
    env.fill("BUY", 45, 100, d(17, 9, 20))
    env.fill("SELL", 60, 110, d(17, 9, 50))
    perf = _perf(env)
    # long closed: 45 * (110 - 100) = 450; new short 15 @ 110, unrealized 15 * (110 - 105) = 75
    assert perf["realized_gross"] == pytest.approx(450.0)
    (pos,) = perf["positions"]
    assert (pos["direction"], pos["open_qty"], pos["avg_entry"]) == ("SHORT", 15.0, 110.0)
    assert pos["realized_so_far"] == 0.0
    assert perf["unrealized_total"] == pytest.approx(75.0)
    (t,) = perf["trades"]
    assert (t["direction"], t["closed_quantity"], t["legs"]) == ("LONG", 45.0, 1)
    assert perf["metrics"]["trades_count"] == 1
    _assert_money_reconciles(perf)


def test_completed_trades_are_deterministic_across_rebuilds_and_insert_order(env):
    fills = [
        ("BUY", 45, 100, d(17, 9, 20)),
        ("SELL", 20, 110, d(17, 9, 50)),
        ("SELL", 25, 108, d(17, 10, 20)),
        ("BUY", 10, 101, d(17, 10, 25)),
        ("SELL", 30, 99, d(17, 10, 30)),
    ]

    def shape(perf):
        keep = ("closed_quantity", "entry_price", "exit_price", "realized_pnl", "legs")
        return (
            [tuple(round(t[k], 9) for k in keep) for t in perf["trades"]],
            perf["metrics"]["trades_count"],
            perf["realized_gross"],
            [(p["direction"], p["open_qty"], p["avg_entry"]) for p in perf["positions"]],
        )

    for f in fills:
        env.fill(*f)
    first = shape(_perf(env))
    pnl.rebuild(force=True)
    assert shape(_perf(env)) == first
    with env.eng.begin() as c:
        c.execute(text("DELETE FROM ledger_fills"))
    shuffled = fills[:]
    random.Random(7).shuffle(shuffled)
    for f in shuffled:
        env.fill(*f)
    assert shape(_perf(env)) == first
    # trades: +400 (2 legs) and -20 (the 10 @ 101 closed at 99); the reversal opens SHORT 20
    assert first[0] == [(10.0, 101.0, 99.0, -20.0, 1), (45.0, 100.0, round(4900 / 45, 9), 400.0, 2)]


def test_tcs_replay_is_one_losing_trade_with_unchanged_charges(env):
    for side, qty, price, when in (
        ("BUY", 4, 2392.6, d(17, 9, 20)),
        ("BUY", 21, 2392.0, d(17, 9, 21)),
        ("BUY", 20, 2401.8, d(17, 9, 22)),
        ("SELL", 30, 2395.2, d(17, 10, 0)),
        ("SELL", 15, 2395.2, d(17, 10, 1)),
    ):
        env.fill(side, qty, price, when, symbol="TCS")
    perf = _perf(env, ltp=2400.0, symbol="TCS")
    # buys 9570.4 + 50232 + 48036 = 107838.4, sells 45 * 2395.2 = 107784 -> -54.40
    assert perf["realized_gross"] == pytest.approx(-54.40)
    assert perf["charges"] == pytest.approx(38.2467, abs=1e-4)  # same figure as per leg
    (t,) = perf["trades"]
    assert (t["closed_quantity"], t["legs"]) == (45.0, 2)
    assert t["entry_price"] == pytest.approx(107838.4 / 45)
    m = perf["metrics"]
    assert (m["trades_count"], m["win_rate"], m["profit_factor"]) == (1, 0.0, 0.0)
    _assert_money_reconciles(perf)


def test_partial_exit_on_day_one_and_close_on_day_two(env):
    env.fill("BUY", 45, 100, d(16, 9, 20))
    env.fill("SELL", 20, 110, d(16, 9, 50))
    env.fill("SELL", 25, 108, d(17, 10, 20))
    perf = _perf(env)
    daily = {r["period"]: r for r in perf["by_period"]["daily"]}
    # day 1: the +200 leg, no trade; day 2: the +200 leg and the ONE trade (+400)
    assert (daily["2026-11-16"]["trades"], daily["2026-11-16"]["realized_gross"]) == (0, 200.0)
    assert (daily["2026-11-17"]["trades"], daily["2026-11-17"]["realized_gross"]) == (1, 200.0)
    assert daily["2026-11-17"]["wins"] == 1
    (t,) = perf["trades"]
    assert t["trade_date"] == "2026-11-17" and t["overnight"] is True
    assert t["realized_pnl"] == pytest.approx(400.0)
    pts = {p["date"]: p["pnl"] for p in perf["metrics"]["daily_pnl"]}
    assert pts["2026-11-16"] == 200.0 and pts["2026-11-17"] == 200.0
    # a period holding only day 1 shows its money and no trade
    day1 = pnl.get_performance(period="custom", start="2026-11-16", end="2026-11-16")
    assert day1["realized_gross"] == pytest.approx(200.0) and day1["metrics"]["trades_count"] == 0
    assert day1["trades"] == []
    day2 = pnl.get_performance(period="custom", start="2026-11-17", end="2026-11-17")
    assert day2["realized_gross"] == pytest.approx(200.0) and day2["metrics"]["trades_count"] == 1
    _assert_money_reconciles(perf)


def test_reconciliation_holds_under_filters_and_truncation(env, monkeypatch):
    _scenario_a(env)  # SBIN A: partial exit, still open
    env.fill("SELL", 25, 108, d(17, 10, 20))  # ... now closed: one trade
    env.fill("BUY", 10, 50, d(17, 9, 0), symbol="ITC", account="B")
    env.fill("SELL", 4, 55, d(17, 9, 5), symbol="ITC", account="B")  # open, partial +20
    env.fill("BUY", 5, 200, d(16, 9, 0), symbol="INFY", broker="zerodha", account="Z")
    env.fill("SELL", 5, 190, d(16, 9, 30), symbol="INFY", broker="zerodha", account="Z")
    env.fill("BUY", 5, 200, d(16, 10, 0), symbol="INFY", broker="zerodha", account="Z")
    env.fill("SELL", 5, 204, d(16, 10, 30), symbol="INFY", broker="zerodha", account="Z")
    monkeypatch.setattr(pnl, "TABLE_LIMIT", 1)
    full = pnl.get_performance(period="all")
    # SBIN +400 (1 trade), ITC +20 (no trade), INFY -50 and +20 (2 trades) -> 390, 3 trades
    assert full["realized_gross"] == pytest.approx(390.0)
    assert full["metrics"]["trades_count"] == 3 and full["trades_total"] == 3
    assert len(full["trades"]) == 1 and full["trades_truncated"] == 2
    _assert_money_reconciles(full)
    for kw, gross, count in (
        ({"broker": "zerodha"}, -30.0, 2),
        ({"symbol": "ITC"}, 20.0, 0),
        ({"account": "A"}, 400.0, 1),
    ):
        perf = pnl.get_performance(period="all", **kw)
        assert perf["realized_gross"] == pytest.approx(gross), kw
        assert perf["metrics"]["trades_count"] == count, kw
        _assert_money_reconciles(perf)


def test_completed_trade_fills_cover_every_leg_and_reject_a_stale_build(env):
    _scenario_a(env)
    env.fill("SELL", 25, 108, d(17, 10, 20))
    perf = pnl.get_performance(period="all")
    (t,) = perf["trades"]
    out = pnl.get_trade_fills(t["id"], build_id=perf["build_id"])
    assert [(f["role"], f["side"], f["qty_used"], f["price"]) for f in out["fills"]] == [
        ("ENTRY", "BUY", 45.0, 100.0),
        ("EXIT", "SELL", 20.0, 110.0),
        ("EXIT", "SELL", 25.0, 108.0),
    ]
    assert out["trade"]["legs"] == 2 and out["linked_fills"] == 4
    # the merged charges equal the trade's charges
    assert sum(f["charge_allocated"] for f in out["fills"]) == pytest.approx(t["charges"])
    env.fill("BUY", 1, 10, d(17, 10, 40), symbol="ITC")
    with pytest.raises(pnl.BuildChanged):
        pnl.get_trade_fills(t["id"], build_id=perf["build_id"])
    assert pnl.get_trade_fills(999999) is None


def test_open_position_id_is_not_a_trade(env):
    _scenario_a(env)
    perf = pnl.get_performance(period="all")
    with env.eng.connect() as c:
        pid = c.execute(text("SELECT id FROM disc_positions")).scalar()
    assert pnl.get_trade_fills(pid, build_id=perf["build_id"]) is None
    assert pnl.get_completed_trades() == [] and len(pnl.get_trades()) == 1  # one leg


def test_quotes_off_default_makes_no_broker_call_with_completed_trades(env):
    _scenario_a(env)
    perf = pnl.get_performance(period="all")
    assert env.quote_calls == [] and env.positionbook_calls == 0
    assert perf["positions"][0]["unrealized"] is None
    assert perf["positions"][0]["realized_so_far"] == 200.0
