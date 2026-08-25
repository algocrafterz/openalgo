# Signal Engine Refactor Log

Running record of structural work on `signal_engine/`. Behaviour changes are called
out explicitly; everything else is behaviour-preserving by construction and verified
by the test suite.

**Standing rules for entries here**
- One section per refactor pass, newest first.
- Record what moved, what was deduplicated, and what was *deliberately not* done.
- Any behaviour change gets its own line under "Behaviour changes", or it does not ship.
- Defects found but not fixed go to "Known defects" in `PRD.md`, linked from here.

---

## 2026-08-25 — `pinescripts/` structure

Structural only. No Pine logic changed in this pass.

**What moved**
- `ORB-STRATEGY-ANALYSIS.md` -> `orb/STRATEGY-ANALYSIS.md`
- `volume-profile/strategy-analysis.md` -> `volume-profile/STRATEGY-ANALYSIS.md`
- `orb/SIGNAL-PERFORMANCE-2026-Q1.md` -> `orb/trade-analysis/SIGNAL-PERFORMANCE-2026-Q1.md`

`STRATEGY-ANALYSIS.md` existed in three different casings, which made it ungreppable. One
spelling now, every folder. Performance reports were split across two directories; they are all
under `trade-analysis/` with `analyze_orb.py`, which globs its inputs relative to `__file__` and
is unaffected by the move.

**What was removed**
- Three `__init__.py` files (`pinescripts/`, `intraday/`, `intraday/orb/`). Nothing imports
  `signal_engine.pinescripts`, and `trade-analysis` cannot be a module anyway — hyphen in the
  name. They made the tree look like a package it never was.

**What was added**
- `pinescripts/README.md`. There was no map of the ten `.pine` files, their Pine titles, or
  which are live versus third-party reference. It also carries the Pine gotchas this work
  turned up, so they are not rediscovered.
- `.gitignore` entries for `result.json`, `charts/` and `*.png` under `pinescripts/` —
  Strategy-Tester exports and chart screenshots, regenerated per run, and previously permanent
  `git status` noise.

**Deliberately not done**
- No wholesale re-taxonomy. The `intraday|swing/<strategy>/` layout is sound, and renaming
  further would have invalidated ~13 documented paths in `PRD.md` and `HOW-IT-WORKS.md` for
  cosmetic gain. Every reference the moves above *did* break was updated.
- `candlestick-patterns.pine` (third-party, MPL-2.0) left in place untracked rather than deleted;
  it is the source `keylevel-candles.pine` was reduced from.

**Behaviour changes**
- None.

---

## 2026-08-23 (c) — Exit paths unified

Closes the item the previous two passes deferred. **Correction to what was written
then:** the earlier entry costed this as a large test migration. That estimate was for
a *module split* (moving the pipelines into their own files), which is a different
change. Unifying the close-booking cost **17 test edits**, none of them assertion
rewrites of behaviour.

### What changed

`PositionTracker.book_close()` is now the single home for what every full-close path
shares: derive the trade economics, file the `TradeRecord`, advance the day counters,
send the close notification.

| | Before | After |
|---|---|---|
| `notify_position_closed` call sites | 4 | **1** |
| `TradeRecord(...)` construction sites | 5 | **2** |
| Paths booking a close independently | 4 | **1** (+ time exit) |

Routed through it: the signal-driven full exit, the SL-HIT reconcile, the defensive
invalid-remainder conversion, and the tracker's own broker-close detection. Callers keep
only what is genuinely theirs — cancelling broker orders, unregistering the position,
releasing the risk slot, deciding whether the day summary goes out.

**Time exit stays separate by design.** It notifies through `notify_time_exit`, attributes
P&L across positions rather than per close, and tracks `_day_time_exits`. Forcing it
through `book_close` would mean parameterising the notifier, which trades one duplication
for a worse abstraction.

### The subtle part

The full-exit path used `projected_day_context()` because `record_exit()` ran *after* the
notification was built. `book_close` records first and reads `day_context_line()`. These
produce the same string — `projected_day_context(trade_pnl, pnl_delta)` computes exactly
what `day_context_line()` returns after `record_exit(pnl_delta, is_partial=False,
total_pnl=trade_pnl)`. Rather than trust that reasoning, three tests in
`TestFullExitDayContext` pin it: a winning close, a losing close, and a partial leg that
must *not* count as a trade.

### Test impact

- 48 characterization tests passed **unchanged** through the whole refactor.
- 32 `patch("signal_engine.main.tracker")` sites moved to a shared `tracker_mock()`
  factory, because a bare `MagicMock` returns non-awaitable attributes and `book_close`
  is a coroutine.
- 2 assertions retargeted: with the tracker mocked, `notify_position_closed` now fires
  inside the mock, so those tests assert the pipeline *booked the close*. Real-tracker
  coverage of the notification lives in the characterization suite.
- The characterization harness patches `signal_engine.tracker.notifier` alongside
  `signal_engine.main.notifier`, since close notifications are now emitted by the tracker.

517 tests green. `main.py` 98%, `tracker.py` 86%, suite 88%.

### What this buys

Changing what a close notification contains, how R is computed at close, or how a closed
trade is recorded is now a single edit in one function. Before, it was four.

---

## 2026-08-23 (b) — Dead-code sweep

Follow-up to the maintainability pass. Tooling: `vulture`, `ruff` (F401/F811/F841/ARG/ERA001),
plus scripted reachability checks over `Settings` fields, `config.yaml` keys, and module imports.

### Removed

| What | Why |
|---|---|
| `executor.build_sl_order` (26 lines) | Zero production callers — superseded by `place_sl_order`, which builds the order inline with identical action and rounding logic |
| `RiskEngine.__init__(default_product=...)` | Parameter accepted but never assigned to `self` or read. Removed from `risk.py`, `runtime.py`, and two test fixtures |
| `timeutils.now_ist` | Added speculatively earlier the same day; no callers |
| Stale references to `signal_engine/test_telegram.py` (×3) | That script no longer exists; the session is created on first engine start |

**The `build_sl_order` tests were not deleted — they were redirected onto `place_sl_order`.**
Its nine tests were the only coverage of SL action selection and conservative tick rounding,
but they exercised the dead copy while the live path was mocked everywhere. Now they test the
code that actually runs.

### Fixed

- `integration` pytest marker registered in `pyproject.toml`. Four Telegram tests hit the live
  API during a plain `pytest` run; the documented `-m "not integration"` escape hatch existed
  but the marker was unregistered, so it emitted `PytestUnknownMarkWarning`. Now clean:
  `-m "not integration"` deselects exactly those four.
- `tracker` `Set[str]` annotation unquoted, so the `typing.Set` import is a real reference
  rather than surviving only inside a string.

### Verified clean (no action needed)

- All 72 `Settings` fields are read by consumers.
- All 65 leaf keys in `config.yaml` are referenced by the loader.
- No orphaned modules.
- `ERA001` hits are explanatory comments, not commented-out code.
- Duplicate test names are class-namespaced; none are redundant.

### Found, deliberately kept

| Item | Reason |
|---|---|
| **`RiskEngine.update_unrealised`** | **Live risk gap, not dead code — see below.** |
| `main._finalize_invalid_partial` (33 lines) | Provably unreachable: when `_resolve_exit_qty` returns `is_full_exit=False` it guarantees `exit_qty < pos.quantity`, so `remaining <= 0` cannot occur. Kept as a backstop — it is defensive code in exit handling on live money, and unreachability depends on an invariant a future edit could break |
| `tracker.tracked_count` | Only tests call it, but it is a legitimate public accessor; removing it forces tests to reach into the private `_positions` dict |
| `strategies.RSI_TP_MR` | Only tests reference it, but it names a live strategy tag that must match the PineScript alert string |
| `models.raw_message`, `TradeRecord.original_qty` | Vulture false positives — both are pydantic/dataclass fields that are written and read |

### Risk gap found: unrealised drawdown is inert

`RiskEngine.unrealised_loss` is read by the daily-loss-limit check in both `check_exposure()`
and `exposure_block_reason()`:

```python
combined_daily = self.daily_realised_loss + self.unrealised_loss
```

Its only writer is `update_unrealised()`, which **has no production caller** — only tests call
it. In a live session the value is therefore always `0.0`, so **the daily loss limit is
enforced on realised loss alone**; open mark-to-market drawdown never counts toward it.

The docstring states the value is "updated by the position tracker when trades close", but the
tracker never calls it. This was not removed, because deleting it would quietly retire a risk
control that appears to exist. Two options:

1. **Wire it up** — the tracker already polls `ltp` per position in `check_positions`, so open
   P&L is computable there and can be pushed via `update_unrealised()` each cycle.
2. **Remove it** — and accept, explicitly, that the daily limit is realised-only.

Needs a decision. Until then the behaviour is: realised-only daily limit.

### Size

Production code is **larger**, not smaller: 5,990 → 6,493 lines (+503). Decomposition trades
line count for navigability — every extracted function costs a signature and a docstring. The
dead-code sweep returned ~60 lines. Trim was achieved in *structure* (no function over 50 lines
in the engine, no test file over 800) rather than in raw volume, and that trade was deliberate.

---

## 2026-08-23 — Maintainability pass

Driver: the folder had grown with each strategy addition to the point where the
maintainer could not locate the code governing a given pipeline stage.

Plan: `.claude/prds/signal-engine-maintainability.prd.md`

### Gate

514 tests green at every step. The 466 pre-existing tests were never edited for
behaviour — only relocated when their file was split. `main.py` was at 52% coverage,
so 48 characterization tests were written *first* to make the gate load-bearing.

| | Before | After |
|---|---|---|
| Functions >50 lines in the engine | 14 | 0 |
| Largest engine function | 359 lines | 53 |
| Largest test file | 1,878 lines | 668 |
| Test files >800 lines | 3 | 0 |
| `main.py` coverage | 52% | 98% |
| Suite total coverage | 85% | 89% |
| Tests | 466 | 514 |

### What moved

| Was | Now |
|---|---|
| `main._handle_exit_locked` (359 lines) | orchestrator + 16 named stage functions |
| `main._handle_entry` (254) | orchestrator + 11 stage functions |
| `main.main()` + `_test_signal` + startup wiring | `startup.py` (collaborators passed in, no globals) |
| `tracker.check_positions` (236) | orchestrator + guard functions returning an explicit verdict |
| `tracker._check_no_progress` (210) | gate construction / evaluation / action, split |
| `tracker.time_exit_all` (144) | square-off, verification, P&L booking, counter reset |
| `config._build_settings` (165) | one builder per `config.yaml` section |
| `validator.validate` (105) | ordered tuple of single-concern checks |
| `openalgoscheduler.auto_login` (217) | TOTP resolution / OAuth retrieval / TOTP login / contract load |
| `parser.parse`, `normalizer.normalize`, `executor.send_order`, `notifier.notify_day_summary`, `listener.start_listener` | split by responsibility |

### Duplication removed

| Mechanism | Copies before | After |
|---|---|---|
| `_IST` timezone definition | 5 modules | `timeutils.IST` |
| `httpx` request boilerplate | 11 blocks in `api_client` | `_post_json` + `_post_tolerant` |
| `RiskEngine` construction | 3 (`main`, two in `smoke_test`) | `runtime.build_risk_engine()` |
| Orphan release (cancel SL, record_rejection, notify) | 3 in `tracker` | `_release_orphan` |
| Day-counter reset | 2 in `tracker` | `_reset_day_counters` |
| Deferred `get_logger` import | 6 in `openalgoscheduler` | `_log()` |

### Behaviour changes

None to trading behaviour. Two things worth recording:

1. **`RiskEngine` construction unified.** The three copies had drifted: `smoke_test`
   omitted `max_sl_pct_for_sizing`, `soft_blacklist` and `soft_blacklist_multipliers`,
   so the pre-session dry run would not have sized like production. Both features are
   currently disabled in `config.yaml` (`max_sl_pct_for_sizing: 0.0`, `soft: []`), so
   today's output is identical — the fix closes a trap rather than changing a result.
2. **Dead `import signal` removed from `main.py`.** It was shadowed by the `signal`
   parameter in nearly every function in the file and unused after the startup split.

### Regressions caught by the gate (fixed before landing)

- `NameError` in the extracted scheduler helpers — that module has no module-level
  logger; each function builds its own.
- `listener.py`: the chat filter had been widened to include stringified channel ids.
- `listener.py`: `retries = 0` had moved so exponential backoff would never have grown.

### Deliberately not done

- **`main.py` (1,114 lines) and `tracker.py` (982) remain over the 800-line cap.**
  Decomposition adds signatures and docstrings, so the count barely moved. Closing the
  gap means moving the entry/exit pipelines into their own modules, but both read the
  module-level `tracker` / `risk_engine` singletons that ~380 `patch("signal_engine.main.X")`
  targets bind to. Either thread an explicit dependency object through every pipeline
  function (no test churn, `deps.` prefix everywhere) or move the singletons to
  `runtime.py` and rewrite those patch targets. Not worth doing silently on live-money
  code; the cap is a proxy for navigability, which is achieved.
- **The four exit paths are not unified.** They are genuinely different flows, not
  copies: signal-driven exit places an order and books P&L immediately; broker-close
  detection books P&L for an order it never placed; the no-progress exit places an order
  and lets close detection book it; time exit closes in bulk and distributes P&L. What
  *was* duplicated between them has been merged (see table above). Going further means
  moving notification and P&L logic across the `main`/`tracker` boundary, which breaks
  tests asserting on `notifier` while `tracker` is mocked.
- **`analyze_orb.py` left untouched.** Its 431-line `generate_report` is the largest
  function in the tree and has no tests. A golden-output baseline was captured and three
  split attempts made; all failed because report sections share fourteen local variables
  (`m`, `d_grade`, `fri`, `multi_tp_pnl`, …) that flow forward between them. Untangling
  by hand risks silently corrupting the performance report used for strategy decisions,
  for no live-trading benefit. Restored byte-identical to `HEAD`.

### Defects found, not fixed

See "Known defects" in `PRD.md`. Currently one: `notify_be_stop_applied(original_sl=...)`
is always `None` in `tracker._no_progress_break_even`.
