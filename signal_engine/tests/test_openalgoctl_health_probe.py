"""openalgoctl.sh must probe a backend route that does not depend on the frontend build.

2026-10-14: the probe hit "/", which serves frontend/dist/index.html and answers HTTP 503 ("Frontend Not
Built") whenever a concurrent frontend build leaves dist empty for a moment. `curl -f` counts 503 as a
failure; three in a row mark the app wedged and restart it, mid-session.
"""

import re
from pathlib import Path

CTL = Path(__file__).resolve().parents[1] / "scripts" / "openalgoctl.sh"


def _health_url() -> str:
    match = re.search(r'^HEALTH_URL="([^"]+)"', CTL.read_text(), re.MULTILINE)
    assert match, "HEALTH_URL is not defined in openalgoctl.sh"
    return match.group(1)


def test_probe_targets_the_backend_health_route_not_the_frontend_root():
    assert _health_url() == "http://127.0.0.1:5000/health/status"


def test_the_probe_url_is_not_the_react_root():
    assert not _health_url().rstrip("/").endswith(":5000")


def test_the_health_route_exists_and_is_backend_only():
    """/health/status is registered by blueprints/health.py and returns the service-health payload."""
    source = (Path(__file__).resolve().parents[2] / "blueprints" / "health.py").read_text()
    assert '@health_bp.route("/status"' in source
