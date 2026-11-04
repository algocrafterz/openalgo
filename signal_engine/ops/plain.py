"""Plain words for every ops check: what it means for a trader, in one sentence.

The check's own name and evidence stay technical (they are what someone fixing it needs); this
is the line the /logs/workflow page and the reports lead with. Keyed by the start of the check
name, longest match first, as (when it is fine, when it needs a look).
"""

from __future__ import annotations

_PLAIN: dict[str, tuple[str, str]] = {
    # open-day
    "services": (
        "OpenAlgo and the trading engine are running.",
        "OpenAlgo or the trading engine is not running.",
    ),
    "OpenAlgo health": ("OpenAlgo is answering normally.", "OpenAlgo is not answering."),
    "OpenAlgo health warning": (
        "OpenAlgo is answering normally.",
        "OpenAlgo is answering, but its own health monitor shows a warning (often many threads in use).",
    ),
    "broker session": (
        "Logged in to the broker and the account balance is visible.",
        "Not logged in to the broker, or the account balance could not be read.",
    ),
    "trading mode": (
        "Sandbox mode: no real money is used.",
        "LIVE mode: orders today use real money.",
    ),
    "engine startup": (
        "The engine started and loaded every stock it trades.",
        "The engine's own start-up checks reported a problem.",
    ),
    "BreakingTrade poller": (
        "The BreakingTrade scanner started.",
        "The BreakingTrade scanner did not start.",
    ),
    "websocket proxy": ("The live price feed is up.", "The live price feed is down."),
    "ZeroMQ bus": ("The internal price channel is up.", "The internal price channel is down."),
    "clock": (
        "The PC clock is correct, so broker login codes work.",
        "The PC clock is off; broker login codes may be rejected.",
    ),
    "disk": ("There is enough disk space.", "The disk is nearly full."),
    "trades.db": ("The trade records can be read.", "The trade records could not be read."),
    "risk counters": (
        "Daily risk limits start fresh.",
        "The daily risk counters already show trades before the open.",
    ),
    "errors since the last close": (
        "No errors since yesterday's close.",
        "Errors were logged since yesterday's close (most are usually broker connection drops).",
    ),
    "close-day ": (
        "Yesterday's end-of-day job finished cleanly.",
        "Yesterday's end-of-day job had a failed step or did not run.",
    ),
    "task ": (
        "The Windows task {subject} ran.",
        "The Windows task {subject} did not run or failed.",
    ),
    "Windows Task Scheduler": (
        "Windows tasks were checked.",
        "Windows tasks could not be checked.",
    ),
    # liveness
    "first runner cycle": (
        "The engine checked every stock after the first 5-minute candle.",
        "The engine's first check after the open was late or missing.",
    ),
    "BreakingTrade first scan": (
        "The scanner's first scan worked.",
        "The scanner's first scan failed or is missing.",
    ),
    "timeouts / 429": (
        "No burst of slow or refused broker calls after the open.",
        "Many broker calls were slow or refused after the open.",
    ),
    "retry loops": (
        "Nothing had to be retried over and over.",
        "Orders or data requests were retried repeatedly.",
    ),
    "stale prices": ("Prices kept updating.", "Some prices stopped updating for a while."),
    "signals reaching": ("Signals are reaching the engine.", "No signals have reached the engine."),
    # close-day: jobs
    "crontab": (
        "All three daily checkpoints are scheduled.",
        "A daily checkpoint is missing from the schedule.",
    ),
    "phase ": (
        "The {subject} checkpoint ran cleanly.",
        "The {subject} checkpoint had a failed step or did not run.",
    ),
    # close-day: value areas
    "rows added": (
        "Today's value-area levels were saved.",
        "Some stocks' value-area levels could not be saved.",
    ),
    "coverage": (
        "Every stock that traded has today's value-area levels.",
        "Some stocks that traded are missing today's value-area levels.",
    ),
    "dataset": ("The value-area history is complete.", "The value-area history has a gap."),
    # close-day: logs
    "NEW": ("", "A new kind of error appeared today."),
    "RISING": ("", "An error happened far more often than usual today."),
    "recurring": (
        "A known error that happens most days; nothing new.",
        "A known error, more often than usual.",
    ),
    "errors": ("No errors today.", "Errors were logged today."),
}


_PHASE_NAMES = {
    "open-day": "before-the-open",
    "liveness": "after-the-open",
    "close-day": "end-of-day",
}


def plain_for(name: str, status: str) -> str:
    """The plain sentence for a check, or "" when the name is not known."""
    if status == "NOT VERIFIED":
        return "Could not be checked this time."
    for key in sorted(_PLAIN, key=len, reverse=True):
        if name.startswith(key):
            fine, needs_look = _PLAIN[key]
            text = (fine if status == "OK" else needs_look) or needs_look
            subject = name[len(key) :].strip()
            return text.format(subject=_PHASE_NAMES.get(subject, subject))
    return ""
