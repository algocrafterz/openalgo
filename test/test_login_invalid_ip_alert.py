"""BUG-029: INVALID_IP alert hook in the scheduler auto-login."""

import logging
import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from signal_engine.ops import loginalert
from signal_engine.scripts import openalgoscheduler as sched

SECRETS = ("pw-SECRET-1", "tok-SECRET-2", "key-SECRET-3")


@pytest.fixture(autouse=True)
def _clear():
    loginalert._sent.clear()
    yield
    loginalert._sent.clear()


def _run_now(fn):
    fn()


def test_invalid_ip_sends_exactly_one_alert_and_dedupes_same_day():
    send = MagicMock()
    day1 = datetime(2026, 11, 17, 9, 0)
    assert loginalert.notify_login_failure("Invalid Input : INVALID_IP", send=send, spawn=_run_now, now=day1)
    assert not loginalert.notify_login_failure("invalid_ip again", send=send, spawn=_run_now, now=day1)
    assert send.call_count == 1
    assert send.call_args[0][0] == (
        "Broker rejected the login: INVALID_IP. Update the Flattrade IP allow-list for this network."
    )
    assert loginalert.notify_login_failure("INVALID_IP", send=send, spawn=_run_now, now=datetime(2026, 11, 18, 9, 0))
    assert send.call_count == 2


def test_other_errors_send_nothing():
    send = MagicMock()
    for err in ("Invalid TOTP", "", None, "token exchange HTTP 500"):
        assert not loginalert.notify_login_failure(err, send=send, spawn=_run_now)
    send.assert_not_called()


def test_sender_exception_does_not_propagate():
    send = MagicMock(side_effect=RuntimeError("boom " + SECRETS[0]))
    assert loginalert.notify_login_failure("INVALID_IP", send=send, spawn=_run_now)
    loginalert._sent.clear()
    bad_spawn = MagicMock(side_effect=RuntimeError("no thread"))
    assert not loginalert.notify_login_failure("INVALID_IP", send=send, spawn=bad_spawn)


def test_default_spawn_does_not_block_caller():
    gate = threading.Event()
    done = threading.Event()

    def slow(_text):
        gate.wait(5)
        done.set()

    assert loginalert.notify_login_failure("INVALID_IP", send=slow)
    assert not done.is_set()
    gate.set()
    assert done.wait(5)


def test_no_secrets_in_logs_or_alert(caplog):
    send = MagicMock(side_effect=RuntimeError("x " + SECRETS[1]))
    err = "INVALID_IP " + " ".join(SECRETS)
    with caplog.at_level(logging.DEBUG):
        loginalert.notify_login_failure(err, send=send, spawn=_run_now)
    assert not any(s in caplog.text for s in SECRETS)
    assert not any(s in loginalert.MESSAGE for s in SECRETS)
    assert not any(s in str(send.call_args) for s in SECRETS)


def _login(error, token=None):
    env = {"broker_password": SECRETS[0], "totp_secret": "JBSWY3DPEHPK3PXP"}
    with patch.object(sched, "validate_auto_login_env", return_value=env):
        return sched._totp_session_token(
            "flattrade", "u", lambda u: (None, None), lambda pw, code: (token, None, error),
            lambda *a, **k: True,
        )


def test_login_result_unchanged_and_alert_sent_only_for_invalid_ip():
    with patch.object(loginalert, "notify_login_failure") as hook:
        assert _login("Invalid Input : INVALID_IP") == (False, "Invalid Input : INVALID_IP", None)
        assert hook.call_count == 1
    with patch.object(loginalert, "notify_login_failure", side_effect=RuntimeError("x")):
        assert _login("Invalid Input : INVALID_IP") == (False, "Invalid Input : INVALID_IP", None)
    assert _login("Invalid TOTP") == (False, "Invalid TOTP", None)
    assert _login(None, "tok") == (True, "", "tok")


def test_login_invalid_ip_end_to_end_one_alert():
    send = MagicMock()
    with patch.object(loginalert, "_deliver", send), patch.object(loginalert, "_spawn", _run_now):
        _login("INVALID_IP")
        _login("INVALID_IP")
    assert send.call_count == 1
