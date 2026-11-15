"""Single-place rule: only close_day.sh schedules --bug-alerts; --logs is never scheduled with --write."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SE = ROOT / "signal_engine"
CLOSE = SE / "analysis" / "close_day.sh"


def _scheduled_files():
    found = []
    for pattern in ("*.sh", "*.ps1", "*.bat"):
        found += [p for p in SE.rglob(pattern) if "node_modules" not in p.parts]
    return sorted(found)


def _code_lines(path):
    return [
        ln
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not ln.lstrip().startswith("#")
    ]


def test_only_close_day_passes_bug_alerts():
    users = [
        p.name for p in _scheduled_files() if any("--bug-alerts" in ln for ln in _code_lines(p))
    ]
    assert users == ["close_day.sh"]
    assert sum("--bug-alerts" in ln for ln in _code_lines(CLOSE)) == 1


def test_no_script_passes_write_to_logs():
    for path in _scheduled_files():
        text = "\n".join(_code_lines(path)).replace("\\\n", " ")
        for ln in text.splitlines():
            if re.search(r"(^|\s)--logs(\s|$)|\blogs --day", ln):
                assert "--write" not in ln, f"{path.name}: {ln}"


def test_close_day_runs_alerts_after_track_and_never_through_step():
    code = "\n".join(_code_lines(CLOSE))
    assert code.index('track --day "$DAY" >>') < code.index("--bug-alerts")
    line = next(ln for ln in code.splitlines() if "--bug-alerts" in ln)
    assert not line.lstrip().startswith("step ")  # a failure must not enter the status/manifest
    block = code[code.index("ALERT_FLAG=") : code.index("REVIEW_REPORT=")]
    assert "--logs" in block and "--write" not in block
    assert "logscan-candidates-$DAY.txt" in block
    assert "close-day not affected" in block
    assert '--day "$DAY"' in block


def test_alerts_are_sent_only_for_a_same_day_run():
    code = "\n".join(_code_lines(CLOSE))
    assert 'ALERT_FLAG="--bug-alerts"; [ "$DAY" = "$(date +%F)" ] || ALERT_FLAG=""' in code


def test_close_day_script_syntax_is_valid():
    result = subprocess.run(["bash", "-n", str(CLOSE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_schedule_definition_agrees_with_jobs_cron_lines():
    """DUE, EXPECTED, RETRY_BEFORE, FINAL_DEADMAN_CLOSE and the script headers: one schedule."""
    from signal_engine.ops import automation, jobs, workflow_status

    proposed = {spec.label: spec.times[-1] for spec in jobs.CRON_LINES}
    assert {k: workflow_status.DUE[k] for k in workflow_status.DUE} == {
        "open-day": proposed["open-day"],
        "liveness": proposed["liveness"],
        "close-day": proposed["close-day"],
    }
    for kind, phases in automation.EXPECTED.items():
        for phase, label, hhmm in phases:
            assert hhmm == proposed[phase] and hhmm in label, (kind, phase)
    assert automation.FINAL_DEADMAN_CLOSE == proposed["deadman close"]
    # the retry cut-off sits after recover and the 08:56 line it replaces, before open-day
    assert proposed["recover"] < "08:56" < automation.RETRY_BEFORE < proposed["open-day"]
    assert proposed["close-day retry"] < proposed["deadman close"]
    # cron comments in the shell headers carry the same times
    pattern = re.compile(
        r"^#\s+(\d+) (\d+) \* \* 1-5 \S*/(run_phase\.sh|run_upkeep\.sh) (.+?) >>", re.M
    )
    seen = {}
    for name in ("ops/run_phase.sh", "ops/run_upkeep.sh"):
        for minute, hour, _, args in pattern.findall((SE / name).read_text(encoding="utf-8")):
            seen[args.strip()] = f"{int(hour):02d}:{int(minute):02d}"
    by_args = {
        spec.needle.split(".sh ", 1)[1]: spec.times[-1]
        for spec in jobs.CRON_LINES
        if ".sh " in spec.needle and "close_day" not in spec.needle
    }
    assert seen and seen == by_args
    close = CLOSE.read_text(encoding="utf-8")
    for text in (
        f"{proposed['close-day']}  start",
        f"{proposed['close-day retry']}  --retry",
        f"{proposed['deadman close']}  the close dead-man",
    ):
        assert text in close, text
