#!/usr/bin/env bash
# Unattended upkeep around the daily workflow (signal_engine/ops/automation.py).
#
#   recover         08:36  retry only the steps the previous trading day left open (backfill,
#                           reports, review), at low priority, ending by 08:55 IST so 09:00-09:15
#                           stays free. One printed notice line, only when something was open.
#                           Replaces retry-failed. Rules: signal_engine/ops/recovery.py.
#   retry-failed   08:56  the line installed until the maintainer installs recover: redo the last
#                           session's data download if it failed (once, before 09:10). It is
#                           removed from the schedule; the command stays for that line.
#   deadman morning 09:40  print and exit 1 when the 09:12 or 09:27 checkpoint left no record; also prints
#                           what the previous day left open and what the recovery did (WARN).
#   deadman close   16:28  print and exit 1 when the close-day run is missing, incomplete or failed, judged
#                           after the 16:01 retry (close_day.sh --retry, which ends by 16:25).
#                           (Installed until the maintainer switches the line: 16:20. Both pass the
#                           jobs audit, signal_engine/ops/jobs.py CRON_LINES.)
#
# ROLLOUT ORDER: (1) merge the code; (2) install the Windows task changes (AutoStart 08:30,
# AutoStop 16:31) on a weekend: recover needs the 08:30 broker login; (3) then install the cron
# lines; (4) then set jobs.REQUIRE_NEW_SCHEDULE = True. Cron before the Windows tasks leaves
# recover without a login (it ends incomplete) and the 16:01 retry without OpenAlgo.
#
# Weekends and NSE holidays are handled in Python. Nothing here touches orders, positions, config
# or code. Output goes to the ops cron log, which is where the other phases write.
#
# CRON (weekdays, IST):
#   36 8 * * 1-5 /home/anand/github/openalgo/signal_engine/ops/run_upkeep.sh recover >> /home/anand/github/openalgo/signal_engine/logs/ops_cron.log 2>&1
#   40 9 * * 1-5 /home/anand/github/openalgo/signal_engine/ops/run_upkeep.sh deadman morning >> /home/anand/github/openalgo/signal_engine/logs/ops_cron.log 2>&1
#   28 16 * * 1-5 /home/anand/github/openalgo/signal_engine/ops/run_upkeep.sh deadman close >> /home/anand/github/openalgo/signal_engine/logs/ops_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

CMD="${1:?usage: run_upkeep.sh recover | retry-failed | deadman morning|close}"
PREFIX=()
case "$CMD" in
  retry-failed) LIMIT=2700 ;;
  recover)
    # 08:36 + 1140 s = 08:55. The Python side also stops at 08:55 by the wall clock; this limit is
    # the backstop. Lowest CPU and disk priority: the recovery must never slow the open.
    LIMIT=1140
    PREFIX=(nice -n 19)
    command -v ionice >/dev/null 2>&1 && PREFIX=(ionice -c3 "${PREFIX[@]}")
    ;;
  deadman) LIMIT=120 ;;
  *) echo "unknown command $CMD"; exit 2 ;;
esac

UV="$(command -v uv || echo /home/anand/.local/bin/uv)"  # cron has a minimal PATH
echo "=== upkeep $* $(date +%F\ %T) ==="
${PREFIX[@]+"${PREFIX[@]}"} env PYTHONPATH=. timeout --kill-after=15 "$LIMIT" "$UV" run python -m signal_engine.ops "$@"
echo "upkeep $* exit $?"
