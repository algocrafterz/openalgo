"""Per-channel "what is this, when does it run, what do I do" reference card, pinned to each
strategy's own Telegram channel.

WHY A PINNED MESSAGE, NOT A DOC LINK

The trader's own ask was "so I don't have to prompt/look at documentation all the time" -
Telegram's pin feature puts the answer at the top of the exact channel it applies to, visible
without leaving the app or remembering a filename. A doc lives in the repo; a pin lives where
the question actually comes up.

WHY RE-SENT AND RE-PINNED ON EVERY STARTUP RATHER THAN ONCE

The engine restarts routinely (daily, and after every code change), and that restart is already
the natural point where "did the strategy's behaviour change" is freshest. Re-pinning here means
the card can never silently drift out of date the way a doc page can - if CARDS below wasn't
updated to match a real change, the NEXT restart re-displays the stale text, which is a visible
prompt to go fix CARDS, not a silent gap.

HOW TO KEEP THIS CURRENT

Whenever a change to a strategy alters what it does, when it runs, or how a trader should react
to its signals, update that strategy's entry in CARDS below in the SAME change - this is the
Telegram-facing equivalent of the STRATEGY-LOG.md entry CLAUDE.md already requires for
BreakingTrade strategy-logic changes, and should be treated as part of that same discipline for
every strategy, not just BreakingTrade.
"""

from __future__ import annotations

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
            "WATCHLIST messages ('no action, not a trade signal') need nothing from you. "
            "A BREAKINGTRADE LONG/SHORT message with Entry/SL/TP is a real trade - the engine "
            "acts on it automatically, same as ORB/BREAKOUT. The system now keeps re-checking "
            "a flagged stock for up to 2 hours after it's first flagged, so a watchlist entry "
            "can still turn into a real trade signal well after you first saw it."
        ),
        updated="2026-09-09",
    ),
}


def render(card: StrategyCard) -> str:
    """Plain-text card, no icons/emoji (project convention) - Telegram renders bold/monospace
    from Markdown, so structure comes from that, not symbols."""
    return (
        f"**{card.title}**\n"
        f"What: {card.what}\n"
        f"When: {card.when}\n"
        f"Action: {card.action}\n"
        f"(reference card, last reviewed {card.updated} - not a trade signal)"
    )


async def send_and_pin_cards(client, channels) -> int:
    """Send (or refresh) each channel's reference card and pin it. Best-effort per channel -
    one failure (e.g. bot lacks pin rights in that channel) must not block the others or the
    engine's own startup.

    Returns how many cards were successfully pinned.
    """
    from loguru import logger

    pinned = 0
    for ch in channels:
        card = CARDS.get(ch.name)
        if card is None:
            continue
        try:
            message = await client.send_message(ch.id, render(card), parse_mode="markdown")
            await client.pin_message(ch.id, message, notify=False)
            pinned += 1
        except Exception as e:
            logger.warning(f"Strategy card: could not pin card for {ch.name}: {e}")
    return pinned
