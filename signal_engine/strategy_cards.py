"""Per-channel "what is this, when does it run, what do I do" reference card, pinned to each
strategy's own Telegram channel.

WHY A PINNED MESSAGE, NOT A DOC LINK

The trader's own ask was "so I don't have to prompt/look at documentation all the time" -
Telegram's pin feature puts the answer at the top of the exact channel it applies to, visible
without leaving the app or remembering a filename. A doc lives in the repo; a pin lives where
the question actually comes up.

WHY PINNED ONCE, NOT RE-SENT ON EVERY START/RESTART/STOP

The engine restarts routinely (daily, and after every code change), and an earlier version of
this module re-sent and re-pinned every card on every one of those restarts. That meant a new
pinned message - and a channel-clutter entry Telegram never lets you fully clean up - for a
restart that changed nothing about the strategy, which trained the reader to stop looking at
pin notifications at all. What actually needs to reach the channel is a CONTENT change, not a
process lifecycle event, so `send_and_pin_cards` now hashes each rendered card and persists the
hash of whatever it last successfully pinned (`signal_engine/data/strategy_card_state.json`).
A channel is touched again only when that hash differs - first ever pin, or CARDS below was
actually edited - and the previous pinned message is unpinned at the same time so exactly one
card stays pinned per channel, never a growing stack of stale ones.

HOW TO KEEP THIS CURRENT

Whenever a change to a strategy alters what it does, when it runs, or how a trader should react
to its signals, update that strategy's entry in CARDS below in the SAME change - this is the
Telegram-facing equivalent of the STRATEGY-LOG.md entry CLAUDE.md already requires for
BreakingTrade strategy-logic changes, and should be treated as part of that same discipline for
every strategy, not just BreakingTrade. Editing CARDS is what makes the next restart re-pin -
skip it, and the channel keeps showing the OLD, now-inaccurate card indefinitely instead of a
silent gap.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyCard:
    title: str
    what: str
    when: str
    action: str
    updated: str  # YYYY-MM-DD of the last time this text was reviewed against the real strategy


CARDS: dict[str, StrategyCard] = {
    "intraday-orb": StrategyCard(
        title="ORB - Opening Range Breakout",
        what=(
            "PineScript strategy on TradingView. Trades a breakout of the first hour's "
            "trading range (09:15-10:15) on a fixed NSE F&O universe."
        ),
        when="Signals only during 09:15-15:30 IST, NSE trading days.",
        action=(
            "Entry/SL/TP alerts here are ALREADY ACTED ON by the engine automatically "
            "(sizing, order, stop-loss, staged exits) - nothing for you to do but watch. "
            "A TP/SL HIT alert reports what the engine already did, not something to act on "
            "yourself."
        ),
        updated="2026-09-09",
    ),
    "intraday-breakout": StrategyCard(
        title="BREAKOUT - Key-Level Breakout",
        what=(
            "PineScript strategy on TradingView. Same breakout mechanics as ORB, extended so "
            "the Opening Range is one key level among several (VAH/VAL/IBH/IBL)."
        ),
        when="Signals only during 09:15-15:30 IST, NSE trading days.",
        action=(
            "Same as ORB: entry/SL/TP alerts are already acted on automatically by the "
            "engine. Watch, don't act manually."
        ),
        updated="2026-09-09",
    ),
    "intraday-breakingtrade": StrategyCard(
        title="BREAKINGTRADE - Scanner-Selected Breakouts",
        what=(
            "A Python scanner (not PineScript) reads breakingtrade.com's Market Profile "
            "scans across 200+ NSE F&O names and proposes trades from whichever names show "
            "real structure, not a fixed watchlist."
        ),
        when=(
            "Scans 09:20-10:30 (every 5 min), 10:30-13:00 (every 15 min), then OFF "
            "13:00-14:50 by design (lunch trap - no scanning). BTST read at 14:50/15:05/15:10."
        ),
        action=(
            "BREAKINGTRADE WATCHLIST messages ('no action, not a trade signal') need nothing "
            "from you. A BREAKINGTRADE LONG/SHORT message with Entry/SL/TP is a real trade - "
            "the engine acts on it automatically, same as ORB/BREAKOUT. This is the CONFIRMED "
            "outcome: the system keeps re-checking a flagged stock for up to 2 hours after "
            "it's first flagged, so a watchlist entry can still turn into a real trade signal "
            "well after you first saw it. See intraday-breakingtrade-watchlist for the other "
            "outcome of the same scanner."
        ),
        updated="2026-09-10",
    ),
    "intraday-breakingtrade-watchlist": StrategyCard(
        title="BREAKINGTRADE-WATCHLIST - Immediate Scanner Entries",
        what=(
            "The SAME Python scanner as intraday-breakingtrade, run side by side as a second, "
            "separately tracked outcome: this one trades the scanner's own watchlist call the "
            "moment it fires, at that price, with no confirming-close wait. The two channels "
            "exist so paper trading can measure which entry style (if either) is worth taking "
            "live, on separate risk slots so one never throttles the other."
        ),
        when=(
            "Scans 09:20-10:30 (every 5 min), 10:30-13:00 (every 15 min), then OFF "
            "13:00-14:50 by design (lunch trap - no scanning)."
        ),
        action=(
            "A BREAKINGTRADE-WATCHLIST LONG/SHORT message with Entry/SL/TP is a real trade - "
            "the engine acts on it automatically, same as every other strategy. This is the "
            "WATCHLIST outcome: entry is the scan-hit price itself, nothing to confirm and "
            "nothing for you to do but watch. Zero scored samples yet - see this strategy's "
            "STRATEGY-LOG.md for when that changes."
        ),
        updated="2026-09-10",
    ),
    "intraday-breakingtrade-btst": StrategyCard(
        title="BREAKINGTRADE-BTST - Buy Today, Sell Tomorrow",
        what=(
            "The SAME Python scanner as intraday-breakingtrade, read for an overnight carry "
            "candidate instead of an intraday one: names with strong delivery volume and a "
            "trending Market Profile day type near the close."
        ),
        when="Read at 14:50, 15:05, and 15:10 IST only - never during the intraday session.",
        action=(
            "This is NOT auto-traded - the engine never acts on this channel. A BTST message "
            "is a manual call: BUY CNC yourself before 15:15 if you're taking it, using the "
            "stated disaster stop. Silence (or a 'no candidates' message) means skip the day."
        ),
        updated="2026-09-11",
    ),
}


def _base_name(channel_name: str) -> str:
    """Strip a channel's "-analyze"/"-live" phase suffix to find its CARDS key - both phases
    of a strategy show the identical reference card (what/when/action don't change with mode),
    just with a different safety line from render() below."""
    for suffix in ("-analyze", "-live"):
        if channel_name.endswith(suffix):
            return channel_name[: -len(suffix)]
    return channel_name


def _phase(channel_name: str) -> str | None:
    if channel_name.endswith("-analyze"):
        return "analyze"
    if channel_name.endswith("-live"):
        return "live"
    return None


#: Appended to every card, keyed by which physical channel it is pinned in. This is the second,
#: redundant confirmation of a fact the channel split already makes structurally true (separate
#: channels for separate money) - worth stating anyway, since it is the one line a trader glances
#: at before reacting to whatever is pinned below it.
_PHASE_LINE = {
    "analyze": "PAPER CHANNEL - every signal here is simulated. No real order is ever placed.",
    "live": "LIVE CHANNEL - every signal here places a REAL order with real money.",
}


def render(card: StrategyCard, phase: str | None = None) -> str:
    """Plain-text card, no icons/emoji (project convention) - Telegram renders bold/monospace
    from Markdown, so structure comes from that, not symbols."""
    phase_line = f"{_PHASE_LINE[phase]}\n" if phase in _PHASE_LINE else ""
    return (
        f"{phase_line}"
        f"**{card.title}**\n"
        f"What: {card.what}\n"
        f"When: {card.when}\n"
        f"Action: {card.action}\n"
        f"(reference card, last reviewed {card.updated} - not a trade signal)"
    )


#: Where the hash of the last successfully pinned card, per channel, is persisted - see the
#: module docstring's "WHY PINNED ONCE" section. Not trading data, so a plain JSON file rather
#: than one of the sqlite databases; tests monkeypatch this path to keep runs isolated.
_STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "strategy_card_state.json")


def _load_state() -> dict:
    """Channel name -> {"hash", "message_id"} of the last card successfully pinned there.

    Never raises - a missing, first-run, or corrupted state file must fall back to "nothing
    pinned yet" (every card gets (re)pinned) rather than blocking the engine's startup.
    """
    try:
        with open(_STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    """Best-effort persist - a failed write means the NEXT restart re-pins unnecessarily, which
    is a cosmetic regression to the old always-re-pin behaviour, not a reason to fail startup."""
    from loguru import logger

    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        with open(_STATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError as e:
        logger.warning(f"Strategy card: could not persist pin state: {e}")


def _content_hash(card: StrategyCard, phase: str | None = None) -> str:
    return hashlib.sha256(render(card, phase).encode()).hexdigest()


async def send_and_pin_cards(client, channels) -> int:
    """Send and pin each channel's reference card ONLY if its content is new or has changed
    since the last successful pin - see the module docstring's "WHY PINNED ONCE" section.
    Best-effort per channel - one failure (e.g. bot lacks pin rights in that channel) must not
    block the others or the engine's own startup.

    Returns how many cards were freshly (re)pinned this call - 0 on an ordinary restart where
    nothing changed.
    """
    from loguru import logger

    state = _load_state()
    state_changed = False
    pinned = 0
    for ch in channels:
        card = CARDS.get(_base_name(ch.name))
        if card is None:
            continue

        phase = _phase(ch.name)
        content_hash = _content_hash(card, phase)
        previous = state.get(ch.name)
        if previous and previous.get("hash") == content_hash:
            continue  # already pinned, content unchanged - nothing to do

        try:
            message = await client.send_message(ch.id, render(card, phase), parse_mode="markdown")
            await client.pin_message(ch.id, message, notify=False)
            if previous and previous.get("message_id"):
                # Best-effort: an old pin left behind if this fails is cosmetic clutter, not
                # worth losing the new pin over.
                try:
                    await client.unpin_message(ch.id, previous["message_id"])
                except Exception as e:
                    logger.debug(f"Strategy card: could not unpin stale card for {ch.name}: {e}")
            state[ch.name] = {"hash": content_hash, "message_id": getattr(message, "id", None)}
            state_changed = True
            pinned += 1
        except Exception as e:
            logger.warning(f"Strategy card: could not pin card for {ch.name}: {e}")

    if state_changed:
        _save_state(state)
    return pinned
