"""Reusable, strategy-agnostic inputs shared by backtest and live signal generation.

Each module is a pure function over an IST-indexed OHLCV frame (columns Open, High, Low,
Close, Volume) and returns Series or DataFrames aligned to that index. Every function is
causal: the value at bar k depends only on bars <= k, so
`f(df[:k])` equals `f(df).iloc[:k]`. That property is tested for each input.

Modules port the named breakout.pine functions faithfully, quirks included, so a Python
strategy sees the same numbers the live Pine script does.
"""
