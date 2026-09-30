"""One pipeline from "a bar closed" to "an order goes out", shared by every mode.

Only the strategy varies. Data source, clock and execution sink are injected adapters
(replay, shadow, analyze and live differ only there); everything else - rolling context,
signal mapping, pre-trade gates, idempotency, the per-bar cycle - is shared code. See
docs/PYTHON-RUNNER-SPEC.md.
"""
