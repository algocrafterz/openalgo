# Strategy Library Classification (backlog item 8.1)

Status 2026-10-07: HISTORICAL. The legacy ideas this document classifies were deleted on
2026-10-04. Use `research/REGISTRY.md` for the current ideas.

Date: 2026-09-30. Sources: `research/REGISTRY.md` (25 ideas), `research/ideas/*/idea.md`,
`research/ideas/_inbox/` raw text, run records, `strategy_library/`. Nothing has been
deleted. Revised 2026-09-30 after reading the full inbox source text and run records: the
`rejection_basis` values marked "Applied" below are now recorded in each `idea.md`
(registry rebuilt); the rest stay as suggestions.

## Classes and test

| Class | Test applied |
| --- | --- |
| Complete Strategy | Entry, exit, direction, risk/SL and holding-period rules are all defined well enough to code with no assumptions |
| Incomplete Strategy | Entry is defined but at least one of exit, SL, direction or holding period is missing or vague |
| Research Idea | Concept or tooling only; entry rules not pinned to numbers |
| Duplicate | Substantially covered by another idea |
| Obsolete | Superseded, or a source artifact with no further use as a candidate |
| Rejected | Evidence-based dead end recorded in the registry |

Evidence for "defined" is the idea write-up, the raw source text in `research/ideas/_inbox/`,
and whether a backtest adapter exists in `backtest/strategies/`. The inbox ideas
(001, 002, 003, 006, 007, 008) were re-read against their raw source text; corrections are
noted in the rows.

## Classification of the 25 registered ideas

| ID | Title | Registry status | Class | Reason | Suggested edit |
| --- | --- | --- | --- | --- | --- |
| IDEA-001 | First 5-minute range trap/breakout | inbox | Incomplete Strategy (corrected from Research Idea; overlaps IDEA-009) | Source text gives both directions, two entries (trap reversal, swing-point breakout), SL (beyond the trap candle or the swing point) and target (opposite end of the range). Missing: how far a break must go to count as a trap, swing-point definition, explicit time exit | Keep inbox; pin the three terms, then test only the 1-hour consolidation filter against orb, else mark duplicate |
| IDEA-002 | 9 EMA with Fibonacci pivots | inbox | Incomplete Strategy (corrected from Research Idea) | Source has long and short entries (break above both the 9 EMA and the pivot after a reversal at a support or resistance zone) and a 1:2 to 1:3 target; SL is only "typically below the breakout candle" and the zone is undefined | Keep inbox; needs a completed spec before any adapter |
| IDEA-003 | Six-confirmation bullish breakout | inbox | Incomplete Strategy (possible Duplicate of IDEA-010) | Long-only, six numeric entry conditions (source confirmed); no exit, SL or hold. Overlaps key-level breakout (IDEA-010) | Check overlap with IDEA-010; if covered, status `shelved`, note duplicate |
| IDEA-004 | 9/21 EMA cross base pullback | rejected | Rejected | Concurrency artifact, negative at a 10-slot cap | Applied: `rejection_basis: concurrency-artifact` |
| IDEA-005 | Liquidity sweep engulfing | rejected | Rejected | No edge gross or net; rescue attempts overfit | Not applied, ambiguous: stored run shows gross +52 bps (t 0.55), excess over random entry -106 bps and IS +106 vs OOS -111 bps, so both `no-gross-edge` and `overfit` fit; owner to pick |
| IDEA-006 | VWAP qualified break, first pullback | inbox | Incomplete Strategy (closer to complete than first judged) | Source gives long entry at the first VWAP pullback, SL (below VWAP or the pullback low) and target (prior swing high or 2:1). Vague: "volume spike" and "strong close" have no thresholds; no hold limit. The regime-change rule is a bias note, not a trade | Keep inbox until the two thresholds and hold limit are numeric |
| IDEA-007 | Double top/bottom at key levels | inbox | Research Idea (confirmed) | Source has no SL, target or hold, and the pattern tolerance is undefined | Keep inbox; candidate for removal from the pool if no numeric rules are supplied |
| IDEA-008 | VWAP 2 SD failed-extreme reversion | inbox | Incomplete Strategy (nearly complete; corrected) | Source states both directions (long at -2SD, short at +2SD), entry, SL, target and ADX gate. Only a max hold or time exit and the "opening trend" avoid-rule are undefined | Add max hold and pin the opening-trend rule, then it can move to `evaluating` |
| IDEA-009 | ORB (production) | live | Complete Strategy | Live, all rules coded | No change |
| IDEA-010 | Key-level breakout engine | evaluating | Complete Strategy | Adapter built and run | No change |
| IDEA-011 | 9 EMA intraday (PDF) | rejected | Rejected | Negative gross with zero cost | Applied: `rejection_basis: no-gross-edge` |
| IDEA-012 | EMA9 x VWAP cross | rejected | Rejected | Follows through less than random | Applied: `rejection_basis: no-gross-edge` (behaves as mean reversion) |
| IDEA-013 | Initial balance extension | rejected | Rejected | No edge before costs | Applied: `rejection_basis: no-gross-edge` |
| IDEA-014 | Narrow-range breakout | rejected | Rejected | Follow-through below random | Applied: `rejection_basis: no-gross-edge` |
| IDEA-015 | Open-drive / value-area scalp | rejected | Rejected | Six attempts, negative gross | Applied: `rejection_basis: no-gross-edge` |
| IDEA-016 | Turtle Soup fade | rejected | Rejected | Thin gross edge, does not survive cost | Applied: `rejection_basis: cost-dominated` |
| IDEA-017 | Volume profile decision assist | shelved | Research Idea (tooling) | Human-read tool, no testable rules | Not a backtest candidate; keep shelved, or move out of the candidate pool as tooling |
| IDEA-019 | BreakingTrade scanner | shelved | Rejected (BTST), scanner is tooling | BTST measured no edge; extractor kept manual-only | Keep shelved; No edit: status is shelved, and `rejection_basis` is only valid on rejected ideas |
| IDEA-020 | DHB day-high breakout | backtested | Complete Strategy | Adapter with every term pinned to numbers; long-only via `run_overrides` | Write up verdict (net -9 bps at 16 bps cost, gross +6.8) |
| IDEA-021 | Dividend growth | rejected | Rejected | "Do not trade" recorded | Not applied, ambiguous. Run v3: gross +404 bps, net +380, t 9.7, but a same-symbol random-entry baseline earns +434 net (excess -54 bps, t -3.07), so the rule adds nothing over the symbols' drift. Also no stop (profit-only exit), peak 174 and median 72 concurrent positions (net still +246 to +371 bps at 3 to 10 slot caps). `no-gross-edge` (no edge over random entry) fits best but is not the literal reading; `not-robust` is the alternative. Owner to pick |
| IDEA-022 | Dual momentum | shelved | Duplicate | Redundant with momentum-rank (IDEA-025) | Keep shelved; record as duplicate of IDEA-025 |
| IDEA-023 | Gap plus RSI swing | rejected | Rejected | Negative gross; only stored swing idea that produced short trades | Applied: `rejection_basis: no-gross-edge` (gross -8.8 bps); a long-only v4 re-run is tracked separately (P1) |
| IDEA-024 | IBS mean reversion | shelved | Complete Strategy | Adapter built; marginal, gross +7.0 bps against 24 bps cost | `rejection_basis` not applicable while shelved; `cost-dominated` if later rejected |
| IDEA-025 | Momentum rank 12-1 | paper | Complete Strategy | Reference edge, paper first | No change |
| IDEA-026 | Phoenix Bird rebound | backtested | Complete Strategy | Adapter built from published source | Write up verdict |

Counts (revised): Complete 6 (009, 010, 020, 024, 025, 026), Incomplete 5 (001, 002, 003,
006, 008), Research Idea 2 (007, 017), Duplicate 1 (022), Rejected 11 (004, 005, 011 to 016,
019, 021, 023). Obsolete: none among registered ideas.

IDEA-018: no folder, registry entry, inbox reference, doc mention or git history contains
it (`git log -S"IDEA-018"` is empty), and the builder has no contiguity check. It was
never assigned; the number is simply skipped. No fix needed. The next free number is 027.

## Items in `strategy_library/` that are not candidates

| Path | Class | Note |
| --- | --- | --- |
| `intraday/orderflow/*.pine` | Research Idea (indicators) | Indicators and a market-structure report, no entry/exit rules. Not strategy candidates; belong with indicator assets |
| `intraday/intraday-dhb.txt` | Obsolete (source artifact) | Raw text behind IDEA-020, which now has an adapter and idea.md; could move to `research/ideas/_inbox/` |
| `intraday/intraday-stocks-watchlist-tradingview` | Obsolete (data) | A watchlist export, not a strategy |
| `telegram/*.json` | Not a strategy | Exported alert history used by trade-analysis; keep, but it is data, not library content |
| `lipiscripts/intraday` | Not a strategy | GoCharting ports of Pine indicators |
| `intraday/{volume-profile,breaking-trade}` | Research Idea / tooling | Correspond to IDEA-017 and IDEA-019 |

## Recommended candidate pool

Backtest candidates after cleanup: the 6 Complete strategies, plus IDEA-008 once a max
hold and the opening-trend rule are added. Put IDEA-001, 002, 003 and 006 in a
spec-completion queue (each needs specific terms pinned, listed in the rows above), and
treat IDEA-007 and 017 as research ideas that do not consume backtest effort until they
pass the completeness check (`research/spec.py`).
