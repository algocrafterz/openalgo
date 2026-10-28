"""Who paces broker history calls: the service's 3-per-second spacer, or the broker itself.

The service books one history call every 0.35 s for every broker, ahead of the broker's own
limiter. A broker whose data module sets ``PACES_OWN_HISTORY = True`` sends every history call
through its own global per-second and per-minute limiter, so the spacer adds only delay: forty
symbols at a bar close took 13 s in the spacer alone. Brokers without the flag keep it.
"""

from __future__ import annotations

import glob
import inspect
import re
import threading
import time
from types import SimpleNamespace

import pytest

from services import history_service

OK = (True, {"status": "success", "data": [{"close": 1.0}]}, 200)


@pytest.fixture
def setup(monkeypatch):
    state = {"spacer": [], "paces": True, "keys": {"good": ("tok", None, "somebroker")}}

    monkeypatch.setattr(
        history_service,
        "_enforce_rate_limit",
        lambda **kw: state["spacer"].append(kw.get("background", False)),
    )
    monkeypatch.setattr(
        history_service,
        "import_broker_module",
        lambda name: SimpleNamespace(PACES_OWN_HISTORY=state["paces"]),
    )
    monkeypatch.setattr(
        history_service,
        "get_auth_token_broker",
        lambda key, include_feed_token=False: state["keys"].get(key, (None, None, None)),
    )
    monkeypatch.setattr(history_service, "get_history_with_auth", lambda *a, **k: OK)
    return state


def _get(symbol="TCS", api_key="good", **kw):
    return history_service.get_history(symbol, "NSE", "5m", "d", "d", api_key=api_key, **kw)


def test_a_broker_that_paces_its_own_history_skips_the_service_spacer(setup):
    setup["paces"] = True
    assert _get() == OK
    assert setup["spacer"] == []


def test_a_broker_without_the_flag_keeps_the_spacer(setup):
    setup["paces"] = False
    assert _get() == OK
    assert setup["spacer"] == [False]


def test_a_module_without_the_attribute_keeps_the_spacer(setup, monkeypatch):
    monkeypatch.setattr(history_service, "import_broker_module", lambda name: SimpleNamespace())
    assert _get() == OK
    assert setup["spacer"] == [False]


def test_a_background_download_keeps_its_background_spacer_on_other_brokers(setup):
    setup["paces"] = False
    _get(background=True)
    assert setup["spacer"] == [True]


def test_an_unknown_broker_module_keeps_the_spacer(setup, monkeypatch):
    monkeypatch.setattr(history_service, "import_broker_module", lambda name: None)
    _get()
    assert setup["spacer"] == [False]


def test_an_invalid_key_is_refused_without_taking_a_spacer_slot(setup):
    setup["paces"] = False
    ok, body, status = _get(api_key="nope")
    assert (ok, status) == (False, 403)
    assert setup["spacer"] == []


def test_internal_callers_with_a_token_are_paced_the_same_way(setup):
    setup["paces"] = True
    history_service.get_history("TCS", "NSE", "5m", "d", "d", auth_token="t", broker="somebroker")
    assert setup["spacer"] == []
    setup["paces"] = False
    history_service.get_history("INFY", "NSE", "5m", "d", "d", auth_token="t", broker="somebroker")
    assert setup["spacer"] == [False]


def test_forty_concurrent_history_requests_are_not_serialised_by_the_service(setup):
    """With the spacer in force these 40 calls take 14 s; without it the service adds nothing."""
    setup["paces"] = True
    started = time.perf_counter()
    threads = [threading.Thread(target=_get, kwargs={"symbol": f"S{i}"}) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert time.perf_counter() - started < 1.0
    assert setup["spacer"] == []


def test_the_real_spacer_still_spaces_calls_a_third_of_a_second_apart(monkeypatch):
    slept = []
    clock = {"now": 100.0}
    monkeypatch.setattr(history_service.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(history_service.time, "sleep", lambda s: slept.append(round(s, 2)))
    monkeypatch.setattr(history_service, "_next_history_slot", 0.0)
    for _ in range(4):
        history_service._enforce_rate_limit()
    assert slept == [0.35, 0.7, 1.05]  # first call at once, then 0.35 s apart


def test_only_flattrade_declares_that_it_paces_its_own_history():
    declared = []
    for path in glob.glob("broker/*/api/data.py"):
        with open(path, encoding="utf-8") as fh:
            if re.search(r"^PACES_OWN_HISTORY\s*=\s*True", fh.read(), re.MULTILINE):
                declared.append(path.split("/")[1])
    assert declared == ["flattrade"]


def test_flattrade_history_goes_through_the_global_data_limiter():
    """The reason the flag is true: every history request acquires DATA_LIMITER first."""
    from broker.flattrade.api import data

    assert data.PACES_OWN_HISTORY is True
    assert "_apply_rate_limit()" in inspect.getsource(data.get_api_response)
    history_source = inspect.getsource(data.BrokerData.get_history)
    assert history_source.count("get_api_response(") == 2  # EOD and intraday, nothing else
    assert "httpx" not in history_source and "requests." not in history_source
