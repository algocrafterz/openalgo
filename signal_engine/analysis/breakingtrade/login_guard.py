"""Rate limit on breakingtrade.com sign-ins, shared by every process on this machine.

On 2026-11-10 the vendor locked the account for "suspicious activity" after repeated login
attempts. The poller had an attempt per failed poll: a poll that failed dropped the browser,
the next 20s loop opened a fresh one (the vendor login is a session cookie and does not survive
a restart), and that browser signed in again. A rejected sign-in therefore repeated every few
seconds, and a second poller instance doubled it.

This guard decides, from a small JSON file, whether a sign-in may be attempted at all:

- at most one attempt per MIN_GAP_MINUTES,
- at most MAX_PER_DAY attempts per IST day,
- after a sign-in that did not take, no attempt until the next IST day, or until the file is
  removed by hand (an account that rejects the password must not be retried by a machine).

The check and the record happen under one file lock, so two processes cannot both pass it.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta

from signal_engine.timeutils import IST

_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_PATH = os.path.join(_DIR, "data", "breakingtrade_login_guard.json")

MIN_GAP_MINUTES = 30
MAX_PER_DAY = 3


def _now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _load(path: str) -> dict:
    try:
        with open(path) as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {}


def _save(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


class _Locked:
    def __init__(self, path: str):
        self._lock_path = f"{path}.lock"
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self._lock_path), exist_ok=True)
        self._fh = open(self._lock_path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()


def reserve_attempt(now: datetime | None = None, path: str = STATE_PATH) -> str | None:
    """Claim the right to sign in. Returns None when allowed, else the reason it is refused.

    A granted claim is recorded before the sign-in starts, so a crash mid-attempt still counts.
    """
    now = now or _now()
    today = now.date().isoformat()
    with _Locked(path):
        state = _load(path)
        if state.get("day") != today:
            if state.get("failed_day") == today:
                state = {"day": today, "failed_day": today, "attempts": []}
            else:
                state = {"day": today, "attempts": []}
        attempts = [datetime.fromisoformat(a) for a in state.get("attempts", [])]

        if state.get("failed_day") == today:
            return (
                "A sign-in to breakingtrade.com was rejected earlier today. No more sign-in "
                "attempts will be made today, to avoid locking the account. Check the "
                "credentials, sign in by hand, then delete "
                f"{path} to allow attempts again."
            )
        if len(attempts) >= MAX_PER_DAY:
            return f"Already signed in {MAX_PER_DAY} times today; no more attempts today."
        if attempts and now - attempts[-1] < timedelta(minutes=MIN_GAP_MINUTES):
            return (
                f"Last sign-in attempt was under {MIN_GAP_MINUTES} minutes ago; "
                "waiting before trying again."
            )

        attempts.append(now)
        state["attempts"] = [a.isoformat() for a in attempts]
        _save(path, state)
        return None


def record_failure(now: datetime | None = None, path: str = STATE_PATH) -> None:
    """A sign-in did not take: block further attempts for the rest of the IST day."""
    now = now or _now()
    with _Locked(path):
        state = _load(path)
        state["day"] = now.date().isoformat()
        state["failed_day"] = now.date().isoformat()
        state.setdefault("attempts", [])
        _save(path, state)
