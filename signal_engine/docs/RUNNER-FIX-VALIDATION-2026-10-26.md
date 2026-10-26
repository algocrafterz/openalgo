# Runner fix: post-deployment validation (Monday 2026-10-26)

**Gate: no further merges (branches #2 to #8) until every check below has PASSED.** A check that
fails stops the integration: investigate and report first. A check that COULD NOT RUN (for example,
no BREAKOUT-PY signal that day) is reported as such and repeated; it is not a pass.

**How the checks start:** the owner messages Claude to run them (after 09:15 for checks 1 to 3,
after 15:40 for check 4, and on Tuesday for check 4b). No scheduled job is created and no schedule
is changed for this.

## State being validated

| Item | Value |
| --- | --- |
| Commit | `52c9d84e2` fix(signal_engine): refuse stale runner warm-up history and backfill daily |
| Branch | `feature/optimize-signal-engine-v2.0.2.1`, fast-forwarded 2026-10-23 after the 16:00 stop; 1 ahead of origin, not pushed |
| First run | `openAlgoAutoStart` Mon 2026-10-26 08:50 -> `C:\Users\Anand\Scripts\openalgoctl.ps1 dailystart` -> `/home/anand/github/openalgo/signal_engine/scripts/openalgoctl.sh` -> `uv run python -m signal_engine.main` |
| Not deployed | Muhurat exclusion (#2), audit fixes (#3, incl. F3 and the never-stored verify), order recording (#4), snapshot (#5), Telegram (#6), Windows task (#7), docs (#8) |

Baseline (2026-10-23 17:40, read-only): store 1m 212 of 214 symbols at 2026-10-23 (COCHINSHIP, GVT&D
at 2026-10-19, known endpoint gap); daily 210 of 211 (COCHINSHIP at 2026-09-29); M&M and NIFTY have
0 daily rows. `warmup_check.py 2026-10-26` already returned 40 of 40 accepted.

## Log and file locations

| What | Where |
| --- | --- |
| Engine log (paper mode) | `/home/anand/github/openalgo/signal_engine/logs/signal_engine_analyze_2026-10-26.log` |
| Critical and error lines | `/home/anand/github/openalgo/signal_engine/logs/errors_2026-10-26.jsonl` |
| Stack control | `/mnt/c/Users/Anand/Scripts/openalgoctl.log` |
| Backfill | `/home/anand/github/openalgo/signal_engine/logs/backfill_cron.log` |
| Telegram alerts | admin channel `signal-engine-analyze` |
| Check scripts (read-only, outside the repo) | `/home/anand/github/openalgo-integration-20261023/{warmup_check.py,check3_levels.py,store_check.py}` |

All commands below run from `/home/anand/github/openalgo` and only read.

## Check 1 (after 09:15): runner warm-up summary and alerts

```bash
grep -E "Python signal runner|runner warm-up finished|warm-up 2026-10-26|NOT started" \
  signal_engine/logs/signal_engine_analyze_2026-10-26.log
grep -c "warm-up" signal_engine/logs/errors_2026-10-26.jsonl
```

- PASS: `Python signal runner: 40 symbol(s) ...`, `runner warm-up finished: 40 ok, 0 failed`, a
  `Python signal runner started for` line per strategy; no `warm-up 2026-10-26: ... excluded` CRITICAL
  line (count 0); no `RUNNER WARM-UP 2026-10-26` message in the admin channel.
- FAIL: any exclusion, `NOT started`, or the alert. Investigate: the reasons in the CRITICAL line
  (`stale:` / `incomplete:` / `no stored bars`), `store_check.py 2026-10-23`, and the 2026-10-23
  section of `backfill_cron.log`.
- COULD NOT RUN: the engine did not start (check `openalgoctl.log` for the 08:50 dailystart).

## Check 2 (after 09:15): warm-up check script

```bash
.venv/bin/python /home/anand/github/openalgo-integration-20261023/warmup_check.py 2026-10-26
```

- PASS: `warm-up 2026-10-26: 40 of 40 runner symbols accepted, 0 rejected`.
- FAIL: fewer accepted; the first five rejections and their reasons are printed. Compare with check 1.

## Check 3 (after the first BREAKOUT-PY entry): signal levels vs the previous regular session

```bash
.venv/bin/python /home/anand/github/openalgo-integration-20261023/check3_levels.py 2026-10-26
```

- PASS (exit 0): `PASS: levels equal the previous regular session 2026-10-23`.
- FAIL (exit 1): the levels match another session (the defect this fix removes) or none. Stop and
  investigate the warm-up for that symbol.
- COULD NOT RUN (exit 2): no BREAKOUT-PY entry yet; repeat later, or report "could not run" for the day.
- The script was validated on 2026-10-23: 2026-10-14 PFC gives FAIL (matches 2026-10-12, the known
  defect); 2026-10-13 HINDALCO gives PASS (matches 2026-10-12, the correct previous session).

## Check 4 (after 15:40): the 15:35 backfill

```bash
sed -n '/=== backfill 2026-10-26 ===/,$p' signal_engine/logs/backfill_cron.log \
  | grep -E "^=== |^--- |unrecognized|verify |WARNING|FATAL"
.venv/bin/python /home/anand/github/openalgo-integration-20261023/store_check.py 2026-10-26
```

- PASS: no `unrecognized arguments: --daily` (the flag is accepted); both verify lines read
  `expected last session 2026-10-26; symbols behind: 0`; `store_check.py` shows 1m 212 of 214 at
  2026-10-26 (only COCHINSHIP and GVT&D behind) and daily 210 of 211 (only COCHINSHIP behind).
- Expected and NOT a failure: `daily rows M&M (NSE): 0` and `daily rows NIFTY (NSE_INDEX): 0`. The
  verify line will not list them: reporting never-stored symbols is fix F7 in branch #3, not deployed.
  Their backfill needs separate approval.
- FAIL: any other symbol behind, an argument error, or a verify `WARNING`. Investigate the extend
  table above the verify lines (`chunks_failed`, `error`) and whether the broker session was up.

## Check 4b (Tuesday 2026-10-27 after 15:40): proof that the backfill is daily

Monday cannot distinguish the new rule from the old one: after the weekend every symbol is three
days stale, which the old `min_stale=2` rule also fetches, and the old verify also printed the same
day once it ran after 15:40. On Tuesday every symbol is one day stale.

```bash
awk '/^=== backfill /{d=$3} /^--- extend_existing interval=1m/{f=1;n=0;next} /^--- extend_existing interval=D/{if(f)print d, "1m symbols fetched:", n; f=0} f && /^ +[A-Z0-9&-]+ +[0-9]+ +[0-9]+ +[0-9]+ +[0-9]+ /{n++}' \
  signal_engine/logs/backfill_cron.log | tail -4
```

This count undercounts (its pattern misses some symbol formats): it reported 164 on full-fetch days
(2026-10-19, 10-21) and 0 to 2 on the old code's skip days (2026-10-20, 10-22).

- PASS: `2026-10-27 1m symbols fetched:` about 164, the same as a full-fetch day.
- FAIL: 0 to 2; the old gate is still in effect.

## Order recording: dependency to carry into its integration

Runbook step 3B (`RUNBOOK-UNTRACKED-BROKER-POSITION.md` in branch `fix/entry-row-on-accept`) was
tested on 2026-10-23 for a single-strategy PFC long of 930: one entry row, found by the restart
lookup with the correct order id, quantity and side. **When several strategies trade the same symbol,
correct restart attribution depends on F3 in branch #3.** Do not integrate the order-recording fix,
or rely on 3B for a multi-strategy symbol, without #3.

## After all checks pass

Report the results first. Then rebuild the Muhurat branch on `52c9d84e2` and present its final diff
and test results for review. Do not commit or merge it until it is separately approved.
