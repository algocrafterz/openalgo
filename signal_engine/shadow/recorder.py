"""Record Quote-mode ticks from OpenAlgo's WebSocket proxy into a local SQLite file.

Read-only: it authenticates like any client, subscribes, and listens. It places no order and
makes no REST call. It adds a client subscription on the proxy, so the broker feed subscribes
the same symbols once (the feed has room for 3000). Run it by hand for a session:

    uv run python -m signal_engine.shadow.recorder --universe runner

It stops itself at --until (IST) and closes its file. A dropped connection is recorded as a gap
so the comparison can set those bars aside instead of counting them as errors.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from datetime import datetime

import websockets

from signal_engine.logger_setup import logger
from signal_engine.shadow import store
from signal_engine.timeutils import IST

FLUSH_SECONDS = 2.0
FLUSH_ROWS = 500
RECONNECT_BACKOFF_SECONDS = 3.0


def _until_epoch(until: str) -> float:
    hh, mm = (int(x) for x in until.split(":"))
    return datetime.now(IST).replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()


async def _session(url, api_key, symbols, exchange, conn, stop_at, state) -> None:
    async with websockets.connect(url, ping_interval=20, max_queue=None) as ws:
        await ws.send(json.dumps({"action": "authenticate", "api_key": api_key}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if auth.get("status") == "error" or auth.get("type") == "error":
            raise RuntimeError("WebSocket authentication was refused")
        await ws.send(
            json.dumps(
                {
                    "action": "subscribe",
                    "mode": "Quote",
                    "symbols": [{"symbol": s, "exchange": exchange} for s in symbols],
                }
            )
        )
        logger.info(f"shadow recorder: subscribed {len(symbols)} symbols")
        pending: list[tuple] = []
        last_flush = time.monotonic()
        state["connected_at"] = None
        try:
            while time.time() < stop_at:
                try:
                    raw = await asyncio.wait_for(ws.recv(), 1.0)
                except TimeoutError:
                    raw = None
                if raw is not None:
                    row = store.tick_row(json.loads(raw), int(time.time() * 1000))
                    if row is not None:
                        pending.append(row)
                        state["rows"] += 1
                if pending and (
                    len(pending) >= FLUSH_ROWS or time.monotonic() - last_flush >= FLUSH_SECONDS
                ):
                    conn.executemany("INSERT INTO ticks VALUES (?,?,?,?,?,?,?)", pending)
                    conn.commit()
                    pending, last_flush = [], time.monotonic()
        finally:
            if pending:
                conn.executemany("INSERT INTO ticks VALUES (?,?,?,?,?,?,?)", pending)
                conn.commit()
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"action": "unsubscribe_all"}))


async def record(url, api_key, symbols, exchange, path, until) -> int:
    conn = store.connect(path)
    stop_at = _until_epoch(until)
    state = {"rows": 0}
    try:
        while time.time() < stop_at:
            dropped_at = None
            try:
                await _session(url, api_key, symbols, exchange, conn, stop_at, state)
            except (TimeoutError, websockets.WebSocketException, OSError, RuntimeError) as exc:
                dropped_at = int(time.time() * 1000)
                logger.warning(
                    f"shadow recorder: connection lost ({type(exc).__name__}); reconnecting"
                )
            if dropped_at is not None and time.time() < stop_at:
                await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)
                conn.execute(
                    "INSERT INTO gaps VALUES (?,?,?)",
                    (dropped_at, int(time.time() * 1000), "disconnect"),
                )
                conn.commit()
    finally:
        conn.close()
    logger.info(f"shadow recorder: wrote {state['rows']} ticks to {path}")
    return state["rows"]


def main() -> None:
    from signal_engine.config import settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbols", help="comma-separated symbols")
    parser.add_argument(
        "--universe", choices=["runner"], help="use runner.universe from config.yaml"
    )
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument("--until", default="15:32", help="IST stop time, HH:MM")
    parser.add_argument("--ws-url", default=None)
    args = parser.parse_args()

    if args.universe == "runner":
        if not isinstance(settings.runner.universe, tuple):
            parser.error("runner.universe is not an explicit list; pass --symbols")
        symbols = list(settings.runner.universe)
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        parser.error("give --symbols or --universe runner")

    import os

    url = args.ws_url or os.environ.get("WEBSOCKET_URL", "ws://127.0.0.1:8765")
    path = store.db_path(datetime.now(IST).date().isoformat())
    asyncio.run(record(url, settings.openalgo_api_key, symbols, args.exchange, path, args.until))


if __name__ == "__main__":
    main()
