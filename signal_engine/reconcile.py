"""Daily reconciliation canary: does the engine's record agree with the broker's?

WHY THIS EXISTS

On 2026-09-11 the engine reported +Rs 986.41 for the day. The broker's own per-symbol
figures made it +Rs 119.31. Nothing noticed, because nothing ever compared the two - the
day summary was assembled from the engine's own counters and reported with confidence.

Every individual bug behind that number is fixed. This is the check that catches the NEXT
one: a single number, computed two independent ways, compared, and alerted on when they
disagree. It is deliberately dumb - it does not care WHY they differ, only that they do,
which is what makes it a canary rather than another thing to maintain.

WHAT IS COMPARED

  engine : SUM of closed-trade P&L in trades.db for (mode, day)  - see db.fetch_day_trades
  broker : OpenAlgo's own realised P&L for the same day          - see api_client

A mismatch means one of: a close the engine never recorded, a close recorded twice, P&L
attributed to the wrong position, or the broker closing something the engine did not know
about. All four have happened. None was visible at the time.
"""

from dataclasses import dataclass

from loguru import logger

from signal_engine import db, notifier

#: Rupees of disagreement tolerated before it is called a mismatch. Not zero: the broker
#: rounds, and a paise-level difference between two float sums is arithmetic, not a defect.
#: Anything a trader would notice on a statement is above this.
TOLERANCE_RUPEES = 1.0


@dataclass(frozen=True)
class Reconciliation:
    """The comparison, and whether it holds."""

    engine_pnl: float
    broker_pnl: "float | None"
    trades: int
    mode: str

    @property
    def comparable(self) -> bool:
        """False when the broker figure could not be read - unknown is not a mismatch."""
        return self.broker_pnl is not None

    @property
    def difference(self) -> float:
        return 0.0 if not self.comparable else self.engine_pnl - self.broker_pnl

    @property
    def agrees(self) -> bool:
        return not self.comparable or abs(self.difference) <= TOLERANCE_RUPEES

    @property
    def is_known_sandbox_limitation(self) -> bool:
        """True for a mismatch that is the SANDBOX being wrong, not signal_engine.

        Confirmed 2026-09-21: OpenAlgo's own analyze-mode sandbox undercounts
        `sandbox_positions.today_realized_pnl` for any position with more than one exit leg
        (a TP1/TP2 before the final close) - checked directly against `db/sandbox.db`, not
        inferred. `sandbox/execution_engine.py` and `position_manager.py` are OpenAlgo core,
        maintained by the OpenAlgo project and out of scope for signal_engine to change.

        The same day, `tracker.py`'s book_close() had its OWN separate bug (double-counting
        a position's partial-exit P&L) - that one WAS signal_engine's and is fixed, verified
        against every fill in that day's `sandbox_trades` table. With it fixed, trades.db's
        `total_pnl` is independently correct, so a live-money mismatch is still worth
        escalating hard - but an analyze-mode one no longer means the engine's number is
        wrong. It means the free, no-money sandbox's own bookkeeping is off, which is not
        something a trader needs to be alerted about as if their numbers were unreliable.
        """
        return self.mode == "analyze" and self.comparable and not self.agrees

    def summary(self) -> str:
        if not self.comparable:
            return (
                f"Reconciliation SKIPPED ({self.mode}): the broker's realised P&L could not "
                f"be read. Engine shows {self.engine_pnl:+,.2f} over {self.trades} trade(s); "
                "nothing to compare it against."
            )
        if self.agrees:
            return (
                f"Reconciled ({self.mode}): engine {self.engine_pnl:+,.2f} = broker "
                f"{self.broker_pnl:+,.2f} over {self.trades} trade(s)."
            )
        return (
            f"RECONCILIATION MISMATCH ({self.mode})\n"
            f"Engine (trades.db): {self.engine_pnl:+,.2f} over {self.trades} trade(s)\n"
            f"Broker (OpenAlgo) : {self.broker_pnl:+,.2f}\n"
            f"Difference        : {self.difference:+,.2f}\n"
            "The day's numbers are NOT trustworthy. One of: a close the engine never "
            "recorded, a close recorded twice, P&L attributed to the wrong position, or the "
            "broker closed something the engine did not know about."
        )


async def reconcile_day(mode: str = None, day: str = None) -> Reconciliation:
    """Compare the engine's day against the broker's. Never raises."""
    from signal_engine.api_client import fetch_realised_pnl

    mode = mode or db._TRADE_MODE
    try:
        trades = db.fetch_day_trades(mode, day)
        engine_pnl = sum(t["total_pnl"] for t in trades)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Reconciliation: could not read trades.db: {e}")
        return Reconciliation(0.0, None, 0, mode)

    try:
        broker_pnl = await fetch_realised_pnl()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Reconciliation: could not read the broker's realised P&L: {e}")
        broker_pnl = None

    return Reconciliation(engine_pnl, broker_pnl, len(trades), mode)


async def check_and_alert(mode: str = None, day: str = None) -> Reconciliation:
    """Run the comparison and escalate a mismatch. Returns the result either way.

    A LIVE-money mismatch is CRITICAL and always reaches Telegram - it is never filtered by
    notify_level, because it means every other number that day is suspect.

    An ANALYZE-mode mismatch is logged (still visible in errors_<day>.jsonl for anyone
    auditing) but does not escalate to Telegram as a CRITICAL "day's numbers are NOT
    trustworthy" claim - see Reconciliation.is_known_sandbox_limitation for why: that
    framing is no longer true once the engine's own P&L bug is fixed (2026-09-21), and
    crying wolf about a sandbox-side limitation the trader cannot act on just trains them to
    ignore this channel.
    """
    result = await reconcile_day(mode, day)
    if result.agrees:
        logger.info(result.summary())
        return result

    if result.is_known_sandbox_limitation:
        logger.warning(
            result.summary().replace("\n", " | ")
            + " | This is a known OpenAlgo sandbox limitation (undercounts multi-leg closes), "
            "not a signal_engine defect - trades.db's own total_pnl is the reliable figure. "
            "Not escalated to Telegram."
        )
        return result

    logger.critical(result.summary().replace("\n", " | "))
    try:
        await notifier.notify_event("reconciliation_mismatch", result.summary())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Reconciliation: could not send the mismatch alert: {e}")
    return result
