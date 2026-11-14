"""close_day.sh --retry and run_upkeep.sh recover on a stub tree.

The real scripts are copied into a temporary repo. `uv` is a stub that records its command line, so
no real phase, step, broker, review or Telegram call happens, and nothing outside tmp_path is read
or written.
"""

import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
bash = shutil.which("bash")
pytestmark = pytest.mark.skipif(bash is None, reason="needs bash on Linux")

STUB_UV = """#!/usr/bin/env bash
echo "$* | nice=$(ps -o ni= -p $$ | tr -d ' ')" >> "$STUB_LOG"
exit "${STUB_EXIT:-0}"
"""
# `timeout` records its limit, then runs the command. `date` answers "today" (date +%F) with a
# fixed weekday from FAKE_TODAY and passes every other call to the real date, so the retry
# tests run on any day of the week, the weekend included.
STUB_TIMEOUT = """#!/usr/bin/env bash
echo "$*" >> "$STUB_TIMEOUT_LOG"
while [[ "$1" == --* ]]; do shift; done
shift
exec "$@"
"""
STUB_DATE = """#!/usr/bin/env bash
if [ "$*" = "+%F" ] && [ -n "${FAKE_TODAY:-}" ]; then echo "$FAKE_TODAY"; exit 0; fi
exec /usr/bin/date "$@"
"""
FAKE_WEEKDAY = "2026-11-12"  # a Thursday
TRAP = '#!/usr/bin/env bash\ntouch "$(dirname "$0")/RAN-$(basename "$0")"\nexit 0\n'
SCRIPTS = (
    "signal_engine/analysis/close_day.sh",
    "signal_engine/ops/run_upkeep.sh",
    "signal_engine/ops/model.py",
)


def last_weekday():
    day = date.today() - timedelta(days=1)
    while day.weekday() > 4:
        day -= timedelta(days=1)
    return day.isoformat()


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "repo"
    for rel in SCRIPTS:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
        dest.chmod(0o755)
    # every step script the 15:36 run starts is a trap: a retry must not start any of them
    for rel in (
        "signal_engine/analysis/eod.sh",
        "signal_engine/analysis/weekly.sh",
        "signal_engine/analysis/eod_claude_review.sh",
        "signal_engine/backtest/backfill_cron.sh",
    ):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(TRAP)
        (repo / rel).chmod(0o755)
    (repo / "signal_engine/logs").mkdir()
    (repo / "signal_engine/reports").mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "uv").write_text(STUB_UV)
    (stubs / "uv").chmod(0o755)
    for name, body in (("timeout", STUB_TIMEOUT), ("date", STUB_DATE)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "STUB_LOG": str(tmp_path / "uv.log"),
        "STUB_TIMEOUT_LOG": str(tmp_path / "timeout.log"),
        "FAKE_TODAY": FAKE_WEEKDAY,
    }
    return repo, env, tmp_path / "uv.log"


def run(repo, env, script, *args):
    return subprocess.run(
        [str(repo / script), *args], env=env, capture_output=True, text=True, timeout=60, cwd=repo
    )


def test_close_day_retry_hands_over_to_the_retry_and_keeps_the_first_run_untouched(tree):
    repo, env, log = tree
    day = last_weekday()
    status = repo / f"signal_engine/reports/close-day-{day}-status.tsv"
    manifest = repo / f"signal_engine/reports/close-day-{day}-run.json"
    status.write_text("step\tstarted\tended\texit\nbackfill\t15:36:00\t15:43:00\t1\n")
    manifest.write_text(json.dumps({"state": "failed", "steps": []}))
    before = (status.read_text(), manifest.read_text())
    res = run(repo, env, "signal_engine/analysis/close_day.sh", "--retry", day)
    assert res.returncode == 0, res.stderr + res.stdout
    calls = log.read_text().splitlines()
    assert len(calls) == 1
    assert f"run python -m signal_engine.ops close-day-retry --day {day}" in calls[0]
    assert (status.read_text(), manifest.read_text()) == before  # not truncated, not restarted
    assert not list((repo / "signal_engine").rglob("RAN-*"))  # no step script was started


def test_close_day_retry_without_a_day_means_today(tree):
    # `date +%F` answers FAKE_TODAY (a Thursday) through a stub, so the test runs on a weekend too.
    repo, env, log = tree
    res = run(repo, env, "signal_engine/analysis/close_day.sh", "--retry")
    assert res.returncode == 0, res.stderr
    assert f"close-day-retry --day {FAKE_WEEKDAY}" in log.read_text()


def test_the_retry_has_an_outer_timeout_that_ends_before_the_1630_hard_limit(tree):
    repo, env, log = tree
    res = run(repo, env, "signal_engine/analysis/close_day.sh", "--retry", FAKE_WEEKDAY)
    assert res.returncode == 0, res.stderr
    seen = Path(env["STUB_TIMEOUT_LOG"]).read_text().splitlines()
    assert len(seen) == 1 and seen[0].startswith("--kill-after=15 ")
    limit = int(seen[0].split()[1])
    assert limit == 120  # a past day: python exits at once. The window of today is tested in
    # test_ops_strict_deadline.py (the limits come from model.py, not from this script).
    assert "close-day-retry" in seen[0]
    text = (ROOT / "signal_engine/analysis/close_day.sh").read_text()
    assert 'timeout --kill-after="${LIMITS#* }" "${LIMITS% *}"' in text


def test_close_day_retry_on_a_weekend_does_nothing(tree):
    repo, env, log = tree
    res = run(repo, env, "signal_engine/analysis/close_day.sh", "--retry", "2026-11-14")
    assert res.returncode == 0 and "weekend" in res.stdout
    assert not log.exists()


def test_close_day_without_retry_is_not_changed_it_still_starts_a_normal_run(tree):
    repo, env, log = tree
    res = run(repo, env, "signal_engine/analysis/close_day.sh", "--no-review", last_weekday())
    assert res.returncode == 0, res.stderr + res.stdout
    assert "close-day-retry" not in log.read_text()
    assert (repo / "signal_engine/analysis/RAN-eod.sh").exists()  # the normal steps ran


def test_run_upkeep_recover_runs_the_recover_command_at_the_lowest_priority(tree):
    repo, env, log = tree
    res = run(repo, env, "signal_engine/ops/run_upkeep.sh", "recover")
    assert res.returncode == 0, res.stderr + res.stdout
    line = log.read_text().splitlines()[0]
    assert "run python -m signal_engine.ops recover" in line
    expected = min(19, os.nice(0) + 19)
    assert line.endswith(f"nice={expected}")


def test_run_upkeep_recover_has_a_hard_limit_that_ends_at_0855_from_a_0836_start():
    text = (ROOT / "signal_engine/ops/run_upkeep.sh").read_text()
    assert "LIMIT=1140" in text  # 19 minutes: 08:36 to 08:55
    assert "nice -n 19" in text and "ionice -c3" in text


def test_the_installed_upkeep_lines_still_work(tree):
    repo, env, log = tree
    for args in (("deadman", "close"), ("deadman", "morning"), ("retry-failed",)):
        res = run(repo, env, "signal_engine/ops/run_upkeep.sh", *args)
        assert res.returncode == 0, (args, res.stderr + res.stdout)
    lines = log.read_text().splitlines()
    assert [ln.split("|")[0].strip() for ln in lines] == [
        "run python -m signal_engine.ops deadman close",
        "run python -m signal_engine.ops deadman morning",
        "run python -m signal_engine.ops retry-failed",
    ]
    # these keep their normal priority: only the recovery is niced
    assert all(ln.endswith(f"nice={os.nice(0)}") for ln in lines)


def test_run_upkeep_refuses_an_unknown_command(tree):
    repo, env, log = tree
    res = run(repo, env, "signal_engine/ops/run_upkeep.sh", "restart")
    assert res.returncode == 2 and not log.exists()


def test_a_failing_recover_exit_code_reaches_the_cron_log(tree):
    repo, env, log = tree
    res = subprocess.run(
        [str(repo / "signal_engine/ops/run_upkeep.sh"), "recover"],
        env={**env, "STUB_EXIT": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        cwd=repo,
    )
    assert "upkeep recover exit 1" in res.stdout


@pytest.mark.parametrize(
    "script",
    [
        "signal_engine/analysis/close_day.sh",
        "signal_engine/ops/run_upkeep.sh",
        "signal_engine/ops/run_phase.sh",
        "signal_engine/analysis/review_catchup.sh",
        "signal_engine/analysis/autofix.sh",
        "signal_engine/analysis/eod.sh",
        "signal_engine/analysis/eod_claude_review.sh",
        "signal_engine/backtest/backfill_cron.sh",
    ],
)
def test_the_changed_scripts_parse(script):
    res = subprocess.run(["bash", "-n", str(ROOT / script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_no_cron_example_in_a_script_header_still_shows_a_retired_line():
    retired = (" 35 21 ", " 5 21 ", " 35 18 ", " 56 8 ", " 20 16 ", " 30 16 ")
    for rel in (
        "signal_engine/analysis/close_day.sh",
        "signal_engine/analysis/review_catchup.sh",
        "signal_engine/analysis/autofix.sh",
        "signal_engine/ops/run_upkeep.sh",
        "signal_engine/ops/run_phase.sh",
    ):
        for line in (ROOT / rel).read_text().splitlines():
            if line.startswith("#   ") and "* * 1-5" in line:
                assert not any(r in f" {line[1:].strip()} " for r in retired), (rel, line)
