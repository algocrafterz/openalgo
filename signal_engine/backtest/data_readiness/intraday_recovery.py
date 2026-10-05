"""Intraday build step 4: fetch the planned AngelOne 1m requests with a full provenance trail.

Every request of `requests.parquet` (intraday_plan) is fetched through the history service with
the in-memory secondary broker login (never `auth_db`). Each response is stored as
`<landing>/<request_id>.json.gz` (gzip level GZIP_LEVEL, mtime 0: equal content gives equal bytes) and every
attempt is one line of the append-only `recovery_ledger.jsonl` in the build folder:

    request_id, symbol, start, end, attempt, requested_ist, outcome (ok | empty | error),
    message, n_bars, sessions, path, sha256

An error is a ledger line, never a silent gap. The latest line of a request decides its state; a
rerun skips requests whose latest line is final (ok, empty) and retries errors up to
MAX_ATTEMPTS. The broker plugin turns a failed request (an expired session: "Invalid Token") into
an empty success, so an empty answer is checked against a canary request that must return data;
if the canary is empty too, the line is an error and the pass stops. Lines recorded while the
source was down are voided by appending a `void` line with the reason (`--void-empty-after`).
INTRADAY-5M-DATASET-PLAN.md section 5.1.

    uv run python -m signal_engine.backtest.data_readiness.intraday_recovery \
        --build db/staging/intraday/build-<root12>
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from signal_engine.research.intake.fingerprints import canonical_json
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

_REPO = Path(__file__).resolve().parents[3]
DEFAULT_LANDING = _REPO / "db" / "landing" / "angel-1m"
LEDGER = "recovery_ledger.jsonl"
MAX_ATTEMPTS = 3
PAUSE_S = 0.55
GZIP_LEVEL = 6                # level 9 cost 1.6 s per response; 6 is 5% larger, fixed for determinism
FINAL = ("ok", "empty")
VOID = "void"
CANARY = ("RELIANCE", "2026-09-01", "2026-09-29")   # always traded; must return data
SCALED_RTOL = 1e-6

# fetch(symbol, start "YYYY-MM-DD", end "YYYY-MM-DD") -> (ok, response dict, http status)
Fetch = Callable[[str, str, str], tuple[bool, dict, int]]


def read_ledger(build: Path) -> pd.DataFrame:
    path = build / LEDGER
    if not path.exists():
        return pd.DataFrame(columns=["request_id", "attempt", "outcome"])
    return pd.DataFrame([json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()])


def latest(ledger: pd.DataFrame) -> pd.DataFrame:
    """The deciding (last) ledger line of every request."""
    return ledger.drop_duplicates("request_id", keep="last")


def _store(landing: Path, request_id: str, body: dict) -> tuple[str, str]:
    blob = gzip.compress(canonical_json(body).encode(), compresslevel=GZIP_LEVEL, mtime=0)
    sha = hashlib.sha256(blob).hexdigest()
    path = landing / f"{request_id}.json.gz"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    return str(path.relative_to(_REPO) if path.is_relative_to(_REPO) else path), sha


def _sessions(rows: list[dict]) -> list[str]:
    return sorted({datetime.fromtimestamp(int(r["timestamp"]), IST).date().isoformat()
                   for r in rows})


def fetch_one(fetch: Fetch, req: dict, attempt: int, landing: Path) -> dict:
    line = {"request_id": req["request_id"], "symbol": req["symbol"], "start": str(req["start"]),
            "end": str(req["end"]), "attempt": attempt,
            "requested_ist": datetime.now(IST).isoformat(), "outcome": "error", "message": "",
            "n_bars": 0, "sessions": [], "path": None, "sha256": None}
    try:
        ok, resp, status = fetch(req["symbol"], str(req["start"]), str(req["end"]))
    except Exception as exc:  # recorded in the ledger, never hidden
        line["message"] = f"exception: {type(exc).__name__}: {exc}"
        return line
    if not ok:
        detail = resp.get("message") if isinstance(resp, dict) else resp
        line["message"] = f"http {status}: {detail}"
        return line
    rows = resp.get("data") or []
    request = {k: line[k] for k in ("request_id", "symbol", "start", "end")}
    body = {"request": request | {"exchange": "NSE", "interval": "1m",
                                  "source": "AngelOne via services.history_service"},
            "response": resp}
    line["path"], line["sha256"] = _store(landing, req["request_id"], body)
    line["n_bars"], line["sessions"] = len(rows), _sessions(rows)
    line["outcome"] = "ok" if rows else "empty"
    return line


PRIORITY = ("irrecoverability", "recovery", "provenance")   # the decide step needs the sample


def _priority(purpose: str) -> int:
    return min((i for i, p in enumerate(PRIORITY) if p in str(purpose)), default=len(PRIORITY))


def pending(requests: pd.DataFrame, ledger: pd.DataFrame) -> list[tuple[dict, int]]:
    """Requests still to fetch, with their next attempt number, in PRIORITY order."""
    last = latest(ledger)
    done = set(last[last.outcome.isin(FINAL)].request_id)
    errors = ledger[ledger.outcome == "error"]
    tries = errors.groupby("request_id").size().to_dict() if len(errors) else {}
    out = []
    for req in requests.to_dict("records"):
        n = tries.get(req["request_id"], 0)
        if req["request_id"] not in done and n < MAX_ATTEMPTS:
            out.append((req, n + 1))
    return sorted(out, key=lambda item: _priority(item[0].get("purpose", "")))


def source_alive(fetch: Fetch) -> bool:
    """The canary request returns data: the source session works."""
    try:
        ok, resp, _ = fetch(*CANARY)
    except Exception:  # an unreachable source is a dead source here
        return False
    return bool(ok and (resp.get("data") or []))


def run(build: Path, fetch: Fetch, *, landing: Path = DEFAULT_LANDING, pause: float = PAUSE_S,
        limit: int | None = None) -> dict:
    landing.mkdir(parents=True, exist_ok=True)
    requests = pd.read_parquet(build / "requests.parquet")
    todo = pending(requests, read_ledger(build))[:limit]
    counts = {"ok": 0, "empty": 0, "error": 0}
    with open(build / LEDGER, "a", encoding="utf-8") as fh:
        for i, (req, attempt) in enumerate(todo):
            line = fetch_one(fetch, req, attempt, landing)
            dead = line["outcome"] == "empty" and not source_alive(fetch)
            if dead:
                line.update(outcome="error", path=None, sha256=None,
                            message="canary empty: source session invalid; pass stopped")
            fh.write(json.dumps(line) + "\n")
            fh.flush()
            counts[line["outcome"]] += 1
            if dead:
                logger.error(f"recovery: source session invalid at {line['requested_ist']}; "
                             "pass stopped, rerun after a fresh login")
                break
            if i % 200 == 0:
                logger.info(f"recovery: {i + 1} of {len(todo)}; {counts}")
            time.sleep(pause)
    logger.info(f"recovery: pass finished; {counts}")
    return counts


def void_empty_after(build: Path, after_ist: str, reason: str) -> int:
    """Append a void line for every request whose latest line is `empty` and was recorded at or
    after `after_ist` (the source was down). The voided requests are fetched again."""
    ledger = read_ledger(build)
    last = latest(ledger)
    when = pd.to_datetime(last.requested_ist)
    hit = last[(last.outcome == "empty") & (when >= pd.Timestamp(after_ist))]
    now = datetime.now(IST).isoformat()
    with open(build / LEDGER, "a", encoding="utf-8") as fh:
        for row in hit.itertuples():
            fh.write(json.dumps({"request_id": row.request_id, "symbol": row.symbol,
                                 "start": row.start, "end": row.end, "attempt": row.attempt,
                                 "requested_ist": now, "outcome": VOID, "message": reason,
                                 "n_bars": 0, "sessions": [], "path": None,
                                 "sha256": None}) + "\n")
    return len(hit)


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else _REPO / p


def verify(build: Path) -> list[str]:
    """Every planned request has a final outcome, and every final ledger line's landing file
    exists and hashes to its recorded sha256."""
    ledger = read_ledger(build)
    requests = pd.read_parquet(build / "requests.parquet")
    last = latest(ledger)
    final = last[last.outcome.isin(FINAL)]
    errors = [f"request {r} has no final outcome"
              for r in sorted(set(requests.request_id) - set(final.request_id))]
    for row in final.itertuples():
        p = _abs(row.path)
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != row.sha256:
            errors.append(f"landing file of {row.request_id} missing or changed")
    return errors


def load_response(path: str) -> dict:
    return json.loads(gzip.decompress(_abs(path).read_bytes()))


def compare_session(legacy: pd.DataFrame, fresh: pd.DataFrame) -> str:
    """exact | scaled (one price multiplier) | mismatch | fresh_missing."""
    if fresh.empty:
        return "fresh_missing"
    a = legacy.sort_values("timestamp")
    b = fresh.sort_values("timestamp")
    if len(a) != len(b) or not np.array_equal(a.timestamp.to_numpy(), b.timestamp.to_numpy()):
        return "mismatch"
    fields = ["open", "high", "low", "close"]
    pa, pb = a[fields].to_numpy(float), b[fields].to_numpy(float)
    if np.array_equal(pa, pb) and np.array_equal(a.volume.to_numpy(), b.volume.to_numpy()):
        return "exact"
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = pb / pa
        r = float(ratio.mean())
        vol = b.volume.to_numpy(float) / a.volume.to_numpy(float)
    one_multiplier = (np.isfinite(ratio).all() and abs(r - 1) > SCALED_RTOL
                      and np.ptp(ratio) <= SCALED_RTOL * abs(r))
    volume_follows = np.array_equal(a.volume.to_numpy(), b.volume.to_numpy()) or (
        np.isfinite(vol).all() and np.allclose(vol, 1 / r, rtol=SCALED_RTOL))
    return "scaled" if one_multiplier and volume_follows else "mismatch"


def angel_fetch() -> Fetch:
    from services.history_service import get_history_with_auth
    from signal_engine.backtest import broker_login

    auth, feed, broker = broker_login.login()
    if broker != "angel":
        raise RuntimeError(f"secondary broker is {broker!r}; the intraday source is AngelOne")

    def fetch(symbol: str, start: str, end: str) -> tuple[bool, dict, int]:
        return get_history_with_auth(auth, feed, broker, symbol, "NSE", "1m", start, end)

    return fetch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.intraday_recovery")
    ap.add_argument("--build", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--void-empty-after", default=None,
                    help="IST timestamp; void empty answers recorded from then (source down)")
    ap.add_argument("--reason", default=None)
    args = ap.parse_args(argv)
    if args.void_empty_after:
        if not args.reason:
            ap.error("--void-empty-after needs --reason")
        print(void_empty_after(args.build, args.void_empty_after, args.reason), "voided")
        return 0
    if args.verify:
        errors = verify(args.build)
        print("\n".join(errors[:50]) or "recovery ledger verifies")
        return 1 if errors else 0
    print(run(args.build, angel_fetch(), limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
