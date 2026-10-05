"""Intraday build step 4: fetch the planned AngelOne 1m requests with a full provenance trail.

Every request of `requests.parquet` (intraday_plan) is fetched through the history service with
the in-memory secondary broker login (never `auth_db`). Each response is stored as
`<landing>/<request_id>.json.gz` (gzip level GZIP_LEVEL, mtime 0: equal content gives equal bytes) and every
attempt is one line of the append-only `recovery_ledger.jsonl` in the build folder:

    request_id, symbol, start, end, attempt, requested_ist, outcome (ok | empty | error),
    message, n_bars, sessions, path, sha256

An error is a ledger line, never a silent gap. A rerun skips requests with a final outcome and
retries errors up to MAX_ATTEMPTS. INTRADAY-5M-DATASET-PLAN.md section 5.1.

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
SCALED_RTOL = 1e-6

# fetch(symbol, start "YYYY-MM-DD", end "YYYY-MM-DD") -> (ok, response dict, http status)
Fetch = Callable[[str, str, str], tuple[bool, dict, int]]


def read_ledger(build: Path) -> pd.DataFrame:
    path = build / LEDGER
    if not path.exists():
        return pd.DataFrame(columns=["request_id", "attempt", "outcome"])
    return pd.DataFrame([json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()])


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
    done = set(ledger[ledger.outcome.isin(FINAL)].request_id)
    tries = ledger.groupby("request_id").size().to_dict() if len(ledger) else {}
    out = []
    for req in requests.to_dict("records"):
        n = tries.get(req["request_id"], 0)
        if req["request_id"] not in done and n < MAX_ATTEMPTS:
            out.append((req, n + 1))
    return sorted(out, key=lambda item: _priority(item[0].get("purpose", "")))


def run(build: Path, fetch: Fetch, *, landing: Path = DEFAULT_LANDING, pause: float = PAUSE_S,
        limit: int | None = None) -> dict:
    landing.mkdir(parents=True, exist_ok=True)
    requests = pd.read_parquet(build / "requests.parquet")
    todo = pending(requests, read_ledger(build))[:limit]
    counts = {"ok": 0, "empty": 0, "error": 0}
    with open(build / LEDGER, "a", encoding="utf-8") as fh:
        for i, (req, attempt) in enumerate(todo):
            line = fetch_one(fetch, req, attempt, landing)
            fh.write(json.dumps(line) + "\n")
            fh.flush()
            counts[line["outcome"]] += 1
            if i % 200 == 0:
                logger.info(f"recovery: {i + 1} of {len(todo)}; {counts}")
            time.sleep(pause)
    logger.info(f"recovery: pass finished; {counts}")
    return counts


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else _REPO / p


def verify(build: Path) -> list[str]:
    """Every planned request has a final outcome, and every final ledger line's landing file
    exists and hashes to its recorded sha256."""
    ledger = read_ledger(build)
    requests = pd.read_parquet(build / "requests.parquet")
    final = ledger[ledger.outcome.isin(FINAL)].drop_duplicates("request_id", keep="last")
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
    args = ap.parse_args(argv)
    if args.verify:
        errors = verify(args.build)
        print("\n".join(errors[:50]) or "recovery ledger verifies")
        return 1 if errors else 0
    print(run(args.build, angel_fetch(), limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
