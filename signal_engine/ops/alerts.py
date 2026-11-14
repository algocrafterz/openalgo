"""Telegram alerts of the ops phases: bounded retry, no repeated RED, recovery, safe channel.

Plain scripts call this (cron shells, `python -m signal_engine.ops`), never the OpenAlgo gunicorn
worker, so real threads, time.sleep and a flock are correct here. State lives in one small file,
reports/telegram-state.json, written atomically under a cross-process lock (model.file_lock).

RULES (one place, so a reader does not have to find them in the code)

Retry: 3 attempts per channel. Wait 1 s, then 3 s. Retry only on a connection error, a timeout,
HTTP 5xx, or HTTP 429 (wait the Retry-After value, at most 5 s). Never retry another 4xx: the
request itself is wrong, and a second try cannot fix it. One shared deadline of 30 s covers
every attempt and every channel.

Dedupe: the key is (kind, phase, condition). Only a RED alert is deduped. A RED for a key that is
already open is not sent again when its text is the same after dates and times are masked, and
the last delivery was less than 23 hours ago and succeeded. The 23 h cooldown (not 24 h) lets the
same 09:12 alert of the next trading day through. A failed delivery never suppresses the next try.
AMBER and GREEN lines are information, not alarms: they are always sent.

Recovery: when a key with an open RED gets a non-RED result, one recovery line is sent (joined to
the day's own line when there is one) and the key closes.

Channel: the trading mode decides between the analyze and live channel.
  1. OpenAlgo answers: use that mode and remember it, with the time, in the state file.
  2. OpenAlgo does not answer and the remembered mode is at most 12 hours old: use it, and say so.
  3. Otherwise send to BOTH configured channels, and say the mode could not be confirmed.
A message is never silently routed to the analyze channel because a lookup failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from datetime import datetime

import httpx
from loguru import logger

from signal_engine.ops import model
from signal_engine.ops.model import atomic_write_json, file_lock, now_ist

STATE_FILE = "telegram-state.json"
ATTEMPTS = 3
BACKOFF = (1.0, 3.0)
RETRY_AFTER_CAP = 5.0
TOTAL_DEADLINE = 30.0
ATTEMPT_TIMEOUT = 8.0
COOLDOWN_SECONDS = 23 * 3600
LAST_MODE_MAX_AGE = 12 * 3600
SENDING_CLAIM_SECONDS = 60
LOG_KEEP = 50
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_MASK = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}(:\d{2})?")

SENT, FAILED, SUPPRESSED = "sent", "failed_after_retries", "suppressed_duplicate"


# ---- state file ----------------------------------------------------------------------------


def _folder(folder: str | None) -> str:
    return folder or model.REPORTS


def _empty() -> dict:
    return {"version": 1, "last_mode": None, "keys": {}, "log": []}


def load_state(folder: str | None = None) -> dict:
    """The state, or an empty one when the file is missing or damaged (the damaged file is kept)."""
    path = os.path.join(_folder(folder), STATE_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
            raise ValueError("not a state object")
        data.setdefault("log", [])
        data.setdefault("last_mode", None)
        return data
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError):
        logger.exception("The Telegram state file is unreadable; starting with an empty state")
        with contextlib.suppress(OSError):
            os.replace(path, path + ".corrupt")
        return _empty()


def _save(state: dict, folder: str | None) -> None:
    state["log"] = state["log"][-LOG_KEEP:]
    atomic_write_json(os.path.join(_folder(folder), STATE_FILE), state)


def _mutate(change, folder: str | None):
    """Read, change and write the state under the lock. Returns what `change` returns."""
    with file_lock(_folder(folder)):
        state = load_state(folder)
        result = change(state)
        _save(state, folder)
    return result


def make_key(kind: str, phase: str, condition: str) -> str:
    return f"{kind}|{phase}|{condition}"


def fingerprint(text: str) -> str:
    """The text with dates and times masked: a re-run of the same problem is not a new problem."""
    return " ".join(_MASK.sub("#", text).split())


def _age(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


# ---- channel choice ------------------------------------------------------------------------


def _lookup_mode() -> str | None:
    """'live' or 'analyze' when OpenAlgo says so, else None."""
    from signal_engine.api_client import fetch_trading_mode

    try:
        mode, is_analyze = asyncio.run(fetch_trading_mode())
    except Exception:  # noqa: BLE001 - an unreadable mode must not stop a failure alert
        logger.exception("The trading mode lookup failed")
        return None
    if mode == "live" and not is_analyze:
        return "live"
    if mode == "analyze" or is_analyze:
        return "analyze"
    return None


def resolve_channel(
    *, now: datetime | None = None, folder: str | None = None
) -> tuple[str, str, str]:
    """(channel, how, note): channel is live, analyze or both; how is confirmed, last_known or
    unconfirmed; note is the line to add to the message ('' when confirmed)."""
    now = now or now_ist()
    mode = _lookup_mode()
    if mode:
        with contextlib.suppress(OSError, TimeoutError):
            _mutate(
                lambda s: s.update(
                    last_mode={"mode": mode, "at": now.isoformat(timespec="seconds")}
                ),
                folder,
            )
        return mode, "confirmed", ""
    last = load_state(folder).get("last_mode") or {}
    age = _age(last.get("at"), now)
    if (
        last.get("mode") in ("live", "analyze")
        and age is not None
        and 0 <= age <= LAST_MODE_MAX_AGE
    ):
        return (
            last["mode"],
            "last_known",
            f"Trading mode could not be confirmed now (OpenAlgo did not answer). "
            f"Sent to the {last['mode']} channel, the last known mode.",
        )
    return (
        "both",
        "unconfirmed",
        "Trading mode could not be confirmed (OpenAlgo did not answer). "
        "Sent to both the live and the analyze channel.",
    )


def _telegram_config() -> tuple[str | None, dict]:
    """The bot token and {name: channel id}. Values are never logged."""
    from signal_engine.analysis.breakingtrade.alerts import _clean_token, _env
    from signal_engine.config import settings

    token = _clean_token(_env().get("BREAKINGTRADE_BOT_TOKEN") or "")
    channels = {
        name: str(ch.id)
        for name, ch in settings.notify_channel.items()
        if name in ("live", "analyze")
    }
    return token, channels


# ---- transport -----------------------------------------------------------------------------


def _retry_after(response) -> float:
    try:
        return min(max(float(response.headers.get("Retry-After", "")), 0.0), RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        return BACKOFF[0]


def post_with_retry(token: str, chat_id: str, text: str, budget: float) -> tuple[bool, int, str]:
    """One message, bounded retry. Returns (delivered, attempts used, reason of the last failure)."""
    started = time.monotonic()
    reason = ""
    body = {
        "chat_id": chat_id,
        "text": f"```\n{text.replace(chr(96), chr(39))}\n```",
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, ATTEMPTS + 1):
        left = budget - (time.monotonic() - started)
        if left <= 0.5:
            return False, attempt - 1, reason or "time budget used"
        wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
        try:
            response = httpx.post(
                _TELEGRAM_API.format(token=token), json=body, timeout=min(ATTEMPT_TIMEOUT, left)
            )
        except httpx.TransportError as exc:
            reason = f"{type(exc).__name__}"
        else:
            if response.status_code == 200:
                return True, attempt, ""
            reason = f"HTTP {response.status_code}"
            if response.status_code == 429:
                wait = _retry_after(response)
            elif response.status_code < 500:
                return False, attempt, reason  # a 4xx is final: the request is wrong
        if attempt == ATTEMPTS or budget - (time.monotonic() - started) <= wait + 0.5:
            return False, attempt, reason
        time.sleep(wait)
    return False, ATTEMPTS, reason


def _send_text(text: str, channel: str) -> tuple[bool, int, str]:
    """Send to the chosen channel (both when `channel` is both). True when any delivery worked."""
    token, channels = _telegram_config()
    targets = [
        channels[n]
        for n in (("live", "analyze") if channel == "both" else (channel,))
        if n in channels
    ]
    if not token or not targets:
        logger.error("Telegram is not configured for the ops alerts (token or channel missing)")
        return False, 0, "not configured"
    started = time.monotonic()
    ok, used, reason = False, 0, ""
    for index, chat_id in enumerate(targets):
        budget = (TOTAL_DEADLINE - (time.monotonic() - started)) / (len(targets) - index)
        sent, attempts, why = post_with_retry(token, chat_id, text, budget)
        ok, used, reason = ok or sent, used + attempts, reason if sent else why
    return ok, used, "" if ok else reason


# ---- public API ----------------------------------------------------------------------------


def _record(
    key: str,
    outcome: str,
    text: str,
    now: datetime,
    attempts: int,
    reason: str,
    *,
    red: bool | None,
    folder: str | None,
) -> None:
    def change(state: dict) -> None:
        entry = state["keys"].setdefault(key, {"status": "ok"})
        stamp = now.isoformat(timespec="seconds")
        if outcome != SUPPRESSED:  # a suppressed run must not release the sender's claim
            entry.pop("sending_since", None)
        entry.update(last_outcome=outcome, last_attempt_at=stamp, attempts=attempts, reason=reason)
        if outcome == SENT:
            entry["last_sent_at"] = stamp
        if red is True:
            entry["status"] = "red"
            entry["fingerprint"] = fingerprint(text)
        elif red is False and outcome == SENT:
            entry["status"] = "ok"
            entry["recovered_at"] = stamp
        state["log"].append({"at": stamp, "key": key, "outcome": outcome, "attempts": attempts})

    try:
        _mutate(change, folder)
    except (OSError, TimeoutError):
        logger.exception("The Telegram delivery outcome could not be recorded")


def deliver(
    text: str,
    *,
    kind: str,
    phase: str,
    condition: str,
    severity: str | None,
    now: datetime | None = None,
    folder: str | None = None,
) -> bool:
    """Send one ops alert under the retry, dedupe, recovery and channel rules above.

    Returns True when the message reached Telegram or an equal RED had already reached it.
    """
    now = now or now_ist()
    key = make_key(kind, phase, condition)
    red = severity == "RED"
    try:
        verdict = _decide(key, text, red, now, folder)
    except TimeoutError:
        logger.error("The ops state lock stayed busy; sending without the duplicate check")
        verdict = "send"
    if verdict == "suppress":
        _record(
            key, SUPPRESSED, text, now, 0, "same RED inside the cooldown", red=None, folder=folder
        )
        return True
    prefix = ""
    if not red and verdict == "recover":
        prefix = f"Recovered: the earlier RED for {phase} is cleared.\n"
    channel, _how, note = resolve_channel(now=now, folder=folder)
    full = f"{prefix}{text}" + (f"\n{note}" if note else "")
    ok, attempts, reason = _send_text(full, channel)
    _record(
        key,
        SENT if ok else FAILED,
        text,
        now,
        attempts,
        reason,
        red=True if red else (False if ok else None),
        folder=folder,
    )
    return ok


def _decide(key: str, text: str, red: bool, now: datetime, folder: str | None) -> str:
    """send, suppress, or recover. Marks the key as being sent so a parallel run does not repeat."""

    def change(state: dict) -> str:
        entry = state["keys"].get(key) or {}
        claim = _age(entry.get("sending_since"), now)
        claimed = claim is not None and 0 <= claim < SENDING_CLAIM_SECONDS
        if not red:
            if entry.get("status") != "red":
                return "send"
            if claimed:
                return "suppress"  # a parallel run is already sending the recovery
            entry["sending_since"] = now.isoformat(timespec="seconds")
            return "recover"
        if claimed:
            return "suppress"
        if entry.get("status") == "red" and entry.get("last_outcome") == SENT:
            since = _age(entry.get("last_sent_at"), now)
            if (
                since is not None
                and 0 <= since < COOLDOWN_SECONDS
                and entry.get("fingerprint") == fingerprint(text)
            ):
                return "suppress"
        state["keys"].setdefault(key, {"status": "ok"})["sending_since"] = now.isoformat(
            timespec="seconds"
        )
        return "send"

    return _mutate(change, folder)


def recover(
    *,
    kind: str,
    phase: str,
    condition: str | None = None,
    now: datetime | None = None,
    folder: str | None = None,
) -> bool:
    """Send one recovery line for every open RED under kind and phase (or one condition).

    Returns True when a line was sent. Nothing is sent when no RED is open.
    """
    now = now or now_ist()
    prefix = make_key(kind, phase, condition or "")

    def claim(state: dict) -> list[str]:
        """Open RED keys with no fresh claim; marks them as being recovered by this run."""
        taken = []
        for k, v in state["keys"].items():
            if v.get("status") != "red" or not (k == prefix if condition else k.startswith(prefix)):
                continue
            age = _age(v.get("sending_since"), now)
            if age is not None and 0 <= age < SENDING_CLAIM_SECONDS:
                continue  # a parallel run is already sending this recovery
            v["sending_since"] = now.isoformat(timespec="seconds")
            taken.append(k)
        return taken

    try:
        open_keys = _mutate(claim, folder)
    except (OSError, TimeoutError):
        return False
    if not open_keys:
        return False
    channel, _how, note = resolve_channel(now=now, folder=folder)
    text = f"Recovered: {phase} is no longer RED. Details: /logs/workflow" + (
        f"\n{note}" if note else ""
    )
    ok, attempts, reason = _send_text(text, channel)
    for key in open_keys:
        _record(
            key,
            SENT if ok else FAILED,
            text,
            now,
            attempts,
            reason,
            red=False if ok else None,
            folder=folder,
        )
    return ok
