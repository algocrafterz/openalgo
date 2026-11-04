"""Child processes for the bug tracker locking tests. Run as: python test/ops_findings_child.py MODE ...

Not a test file (no test_ prefix), so pytest never collects it. Each mode is one real OS process,
so the locking tests exercise cross-process behaviour, not thread behaviour.

    http   REPORTS TRACKER START_AT PAYLOAD_JSON   one POST to the real /api/ops/report-finding route
    cli    TRACKER START_AT PREFIX COUNT           the real CLI, `add` COUNT times
    hold   TRACKER SECONDS READY_FILE              take the tracker lock and sleep (to be killed)
    crash  TRACKER                                 die between writing the new file and swapping it in
"""

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False  # a child reads no .env
dotenv.main.load_dotenv = dotenv.load_dotenv

from signal_engine.bugs import store  # noqa: E402


def _wait_until(start_at: float) -> None:
    while time.time() < start_at:
        time.sleep(0.001)


def _http(reports: str, tracker: str, start_at: float, payload: dict) -> None:
    from flask import Flask
    from flask_wtf.csrf import CSRFProtect, generate_csrf

    import blueprints.ops_findings as mod
    import utils.session as session_utils
    from limiter import limiter

    mod.REPORTS_DIR = reports
    mod.TRACKER_PATH = tracker
    store.TRACKER_PATH = tracker
    limiter.enabled = False
    session_utils.is_session_expiry_disabled = lambda: True

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = True
    CSRFProtect(app)
    app.register_blueprint(mod.ops_findings_bp)

    @app.route("/t/csrf")
    def token():
        return generate_csrf()

    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = "tester"
        session["logged_in"] = True
        session["login_time"] = "2026-01-01T09:00:00+05:30"
    headers = {"X-CSRFToken": client.get("/t/csrf").get_data(as_text=True)}
    _wait_until(start_at)
    resp = client.post("/api/ops/report-finding", json=payload, headers=headers)
    body = resp.get_json() or {}
    print(
        json.dumps(
            {
                "http": resp.status_code,
                "created": body.get("created"),
                "recorded": body.get("recorded"),
                "bug_id": (body.get("bug") or {}).get("id"),
                "message": body.get("message"),
            }
        )
    )


def _cli(tracker: str, start_at: float, prefix: str, count: int) -> None:
    from signal_engine.bugs.__main__ import main

    store.TRACKER_PATH = tracker
    _wait_until(start_at)
    codes = []
    for i in range(count):
        codes.append(
            main(
                [
                    "add",
                    "--title",
                    f"{prefix} number {i}",
                    "--kind",
                    "BUG",
                    "--severity",
                    "LOW",
                    "--area",
                    "ops",
                    "--seen",
                    "2026-11-01",
                    "--impact",
                    "from a CLI process",
                ]
            )
        )
    print(json.dumps({"cli_exit_codes": codes}))


def _hold(tracker: str, seconds: float, ready_file: str) -> None:
    with store._locked(tracker):
        with open(ready_file, "w") as handle:
            handle.write("held")
        time.sleep(seconds)


def _crash(tracker: str) -> None:
    def die(src, dst):
        os._exit(7)  # the new file is fully written, the swap never happens

    os.replace = die
    store.add(
        title="Written by a process that crashed",
        kind="BUG",
        severity="LOW",
        area="ops",
        seen="2026-11-01",
        impact="x",
        path=tracker,
    )


if __name__ == "__main__":
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "http":
        _http(args[0], args[1], float(args[2]), json.loads(args[3]))
    elif mode == "cli":
        _cli(args[0], float(args[1]), args[2], int(args[3]))
    elif mode == "hold":
        _hold(args[0], float(args[1]), args[2])
    elif mode == "crash":
        _crash(args[0])
