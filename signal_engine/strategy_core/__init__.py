"""Strategy logic shared by backtest and live execution.

Each module here is a pure function from a prepared input frame (built from
signal_engine.features on broker data) to signal rows. No I/O, no clock, no globals, so the
same code path can be replayed over history or run on a rolling live window.
"""
