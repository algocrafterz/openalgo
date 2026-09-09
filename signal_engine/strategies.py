"""Strategy tag constants.

These match the strategy name prefix used in TradingView PineScript alerts
(e.g. "ORB LONG | SYMBOL", "RSI-TP-MR TP1 HIT | SYMBOL", "EMA9 EXIT | SYMBOL").

Update here if a strategy is renamed — normalizer, config.yaml blacklist keys,
and PineScript alert strings must all be updated together.
"""

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
