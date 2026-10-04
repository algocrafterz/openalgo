"""Strategy evaluation profile: a run scored gate by gate against the acceptance criteria.

`docs/STRATEGY-ACCEPTANCE-CRITERIA.md` is the source of truth for every threshold used
here; change a number there and here together, with a dated reason.

The result is a profile, not a score. Rows are grouped into the backlog's evaluation
dimensions so the reason a strategy passed or failed is always visible:

  edge, robustness, risk, stability, cost_sensitivity, regime_robustness,
  parameter_sensitivity, statistical_confidence, complexity

plus `integrity` (G0) and `deployment` (G6 to G9), which the criteria define but the
backlog does not list. Calendar-year results (G4.4) sit under regime_robustness because
years are the only market-regime slice the engine can cut today.

Every gate is listed exactly once. One the backtest cannot compute is `not_evaluated`
with the reason, never dropped, so a reader can tell "passed" from "never checked". The
statuses are:

  pass / fail      a requirement with a threshold, measured here
  report           a REPORT gate: computed and explained, a poor value is a finding
  not_evaluated    cannot be computed from a trade list; the row says why
  not_applicable   the gate does not apply to this horizon (MIS gates for swing books)

A HARD gate that fails stops the process, per the criteria: `summary.hard_failed` lists
them in gate order.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as _st

from signal_engine.backtest import splits, stats
from signal_engine.backtest.metrics import hurdle_t
from signal_engine.research.build_registry import EVALUATION_STATUSES as STATUSES

EVALUATION_VERSION = 1
CRITERIA_DOC = "docs/STRATEGY-ACCEPTANCE-CRITERIA.md"

DIMENSIONS = (
    "edge",
    "robustness",
    "risk",
    "stability",
    "cost_sensitivity",
    "regime_robustness",
    "parameter_sensitivity",
    "statistical_confidence",
    "complexity",
    "integrity",
    "deployment",
)
HORIZONS = ("intraday", "swing", "positional")

# Thresholds, each cited to its gate in the criteria document.
MIN_NET_R = 0.05  # G1.2
MAX_COST_R = 0.40  # G1.5
COMFORT_GROSS_OVER_COST = 1.5  # G1.4
MIN_TRADES = 200  # G2.1
BASE_T = 2.5  # G2.2, at ten trials or fewer
BASE_T_MAX_TRIALS = 10
FAMILY_ALPHA = 0.05
HOLDOUT_RANGE = (0.30, 0.40)  # G3.1
MIN_OOS_FRACTION_OF_IS = 0.40  # G3.2
MIN_POSITIVE_YEAR_SHARE = 0.60  # G4.4
WORST_YEAR_R = -0.25  # G4.4
TOP_SYMBOLS = 10  # G4.5
MAX_TOP_SHARE = 0.40  # G4.5
MIN_POSITIVE_SYMBOL_SHARE = 0.50  # G4.5
SQUAREOFF_HOUR = 15  # G6.9: no trade open at or after 15:00 IST
#: X.2 is a heuristic, not a criteria-doc gate. "all" = every sampled regime net positive,
#: chosen from the null false-pass rates in docs/REGIME-GATE-CALIBRATION.md.
REGIME_RULE = "all"
MIN_DSR = 0.90  # G2.3
WALK_FORWARD_MONTHS = 6  # G3.3
MIN_WALK_FORWARD_WINDOWS = 4  # G3.3
MIN_WALK_FORWARD_POSITIVE = 0.60  # G3.3
MIN_EMBARGO_SESSIONS = 1  # G3.5
REGIME_HEURISTIC = (
    "heuristic (calibrated): net positive in every sampled regime of each judged dimension"
)

#: (gate, dimension, title, hard, why it is not computed here). The last field is used when
#: no evaluator exists for the gate.
_GATES: tuple[tuple[str, str, str, bool, str], ...] = (
    ("G0.1", "integrity", "Same logic in every mode", False, "code review and adapter tests"),
    ("G0.2", "integrity", "Causality of every input", False, "no truncation check was run"),
    ("G0.3", "integrity", "Timestamps in IST", False, "unit test per data entry point"),
    ("G0.4", "integrity", "Data coverage", False, "coverage report outside the run"),
    ("G0.5", "integrity", "No silent exclusion", False, "log review, not stored in a record"),
    ("G0.6", "integrity", "Closing auction handled", False, "feature-level test"),
    ("G0.7", "integrity", "Test quality", False, "pytest --cov"),
    ("G0.8", "integrity", "Corporate actions", False, "split-day exclusion is in the data layer"),
    ("G1.1", "cost_sensitivity", "Cost model", False, "no cost model recorded"),
    ("G1.2", "edge", "Net expectancy at 1.0x cost", True, "no trades"),
    (
        "G1.3",
        "cost_sensitivity",
        "Slippage stress",
        True,
        "the flat cost model does not separate slippage from statutory charges",
    ),
    ("G1.4", "edge", "Gross edge vs cost", False, "no trades"),
    ("G1.5", "cost_sensitivity", "Cost per trade in R", True, "no trades"),
    ("G1.6", "cost_sensitivity", "Stop floor", False, "needs a stop-width table"),
    ("G2.1", "statistical_confidence", "Trade count", False, ""),
    ("G2.2", "statistical_confidence", "Day-clustered t-statistic", True, "no t-statistic"),
    ("G2.3", "statistical_confidence", "Deflated Sharpe", False, "needs a daily P&L series"),
    (
        "G2.4",
        "statistical_confidence",
        "Probability of backtest overfitting",
        False,
        "needs P&L for every variant tried on a common grid; stats.pbo_cscv is implemented "
        "and waits for a parameter grid",
    ),
    ("G2.5", "statistical_confidence", "Trials ledger", False, "n_trials not recorded"),
    (
        "G2.6",
        "statistical_confidence",
        "Dependence (Newey-West)",
        False,
        "needs a daily P&L series",
    ),
    ("G2.7", "statistical_confidence", "Track length", False, "needs a daily P&L series"),
    ("G3.1", "stability", "Chronological hold-out", False, "no out-of-sample split"),
    ("G3.2", "stability", "Out-of-sample result", False, "no out-of-sample split"),
    ("G3.3", "stability", "Walk-forward", False, "needs trades spanning four full windows"),
    ("G3.4", "stability", "Symbol hold-out", False, "needs at least 10 symbols with trades"),
    ("G3.5", "stability", "Leakage and embargo", False, "needs an out-of-sample split"),
    ("G4.1", "parameter_sensitivity", "Parameter perturbation", True, "needs a parameter sweep"),
    ("G4.2", "robustness", "Latency", False, "needs a delayed-entry rerun"),
    ("G4.3", "robustness", "Bar resolution", False, "needs a pessimistic same-bar rerun"),
    ("G4.4", "regime_robustness", "Sub-periods", True, "no trades"),
    ("G4.5", "robustness", "Dispersion", True, "no trades"),
    ("G4.6", "robustness", "Direction", False, "no trades"),
    ("G4.7", "robustness", "Survivorship", False, "universe is today's F&O list"),
    ("G4.8", "robustness", "Event days", False, "needs an event calendar"),
    ("G5.1", "risk", "Monte Carlo", False, "not implemented"),
    ("G5.2", "risk", "Live caps applied", False, "no position-cap run recorded"),
    ("G5.3", "risk", "Sizing", False, "a live-sizing check, not a trade-list one"),
    ("G5.4", "risk", "Capacity", False, "needs order size against bar volume"),
    ("G5.5", "risk", "Margin", False, "needs a simultaneous-margin model"),
    ("G6.1", "deployment", "Session and square-off", False, "needs the strategy window"),
    ("G6.2", "deployment", "Daily eligibility", False, "live-universe check"),
    ("G6.3", "deployment", "Circuits", False, "needs the daily price band"),
    ("G6.4", "deployment", "Tick size", True, "verified against the broker before live"),
    ("G6.5", "deployment", "Gap-through stops", False, "live stop-type test"),
    ("G6.6", "deployment", "SEBI retail algo framework", False, "operational"),
    ("G6.7", "deployment", "Charges current", False, "operational"),
    ("G6.8", "deployment", "Closing auction awareness", False, "needs the exit plan"),
    ("G6.9", "deployment", "Backtest respects the square-off", True, "no exit times"),
    ("G7.1", "deployment", "Paper incubation duration", False, "needs paper data"),
    ("G7.2", "deployment", "Signal parity", False, "needs paper data"),
    ("G7.3", "deployment", "Execution quality", False, "needs paper data"),
    ("G7.4", "deployment", "Paper tracking", False, "needs paper data"),
    ("G7.5", "deployment", "Capital ramp", False, "needs live data"),
    ("G8.1", "deployment", "Kill switch", False, "operational"),
    ("G8.2", "deployment", "Pre-trade controls", False, "operational"),
    ("G8.3", "deployment", "Recovery", False, "operational"),
    ("G8.4", "deployment", "Clock", False, "operational"),
    ("G8.5", "deployment", "Broker sessions", False, "operational"),
    ("G8.6", "deployment", "Audit trail", False, "operational"),
    ("G8.7", "deployment", "Change control", False, "operational"),
    ("G8.8", "deployment", "Alert escalation", False, "operational"),
    ("G9.1", "deployment", "Decay watch", False, "needs live data"),
    ("G9.2", "deployment", "Drawdown watch", False, "needs live data"),
    ("G9.3", "deployment", "Re-validation", False, "needs live data"),
    ("G9.4", "deployment", "Retirement", False, "needs live data"),
    ("X.1", "complexity", "Strategy parameter count", False, "no parameters recorded"),
    ("X.2", "regime_robustness", "Regime dispersion", False, "no regime block supplied"),
    ("X.3", "regime_robustness", "Regime activation hypothesis", False, "no regime block supplied"),
)

#: Gates the swing engine does not face: MIS session, square-off and auction rules.
_INTRADAY_ONLY = frozenset(g[0] for g in _GATES if g[0].startswith("G6."))


@dataclass(frozen=True)
class _Inputs:
    trades: Sequence
    metrics: dict[str, Any]
    n_trials: int
    horizon: str
    cost_bps: float
    cost_model: str | None
    oos_start: str | None
    position_caps: Sequence[dict[str, Any]] | None
    params: dict[str, Any] | None
    regimes: dict[str, Any] | None = None
    cost_profile: dict[str, Any] | None = None
    split: dict[str, Any] | None = None
    ledgers: dict[str, Any] | None = None
    session: dict[str, Any] | None = None
    ledger_problems: Sequence[str] = ()
    data_quality: dict[str, Any] | None = None
    calendar: Any = None
    membership: dict[str, Any] | None = None
    trials: dict[str, Any] | None = None
    truncation: Any = None


#: An evaluator returns (status, value, threshold, evidence).
_Result = tuple[str, Any, str, str]


def t_threshold(n_trials: int) -> float:
    """The |t| G2.2 requires: 2.5 up to ten trials, then a one-sided Bonferroni level.

    The criteria quote about 2.9 at 20 trials, 3.0 at 30 and 3.3 at 50; the t(200)
    quantile of 0.05 / trials gives 2.85, 2.98 and 3.13, continuous with 2.5 at the
    boundary. `metrics.hurdle_t` is a different, weaker figure (1.97 at one trial).
    """
    n = max(1, int(n_trials))
    if n <= BASE_T_MAX_TRIALS:
        return BASE_T
    return float(max(BASE_T, _st.t.ppf(1.0 - FAMILY_ALPHA / n, 200)))


def _num(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _r_applies(trades: Sequence) -> bool:
    """True when every trade has a stop, so its R multiples mean something."""
    return all(t.r_defined for t in trades)


def _r_net(trades: Sequence) -> np.ndarray:
    """Per-trade net result: R when every trade has a stop, else net bps (R is not defined).

    The gates that use it test signs, ratios and shares, which do not depend on the unit.
    Gates whose threshold is an absolute number of R are handled separately.
    """
    if _r_applies(trades):
        return np.array([t.r_net for t in trades], dtype=float)
    return np.array([t.net_bps for t in trades], dtype=float)


def _fmt(value: float, trades: Sequence, digits: int = 3) -> str:
    """A per-trade or total net result with its unit."""
    if _r_applies(trades):
        return f"{value:+.{digits}f}R"
    return f"{value:+.{max(digits - 2, 1)}f} bps"


_NO_STOP_NOTE = (
    "the criterion is defined in R, the trade's own stop distance, and these trades have no stop; "
    "a stop-independent threshold has to be defined by the owner"
)


def _day(trade: Any) -> pd.Timestamp:
    return pd.Timestamp(trade.day)


def _g11(x: _Inputs) -> _Result:
    threshold = "statutory charges at deployed notional plus slippage"
    profile = x.cost_profile
    if profile is not None:
        return (
            "pass",
            _num(profile["mean_total_bps"]),
            threshold,
            f"statutory schedule ({profile['product']}, {profile['exchange']}) priced per trade "
            f"at the deployed notional (capital {profile['capital']:,.0f}, risk "
            f"{profile['risk_per_trade']:.2%} per trade, leverage {profile['leverage']:g}x) plus "
            f"{profile['slippage_bps_per_side']:g} bps slippage per side: mean "
            f"{profile['mean_total_bps']:.2f} bps against the engine's flat "
            f"{profile['flat_bps']:g} bps",
        )
    if x.cost_model is None:
        return "not_evaluated", None, threshold, ""
    return (
        "report",
        _num(x.cost_bps),
        threshold,
        f"cost model '{x.cost_model}' at {x.cost_bps:g} bps round trip is a flat figure, "
        "not the statutory schedule computed at the deployed notional",
    )


def _g13(x: _Inputs) -> _Result:
    threshold = "net expectancy above 0 with slippage doubled (8 bps per side)"
    profile = x.cost_profile
    if profile is None:
        return "not_evaluated", None, threshold, "no per-trade cost model recorded for this run"
    at = profile["total_bps_at"]
    if "net_r_slip_2x" in profile:
        stressed = float(profile["net_r_slip_2x"])
        result = f"net {stressed:+.3f}R at 2.0x slippage (1.0x {float(profile['net_r']):+.3f}R)"
    else:
        stressed = float(profile["net_bps_slip_2x"])
        result = (
            f"net {stressed:+.2f} bps at 2.0x slippage (1.0x {float(profile['net_bps']):+.2f} bps)"
        )
    evidence = (
        f"{result}; total cost "
        f"{at['1.0']:.2f} bps at 1.0x, {at['1.5']:.2f} bps at 1.5x, {at['2.0']:.2f} bps at 2.0x"
    )
    return ("pass" if stressed > 0 else "fail"), _num(stressed), threshold, evidence


def _g12(x: _Inputs) -> _Result:
    threshold = f">= {MIN_NET_R:+.2f}R per trade"
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    if not _r_applies(x.trades):
        mean_bps = float(_r_net(x.trades).mean())
        return (
            "not_evaluated",
            None,
            threshold,
            f"{_NO_STOP_NOTE}; measured net expectancy is {mean_bps:+.2f} bps over "
            f"{len(x.trades)} trades",
        )
    mean = float(_r_net(x.trades).mean())
    status = "pass" if round(mean, 6) >= MIN_NET_R else "fail"
    return status, _num(mean), threshold, f"net expectancy {mean:+.3f}R over {len(x.trades)} trades"


def _g14(x: _Inputs) -> _Result:
    threshold = f"gross / cost >= {COMFORT_GROSS_OVER_COST:g} is comfortable"
    gross = x.metrics.get("gross_bps")
    if gross is None or x.cost_bps <= 0:
        return "not_evaluated", None, threshold, "gross bps or cost not available"
    ratio = float(gross) / x.cost_bps
    verdict = "comfortable" if ratio >= COMFORT_GROSS_OVER_COST else "thin"
    return (
        "report",
        _num(ratio),
        threshold,
        f"gross {float(gross):.2f} bps against {x.cost_bps:g} bps round trip: {verdict}",
    )


def _g15(x: _Inputs) -> _Result:
    threshold = f"median cost <= {MAX_COST_R:.2f}R"
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    if not _r_applies(x.trades):
        median_bps = float(np.median([t.gross_bps - t.net_bps for t in x.trades]))
        return (
            "not_evaluated",
            None,
            threshold,
            f"{_NO_STOP_NOTE}; median modelled cost is {median_bps:.2f} bps of notional per trade",
        )
    cost_r = np.array([t.r_gross - t.r_net for t in x.trades], dtype=float)
    median = float(np.median(cost_r))
    status = "pass" if round(median, 6) <= MAX_COST_R else "fail"
    return status, _num(median), threshold, f"median cost {median:.3f}R per trade"


def _g21(x: _Inputs) -> _Result:
    n = len(x.trades)
    status = "pass" if n >= MIN_TRADES else "fail"
    return status, n, f">= {MIN_TRADES} net trades", f"{n} trades"


def _g22(x: _Inputs) -> _Result:
    criteria = t_threshold(x.n_trials)
    hurdle = hurdle_t(x.n_trials)
    bar = max(criteria, hurdle)
    threshold = f">= {bar:.2f} at {x.n_trials} trials"
    t_stat = x.metrics.get("t_stat")
    if t_stat is None:
        return "not_evaluated", None, threshold, "t-statistic is undefined for this run"
    status = "pass" if float(t_stat) >= bar else "fail"
    return (
        status,
        _num(t_stat),
        threshold,
        f"day-clustered t {float(t_stat):.2f} against the larger of the criteria level "
        f"{criteria:.2f} and the record's Sidak hurdle {hurdle:.2f}",
    )


def _g25(x: _Inputs) -> _Result:
    threshold = "every variant tried is counted in trials.yaml; DSR and the t-hurdle use that count"
    t = x.trials
    if t is None:
        if not x.n_trials or x.n_trials < 1:
            return "not_evaluated", None, threshold, "no trial count and no ledger for this run"
        return (
            "fail",
            x.n_trials,
            threshold,
            f"n_trials {x.n_trials} was not derived from trials.yaml (no ledger block recorded)",
        )
    if not hasattr(t, "get") or t.get("source") != "trials.yaml":
        source = t.get("source") if hasattr(t, "get") else t
        return (
            "fail",
            x.n_trials,
            threshold,
            f"n_trials {x.n_trials} did not come from trials.yaml (source {source!r})",
        )
    total = t.get("total")
    if total != x.n_trials:
        return (
            "fail",
            x.n_trials,
            threshold,
            f"n_trials {x.n_trials} disagrees with the ledger total {total!r}",
        )
    return (
        "pass",
        x.n_trials,
        threshold,
        f"n_trials {x.n_trials} from trials.yaml: {t.get('material')} material and "
        f"{t.get('external')} external",
    )


_G02_THRESHOLD = "a run on data cut at earlier sessions reproduces every earlier signal and trade"


def _g02(x: _Inputs) -> _Result:
    if x.truncation is None:
        return "not_evaluated", None, _G02_THRESHOLD, "no truncation check was run for this run"
    try:
        data = x.truncation.to_dict() if hasattr(x.truncation, "to_dict") else x.truncation
        ok = data.get("ok")
        checked = list(data.get("checked") or [])
        mismatches = list(data.get("mismatches") or [])
        first = mismatches[0] if mismatches else None
        named = (
            f"{first.get('symbol')} cut {first.get('cut')}: {first.get('kind')}: "
            f"{first.get('detail')}"
            if first is not None
            else None
        )
    except (AttributeError, TypeError, IndexError):
        return "fail", None, _G02_THRESHOLD, "the truncation result is malformed"
    if mismatches:
        return (
            "fail",
            len(mismatches),
            _G02_THRESHOLD,
            f"{len(mismatches)} mismatch(es); first: {named}",
        )
    if ok is not True:
        return "fail", None, _G02_THRESHOLD, "the look-ahead check reported failure"
    if data.get("compared_total") == 0:
        return "not_evaluated", 0, _G02_THRESHOLD, "nothing was compared"
    symbols = sorted({str(c.get("symbol")) for c in checked})
    if not symbols:
        return "not_evaluated", 0, _G02_THRESHOLD, "no symbol had enough history to be checked"
    return (
        "pass",
        len(symbols),
        _G02_THRESHOLD,
        f"{len(symbols)} symbol(s) re-run at {len(checked)} cut(s): no earlier signal or trade changed",
    )


def _split(x: _Inputs) -> tuple[list, list] | None:
    if not x.oos_start:
        return None
    before = [t for t in x.trades if _day(t).date().isoformat() < x.oos_start]
    after = [t for t in x.trades if _day(t).date().isoformat() >= x.oos_start]
    return (before, after) if before and after else None


def _g31(x: _Inputs) -> _Result:
    low, high = HOLDOUT_RANGE
    threshold = f"last {low:.0%} to {high:.0%} of sessions"
    parts = _split(x)
    if parts is None:
        return "not_evaluated", None, threshold, "no out-of-sample split"
    days = [{_day(t) for t in part} for part in parts]
    fraction = len(days[1]) / (len(days[0]) + len(days[1]))
    return (
        "report",
        _num(fraction),
        threshold,
        f"{fraction:.1%} of trade days held out from {x.oos_start}; "
        "whether it stayed untouched until the candidate was frozen is not recorded",
    )


def _g32(x: _Inputs) -> _Result:
    threshold = f"OOS net > 0 and >= {MIN_OOS_FRACTION_OF_IS:.0%} of in-sample"
    parts = _split(x)
    if parts is None:
        return "not_evaluated", None, threshold, "no out-of-sample split"
    in_sample = float(_r_net(parts[0]).mean())
    oos = float(_r_net(parts[1]).mean())
    holds = oos > 0 and (in_sample <= 0 or oos >= MIN_OOS_FRACTION_OF_IS * in_sample)
    return (
        "pass" if holds else "fail",
        _num(oos),
        threshold,
        f"OOS {_fmt(oos, x.trades)} ({len(parts[1])} trades) vs in-sample "
        f"{_fmt(in_sample, x.trades)} ({len(parts[0])} trades)",
    )


MIN_HOLDOUT_SYMBOLS = 10  # G3.4
MIN_HOLDOUT_TRADES = 100  # G3.4, per half


def symbol_half(symbol: str) -> int:
    """Deterministic 0/1 split of a symbol, stable across runs and machines."""
    return hashlib.sha1(symbol.encode()).digest()[0] & 1


def _g34(x: _Inputs) -> _Result:
    """Symbol hold-out: does the edge hold on symbols chosen without looking at results.

    Parameters are fixed in a record, so no re-fit is involved: the trade list is split by
    a hash of the symbol and both halves must be net positive, the weaker at least
    MIN_OOS_FRACTION_OF_IS of the stronger.
    """
    threshold = f"both symbol halves net > 0, weaker >= {MIN_OOS_FRACTION_OF_IS:.0%} of stronger"
    symbols = {t.symbol for t in x.trades}
    if len(symbols) < MIN_HOLDOUT_SYMBOLS:
        return (
            "not_evaluated",
            None,
            threshold,
            f"{len(symbols)} symbols traded; need {MIN_HOLDOUT_SYMBOLS}",
        )
    halves = [[t for t in x.trades if symbol_half(t.symbol) == h] for h in (0, 1)]
    if min(len(h) for h in halves) < MIN_HOLDOUT_TRADES:
        return (
            "not_evaluated",
            None,
            threshold,
            f"a symbol half has under {MIN_HOLDOUT_TRADES} trades",
        )
    means = [float(_r_net(h).mean()) for h in halves]
    weak, strong = min(means), max(means)
    holds = weak > 0 and weak >= MIN_OOS_FRACTION_OF_IS * strong
    return (
        "pass" if holds else "fail",
        _num(weak),
        threshold,
        f"half A {_fmt(means[0], x.trades)} ({len(halves[0])} trades), "
        f"half B {_fmt(means[1], x.trades)} ({len(halves[1])} trades)",
    )


def _g44(x: _Inputs) -> _Result:
    threshold = (
        f">= {MIN_POSITIVE_YEAR_SHARE:.0%} of calendar years net positive, "
        f"worst year >= {WORST_YEAR_R:+.2f}R"
    )
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    frame = pd.DataFrame({"year": [_day(t).year for t in x.trades], "r": _r_net(x.trades)})
    yearly = frame.groupby("year")["r"].mean()
    share = float((yearly > 0).mean())
    worst_year, worst = int(yearly.idxmin()), float(yearly.min())
    if not _r_applies(x.trades):
        return (
            "not_evaluated",
            _num(share),
            threshold,
            f"{int((yearly > 0).sum())} of {len(yearly)} years net positive; worst {worst_year} "
            f"at {worst:+.1f} bps. The worst-year limit is an absolute number of R and these "
            "trades have no stop, so a stop-independent limit has to be defined by the owner",
        )
    ok = share >= MIN_POSITIVE_YEAR_SHARE and worst >= WORST_YEAR_R
    return (
        "pass" if ok else "fail",
        _num(share),
        threshold,
        f"{int((yearly > 0).sum())} of {len(yearly)} years net positive; worst {worst_year} at "
        f"{worst:+.3f}R; market regimes are scored by X.2",
    )


def _g45(x: _Inputs) -> _Result:
    threshold = (
        f"top {TOP_SYMBOLS} symbols <= {MAX_TOP_SHARE:.0%} of net profit, "
        f">= {MIN_POSITIVE_SYMBOL_SHARE:.0%} of symbols net positive"
    )
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    frame = pd.DataFrame({"symbol": [t.symbol for t in x.trades], "r": _r_net(x.trades)})
    per_symbol = frame.groupby("symbol")["r"].sum().sort_values(ascending=False)
    positive_share = float((per_symbol > 0).mean())
    total = float(per_symbol.sum())
    if total <= 0:
        return (
            "fail",
            None,
            threshold,
            f"net profit {_fmt(total, x.trades, 1)} is not positive; {positive_share:.0%} of "
            f"{len(per_symbol)} symbols net positive",
        )
    top_share = float(per_symbol.head(TOP_SYMBOLS).sum()) / total
    ok = top_share <= MAX_TOP_SHARE and positive_share >= MIN_POSITIVE_SYMBOL_SHARE
    return (
        "pass" if ok else "fail",
        _num(top_share),
        threshold,
        f"top {TOP_SYMBOLS} symbols hold {top_share:.0%} of net profit; "
        f"{positive_share:.0%} of {len(per_symbol)} symbols net positive",
    )


def _g46(x: _Inputs) -> _Result:
    threshold = "longs and shorts reported separately; a one-sided strategy is declared"
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    longs = [t for t in x.trades if t.direction == 1]
    shorts = [t for t in x.trades if t.direction == -1]

    def mean(side: list) -> str:
        return _fmt(float(_r_net(side).mean()), side) if side else "n/a"

    text = (
        f"{len(longs)} long / {len(shorts)} short trades; net {mean(longs)} long, "
        f"{mean(shorts)} short"
    )
    if not longs or not shorts:
        text += "; one-sided (" + ("long" if longs else "short") + " only)"
    return "report", len(shorts), threshold, text


def _g52(x: _Inputs) -> _Result:
    threshold = "run with the live position and loss caps"
    if not x.position_caps:
        return "not_evaluated", None, threshold, "no position-cap run recorded"
    rows = sorted(x.position_caps, key=lambda r: r["max_positions"])
    text = "; ".join(f"{r['max_positions']} slots: net {r['net_bps']:+.2f} bps" for r in rows)
    return (
        "report",
        _num(rows[0]["net_bps"]),
        threshold,
        f"{text}. The live max_open_positions is set in config.yaml and may not be among these",
    )


def _g69(x: _Inputs) -> _Result:
    threshold = f"no trade open at or after {SQUAREOFF_HOUR}:00 IST"
    exits = [pd.Timestamp(t.exit_time) for t in x.trades if t.exit_time is not None]
    if not exits:
        return "not_evaluated", None, threshold, "no exit times"
    latest = max(exits, key=lambda ts: (ts.hour, ts.minute))
    stamp = f"{latest.hour:02d}:{latest.minute:02d}"
    status = "pass" if latest.hour < SQUAREOFF_HOUR else "fail"
    return status, stamp, threshold, f"latest exit at {stamp}"


def _daily_r(trades: Sequence) -> np.ndarray:
    """One number per session: the sum of that day's net R, in date order."""
    frame = pd.DataFrame({"day": [_day(t) for t in trades], "r": _r_net(trades)})
    return frame.groupby("day")["r"].sum().sort_index().to_numpy(dtype=float)


def _g23(x: _Inputs) -> _Result:
    threshold = f"DSR >= {MIN_DSR:.2f} at the true trial count"
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    out = stats.deflated_sharpe(_daily_r(x.trades), x.n_trials)
    if out is None:
        return "not_evaluated", None, threshold, "daily P&L too short or constant for a Sharpe"
    note = (
        "trial Sharpe variance is assumed to be the no-edge sampling variance, because only the "
        "trial count is recorded"
        if out["sr_var_assumed"]
        else "trial Sharpe variance supplied"
    )
    return (
        "pass" if out["dsr"] >= MIN_DSR else "fail",
        out["dsr"],
        threshold,
        f"DSR {out['dsr']:.3f} over {out['n_obs']} sessions at {out['n_trials']} trials "
        f"(daily Sharpe {out['sharpe_daily']:.3f} against a deflation threshold of "
        f"{out['sharpe_threshold']:.3f}); assumption: {note}",
    )


def _g26(x: _Inputs) -> _Result:
    threshold = "t on daily aggregated P&L with a Newey-West autocorrelation adjustment"
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    daily = _daily_r(x.trades)
    nw = stats.newey_west_t(daily)
    plain = stats.newey_west_t(daily, lags=0)
    if not math.isfinite(nw):
        return "not_evaluated", None, threshold, "daily P&L too short or constant"
    return (
        "report",
        _num(nw, 3),
        threshold,
        f"Newey-West t {nw:.2f} over {len(daily)} sessions (plain daily t {plain:.2f}); "
        "compare with the G2.2 bar, which the trade-clustered t already has to clear",
    )


def _g27(x: _Inputs) -> _Result:
    threshold = "sample length against the Minimum Track Record Length at 95 percent"
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    daily = _daily_r(x.trades)
    minimum = stats.min_track_record_length(daily)
    if math.isnan(minimum):
        return "not_evaluated", None, threshold, "daily P&L too short or constant"
    if math.isinf(minimum):
        return (
            "report",
            None,
            threshold,
            f"{len(daily)} sessions observed; the observed Sharpe is not above zero, so no "
            "track length would be enough",
        )
    verdict = "sufficient" if len(daily) >= minimum else "too short"
    return (
        "report",
        _num(minimum, 1),
        threshold,
        f"{len(daily)} sessions observed against a minimum of {minimum:.0f} sessions: {verdict}",
    )


def _g33(x: _Inputs) -> _Result:
    threshold = (
        f"at least {MIN_WALK_FORWARD_WINDOWS} rolling windows of {WALK_FORWARD_MONTHS} months; "
        f"net above 0 in at least {MIN_WALK_FORWARD_POSITIVE:.0%}"
    )
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    wf = stats.walk_forward(x.trades, months=WALK_FORWARD_MONTHS)
    if wf["windows"] < MIN_WALK_FORWARD_WINDOWS:
        return (
            "not_evaluated",
            None,
            threshold,
            f"only {wf['windows']} full {WALK_FORWARD_MONTHS}-month windows fit the trades; "
            f"{MIN_WALK_FORWARD_WINDOWS} are required",
        )
    share = wf["share_positive"]
    return (
        "pass" if share >= MIN_WALK_FORWARD_POSITIVE else "fail",
        _num(share),
        threshold,
        f"{wf['positive']} of {wf['windows']} windows net positive ({wf['empty']} empty); "
        "shipped parameters held fixed in every window, so this is rolling stability, "
        "not a re-fit per window",
    )


def _g35(x: _Inputs) -> _Result:
    threshold = f"no trade straddles the split; embargo of at least {MIN_EMBARGO_SESSIONS} session"
    if not x.oos_start:
        return "not_evaluated", None, threshold, "no out-of-sample split"
    straddlers = splits.count_straddlers(x.trades, x.oos_start)
    info = x.split or {}
    embargo = int(info.get("embargo_sessions", 0))
    purged = int(info.get("purged_in_sample", 0))
    if embargo >= MIN_EMBARGO_SESSIONS and purged >= straddlers:
        return (
            "pass",
            straddlers,
            threshold,
            f"{straddlers} trades straddled the cut and were purged from in-sample; embargo "
            f"{embargo} session(s) removed {int(info.get('embargoed_oos', 0))} out-of-sample trades",
        )
    return (
        "fail",
        straddlers,
        threshold,
        f"{straddlers} trades straddle the cut and were scored in-sample; "
        f"embargo {embargo} session(s)",
    )


def _params_count(x: _Inputs) -> _Result:
    strategy = (x.params or {}).get("strategy")
    if not isinstance(strategy, dict):
        return "not_evaluated", None, "fewer assumptions are better", "no parameters recorded"

    def leaves(value: Any) -> int:
        return sum(leaves(v) for v in value.values()) if isinstance(value, dict) else 1

    count = leaves(strategy)
    return "report", count, "fewer assumptions are better", f"{count} strategy parameters"


def _regime_dispersion(x: _Inputs) -> _Result:
    """X.2, a HEURISTIC: is the edge positive in every sampled market regime?

    Not a number from the acceptance criteria. The rule was calibrated on real swing trade
    lists (docs/REGIME-GATE-CALIBRATION.md): ">= 2 positive regimes" passed 35 to 37 percent
    of zero-edge books, so it requires every sampled regime of each judged dimension to be
    net positive, which passes about 6 percent of zero-edge books. Power at a modest true
    edge is low by design, so a fail here is a prompt to look, not a verdict; the gate is
    not HARD.
    """
    threshold = REGIME_HEURISTIC
    if x.regimes is None:
        return "not_evaluated", None, threshold, "no regime block supplied for this run"
    shares: list[float] = []
    negative: list[str] = []
    for dim in ("trend", "vol"):
        rows = [r for r in x.regimes.get(dim, []) if not r["low_sample"]]
        if len(rows) < 2:
            continue
        shares.append(sum(1 for r in rows if r["net_r"] > 0) / len(rows))
        for r in rows:
            if not r["net_r"] > 0:
                negative.append(f"{dim}/{r['regime']} {r['net_r']:+.3f}R over {r['trades']} trades")
    if not shares:
        return (
            "report",
            None,
            threshold,
            "fewer than two regimes have enough trades in any dimension, so dispersion is "
            f"not judged ({x.regimes.get('unclassified_trades', 0)} trades unclassified)",
        )
    if negative:
        return (
            "fail",
            _num(min(shares)),
            threshold,
            "not positive in every sampled regime: " + "; ".join(negative),
        )
    return (
        "pass",
        _num(min(shares)),
        threshold,
        "net positive in every sampled regime of every judged dimension",
    )


def _regime_activation(x: _Inputs) -> _Result:
    """X.3: which regimes a strategy could be enabled in. A hypothesis, never a rule.

    Lists adequately sampled regimes that are net positive (enable) and net negative
    (disable) per dimension. Choosing these from the same history that measured them is a
    fitted filter, so the gate only reports; a condition is adopted only after the filtered
    strategy is tested out-of-sample and clears the acceptance criteria as its own
    candidate.
    """
    threshold = "hypothesis only; validate the filtered strategy out-of-sample"
    if x.regimes is None:
        return "not_evaluated", None, threshold, "no regime block supplied for this run"
    parts: list[str] = []
    for dim in ("trend", "vol"):
        rows = [r for r in x.regimes.get(dim, []) if not r["low_sample"]]
        if len(rows) < 2:
            continue
        on = [r["regime"] for r in rows if r["net_r"] > 0]
        off = [r["regime"] for r in rows if not r["net_r"] > 0]
        if off and on:
            parts.append(f"{dim}: enable {', '.join(on)}; disable {', '.join(off)}")
        elif on:
            parts.append(f"{dim}: no regime condition needed, all sampled regimes positive")
        else:
            parts.append(f"{dim}: no sampled regime is positive, a filter cannot rescue it")
    if not parts:
        return "report", None, threshold, "fewer than two adequately sampled regimes"
    return (
        "report",
        len(parts),
        threshold,
        "; ".join(parts) + ". Validate out-of-sample before use.",
    )


# ---- ledger-backed gates (G0.5, G6.1, G6.5, G1.6) ------------------------------------------

#: Stop-width buckets, percent of signal price. Left-closed, six buckets (G1.6).
STOP_WIDTH_EDGES = (0.20, 0.30, 0.50, 0.75, 1.00)
#: Share of accepted signals at one stop width above which a fixed floor "sets most stops".
FLOOR_DOMINANCE = 0.5
#: NSE continuous session, minutes after midnight IST: the outer bound for any fill.
MARKET_OPEN_MIN = 9 * 60 + 15
MARKET_CLOSE_MIN = 15 * 60 + 30
GAP_REASONS = ("SL_GAP", "SL_GAP_ENTRY")
_NOT_EVALUATED_NO_LEDGERS = "no lineage ledgers recorded for this run"


def reconcile_frames(frames: dict[str, Any], trades: Sequence = ()) -> list[str]:
    """Problems found reconciling the four ledger frames; empty means they reconcile exactly.

    The same checks as `LedgerRecorder.reconcile`, but from the frames alone, so a recorded run
    can be audited without the recorder: ids are unique, every reason code is known, every
    signal is accepted or excluded, every accepted signal has an order, every order is filled or
    dropped with a code, every filled order has an entry fill and every entry has a trade (by
    position id) and an exit.
    """
    from signal_engine.backtest import ledgers as lg

    sig, ordr, fills, exc = (
        frames["signals"],
        frames["orders"],
        frames["fills"],
        frames["exclusions"],
    )
    out: list[str] = []
    for name, df, col in (
        ("signals", sig, "signal_id"),
        ("orders", ordr, "order_id"),
        ("fills", fills, "fill_id"),
        ("exclusions", exc, "exclusion_id"),
    ):
        dup = int(df[col].duplicated().sum())
        if dup:
            out.append(f"{name}: {dup} duplicate {col}")
    for code in sorted(set(exc["reason_code"].dropna())):
        if not lg.is_known(code):
            out.append(f"exclusions: unknown reason code {code}")
    exc_ids = set(exc["exclusion_id"])
    accepted = sig["accepted"].astype(bool)
    sig_exc = sig["exclusion_id"]
    bad = sig[~accepted & (sig_exc.isna() | ~sig_exc.isin(exc_ids))]
    if len(bad):
        out.append(f"signals: {len(bad)} rejected without an exclusion row")
    bad = sig[accepted & sig_exc.notna()]
    if len(bad):
        out.append(f"signals: {len(bad)} accepted but carry an exclusion")
    accepted_ids = set(sig.loc[accepted, "signal_id"])
    orphan = set(ordr["signal_id"]) - accepted_ids
    if orphan:
        out.append(f"orders: {len(orphan)} without an accepted signal")
    lost = accepted_ids - set(ordr["signal_id"])
    if lost:
        out.append(f"signals: {len(lost)} accepted without an order")
    bad_status = ordr[~ordr["status"].isin(["FILLED", "DROPPED"])]
    if len(bad_status):
        out.append(f"orders: {len(bad_status)} neither FILLED nor DROPPED")
    dropped = ordr[ordr["status"] == "DROPPED"]
    miss = dropped[dropped["exclusion_id"].isna() | ~dropped["exclusion_id"].isin(exc_ids)]
    if len(miss):
        out.append(f"orders: {len(miss)} dropped without an exclusion row")
    filled = ordr[ordr["status"] == "FILLED"]
    entries = fills[fills["kind"] == "entry"]
    no_entry = set(filled["order_id"]) - set(entries["order_id"])
    if no_entry:
        out.append(f"orders: {len(no_entry)} filled without an entry fill")
    extra_entry = set(entries["order_id"]) - set(filled["order_id"])
    if extra_entry:
        out.append(f"fills: {len(extra_entry)} entry fills without a filled order")
    pids = [t.position_id for t in trades if getattr(t, "position_id", None) is not None]
    entry_pids = set(entries["position_id"])
    if set(pids) != entry_pids or len(pids) != len(set(pids)):
        out.append(f"trades: {len(pids)} trades versus {len(entry_pids)} entry positions")
    no_exit = entry_pids - set(fills.loc[fills["kind"] == "exit", "position_id"])
    if no_exit:
        out.append(f"fills: {len(no_exit)} positions without an exit fill")
    return out


def _g05(x: _Inputs) -> _Result:
    threshold = "ledgers reconcile exactly; every exclusion carries a known reason code"
    if x.ledgers is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_LEDGERS
    problems = [*reconcile_frames(x.ledgers, x.trades), *x.ledger_problems]
    exc = x.ledgers["exclusions"]
    counts = {str(k): int(v) for k, v in exc.groupby("reason_code")["count"].sum().items()}
    table = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    text = f"exclusions by reason code: {table}"
    if problems:
        shown = "; ".join(problems[:5]) + (
            f"; +{len(problems) - 5} more" if len(problems) > 5 else ""
        )
        return "fail", len(problems), threshold, f"{len(problems)} problems: {shown}. {text}"
    return "pass", 0, threshold, f"ledgers reconcile; {text}"


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _entry_window(text: Any) -> tuple[int, int] | None:
    if not isinstance(text, str) or len(text) != 11 or text[5] != "-":
        return None
    try:
        start = int(text[:2]) * 60 + int(text[3:5])
        end = int(text[6:8]) * 60 + int(text[9:11])
    except ValueError:
        return None
    return (start, end) if start < end else None


def _g61(x: _Inputs) -> _Result:
    # Entry: signal bar start inside [window start, window end), the engine's cutoff semantics.
    # Exit: the exit bar's own span is `bar_minutes_by_symbol[symbol]` when the session gives one
    # (mixed bar sizes), else the declared `bar_minutes`; its close must be <= square-off - margin.
    threshold = "entries from signal bars inside the strategy window; every exit bar closes by square-off - margin"
    if x.ledgers is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_LEDGERS
    s = x.session or {}
    squareoff, margin, bar = (
        s.get(k) for k in ("mis_squareoff_min", "squareoff_margin_min", "bar_minutes")
    )
    if squareoff is None or margin is None or bar is None:
        return (
            "not_evaluated",
            None,
            threshold,
            "the session (square-off, margin, bar size) is not recorded",
        )
    fills = x.ledgers["fills"]
    if len(fills) == 0:
        return "not_evaluated", None, threshold, "no fills"
    limit = int(squareoff) - int(margin)
    window = _entry_window(s.get("entry_window"))
    spans = {str(k): int(v) for k, v in (s.get("bar_minutes_by_symbol") or {}).items()}
    mins = _minutes(fills["fill_time"])
    kind = fills["kind"].to_numpy()
    ids = fills["fill_id"].to_numpy()
    symbols = fills["symbol"].to_numpy()
    signal_min = _signal_minutes(x.ledgers, fills)
    bad: list[str] = []
    exit_close: list[int] = []
    for i in range(len(fills)):
        m, entry = int(mins[i]), kind[i] == "entry"
        if m < MARKET_OPEN_MIN or m > MARKET_CLOSE_MIN:
            bad.append(f"{ids[i]} at {_hhmm(m)} is outside the market session")
        elif entry:
            sig = signal_min.get(ids[i])
            if window is not None and sig is not None and not window[0] <= sig < window[1]:
                bad.append(
                    f"{ids[i]} entry from a signal bar at {_hhmm(sig)} is outside the entry window"
                )
        else:
            close = m + spans.get(str(symbols[i]), int(bar))
            exit_close.append(close)
            if close > limit:
                bad.append(f"{ids[i]} exit bar closes {_hhmm(close)}, after {_hhmm(limit)}")
    window_text = s.get("entry_window") or "no entry window"
    if bad:
        more = f" ({len(bad)} offending fills)" if len(bad) > 1 else ""
        return "fail", len(bad), threshold, f"first offender: {bad[0]}{more}"
    latest = _hhmm(max(exit_close)) if exit_close else "n/a"
    return (
        "pass",
        latest,
        threshold,
        f"{int((kind == 'entry').sum())} entries from signal bars inside {window_text}; latest "
        f"exit bar closes {latest}, limit {_hhmm(limit)}",
    )


def _minutes(times: pd.Series) -> np.ndarray:
    stamps = pd.to_datetime(times, utc=True).dt.tz_convert("Asia/Kolkata")
    return (stamps.dt.hour * 60 + stamps.dt.minute).to_numpy()


def _signal_minutes(ledgers: dict[str, Any], fills: pd.DataFrame) -> dict[str, int]:
    """Entry fill id -> start minute of the signal bar that caused it, from the orders ledger.

    The engine blocks a signal whose bar start is at or after the entry cutoff, so the entry
    window is judged on the signal bar (start <= bar < end), not on the next-bar-open fill
    stamp. An entry whose order is not in the ledger is left unchecked here; G0.5 reports it.
    """
    orders = ledgers["orders"]
    if len(orders) == 0:
        return {}
    by_order = dict(zip(orders["order_id"], _minutes(orders["submit_time"]), strict=True))
    entries = fills[fills["kind"] == "entry"]
    return {
        fid: int(by_order[oid])
        for fid, oid in zip(entries["fill_id"], entries["order_id"], strict=True)
        if oid in by_order
    }


def _g65(x: _Inputs) -> _Result:
    threshold = "report: share of trades stopped by a gap, and the mean loss beyond the stop"
    if x.ledgers is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_LEDGERS
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
    gaps = [t for t in x.trades if t.reason in GAP_REASONS and t.risk > 0]
    share = len(gaps) / len(x.trades)
    head = (
        f"{len(gaps)} of {len(x.trades)} trades ({share:.1%}) exited by {' or '.join(GAP_REASONS)}"
    )
    if not gaps:
        return "report", 0.0, threshold, head + "; no gap exits"
    beyond = np.array([t.direction * (t.sl - t.exit) / t.risk for t in gaps], dtype=float)
    return (
        "report",
        _num(share),
        threshold,
        f"{head}; mean loss beyond the stop {float(beyond.mean()):.3f}R. The live stop type "
        "(SL-M versus SL) is not tested here",
    )


def stop_width_table(signals: pd.DataFrame) -> list[dict[str, Any]]:
    """Accepted signals bucketed by stop width, risk as a percent of signal price (G1.6)."""
    edges = (0.0, *STOP_WIDTH_EDGES, math.inf)
    labels = [f"<{edges[1]:.2f}%"]
    labels += [f"{lo:.2f}-{hi:.2f}%" for lo, hi in zip(edges[1:-2], edges[2:-1], strict=True)]
    labels.append(f">={edges[-2]:.2f}%")
    pct = _stop_pct(signals)
    counts = [
        int(((pct >= lo) & (pct < hi)).sum()) for lo, hi in zip(edges[:-1], edges[1:], strict=True)
    ]
    total = sum(counts)
    return [
        {"bucket": label, "count": n, "share": (n / total if total else 0.0)}
        for label, n in zip(labels, counts, strict=True)
    ]


def _stop_pct(signals: pd.DataFrame) -> pd.Series:
    price = signals["signal_price"].astype(float)
    risk = signals["risk"].astype(float)
    keep = signals["accepted"].astype(bool) & (risk > 0) & (price > 0)
    return (risk[keep] / price[keep] * 100.0).round(6)


def _g16(x: _Inputs) -> _Result:
    threshold = (
        f"report: if a fixed floor sets most stops (more than {FLOOR_DOMINANCE:.0%} at one width), "
        "show results at other floors"
    )
    if x.ledgers is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_LEDGERS
    pct = _stop_pct(x.ledgers["signals"])
    if len(pct) == 0:
        return "not_evaluated", None, threshold, "no accepted signals with a stop"
    table = stop_width_table(x.ledgers["signals"])
    text = "; ".join(f"{r['bucket']}: {r['count']} ({r['share']:.0%})" for r in table)
    modal = pct.round(2).value_counts()
    width, n = float(modal.index[0]), int(modal.iloc[0])
    share = n / len(pct)
    evidence = f"stop width, {len(pct)} accepted signals: {text}"
    if share > FLOOR_DOMINANCE:
        evidence += (
            f". A fixed floor appears to set most stops: {share:.0%} at {width:.2f}% of price. "
            "Results at other floors are not computed here; rerun with another min_sl_pct"
        )
    return "report", _num(share), threshold, evidence


_NOT_EVALUATED_NO_DQ = "no data-quality report recorded for this run"
_NOT_EVALUATED_NO_MEMBERSHIP = "no universe membership record for this run"
_IST_NAMES = ("Asia/Kolkata", "Asia/Calcutta")
_LEDGER_TIME_COLUMNS = (
    ("signals", "bar_time"),
    ("orders", "submit_time"),
    ("orders", "fill_bar_time"),
    ("fills", "fill_time"),
    ("exclusions", "bar_time"),
)
CRITERION_MIN_BARS = 370  # G0.4, one-minute bars in a 375-bar session
_DAILY_INTERVALS = ("1d", "d")


def _policy_text(dq: dict[str, Any], key: str) -> str:
    return str((dq.get("policy") or {}).get(key, "not stated"))


def _g03(x: _Inputs) -> _Result:
    threshold = "all external times converted to IST at the boundary; bar timestamps are open-time"
    dq = x.data_quality
    if dq is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_DQ
    stated = _policy_text(dq, "timestamps")
    caveat = (
        "open-time stamping is a property of each loader and is covered by its unit test, "
        "not re-proved per run"
    )
    if x.ledgers is None:
        return "report", None, threshold, f"{stated}; no ledgers to check. {caveat}"
    bad, checked = [], 0
    for name, column in _LEDGER_TIME_COLUMNS:
        frame = x.ledgers.get(name)
        if frame is None or column not in frame.columns:
            continue
        checked += 1
        tz = getattr(frame[column].dtype, "tz", None)
        if tz is None or str(tz) not in _IST_NAMES:
            bad.append(f"{name}.{column}")
    if checked == 0:
        return "report", None, threshold, f"{stated}; no ledger time columns to check. {caveat}"
    if bad:
        return (
            "fail",
            len(bad),
            threshold,
            f"{len(bad)} ledger time columns are not in IST: {', '.join(bad)}. {stated}",
        )
    return (
        "pass",
        0,
        threshold,
        f"{stated}; all {checked} ledger time columns are IST. {caveat}",
    )


def _g04(x: _Inputs) -> _Result:
    threshold = (
        f"every symbol-session used has at least {CRITERION_MIN_BARS} one-minute bars "
        "(98.7% of a 375-bar session); gaps listed by name"
    )
    dq = x.data_quality
    if dq is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_DQ
    interval = str((dq.get("dropped") or {}).get("interval", "")).lower()
    if interval in _DAILY_INTERVALS:
        return (
            "not_evaluated",
            None,
            threshold,
            "daily bars: one bar per session, so there is no intraday coverage to check",
        )
    if dq.get("status") == "NOT_RUN":
        return "not_evaluated", None, threshold, "no symbols were audited"
    summary = dq.get("summary") or {}
    audited = list(summary.get("audited_symbols") or [])
    total = int(summary.get("symbols") or len(audited))
    floor = (dq.get("thresholds") or {}).get("min_session_completeness")
    note = (
        f"audit floor is {floor:.0%} of expected bars against 98.7% in the criterion; the "
        "auction-era exemption from 2026-08-03 is not modelled"
        if isinstance(floor, (int, float))
        else "the auction-era exemption from 2026-08-03 is not modelled"
    )
    short = [str(d) for d in (getattr(x.calendar, "short_sessions", ()) or ())]
    short_text = (
        f" Short or irregular sessions in the calendar: {', '.join(short)}." if short else ""
    )
    audit_text = f"{len(audited)} of {total} symbols audited"
    item = next((i for i in dq.get("items") or [] if i.get("check") == "incomplete_sessions"), None)
    if item is not None:
        names = ", ".join(item.get("symbols") or [])
        # within the data-quality tolerance (info severity) the gaps are reported, not failed
        status = "fail" if item.get("severity") == "warning" else "report"
        return (
            status,
            int(item.get("count", 0)),
            threshold,
            f"{item.get('detail')}; symbols (up to 10): {names}; {audit_text}; "
            f"full list in data_quality.json. {note}.{short_text}",
        )
    if len(audited) < total:
        return (
            "report",
            None,
            threshold,
            f"{audit_text} (an evenly spaced sample); no gap in the sample, the remaining "
            f"symbols are not checked, so the criterion cannot be passed. {note}.{short_text}",
        )
    return (
        "pass",
        0,
        threshold,
        f"{audit_text}; no symbol-session below the audit floor. {note}.{short_text}",
    )


def _g06(x: _Inputs) -> _Result:
    threshold = (
        "report: intraday level profiles use continuous-market bars only, stamped before 15:15"
    )
    dq = x.data_quality
    if dq is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_DQ
    return (
        "report",
        None,
        threshold,
        f"data layer: {_policy_text(dq, 'closing_auction')}. Whether a feature or strategy "
        "reads bars from 15:15 onward is a property of its code (features/session.py), not "
        "checked per run",
    )


def _g08(x: _Inputs) -> _Result:
    threshold = "report: split and bonus sessions excluded or adjusted; ex-date handling verified"
    dq = x.data_quality
    if dq is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_DQ
    split = (dq.get("dropped") or {}).get("split_days_dropped") or {}
    n_days = sum(len(v) for v in split.values())
    return (
        "report",
        n_days,
        threshold,
        f"policy: {_policy_text(dq, 'corporate_actions')}. This run excluded {n_days} "
        f"symbol-sessions across {len(split)} symbols as probable splits. Limits: the 0.75 to "
        "1.33 test cannot see dividends, bonuses or rights, and ex-date handling is not "
        "verified, so this gate does not pass",
    )


def _g47(x: _Inputs) -> _Result:
    threshold = (
        "report: the universe uses today's list, which is survivor-biased; test on symbols as "
        "they existed and list names that later left"
    )
    m = x.membership
    if m is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_MEMBERSHIP
    bias = bool(m.get("survivorship_bias"))
    return (
        "report",
        bias,
        threshold,
        f"membership source {m.get('membership_source')}; survivorship bias {bias}. The "
        "universe is today's symbol master applied to the past, not point-in-time membership: "
        "names that later left F&O or were delisted are absent and cannot be listed here "
        "because no historical membership source exists. Results are biased toward survivors",
    )


def _g62(x: _Inputs) -> _Result:
    threshold = (
        "report: skip names under ASM, GSM or T2T, names the broker blocks for MIS, and names "
        "without a valid tick; log each by name"
    )
    if x.membership is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_MEMBERSHIP
    if x.ledgers is None:
        return "not_evaluated", None, threshold, _NOT_EVALUATED_NO_LEDGERS
    exc = x.ledgers["exclusions"]
    rows = exc[exc["reason_code"] == "NOT_ELIGIBLE"]
    total = int(rows["count"].sum())
    by_reason = {str(k): int(v) for k, v in rows.groupby("detail")["count"].sum().items()}
    table = ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())) or "none"
    policy = x.membership.get("eligibility_policy")
    if policy and x.membership.get("price_floor_applied") is False:
        # Report what was applied: the floor is in the policy but this run skipped it.
        policy = {k: v for k, v in policy.items() if k != "min_price"}
        floor = "min_price not applied (split-adjusted daily prices)"
    else:
        floor = ""
    policy_text = (
        ", ".join([*(f"{k}={v}" for k, v in sorted(policy.items())), *([floor] if floor else [])])
        if policy
        else "not recorded"
    )
    return (
        "report",
        total,
        threshold,
        f"data-driven eligibility policy ({policy_text}); {total} symbol-sessions NOT_ELIGIBLE "
        f"({table}), each logged by symbol and date in exclusions.parquet. ASM, GSM, T2T and "
        "broker MIS block lists are not modelled",
    )


_EVALUATORS: dict[str, Callable[[_Inputs], _Result]] = {
    "G0.2": _g02,
    "G0.3": _g03,
    "G0.4": _g04,
    "G0.5": _g05,
    "G0.6": _g06,
    "G0.8": _g08,
    "G1.1": _g11,
    "G1.2": _g12,
    "G1.3": _g13,
    "G1.4": _g14,
    "G1.5": _g15,
    "G1.6": _g16,
    "G2.1": _g21,
    "G2.2": _g22,
    "G2.3": _g23,
    "G2.5": _g25,
    "G2.6": _g26,
    "G2.7": _g27,
    "G3.1": _g31,
    "G3.2": _g32,
    "G3.3": _g33,
    "G3.4": _g34,
    "G3.5": _g35,
    "G4.4": _g44,
    "G4.5": _g45,
    "G4.6": _g46,
    "G4.7": _g47,
    "G5.2": _g52,
    "G6.1": _g61,
    "G6.2": _g62,
    "G6.5": _g65,
    "G6.9": _g69,
    "X.1": _params_count,
    "X.2": _regime_dispersion,
    "X.3": _regime_activation,
}


def _row(gate: str, title: str, hard: bool, status: str, value: Any, threshold: str, evidence: str):
    return {
        "gate": gate,
        "title": title,
        "status": status,
        "hard": hard,
        "value": value,
        "threshold": threshold,
        "evidence": evidence,
    }


def evaluate(
    trades: Sequence,
    *,
    metrics: dict[str, Any],
    n_trials: int,
    horizon: str,
    cost_bps: float,
    oos_start: str | None,
    position_caps: Sequence[dict[str, Any]] | None,
    params: dict[str, Any] | None,
    cost_model: str | None = None,
    regimes: dict[str, Any] | None = None,
    cost_profile: dict[str, Any] | None = None,
    split: dict[str, Any] | None = None,
    ledgers: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    ledger_problems: Sequence[str] = (),
    data_quality: dict[str, Any] | None = None,
    calendar: Any = None,
    membership: dict[str, Any] | None = None,
    trials: dict[str, Any] | None = None,
    truncation: Any = None,
) -> dict[str, Any]:
    """Score a run against the acceptance criteria. Inputs are not modified.

    `metrics` is the run record's headline block (it supplies `t_stat` and `gross_bps`);
    `horizon` is intraday, swing or positional; `position_caps` are the record's
    position-cap rows; `regimes` is the run's optional regimes block; `cost_profile` is the optional per-trade
    `cost_model` block (G1.1, G1.3); `split` is the record's split block with its purge and
    embargo counts (G3.5); `ledgers` (the four lineage frames) and `session`
    (mis_squareoff_min, squareoff_margin_min, bar_minutes, entry_window) feed G0.5, G6.1, G6.5
    and G1.6, which stay not_evaluated without them; `ledger_problems` are the recorder's own
    reconciliation problems and make G0.5 fail. `data_quality` (the per-run report),
    `calendar` (the derived session calendar) and `membership` (source, survivorship flag,
    eligibility policy) feed G0.3, G0.4, G0.6, G0.8, G4.7 and G6.2, each not_evaluated without
    its input. `trials` (material, external, total, source) feeds G2.5 and `truncation` (the
    `TruncationResult` or its dict) feeds G0.2, each not_evaluated without its input. Returns `{version, criteria, horizon, dimensions, summary}`.
    """
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {', '.join(HORIZONS)}, got {horizon!r}")
    inputs = _Inputs(
        trades=list(trades),
        metrics=metrics,
        n_trials=n_trials,
        horizon=horizon,
        cost_bps=float(cost_bps),
        cost_model=cost_model,
        oos_start=oos_start,
        position_caps=position_caps,
        params=params,
        regimes=regimes,
        cost_profile=cost_profile,
        split=split,
        ledgers=ledgers,
        session=session,
        ledger_problems=tuple(ledger_problems),
        data_quality=data_quality,
        calendar=calendar,
        membership=membership,
        trials=trials,
        truncation=truncation,
    )
    rows_by_dimension: dict[str, list[dict[str, Any]]] = {d: [] for d in DIMENSIONS}
    for gate, dimension, title, hard, why in _GATES:
        if horizon != "intraday" and gate in _INTRADAY_ONLY:
            row = _row(
                gate,
                title,
                hard,
                "not_applicable",
                None,
                "intraday MIS rules",
                "swing books hold CNC delivery; MIS session and square-off gates do not apply",
            )
        elif gate in _EVALUATORS:
            status, value, threshold, evidence = _EVALUATORS[gate](inputs)
            row = _row(gate, title, hard, status, value, threshold, evidence or why)
        else:
            row = _row(gate, title, hard, "not_evaluated", None, "", why)
        rows_by_dimension[dimension].append(row)
    dimensions = [{"name": name, "rows": rows_by_dimension[name]} for name in DIMENSIONS]
    rows = [r for d in dimensions for r in d["rows"]]
    counts = Counter(r["status"] for r in rows)
    return {
        "version": EVALUATION_VERSION,
        "criteria": CRITERIA_DOC,
        "horizon": horizon,
        "dimensions": dimensions,
        "summary": {
            "counts": {status: counts.get(status, 0) for status in STATUSES},
            "hard_failed": [r["gate"] for r in rows if r["hard"] and r["status"] == "fail"],
        },
    }
