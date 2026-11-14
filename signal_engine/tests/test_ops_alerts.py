"""Telegram alerts: bounded retry, dedupe, cooldown, recovery, channel rule, state file safety.

The HTTP layer is mocked in every test: no real Telegram message is ever sent.
"""

import json
import threading
import time as real_time
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from signal_engine.ops import alerts, model, verdict
from signal_engine.timeutils import IST

T0 = datetime(2026, 11, 12, 9, 12, tzinfo=IST)
RED = "open-day 2026-11-12: RED. Failed: services. Details: /logs/workflow"


class Resp:
    def __init__(self, code, headers=None):
        self.status_code = code
        self.headers = headers or {}
        self.text = ""


class Net:
    """Scripted httpx.post: a list of outcomes (status code, (code, headers) or exception)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.sleeps = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - mirrors httpx.post
        self.calls.append(
            {"url": url, "chat": json["chat_id"], "text": json["text"], "timeout": timeout}
        )
        item = self.script.pop(0) if self.script else 200
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return Resp(item[0], item[1])
        return Resp(item)


@pytest.fixture
def net(tmp_path, monkeypatch):
    n = Net()
    monkeypatch.setattr(alerts.httpx, "post", n.post)
    # Replace the module's clock, never the global time module (the file lock uses that one).
    monkeypatch.setattr(
        alerts, "time", SimpleNamespace(monotonic=real_time.monotonic, sleep=n.sleeps.append)
    )
    monkeypatch.setattr(
        alerts, "_telegram_config", lambda: ("tok", {"live": "111", "analyze": "222"})
    )
    monkeypatch.setattr(alerts, "_lookup_mode", lambda: "live")
    monkeypatch.setattr(model, "REPORTS", str(tmp_path))
    return n


def _send(
    text=RED,
    *,
    severity="RED",
    now=T0,
    phase="open-day",
    condition="verdict",
    kind="phase",
    folder=None,
):
    return alerts.deliver(
        text, kind=kind, phase=phase, condition=condition, severity=severity, now=now, folder=folder
    )


def _state(tmp_path):
    return json.loads((tmp_path / "telegram-state.json").read_text())


# ---- retry -------------------------------------------------------------------------------------


def test_transient_failures_then_success(net, tmp_path):
    net.script = [httpx.ConnectError("down"), 503, 200]
    assert _send() is True
    assert len(net.calls) == 3
    assert net.sleeps == [1.0, 3.0]
    entry = _state(tmp_path)["keys"]["phase|open-day|verdict"]
    assert entry["last_outcome"] == "sent" and entry["attempts"] == 3


def test_timeout_is_retried(net):
    net.script = [httpx.ReadTimeout("slow"), 200]
    assert _send() is True
    assert len(net.calls) == 2 and net.sleeps == [1.0]


def test_exhausted_retries_are_recorded_as_failed(net, tmp_path):
    net.script = [503, 500, 502]
    assert _send() is False
    assert len(net.calls) == 3
    assert net.sleeps == [1.0, 3.0]
    entry = _state(tmp_path)["keys"]["phase|open-day|verdict"]
    assert entry["last_outcome"] == "failed_after_retries"
    assert entry["reason"] == "HTTP 502"
    assert entry["last_attempt_at"]


def test_a_4xx_is_not_retried(net, tmp_path):
    net.script = [400]
    assert _send() is False
    assert len(net.calls) == 1 and net.sleeps == []
    assert _state(tmp_path)["keys"]["phase|open-day|verdict"]["reason"] == "HTTP 400"


def test_429_waits_the_retry_after_value_capped(net):
    net.script = [(429, {"Retry-After": "100"}), (429, {"Retry-After": "2"}), 200]
    assert _send() is True
    assert net.sleeps == [alerts.RETRY_AFTER_CAP, 2.0]


def test_each_attempt_has_a_bounded_timeout_and_the_total_is_bounded(net):
    net.script = [503, 503, 503]
    _send()
    assert all(c["timeout"] <= alerts.ATTEMPT_TIMEOUT for c in net.calls)
    worst = alerts.ATTEMPTS * alerts.ATTEMPT_TIMEOUT + sum(alerts.BACKOFF)
    assert worst <= alerts.TOTAL_DEADLINE <= 30


def test_the_time_budget_stops_the_retries(monkeypatch):
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(alerts, "time", SimpleNamespace(monotonic=lambda: clock["t"], sleep=sleep))

    def slow(url, json=None, timeout=None):  # noqa: A002
        clock["t"] += timeout  # each call burns its whole timeout
        raise httpx.ConnectTimeout("x")

    monkeypatch.setattr(alerts.httpx, "post", slow)
    ok, attempts, _ = alerts.post_with_retry("tok", "1", "t", budget=12.0)
    assert ok is False and attempts < 3
    assert clock["t"] <= 12.5


def test_message_is_plain_text_without_backticks_inside(net):
    _send("a `quoted` word. Details: /logs/workflow")
    body = net.calls[0]["text"]
    assert "`quoted`" not in body
    assert body.isascii()


# ---- dedupe, cooldown, recovery ----------------------------------------------------------------


def test_the_same_unresolved_red_is_not_sent_again(net, tmp_path):
    assert _send() is True
    assert _send(now=T0 + timedelta(minutes=15)) is True  # suppressed counts as delivered
    assert len(net.calls) == 1
    entry = _state(tmp_path)["keys"]["phase|open-day|verdict"]
    assert entry["last_outcome"] == "suppressed_duplicate"
    assert entry["status"] == "red"
    assert entry["last_sent_at"]


def test_the_same_problem_on_a_new_day_is_the_same_text_inside_the_cooldown(net):
    _send(RED)
    _send(RED.replace("2026-11-12", "2026-11-13"), now=T0 + timedelta(minutes=30))
    assert len(net.calls) == 1  # dates and times are masked


def test_the_alert_text_changing_materially_sends_again(net):
    _send(RED)
    _send(RED.replace("services", "services, broker session"), now=T0 + timedelta(minutes=30))
    assert len(net.calls) == 2


def test_the_cooldown_lets_the_same_red_through_after_23_hours(net):
    _send()
    _send(now=T0 + timedelta(hours=22, minutes=59))
    assert len(net.calls) == 1
    _send(now=T0 + timedelta(hours=23, minutes=1))
    assert len(net.calls) == 2


def test_a_failed_delivery_never_suppresses_the_retry_next_run(net):
    net.script = [500, 500, 500]
    assert _send() is False
    assert _send(now=T0 + timedelta(minutes=5)) is True
    assert len(net.calls) == 4


def test_different_keys_do_not_dedupe_each_other(net):
    _send(phase="open-day")
    _send(phase="liveness")
    _send(phase="open-day", condition="other")
    assert len(net.calls) == 3


def test_amber_and_green_lines_are_always_sent(net):
    for i in range(3):
        _send(
            "Close-day 2026-11-12: AMBER. watch. Details: /logs/workflow",
            severity="INFO",
            now=T0 + timedelta(minutes=i),
        )
    assert len(net.calls) == 3


def test_recovery_is_joined_to_the_next_non_red_line(net, tmp_path):
    _send()
    _send("Close-day 2026-11-12: AMBER. watch.", severity="INFO", now=T0 + timedelta(hours=1))
    assert "Recovered: the earlier RED for open-day is cleared." in net.calls[-1]["text"]
    assert _state(tmp_path)["keys"]["phase|open-day|verdict"]["status"] == "ok"
    _send("Close-day 2026-11-12: AMBER. watch.", severity="INFO", now=T0 + timedelta(hours=2))
    assert "Recovered" not in net.calls[-1]["text"]  # only once


def test_standalone_recovery_when_the_phase_has_nothing_to_send(net, tmp_path):
    _send()
    when = T0 + timedelta(hours=1)
    assert alerts.recover(kind="phase", phase="open-day", condition="verdict", now=when)
    assert "Recovered: open-day is no longer RED" in net.calls[-1]["text"]
    assert _state(tmp_path)["keys"]["phase|open-day|verdict"]["status"] == "ok"
    sent = len(net.calls)
    assert alerts.recover(kind="phase", phase="open-day", condition="verdict", now=when) is False
    assert len(net.calls) == sent  # nothing open: nothing sent


def test_recovery_clears_every_open_deadman_condition_for_a_kind(net, tmp_path):
    _send("a", kind="deadman", phase="close", condition="missing")
    _send("b", kind="deadman", phase="close", condition="failed")
    assert alerts.recover(kind="deadman", phase="close", now=T0 + timedelta(hours=1))
    keys = _state(tmp_path)["keys"]
    assert {v["status"] for v in keys.values()} == {"ok"}
    assert len(net.calls) == 3  # two alerts and one recovery line


def test_a_failed_recovery_send_keeps_the_red_open(net, tmp_path):
    _send()
    net.script = [500, 500, 500]
    assert alerts.recover(kind="phase", phase="open-day", now=T0 + timedelta(hours=1)) is False
    assert _state(tmp_path)["keys"]["phase|open-day|verdict"]["status"] == "red"


def test_delivery_outcomes_are_logged_with_a_time(net, tmp_path):
    _send()
    _send(now=T0 + timedelta(minutes=1))
    log = _state(tmp_path)["log"]
    assert [e["outcome"] for e in log] == ["sent", "suppressed_duplicate"]
    assert all(e["at"] for e in log)


# ---- channel rule ------------------------------------------------------------------------------


def test_confirmed_mode_picks_its_channel_and_is_remembered(net, tmp_path):
    _send()
    assert [c["chat"] for c in net.calls] == ["111"]
    assert _state(tmp_path)["last_mode"]["mode"] == "live"
    assert "could not be confirmed" not in net.calls[0]["text"]


def test_confirmed_analyze_mode_uses_the_analyze_channel(net, monkeypatch):
    monkeypatch.setattr(alerts, "_lookup_mode", lambda: "analyze")
    _send()
    assert [c["chat"] for c in net.calls] == ["222"]


def test_lookup_failure_uses_the_fresh_last_known_mode_and_says_so(net, monkeypatch):
    _send()  # remembers live
    monkeypatch.setattr(alerts, "_lookup_mode", lambda: None)
    _send(RED.replace("services", "x"), now=T0 + timedelta(hours=3))
    assert net.calls[-1]["chat"] == "111"
    assert "could not be confirmed" in net.calls[-1]["text"]
    assert "last known mode" in net.calls[-1]["text"]


def test_lookup_failure_with_no_or_stale_memory_sends_to_both_channels(net, monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "_lookup_mode", lambda: None)
    _send()
    assert sorted(c["chat"] for c in net.calls) == ["111", "222"]  # never silently analyze only
    assert all("could not be confirmed" in c["text"] for c in net.calls)
    assert "both the live and the analyze channel" in net.calls[0]["text"]
    # a memory older than 12 hours is not trusted
    st = _state(tmp_path)
    st["last_mode"] = {"mode": "live", "at": (T0 - timedelta(hours=13)).isoformat()}
    (tmp_path / "telegram-state.json").write_text(json.dumps(st))
    net.calls.clear()
    _send("other. Details: /logs/workflow", phase="liveness")
    assert sorted(c["chat"] for c in net.calls) == ["111", "222"]


def test_both_channels_count_as_delivered_if_one_works(net, monkeypatch):
    monkeypatch.setattr(alerts, "_lookup_mode", lambda: None)
    net.script = [400, 200]  # the live channel refuses, the analyze channel accepts
    assert _send() is True


def test_the_lookup_itself_never_raises(monkeypatch):
    async def boom():
        raise RuntimeError("down")

    monkeypatch.setattr("signal_engine.api_client.fetch_trading_mode", boom)
    assert alerts._lookup_mode() is None

    async def unknown():
        return ("unknown", False)

    monkeypatch.setattr("signal_engine.api_client.fetch_trading_mode", unknown)
    assert alerts._lookup_mode() is None


def test_missing_token_or_channel_is_a_recorded_failure(net, monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "_telegram_config", lambda: (None, {}))
    assert _send() is False
    assert net.calls == []
    key = _state(tmp_path)["keys"]["phase|open-day|verdict"]
    assert key["last_outcome"] == "failed_after_retries"


# ---- the state file ----------------------------------------------------------------------------


def test_a_corrupt_state_file_is_tolerated_and_kept(net, tmp_path):
    (tmp_path / "telegram-state.json").write_text("{ this is not json")
    assert _send() is True
    assert (tmp_path / "telegram-state.json.corrupt").read_text() == "{ this is not json"
    assert _state(tmp_path)["keys"]["phase|open-day|verdict"]["last_outcome"] == "sent"


@pytest.mark.parametrize("junk", ["[]", '{"keys": 5}', ""])
def test_a_state_file_of_the_wrong_shape_is_tolerated(net, tmp_path, junk):
    (tmp_path / "telegram-state.json").write_text(junk)
    assert _send() is True


def test_concurrent_deliveries_of_different_keys_lose_no_state(net, tmp_path):
    def go(i):
        _send(f"text {i}. Details: /logs/workflow", phase=f"p{i}")

    threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    keys = _state(tmp_path)["keys"]
    assert sorted(keys) == sorted(f"phase|p{i}|verdict" for i in range(12))
    assert all(v["last_outcome"] == "sent" for v in keys.values())


def test_two_runs_with_the_same_red_at_once_send_one_message(net, tmp_path, monkeypatch):
    gate = threading.Event()
    real = net.post

    def slow(url, json=None, timeout=None):  # noqa: A002
        gate.wait(5)
        return real(url, json=json, timeout=timeout)

    monkeypatch.setattr(alerts.httpx, "post", slow)
    results = []
    first = threading.Thread(target=lambda: results.append(_send()))
    first.start()
    for _ in range(200):  # wait until the first run has claimed the key
        entry = alerts.load_state(str(tmp_path))["keys"].get("phase|open-day|verdict") or {}
        if "sending_since" in entry:
            break
        real_time.sleep(0.02)
    second = threading.Thread(target=lambda: results.append(_send(now=T0 + timedelta(seconds=5))))
    second.start()
    second.join(10)
    gate.set()
    first.join(10)
    assert results == [True, True]
    assert len(net.calls) == 1


def test_two_runs_recovering_one_red_at_once_send_one_recovery(net, tmp_path, monkeypatch):
    _send()  # leaves the key red
    sent_before = len(net.calls)
    barrier = threading.Barrier(2)
    real = net.post

    def slow(url, json=None, timeout=None):  # noqa: A002
        real_time.sleep(0.3)
        return real(url, json=json, timeout=timeout)

    monkeypatch.setattr(alerts.httpx, "post", slow)
    when = T0 + timedelta(hours=1)

    def go():
        barrier.wait(5)
        _send("Close-day 2026-11-12: AMBER. watch.", severity="INFO", now=when)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(net.calls) - sent_before == 1
    assert _state(tmp_path)["keys"]["phase|open-day|verdict"]["status"] == "ok"


def test_two_standalone_recoveries_at_once_send_one_line(net, tmp_path, monkeypatch):
    _send()
    sent_before = len(net.calls)
    barrier = threading.Barrier(2)
    real = net.post

    def slow(url, json=None, timeout=None):  # noqa: A002
        real_time.sleep(0.3)
        return real(url, json=json, timeout=timeout)

    monkeypatch.setattr(alerts.httpx, "post", slow)
    results = []

    def go():
        barrier.wait(5)
        results.append(alerts.recover(kind="phase", phase="open-day", now=T0 + timedelta(hours=1)))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert sorted(results) == [False, True]
    assert len(net.calls) - sent_before == 1


def test_a_failed_recovery_releases_its_claim_for_the_next_run(net, tmp_path):
    _send()
    net.script = [500, 500, 500]
    when = T0 + timedelta(hours=1)
    assert alerts.recover(kind="phase", phase="open-day", now=when) is False
    assert "sending_since" not in _state(tmp_path)["keys"]["phase|open-day|verdict"]
    assert alerts.recover(kind="phase", phase="open-day", now=when) is True


# ---- the ops entry points ----------------------------------------------------------------------


def test_verdict_send_marks_only_red_as_a_dedupe_candidate(monkeypatch):
    seen = []
    monkeypatch.setattr(alerts, "deliver", lambda text, **kw: seen.append((text, kw)) or True)
    monkeypatch.setattr(verdict, "message", lambda phase, day, folder=model.REPORTS: f"{phase} t")
    monkeypatch.setattr(
        verdict, "review_outcome", lambda day, folder=model.REPORTS: {"verdict": "AMBER"}
    )
    assert verdict.send("close-day", "2026-11-12")
    assert seen[-1][1]["severity"] == "INFO"  # the daily close-day line is never a repeated alarm
    monkeypatch.setattr(
        verdict, "review_outcome", lambda day, folder=model.REPORTS: {"verdict": "RED"}
    )
    verdict.send("close-day", "2026-11-12")
    assert seen[-1][1]["severity"] == "RED"
    verdict.send("open-day", "2026-11-12")
    assert seen[-1][1]["severity"] == "RED"  # open-day only has a message when it is RED
    assert seen[-1][1]["kind"] == "phase" and seen[-1][1]["condition"] == "verdict"


def test_verdict_phase_helper_reports_only_the_channel(monkeypatch):
    monkeypatch.setattr(alerts, "resolve_channel", lambda **kw: ("both", "unconfirmed", "note"))
    assert verdict._phase() == "both"
