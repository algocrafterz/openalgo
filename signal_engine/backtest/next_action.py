"""Next action for a finished run, looked up from a rule table. Pure, no I/O.

The table is data: add a gate to `evaluation._GATES` and a test forces a rule here.

Most rules read the same whether the gate failed or was never evaluated. The gates in
`NOT_RUN_RULES` are evaluated by an opt-in `record_run` stage or an attestation, so when one was
not evaluated the useful step is to attach that evidence. The gates in `FAILED_RULES` have already
run, so telling the user to run them again would be wrong. A test checks that every flag named
here exists in `record_run`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from signal_engine.backtest.promotion import DIAGNOSTIC_GATES, READY, STAGES, evaluation_rows

DIRTY_ACTION = "Commit the working tree and re-run from a clean state"
ADVANCE_ACTION = "Advance to the next stage"


def _r(action: str, reason: str) -> dict[str, str]:
    return {"action": action, "reason": reason}


#: Dimension defaults, used when a gate has no rule of its own.
DIMENSION_DEFAULTS: dict[str, dict[str, str]] = {
    "integrity": _r(
        "Fix the data or logic integrity check", "Results are only as good as the inputs"
    ),
    "edge": _r("Improve the strategy's net edge", "The trades do not earn enough after costs"),
    "cost_sensitivity": _r(
        "Re-test with a realistic cost model", "Costs decide whether the edge survives"
    ),
    "statistical_confidence": _r("Collect more evidence", "The result may be luck"),
    "stability": _r(
        "Run out-of-sample validation", "The edge must hold on data it was not tuned on"
    ),
    "parameter_sensitivity": _r(
        "Run a parameter sweep", "A good result must not depend on exact settings"
    ),
    "robustness": _r("Run the robustness re-test", "The edge must survive small changes"),
    "regime_robustness": _r(
        "Check results across market regimes", "The edge must not belong to one period"
    ),
    "risk": _r("Run the risk checks", "Losses must stay inside what can be survived"),
    "deployment": _r("Complete the deployment checks", "The live set-up must match the tested one"),
    "complexity": _r("Review strategy complexity", "More parameters raise the overfitting risk"),
}

RULES: dict[str, dict[str, str]] = {
    "G0.1": _r(
        "Verify that backtest and live share one code path",
        "Divergent logic makes results unreliable",
    ),
    "G0.2": _r(
        "Add truncation tests proving no input looks ahead", "Look-ahead silently inflates results"
    ),
    "G0.3": _r(
        "Add a timestamp test for every data entry point",
        "A time-zone slip invalidates every signal",
    ),
    "G0.4": _r("Check data coverage for the tested universe", "Missing bars distort trades"),
    "G0.5": _r(
        "Review logs for silently excluded symbols or days", "Hidden exclusions bias results"
    ),
    "G0.6": _r("Test the closing auction handling", "Late-session prices behave differently"),
    "G0.7": _r("Raise test coverage on the strategy code", "Untested code cannot be trusted"),
    "G0.8": _r("Check corporate action handling", "Splits and bonuses create false price moves"),
    "G1.1": _r("Record a cost model for the run", "Without costs the result is not net"),
    "G1.2": _r(
        "Improve net expectancy at normal costs", "The strategy must earn money after costs"
    ),
    "G1.3": _r("Re-run with stressed slippage", "The edge must survive worse fills"),
    "G1.4": _r("Compare gross edge to cost", "The edge must be large relative to costs"),
    "G1.5": _r(
        "Check cost per trade against the risk taken", "Costs must be small next to the risk"
    ),
    "G1.6": _r("Set a minimum stop width", "Tight stops let costs dominate"),
    "G2.1": _r(
        "Run on more symbols or a longer period to get enough trades", "Too few trades to judge"
    ),
    "G2.2": _r(
        "Compute the day-clustered t-statistic",
        "The edge must be statistically distinguishable from luck",
    ),
    "G2.3": _r("Compute the deflated Sharpe ratio", "Many trials inflate the best result"),
    "G2.4": _r(
        "Run a parameter grid to estimate overfitting probability", "Tuned results often fail live"
    ),
    "G2.5": _r(
        "Record the number of trials tried", "The trial count is needed to discount the result"
    ),
    "G2.6": _r("Test for dependence between trades", "Correlated trades overstate confidence"),
    "G2.7": _r(
        "Record a daily P&L series long enough to judge", "A short track record proves little"
    ),
    "G3.1": _r("Run out-of-sample validation", "The edge must hold on data it was not tuned on"),
    "G3.2": _r("Run out-of-sample validation", "The edge must hold on data it was not tuned on"),
    "G3.3": _r("Run walk-forward validation", "The edge must hold in successive windows"),
    "G3.4": _r("Run a symbol hold-out test", "The edge must hold on symbols not used for tuning"),
    "G3.5": _r(
        "Add an embargo between train and test data", "Leakage across the split inflates results"
    ),
    "G4.1": _r("Run a parameter sweep", "A good result must not depend on exact settings"),
    "G4.2": _r("Re-run with delayed entries", "Live orders are never instant"),
    "G4.3": _r("Re-run with pessimistic same-bar fills", "Bar data hides intrabar order"),
    "G4.4": _r("Check results across sub-periods", "The edge must not belong to one period"),
    "G4.5": _r(
        "Check that profit is spread across symbols", "A few symbols must not carry the result"
    ),
    "G4.6": _r("Compare long and short results", "One side should not carry the result"),
    "G4.7": _r("Check survivorship in the tested universe", "Today's list hides past failures"),
    "G4.8": _r("Review results around event days", "Events can create one-off profits or losses"),
    "G5.1": _r(
        "Run a Monte Carlo drawdown analysis", "The worst case matters more than the average"
    ),
    "G5.2": _r(
        "Re-run with the live position caps applied", "Live limits change which trades happen"
    ),
    "G5.3": _r("Verify position sizing against the live rules", "Sizing decides real risk"),
    "G5.4": _r("Check order size against bar volume", "Large orders move the price"),
    "G5.5": _r(
        "Model margin for simultaneous positions", "Margin limits how many trades can be open"
    ),
    "G6.1": _r(
        "Check the session window and square-off times",
        "Positions must close before the broker does it",
    ),
    "G6.2": _r(
        "Check daily eligibility of the live universe", "Some symbols cannot be traded on some days"
    ),
    "G6.3": _r("Check price band circuits", "Circuit limits can trap an exit"),
    "G6.4": _r(
        "Record the run again so its stop triggers are checked against the dated tick tiers",
        "Orders at invalid prices are rejected; broker acceptance is a separate pre-live check",
    ),
    "G6.5": _r("Test gap-through stop behaviour", "Stops can fill far from their price"),
    "G6.6": _r(
        "Confirm compliance with the SEBI retail algo framework", "Non-compliant algos cannot run"
    ),
    "G6.7": _r("Update the charges to current rates", "Stale charges misstate net results"),
    "G6.8": _r("Define the closing auction exit plan", "Auction fills differ from normal fills"),
    "G6.9": _r(
        "Make the backtest respect the square-off time", "Trades held past square-off are not real"
    ),
    "G7.1": _r(
        "Run paper trading for the required duration",
        "Live behaviour must be seen before real money",
    ),
    "G7.2": _r("Compare paper signals with backtest signals", "Signals must match between modes"),
    "G7.3": _r("Measure paper execution quality", "Real fills differ from simulated ones"),
    "G7.4": _r("Track paper results against the backtest", "Divergence signals a problem"),
    "G7.5": _r("Plan the capital ramp-up", "Capital should rise only as evidence grows"),
    "G8.1": _r("Set up and test the kill switch", "A runaway strategy must be stoppable"),
    "G8.2": _r("Enable pre-trade risk controls", "Bad orders must be blocked before they leave"),
    "G8.3": _r("Test recovery after a crash or restart", "State must survive failures"),
    "G8.4": _r("Verify clock synchronisation", "Drift breaks time-based rules"),
    "G8.5": _r("Verify broker session handling", "Expired sessions must not drop orders"),
    "G8.6": _r("Enable the audit trail", "Every order must be traceable"),
    "G8.7": _r("Put change control in place", "Untracked changes break the tested set-up"),
    "G8.8": _r("Set up alert escalation", "Problems must reach a person quickly"),
    "G9.1": _r("Start the live decay watch", "Edges fade and must be monitored"),
    "G9.2": _r("Start the live drawdown watch", "Losses beyond plan require action"),
    "G9.3": _r("Schedule re-validation", "Old validation goes stale"),
    "G9.4": _r("Define retirement criteria", "A failing strategy needs a stop rule"),
    "X.1": _r("Record the strategy parameter count", "More parameters raise the overfitting risk"),
    "X.2": _r(
        "Supply a regime block and check dispersion", "Results should not depend on one regime"
    ),
    "X.3": _r("State and test the regime activation hypothesis", "Regime filters need a reason"),
    "X.4": _r("Record the matched random-entry baseline", "X.4 needs it; it never blocks"),
}


#: The gate has not been evaluated and a specific stage or attestation evaluates it.
NOT_RUN_RULES: dict[str, dict[str, str]] = {
    "G0.1": _r(
        "Record an attestation of the shared-code review for this strategy at the current commit",
        "Without a valid attestation in the ledger the same-logic gate cannot be evaluated",
    ),
    "G0.7": _r(
        "Record pytest coverage for features and strategy_core in the attestation ledger",
        "Without a valid attestation the test-quality gate cannot be evaluated",
    ),
    "G4.1": _r(
        "Re-record the run with --perturbation",
        "The parameter neighbourhood is only tested when this stage is run",
    ),
    "G4.2": _r(
        "Re-record the run with --latency",
        "The delayed-entry replay is only run when this stage is requested",
    ),
    "G5.1": _r(
        "Re-record the run with --monte-carlo",
        "The drawdown and ruin bootstrap is only run when this stage is requested",
    ),
    "G5.2": _r(
        "Re-record the run with --live-caps",
        "The live position and loss limits are only applied when this stage is requested",
    ),
    "G5.4": _r(
        "Re-record the run with --capacity",
        "Order size against bar volume is only measured when this stage is requested",
    ),
    "G5.5": _r(
        "Re-record the run with --live-caps so it records the margin figures",
        "Peak margin comes from the live-caps stage",
    ),
}

#: The gate ran and failed. Running it again would not help; the strategy or its sizing must change.
FAILED_RULES: dict[str, dict[str, str]] = {
    "G4.1": _r(
        "Review the parameter perturbation result",
        "The base run was not positive or too many neighbouring settings lost the edge",
    ),
    "G5.1": _r(
        "Reduce risk per trade or the strategy's drawdown",
        "The simulated drawdown or the chance of ruin is above the limit",
    ),
    "G5.2": _r(
        "Review the result with the live limits applied",
        "With the live position and loss limits the book fails the expectancy or trade-count test",
    ),
    "G5.3": _r(
        "Lower the risk per trade",
        "Risk per trade is above 1% or half the estimated Kelly fraction, which is zero "
        "when expectancy is not positive",
    ),
    "G5.4": _r(
        "Reduce order size or avoid thinly traded symbols",
        "At least one entry order is above the share of bar volume allowed",
    ),
    "G5.5": _r(
        "Reduce the number or size of simultaneous positions",
        "Peak margin use is above 90% of capital",
    ),
}


def _lookup(gate: str, dimension: str | None) -> dict[str, str] | None:
    return RULES.get(gate) or DIMENSION_DEFAULTS.get(dimension or "")


def _status_rule(gate: str, status: str | None) -> dict[str, str] | None:
    if status == "NOT_RUN":
        return NOT_RUN_RULES.get(gate)
    if status == "FAIL":
        return FAILED_RULES.get(gate)
    return None


DIRTY_SUFFIX = "; also commit the working tree and re-run from a clean state"


def _pick_gate(
    rows: list[dict[str, Any]], open_gates: set[str], primary_gate: str | None
) -> str | None:
    """The open gate to act on: the verdict's primary gate, a hard failure, then gate order."""
    if primary_gate in open_gates:
        return primary_gate
    for row in rows:
        if row["gate"] in open_gates and row.get("hard") and row.get("status") == "fail":
            return row["gate"]
    return next((r["gate"] for r in rows if r["gate"] in open_gates), None)


def next_action(
    promotion: Mapping[str, Any],
    evaluation: Mapping[str, Any] | None,
    *,
    code_dirty: bool,
    reproducible: bool,
    primary_gate: str | None = None,
) -> dict[str, Any]:
    """Pick the single most useful next step. It never changes the verdict.

    A dirty or non-reproducible run still reports its open gate, with a note to re-run clean;
    the commit-and-re-run action alone is returned only when no gate is open.
    """
    unclean = code_dirty or not reproducible
    stages = promotion["stages"]
    stage = next((s for s in STAGES if stages[s]["decision"] != READY), None)
    open_gates: set[str] = set()
    if stage is not None:
        open_gates = {
            g["gate"]
            for g in stages[stage]["gates"]
            if g["status"] in ("FAIL", "NOT_RUN") and g["gate"] not in DIAGNOSTIC_GATES
        }
    rows = evaluation_rows(evaluation)
    dims = {
        r["gate"]: dim["name"]
        for dim in (evaluation or {}).get("dimensions") or []
        for r in dim.get("rows") or []
    }
    gate = _pick_gate(rows, open_gates, primary_gate)
    status = None
    if stage is not None:
        status = next((g["status"] for g in stages[stage]["gates"] if g["gate"] == gate), None)
    rule = _status_rule(gate, status) if gate is not None else None
    rule = rule or (_lookup(gate, dims.get(gate)) if gate is not None else None)
    if gate is not None and rule:
        reason = rule["reason"] + (DIRTY_SUFFIX if unclean else "")
        return {"action": rule["action"], "reason": reason, "gate": gate}
    if unclean:
        why = "uncommitted changes" if code_dirty else "the run cannot be reproduced"
        return {
            "action": DIRTY_ACTION,
            "reason": f"A run with {why} cannot be promoted.",
            "gate": None,
        }
    if stage is None:
        return {"action": ADVANCE_ACTION, "reason": "Every stage is ready.", "gate": None}
    reasons = list(stages[stage].get("reasons") or [])
    invalid = [r for r in reasons if r.startswith(("run is not valid", "data quality problem"))]
    if invalid:
        return {
            "action": "Fix the run's validity problem and record a new run version",
            "reason": "; ".join(invalid) + ". No performance result can be trusted until then.",
            "gate": None,
        }
    critical = [r for r in reasons if r.startswith("critical engine capability not verified")]
    if critical:
        return {
            "action": "Verify the critical engine capability, then record a new run version",
            "reason": "; ".join(critical)
            + ". Every gate has a result; the engine cannot yet be trusted on this capability.",
            "gate": None,
        }
    return {
        "action": f"Evaluate the gates for the {stage} stage",
        "reason": "No gate in this stage has been evaluated.",
        "gate": None,
    }
