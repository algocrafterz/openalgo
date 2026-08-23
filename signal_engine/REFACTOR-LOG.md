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
