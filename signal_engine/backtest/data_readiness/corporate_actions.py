"""Corporate-action layer: NSE announcements -> price and volume factors (plan revision 4, D22-D24).

Raw NSE prices are never changed. This module turns the NSE corporate-action announcements into
events with a price factor and a volume factor, and computes, for each (symbol, session), the
backward-adjustment factor: the product of the factors of every event whose ex-date is after the
session. Adjusted price = raw price * price_factor; adjusted volume = raw volume / volume_factor.

Factor methods (MVP):
    split "From Rs A To Rs B"        price B/A, volume B/A
    bonus "a:b" (a new for b held)   price b/(a+b), volume b/(a+b)
    rights "a:b @ Premium Rs P"      S = face value + P, C = official close before the ex-date,
                                     TERP = (b*C + a*S)/(a+b), price TERP/C (1 when S >= C), volume 1
    demerger                         official ex-date open / official previous close (owner decision
                                     2026-10-05); an APPROXIMATION: the open also carries that
                                     morning's market move. Volume 1. A ratio not below 1 is
                                     unresolved.
    bonus debentures / NCRPS         not adjusted (a non-equity distribution, like a dividend)
    dividends                        not adjusted (documented MVP limitation)

An announcement that looks price-affecting but cannot be read is UNRESOLVED; the caller must block
publication until an override resolves it. Unknown subjects are UNRESOLVED too, never ignored.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from signal_engine.research.intake.fingerprints import canonical_json, sha256_hex

SPLIT, BONUS, RIGHTS, DEMERGER = "split", "bonus", "rights", "demerger"
#: Override methods: a fixed factor with a source, or a named NSE-data method with its inputs.
FIXED, OPEN_OVER_PREV_CLOSE, RIGHTS_TRANCHES = "fixed", "open_over_prev_close", "rights_tranches"
_METHODS = (FIXED, OPEN_OVER_PREV_CLOSE, RIGHTS_TRANCHES)
NON_EQUITY, IGNORED, UNRESOLVED = "non_equity_distribution", "ignored", "unresolved"

DEFAULT_OVERRIDES = (Path(__file__).resolve().parents[2] / "research" / "reference"
                     / "corporate_action_overrides.yaml")

#: Non-price subjects, including the misspellings found in the NSE feed (2026-10-04 snapshot):
#: "Div-Rs", "Divdend", "Dividned", "Genaral Meeting".
_IGNORE = re.compile(r"divid|divdend|dividned|\bdiv\b|\bdiv-|gen[ae]ral meeting|\bagm\b|\begm\b"
                     r"|interest|redemption|buy[\s-]?back|book closure|registrar|transfer agent")
_PRICE_WORDS = re.compile(r"bonus|split|sub-division|rights|consolidat|reduction|arrangement"
                          r"|amalgamation|merger")
_NON_EQUITY = re.compile(r"bonus.*(debenture|ncrps|preference)|(debenture|ncrps|preference).*bonus")
_NUM = r"(\d[\d,]*(?:\.\d+)?|\.\d+)"
_BONUS = re.compile(r"^\s*bonus\s*(\d+)\s*:\s*(\d+)\s*$")
_SPLIT = re.compile(r"fro?m\s*(?:rs|re)\.?\s*" + _NUM + r"\s*/?-?\s*(?:per\s*share\s*)?"
                    r"to\s*(?:rs|re)\.?\s*" + _NUM)
_RIGHTS = re.compile(r"rights\s*(\d+)\s*:\s*(\d+)\s*@\s*prem(?:ium)?\s*(?:of\s*)?rs\.?\s*"
                     + _NUM)
#: Parts of a compound subject are joined by "/" (but "Rs 10/-" is an amount, not a separator).
_PART_SEP = re.compile(r"/(?!-)")
_NOTE = re.compile(r"\((?:revised|corrected|amended)\)")


def _num(text: str) -> float:
    return float(text.replace(",", ""))


class CorporateActionError(ValueError):
    """A corporate-action input or factor is invalid."""


@dataclass(frozen=True)
class Parsed:
    kind: str
    params: tuple = ()


@dataclass(frozen=True)
class Event:
    symbol: str
    ex_date: date
    kind: str
    price_factor: float
    volume_factor: float
    method: str
    source: str


@dataclass(frozen=True)
class Unresolved:
    symbol: str
    ex_date: date
    kind: str
    subject: str
    reason: str


@dataclass(frozen=True)
class Override:
    """An auditable correction for one announcement. `match_kind` is the parsed kind it applies
    to (default: `kind`); `kind` is what the event becomes. `method` FIXED uses the factors;
    the other methods compute the factor from official NSE prices and `params`."""

    symbol: str
    ex_date: date
    kind: str
    price_factor: float | None
    volume_factor: float | None
    source: str
    match_kind: str | None = None
    method: str = FIXED
    params: tuple = ()

    def __post_init__(self) -> None:
        if self.match_kind is None:
            object.__setattr__(self, "match_kind", self.kind)


def parse_subject(subject: str) -> Parsed:
    """Classify one NSE announcement subject."""
    s = " ".join(subject.lower().split())
    if _NON_EQUITY.search(s):
        return Parsed(NON_EQUITY)
    if "demerger" in s:
        return Parsed(DEMERGER)
    m = _BONUS.match(s)
    if m:
        return Parsed(BONUS, (int(m.group(1)), int(m.group(2))))
    if "split" in s or "splt" in s or "sub-division" in s:
        m = _SPLIT.search(s)
        if m and "bonus" not in s:      # a combined subject must be split into two by hand
            return Parsed(SPLIT, (_num(m.group(1)), _num(m.group(2))))
        return Parsed(UNRESOLVED)
    m = _RIGHTS.search(s)
    if m:
        return Parsed(RIGHTS, (int(m.group(1)), int(m.group(2)), _num(m.group(3))))
    if _PRICE_WORDS.search(s):
        return Parsed(UNRESOLVED)
    if _IGNORE.search(s):
        return Parsed(IGNORED)
    return Parsed(UNRESOLVED)


def parse_announcement(subject: str) -> list[Parsed]:
    """Every part of a possibly compound subject ("Bonus 1:1/Face Value Split ..."), in order."""
    text = _NOTE.sub("", subject.lower())
    return [parse_subject(part) for part in _PART_SEP.split(text) if part.strip()]


def ex_date_of(ann: dict) -> date | None:
    """The ex-date, or None when NSE lists none ("-")."""
    if str(ann.get("exDate", "")).strip() in ("", "-"):
        return None
    return _ex_date(ann)


def _ex_date(ann: dict) -> date:
    return pd.to_datetime(ann["exDate"], format="%d-%b-%Y").date()


def _event(ann: dict, p: Parsed, prev_close) -> Event | str:
    """The event for one parsed announcement, or the reason it cannot be computed."""
    sym, ex = ann["symbol"], _ex_date(ann)
    if p.kind == SPLIT:
        old, new = p.params
        f = new / old
        return Event(sym, ex, SPLIT, f, f, f"ratio new/old face value {new:g}/{old:g}", "nse")
    if p.kind == BONUS:
        a, b = p.params
        f = b / (a + b)
        return Event(sym, ex, BONUS, f, f, f"ratio b/(a+b) for bonus {a}:{b}", "nse")
    if p.kind == RIGHTS:
        a, b, premium = p.params
        try:
            face = float(ann.get("faceVal"))
        except (TypeError, ValueError):
            return "no face value"
        c = prev_close(sym, ex)
        if not c or c <= 0:
            return "no official close before the ex-date"
        s = face + premium
        if s >= c:
            return Event(sym, ex, RIGHTS, 1.0, 1.0, f"rights at {s:g} >= close {c:g}: no effect",
                         "nse")
        terp = (b * c + a * s) / (a + b)
        return Event(sym, ex, RIGHTS, terp / c, 1.0,
                     f"TERP ({b}*{c:g} + {a}*{s:g})/{a + b} over close {c:g}", "nse")
    return "no factor method for this kind"


def _demerger(sym: str, ex: date, prev_close, ex_open, source: str) -> Event | str:
    c = prev_close(sym, ex)
    o = ex_open(sym, ex) if ex_open else None
    if not c or not o or c <= 0 or o <= 0:
        return "no official ex-date open or previous close"
    if o >= c:
        return f"ex-date open {o:g} is not below the previous close {c:g}"
    return Event(sym, ex, DEMERGER, o / c, 1.0,
                 f"NSE ex-date open {o:g} / previous close {c:g} (approximation: includes the "
                 "ex-date market move)", source)


def _rights_tranches(o: Override, prev_close) -> Event | str:
    held, tranches = o.params
    c = prev_close(o.symbol, o.ex_date)
    if not c or c <= 0:
        return "no official close before the ex-date"
    new = sum(n for n, _ in tranches)
    terp = (held * c + sum(n * price for n, price in tranches)) / (held + new)
    parts = " + ".join(f"{n}*{price:g}" for n, price in tranches)
    return Event(o.symbol, o.ex_date, o.kind, terp / c, 1.0,
                 f"TERP ({held}*{c:g} + {parts})/{held + new} over close {c:g}", o.source)


def _override_event(o: Override, prev_close, ex_open) -> Event | str:
    if o.method == FIXED:
        return Event(o.symbol, o.ex_date, o.kind, o.price_factor, o.volume_factor, "override",
                     o.source)
    if o.method == OPEN_OVER_PREV_CLOSE:
        return _demerger(o.symbol, o.ex_date, prev_close, ex_open, o.source)
    return _rights_tranches(o, prev_close)


def build_events(announcements: Iterable[dict], *, prev_close: Callable[[str, date], float | None],
                 ex_open: Callable[[str, date], float | None] | None = None,
                 overrides: Sequence[Override] = ()) -> tuple[list[Event], list[Unresolved]]:
    """Events with factors, and the announcements that could not be resolved.

    `prev_close(symbol, ex_date)` returns the official close of the session before the ex-date.
    An override for (symbol, ex_date, kind) replaces the computed factor.
    """
    by_key = {(o.symbol, o.ex_date, o.match_kind): o for o in overrides}
    events: list[Event] = []
    unresolved: list[Unresolved] = []
    seen: dict[tuple, tuple] = {}
    for ann in announcements:
        parts = [p for p in parse_announcement(ann["subject"])
                 if p.kind not in (IGNORED, NON_EQUITY)]
        sym, ex = ann["symbol"], ex_date_of(ann)
        if parts and ex is None:
            unresolved.append(Unresolved(sym, None, parts[0].kind, ann["subject"], "no ex-date"))
            continue
        if any(p.kind == UNRESOLVED for p in parts):
            parts = [Parsed(UNRESOLVED)]          # one unreadable part makes the whole unsafe
        for p in parts:
            _add(ann, sym, ex, p, seen, by_key, prev_close, ex_open, events, unresolved)
    return events, unresolved


def _add(ann, sym, ex, p: Parsed, seen, by_key, prev_close, ex_open, events, unresolved) -> None:
    """Add one parsed part as an event or an unresolved entry. NSE repeats announcements, so the
    same action counts once; two different actions of one kind on one ex-date cannot be told
    apart, so they stop the build."""
    key = (sym, ex, p.kind)
    if key in seen:
        if seen[key] != p.params:
            raise CorporateActionError(f"{sym} {ex}: conflicting {p.kind} announcements "
                                       f"{seen[key]} and {p.params}")
        return
    seen[key] = p.params
    o = by_key.get(key)
    if o is not None:
        result = _override_event(o, prev_close, ex_open)
    elif p.kind == DEMERGER:
        result = _demerger(sym, ex, prev_close, ex_open, "nse")
    elif p.kind == UNRESOLVED:
        result = "unreadable subject"
    else:
        result = _event(ann, p, prev_close)
    if isinstance(result, Event):
        events.append(result)
    else:
        unresolved.append(Unresolved(sym, ex, p.kind, ann["subject"], result))


def factor_table(events: Sequence[Event]) -> pd.DataFrame:
    """One row per (symbol, ex_date): the product of that day's event factors."""
    for e in events:
        for f in (e.price_factor, e.volume_factor):
            if not np.isfinite(f) or not 0 < f <= 1:
                raise CorporateActionError(f"{e.symbol} {e.ex_date} {e.kind}: factor {f} is "
                                           "outside (0, 1]")
    if not events:
        return pd.DataFrame(columns=["price_factor", "volume_factor"],
                            index=pd.MultiIndex.from_tuples([], names=["symbol", "ex_date"]))
    ordered = sorted(events, key=lambda e: (e.symbol, e.ex_date, e.kind, e.price_factor))
    df = pd.DataFrame([(e.symbol, e.ex_date, e.price_factor, e.volume_factor) for e in ordered],
                      columns=["symbol", "ex_date", "price_factor", "volume_factor"])
    return df.groupby(["symbol", "ex_date"]).prod()


def apply_factors(sessions: pd.DataFrame, events: Sequence[Event]) -> pd.DataFrame:
    """A copy of `sessions` (columns symbol, session) with the backward-adjustment factors."""
    table = factor_table(events)
    out = sessions.reset_index(drop=True)
    out["price_factor"] = 1.0
    out["volume_factor"] = 1.0
    for sym, idx in out.groupby("symbol").groups.items():
        if sym not in table.index.get_level_values(0):
            continue
        t = table.loc[sym].sort_index()
        ex = np.array(t.index, dtype="datetime64[D]")
        # suffix products: the factor of every event at position i or later
        pf = np.cumprod(t["price_factor"].to_numpy()[::-1])[::-1]
        vf = np.cumprod(t["volume_factor"].to_numpy()[::-1])[::-1]
        days = np.array(out.loc[idx, "session"].tolist(), dtype="datetime64[D]")
        pos = np.searchsorted(ex, days, side="right")      # first event with ex_date > session
        has = pos < len(ex)
        p = np.ones(len(days))
        v = np.ones(len(days))
        p[has], v[has] = pf[pos[has]], vf[pos[has]]
        out.loc[idx, "price_factor"] = p
        out.loc[idx, "volume_factor"] = v
    return out


def events_version(events: Sequence[Event]) -> str:
    """Content version of the event list (order-independent)."""
    rows = sorted([e.symbol, e.ex_date.isoformat(), e.kind, repr(float(e.price_factor)),
                   repr(float(e.volume_factor)), e.method, e.source] for e in events)
    return sha256_hex(canonical_json(rows))


def _override(i: int, e: dict) -> Override:
    for k in ("symbol", "ex_date", "kind", "method", "source"):
        if k not in e or not str(e[k]).strip():
            raise CorporateActionError(f"override {i} needs a non-empty {k}")
    method = str(e["method"])
    if method not in _METHODS:
        raise CorporateActionError(f"override {i}: unknown method {method!r}")
    pf = vf = None
    params: tuple = ()
    if method == FIXED:
        if "price_factor" not in e or "volume_factor" not in e:
            raise CorporateActionError(f"override {i}: method fixed needs both factors")
        pf, vf = float(e["price_factor"]), float(e["volume_factor"])
    elif method == RIGHTS_TRANCHES:
        if "held" not in e or not e.get("tranches"):
            raise CorporateActionError(f"override {i}: rights_tranches needs held and tranches")
        params = (int(e["held"]), tuple((int(n), float(px)) for n, px in e["tranches"]))
    return Override(str(e["symbol"]), pd.Timestamp(str(e["ex_date"])).date(), str(e["kind"]),
                    pf, vf, str(e["source"]), match_kind=e.get("match_kind"), method=method,
                    params=params)


def load_overrides(path: str | Path = DEFAULT_OVERRIDES) -> list[Override]:
    """Overrides from YAML (versioned in git). Every entry needs symbol, ex_date, kind, method and
    a source; the method's own inputs are checked here, so a bad entry fails fast."""
    p = Path(path)
    if not p.is_file():
        return []
    doc = yaml.safe_load(p.read_text()) or {}
    return [_override(i, e) for i, e in enumerate(doc.get("overrides") or [])]


# ---------------------------------------------------------------- NSE feed snapshot

FEED_URL = "https://www.nseindia.com/api/corporates-corporateActions"
_FEED_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 "
                               "Safari/537.36", "Accept": "application/json",
                 "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions"}

FeedFetch = Callable[[dict], tuple[int, list | None]]


def _months(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
        yield max(cur, start), min(nxt - timedelta(days=1), end)
        cur = nxt


def fetch_feed(start: date, end: date, *, fetch: FeedFetch, pause: float = 0.0) -> list[dict]:
    """Every equity announcement with an ex-date in [start, end], one query per month.

    Date-range queries are required: the NSE answer to a per-symbol query without dates is a
    truncated recent list (it missed TCS 2018-05-31 and HCLTECH 2019-12-05 bonuses).
    """
    import time

    rows: list[dict] = []
    seen: set[str] = set()
    for lo, hi in _months(start, end):
        params = {"index": "equities", "from_date": lo.strftime("%d-%m-%Y"),
                  "to_date": hi.strftime("%d-%m-%Y")}
        status, data = fetch(params)
        if status != 200 or data is None:
            raise CorporateActionError(f"NSE corporate-action feed HTTP {status} for {params}")
        for row in data:
            key = canonical_json(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if pause:
            time.sleep(pause)
    return rows


def http_feed_fetch(params: dict) -> tuple[int, list | None]:
    from utils.httpx_client import get_httpx_client

    r = get_httpx_client().get(FEED_URL, params=params, headers=_FEED_HEADERS, timeout=60.0)
    return r.status_code, (r.json() if r.status_code == 200 else None)


def save_snapshot(rows: list[dict], path: str | Path) -> tuple[Path, str]:
    """Write the feed rows once (never overwritten) and return the path and its sha256."""
    import hashlib
    import os

    p = Path(path)
    if p.exists():
        raise CorporateActionError(f"{p} exists; a feed snapshot is written once")
    blob = canonical_json(rows).encode()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, p)
    return p, hashlib.sha256(blob).hexdigest()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.corporate_actions")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True, help="new JSON snapshot, e.g. db/landing/nse/ca/...")
    ap.add_argument("--pause", type=float, default=1.0)
    args = ap.parse_args(argv)
    rows = fetch_feed(date.fromisoformat(args.start), date.fromisoformat(args.end),
                      fetch=http_feed_fetch, pause=args.pause)
    path, sha = save_snapshot(rows, args.out)
    print(f"{len(rows)} announcements -> {path} sha256 {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
