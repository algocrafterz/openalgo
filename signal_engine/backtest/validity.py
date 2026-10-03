"""Run validity checks: data quality, config, lineage and look-ahead (truncation).

`run_validity_checks` returns the `validity` block the verdict consumes:
`{ok, failures: [{check, detail}], checks_run}`. `checks_run` lists the checks that actually
executed, so a skipped check is visible and is never counted as passed. Failures are ordered
by check (data_quality, config, lineage, truncation) and then deterministically within a
check. Bad input shapes are failures, never exceptions.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from signal_engine.backtest.trades_io import trades_to_frame

CHECK_ORDER = ("data_quality", "config", "lineage", "truncation")
_MAX_DQ_ITEMS = 3
_TRADE_ID_COLS = ("signal_id", "order_id", "entry_fill_id", "exit_fill_id", "position_id")


def _fail(check: str, detail: str) -> dict:
    return {"check": check, "detail": detail}


# ---- data quality -----------------------------------------------------------------------


def _check_data_quality(dq) -> list[dict]:
    if not hasattr(dq, "get"):
        return [_fail("data_quality", f"data quality report is not a mapping: {type(dq).__name__}")]
    if not dq.get("affects_validity"):
        return []
    items = dq.get("items") or []
    if not isinstance(items, (list, tuple)):
        items = [items]
    shown = "; ".join(str(i) for i in items[:_MAX_DQ_ITEMS]) or "see data quality"
    more = f" (+{len(items) - _MAX_DQ_ITEMS} more)" if len(items) > _MAX_DQ_ITEMS else ""
    return [_fail("data_quality", f"data quality affects validity: {shown}{more}")]


# ---- config -----------------------------------------------------------------------------


def _check_config(intent, profiles_dir) -> list[dict]:
    from signal_engine.research.intake.spec import validate_spec

    try:
        problems = validate_spec(intent, Path(profiles_dir) if profiles_dir else None)
    except Exception as exc:  # a spec that cannot be validated is itself invalid
        return [_fail("config", f"intent could not be validated: {exc}")]
    return [_fail("config", str(p)) for p in problems if p.kind in ("validity", "intake")]


# ---- lineage ----------------------------------------------------------------------------


def _ts(value):
    return None if value is None or pd.isna(value) else pd.Timestamp(value)


def _dupes(frame, col, name) -> list[dict]:
    ids = frame[col]
    dup = ids[ids.duplicated(keep="first")]
    if len(dup):
        return [_fail("lineage", f"{name} id {dup.iloc[0]} is not unique")]
    return []


def _ledger_frames(ledgers) -> dict:
    try:
        out = {k: ledgers[k] for k in ("signals", "orders", "fills")}
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError(f"ledgers must hold signals, orders and fills ({exc!r})") from exc
    for name, frame in out.items():
        if not isinstance(frame, pd.DataFrame):
            raise ValueError(f"{name} ledger is not a frame")
    need = {
        "signals": ["signal_id", "bar_time"],
        "orders": ["order_id", "signal_id"],
        "fills": ["fill_id", "order_id", "position_id", "symbol", "fill_time", "fill_price"]
        + ["kind"],
    }
    for name, cols in need.items():
        missing = [c for c in cols if c not in out[name].columns]
        if missing:
            raise ValueError(f"{name} ledger lacks column(s) {', '.join(missing)}")
    return out


def _as_trade_frame(trades) -> pd.DataFrame:
    if isinstance(trades, pd.DataFrame):
        frame = trades
    else:
        frame = trades_to_frame(list(trades))
    missing = [c for c in ("symbol", "entry_time", "exit_time") if c not in frame.columns]
    if missing:
        raise ValueError(f"trades lack column(s) {', '.join(missing)}")
    return frame


def _ledger_integrity(led) -> list[dict]:
    out = _dupes(led["signals"], "signal_id", "signal")
    out += _dupes(led["orders"], "order_id", "order")
    out += _dupes(led["fills"], "fill_id", "fill")
    known = set(led["signals"]["signal_id"])
    for _, row in led["orders"].iterrows():
        if row["signal_id"] not in known:
            out.append(_fail("lineage", f"order {row['order_id']} references missing signal"))
            break
    orders = set(led["orders"]["order_id"])
    for _, row in led["fills"].iterrows():
        if row["order_id"] not in orders:
            out.append(_fail("lineage", f"fill {row['fill_id']} references missing order"))
            break
    for _, row in led["fills"].iterrows():
        price = row["fill_price"]
        ok = isinstance(price, (int, float)) or hasattr(price, "__float__")
        if not ok or not math.isfinite(float(price)) or float(price) <= 0:
            out.append(_fail("lineage", f"fill {row['fill_id']} has invalid price {price!r}"))
            break
    fills = led["fills"]
    entry_pos = set(fills.loc[fills["kind"] == "entry", "position_id"])
    for _, row in fills[fills["kind"] == "exit"].iterrows():
        if row["position_id"] not in entry_pos:
            out.append(_fail("lineage", f"exit fill {row['fill_id']} has no entry fill"))
            break
    return out


def _trade_references(trades, led) -> list[dict]:
    out: list[dict] = []
    sig_t = dict(zip(led["signals"]["signal_id"], led["signals"]["bar_time"], strict=False))
    orders = set(led["orders"]["order_id"])
    fills = led["fills"].drop_duplicates("fill_id").set_index("fill_id")
    names = {
        "signal_id": set(sig_t),
        "order_id": orders,
        "entry_fill_id": set(fills.index),
        "exit_fill_id": set(fills.index),
        "position_id": set(led["fills"]["position_id"]),
    }
    for col in _TRADE_ID_COLS:
        if col not in trades.columns:
            out.append(_fail("lineage", f"trades carry no {col} column"))
            continue
        for tid, val in zip(trades.index, trades[col], strict=True):
            if val is None or (not isinstance(val, str) and pd.isna(val)) or val not in names[col]:
                out.append(_fail("lineage", f"trade {tid} {col} {val!r} is not in the ledgers"))
                break
    if out:
        return out
    for tid, row in trades.iterrows():
        e, x = fills.loc[row["entry_fill_id"]], fills.loc[row["exit_fill_id"]]
        sig, ent, ext = _ts(sig_t[row["signal_id"]]), _ts(e["fill_time"]), _ts(x["fill_time"])
        if sig is not None and ent is not None and sig > ent:
            out.append(
                _fail(
                    "lineage",
                    f"trade {tid} entry fill {row['entry_fill_id']} is before its signal "
                    f"{row['signal_id']}",
                )
            )
        if ent is not None and ext is not None and ext < ent:
            out.append(
                _fail(
                    "lineage",
                    f"trade {tid} exit fill {row['exit_fill_id']} is before its entry fill",
                )
            )
    return out


def _overlaps(trades) -> list[dict]:
    out: list[dict] = []
    frame = pd.DataFrame(
        {
            "symbol": trades["symbol"].astype(str),
            "entry": pd.to_datetime(trades["entry_time"]),
            "exit": pd.to_datetime(trades["exit_time"]),
        }
    )
    for sym, grp in sorted(frame.groupby("symbol"), key=lambda kv: kv[0]):
        prev_exit = None
        for tid, row in grp.sort_values(["entry", "exit"], kind="mergesort").iterrows():
            if prev_exit is not None and row["entry"] < prev_exit:
                out.append(
                    _fail(
                        "lineage",
                        f"positions overlap for {sym}: trade {tid} entered "
                        f"{row['entry']} before the previous exit {prev_exit}",
                    )
                )
                break
            prev_exit = row["exit"] if prev_exit is None else max(prev_exit, row["exit"])
    return out


def _check_lineage(ledgers, trades) -> list[dict]:
    try:
        led = _ledger_frames(ledgers)
    except Exception as exc:
        return [_fail("lineage", f"malformed ledgers: {exc}")]
    try:
        out = _ledger_integrity(led)
        if trades is None:
            return [*out, _fail("lineage", "ledgers were given without trades")]
        frame = _as_trade_frame(trades)
        if len(frame):
            out += _trade_references(frame, led)
            out += _overlaps(frame)
        return out
    except Exception as exc:
        return [_fail("lineage", f"malformed lineage input: {exc}")]


# ---- truncation -------------------------------------------------------------------------


def _check_truncation(truncation) -> list[dict]:
    try:
        data = truncation.to_dict() if hasattr(truncation, "to_dict") else truncation
        mismatches = data.get("mismatches") or []
        out = []
        for m in mismatches:
            out.append(
                _fail(
                    "truncation",
                    f"{m.get('symbol')} cut {m.get('cut')}: {m.get('kind')}: {m.get('detail')}",
                )
            )
        if not out and data.get("ok") is False:
            out.append(_fail("truncation", "look-ahead check reported failure"))
        return out
    except Exception as exc:
        return [_fail("truncation", f"malformed truncation result: {exc}")]


def run_validity_checks(
    *,
    data_quality=None,
    intent=None,
    profiles_dir=None,
    ledgers=None,
    trades=None,
    truncation=None,
) -> dict:
    """Run every check whose input is given. See the module docstring for the result shape."""
    failures: list[dict] = []
    run: list[str] = []
    if data_quality is not None:
        run.append("data_quality")
        failures += _check_data_quality(data_quality)
    if intent is not None:
        run.append("config")
        failures += _check_config(intent, profiles_dir)
    if ledgers is not None:
        run.append("lineage")
        failures += _check_lineage(ledgers, trades)
    if truncation is not None:
        run.append("truncation")
        failures += _check_truncation(truncation)
    return {"ok": not failures, "failures": failures, "checks_run": run}
