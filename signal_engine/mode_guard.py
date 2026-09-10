"""Which mode (analyze/live) the engine is in, and the guard against a mid-session flip.

ONE SOURCE OF TRUTH. Three separate 60s-TTL mode caches had grown up independently —
notifier.py's, analysis/breakingtrade/alerts.py's, and (with C1) the listener's — each
answering the same question with its own copy of the same code. This module owns the cache;
notifier delegates to it. The BreakingTrade poller keeps its own because it is a SEPARATE
PROCESS with no shared memory to delegate into.

WHY THE HALT EXISTS

The trading mode used to be resolved exactly once, in startup._run_engine(), and fanned out
to runtime.apply_trade_mode(), db.set_trade_mode() and logger_setup.set_mode(). Nothing ever
re-checked it. If OpenAlgo's mode changed while the engine was running, the state diverged:

  OpenAlgo order routing  -> the real broker
  risk_engine limits      -> still the analyze profile: max_open_positions 0 (unlimited),
                             daily/weekly/monthly loss limits all 1.0 (off)
  risk_engine counters    -> still isolated per strategy, not pooled into one account
  trades.db.trade_mode    -> still "analyze"
  risk.db counter rows    -> still keyed mode="analyze"
  log file                -> still signal_engine_analyze_*.log
  Telegram destination    -> flipped to the -live channels within 60 seconds

That is real money traded with every risk limit disabled and the audit trail labelled paper.

Migrating counters and limits mid-session is not worth the complexity and is not what a
trader wants anyway. The correct response is to STOP TAKING NEW ENTRIES, say so loudly, and
let the operator restart cleanly under the new mode. Existing positions keep their tracker,
their stop-losses and their time exit — a halt must never abandon open risk.
"""

import time

from loguru import logger

#: How long a checked mode is trusted. Same value and same reasoning as the caches this
#: replaces: long enough that a burst of signals doesn't hammer OpenAlgo once per message,
#: short enough that a flip is noticed without an engine restart.
_CACHE_TTL_SECONDS = 60

#: "analyze" until proven otherwise — the lower-stakes assumption for anything that has to
#: pick a destination before OpenAlgo has answered. `checked_at` is a time.monotonic() stamp.
_cache = {"phase": "analyze", "checked_at": 0.0, "known": False}

#: The phase startup resolved and configured everything against. None until set.
_startup_phase: str | None = None

#: Sticky once set: the first reason wins, so the log/alert names the ORIGINAL cause rather
#: than whatever re-tripped it on a later poll.
_halted_reason: str = ""


def reset() -> None:
    """Clear all state. Tests only — the engine never un-halts itself."""
    global _startup_phase, _halted_reason
    _cache.update({"phase": "analyze", "checked_at": 0.0, "known": False})
    _startup_phase = None
    _halted_reason = ""


def prime(phase: str) -> None:
    """Seed the cache from a mode check made elsewhere, without a second API call."""
    _cache.update({"phase": phase, "checked_at": time.monotonic(), "known": True})


def set_startup_phase(phase: str) -> None:
    """Record the phase the engine's limits, counters and log routing were configured for."""
    global _startup_phase
    _startup_phase = phase
    prime(phase)
    logger.info(f"Mode guard armed against startup phase: {phase.upper()}")


def startup_phase() -> str | None:
    return _startup_phase


async def current_phase() -> str:
    """"analyze" or "live", from OpenAlgo's live analyze/live state, cached briefly.

    An unreachable OpenAlgo returns the LAST KNOWN phase rather than a guess. Defaulting to
    "analyze" on every failure would silently route live-money alerts into the paper channel
    the moment the API blipped; keeping the last real answer is both more accurate and more
    stable. With nothing known yet it does fall back to "analyze".
    """
    from signal_engine import api_client

    now = time.monotonic()
    if now - _cache["checked_at"] <= _CACHE_TTL_SECONDS:
        return _cache["phase"]

    mode, is_analyze = await api_client.fetch_trading_mode()
    _cache["checked_at"] = now
    if mode == "unknown":
        return _cache["phase"]
    _cache.update({"phase": "analyze" if is_analyze else "live", "known": True})
    return _cache["phase"]


async def check_for_flip() -> str | None:
    """Compare OpenAlgo's mode against the phase startup configured for.

    Returns the new phase the FIRST time a change is seen and halts new entries; returns
    None otherwise — including when the mode is simply unreadable, which must never be
    mistaken for a change (that would halt the engine on a transient network blip).
    """
    global _halted_reason

    if _startup_phase is None or _halted_reason:
        # Nothing to compare against, or already halted — report the flip once, not on
        # every poll for the rest of the session.
        if _startup_phase is None:
            await current_phase()
        return None

    phase = await current_phase()
    if phase == _startup_phase:
        return None

    halt(
        f"OpenAlgo mode changed from {_startup_phase.upper()} to {phase.upper()} while the "
        f"engine was running. Risk limits, counter isolation, trades.db/risk.db labels and "
        f"log routing are all still configured for {_startup_phase.upper()}. New entries are "
        f"BLOCKED; open positions keep their stop-loss and time exit. Restart the engine to "
        f"resume under {phase.upper()}."
    )
    return phase


def halt(reason: str) -> None:
    """Block new entries. Sticky: the first reason is the one that is kept and reported."""
    global _halted_reason
    if _halted_reason:
        return
    _halted_reason = reason
    logger.critical(f"ENTRIES HALTED: {reason}")


def is_halted() -> bool:
    return bool(_halted_reason)


def halt_reason() -> str:
    return _halted_reason
