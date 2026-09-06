"""Strategy tag constants.

These match the strategy name prefix used in TradingView PineScript alerts
(e.g. "ORB LONG | SYMBOL", "RSI-TP-MR TP1 HIT | SYMBOL", "EMA9 EXIT | SYMBOL").

Update here if a strategy is renamed — normalizer, config.yaml blacklist keys,
and PineScript alert strings must all be updated together.
"""

ORB = "ORB"
RSI_TP_MR = "RSI-TP-MR"
EMA9 = "EMA9"

# BreakingTrade scanner-selected intraday breakouts. Unlike the tags above, these alerts are
# not authored by a PineScript on a chart - they are emitted by
# signal_engine/analysis/breakingtrade, which selects the symbol from the vendor's scanner and
# computes entry/SL/TP from OpenAlgo bars. The alert SHAPE is identical, so the parser,
# validator, risk engine and executor need no special case.
BREAKINGTRADE = "BREAKINGTRADE"
