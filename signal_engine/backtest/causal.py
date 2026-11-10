"""Look-ahead protection for strategy hooks: a Ctx whose columns refuse to be read past a bound.

The engines hand a strategy hook the whole `Ctx`, so `c["Close"][i + 1]` used to run silently.
`GuardedCtx` wraps a `Ctx` so that every column is a `GuardedArray`: a scalar index, a negative
index or a slice end beyond the bound the engine set for the current hook raises `LookAheadError`
naming the hook, the column, the requested index and the allowed bound. Slices inside the bound
are plain ndarrays. A whole-array operation (`.mean()`, `.sum()`, `np.mean(col)`, a comparison,
iteration) reads every bar including the future and raises too. `Ctx.index`, `Ctx.n`, `len(col)`
and `col.shape` reveal no prices and stay available.

Bound per hook (set by the engines before each call; `i` is the hook's bar argument):

  entry, on_bar, on_entry (intraday), custom_exit, on_exit   bars <= i (bar i is closed)
  trail                                                       bars <= i, where the engine passes
                                                              j - 1 while managing bar j
  target (dynamic)                                            bars <= j - 1
  on_fill, on_fill_skipped                                    bars <= i - 1, plus Open[i]
                                                              (the fill price is the open)
  swing entry_at_open / exit_at_open / on_entry               bars <= i - 1, plus Open[i], plus
                                                              prepared (non-OHLCV) columns at i

The swing exception is the documented swing idiom: `SwingStrategy.prepare` shifts everything that
must be known before today's open (`p_close`, `s_atr`, `target_prev`, ...), so a prepared column is
read at index i by design. High, Low, Close and Volume stay bounded at i - 1 there, which is the
rule `swing_base` states ("never c['High'][i], c['Low'][i] or c['Close'][i]"). Whether a prepared
column really is shifted is not visible at this level; that is `prepare()` and the truncation check.

What this cannot see: `prepare()` / `prepare_for()` run on the whole frame before the engine and
can leak inside an indicator (a centred window, a whole-series statistic). That is the job of
`truncation.truncation_check`, which is mandatory for a recorded run. Code that converts a column
to a fresh array first (`np.asarray(col)`, `np.array(col)`) is not intercepted; the guard catches
honest mistakes, not deliberate evasion.
"""

from __future__ import annotations

import numpy as np

from signal_engine.backtest.types import Ctx


class LookAheadError(RuntimeError):
    """A strategy hook read a bar it could not have known at that point."""


_RAW_PRICE = frozenset(("High", "Low", "Close", "Volume"))


class GuardState:
    """The read bound shared by every column of one GuardedCtx; the engine sets it per hook."""

    __slots__ = ("bound", "derived_extra", "hook", "open_extra", "symbol")

    def __init__(self, symbol: str = "") -> None:
        self.hook = "setup"
        self.bound = -1
        self.open_extra = 0
        self.derived_extra = 0
        self.symbol = symbol

    def set(self, hook: str, bound: int, open_extra: int = 0, derived_extra: int = 0) -> None:
        self.hook = hook
        self.bound = bound
        self.open_extra = open_extra
        self.derived_extra = derived_extra


def _blocked(name: str):
    def method(self, *args, **kwargs):
        self._whole(f".{name}()")

    method.__name__ = name
    return method


class GuardedArray(np.ndarray):
    """A column that raises LookAheadError for any read beyond the current bound."""

    def __new__(cls, raw: np.ndarray, state: GuardState, name: str):
        obj = raw.view(cls)
        obj._raw = raw
        obj._st = state
        obj._name = name
        # 0: raw price (High/Low/Close/Volume), 1: Open, 2: prepared column
        obj._slot = 1 if name == "Open" else 0 if name in _RAW_PRICE else 2
        return obj

    def __array_finalize__(self, obj) -> None:
        if obj is not None:
            self._raw = getattr(obj, "_raw", None)
            self._st = getattr(obj, "_st", None)
            self._name = getattr(obj, "_name", "")
            self._slot = getattr(obj, "_slot", 0)

    # ---- failure -----------------------------------------------------------------------
    def _fail(self, requested, limit: int):
        st = self._st
        raise LookAheadError(
            f"look-ahead in {st.hook}: column {self._name!r} index {requested} requested, "
            f"allowed bound {limit}" + (f" (symbol {st.symbol})" if st.symbol else "")
        )

    def _whole(self, what: str):
        st = self._st
        raise LookAheadError(
            f"look-ahead in {st.hook}: column {self._name!r} read as a whole array via {what}; "
            f"it includes bars after the allowed bound {st.bound}. Slice [a:i + 1] instead"
        )

    # ---- reads -------------------------------------------------------------------------
    def __getitem__(self, k):
        st = self._st
        slot = self._slot
        limit = (
            st.bound if slot == 0 else st.bound + (st.open_extra if slot == 1 else st.derived_extra)
        )
        tp = type(k)
        if tp is int or tp is np.int64 or tp is np.intp:
            if 0 <= k <= limit:
                return self._raw[k]
            self._fail(int(k), limit)
        if tp is slice:
            start, stop, step = k.indices(len(self._raw))
            if step > 0:
                last = start + ((stop - start - 1) // step) * step if start < stop else None
            else:
                last = start if start > stop else None
            if last is None or 0 <= last <= limit:
                return self._raw[k]
            self._fail(f"slice end {last}", limit)
        if isinstance(k, np.integer):
            return self.__getitem__(int(k))
        if isinstance(k, (list, tuple, np.ndarray)):
            idx = np.asarray(k)
            if idx.dtype.kind in "iu":
                if idx.size == 0 or (idx.min() >= 0 and idx.max() <= limit):
                    return self._raw[idx]
                self._fail(f"indices up to {int(idx.max())}", limit)
        self._fail(f"{k!r} (unsupported index on a guarded column)", limit)

    def __len__(self) -> int:
        return len(self._raw)

    def __iter__(self):
        self._whole("iteration")

    def __contains__(self, item) -> bool:
        self._whole("'in'")

    def __repr__(self) -> str:
        return f"GuardedArray({self._name!r}, n={len(self._raw)})"

    __str__ = __repr__

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        self._whole(f"np.{ufunc.__name__}")

    def __array_function__(self, func, types, args, kwargs):
        self._whole(f"np.{func.__name__}")


for _name in (
    "all",
    "any",
    "argmax",
    "argmin",
    "argsort",
    "astype",
    "clip",
    "compress",
    "copy",
    "cumprod",
    "cumsum",
    "diagonal",
    "dot",
    "fill",
    "flatten",
    "item",
    "max",
    "mean",
    "min",
    "nonzero",
    "prod",
    "ptp",
    "put",
    "ravel",
    "repeat",
    "reshape",
    "round",
    "searchsorted",
    "sort",
    "std",
    "sum",
    "take",
    "tolist",
    "tobytes",
    "trace",
    "var",
    "view",
):
    setattr(GuardedArray, _name, _blocked(_name))
del _name


class GuardedCtx(Ctx):
    """A `Ctx` for strategy hooks. `raw` is the plain Ctx the engine itself reads."""

    __slots__ = ("raw", "state")

    def __init__(self, ctx: Ctx) -> None:  # noqa: D107 - mirrors Ctx, which takes a frame
        if isinstance(ctx, GuardedCtx):
            ctx = ctx.raw
        self.raw = ctx
        self.state = GuardState(ctx.symbol)
        self.a = {k: (v if v is None else GuardedArray(v, self.state, k)) for k, v in ctx.a.items()}
        self.index = ctx.index
        self.n = ctx.n
        self.symbol = ctx.symbol
