# Data-store audit 2026-10-04 19:20:08

Store: `db/historify.duckdb`. Policy version `ee765d3553db`.
Calendar: Phase 1 proxy (data-derived, weekdays only). Weekend sessions are listed as unexpected until the versioned NSE calendar classifies them.

## Interval D

211 symbols, 314,497 rows, 2019-12-03 to 2026-10-01.

| Dimension | Status |
|---|---|
| validity | WARNING |
| completeness | WARNING |
| identity_continuity | PASS |
| coverage | NOT_EVALUATED |
| calendar | FAIL |
| universe_membership | NOT_EVALUATED |
| survivorship | NOT_EVALUATED |
| adjustment | NOT_EVALUATED |
| snapshot_integrity | PASS |
| freshness | PASS |

| Rule | Action | Status | Rows | Symbols | Sessions |
|---|---|---|---|---|---|
| SNAP-DUP-KEY | BLOCK | evaluated | 0 | 0 | 0 |
| CANON-DAILY-TS | BLOCK | evaluated | 0 | 0 | 0 |
| SNAP-EXCHANGE | BLOCK | evaluated | 0 | 0 | 0 |
| VAL-OHLC | EXCLUDE_SESSION | evaluated | 0 | 0 | 0 |
| VAL-NONPOS | EXCLUDE_SESSION | evaluated | 2 | 1 | 2 |
| VAL-NEG-VOL | EXCLUDE_SESSION | evaluated | 0 | 0 | 0 |
| VAL-NULL-ROW | EXCLUDE_SESSION | evaluated | 0 | 0 | 0 |
| VAL-PLACEHOLDER | EXCLUDE_SESSION | evaluated | 164 | 164 | 2 |
| DIAG-LOCKED | WARNING | evaluated | 418 | 173 | 204 |
| INFO-OUTSIDE-HOURS | INFO | not_evaluated | 0 | 0 | 0 |
| CAL-UNEXPECTED-DATE | BLOCK | evaluated | 9 | 0 | 9 |
| CAL-UNEXPECTED-ISOLATED | EXCLUDE_SESSION | evaluated | 0 | 0 | 0 |
| CMP-MISSING | EXCLUDE_SYMBOL | evaluated | 493 | 14 | 0 |
| CMP-MISSING-FEW | WARNING | evaluated | 5,198 | 183 | 31 |
| CMP-LATEST | EXCLUDE_SESSION | evaluated | 0 | 0 | 0 |
| FRESH-SYMBOL-LAG | WARNING | evaluated | 0 | 0 | 0 |
| DIAG-ZERO-VOL | WARNING | evaluated | 2 | 1 | 0 |
| DIAG-SPIKE | INFO | evaluated | 5 | 5 | 4 |
| DIAG-STALE-REPEAT | INFO | evaluated | 0 | 0 | 0 |
| DIAG-LATE-START | INFO | evaluated | 48 | 48 | 46 |
| DIAG-CROSS-INTERVAL | INFO | evaluated | 56,279 | 208 | 1609 |

Projection for a run over all 211 symbols (session exclusion not allowed):
- state: NOT_READY (CAL-UNEXPECTED-DATE)
- blocks: CAL-UNEXPECTED-DATE
- defect-excluded symbols: 197 (limit 10 or 5%)
- invalid-bar share: 0.00064% (2 of 314,497)

## Interval 1m

214 symbols, 165,179,409 rows, 2016-10-03 to 2026-10-01.

| Dimension | Status |
|---|---|
| validity | WARNING |
| completeness | WARNING |
| identity_continuity | PASS |
| coverage | NOT_EVALUATED |
| calendar | FAIL |
| universe_membership | NOT_EVALUATED |
| survivorship | NOT_EVALUATED |
| adjustment | NOT_EVALUATED |
| snapshot_integrity | PASS |
| freshness | WARNING |

| Rule | Action | Status | Rows | Symbols | Sessions |
|---|---|---|---|---|---|
| SNAP-DUP-KEY | BLOCK | evaluated | 0 | 0 | 0 |
| CANON-DAILY-TS | BLOCK | not_evaluated | 0 | 0 | 0 |
| SNAP-EXCHANGE | BLOCK | evaluated | 0 | 0 | 0 |
| VAL-OHLC | EXCLUDE_SESSION | evaluated | 326 | 177 | 61 |
| VAL-NONPOS | EXCLUDE_SESSION | evaluated | 15 | 15 | 1 |
| VAL-NEG-VOL | EXCLUDE_SESSION | evaluated | 17,450 | 210 | 47 |
| VAL-NULL-ROW | EXCLUDE_SESSION | evaluated | 0 | 0 | 0 |
| VAL-PLACEHOLDER | EXCLUDE_SESSION | not_evaluated | 0 | 0 | 0 |
| DIAG-LOCKED | WARNING | not_evaluated | 0 | 0 | 0 |
| INFO-OUTSIDE-HOURS | INFO | evaluated | 297,641 | 214 | 1976 |
| CAL-UNEXPECTED-DATE | BLOCK | evaluated | 10 | 0 | 10 |
| CAL-UNEXPECTED-ISOLATED | EXCLUDE_SESSION | evaluated | 3 | 3 | 3 |
| CMP-MISSING | EXCLUDE_SYMBOL | evaluated | 11,403 | 171 | 0 |
| CMP-MISSING-FEW | WARNING | evaluated | 1,254 | 43 | 44 |
| CMP-LATEST | EXCLUDE_SESSION | evaluated | 0 | 0 | 0 |
| FRESH-SYMBOL-LAG | WARNING | evaluated | 3 | 3 | 2 |
| DIAG-ZERO-VOL | WARNING | evaluated | 390,480 | 1 | 0 |
| DIAG-SPIKE | INFO | not_evaluated | 0 | 0 | 0 |
| DIAG-STALE-REPEAT | INFO | not_evaluated | 0 | 0 | 0 |
| DIAG-LATE-START | INFO | evaluated | 62 | 62 | 57 |
| DIAG-CROSS-INTERVAL | INFO | not_evaluated | 0 | 0 | 0 |

Projection for a run over all 214 symbols (session exclusion allowed):
- state: NOT_READY (CAL-UNEXPECTED-DATE)
- blocks: CAL-UNEXPECTED-DATE
- defect-excluded symbols: 172 (limit 10 or 5%)
- invalid-bar share: 0.01076% (17,776 of 165,179,409)

