"""
Tests for the strategy performance report generator and its saved-report routes.

Run with: uv run pytest test/test_strategy_performance_report_service.py -v

Small fixture databases in tmp_path only. No operational database is opened.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blueprints.auth as auth_bp_module  # noqa: E402
import blueprints.strategy_pnl as strategy_pnl_module  # noqa: E402
import services.strategy_performance_report_service as svc  # noqa: E402
from limiter import limiter  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 13, 16, 0, tzinfo=IST)  # a Friday
EVIL = "<b>evil</b>"

SCHEMA = """create table strategy_closed_trades (
 id integer primary key, user_id text, strategy text, symbol text, exchange text, product text,
 mode text, direction text, closed_quantity float, entry_price float, exit_price float,
 realized_pnl float, trade_date text, closed_at text, broker text, opened_at text)"""


def _make_ledger(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    for i, (strategy, mode, day, pnl) in enumerate(rows, start=1):
        conn.execute(
            "insert into strategy_closed_trades values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                "u",
                strategy,
                f"SYM{i % 3}",
                "NSE",
                "MIS",
                mode,
                "LONG",
                10,
                100.0,
                100.0 + pnl / 10,
                pnl,
                day,
                f"{day} 11:00:00",
                "flattrade",
                f"{day} 10:00:00",
            ),
        )
    conn.commit()
    conn.close()


def _rows():
    days = [f"2026-10-{d:02d}" for d in (1, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19)]  # 12 sessions
    rows = []
    for day in days:
        rows.append(("ALPHA", "analyze", day, 120.0))
        rows.append(("ALPHA", "analyze", day, -60.0))
    for _ in range(3):
        rows.append((EVIL, "analyze", days[-1], 50.0))
    rows.append(("BEFORE-FLOOR", "analyze", "2026-09-10", 999.0))  # before LEDGER_RELIABLE_SINCE
    return rows


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    _make_ledger(path, _rows())
    return path


def _gen(mode, ledger, out):
    return svc.generate_report(
        mode, out_dir=out, ledger_db=ledger, signal_db=ledger.parent / "none.db", now=NOW
    )


def test_paper_report_numbers_and_floor(ledger, tmp_path):
    out = tmp_path / "out"
    path = _gen("paper", ledger, out)
    assert path.name == "paper-2026-11-13.html"
    html = path.read_text(encoding="utf-8")
    assert "Strategy Performance Report - Paper" in html
    assert "ALPHA" in html
    assert "BEFORE-FLOOR" not in html  # excluded: before the ledger reliable-since date
    assert "Charges are estimated" in html
    assert "<script" not in html and "http://" not in html and "https://" not in html
    # 5 recent sessions x 2 trades, +120 and -60 each: gross +300, net a little under that
    import re

    assert re.search(
        r"<b>ALPHA</b></td><td>10</td><td>Rs 29\d</td><td>50%</td><td>2\.00</td>", html
    )


def test_live_unavailable_states_it_plainly(ledger, tmp_path):
    html = _gen("live", ledger, tmp_path / "out").read_text(encoding="utf-8")
    assert "No live performance report can yet be produced" in html
    assert "ALPHA" not in html  # paper rows never stand in for live


def test_both_keeps_modes_separate(ledger, tmp_path):
    html = _gen("both", ledger, tmp_path / "out").read_text(encoding="utf-8")
    assert "<h3>Paper</h3>" in html and "<h3>Live</h3>" in html
    assert "No live performance report can yet be produced" in html
    assert "never added together" in html
    assert "combined total" not in html.lower()


def test_both_with_live_rows_never_mixes(tmp_path):
    rows = _rows() + [("LIVEONLY", "live", "2026-10-19", 500.0)]
    path = tmp_path / "ledger.db"
    _make_ledger(path, rows)
    html = _gen("both", path, tmp_path / "out").read_text(encoding="utf-8")
    paper, live = html.split("<h3>Live</h3>", 1)[0], html.split("<h3>Live</h3>", 1)[1]
    assert "LIVEONLY" not in paper.split("<h3>Paper</h3>", 1)[1].split("<h2>2.")[0]
    assert "LIVEONLY" in live
    only_paper = _gen("paper", path, tmp_path / "out2").read_text(encoding="utf-8")
    assert "LIVEONLY" not in only_paper


def test_insufficient_data_wording_uses_existing_rules(ledger, tmp_path):
    html = _gen("paper", ledger, tmp_path / "out").read_text(encoding="utf-8")
    assert "not assessed (fewer than 10 trades)" in html  # the 3-trade strategy
    assert "insufficient data" in html
    assert "low confidence (10 to 19 trades)" in html  # ALPHA recent: 10 trades


def test_dynamic_text_is_escaped(ledger, tmp_path):
    html = _gen("paper", ledger, tmp_path / "out").read_text(encoding="utf-8")
    assert EVIL not in html
    assert "&lt;b&gt;evil&lt;/b&gt;" in html
    assert "Content-Security-Policy" in html


def test_error_writes_no_file(tmp_path):
    out = tmp_path / "out"
    with pytest.raises(svc.ReportError):
        svc.generate_report("paper", out_dir=out, ledger_db=tmp_path / "missing.db", now=NOW)
    assert not out.exists() or list(out.iterdir()) == []
    with pytest.raises(svc.ReportError):
        svc.generate_report("weekly", out_dir=out, now=NOW)
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all")
    with pytest.raises(svc.ReportError):
        svc.generate_report("paper", out_dir=out, ledger_db=broken, now=NOW)
    assert not out.exists() or list(out.iterdir()) == []


def test_history_kept_and_same_day_replaced(ledger, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    old = out / "paper-2026-11-06.html"
    old.write_text("last week", encoding="utf-8")
    first = _gen("paper", ledger, out)
    assert old.read_text(encoding="utf-8") == "last week"  # a different day is never touched
    assert first.name == "paper-2026-11-13.html"
    first.write_text("stale", encoding="utf-8")
    again = _gen("paper", ledger, out)
    assert "Strategy Performance Report" in again.read_text(encoding="utf-8")
    assert sorted(p.name for p in out.iterdir()) == [
        "paper-2026-11-06.html",
        "paper-2026-11-13.html",
    ]


def test_failed_run_keeps_existing_file_untouched(ledger, tmp_path):
    out = tmp_path / "out"
    good = _gen("paper", ledger, out)
    before = good.read_text(encoding="utf-8")
    with pytest.raises(svc.ReportError):
        svc.generate_report("paper", out_dir=out, ledger_db=tmp_path / "gone.db", now=NOW)
    assert good.read_text(encoding="utf-8") == before
    assert [p.name for p in out.iterdir()] == [good.name]  # no leftover temp file


def test_list_and_report_path_whitelist(tmp_path):
    (tmp_path / "paper-2026-11-13.html").write_text("a", encoding="utf-8")
    (tmp_path / "both-2026-11-06.html").write_text("b", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "paper-2026-11-13.html.part").write_text("x", encoding="utf-8")
    names = [r["filename"] for r in svc.list_reports(tmp_path)]
    assert names == ["paper-2026-11-13.html", "both-2026-11-06.html"]
    assert svc.report_path("paper-2026-11-13.html", tmp_path) is not None
    for bad in (
        "../x.html",
        "..%2fpaper-2026-11-13.html",
        "paper-2026-11-13.html/",
        "notes.txt",
        "paper-2026-11-99x.html",
        "/etc/passwd",
        "paper-2026-11-13.HTML",
        "",
    ):
        assert svc.report_path(bad, tmp_path) is None


def test_symlink_is_not_served(tmp_path):
    secret = tmp_path / "secret.html"
    secret.write_text("secret", encoding="utf-8")
    folder = tmp_path / "reports"
    folder.mkdir()
    (folder / "paper-2026-11-13.html").symlink_to(secret)
    assert svc.report_path("paper-2026-11-13.html", folder) is None
    assert svc.list_reports(folder) == []


# ----------------------------------------------------------------------------- routes


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(strategy_pnl_module.strategy_pnl_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    return app


def _log_in(client):
    with client.session_transaction() as session:
        session["user"] = "tester"
        session["logged_in"] = True
        session["login_time"] = "2026-01-01T09:00:00+05:30"
        session["broker"] = "flattrade"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    d = tmp_path / "strategy-performance"
    d.mkdir()
    (d / "paper-2026-11-13.html").write_text("<html>paper</html>", encoding="utf-8")
    monkeypatch.setattr(svc, "REPORT_DIR", d)
    import utils.session as session_utils

    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    return d


def test_routes_require_login(folder):
    with _app().test_client() as client:
        assert client.get("/api/strategy-pnl/reports").status_code in (302, 401)
        assert client.get("/api/strategy-pnl/reports/paper-2026-11-13.html").status_code in (
            302,
            401,
        )


def test_list_view_and_download(folder):
    with _app().test_client() as client:
        _log_in(client)
        listing = client.get("/api/strategy-pnl/reports").get_json()
        assert listing["status"] == "success"
        assert [r["filename"] for r in listing["reports"]] == ["paper-2026-11-13.html"]

        view = client.get("/api/strategy-pnl/reports/paper-2026-11-13.html")
        assert view.status_code == 200
        assert view.data == b"<html>paper</html>"
        assert "sandbox" in view.headers["Content-Security-Policy"]
        assert "default-src 'none'" in view.headers["Content-Security-Policy"]
        assert view.headers["Content-Disposition"].startswith("inline")

        dl = client.get("/api/strategy-pnl/reports/paper-2026-11-13.html?download=1")
        assert dl.headers["Content-Disposition"] == 'attachment; filename="paper-2026-11-13.html"'


@pytest.mark.parametrize(
    "name",
    [
        "..%2f..%2fetc%2fpasswd",
        "..",
        "paper-2026-11-13.html%2f..",
        "notes.txt",
        "%2e%2e%2fapp.py",
        "paper-2026-11-13.html.part",
        "paper-2026-11-14.html",
    ],
)
def test_traversal_and_unknown_names_are_404(folder, name):
    with _app().test_client() as client:
        _log_in(client)
        assert client.get(f"/api/strategy-pnl/reports/{name}").status_code == 404


def test_list_error_is_reported(folder, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(strategy_pnl_module, "list_reports", boom)
    with _app().test_client() as client:
        _log_in(client)
        res = client.get("/api/strategy-pnl/reports")
        assert res.status_code == 500
        assert res.get_json()["status"] == "error"
