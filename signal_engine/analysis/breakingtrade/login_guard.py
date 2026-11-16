"""Rate limit on breakingtrade.com sign-ins, shared by every process on this machine.

On 2026-11-10 the vendor locked the account for "suspicious activity" after repeated login
attempts. The poller had an attempt per failed poll: a poll that failed dropped the browser,
the next 20s loop opened a fresh one (the vendor login is a session cookie and does not survive
a restart), and that browser signed in again. A rejected sign-in therefore repeated every few
seconds, and a second poller instance doubled it.

This guard decides, from a small JSON file, whether a sign-in may be attempted at all:

- at most one attempt per MIN_GAP_MINUTES,
- at most MAX_PER_DAY attempts per IST day,
- one EXTRA attempt per day (hard cap MAX_PER_DAY + 1) once a session drop is confirmed: a
  keepalive or page check saw signed_out at least MIN_GAP_MINUTES after a sign-in that had
  succeeded (BUG-027: the vendor session kept dying 20-30 minutes after each sign-in),
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
MAX_EXTRA_PER_DAY = 1


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


def _parse(values) -> list:
    out = []
    for v in values or []:
        try:
            out.append(datetime.fromisoformat(v))
        except (TypeError, ValueError):
            pass
    return out


def _extra_available(state: dict, attempts: list) -> bool:
    """True when the one extra sign-in may be used: the normal quota is spent, the extra is
    unused, and a drop was confirmed after the latest successful sign-in."""
    if len(attempts) != MAX_PER_DAY or state.get("extra_used"):
        return False
    successes = _parse(state.get("successes"))
    drop = _parse([state.get("drop_confirmed_at")])
    return bool(successes and drop and drop[0] > successes[-1])


def _refusal(state: dict, attempts: list, now: datetime, path: str) -> str | None:
    today = now.date().isoformat()
    if state.get("failed_day") == today:
        return (
            "A sign-in to breakingtrade.com was rejected earlier today. No more sign-in "
            "attempts will be made today, to avoid locking the account. Check the "
            "credentials, sign in by hand, then delete "
            f"{path} to allow attempts again."
        )
    extra_ok = _extra_available(state, attempts)
    if len(attempts) >= MAX_PER_DAY and not extra_ok:
        if state.get("extra_used"):
            return (
                f"Already signed in {len(attempts)} times today; the extra sign-in after a "
                "confirmed session drop was used; no more attempts today."
            )
        return (
            f"Already signed in {MAX_PER_DAY} times today; no more attempts today. "
            "Extra sign-in allowance not used (no confirmed session drop)."
        )
    if attempts and now - attempts[-1] < timedelta(minutes=MIN_GAP_MINUTES):
        return (
            f"Last sign-in attempt was under {MIN_GAP_MINUTES} minutes ago; "
            "waiting before trying again."
        )
    return None


def _today_state(path: str, now: datetime) -> tuple:
    today = now.date().isoformat()
    state = _load(path)
    if state.get("day") != today:
        if state.get("failed_day") == today:
            state = {"day": today, "failed_day": today, "attempts": []}
        else:
            state = {"day": today, "attempts": []}
    attempts = [datetime.fromisoformat(a) for a in state.get("attempts", [])]
    return state, attempts


def refusal(now: datetime | None = None, path: str = STATE_PATH) -> str | None:
    """Would a sign-in be refused right now? Read-only: claims nothing, so the caller can skip
    starting a browser that could not sign in anyway."""
    now = now or _now()
    with _Locked(path):
        state, attempts = _today_state(path, now)
        return _refusal(state, attempts, now, path)


def reserve_attempt(now: datetime | None = None, path: str = STATE_PATH) -> str | None:
    """Claim the right to sign in. Returns None when allowed, else the reason it is refused.

    A granted claim is recorded before the sign-in starts, so a crash mid-attempt still counts.
    """
    now = now or _now()
    with _Locked(path):
        state, attempts = _today_state(path, now)
        reason = _refusal(state, attempts, now, path)
        if reason:
            return reason
        if len(attempts) >= MAX_PER_DAY:
            state["extra_used"] = True
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


def record_success(now: datetime | None = None, path: str = STATE_PATH) -> None:
    """A sign-in took (the page showed a signed-in state afterwards)."""
    now = now or _now()
    with _Locked(path):
        state, _attempts = _today_state(path, now)
        successes = [s.isoformat() for s in _parse(state.get("successes"))]
        successes.append(now.isoformat())
        state["successes"] = successes
        state.pop("drop_confirmed_at", None)
        _save(path, state)


def note_session_drop(now: datetime | None = None, path: str = STATE_PATH) -> bool:
    """A check observed signed_out. Confirmed only if a sign-in succeeded today and that
    success is at least MIN_GAP_MINUTES old. Returns True when the drop is confirmed."""
    now = now or _now()
    with _Locked(path):
        state, _attempts = _today_state(path, now)
        successes = _parse(state.get("successes"))
        if not successes or now - successes[-1] < timedelta(minutes=MIN_GAP_MINUTES):
            return False
        state["drop_confirmed_at"] = now.isoformat()
        _save(path, state)
        return True


def attempt_info(now: datetime | None = None, path: str = STATE_PATH) -> dict:
    """Facts for the per-attempt log line: attempt count today, whether the extra allowance
    has been used, and minutes since the last successful sign-in (None if none today)."""
    now = now or _now()
    with _Locked(path):
        state, attempts = _today_state(path, now)
        successes = _parse(state.get("successes"))
        age = round((now - successes[-1]).total_seconds() / 60) if successes else None
        return {
            "attempts": len(attempts),
            "extra_used": bool(state.get("extra_used")),
            "last_success_age_min": age,
        }
