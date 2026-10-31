---
name: bug-tracker
description: The register of OpenAlgo/signal_engine defects: how to record them in the single bug tracker (signal_engine/bugs/bugs.json, shown at http://localhost:5000/logs/bugs). Use whenever a review (eod, paper-review, execution-health, a weekly report) finds a defect, an execution problem, a data error or a tuning idea; when asked to "log this bug" or "what bugs are open"; and in the unattended close-day review. Reviews find problems, this skill keeps them, and the resolve skill works them to closed.
---

# Bug tracker

One defect is one entry, however many days it recurs. Reviews describe a day; the tracker
describes a problem's whole life: first seen, every day seen again, cause, fix, proof.

- **Data**: `signal_engine/bugs/bugs.json`, git-tracked. Never edit it by hand or with a script;
  write only through the CLI (it validates, locks and keeps history).
- **Page**: http://localhost:5000/logs/bugs (read-only, `blueprints/bug_tracker.py`).
- **CLI**: `uv run python -m signal_engine.bugs <list|show|add|seen|update|note|apply>`; `--help`
  on each.

## Lifecycle

```
open -> in_progress -> fixed -> verified
                         ^         |
                         +-- seen again reopens it (back to open)
wontfix: a deliberate decision, with the reason in --note
```

| Status | Meaning | Who sets it |
| --- | --- | --- |
| open | Confirmed from evidence, not being worked | any review, interactive or unattended |
| in_progress | Someone is fixing it now (name the branch in `--note`) | interactive session |
| fixed | Code or data changed; `--fix` says what, `--commit` names it once committed | interactive session |
| verified | A later session exercised the fixed path and it held; `--verification` names the session and the evidence | interactive session, never unattended |
| wontfix | Not worth fixing or not ours (upstream core), reason in `--note` | owner decision |

A fix that has not met its trigger yet stays `fixed`. "No timed-out exits today" proves nothing
about a timed-out-exit fix: note it and wait for a session that exercised the path.

## 1. Record (every review)

1. `list --all` first. A finding that matches an existing entry is `seen ID --date DAY`, with new
   evidence, not a new entry. `add` refuses a duplicate open title.
2. Record only what the evidence confirms: a trades.db row, a tradebook fill, a log line, a query.
   A suspicion goes in the review's prose as unconfirmed, not in the tracker.
3. `add` fields:
   - `--kind`: BUG (code does not do what spec/config says), EXECUTION (broker, sandbox, timing,
     operations), DATA (stored data wrong, code may be fine), TUNING-IDEA (a parameter or design
     change; sample size and overfit risk go in `--root-cause`).
   - `--severity`: HIGH = can leave a position unprotected, send a wrong order, or misstate P&L the
     trader acts on; MEDIUM = wrong records or degraded execution with no direct position risk;
     LOW = wording, cosmetics, noise.
   - `--impact`: what it costs a trader, in plain words. `--evidence`: exact ids, rows, times.
   - `--area`: signal_engine, openalgo-core, sandbox, broker, data, strategy, ops.
4. TUNING-IDEA entries never become code changes from fewer than 20 sessions, and frozen strategies
   (see memory) get bug fixes only.

## 2. Fix, verify and close

Working an entry (investigate, fix, prove, close) is the **`resolve`** skill (`/resolve`). This
skill only owns the register and its rules. Two of them bind every fix:

- A fix that has not met its trigger stays `fixed`; `verified` needs a session that exercised the
  fixed path, named in `--verification`.
- An EXECUTION entry with no code fix closes as verified only after the symptom is absent for 5
  sessions in a row with the trigger present (for example, slow broker but no timeout fallout).

## Unattended runs (the close-day review run by close_day.sh at 15:36)

The run is read-only. Do not call `add`, `seen`, `note` or `update` yourself. End the response with
one fenced block, info string `bug-tracker`, holding a JSON list; the cron script applies it with
`apply`, which accepts only these actions:

```bug-tracker
[
  {"action": "seen", "id": "BUG-001", "date": "2026-11-02",
   "evidence": ["trades.db row 3501 booked under stop 2611..."], "sources": ["signal_engine/reports/eod-2026-11-02.md"]},
  {"action": "add", "title": "...", "kind": "BUG", "severity": "HIGH", "area": "signal_engine",
   "date": "2026-11-02", "impact": "...", "strategies": ["ORB"], "evidence": ["..."], "root_cause": "..."},
  {"action": "note", "id": "BUG-003", "text": "2026-11-02: path exercised (restart 10:41, ORB re-entry declined), held"}
]
```

- One `note` for every `fixed` bug, saying whether the day exercised its path and what happened.
  A person promotes it to verified.
- An empty list `[]` when nothing applies. A missing block is reported in the report's footer.

## Weekly sweep

- Every entry not verified or wontfix: still occurring (days seen this week), fix status, and
  whether a fixed one was exercised.
- Fixed for more than 5 sessions and never exercised: say so, so the owner decides whether to
  force the path in a test or accept it.
- New entries for anything the daily reviews described but did not record.
