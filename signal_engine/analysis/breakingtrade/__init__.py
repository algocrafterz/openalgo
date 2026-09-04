"""Extractor and scorer for breakingtrade.com Market Profile / volume scanner exports.

breakingtrade.com has no API (see signal_engine/pinescripts/intraday/breaking-trade/README.md),
so the only way to get its cross-sectional view of the F&O universe is a manual "save page as
Excel" snapshot. This package turns that snapshot into structured data and a ranked watchlist.

It is a SELECTION aid, not a signal source: the output is a list of symbols worth watching or
setting alerts on, evaluated by a human, not something wired to auto-fire orders.
"""
