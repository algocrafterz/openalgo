# Pre-registration (written before any trial was run), 2026-10-28

Method: one parameter changed at a time against baseline B (the live rules as reproduced by
amt_replay.py). Same data, sizing, costs and look-ahead check in every trial. Periods:
P1 = 2026-08-18..2026-09-23 (picks with captured_at < 2026-09-24), P2 = 2026-09-24..2026-10-27.

## Grid (21 one-at-a-time trials plus baseline; neighbour/combination trials added later are counted)

| # | Parameter (baseline) | Values tried | Mechanical reason |
|---|---|---|---|
| 1 | confirmation window (2h) | 1h, 1.5h | A stale pick whose setup has aged 2h may no longer describe the market; shorter window drops late, weaker triggers |
| 2 | stop ATR buffer (0.25) | 0.0, 0.5 | Buffer decides how often the far IB edge plus noise is hit; it also sets size (wider stop, fewer shares) and R:R |
| 3 | TP1 IB multiple (1.0) | 0.75, 1.25 | Median best favourable move is only ~0.30 IB range, so a nearer TP1 may be reached more often; the ladder scales with TP1 |
| 4 | min_rr gate (0.75) | 0.5, 1.0 | Gate removes setups with a stop wide relative to the IB range |
| 5 | no-progress threshold (20%) | 10%, 30% | The no-progress gate closes ~80% of trades; threshold sets how much progress is "real" |
| 6 | no-progress time (90 min) | 120 min | 60 min already rejected; test the opposite direction only |
| 7 | loss-cut threshold (-0.80) | -0.60, off | Loss cut exits before the stop is hit; tests whether it earns its keep |
| 8 | ladder split (50/30/20) | 100% at TP1, 33/33/34 | Low hit rate on TP1 makes the back half of the ladder rarely used |
| 9 | runner buffer (0.3 x level R) | 0.2, 0.5 | Controls how much of a reached level is given back by the runner stop |
| 10 | time exit (14:45) | 14:15, 14:30 | Later-day drift (NSE intraday drift) and spread; 15:00 not executable (Flattrade square-off) |
| 11 | latest entry time (poll before 14:45, effectively 13:00) | 12:00, 12:30 | Late entries have little time to reach TP1 before the time exit |

No parameter value is chosen after seeing results; the stability test (item 4 of the task) uses
the adjacent grid points listed above plus at most one further step.

## Acceptance rule (fixed in advance)

A value is a CANDIDATE only if, versus baseline B on net4 (Flattrade charges + 4 bps per side),
in BOTH P1 and P2 separately, all of:
- expectancy (mean net4 / planned risk) improves by >= 0.03R;
- profit factor improves;
- max drawdown (daily-cumulative, rupees) is no worse (not more negative);
- at least 30 trades;
- the gain is not confined to one side: both long and short expectancy change must be >= 0
  (neither side may be worse than its baseline in that period... judged on each period).
Any single pass is provisional: with ~21+ trials x 2 periods, some passes are expected by chance.
P1 was already looked at in earlier experiments, so it is not a pristine holdout.
