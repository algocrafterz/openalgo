"""Strategy tag constants.

These match the strategy name prefix used in TradingView PineScript alerts
(e.g. "ORB LONG | SYMBOL", "RSI-TP-MR TP1 HIT | SYMBOL").

Update here if a strategy is renamed — normalizer, config.yaml blacklist keys,
and PineScript alert strings must all be updated together.
"""

ORB = "ORB"
RSI_TP_MR = "RSI-TP-MR"
