# Shadow comparison: WebSocket-built 5-minute candles vs Flattrade history

Status: tooling built and tested (2026-10-29). No trading session has been recorded yet, so
there are no comparison results. Nothing here feeds a strategy; the engine still reads candles
from `/api/v1/history`.

## Why

The runner fetches 40 history bars at every 5-minute boundary, one REST call per symbol, against a
110 per minute account cap. Building the bar from the WebSocket quote feed would need no REST
call at all. That is only acceptable if the built bar matches the broker's bar, so it is measured
first, read-only.

## How to run a session

1. Before 09:10 IST: `uv run python -m signal_engine.shadow.recorder --universe runner`
   (stops itself at 15:32 IST; `--symbols A,B` for an explicit list). It only listens. It adds a
   client subscription on the proxy and makes no REST call and places no order.
2. After 15:35 IST: `uv run python -m signal_engine.shadow.compare --dates 2026-10-30 ...`
   (one history call per symbol per day, paced 0.3 s). Prints a Markdown verdict per session.
3. Repeat for at least 5 sessions and pass all of them to `--dates` for the overall verdict.

Ticks go to `signal_engine/data/shadow/ticks-<date>.db` (gitignored). A dropped connection is
recorded as a gap; bars overlapping a gap are set aside, not scored as errors.

## What is compared

Per symbol and 5-minute bar, tick-built (by arrival time) against history, tolerance 0.05 price:
open, close, high and low, volume, bar timestamps, bars missing, bars with no history
counterpart, arrival lag, and whether assigning ticks by the feed's exchange time instead of
arrival time changes the close. High and low are scored with the day high and day low
recovery (`high_aug`, `low_aug`), because the feed is a conflated quote stream and a plain
last-price high can miss an extreme between two updates.

## Acceptance criteria (proposed, constants in `compare.py`)

A session passes only if all hold. The tick-built bar is a candidate to replace history only
after at least 5 consecutive sessions all pass.

| criterion | limit |
|---|---|
| coverage: history bars outside feed gaps that have a tick-built bar | >= 99% |
| tick-built bars at times history has no bar | 0 |
| close within tolerance | >= 99% |
| open within tolerance | >= 95% |
| high and low within tolerance (each) | >= 95% |
| bars whose tick range exceeds the history range | <= 0.5% |
| volume error, median / 95th percentile | <= 1% / <= 5% |
| arrival lag behind exchange time, 99th percentile | <= 1.0 s |
| arrival vs exchange time assignment giving a different close | <= 1% |

The thresholds are a proposal; confirm or change them before the first verdict is read.

## Found on first contact with the live feed

- The feed's `last_trade_time` arrived truncated ("15:", "15"), so exchange-time assignment is
  unusable for those ticks. The parser returns "no exchange time" and the bar falls back to
  arrival time. The share of usable exchange times will show in the arrival-lag metric.
- A fresh subscription delivers one snapshot tick per symbol immediately, which is why a
  recorder started after the close still records two ticks.

## Not covered

- Strategy behavior on tick-built bars. Only bar equality is measured.
- Sessions with a feed gap longer than a bar: those bars are excluded, so a session with many
  gaps can pass on a small sample. The report prints the gap count; read it with the verdict.
