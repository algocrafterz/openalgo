"""Late-wake check for open-day: was the PC asleep through the 08:30 start? (BUG-024)

On 2026-11-06 and 2026-11-09 the PC woke at 09:39 and 09:46 (Modern Standby; the 08:30 task could
not wake it), so the engine and the broker login missed the morning. The last resume or boot time
comes from Windows through powershell.exe; the provider is injectable and never raises. When the
time cannot be read the check is NOT VERIFIED, not a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone

from signal_engine.ops.model import FAIL, IST, NOT_VERIFIED, OK, WARN, Check

CHECK_NAME = "PC wake time"
#: A resume or boot after this time on a trading day is a WARN, after FAIL_AFTER a FAIL (the
#: engine's own start-up checks and the broker auto-login run in the 08:5x window).
WARN_AFTER = "08:30"
FAIL_AFTER = "09:00"
WINDOWS_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
POWERSHELL_SECONDS = 25

#: Prints the later of the last boot and the last resume from sleep, as one UTC ISO 8601 line.
_SCRIPT = (
    "$t=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime;"
    "$r=Get-WinEvent -FilterHashtable @{LogName='System';"
    "ProviderName='Microsoft-Windows-Power-Troubleshooter';Id=1} -MaxEvents 1 "
    "-ErrorAction SilentlyContinue;"
    "if($r -and $r.TimeCreated -gt $t){$t=$r.TimeCreated};"
    "$t.ToUniversalTime().ToString('o')"
)


def parse_output(text: str) -> datetime | None:
    """The UTC ISO line printed by the script as an IST datetime, or None when unreadable."""
    line = (text or "").strip().splitlines()[-1].strip() if (text or "").strip() else ""
    if not line:
        return None
    try:
        head, _, frac = line.rstrip("Z").partition(".")
        parsed = datetime.fromisoformat(head + ("." + frac[:6] if frac else ""))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).astimezone(IST)


def _powershell() -> str | None:
    found = shutil.which("powershell.exe")
    if found:
        return found
    return WINDOWS_POWERSHELL if os.path.exists(WINDOWS_POWERSHELL) else None


def last_wake() -> datetime | None:
    """The last Windows resume or boot as an IST datetime; None when it cannot be read."""
    exe = _powershell()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
            capture_output=True,
            text=True,
            timeout=POWERSHELL_SECONDS,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_output(out)


def _default_is_trading(day: str) -> bool:
    from signal_engine.ops import automation

    return automation.is_trading_day(day)


def late_wake_check(
    day: str,
    *,
    provider: Callable[[], datetime | None] | None = None,
    is_trading: Callable[[str], bool] = _default_is_trading,
) -> Check:
    """WARN/FAIL when the last resume or boot of `day` was after 08:30 IST; never raises."""
    try:
        if not is_trading(day):
            return Check(CHECK_NAME, OK, f"{day} is not a trading day: not judged")
        woke = (provider or last_wake)()
    except Exception as exc:  # noqa: BLE001 - the check must never break open-day
        return Check(CHECK_NAME, NOT_VERIFIED, f"could not read the wake time: {type(exc).__name__}")
    if woke is None:
        return Check(CHECK_NAME, NOT_VERIFIED, "powershell.exe unavailable or no wake time found")
    if woke.tzinfo is None:
        woke = woke.replace(tzinfo=IST)
    woke = woke.astimezone(IST)
    clock = woke.strftime("%H:%M")
    if woke.strftime("%Y-%m-%d") != day or clock <= WARN_AFTER:
        return Check(CHECK_NAME, OK, f"last resume or boot {woke:%Y-%m-%d %H:%M} IST")
    if clock <= FAIL_AFTER:
        return Check(
            CHECK_NAME, WARN, f"resumed or booted at {clock} IST, after {WARN_AFTER}: late start"
        )
    return Check(
        CHECK_NAME,
        FAIL,
        f"resumed or booted at {clock} IST, after {FAIL_AFTER}: the PC was asleep through the "
        "morning start (keep-awake task or standby timeout)",
    )
