"""POST /api/strategy-pnl/evaluation/prepare and /validate: session, CSRF, validation
parity with the GET routes, caps, mode from the server, the package contents, and
proof that no model, network or database write is reached.

Run with: PYTHONPATH=. uv run pytest test/test_performance_eval_route.py -v
"""

import copy
import json
import re
import sys
from datetime import date, timedelta

import pytest
from flask import Flask
from flask_wtf.csrf import CSRFProtect

import blueprints.auth as auth_bp_module
import blueprints.strategy_pnl as route
import services.strategy_daily_performance_service as svc
from limiter import limiter

PREPARE = "/api/strategy-pnl/evaluation/prepare"
VALIDATE = "/api/strategy-pnl/evaluation/validate"
TODAY = date.today()
CODE_WORDS = re.compile(r"\b(HTTP|status code|Traceback|Exception|Error)\b|\b[45]\d\d\b")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(route, "schedule_reconcile", lambda mode, creds: False)


def _app(csrf=False):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(route.strategy_pnl_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    if csrf:
        app.config["WTF_CSRF_ENABLED"] = True
        CSRFProtect(app)
    return app


def _client(monkeypatch, app=None, logged_in=True):
    import utils.session as session_utils

    client = (app or _app()).test_client()
    if logged_in:
        with client.session_transaction() as session:
            session["user"] = "rajandran"
            session["logged_in"] = True
            session["login_time"] = "2026-01-01T09:00:00+05:30"
            session["broker"] = "flattrade"
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    return client


class _Boom:
    def __call__(self, *_a, **_k):
        raise AssertionError("this must not be reached")


def _trade(strategy, i, pnl, mode="analyze"):
    day = TODAY - timedelta(days=i % 20)
    return {
        "strategy": strategy,
        "symbol": f"SYM{i % 4}",
        "exchange": "NSE",
        "product": "MIS",
        "mode": mode,
        "direction": "LONG",
        "closed_quantity": 10.0,
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl / 10,
        "realized_pnl": pnl,
        "trade_date": day.isoformat(),
        "opened_at": f"{day.isoformat()}T{9 + i % 3:02d}:00:00",
        "closed_at": f"{day.isoformat()}T{9 + i % 3:02d}:40:00",
    }


def _install_ledger(monkeypatch, tmp_path, trades, current="analyze"):
    monkeypatch.setattr(
        svc,
        "get_closed_trades",
        lambda mode=None, **kw: [copy.deepcopy(t) for t in trades if t["mode"] == mode],
    )
    monkeypatch.setattr(svc, "_current_mode", lambda: current)
    monkeypatch.setattr(
        "services.strategy_trade_enrichment.SIGNAL_TRADES_DB", tmp_path / "absent.db"
    )
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    trades = [_trade("ALPHA", i, 40.0 if i % 3 else -35.0) for i in range(45)]
    trades += [_trade("BETA", i, 10.0 if i % 2 else -10.0) for i in range(12)]
    _install_ledger(monkeypatch, tmp_path, trades)
    return trades


def _prepare(client, **body):
    return client.post(PREPARE, json={"period": "90d", **body})


# -- session, method, CSRF -----------------------------------------------------------


@pytest.mark.parametrize("url", [PREPARE, VALIDATE])
def test_a_session_is_required(monkeypatch, url):
    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch, logged_in=False)
    assert client.post(url, json={}).status_code in (302, 401)


@pytest.mark.parametrize("url", [PREPARE, VALIDATE])
def test_only_post_is_allowed(monkeypatch, url):
    client = _client(monkeypatch)
    assert client.get(url).status_code == 405
    assert client.put(url, json={}).status_code == 405


@pytest.mark.parametrize("url", [PREPARE, VALIDATE])
def test_csrf_is_enforced_like_every_other_post(monkeypatch, url):
    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch, app=_app(csrf=True))
    assert client.post(url, json={"period": "7d"}).status_code == 400


def test_the_old_evaluate_route_is_gone(monkeypatch):
    client = _client(monkeypatch)
    assert client.post("/api/strategy-pnl/evaluate", json={}).status_code == 404


# -- validation parity with the GET routes ---------------------------------------------


@pytest.mark.parametrize("url", [PREPARE, VALIDATE])
@pytest.mark.parametrize(
    "raw", ["0", "-5", "abc", "nan", "inf", "-inf", "1e400", "1000000000001", "1e13"]
)
def test_invalid_capital_matches_the_get_route(monkeypatch, url, raw):
    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch)
    get = client.get(f"/api/strategy-pnl/daily?capital={raw}")
    post = client.post(url, json={"capital": raw, "response": "x"})
    assert get.status_code == post.status_code == 400
    assert get.get_json()["message"] == post.get_json()["message"]


@pytest.mark.parametrize("value", [0, -5, True, [1], {"a": 1}])
def test_invalid_json_capital_types_are_refused(monkeypatch, value):
    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch)
    response = client.post(PREPARE, json={"capital": value})
    assert response.status_code == 400
    assert "capital" in response.get_json()["message"].lower()


def test_capital_at_the_ceiling_is_accepted(monkeypatch):
    seen = []

    def spy(include, exclude, strategy, period, start, end, capital):
        seen.append(capital)
        return False, {"status": "error", "message": "stop here"}, 400

    monkeypatch.setattr(route, "build_evaluation_dataset", spy)
    client = _client(monkeypatch)
    client.post(PREPARE, json={"capital": route.MAX_CAPITAL})
    client.post(PREPARE, json={"capital": " 250000.5 "})
    client.post(PREPARE, json={"capital": ""})
    assert seen == [route.MAX_CAPITAL, 250000.5, None]


def test_filter_limits_match_the_get_route(monkeypatch):
    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch)
    many = [f"S{i}" for i in range(101)]
    long_name = "x" * 65
    query = "&".join(f"strategy={n}" for n in many)
    get_many = client.get(f"/api/strategy-pnl/compare?{query}")
    post_many = client.post(PREPARE, json={"strategy": many})
    assert get_many.status_code == post_many.status_code == 400
    assert get_many.get_json()["message"] == post_many.get_json()["message"]
    get_long = client.get(f"/api/strategy-pnl/compare?exclude={long_name}")
    post_long = client.post(PREPARE, json={"exclude": [long_name]})
    assert get_long.get_json()["message"] == post_long.get_json()["message"]
    assert client.post(PREPARE, json={"strategy": [1, 2]}).status_code == 400
    assert client.post(PREPARE, json={"exclude": "ok", "strategy": {"a": 1}}).status_code == 400


def test_names_are_cleaned_like_the_query_string(monkeypatch):
    seen = {}

    def spy(include, exclude, strategy, period, start, end, capital):
        seen.update(include=include, exclude=exclude, strategy=strategy, period=period)
        return False, {"status": "error", "message": "stop"}, 400

    monkeypatch.setattr(route, "build_evaluation_dataset", spy)
    client = _client(monkeypatch)
    client.post(
        PREPARE, json={"strategy": [" ORB ", "", "ORB", "VWAP"], "exclude": " OLD ", "period": "7d"}
    )
    assert seen == {"include": ["VWAP"], "exclude": ["OLD"], "strategy": "ORB", "period": "7d"}


@pytest.mark.parametrize("url", [PREPARE, VALIDATE])
def test_body_must_be_a_json_object(monkeypatch, url):
    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch)
    assert client.post(url, data="not json", content_type="text/plain").status_code == 400
    assert client.post(url, json=[1, 2]).status_code == 400
    assert client.post(url, json={"period": 7}).status_code == 400
    assert client.post(url, json={"start": 5}).status_code == 400


# -- size caps ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [PREPARE, VALIDATE])
def test_oversized_body_is_refused_with_a_sentence(monkeypatch, url):
    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch)
    response = client.post(url, json={"response": "x" * (route.MAX_EVALUATE_BODY_BYTES + 10)})
    assert response.status_code == 413
    assert response.get_json()["message"] == route._TOO_LARGE_MESSAGE


def test_the_body_cap_is_130000_bytes():
    assert route.MAX_EVALUATE_BODY_BYTES == 130_000


def test_a_body_over_the_per_request_limit_is_refused_like_the_header_check(monkeypatch):
    from werkzeug.exceptions import RequestEntityTooLarge

    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())

    class Spy:
        def __getattr__(self, name):
            return getattr(real, name)

        def __setattr__(self, name, value):
            setattr(real, name, value)

        def get_json(self, **_kw):
            raise RequestEntityTooLarge()

    real = route.request
    monkeypatch.setattr(route, "request", Spy())
    client = _client(monkeypatch)
    for url in (PREPARE, VALIDATE):
        response = client.post(url, json={})
        assert response.status_code == 413
        assert response.get_json()["message"] == route._TOO_LARGE_MESSAGE


def test_too_large_message_is_a_trader_sentence():
    assert route._TOO_LARGE_MESSAGE == (
        "That request is too large. Paste a shorter analysis and try again."
    )


def test_the_real_per_request_limit_stops_reading_a_body_without_content_length(monkeypatch):
    import io

    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch)
    big = b'{"response": "' + b"x" * (route.MAX_EVALUATE_BODY_BYTES + 10) + b'"}'
    for url in (PREPARE, VALIDATE):
        response = client.post(
            url,
            input_stream=io.BytesIO(big),
            content_type="application/json",
            environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
        )
        assert not response.request.environ.get("CONTENT_LENGTH")
        # Werkzeug cuts the read at the limit, so the cut-off JSON is unreadable.
        assert response.status_code == 400
        assert response.get_json()["message"] == route._UNREADABLE_MESSAGE


def test_crafted_nesting_never_reaches_the_generic_error_handler(monkeypatch):
    monkeypatch.setattr(route, "build_evaluation_dataset", _Boom())
    client = _client(monkeypatch)
    deep_array = "[" * 100_000
    deep_dict = "{" + '"a":{' * 20_000
    for url in (PREPARE, VALIDATE):
        for raw in (deep_array, deep_dict):
            response = client.post(url, data=raw, content_type="application/json")
            assert response.status_code == 400, (url, raw[:10])
            assert response.get_json()["message"] == route._UNREADABLE_MESSAGE


def test_deeply_nested_response_object_is_a_400_sentence(monkeypatch, ledger):
    client = _client(monkeypatch)
    # Built by hand: the test client's own json.dumps would recurse as well.
    depth = 450
    text = '{"response": ' + '{"a":' * depth + "1" + "}" * depth + "}"
    response = client.post(VALIDATE, data=text, content_type="application/json")
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"
    assert response.get_json()["message"]


def test_deeply_nested_response_string_is_a_400_sentence(monkeypatch, ledger):
    client = _client(monkeypatch)
    response = client.post(VALIDATE, json={"response": "```json\n" + "[" * 50_000 + "\n```"})
    assert response.status_code == 400
    assert response.get_json()["message"] == (
        "The pasted text has no JSON block. Ask Claude to repeat the answer and end it "
        "with the JSON block described in the prompt, then paste it again."
    )


# -- prepare -----------------------------------------------------------------------------


def test_empty_selection_is_a_trader_message(monkeypatch, tmp_path):
    _install_ledger(monkeypatch, tmp_path, [])
    client = _client(monkeypatch)
    for response in (_prepare(client), client.post(VALIDATE, json={"response": "{}"})):
        assert response.status_code == 400
        assert (
            response.get_json()["message"]
            == "There are no closed trades in this selection to evaluate."
        )


def test_prepare_returns_the_whole_package(monkeypatch, ledger):
    client = _client(monkeypatch)
    response = _prepare(client, capital=500000)
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success" and body["schema_version"] == 1
    package = body["package"]
    assert set(package) == {"meta", "dataset", "report", "prompt", "export"}
    meta = package["meta"]
    assert meta["mode_display"] == "Sandbox"
    assert meta["strategies_included"] == ["ALPHA", "BETA"]
    assert meta["capital"] == 500000
    assert meta["period"] == "90d"
    assert re.fullmatch(r"[0-9a-f]{64}", meta["package_hash"])
    assert meta["generated_at"] and meta["truncated"] == []
    assert package["dataset"]["pooled"]["trades_count"] == 57
    assert "mode" not in package["dataset"]["meta"]
    assert package["report"]["scope"]["n_trades"] == 57
    assert [s["id"] for s in package["report"]["sections"]][0] == "profitability"
    prompt = package["prompt"]
    assert "<performance_data>" not in prompt["text"] and "ALPHA" not in prompt["text"]
    assert "<performance_data>" in prompt["text_with_data"]
    assert meta["package_hash"] in prompt["text"]
    export = package["export"]
    assert export["data_filename"] == (
        f"openalgo-evaluation-{meta['start_date']}_{meta['end_date']}.json"
    )
    assert json.loads(export["data_json"]) == package["dataset"]
    assert "Sandbox" in export["notice"] and export["notice"].endswith(".")
    assert export["data_json"].count("\n") > 10


def test_the_package_hash_is_stable_across_requests(monkeypatch, ledger):
    client = _client(monkeypatch)
    first = _prepare(client).get_json()["package"]
    second = _prepare(client).get_json()["package"]
    assert first["meta"]["package_hash"] == second["meta"]["package_hash"]
    assert first["dataset"] == second["dataset"]
    assert first["report"] == second["report"]


def test_prompt_with_data_is_none_over_the_paste_limit(monkeypatch, ledger):
    monkeypatch.setattr(route, "PROMPT_PASTE_MAX_CHARS", 100)
    client = _client(monkeypatch)
    package = _prepare(client).get_json()["package"]
    assert package["prompt"]["text_with_data"] is None
    assert package["prompt"]["text"]


def test_excluded_strategies_are_absent_from_the_whole_package(monkeypatch, tmp_path):
    trades = [_trade("KEEP", i, 40.0 if i % 3 else -35.0) for i in range(45)]
    trades += [_trade("HIDDEN-EXCLUDED-XYZ", i, -20.0) for i in range(45)]
    _install_ledger(monkeypatch, tmp_path, trades)
    client = _client(monkeypatch)
    response = _prepare(client, exclude=["HIDDEN-EXCLUDED-XYZ"])
    assert response.status_code == 200
    assert "HIDDEN-EXCLUDED-XYZ" not in response.get_data(as_text=True)


def test_mode_comes_from_the_server_never_the_body(monkeypatch, tmp_path):
    trades = [_trade("ALPHA", i, 10.0, mode="live") for i in range(4)]
    trades += [_trade("ALPHA", i, 10.0, mode="analyze") for i in range(9)]
    _install_ledger(monkeypatch, tmp_path, trades)
    client = _client(monkeypatch)
    response = _prepare(client, mode="live", analyze=False)
    package = response.get_json()["package"]
    assert package["meta"]["mode_display"] == "Sandbox"
    assert package["dataset"]["pooled"]["trades_count"] == 9
    assert "analyze" not in json.dumps(package)


def test_prepare_failure_is_a_plain_sentence(monkeypatch):
    def broken(*_a, **_k):
        raise ValueError("secret internals")

    monkeypatch.setattr(route, "build_evaluation_dataset", broken)
    client = _client(monkeypatch)
    response = _prepare(client)
    assert response.status_code == 500
    message = response.get_json()["message"]
    assert message == "The evaluation could not be prepared. Try again in a moment."
    assert "secret internals" not in response.get_data(as_text=True)


# -- validate ------------------------------------------------------------------------------


def _answer(package, **over):
    meta = package["meta"]
    value = {
        "schema_version": 1,
        "package_hash": meta["package_hash"],
        "summary": "Short summary.",
        "overall_confidence": "low",
        "strategies": [
            {
                "name": name,
                "assessment": package["report"]["scope"]["labels"]["per_strategy"][name],
                "n": n,
                "confidence": "low",
                "observations": [],
                "weaknesses": [],
                "suggested_tests": [],
            }
            for name, n in (("ALPHA", 45), ("BETA", 12))
        ],
        "portfolio": None,
        "limitations": ["Small sample."],
    }
    value.update(over)
    return value


def test_validate_accepts_a_matching_answer_text_or_object(monkeypatch, ledger):
    client = _client(monkeypatch)
    package = _prepare(client).get_json()["package"]
    answer = _answer(package)
    for pasted in (answer, "Here you go\n```json\n" + json.dumps(answer) + "\n```"):
        response = client.post(VALIDATE, json={"period": "90d", "response": pasted})
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "success"
        assert body["checks"] == {"package_match": True, "label_conflicts": [], "warnings": []}
        assert [s["name"] for s in body["evaluation"]["strategies"]] == ["ALPHA", "BETA"]
        assert body["evaluation"]["strategies"][0]["deterministic_label"]


def test_validate_warns_on_a_hash_mismatch_and_a_label_conflict(monkeypatch, ledger):
    client = _client(monkeypatch)
    package = _prepare(client).get_json()["package"]
    answer = _answer(package, package_hash="0" * 64)
    answer["strategies"][1]["assessment"] = "possible edge (screening only)"
    response = client.post(VALIDATE, json={"period": "90d", "response": answer})
    assert response.status_code == 200
    checks = response.get_json()["checks"]
    assert checks["package_match"] is False
    assert checks["warnings"][0].startswith("This analysis was written for different data")
    assert checks["label_conflicts"][0]["strategy"] == "BETA"


def test_validate_refuses_a_strategy_outside_the_selection(monkeypatch, ledger):
    client = _client(monkeypatch)
    package = _prepare(client).get_json()["package"]
    answer = _answer(package)
    response = client.post(
        VALIDATE, json={"period": "90d", "strategy": ["ALPHA"], "response": answer}
    )
    body = response.get_json()
    assert response.status_code == 400 and body["status"] == "error"
    assert any(p["path"] == "strategies[1].name" for p in body["problems"])
    assert len(body["problems"]) <= 20
    assert not CODE_WORDS.search(body["message"])


def test_validate_refuses_bad_text_with_one_sentence(monkeypatch, ledger):
    client = _client(monkeypatch)
    for pasted, needle in (("no json at all", "no JSON block"), ("x" * 60_001, "too long")):
        response = client.post(VALIDATE, json={"period": "90d", "response": pasted})
        body = response.get_json()
        assert response.status_code == 400
        assert needle in body["message"]
        assert body["problems"][0]["path"] == "response"


@pytest.mark.parametrize("pasted", [None, "", "   ", 5, [1], True])
def test_validate_needs_a_pasted_response(monkeypatch, ledger, pasted):
    client = _client(monkeypatch)
    response = client.post(VALIDATE, json={"period": "90d", "response": pasted})
    assert response.status_code == 400
    assert "Paste the analysis" in response.get_json()["message"]


def test_validate_rebuilds_the_package_from_the_selection(monkeypatch, ledger):
    client = _client(monkeypatch)
    package = _prepare(client).get_json()["package"]
    answer = _answer(package)
    # A different selection gives different data: the hash no longer matches.
    response = client.post(
        VALIDATE,
        json={
            "period": "90d",
            "strategy": ["ALPHA"],
            "exclude": ["BETA"],
            "response": {**answer, "strategies": answer["strategies"][:1]},
        },
    )
    assert response.status_code == 200
    assert response.get_json()["checks"]["package_match"] is False


# -- nothing is stored, nothing is called -----------------------------------------------------


def _forbid_everything(monkeypatch):
    import socket

    import httpx

    from database import agent_db

    for name in ("add_message", "create_conversation", "update_conversation", "mark_secret_used"):
        monkeypatch.setattr(agent_db, name, _Boom(), raising=False)
    monkeypatch.setattr(socket.socket, "connect", _Boom())
    monkeypatch.setattr(socket, "create_connection", _Boom())
    monkeypatch.setattr(httpx.Client, "send", _Boom())
    monkeypatch.setattr(httpx.AsyncClient, "send", _Boom())
    # Importing litellm itself reaches the network, so only patch it when
    # something else has loaded it. The package never imports it (see the prompt test).
    litellm = sys.modules.get("litellm")
    if litellm is not None:
        monkeypatch.setattr(litellm, "completion", _Boom())
        monkeypatch.setattr(litellm, "acompletion", _Boom())
    import sqlalchemy.orm

    monkeypatch.setattr(sqlalchemy.orm.Session, "commit", _Boom())
    monkeypatch.setattr(sqlalchemy.orm.Session, "flush", _Boom())


def test_no_model_network_or_database_write_is_reached(monkeypatch, ledger):
    client = _client(monkeypatch)
    package = _prepare(client).get_json()["package"]
    _forbid_everything(monkeypatch)
    prepared = _prepare(client)
    assert prepared.status_code == 200
    checked = client.post(VALIDATE, json={"period": "90d", "response": _answer(package)})
    assert checked.status_code == 200
    refused = client.post(VALIDATE, json={"period": "90d", "response": "nothing"})
    assert refused.status_code == 400


def test_error_messages_carry_no_status_codes_or_exception_names(monkeypatch, ledger):
    client = _client(monkeypatch)
    responses = [
        client.post(PREPARE, json={"capital": "abc"}),
        client.post(PREPARE, json={"strategy": [1]}),
        client.post(PREPARE, json={"period": 5}),
        client.post(PREPARE, data="nope", content_type="text/plain"),
        client.post(VALIDATE, json={"period": "90d", "response": "nope"}),
        client.post(VALIDATE, json={"period": "90d"}),
        client.post(PREPARE, json={"response": "x" * 200_000}),
    ]
    for response in responses:
        message = response.get_json()["message"]
        assert message and not CODE_WORDS.search(message), message
