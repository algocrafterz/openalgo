# BREAKOUT / BREAKOUT-PY - Strategy Log

Key-level breakout. Python runner (BREAKOUT-PY) is canonical, the Pine BREAKOUT is the oracle.
Rules: `BREAKOUT-SPEC.md`, `strategy_core/breakout.py`. Paper only.

### 2026-10-14 13:03 — Stops exactly 0.30% away no longer rejected by rounding

- **What changed:** The pre-trade check that rejects stops placed too close to the entry price used
  a strict "less than 0.30%" test. The strategy places its stop at exactly 0.30%, and computer
  arithmetic stores that distance as 0.2999999999999996% for many prices, so the check wrongly
  rejected about 3 in 10 valid signals ("0.3000% below minimum 0.3000%"). The check now allows a
  shortfall of one billionth of the price, which is smaller than any real price step.
- **Entry:** Yes. Signals whose stop sits exactly at the 0.30% limit now open a position. Before
  the fix they were silently dropped. A stop that is truly tighter than 0.30% is still rejected
  (0.299% is rejected, 0.30% is accepted).
- **Exit (SL):** Not affected. The stop is still placed at 0.30% of the entry price.
- **Exit (TP):** Not affected.
- **Consideration:** This makes the live check match the frozen v1.0 rule (a minimum stop of
  0.30%). The 0.30% limit went live on 2026-10-14 (it was 0.20% before), so only that day lost
  signals to the rounding: M&M, SHRIRAMFIN and SUNPHARMA were dropped. About 3 in 10 recorded
  BREAKOUT-PY signals (48 of 162 since 2026-10-01) sit exactly on the limit, so expect the same
  share to be affected on any day the limit is enforced. Do not compare signal counts across
  2026-10-14. Nothing was tuned from the result.

NOTE: `validator._check_sl_distance` now tests `sl_pct < floor - _SL_FLOOR_EPS` with
`_SL_FLOOR_EPS = 1e-9`. The floor is 0.003 (`strategy_profiles.BREAKOUT-PY.min_sl_pct`). The
tolerance is 0.0001% of the entry price, 5e-5 rupees at Rs 50,000. The earlier config comment that
the 0.20% to 0.30% floor change "changes no signal" was wrong for the same reason and is corrected.
It applies to every strategy that uses a stop-distance floor, but only BREAKOUT-PY places stops
exactly on the floor. Tests: `tests/test_validator_sl_boundary.py`.
