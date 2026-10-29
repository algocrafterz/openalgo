# Flattrade market-data WebSocket drops about every 10 minutes (upstream issue draft)

Draft for the upstream tracker. Observed 2026-10-29 on one self-hosted instance. Separate from
the quote rate-limit and candle work; nothing was changed for it.

## Symptom

`log/openalgo_2026-10-29.log` holds 37 occurrences of
`Flattrade WebSocket error: Connection to remote host was lost.` during the session, roughly one
every 10 minutes. After each drop the adapter schedules a reconnect in 5 s, reconnects,
resubscribes every symbol, and ticks stop in between.

Each drop writes `Reconnecting in 5s (attempt 1)` twice within about 10 ms (66 lines for 37
drops), from the error handler and the close handler. This is probably only a duplicate log
line, because `_schedule_reconnection` cancels the earlier timer before starting a new one.
It was not confirmed that a single reconnect results.

## Why it matters

- Every drop leaves a gap in the tick stream. Anything built from ticks (live MTM, tick-built
  candles) must treat the gap explicitly.
- A resubscribe of the whole book follows each drop. On 2026-10-29 at 13:39 IST a
  resubscribe was followed within 8 seconds by four identical 25-symbol multi-quote requests.
  The link between the two was not established; the multi-quote requests are now coalesced.

## Not known

- Whether the drops come from Flattrade, the network (this runs under WSL2), or the adapter's
  keepalive. The client's ping interval and the server's idle timeout were not measured.
- Whether the drops happen on other days at the same rate. Only 2026-10-29 was counted.

## Suggested checks

1. Count drops per day across the retained logs.
2. Compare drop times with the adapter's heartbeat and ping settings.
3. Run the same feed from a non-WSL host for one session.
4. Deduplicate the double "Reconnecting" log line.
