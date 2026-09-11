"""Strategy tags, and the one registry that says what each strategy is wired to.

The tags match the strategy name prefix in the alert header (e.g. "ORB LONG | SYMBOL",
"RSI-TP-MR TP1 HIT | SYMBOL"). Rename one here and the normalizer, config.yaml blacklist
keys and the PineScript alert strings must all move with it.

WHY THE REGISTRY EXISTS (2026-09-11)

Adding a strategy used to mean editing SIX hand-maintained places: config.yaml's
`telegram.channels`, `strategy_profiles` and `blacklist`, plus notifier's
_STRATEGY_CHANNEL_BASE, strategy_cards' CARDS and (for a Python strategy) alerts'
_CHANNEL_NAME_BY_GROUP. EMA9 and EMA9VWAP had the first three and none of the last three -
so no part of the running system knew they existed, and nothing in the code recorded whether
that was deliberate. It is (both are measured, not traded), but that was indistinguishable
from an oversight, which is exactly the failure mode `enabled: false` was introduced to end
for channels.

REGISTRY is now the single statement of what a strategy IS. `channel_base` of None means
"deliberately has no engine channel" - the strategy runs for measurement only and its alerts
have nowhere to be traded from. config.validate_channels() and startup's config check are
what turn a gap here into a visible startup message instead of silence.
"""

from dataclasses import dataclass

ORB = "ORB"
RSI_TP_MR = "RSI-TP-MR"
EMA9 = "EMA9"

# Plain 9 EMA / VWAP crossover. Measured as NOT tradeable (gross -0.41 bps out-of-sample
# against a ~10 bps cost line, 11,815 trades); the tag exists so the script can paper-trade
# through the real pipeline and log its own signals for comparison against the backtest.
# See signal_engine/pinescripts/intraday/ema9-vwap/STRATEGY-LOG.md before enabling it.
EMA9_VWAP = "EMA9VWAP"

# BreakingTrade scanner-selected intraday breakouts. Unlike the tags above, these alerts are
# not authored by a PineScript on a chart - they are emitted by
# signal_engine/analysis/breakingtrade, which selects the symbol from the vendor's scanner and
# computes entry/SL/TP from OpenAlgo bars. The alert SHAPE is identical, so the parser,
# validator, risk engine and executor need no special case.
BREAKINGTRADE = "BREAKINGTRADE"

BREAKOUT = "BREAKOUT"

# WATCHLIST outcome of the BreakingTrade scanner: entered at the scan-hit price with no
# confirming close (trigger.plan_trade_watchlist), against BREAKINGTRADE's confirmed entry.
# Its own tag, channel and risk slots so paper P&L can compare the two entry philosophies.
BREAKINGTRADE_WATCHLIST = "BREAKINGTRADE-WATCHLIST"


@dataclass(frozen=True)
class StrategyMeta:
    """What one strategy is wired to.

    channel_base: the config.yaml `telegram.channels` name WITHOUT its -analyze/-live
        suffix, and the strategy_cards.CARDS key. None means the strategy deliberately has
        no engine channel - see the module docstring.
    tradeable: whether this strategy is intended to reach a broker at all. False is a
        statement in the file, not an absence from it.
    note: why, in one line, for anything a reader would otherwise have to go looking for.
    """

    tag: str
    channel_base: str | None
    tradeable: bool
    note: str = ""


REGISTRY: dict[str, StrategyMeta] = {
    ORB: StrategyMeta(ORB, "intraday-orb", True),
    BREAKOUT: StrategyMeta(BREAKOUT, "intraday-breakout", True),
    BREAKINGTRADE: StrategyMeta(BREAKINGTRADE, "intraday-breakingtrade", True),
    BREAKINGTRADE_WATCHLIST: StrategyMeta(
        BREAKINGTRADE_WATCHLIST, "intraday-breakingtrade-watchlist", True
    ),
    RSI_TP_MR: StrategyMeta(
        RSI_TP_MR, None, False,
        "Swing mean reversion (2-7 day CNC). Has a strategy_profiles entry but no engine "
        "channel - not part of the current intraday paper phase.",
    ),
    EMA9: StrategyMeta(
        EMA9, None, False,
        "No forward history yet and no channel. Measured through the backtest only.",
    ),
    EMA9_VWAP: StrategyMeta(
        EMA9_VWAP, None, False,
        "Measured NOT tradeable: gross -0.41 bps out-of-sample against a ~10 bps cost line "
        "over 11,815 trades. Deliberately has no channel - see its STRATEGY-LOG.md.",
    ),
}


def channel_base(tag: str) -> str | None:
    """The config.yaml channel base name for a strategy tag, or None.

    None covers both "registered, deliberately has no channel" and "unknown tag" - callers
    treat the two the same (no per-strategy routing), and validate_channels() is what
    distinguishes them at startup.
    """
    meta = REGISTRY.get((tag or "").strip().upper())
    return meta.channel_base if meta else None


def channelled() -> dict:
    """tag -> channel_base for every strategy that has an engine channel."""
    return {tag: m.channel_base for tag, m in REGISTRY.items() if m.channel_base}
