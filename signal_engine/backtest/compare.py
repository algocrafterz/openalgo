"""Compare two immutable run folders: the engine behind the Backtests UI comparison view.

Everything is read from the run folders (manifest, frozen spec, resolved execution config,
report and retained trades). Metrics for the full, common and incremental period views are
recomputed from `evidence/trades.parquet` with `tearsheet.headline_metrics`, the same
function that builds a run record, so there is no second implementation. Deltas are never
labelled better or worse.

This module must stay light: it does not import the harness, the data loaders or duckdb.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from signal_engine.backtest import change_class
from signal_engine.backtest.fingerprints import ROUND_TRIP_FIELD, SLIPPAGE_FIELD
from signal_engine.backtest.tearsheet import MAX_SERIES_POINTS, downsample, headline_metrics
from signal_engine.backtest.trades_io import IST_NAME
from signal_engine.research.intake.fingerprints import NON_RULE_KEYS

SCHEMA = 1
#: The only parquet columns the comparison reads (a large file is never loaded whole).
_FRAME_COLUMNS = ["entry_time", "gross_r", "net_r", "gross_bps", "net_bps"]
DISTRIBUTION_BINS = 20
ROLLING_MAX_WINDOW = 50
#: report.json cost_model keys that are results of the trades, not cost assumptions.
_COST_MODEL_OUTPUTS = frozenset(
    {"mean_statutory_bps", "mean_total_bps", "net_r", "net_r_slip_2x", "trades"}
)


class CompareError(Exception):
    """A run folder cannot be compared (no valid manifest, no retained trades)."""


# ---- reading --------------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_json(path: Path) -> dict[str, Any]:
    try:
        return _dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _optional_yaml(path: Path) -> dict[str, Any]:
    try:
        return _dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError):
        return {}


def _load_manifest(folder: Path) -> dict[str, Any]:
    path = Path(folder) / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompareError(f"{folder}: no valid manifest.json ({exc})") from exc
    period = _dict(data).get("period") if isinstance(data, dict) else None
    ok = (
        isinstance(data, dict)
        and isinstance(period, dict)
        and all(_is_iso_date(period.get(k)) for k in ("start", "end"))
        and isinstance(data.get("fingerprints"), dict)
        and isinstance(data.get("strategy"), dict)
        and isinstance(data.get("dataset"), dict)
    )
    if not ok:
        raise CompareError(f"{folder}: no valid manifest.json (missing period or fingerprints)")
    return data


def _is_iso_date(value: Any) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _load_frame(folder: Path) -> tuple[pd.DataFrame, int]:
    """The retained trades (needed columns only) and the count of rows dropped for no entry time."""
    path = Path(folder) / "evidence" / "trades.parquet"
    if not path.is_file():
        raise CompareError(f"{folder}: no evidence/trades.parquet")
    try:
        frame = pd.read_parquet(path, columns=_FRAME_COLUMNS)
        frame["entry_time"] = pd.to_datetime(frame["entry_time"], utc=True).dt.tz_convert(IST_NAME)
    except Exception as exc:  # noqa: BLE001 - any parquet failure is a CompareError
        raise CompareError(f"{folder}: evidence/trades.parquet is unreadable ({exc})") from exc
    keep = frame["entry_time"].notna()
    dropped = int((~keep).sum())
    frame = frame[keep].copy()
    # The session day is the IST entry date, the same definition build_run_record uses
    # (Trade.day is the IST entry date), so full-view metrics equal the recorded headline.
    frame["day"] = frame["entry_time"].dt.date.map(lambda d: d.isoformat())
    return frame, dropped


class _Run:
    """Everything compare reads from one run folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.manifest = _load_manifest(self.folder)
        self.frame, self.dropped_trades = _load_frame(self.folder)
        self.strategy = _optional_json(self.folder / "strategy" / "strategy.json")
        self.spec = _optional_yaml(self.folder / "strategy" / "spec.yaml")
        self.execution = _optional_json(self.folder / "execution" / "config.json")
        self.report = _optional_json(self.folder / "report" / "report.json")
        n_trials = self.report.get("n_trials")
        self.n_trials = n_trials if isinstance(n_trials, int) and n_trials >= 1 else 1
        self.scope = _scope(self.manifest, self.execution, self.strategy, self.spec)

    @property
    def period(self) -> tuple[str, str]:
        p = self.manifest["period"]
        return p["start"], p["end"]


# ---- scope ----------------------------------------------------------------------------------


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _resolved_value(execution: Mapping[str, Any], field: str) -> Any:
    return _dict(_dict(execution.get("resolved")).get(f"execution.{field}")).get("value")


def _scope(
    manifest: Mapping[str, Any],
    execution: Mapping[str, Any],
    strategy: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    session, dataset = _dict(manifest.get("session")), _dict(manifest.get("dataset"))
    profile = _dict(manifest.get("deployment_profile"))
    sizing, portfolio = _dict(spec.get("sizing")), _dict(spec.get("portfolio"))
    period = _dict(manifest.get("period"))
    window = session.get("strategy_window")
    name, sha = profile.get("name"), profile.get("sha256")
    return {
        "market": _dict(execution.get("profile")).get("market"),
        "universe": _dict(manifest.get("universe")).get("definition"),
        "symbol_count": dataset.get("symbol_count"),
        "style": strategy.get("style"),
        "direction": strategy.get("direction"),
        "timeframe": strategy.get("timeframe"),
        "product": strategy.get("product"),
        "period": (
            {"start": period["start"], "end": period["end"]}
            if isinstance(period.get("start"), str) and isinstance(period.get("end"), str)
            else None
        ),
        "strategy_window": (
            window
            if isinstance(window, str)
            else _jsonable(window)
            if isinstance(window, dict)
            else None
        ),
        "exchange_session": session.get("exchange"),
        "broker_cutoff": session.get("broker_cutoff"),
        "force_flat": session.get("force_flat"),
        "capital_per_trade": sizing.get("capital_per_trade"),
        "risk_per_trade_pct": sizing.get("risk_per_trade_pct"),
        "portfolio_mode": portfolio.get("mode"),
        "portfolio_scope": portfolio.get("scope"),
        "slippage_bps_per_side": _resolved_value(execution, "slippage_bps_per_side"),
        "cost_bps_round_trip": _resolved_value(execution, "cost_bps_round_trip"),
        "profile": {"name": name, "sha256": sha}
        if isinstance(name, str) and isinstance(sha, str)
        else None,
        "dataset": {
            "id": _str_or_none(dataset.get("id")),
            "source": _str_or_none(dataset.get("source")),
            "interval": _str_or_none(dataset.get("interval")),
            "data_as_of": _str_or_none(dataset.get("data_as_of")),
            "fingerprint": _str_or_none(dataset.get("fingerprint")),
        },
    }


def build_scope(folder: Path) -> dict[str, Any]:
    """The RunScope of one run folder (keys match the TypeScript `RunScope`).

    Raises:
        CompareError: The folder has no valid manifest.json.
    """
    folder = Path(folder)
    manifest = _load_manifest(folder)
    return _scope(
        manifest,
        _optional_json(folder / "execution" / "config.json"),
        _optional_json(folder / "strategy" / "strategy.json"),
        _optional_yaml(folder / "strategy" / "spec.yaml"),
    )


# ---- diffs ----------------------------------------------------------------------------------


def _field(field: str, label: str, a: Any, b: Any) -> dict[str, Any]:
    a, b = _jsonable(a), _jsonable(b)
    return {"field": field, "label": label, "a": a, "b": b, "changed": a != b}


def _humanize(name: str) -> str:
    text = name.replace("_", " ")
    return text[:1].upper() + text[1:]


_SCOPE_LABELS = {
    "market": "Market",
    "universe": "Universe",
    "symbol_count": "Symbol count",
    "style": "Style",
    "direction": "Direction",
    "timeframe": "Timeframe",
    "product": "Product",
    "period.start": "Period start",
    "period.end": "Period end",
    "strategy_window": "Strategy window",
    "exchange_session": "Exchange session",
    "broker_cutoff": "Broker cutoff",
    "force_flat": "Force flat",
    "capital_per_trade": "Capital per trade",
    "risk_per_trade_pct": "Risk per trade (%)",
    "portfolio_mode": "Portfolio mode",
    "portfolio_scope": "Portfolio scope",
    "slippage_bps_per_side": "Slippage (bps per side)",
    "cost_bps_round_trip": "Round-trip cost (bps)",
    "profile.name": "Profile name",
    "profile.sha256": "Profile hash",
    "dataset.id": "Dataset id",
    "dataset.source": "Dataset source",
    "dataset.interval": "Dataset interval",
    "dataset.data_as_of": "Dataset as of",
    "dataset.fingerprint": "Dataset fingerprint",
}


def _flatten_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in scope.items():
        if key in ("period", "profile", "dataset"):
            for sub in _SCOPE_SUBKEYS[key]:
                flat[f"{key}.{sub}"] = _dict(value).get(sub)
        else:
            flat[key] = value
    return flat


_SCOPE_SUBKEYS = {
    "period": ("start", "end"),
    "profile": ("name", "sha256"),
    "dataset": ("id", "source", "interval", "data_as_of", "fingerprint"),
}


def _scope_diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[dict[str, Any]]:
    fa, fb = _flatten_scope(a), _flatten_scope(b)
    return [_field(k, _SCOPE_LABELS.get(k, _humanize(k)), fa.get(k), fb.get(k)) for k in fa]


def _dataset_diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    da, db = _dict(a.get("dataset")), _dict(b.get("dataset"))
    keys = ("id", "source", "interval", "data_as_of", "symbol_count", "fingerprint")
    fields = [_field(k, f"Dataset {_humanize(k).lower()}", da.get(k), db.get(k)) for k in keys]
    return {"fields": fields, "fingerprint_changed": da.get("fingerprint") != db.get("fingerprint")}


def _resolved(run: _Run) -> dict[str, Any]:
    resolved = _dict(run.execution.get("resolved"))
    return {k: _dict(v).get("value") for k, v in resolved.items()}


def _execution_diff(a: _Run, b: _Run) -> list[dict[str, Any]]:
    ra, rb = _resolved(a), _resolved(b)
    skip = {SLIPPAGE_FIELD, ROUND_TRIP_FIELD}
    names = sorted(
        n for n in {*ra, *rb} if n.startswith(("execution.", "profile.")) and n not in skip
    )
    out = []
    for name in names:
        head, rest = name.split(".", 1)
        label = _humanize(rest) if head == "execution" else f"Profile {rest.replace('_', ' ')}"
        out.append(_field(name, label, ra.get(name), rb.get(name)))
    return out


def _cost_model_scalars(run: _Run) -> dict[str, Any]:
    model = _dict(run.report.get("cost_model"))
    return {
        k: v
        for k, v in model.items()
        if k not in _COST_MODEL_OUTPUTS and (v is None or isinstance(v, (str, int, float, bool)))
    }


def _cost_diff(a: _Run, b: _Run) -> list[dict[str, Any]]:
    out = [
        _field(
            "slippage_bps_per_side",
            "Slippage (bps per side)",
            a.scope["slippage_bps_per_side"],
            b.scope["slippage_bps_per_side"],
        ),
        _field(
            "cost_bps_round_trip",
            "Round-trip cost (bps)",
            a.scope["cost_bps_round_trip"],
            b.scope["cost_bps_round_trip"],
        ),
    ]
    ma, mb = _cost_model_scalars(a), _cost_model_scalars(b)
    for key in sorted({*ma, *mb}):
        out.append(
            _field(
                f"cost_model.{key}", f"Cost model {key.replace('_', ' ')}", ma.get(key), mb.get(key)
            )
        )
    return out


def _spec_diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "changed": {
            k: {"a": _jsonable(a[k]), "b": _jsonable(b[k])}
            for k in sorted(a.keys() & b.keys())
            if a[k] != b[k]
        },
        "added": {k: _jsonable(b[k]) for k in sorted(b.keys() - a.keys())},
        "removed": {k: _jsonable(a[k]) for k in sorted(a.keys() - b.keys())},
    }


def _strategy_diff(a: _Run, b: _Run) -> dict[str, Any]:
    def rules(run: _Run) -> dict[str, Any]:
        return {k: v for k, v in run.spec.items() if k not in NON_RULE_KEYS}

    def params(run: _Run) -> dict[str, Any]:
        return _dict(run.spec.get("parameters"))

    def version(run: _Run) -> str | None:
        v = run.strategy.get("version", _dict(run.manifest.get("strategy")).get("version"))
        return v if isinstance(v, str) else None

    def pair(key: str) -> dict[str, Any]:
        fa = _dict(a.manifest.get("fingerprints")).get(key)
        fb = _dict(b.manifest.get("fingerprints")).get(key)
        return {"a": fa, "b": fb, "changed": fa != fb}

    va, vb = version(a), version(b)
    return {
        "version": {"a": va, "b": vb},
        "same_version": va == vb,
        "rules": _spec_diff(rules(a), rules(b)),
        "parameters": _spec_diff(params(a), params(b)),
        "fingerprints": {k: pair(k) for k in ("rules", "params", "strategy")},
    }


# ---- metrics --------------------------------------------------------------------------------


def metrics_from_frame(frame: pd.DataFrame, n_trials: int = 1) -> dict[str, Any]:
    """Headline metrics of a retained-trades frame via `tearsheet.headline_metrics`.

    The session day is the entry date in IST. `risk_pct` is not stored, so it is recovered
    from the bps columns (`bps = R x risk_pct x 100`).
    """
    if "day" in frame.columns:
        days = frame["day"].tolist()
    else:
        days = [d.isoformat() for d in frame["entry_time"].dt.date]
    gross_r = frame["gross_r"].to_numpy(dtype=float)
    net_r = frame["net_r"].to_numpy(dtype=float)
    gross_bps = frame["gross_bps"].to_numpy(dtype=float)
    net_bps = frame["net_bps"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        from_gross = gross_bps / gross_r / 100
        from_net = net_bps / net_r / 100
    risk_pct = np.where(gross_r != 0, from_gross, np.where(net_r != 0, from_net, 0.0))
    return headline_metrics(days, gross_r, net_r, risk_pct, n_trials=n_trials)


def _within(frame: pd.DataFrame, window: Mapping[str, str]) -> pd.DataFrame:
    return frame[(frame["day"] >= window["start"]) & (frame["day"] <= window["end"])]


def _in_ranges(frame: pd.DataFrame, ranges: Sequence[Mapping[str, str]]) -> pd.DataFrame:
    mask = pd.Series(False, index=frame.index)
    for r in ranges:
        mask |= (frame["day"] >= r["start"]) & (frame["day"] <= r["end"])
    return frame[mask]


def _deltas(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    def numeric(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    keys = [k for k in dict.fromkeys([*a, *b]) if numeric(a.get(k)) or numeric(b.get(k))]
    out = {}
    for key in keys:
        va = a.get(key) if numeric(a.get(key)) else None
        vb = b.get(key) if numeric(b.get(key)) else None
        out[key] = {
            "a": va,
            "b": vb,
            "delta": round(vb - va, 6) if va is not None and vb is not None else None,
        }
    return out


# ---- overlap --------------------------------------------------------------------------------


def _range(start: str, end: str) -> dict[str, str]:
    return {"start": start, "end": end}


def _common(pa: tuple[str, str], pb: tuple[str, str]) -> dict[str, str] | None:
    start, end = max(pa[0], pb[0]), min(pa[1], pb[1])
    return _range(start, end) if start <= end else None


def _extra(period: tuple[str, str], common: Mapping[str, str] | None) -> list[dict[str, str]]:
    """The parts of a period outside the common window: up to two disjoint inclusive ranges."""
    if common is None:
        return [_range(*period)]
    one_day = timedelta(days=1)
    parts = []
    if period[0] < common["start"]:
        before = (date.fromisoformat(common["start"]) - one_day).isoformat()
        parts.append(_range(period[0], before))
    if period[1] > common["end"]:
        after = (date.fromisoformat(common["end"]) + one_day).isoformat()
        parts.append(_range(after, period[1]))
    return parts


def _span(r: Mapping[str, str]) -> str:
    return f"{r['start']} to {r['end']}"


def _warning(
    pa: tuple[str, str],
    pb: tuple[str, str],
    common: Mapping[str, str] | None,
    extra: Mapping[str, list[dict[str, str]]],
) -> str | None:
    if pa == pb:
        return None
    if common is None:
        return (
            f"The two runs cover different periods with no sessions in common (run A "
            f"{pa[0]} to {pa[1]}, run B {pb[0]} to {pb[1]}), so there is no common period "
            "to compare. This is not a like-for-like comparison."
        )
    parts = [
        f"run {k.upper()} also covers {' and '.join(_span(r) for r in v)}"
        for k, v in extra.items()
        if v
    ]
    return (
        f"The runs cover different periods: {'; '.join(parts)}. This is not a pure "
        f"strategy comparison, because the extra sessions can change the result. Use the "
        f"common-period view ({_span(common)}) for a like-for-like comparison."
    )


def _incremental_run(
    pa: tuple[str, str], pb: tuple[str, str], extra: Mapping[str, Any]
) -> str | None:
    candidates = [k for k in ("a", "b") if extra[k]]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return "a" if pa[1] > pb[1] else "b"


# ---- curves ---------------------------------------------------------------------------------


def _round(value: Any) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), 4)


def _on_axis(series: pd.Series, axis: Sequence[str], end: str) -> list[float | None]:
    """The series carried flat across the shared axis.

    None before the first value and after `end` (the run's own period end, or its last
    trade day if that is later), so a shorter run never extends past where it stopped.
    """
    if series.empty:
        return [None] * len(axis)
    last = max(end, str(series.index.max()))
    aligned = series.sort_index().reindex(list(axis), method="ffill")
    return [
        _round(v) if pd.notna(v) and x <= last else None
        for x, v in zip(axis, aligned.to_numpy(), strict=True)
    ]


def _equity(frame: pd.DataFrame, anchor: str | None = None) -> pd.Series:
    """Cumulative net R by day; with `anchor`, a 0 is placed there if no trade falls on it."""
    eq = frame.groupby("day")["net_r"].sum().sort_index().cumsum()
    if anchor is not None and (eq.empty or anchor not in eq.index):
        eq = pd.concat([pd.Series([0.0], index=[anchor]), eq]).sort_index()
        eq = eq.groupby(level=0).last()
    return eq


def _drawdown(equity: pd.Series) -> pd.Series:
    """Equity minus its running peak (peak floored at 0): zero or negative, as the tearsheet."""
    if equity.empty:
        return equity
    peak = np.maximum.accumulate(np.maximum(equity.to_numpy(), 0.0))
    return pd.Series(equity.to_numpy() - peak, index=equity.index)


def _rolling(frame: pd.DataFrame, window: int) -> pd.Series:
    if frame.empty or window < 1:
        return pd.Series(dtype=float)
    ordered = frame.sort_values("entry_time", kind="stable")
    roll = ordered["net_r"].reset_index(drop=True).rolling(window).mean()
    roll.index = ordered["day"].to_numpy()
    last = roll[~roll.index.duplicated(keep="last")]
    return last.dropna() if last.notna().any() else last.iloc[0:0]


def _thin(points: list[list], keep: str = "last") -> list[list]:
    return downsample(points, MAX_SERIES_POINTS, keep=keep)


def _series_points(
    axis: Sequence[str], a: list[float | None], b: list[float | None]
) -> list[dict[str, Any]]:
    points = _thin([[x, 0, va, vb] for x, va, vb in zip(axis, a, b, strict=True)])
    return [{"x": p[0], "a": p[2], "b": p[3]} for p in points]


def _drawdown_points(
    axis: Sequence[str], a: list[float | None], b: list[float | None]
) -> list[dict[str, Any]]:
    """Downsample keeping the deepest drawdown of either run in each bucket."""
    raw = [[x, min(va or 0.0, vb or 0.0), va, vb] for x, va, vb in zip(axis, a, b, strict=True)]
    return [{"x": p[0], "a": p[2], "b": p[3]} for p in _thin(raw, keep="min")]


def _monthly(fa: pd.DataFrame, fb: pd.DataFrame) -> list[dict[str, Any]]:
    def sums(frame: pd.DataFrame) -> dict[str, float]:
        if frame.empty:
            return {}
        return frame.groupby(frame["day"].str[:7])["net_r"].sum().to_dict()

    ma, mb = sums(fa), sums(fb)
    return [
        {"x": m, "a": _round(ma[m]) if m in ma else None, "b": _round(mb[m]) if m in mb else None}
        for m in sorted({*ma, *mb})
    ]


def _distribution(fa: pd.DataFrame, fb: pd.DataFrame) -> list[dict[str, Any]]:
    va, vb = fa["net_r"].to_numpy(float), fb["net_r"].to_numpy(float)
    va, vb = va[np.isfinite(va)], vb[np.isfinite(vb)]
    values = np.concatenate([va, vb])
    if not len(values):
        return []
    rng = (float(values.min()), float(values.max()))
    counts_a, edges = np.histogram(va, bins=DISTRIBUTION_BINS, range=rng)
    counts_b, _ = np.histogram(vb, bins=DISTRIBUTION_BINS, range=rng)
    return [
        {
            "bin_start": round(float(edges[i]), 6),
            "bin_end": round(float(edges[i + 1]), 6),
            "a": int(counts_a[i]),
            "b": int(counts_b[i]),
        }
        for i in range(DISTRIBUTION_BINS)
    ]


def _curves(
    fa: pd.DataFrame,
    fb: pd.DataFrame,
    ends: tuple[str, str],
    common: Mapping[str, str] | None,
    incremental: Sequence[Mapping[str, str]],
    anchors: tuple[str | None, str | None] = (None, None),
) -> dict[str, Any]:
    """Curves for two trade frames on one shared axis.

    `ends` are each run's last carried day; `anchors` place a 0 equity point at a window start
    for a run (None: the run has no series in this view).
    """
    eq_a, eq_b = _equity(fa, anchors[0]), _equity(fb, anchors[1])
    axis = sorted({*fa["day"], *fb["day"], *eq_a.index, *eq_b.index})
    counts = [len(f) for f in (fa, fb) if len(f)]
    window = min([ROLLING_MAX_WINDOW, *counts]) if counts else 0
    return {
        "equity_r": _series_points(
            axis, _on_axis(eq_a, axis, ends[0]), _on_axis(eq_b, axis, ends[1])
        ),
        "drawdown_r": _drawdown_points(
            axis,
            _on_axis(_drawdown(eq_a), axis, ends[0]),
            _on_axis(_drawdown(eq_b), axis, ends[1]),
        ),
        "monthly": _monthly(fa, fb),
        "rolling": _series_points(
            axis,
            _on_axis(_rolling(fa, window), axis, ends[0]),
            _on_axis(_rolling(fb, window), axis, ends[1]),
        ),
        "rolling_window": window,
        "distribution": _distribution(fa, fb),
        "common": dict(common) if common else None,
        "incremental": [dict(r) for r in incremental],
    }


def _curve_sets(
    a: _Run,
    b: _Run,
    common: Mapping[str, str] | None,
    inc_run: str | None,
    inc_ranges: list[dict[str, str]],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Full-period curves, common-window curves and incremental curves."""
    ends = (a.period[1], b.period[1])
    full = _curves(a.frame, b.frame, ends, common, inc_ranges)
    curves_common = None
    if common is not None:
        window_ends = (min(ends[0], common["end"]), min(ends[1], common["end"]))
        curves_common = _curves(
            _in_ranges(a.frame, [common]),
            _in_ranges(b.frame, [common]),
            window_ends,
            common,
            inc_ranges,
            (common["start"], common["start"]),
        )
    curves_inc = None
    if inc_run is not None:
        run = a if inc_run == "a" else b
        mine = _in_ranges(run.frame, inc_ranges)
        empty = run.frame.iloc[0:0]
        anchor = inc_ranges[0]["start"]
        fa, fb = (mine, empty) if inc_run == "a" else (empty, mine)
        anchors = (anchor, None) if inc_run == "a" else (None, anchor)
        curves_inc = _curves(fa, fb, ends, common, inc_ranges, anchors)
    return full, curves_common, curves_inc


# ---- entry point ----------------------------------------------------------------------------


def compare_run_folders(folder_a: Path, folder_b: Path) -> dict[str, Any]:
    """Compare two run folders and return a RunComparison (schema 1).

    Args:
        folder_a: The earlier (previous) run folder.
        folder_b: The later (current) run folder.

    Raises:
        CompareError: Either folder has no valid manifest.json or no readable
            evidence/trades.parquet.
    """
    a, b = _Run(folder_a), _Run(folder_b)
    pa, pb = a.period, b.period
    common = _common(pa, pb)
    extra = {"a": _extra(pa, common), "b": _extra(pb, common)}
    if pa == pb:
        extra = {"a": [], "b": []}

    full = {
        "a": metrics_from_frame(a.frame, a.n_trials),
        "b": metrics_from_frame(b.frame, b.n_trials),
    }
    common_view = None
    if common is not None:
        common_view = {
            "a": metrics_from_frame(_within(a.frame, common), a.n_trials),
            "b": metrics_from_frame(_within(b.frame, common), b.n_trials),
        }
    incremental = None
    inc_run = _incremental_run(pa, pb, extra)
    inc_ranges = extra[inc_run] if inc_run is not None else []
    if inc_run is not None:
        run = a if inc_run == "a" else b
        incremental = {
            "run": inc_run,
            "ranges": inc_ranges,
            "metrics": metrics_from_frame(_in_ranges(run.frame, inc_ranges), run.n_trials),
        }
    curves, curves_common, curves_incremental = _curve_sets(a, b, common, inc_run, inc_ranges)

    return {
        "schema": SCHEMA,
        "change_classes": change_class.classify(a.manifest, b.manifest),
        "strategy": _strategy_diff(a, b),
        "scope": _scope_diff(a.scope, b.scope),
        "dataset": _dataset_diff(a.manifest, b.manifest),
        "execution": _execution_diff(a, b),
        "cost": _cost_diff(a, b),
        "performance": _deltas(full["a"], full["b"]),
        "performance_common": (
            _deltas(common_view["a"], common_view["b"]) if common_view is not None else None
        ),
        "overlap": {
            "periods": {"a": _range(*pa), "b": _range(*pb)},
            "common": common,
            "extra": extra,
            "warning": _warning(pa, pb, common, extra),
        },
        "views": {"full": full, "common": common_view, "incremental": incremental},
        "curves": curves,
        "curves_common": curves_common,
        "curves_incremental": curves_incremental,
    }
