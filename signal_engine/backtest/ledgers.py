"""Lineage ledgers: a recorder the engines call, and the frames it yields.

Every decision the simulator takes is recorded as a row, so a reader can walk from a
trade back to the signal that caused it, the order that carried it and the fills that
opened and closed it, and so that nothing is excluded silently (gate G0.5).

Design for speed. The hot loop only appends plain ints and tuples of bar indices to
per-symbol lists. Per-bar skips are one packed int each (bar index << 6 | reason code)
and become per-day aggregate exclusion rows. Timestamps, ids and DataFrames are built
once per symbol afterwards in `to_frames()`.

Fills record the NEXT BAR OPEN as-is. Slippage is a cost component, not a fill price
(plan decision 1); nothing here adds it to a fill.

Ids (plan decision 3), all deterministic:
    signal_id   = <symbol>|<signal bar ISO IST>|<direction>
    order_id    = O|<signal_id>
    fill_id     = F|<order_id>|<kind><n>      (entry1, exit1, exit2, ...)
    position_id = P|<entry fill_id>
"""

from __future__ import annotations

from array import array

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"

# ---- reason codes (plan decision 4) ------------------------------------------------

MAX_TRADES_DAY = "MAX_TRADES_DAY"
OPEN_WINDOW = "OPEN_WINDOW"
ENTRY_CUTOFF = "ENTRY_CUTOFF"
DIRECTION_DISABLED = "DIRECTION_DISABLED"
DIRECTION_ALREADY_TAKEN = "DIRECTION_ALREADY_TAKEN"
RISK_INVALID = "RISK_INVALID"
STOP_WRONG_SIDE = "STOP_WRONG_SIDE"
STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
POSITION_OPEN = "POSITION_OPEN"
NO_NEXT_BAR = "NO_NEXT_BAR"
ORDER_DROPPED_SESSION_END = "ORDER_DROPPED_SESSION_END"
ORDER_DROPPED_POSITION_OPEN = "ORDER_DROPPED_POSITION_OPEN"

# Reserved for later steps (concurrency, data and session layer); not raised by the engines.
POSITION_CAP = "POSITION_CAP"
SYMBOL_MIN_SESSIONS = "SYMBOL_MIN_SESSIONS"
SPLIT_DAY = "SPLIT_DAY"
BAD_BAR = "BAD_BAR"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
WARMUP = "WARMUP"

LIVE_CODES: tuple[str, ...] = (
    MAX_TRADES_DAY,
    OPEN_WINDOW,
    ENTRY_CUTOFF,
    DIRECTION_DISABLED,
    DIRECTION_ALREADY_TAKEN,
    RISK_INVALID,
    STOP_WRONG_SIDE,
    STOP_TOO_TIGHT,
    POSITION_OPEN,
    NO_NEXT_BAR,
    ORDER_DROPPED_SESSION_END,
    ORDER_DROPPED_POSITION_OPEN,
)
RESERVED_CODES: tuple[str, ...] = (
    POSITION_CAP,
    SYMBOL_MIN_SESSIONS,
    SPLIT_DAY,
    BAD_BAR,
    NOT_ELIGIBLE,
    WARMUP,
)
REASON_CODES: tuple[str, ...] = LIVE_CODES + RESERVED_CODES

#: code -> small int, used by the engines in the hot loop. Order is part of the contract.
CODE_INDEX: dict[str, int] = {c: k for k, c in enumerate(REASON_CODES)}

SCOPES = ("bar", "signal", "order", "symbol", "run")

_IST_OFFSET_NS = 19_800 * 1_000_000_000
_SHIFT = 6  # packed skip = (bar index << 6) | code int; 64 codes max
_MASK = (1 << _SHIFT) - 1
assert len(REASON_CODES) <= _MASK + 1

SIGNAL_COLS = [
    "signal_id",
    "symbol",
    "bar_time",
    "direction",
    "tag",
    "signal_price",
    "stop",
    "target",
    "risk",
    "accepted",
    "exclusion_id",
]
ORDER_COLS = [
    "order_id",
    "signal_id",
    "symbol",
    "submit_time",
    "order_type",
    "side",
    "fill_bar_time",
    "status",
    "exclusion_id",
]
FILL_COLS = [
    "fill_id",
    "order_id",
    "position_id",
    "symbol",
    "fill_time",
    "fill_price",
    "side",
    "kind",
    "exit_reason",
    "leg",
]
EXCLUSION_COLS = [
    "exclusion_id",
    "scope",
    "symbol",
    "date",
    "bar_time",
    "reason_code",
    "detail",
    "count",
]
_TIME_COLS = {"bar_time", "submit_time", "fill_bar_time", "fill_time"}


def is_known(code: str) -> bool:
    return code in CODE_INDEX


def code_index(code: str) -> int:
    try:
        return CODE_INDEX[code]
    except KeyError:
        raise ValueError(f"unknown exclusion reason code: {code!r}") from None


# ---- id builders -------------------------------------------------------------------


def signal_id(symbol: str, bar_time, direction: int) -> str:
    stamp = bar_time if isinstance(bar_time, str) else _ist(bar_time).isoformat()
    return f"{symbol}|{stamp}|{int(direction)}"


def order_id(sig_id: str) -> str:
    return f"O|{sig_id}"


def fill_id(ord_id: str, kind: str, n: int) -> str:
    return f"F|{ord_id}|{kind}{n}"


def position_id(entry_fill_id: str) -> str:
    return f"P|{entry_fill_id}"


def _ist(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)


def _side(direction: int, kind: int) -> str:
    """kind 0 = entry, 1 = exit. A long entry buys, a long exit sells."""
    return "BUY" if (direction == 1) == (kind == 0) else "SELL"


N_CODES = len(REASON_CODES)
_PER_DIRECTION = (CODE_INDEX["DIRECTION_DISABLED"], CODE_INDEX["DIRECTION_ALREADY_TAKEN"])
#: unit of one bar-scope count. The two direction skips are counted once per direction per bar
#: (a bar can add up to two); every other bar skip is once per bar. Counting is unchanged.
UNIT_DETAIL = tuple(
    "per direction per bar" if k in _PER_DIRECTION else "per bar" for k in range(N_CODES)
)


def _int_array(a) -> array:
    """Compact int array (8 bytes per element, no per-element objects kept alive)."""
    out = array("q")
    out.frombytes(np.ascontiguousarray(a, dtype=np.int64).tobytes())
    return out


_COUNT_BITS = 24
_COUNT_MASK = (1 << _COUNT_BITS) - 1


def _make_skip(di, cnt):
    # one list slot per (day, code): the low 24 bits count skips, the bits above hold the first
    # bar index that was skipped, so a single list read and write does everything
    def skip(i, code):
        k = di[i] * N_CODES + code
        v = cnt[k]
        cnt[k] = v + 1 if v else (i << _COUNT_BITS) | 1

    return skip


# ---- per-symbol buffer -------------------------------------------------------------


class SymbolBuffer:
    """What one engine run appends to. Plain lists of tuples and ints, no pandas."""

    __slots__ = (
        "symbol",
        "times",
        "days",
        "n_skipped",
        "agg",
        "skip",
        "_cnt",
        "_di",
        "_udays",
        "signals",
        "orders",
        "fills",
        "trade_pids",
        "_pending",
    )

    def __init__(self, symbol, times, days):
        self.symbol = symbol
        self.times = times
        self.days = days
        # Per-bar skips are counted per (day, code), never stored per bar: the memory is
        # O(days * codes). `skip(i, code)` increments; `finish()` turns the counters into
        # `agg` = (day values, code ints, counts, first bar index) and drops them.
        self.n_skipped = 0
        self.agg = (days[:0], np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64))
        chg = np.empty(len(days), dtype=bool)
        if len(days):
            chg[0] = True
            chg[1:] = days[1:] != days[:-1]
        self._udays = days[chg]
        self._di = _int_array(np.cumsum(chg) - 1)
        self._cnt = [0] * (len(self._udays) * N_CODES)
        self.skip = _make_skip(self._di, self._cnt)
        self.signals: list[tuple] = []  # (bar, dir, tag, signal_px, stop, target, risk, code|-1)
        self.orders: list[tuple] = []  # (signal bar, dir, fill bar|-1, code|-1)
        self.fills: list[tuple] = []  # (signal bar, dir, kind, bar, price, reason, leg)
        self.trade_pids: list[str] = []
        self._pending: list[tuple] = []

    # called by the engines; each is only reached when a recorder is attached
    def signal(self, i, d, tag, sig_px, stop, target, risk, code=-1):
        self.signals.append(
            (i, d, tag, float(sig_px), float(stop), float(target), float(risk), code)
        )

    def order(self, sig_i, d, fill_i, code=-1):
        self.orders.append((sig_i, d, fill_i, code))

    def entry_fill(self, sig_i, d, i, price):
        # next-bar open, as-is: slippage is a cost component, not a fill price (decision 1)
        self.fills.append((sig_i, d, 0, i, float(price), "", 1))

    def exit_fill(self, sig_i, d, i, price, reason, leg):
        self.fills.append((sig_i, d, 1, i, float(price), reason, leg))

    def iso_many(self, idxs) -> dict[int, str]:
        """ISO IST strings for the given bar indices, formatted in one vectorised pass."""
        uniq = sorted(set(idxs))
        if not uniq:
            return {}
        t = self.times[uniq]
        if t.tz is None:
            wall = t.as_unit("ns").asi8  # naive stamps are IST wall time
        else:
            wall = t.as_unit("ns").asi8 + _IST_OFFSET_NS  # asi8 is UTC; shift to IST wall time
        txt = np.datetime_as_string(wall.astype("datetime64[ns]").astype("datetime64[s]"))
        return {k: v + "+05:30" for k, v in zip(uniq, txt.tolist(), strict=True)}

    def iso(self, i: int) -> str:
        return self.iso_many([i])[i]

    def stamp(self, trade, sig_i, d, n_exits) -> None:
        """Queue a Trade for its lineage ids; `finish()` sets them after the loop."""
        self._pending.append((trade, sig_i, d, n_exits))

    def finish(self) -> None:
        """Off the hot loop, once per symbol: aggregate the skip counters and set lineage ids."""
        if self._cnt is not None:
            cnt = np.asarray(self._cnt, dtype=np.int64)
            nz = np.flatnonzero(cnt)
            self.n_skipped = int((cnt & _COUNT_MASK).sum())
            self.agg = (
                self._udays[nz // N_CODES],
                nz % N_CODES,
                cnt[nz] & _COUNT_MASK,
                cnt[nz] >> _COUNT_BITS,
            )
            self._cnt = self._di = self._udays = self.skip = None
        if not self._pending:
            return
        iso = self.iso_many([sig_i for _, sig_i, _, _ in self._pending])
        sym, pids = self.symbol, self.trade_pids
        for trade, sig_i, d, n_exits in self._pending:
            sid = f"{sym}|{iso[sig_i]}|{d}"
            oid = "O|" + sid
            entry = f"F|{oid}|entry1"
            trade.signal_id, trade.order_id = sid, oid
            trade.entry_fill_id = entry
            trade.exit_fill_id = f"F|{oid}|exit{n_exits}"
            pos_id = "P|" + entry
            trade.position_id = pos_id
            pids.append(pos_id)
        self._pending = []


# ---- recorder ----------------------------------------------------------------------


class LedgerRecorder:
    """Collects one recorded run. Attach to a single run, never to sweeps."""

    def __init__(self):
        self._buffers: dict[str, SymbolBuffer] = {}
        self._extra: list[tuple] = []

    def begin(self, c) -> SymbolBuffer:
        if c.symbol in self._buffers:
            raise ValueError(f"{c.symbol}: already recorded; a recorder serves one run")
        days = c["day"] if c.has("day") and c["day"] is not None else np.asarray(c.index.date)
        buf = SymbolBuffer(c.symbol, c.index, days)
        self._buffers[c.symbol] = buf
        return buf

    def add_exclusion(
        self,
        symbol: str,
        scope: str,
        reason_code: str,
        *,
        date=None,
        bar_time=None,
        detail: str = "",
        count: int = 1,
    ) -> None:
        """Record an exclusion made outside the engines (position caps, data layer)."""
        code_index(reason_code)
        if scope not in SCOPES:
            raise ValueError(f"unknown exclusion scope: {scope!r}")
        if date is not None:
            date = pd.Timestamp(date).date()
        self._extra.append((symbol, scope, reason_code, date, bar_time, detail, int(count)))

    # ---- frames ----

    def to_frames(self) -> dict[str, pd.DataFrame]:
        sig_rows, ord_rows, fill_rows, exc_rows, bar_frames = [], [], [], [], []
        for sym in sorted(self._buffers):
            self._frame_rows(
                self._buffers[sym], sig_rows, ord_rows, fill_rows, exc_rows, bar_frames
            )
        for k, (sym, scope, code, date, bt, detail, count) in enumerate(self._extra):
            bt = None if bt is None else _ist(bt)
            date = date if date is not None else (None if bt is None else bt.date())
            exc_rows.append(
                (_extra_id(k, sym, date, code, scope), scope, sym, date, bt, code, detail, count)
            )
        return {
            "signals": _frame(sig_rows, SIGNAL_COLS, ["symbol", "bar_time", "direction"]),
            "orders": _frame(ord_rows, ORDER_COLS, ["symbol", "submit_time", "signal_id"]),
            "fills": _frame(fill_rows, FILL_COLS, ["symbol", "fill_time", "order_id", "leg"]),
            "exclusions": _merge_exclusions(exc_rows, bar_frames),
        }

    def _frame_rows(self, b, sig_rows, ord_rows, fill_rows, exc_rows, bar_frames) -> None:
        sym, t = b.symbol, b.times
        b.finish()
        iso = b.iso_many(
            [r[0] for r in b.signals] + [r[0] for r in b.orders] + [r[0] for r in b.fills]
        )
        ts = lambda i: _ist(t[i])  # noqa: E731
        for i, d, tag, sp, stop, tp, risk, code in b.signals:
            sp, stop, tp, risk = float(sp), float(stop), float(tp), float(risk)
            sid = signal_id(sym, iso[i], d)
            xid = None
            if code >= 0:
                xid = f"X|{sid}"
                exc_rows.append(
                    (
                        xid,
                        "signal",
                        sym,
                        b.days[i],
                        ts(i),
                        _name(code),
                        f"signal_price={sp} stop={stop} risk={risk}",
                        1,
                    )
                )
            sig_rows.append((sid, sym, ts(i), d, tag, sp, stop, tp, risk, code < 0, xid))
        for i, d, fi, code in b.orders:
            sid = signal_id(sym, iso[i], d)
            oid = order_id(sid)
            xid = None
            if code >= 0:
                xid = f"X|{oid}"
                exc_rows.append(
                    (xid, "order", sym, b.days[i], ts(i), _name(code), "pending order dropped", 1)
                )
            ord_rows.append(
                (
                    oid,
                    sid,
                    sym,
                    ts(i),
                    "MARKET",
                    _side(d, 0),
                    ts(fi) if fi >= 0 else None,
                    "FILLED" if code < 0 else "DROPPED",
                    xid,
                )
            )
        for i, d, kind, bar, price, reason, leg in b.fills:
            price = float(price)
            oid = order_id(signal_id(sym, iso[i], d))
            fid = fill_id(oid, "entry" if kind == 0 else "exit", leg)
            pid = position_id(fill_id(oid, "entry", 1))
            fill_rows.append(
                (
                    fid,
                    oid,
                    pid,
                    sym,
                    ts(bar),
                    price,
                    _side(d, kind),
                    "entry" if kind == 0 else "exit",
                    reason or None,
                    leg,
                )
            )
        days_v, codes_v, counts_v, first_v = b.agg
        if len(codes_v):
            names = np.asarray(REASON_CODES, dtype=object)[codes_v]
            bt = t[first_v]
            bt = bt.tz_localize(IST) if bt.tz is None else bt.tz_convert(IST)
            bar_frames.append(
                pd.DataFrame(
                    {
                        "exclusion_id": [
                            f"X|{sym}|{d}|{nm}"
                            for d, nm in zip(days_v, names.tolist(), strict=True)
                        ],
                        "scope": "bar",
                        "symbol": sym,
                        "date": days_v,
                        "bar_time": bt,
                        "reason_code": names,
                        "detail": np.asarray(UNIT_DETAIL, dtype=object)[codes_v],
                        "count": counts_v.astype(int),
                    }
                )
            )

    # ---- reconciliation ----

    def reconcile(self, frames: dict[str, pd.DataFrame] | None = None, trades=None) -> list[str]:
        """Return the problems found; an empty list means the ledgers reconcile exactly.

        `frames` defaults to a fresh `to_frames()`; pass edited frames to check them.
        `trades` defaults to the position ids the engines stamped on their own trades.
        """
        f = self.to_frames() if frames is None else frames
        sig, ordr, fills, exc = f["signals"], f["orders"], f["fills"], f["exclusions"]
        pids = (
            list(self._all_trade_pids())
            if trades is None
            else [t.position_id for t in trades if t.position_id is not None]
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
        for code in exc["reason_code"].unique():
            if not is_known(code):
                out.append(f"exclusions: unknown reason code {code}")
        exc_ids = set(exc["exclusion_id"])
        sig_exc = sig["exclusion_id"]
        bad = sig[~sig["accepted"] & (sig_exc.isna() | ~sig_exc.isin(exc_ids))]
        if len(bad):
            out.append(f"signals: {len(bad)} rejected without an exclusion row")
        bad = sig[sig["accepted"] & sig_exc.notna()]
        if len(bad):
            out.append(f"signals: {len(bad)} accepted but carry an exclusion")
        accepted_ids = set(sig.loc[sig["accepted"], "signal_id"])
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
        entry_pids = set(entries["position_id"])
        if set(pids) != entry_pids or len(pids) != len(set(pids)):
            out.append(f"trades: {len(pids)} trades versus {len(entry_pids)} entry positions")
        no_exit = entry_pids - set(fills.loc[fills["kind"] == "exit", "position_id"])
        if no_exit:
            out.append(f"fills: {len(no_exit)} positions without an exit fill")
        # exact counts, against what the engines appended
        n_sig = sum(len(b.signals) for b in self._buffers.values())
        n_rej = sum(1 for b in self._buffers.values() for s in b.signals if s[7] >= 0)
        n_ord = sum(len(b.orders) for b in self._buffers.values())
        n_drop = sum(1 for b in self._buffers.values() for o in b.orders if o[3] >= 0)
        n_skip = sum(b.n_skipped for b in self._buffers.values())
        extra_ids = {
            _extra_id(k, sym, date, code, scope)
            for k, (sym, scope, code, date, _bt, _d, _n) in enumerate(self._extra)
        }
        exc = exc[~exc["exclusion_id"].isin(extra_ids)]
        engine_scopes = {
            s: int(exc.loc[exc["scope"] == s, "count"].sum()) for s in ("signal", "order", "bar")
        }
        for label, got, want in (
            ("signals", len(sig), n_sig),
            ("orders", len(ordr), n_ord),
            ("signal exclusions", engine_scopes["signal"], n_rej),
            ("order exclusions", engine_scopes["order"], n_drop),
            ("bar exclusions", engine_scopes["bar"], n_skip),
        ):
            if got != want:
                out.append(f"count: {label} {got} != recorded {want}")
        return out

    def _all_trade_pids(self):
        for sym in sorted(self._buffers):
            yield from self._buffers[sym].trade_pids


def _name(code: int) -> str:
    return REASON_CODES[code]


def _frame(rows: list[tuple], cols: list[str], sort_by: list[str]) -> pd.DataFrame:
    if not rows:
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in cols})
        for c in _TIME_COLS & set(cols):
            df[c] = pd.Series(dtype=f"datetime64[ns, {IST}]")
        return df
    df = pd.DataFrame(rows, columns=cols)
    for c in _TIME_COLS & set(cols):
        df[c] = pd.to_datetime(df[c], utc=True).dt.tz_convert(IST)
    return df.sort_values(sort_by, kind="mergesort").reset_index(drop=True)


def _extra_id(k, sym, date, code, scope) -> str:
    """Id of the k-th out-of-engine exclusion; the sequence number keeps repeats unique."""
    return f"X|{sym}|{date}|{code}|{scope}|{k}"


def _merge_exclusions(rows, bar_frames) -> pd.DataFrame:
    sort_by = ["symbol", "date", "bar_time", "reason_code", "exclusion_id"]
    other = _frame(rows, EXCLUSION_COLS, sort_by) if rows else None
    parts = ([other] if other is not None else []) + bar_frames
    if not parts:
        return _frame([], EXCLUSION_COLS, sort_by)
    if len(parts) == 1 and other is not None:
        return other
    df = pd.concat(parts, ignore_index=True)
    return df.sort_values(sort_by, kind="mergesort").reset_index(drop=True)
