"""Tests for signal_engine.analysis.execution_health using synthetic log fixtures."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from signal_engine.analysis import execution_health as eh
from signal_engine.timeutils import IST

DAY = date(2026, 10, 29)


def _oa(ts: str, level: str, name: str, msg: str) -> str:
    return f"[{ts},123] {level} in {name}: {msg}\n"


def _se(ts: str, level: str, sym: str, name: str, msg: str) -> str:
    return f"{ts} | {level:<7} | {sym:<12} | {name} | {msg}\n"


LIMITER = (
    "Flattrade data limiter, last 60s: critical n=5 avg_wait=1.0s max_wait={cw}s delayed=1 "
    "refused={rf}; normal n=100 avg_wait=0.1s max_wait=1.0s delayed=2 refused=0; background "
    "n=30 avg_wait=0.0s max_wait=0.0s delayed=0 refused=0; broker_rejections={br} timeouts=0 "
    "latency_avg=0.1s latency_max=0.3s cap={cap}"
)


def _write_openalgo(root: Path, day: str = "2026-10-29", cw: str = "1.5", cap: str = "7/s,110/min"):
    d = root / "log"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        _oa(
            f"{day} 09:41:08",
            "WARNING",
            "rate_limit",
            "Flattrade rate limit hit on NTPC@NSE (Invalid Input :  Order Recieved 11 in a "
            "current second exceeds Limit 10 for user). Retrying in 2.0s (attempt 1/3)",
        ),
        _oa(f"{day} 09:50:00", "INFO", "rate_limit", LIMITER.format(cw=cw, rf=0, br=1, cap=cap)),
        _oa(f"{day} 09:51:00", "INFO", "rate_limit", LIMITER.format(cw="0.2", rf=2, br=0, cap=cap)),
        _oa(
            f"{day} 09:35:29",
            "WARNING",
            "order_manager",
            "Slow sandbox place_order NTPC MARKET: total=6885ms validate=1ms position_lookup=1ms "
            "price=1850ms margin=1ms insert=1ms execute=1ms rest=1ms quote_calls_ms=[1850]",
        ),
        _oa(
            f"{day} 10:00:00",
            "ERROR",
            "flattrade_websocket",
            "WebSocket error: Connection to remote host was lost.",
        ),
        _oa(
            f"{day} 10:00:00",
            "ERROR",
            "flattrade_adapter",
            "WebSocket error: Connection to remote host was lost.",
        ),
        _oa(
            f"{day} 10:01:30",
            "INFO",
            "flattrade_websocket",
            "Market data is flowing; watching for tick silence from here on",
        ),
        _oa(
            f"{day} 11:00:00",
            "ERROR",
            "history_service",
            "Error in broker_module.get_history: Error fetching historical data: "
            "<ConnectionTerminated error_code:1>",
        ),
        "Traceback continuation line\n",
        _oa(
            f"{day} 11:12:15",
            "WARNING",
            "data",
            "Error processing quote for NTPC@NSE: [Errno -3] Temporary failure in name resolution",
        ),
        _oa(
            "2026-10-28 11:12:15",
            "WARNING",
            "data",
            "[Errno -3] Temporary failure in name resolution",
        ),
    ]
    # duplicate limiter line (two handlers) must be deduplicated
    lines.append(lines[1])
    (d / f"openalgo_{day}.log").write_text("".join(lines), encoding="utf-8")


def _write_signal(root: Path, day: str = "2026-10-29"):
    d = root / "signal_engine" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        _se(f"{day} 09:50:20", "ERROR", "NTPC", "executor", "Timeout sending order for NTPC"),
        _se(
            f"{day} 10:00:46",
            "CRITICAL",
            "LT",
            "main",
            "EXIT UNCERTAIN: BUY 26 LT [EVWAP-EARLY-PY] timed out and could not be confirmed",
        ),
        _se(
            f"{day} 10:50:25",
            "ERROR",
            "-",
            "api_client",
            "Failed to fetch positionbook after 3 attempts",
        ),
        _se(
            f"{day} 11:00:00",
            "WARNING",
            "TCS",
            "tracker",
            "TCS: price 2487.20 unchanged for 125s while held; treating it as unconfirmed (x)",
        ),
        _se(
            f"{day} 11:05:00",
            "INFO",
            "TCS",
            "tracker",
            "TCS: price is moving again; resuming normal use",
        ),
        _se(
            f"{day} 10:50:07",
            "INFO",
            "-",
            "cycle",
            "pipeline cycle 10:50: evaluated 28, fires 0, submitted 0, skipped 12, late 12 in 64.09s",
        ),
    ]
    (d / f"signal_engine_analyze_{day}.log").write_text("".join(lines), encoding="utf-8")


def _events(root: Path):
    return eh.load_events(eh.Paths(root), DAY)[0]


def test_parse_each_metric(tmp_path: Path):
    _write_openalgo(tmp_path)
    _write_signal(tmp_path)
    m = eh.compute_metrics(_events(tmp_path))
    assert m["broker_rejections"] == 1
    assert m["rejection_detail"]["max_in_second"] == 11
    assert m["limiter_summaries"] == 2  # duplicate removed
    assert m["crit_max_wait"] == 1.5
    assert m["peak_calls_min"] == 135.0
    assert m["limiter_refused"] == 2
    assert m["limiter_broker_rejections"] == 1
    assert m["last_cap"] == "7/s,110/min"
    assert m["slow_orders_5s"] == 1 and m["slow_order_max_price_ms"] == 1850
    assert m["ws_drops"] == 1  # counts the websocket logger line only
    assert m["ws_max_outage_s"] == 90.0
    assert m["history_errors"] == 1
    assert m["dns_blips"] == 1  # the 10-28 line is ignored
    assert m["order_timeouts"] == 1
    assert m["exit_uncertain"] == 1
    assert m["positionbook_fail"] == 1
    assert m["stale_warnings"] == 1 and m["stale_recovered"] == 1
    assert m["late_cycles"] == 12


def test_thresholds_and_cap(tmp_path: Path):
    _write_openalgo(tmp_path, cw="6.0", cap="9/s,110/min")
    _write_signal(tmp_path)
    m = eh.compute_metrics(_events(tmp_path))
    rows = {r["key"]: r["status"] for r in eh.evaluate(m)}
    assert rows["crit_max_wait"] == "REGRESSION"
    assert rows["exit_uncertain"] == "REGRESSION"
    assert rows["order_timeouts"] == "WATCH"  # 1 is above 0 but not above 3
    assert rows["broker_rejections"] == "WATCH"
    assert rows["ack_p95_s"] == "N/A"
    cap = eh.cap_check(m)
    assert cap["status"] == "WATCH"
    assert eh.overall(eh.evaluate(m), cap) == "REGRESSION"


def test_clean_day_is_ok():
    m = eh.compute_metrics([])
    rows = eh.evaluate(m)
    # nothing observed: counts are zero (OK), limiter-derived values are N/A
    assert all(r["status"] in ("OK", "N/A") for r in rows)
    assert eh.cap_check(m)["status"] == "N/A"


def _make_db(path: Path, rows: list[dict]):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT, direction TEXT,"
        " symbol TEXT, entry REAL, sl REAL, tp REAL, quantity INTEGER, order_id TEXT, status TEXT,"
        " message TEXT, signal_time TEXT, received_at TEXT, executed_at TEXT, raw_message TEXT,"
        " context TEXT, fill_price REAL, sig_id TEXT, trade_mode TEXT, data_quality TEXT)"
    )
    for r in rows:
        cols = ",".join(r)
        conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(r))})", list(r.values())
        )
    conn.commit()
    conn.close()


def _trade_rows():
    base = {
        "strategy": "S",
        "status": "SUCCESS",
        "trade_mode": "analyze",
        "quantity": 10,
        "context": "{}",
        "data_quality": None,
    }
    out = []
    # NTPC: timeout near entry (09:50:20 vs entry ack 09:50:24), slow order flagged, long
    out.append(
        {
            **base,
            "direction": "LONG",
            "symbol": "NTPC",
            "entry": 100.0,
            "sl": 98.0,
            "tp": 0.0,
            "order_id": "1",
            "sig_id": "a",
            "received_at": "2026-10-29T09:50:10",
            "executed_at": "2026-10-29T09:50:24",
            "fill_price": 100.2,
        }
    )
    out.append(
        {
            **base,
            "direction": "",
            "symbol": "NTPC",
            "entry": 0.0,
            "sl": 0.0,
            "tp": 101.0,
            "order_id": "2",
            "sig_id": "a",
            "received_at": "2026-10-29T12:00:00",
            "executed_at": "2026-10-29T12:00:00",
            "fill_price": 101.0,
            "context": json.dumps({"reason": "TP1"}),
        }
    )
    # INFY: untouched
    out.append(
        {
            **base,
            "direction": "SHORT",
            "symbol": "INFY",
            "entry": 50.0,
            "sl": 51.0,
            "tp": 0.0,
            "order_id": "3",
            "sig_id": "b",
            "received_at": "2026-10-29T13:00:00",
            "executed_at": "2026-10-29T13:00:02",
            "fill_price": 50.0,
        }
    )
    out.append(
        {
            **base,
            "direction": "",
            "symbol": "INFY",
            "entry": 0.0,
            "sl": 0.0,
            "tp": 49.0,
            "order_id": "4",
            "sig_id": "b",
            "received_at": "2026-10-29T13:30:00",
            "executed_at": "2026-10-29T13:30:00",
            "fill_price": 49.0,
            "context": json.dumps({"reason": "TP1"}),
        }
    )
    return out


def test_trade_flagging_and_readonly(tmp_path: Path):
    _write_openalgo(tmp_path)
    _write_signal(tmp_path)
    db = tmp_path / "signal_engine" / "data" / "trades.db"
    db.parent.mkdir(parents=True)
    _make_db(db, _trade_rows())
    before = db.read_bytes()

    paths = eh.Paths(tmp_path)
    impact = eh.trade_impact(paths, DAY, _events(tmp_path))
    assert impact["available"]
    assert impact["n_total"] == 2
    assert impact["n_flagged"] == 1
    assert "entry_timeout" in impact["flag_counts"]
    assert "slow_place_order" not in impact["flag_counts"]  # 09:35 is outside the window
    assert impact["flagged"]["n_trades"] == 1 and impact["untouched"]["n_trades"] == 1
    assert impact["untouched"]["total_pnl"] == 10 * (50.0 - 49.0)
    assert impact["latency"]["derivable"] and impact["latency"]["n"] == 2
    assert db.read_bytes() == before  # read-only


def test_flag_positions_stale_and_positionbook(tmp_path: Path):
    _write_openalgo(tmp_path)
    _write_signal(tmp_path)
    db = tmp_path / "signal_engine" / "data" / "trades.db"
    db.parent.mkdir(parents=True)
    rows = _trade_rows()
    # TCS held 10:30-11:30 covers the stale episode and the positionbook failure
    rows.append(
        {
            "strategy": "S",
            "status": "SUCCESS",
            "trade_mode": "analyze",
            "quantity": 5,
            "context": "{}",
            "direction": "LONG",
            "symbol": "TCS",
            "entry": 10.0,
            "sl": 9.0,
            "tp": 0.0,
            "order_id": "5",
            "sig_id": "c",
            "received_at": "2026-10-29T10:30:00",
            "executed_at": "2026-10-29T10:30:01",
            "fill_price": 10.0,
        }
    )
    rows.append(
        {
            "strategy": "S",
            "status": "SUCCESS",
            "trade_mode": "analyze",
            "quantity": 5,
            "context": json.dumps({"reason": "SL"}),
            "direction": "",
            "symbol": "TCS",
            "entry": 0.0,
            "sl": 0.0,
            "tp": 9.0,
            "order_id": "6",
            "sig_id": "c",
            "received_at": "2026-10-29T11:30:00",
            "executed_at": "2026-10-29T11:30:00",
            "fill_price": 9.0,
        }
    )
    _make_db(db, rows)
    impact = eh.trade_impact(eh.Paths(tmp_path), DAY, _events(tmp_path))
    fc = impact["flag_counts"]
    assert fc.get("stale_price_while_held") == 1
    assert fc.get("positionbook_fail_while_held") == 2  # NTPC and TCS both held at 10:50
    assert impact["n_total"] == 3


def test_empty_day_and_missing_db(tmp_path: Path):
    rep = eh.build_report(eh.Paths(tmp_path), date(2026, 10, 31))
    assert rep["empty"]
    assert "No execution data" in eh.render_md(rep)
    assert eh.main(["--root", str(tmp_path), "--day", "2026-10-31"]) == 0


def test_logs_without_db_still_reports(tmp_path: Path, capsys):
    _write_openalgo(tmp_path)
    _write_signal(tmp_path)
    assert eh.main(["--root", str(tmp_path), "--day", "2026-10-29"]) == 0
    out = capsys.readouterr().out
    assert "Overall: **REGRESSION**" in out
    assert "Not available" in out
    assert eh.main(["--root", str(tmp_path), "--day", "2026-10-29", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["day"] == "2026-10-29"


def test_previous_day_and_errors_jsonl(tmp_path: Path):
    _write_openalgo(tmp_path, day="2026-10-28")
    _write_openalgo(tmp_path)
    _write_signal(tmp_path)
    (tmp_path / "log" / "errors.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-10-29 10:00:00",
                "logger": "flattrade_websocket",
                "message": "WebSocket error: Connection to remote host was lost.",
            }
        )
        + "\n"
        + json.dumps({"ts": "2026-10-28 10:00:00", "logger": "x", "message": "m"})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "signal_engine" / "logs" / "errors_2026-10-29.jsonl").write_text(
        json.dumps({"time": "2026-10-29T04:30:00+00:00", "logger": "y", "message": "m"}) + "\n",
        encoding="utf-8",
    )
    rep = eh.build_report(eh.Paths(tmp_path), DAY)
    assert rep["previous_day"] == "2026-10-28"
    assert rep["errors_jsonl"]["total"] == 2  # 04:30 UTC normalised to 10:00 IST same day
    assert rep["errors_jsonl"]["ws_drops"] == 1


def test_outage_ignores_after_close():
    d = datetime(2026, 10, 29, 16, 0, 0, tzinfo=IST)
    f = datetime(2026, 10, 30, 9, 0, 0, tzinfo=IST)
    m = eh.compute_metrics([eh.Event(d, "ws_drop"), eh.Event(f, "ws_flow")])
    assert m["ws_outages"] == []
