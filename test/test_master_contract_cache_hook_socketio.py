"""BUG-015: the master contract cache hook with and without a bound Socket.IO server.

2026-11-04: the scheduler's startup CLI imports extensions.socketio but never binds it, so
socketio.server is None and emit() raised AttributeError after the cache had loaded. The error
handler emitted again, the second failure escaped, and two false errors were logged on every
automatic login.
"""

import sys
import types

import pytest

from database import master_contract_cache_hook as hook


class _Server:
    pass


@pytest.fixture
def fakes(monkeypatch):
    """A cache that loads, a strategy module that records restores, and a recording emit."""
    stats = {"total_symbols": 5, "stats": {"memory_usage_mb": 1.5}}
    cache = types.ModuleType("database.token_db_enhanced")
    cache.load_cache_for_broker = lambda broker: True
    cache.get_cache_stats = lambda: stats
    monkeypatch.setitem(sys.modules, "database.token_db_enhanced", cache)

    restores = []
    strategy = types.ModuleType("blueprints.python_strategy")
    strategy.restore_strategies_after_login = lambda: restores.append(1) or (True, "ok")
    monkeypatch.setitem(sys.modules, "blueprints.python_strategy", strategy)

    emitted = []
    monkeypatch.setattr(hook.socketio, "emit", lambda event, payload, **kw: emitted.append((event, payload)))
    monkeypatch.setattr(hook.time, "sleep", lambda s: None)
    return types.SimpleNamespace(cache=cache, restores=restores, emitted=emitted)


def test_without_a_server_the_cache_loads_cleanly_and_nothing_is_emitted(fakes, monkeypatch):
    monkeypatch.setattr(hook.socketio, "server", None, raising=False)
    errors = []
    monkeypatch.setattr(hook.logger, "exception", lambda *a, **k: errors.append(a))
    assert hook.load_symbols_to_cache("flattrade") is True
    assert fakes.emitted == [] and errors == []


def test_without_a_server_the_scheduler_cli_does_not_start_the_strategy_module(fakes, monkeypatch):
    """Lifecycle kept as it was in the CLI: no restore there (a separate decision)."""
    monkeypatch.setattr(hook.socketio, "server", None, raising=False)
    hook.hook_into_master_contract_download("flattrade")
    assert fakes.restores == []


def test_with_a_server_the_browser_gets_the_same_event_as_before(fakes, monkeypatch):
    monkeypatch.setattr(hook.socketio, "server", _Server(), raising=False)
    assert hook.load_symbols_to_cache("flattrade") is True
    assert fakes.emitted == [
        (
            "cache_loaded",
            {
                "status": "success",
                "broker": "flattrade",
                "total_symbols": 5,
                "memory_usage_mb": 1.5,
                "load_time": fakes.emitted[0][1]["load_time"],
            },
        )
    ]


def test_with_a_server_the_restore_still_runs(fakes, monkeypatch):
    monkeypatch.setattr(hook.socketio, "server", _Server(), raising=False)
    hook.hook_into_master_contract_download("flattrade")
    assert fakes.restores == [1]


def test_a_failing_emit_never_stops_the_cache_or_the_restore(fakes, monkeypatch):
    monkeypatch.setattr(hook.socketio, "server", _Server(), raising=False)

    def boom(event, payload, **kw):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(hook.socketio, "emit", boom)
    assert hook.load_symbols_to_cache("flattrade") is True
    hook.hook_into_master_contract_download("flattrade")
    assert fakes.restores == [1]


def test_a_failed_cache_load_still_reports_false_and_notifies_when_a_server_exists(fakes, monkeypatch):
    monkeypatch.setattr(hook.socketio, "server", _Server(), raising=False)
    fakes.cache.load_cache_for_broker = lambda broker: False
    assert hook.load_symbols_to_cache("flattrade") is False
    assert fakes.emitted[0][1]["status"] == "error"


def test_an_exception_in_the_cache_load_does_not_raise_without_a_server(fakes, monkeypatch):
    monkeypatch.setattr(hook.socketio, "server", None, raising=False)

    def explode(broker):
        raise ValueError("bad master contract")

    fakes.cache.load_cache_for_broker = explode
    assert hook.load_symbols_to_cache("flattrade") is False
