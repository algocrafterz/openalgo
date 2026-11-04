"""The report-a-finding route under the real gunicorn workers, eventlet (the default) and gthread.

The tracker lock is waited on by polling, never inside the operating system. Under eventlet one
greenlet blocked in a C wait would stop the whole worker, and the holder on the same hub could
never release it, so the point of these tests is what the OTHER requests see while one request
waits for the lock: a /ping on the same worker must keep answering.

Each test starts a real `gunicorn -w 1` with the worker class under test, a tiny WSGI module that
mounts the real blueprint, and a temporary tracker. The real bugs.json is never opened.
"""

import fcntl
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_findings_fixtures import DAYS, REPO, child_env, write_day  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="gunicorn workers need POSIX")

CHILD = REPO / "test" / "ops_findings_child.py"
DAY = DAYS[0]

PROBE = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {repo!r})
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.main.load_dotenv = dotenv.load_dotenv
    from flask import Flask
    import blueprints.ops_findings as mod
    import utils.session as session_utils
    from limiter import limiter
    from signal_engine.bugs import store

    mod.REPORTS_DIR = os.environ["PROBE_REPORTS"]
    mod.TRACKER_PATH = os.environ["PROBE_TRACKER"]
    store.TRACKER_PATH = os.environ["PROBE_TRACKER"]
    store.LOCK_TIMEOUT = float(os.environ["PROBE_LOCK_TIMEOUT"])
    limiter.enabled = False
    session_utils.is_session_valid = lambda: True  # CSRF and sessions are tested elsewhere

    app = Flask(__name__)
    app.secret_key = "x"
    app.register_blueprint(mod.ops_findings_bp)

    @app.route("/ping")
    def ping():
        return "pong"

    @app.route("/patched")
    def patched():
        try:
            import eventlet.patcher as p
            return "eventlet" if p.is_monkey_patched("time") else "plain"
        except Exception:
            return "plain"
    """
)

WORKERS = {
    "eventlet": ["--worker-class", "eventlet", "-w", "1"],
    "gthread": ["--worker-class", "gthread", "--threads", "16", "-w", "1"],
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(params=sorted(WORKERS))
def server(request, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    for day in DAYS:
        write_day(reports, day)
    tracker = tmp_path / "bugs.json"
    tracker.write_text(json.dumps({"version": 1, "bugs": []}))
    (tmp_path / "probe.py").write_text(PROBE.format(repo=str(REPO)))

    port = _free_port()
    env = child_env(tmp_path)
    env.update(
        {
            "PROBE_REPORTS": str(reports),
            "PROBE_TRACKER": str(tracker),
            "PROBE_LOCK_TIMEOUT": "10",
            "PYTHONPATH": f"{tmp_path}{os.pathsep}{REPO}",
        }
    )
    log = (tmp_path / "gunicorn.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gunicorn",
            *WORKERS[request.param],
            "-b",
            f"127.0.0.1:{port}",
            "--timeout",
            "120",
            "probe:app",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/ping", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail(f"gunicorn did not start: {(tmp_path / 'gunicorn.log').read_text()[-1500:]}")
    try:
        yield {
            "worker": request.param,
            "base": base,
            "tracker": tracker,
            "tmp": tmp_path,
            "reports": reports,
            "env": env,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _payload(day=DAY, **over):
    base = {
        "day": day,
        "phase": "open-day",
        "group": "open-day",
        "name": f"close-day {day}",
        "severity": "MEDIUM",
        "kind": "EXECUTION",
        "area": "ops",
        "impact": "Not assessed",
        "severity_confirmed": True,
    }
    return {**base, **over}


def _bugs(tracker: Path) -> list:
    return json.loads(tracker.read_text())["bugs"]


class _Holder:
    """Holds the tracker lock from this (separate) process, the way a CLI write would."""

    def __init__(self, tracker: Path):
        self.handle = open(f"{tracker}.lock", "a")
        fcntl.flock(self.handle, fcntl.LOCK_EX)

    def release(self):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def _post_in_thread(base, payload, out):
    def run():
        started = time.time()
        try:
            r = httpx.post(f"{base}/api/ops/report-finding", json=payload, timeout=60)
            out.update(status=r.status_code, body=r.json(), seconds=time.time() - started)
        except Exception as exc:  # noqa: BLE001 - reported by the assertion that reads `out`
            out.update(error=repr(exc))

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _ping_latencies(base, count=12, spacing=0.1):
    worst = 0.0
    for _ in range(count):
        started = time.time()
        assert httpx.get(f"{base}/ping", timeout=5).text == "pong"
        worst = max(worst, time.time() - started)
        time.sleep(spacing)
    return worst


def test_the_worker_really_is_the_configured_class(server):
    expected = "eventlet" if server["worker"] == "eventlet" else "plain"
    assert httpx.get(f"{server['base']}/patched", timeout=5).text == expected


def test_a_request_waiting_for_the_lock_does_not_stop_the_worker(server):
    """The eventlet hazard: a blocking wait would freeze /ping until the holder let go."""
    holder = _Holder(server["tracker"])
    out: dict = {}
    thread = _post_in_thread(server["base"], _payload(), out)
    try:
        time.sleep(0.6)
        assert thread.is_alive() and not out, "the write should be waiting for the lock"
        assert _ping_latencies(server["base"]) < 1.0, "other requests stalled behind the lock"
        assert _bugs(server["tracker"]) == []
    finally:
        holder.release()
    thread.join(timeout=30)
    assert out["status"] == 201 and out["body"]["created"] is True
    assert len(_bugs(server["tracker"])) == 1


def test_a_lock_held_too_long_gives_a_busy_answer_while_the_worker_stays_up(server):
    env_timeout = 10.0
    holder = _Holder(server["tracker"])
    out: dict = {}
    thread = _post_in_thread(server["base"], _payload(), out)
    try:
        time.sleep(1.0)
        assert _ping_latencies(server["base"], count=5) < 1.0
        # the probe's LOCK_TIMEOUT is 10s: let the request time out for real
        thread.join(timeout=env_timeout + 20)
        assert out["status"] == 503
        assert out["body"]["message"] == "The bug tracker is busy. Try again in a moment."
        assert out["seconds"] >= env_timeout - 1
        assert _bugs(server["tracker"]) == []
    finally:
        holder.release()
    after = httpx.post(f"{server['base']}/api/ops/report-finding", json=_payload(), timeout=30)
    assert after.status_code == 201
    assert len(_bugs(server["tracker"])) == 1


def test_a_burst_of_requests_and_cli_processes_through_one_worker_loses_nothing(server):
    base = server["base"]
    start_at = time.time() + 5
    cli = [
        subprocess.Popen(
            [
                sys.executable,
                str(CHILD),
                "cli",
                str(server["tracker"]),
                str(start_at),
                f"Burst {n}",
                "10",
            ],
            cwd=str(REPO),
            env=child_env(server["tmp"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(2)
    ]
    outs: list[dict] = [{} for _ in range(15)]
    payloads = [_payload(DAYS[i % len(DAYS)]) for i in range(15)]  # one finding, five days, 3x each
    while time.time() < start_at:
        time.sleep(0.01)
    threads = [_post_in_thread(base, p, o) for p, o in zip(payloads, outs, strict=True)]
    for t in threads:
        t.join(timeout=90)
    for p in cli:
        stdout, stderr = p.communicate(timeout=120)
        assert p.returncode == 0, stderr[-1000:]
    assert all("error" not in o for o in outs), outs
    assert sorted({o["status"] for o in outs}) == [200, 201]
    assert [o["status"] for o in outs].count(201) == 1
    bugs = _bugs(server["tracker"])
    assert len(bugs) == 1 + 20, "a write was lost"
    ids = [b["id"] for b in bugs]
    assert len(set(ids)) == len(ids)
    finding = next(b for b in bugs if any(s.startswith("workflow-finding:") for s in b["sources"]))
    assert finding["occurrences"] == sorted(DAYS)
    assert len(finding["evidence"]) == len(DAYS)
