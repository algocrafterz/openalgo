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
    ("G0.2", "integrity", "Causality of every input", False, "truncation tests in tests/"),
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


def _r_net(trades: Sequence) -> np.ndarray:
    return np.array([t.r_net for t in trades], dtype=float)


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
    stressed = float(profile["net_r_slip_2x"])
    at = profile["total_bps_at"]
    evidence = (
        f"net {stressed:+.3f}R at 2.0x slippage (1.0x {float(profile['net_r']):+.3f}R); total cost "
        f"{at['1.0']:.2f} bps at 1.0x, {at['1.5']:.2f} bps at 1.5x, {at['2.0']:.2f} bps at 2.0x"
    )
    return ("pass" if stressed > 0 else "fail"), _num(stressed), threshold, evidence


def _g12(x: _Inputs) -> _Result:
    threshold = f">= {MIN_NET_R:+.2f}R per trade"
    if not x.trades:
        return "not_evaluated", None, threshold, "no trades"
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
    return (
        "report",
        x.n_trials,
        "every variant tried is counted",
        f"n_trials {x.n_trials}; the hurdle is a lower bound when the count is not known",
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
        f"OOS {oos:+.3f}R ({len(parts[1])} trades) vs in-sample {in_sample:+.3f}R "
        f"({len(parts[0])} trades)",
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
        f"half A {means[0]:+.3f}R ({len(halves[0])} trades), "
        f"half B {means[1]:+.3f}R ({len(halves[1])} trades)",
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
            f"net profit {total:+.1f}R is not positive; {positive_share:.0%} of "
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
        return f"{float(_r_net(side).mean()):+.3f}R" if side else "n/a"

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


_EVALUATORS: dict[str, Callable[[_Inputs], _Result]] = {
    "G1.1": _g11,
    "G1.2": _g12,
    "G1.3": _g13,
    "G1.4": _g14,
    "G1.5": _g15,
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
    "G5.2": _g52,
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
) -> dict[str, Any]:
    """Score a run against the acceptance criteria. Inputs are not modified.

    `metrics` is the run record's headline block (it supplies `t_stat` and `gross_bps`);
    `horizon` is intraday, swing or positional; `position_caps` are the record's
    position-cap rows; `regimes` is the run's optional regimes block; `cost_profile` is the optional per-trade
    `cost_model` block (G1.1, G1.3); `split` is the record's split block with its purge and
    embargo counts (G3.5). Returns `{version, criteria, horizon, dimensions, summary}`.
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
