"""Shared setup for the bug tracker process tests (not a test file: no test_ prefix)."""

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DAYS = [f"2026-11-{d:02d}" for d in (3, 4, 5, 6, 9)]


def write_day(folder: Path, day: str) -> None:
    (folder / f"open-day-{day}.json").write_text(
        json.dumps(
            {
                "phase": "open-day",
                "day": day,
                "verdict": "RED",
                "summary": "s",
                "checks": [
                    {
                        "name": f"close-day {day}",
                        "status": "FAIL",
                        "evidence": f"close-day {day}: failed steps: ['jobs']",
                        "plain": "Yesterday's end-of-day job had a failed step or did not run.",
                    }
                ],
            }
        )
    )
    (folder / f"open-day-{day}-status.tsv").write_text(
        "step\tstarted\tended\texit\ncheck\t09:12:03\t09:12:07\t1\n"
    )
    (folder / f"logs-{day}.json").write_text(
        json.dumps(
            {
                "phase": "logs",
                "day": day,
                "verdict": "AMBER",
                "summary": "s",
                "checks": [
                    {
                        "name": "NEW x4",
                        "status": "WARN",
                        "evidence": "first 13:20:07; services.history_service: Error fetching",
                        "plain": "A new kind of error appeared today.",
                    }
                ],
            }
        )
    )
    (folder / f"close-day-{day}-status.tsv").write_text(
        "step\tstarted\tended\texit\nlogs\t15:36:00\t15:36:09\t0\n"
    )
    (folder / f"execution-health-{day}.md").write_text(
        "| Metric | Today | Status | Prev day |\n| --- | --- | --- | --- |\n"
        "| Limiter refused calls (total) | 3575 | REGRESSION | 5193 |\n"
    )


def child_env(tmp_path: Path) -> dict:
    db = tmp_path / "db"
    db.mkdir(exist_ok=True)
    e = dict(os.environ)
    e.update(
        {
            "DATABASE_URL": f"sqlite:///{(db / 'openalgo.db').as_posix()}",
            "SANDBOX_DATABASE_URL": f"sqlite:///{(db / 'sandbox.db').as_posix()}",
            "LOGS_DATABASE_URL": f"sqlite:///{(db / 'logs.db').as_posix()}",
            "LATENCY_DATABASE_URL": f"sqlite:///{(db / 'latency.db').as_posix()}",
            "HEALTH_DATABASE_URL": f"sqlite:///{(db / 'health.db').as_posix()}",
            "LOG_DIR": str(tmp_path / "log"),
            "LOG_TO_FILE": "False",
            "API_KEY_PEPPER": "0" * 64,
            "APP_KEY": "test-only-app-key",
            "BROKER_API_KEY": "client:::key:::secret",
            "PYTHONPATH": str(REPO),
        }
    )
    e.pop("OPENALGO_WORKER_CLASS", None)
    return e
