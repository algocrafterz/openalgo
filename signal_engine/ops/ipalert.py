"""INVALID_IP alert: tell the owner to update the Flattrade IP allow-list (BUG-024).

The broker rejects the login when the public IP is not on its allow-list. The failure text of the
funds check does not always carry the reason (2026-11-06 reported 'Invalid openalgo apikey' while
OpenAlgo's own log held 'INVALID_IP'), so the check also looks for INVALID_IP in today's OpenAlgo
log. The egress IP is logged and sent masked (a.b.xxx.xxx). Never raises. The core hook that raises
INVALID_IP (the OpenAlgo scheduler auto-login) is outside signal_engine and is not changed here.
"""

from __future__ import annotations

import os
import re
import urllib.request
from collections.abc import Callable

from loguru import logger

from signal_engine.ops import logs

MARKER = "INVALID_IP"
IP_ECHO_URL = "https://api.ipify.org"
MESSAGE = (
    "Flattrade rejected the broker login with INVALID_IP: update the Flattrade IP allow-list "
    "with this PC's current public IP (now {ip}; find it with: curl https://api.ipify.org). "
    "Login stays blocked until then."
)
_V4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}$")
_V6 = re.compile(r"^([0-9a-fA-F]{1,4}):([0-9a-fA-F]{1,4}):[0-9a-fA-F:]+$")


def mask_ip(ip: str) -> str:
    """49.207.12.34 -> 49.207.xxx.xxx; an IPv6 keeps its first two groups; else 'unknown'."""
    ip = (ip or "").strip()
    m = _V4.match(ip)
    if m:
        return f"{m.group(1)}.{m.group(2)}.xxx.xxx"
    m = _V6.match(ip)
    if m:
        return f"{m.group(1)}:{m.group(2)}:xxxx"
    return "unknown"


def egress_ip() -> str:
    """The public IP seen by an IP-echo service ('' when it cannot be fetched)."""
    try:
        with urllib.request.urlopen(IP_ECHO_URL, timeout=5) as response:  # noqa: S310 - fixed URL
            return response.read().decode().strip()
    except Exception:  # noqa: BLE001
        return ""


def _read_log(day: str) -> str:
    path = os.path.join(logs.REPO, "log", f"openalgo_{day}.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "".join(line for line in handle if MARKER in line)
    except OSError:
        return ""


def _send(text: str, **kwargs) -> bool:
    from signal_engine.ops import alerts

    return alerts.deliver(text, **kwargs)


def maybe_alert(
    failure_text: str,
    day: str,
    *,
    send: Callable[..., bool] = _send,
    egress: Callable[[], str] = egress_ip,
    read_log: Callable[[str], str] = _read_log,
) -> bool:
    """Alert when the broker failure is INVALID_IP. True when an alert was sent; never raises."""
    try:
        if MARKER not in (failure_text or "") and MARKER not in read_log(day):
            return False
        masked = mask_ip(egress())
        logger.error("Broker login rejected with INVALID_IP; egress public IP is {}", masked)
        return bool(
            send(
                MESSAGE.format(ip=masked),
                kind="invalid-ip",
                phase="open-day",
                condition=MARKER,
                severity="RED",
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("INVALID_IP alert failed: {}", type(exc).__name__)
        return False
