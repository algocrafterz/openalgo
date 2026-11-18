"""GOLDEN: /api/strategy-pnl/* responses must not change when the Discretionary blueprint exists.

The service layer is stubbed with fixed trades and the strategy-pnl routes are exercised through
the Flask test client. The canonical JSON of /daily and /compare was recorded on the untouched
blueprint BEFORE blueprints/discretionary_pnl.py was added (golden file
test/golden/strategy_pnl_daily_golden.json) and must stay byte-identical with the Discretionary
flags off, on, and with an ignored ?source=discretionary query parameter.

Run with: uv run pytest test/test_strategy_pnl_daily_golden.py -q
Record (only on the untouched blueprint): GOLDEN_RECORD=1 pytest test/test_strategy_pnl_daily_golden.py
"""

import importlib
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blueprints.auth as auth_bp_module  # noqa: E402
import blueprints.strategy_pnl as strategy_pnl_module  # noqa: E402
import services.strategy_daily_performance_service as svc  # noqa: E402
from limiter import limiter  # noqa: E402
from utils.single_flight import SingleFlight  # noqa: E402

GOLDEN_FILE = Path(__file__).parent / "golden" / "strategy_pnl_daily_golden.json"

URLS = {
    "daily_default": "/api/strategy-pnl/daily?period=all",
    "daily_strategy": "/api/strategy-pnl/daily?period=all&strategy=ORB",
    "daily_exclude": "/api/strategy-pnl/daily?period=all&exclude=VWAP",
    "daily_capital": "/api/strategy-pnl/daily?period=all&capital=500000",
    "daily_one_day": "/api/strategy-pnl/daily?period=1d",
    "daily_bad_period": "/api/strategy-pnl/daily?period=nope",
    "compare": "/api/strategy-pnl/compare?period=all",
}


def _t(strategy, symbol, pnl, day, hour):
    return {
        "strategy": strategy,
        "symbol": symbol,
        "exchange": "NSE",
        "product": "MIS",
        "mode": "live",
        "direction": "LONG",
        "closed_quantity": 10.0,
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl / 10,
        "realized_pnl": pnl,
        "trade_date": day,
        "opened_at": f"{day}T{hour - 1:02d}:00:00",
        "closed_at": f"{day}T{hour:02d}:00:00",
    }


TRADES = [
    _t("ORB", "AAA", 120.0, "2026-10-26", 10),
    _t("ORB", "AAA", -40.0, "2026-10-27", 11),
    _t("VWAP", "BBB", 75.5, "2026-10-27", 12),
    _t("VWAP", "CCC", -90.25, "2026-10-28", 13),
    _t("EMA", "AAA", 33.0, "2026-10-29", 10),
    _t("ORB", "DDD", 210.0, "2026-10-30", 14),
]


@pytest.fixture(autouse=True)
def _stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: [dict(t) for t in TRADES])
    monkeypatch.setattr(svc, "_current_mode", lambda: "live")
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))
    monkeypatch.setattr(
        "services.strategy_trade_enrichment.SIGNAL_TRADES_DB", tmp_path / "absent.db"
    )
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    monkeypatch.setattr(strategy_pnl_module, "schedule_reconcile", lambda mode, creds: False)
    monkeypatch.setattr(strategy_pnl_module, "get_auth_token", lambda _u: "tok123")
    monkeypatch.setattr(strategy_pnl_module, "get_api_key_for_tradingview", lambda _u: "key123")
    monkeypatch.setattr(strategy_pnl_module, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(
        strategy_pnl_module, "get_data_health", lambda mode: {"status": "ok", "mode": mode}
    )
    # a fresh cache per test: no result is shared between tests
    monkeypatch.setattr(
        strategy_pnl_module, "_pnl_flights", SingleFlight(join_window=5.0, wait_timeout=60.0)
    )
    for name in ("DISCRETIONARY_PERFORMANCE_UI_ENABLED", "DISCRETIONARY_LEDGER_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(strategy_pnl_module.strategy_pnl_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    try:  # present once Stage 3a exists: it must not change the routes above
        module = importlib.import_module("blueprints.discretionary_pnl")
    except ImportError:
        module = None
    if module is not None:
        app.register_blueprint(module.discretionary_pnl_bp)
    return app


def _collect(monkeypatch, suffix=""):
    import utils.session as session_utils

    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    out = {}
    app = _app()
    with app.test_client() as client:
        with client.session_transaction() as s:
            s["user"] = "rajandran"
            s["logged_in"] = True
            s["login_time"] = "2026-01-01T09:00:00+05:30"
            s["broker"] = "flattrade"
        for name, url in URLS.items():
            # a new cache for every URL so each one is computed, never replayed
            monkeypatch.setattr(
                strategy_pnl_module,
                "_pnl_flights",
                SingleFlight(join_window=5.0, wait_timeout=60.0),
            )
            sep = "&" if "?" in url else "?"
            response = client.get(url + (f"{sep}{suffix}" if suffix else ""))
            out[name] = {"status": response.status_code, "body": response.get_json()}
    return json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _golden():
    if not GOLDEN_FILE.exists():
        if os.environ.get("GOLDEN_RECORD") == "1":
            return None
        pytest.fail(f"golden file missing: {GOLDEN_FILE}")
    return GOLDEN_FILE.read_text(encoding="utf-8")


def test_record_or_match_baseline(monkeypatch):
    actual = _collect(monkeypatch)
    golden = _golden()
    if golden is None:
        GOLDEN_FILE.parent.mkdir(exist_ok=True)
        GOLDEN_FILE.write_text(actual, encoding="utf-8", newline="")
        pytest.skip("golden recorded")
    assert actual == golden


def test_sanity_of_the_golden_itself():
    golden = json.loads(_golden() or "{}") if GOLDEN_FILE.exists() else {}
    if not golden:
        pytest.skip("no golden yet")
    assert golden["daily_default"]["status"] == 200
    assert golden["daily_default"]["body"]["trades_count"] == 6
    assert golden["daily_default"]["body"]["net_profit"] == pytest.approx(308.25)
    assert golden["daily_bad_period"]["status"] == 400
    assert golden["compare"]["status"] == 200


@pytest.mark.parametrize(
    "flags",
    [
        {"DISCRETIONARY_PERFORMANCE_UI_ENABLED": "false", "DISCRETIONARY_LEDGER_ENABLED": "false"},
        {"DISCRETIONARY_PERFORMANCE_UI_ENABLED": "true", "DISCRETIONARY_LEDGER_ENABLED": "true"},
        {"DISCRETIONARY_PERFORMANCE_UI_ENABLED": "true", "DISCRETIONARY_LEDGER_ENABLED": "false"},
    ],
)
def test_byte_identical_with_flags(monkeypatch, flags):
    golden = _golden()
    if golden is None:
        pytest.skip("golden not recorded yet")
    for k, v in flags.items():
        monkeypatch.setenv(k, v)
    assert _collect(monkeypatch) == golden


def test_byte_identical_with_ignored_source_param(monkeypatch):
    golden = _golden()
    if golden is None:
        pytest.skip("golden not recorded yet")
    monkeypatch.setenv("DISCRETIONARY_PERFORMANCE_UI_ENABLED", "true")
    monkeypatch.setenv("DISCRETIONARY_LEDGER_ENABLED", "true")
    assert _collect(monkeypatch, "source=discretionary") == golden
