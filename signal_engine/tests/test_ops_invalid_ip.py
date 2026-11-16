"""INVALID_IP alert: the broker rejected the login from an IP that is not on the allow-list (BUG-024)."""

from signal_engine.ops import ipalert

DAY = "2026-11-06"
LOG = (
    "[2026-11-06 09:44:38,259] ERROR in auth_api: Flattrade token exchange failed: "
    "Invalid Input : INVALID_IP\n"
)


class Fakes:
    def __init__(self, ip="49.207.12.34", ok=True):
        self.sent = []
        self.ip = ip
        self.ok = ok

    def send(self, text, **kw):
        self.sent.append((text, kw))
        return self.ok

    def egress(self):
        return self.ip


def fire(text, log="", fakes=None):
    fakes = fakes or Fakes()
    result = ipalert.maybe_alert(
        text, DAY, send=fakes.send, egress=fakes.egress, read_log=lambda day: log
    )
    return result, fakes


def test_mask_ip():
    assert ipalert.mask_ip("49.207.12.34") == "49.207.xxx.xxx"
    assert ipalert.mask_ip("") == "unknown"
    assert ipalert.mask_ip("not an ip") == "unknown"
    assert ipalert.mask_ip("2401:4900:1:2::3") == "2401:4900:xxxx"


def test_invalid_ip_in_the_failure_text_sends_the_allow_list_message():
    result, f = fire("HTTP 403: Invalid Input : INVALID_IP")
    assert result is True and len(f.sent) == 1
    text, kw = f.sent[0]
    assert "update the Flattrade IP allow-list" in text
    assert "49.207.xxx.xxx" in text and "12.34" not in text
    assert kw["severity"] == "RED" and kw["kind"] == "invalid-ip"


def test_failure_text_without_invalid_ip_and_clean_log_sends_nothing():
    result, f = fire("HTTP 403: Invalid openalgo apikey", log="all fine\n")
    assert result is False and f.sent == []


def test_apikey_403_with_invalid_ip_in_todays_openalgo_log_still_alerts():
    """On 2026-11-06 the funds API said 'Invalid openalgo apikey'; INVALID_IP was only in the app log."""
    result, f = fire("HTTP 403: Invalid openalgo apikey", log=LOG)
    assert result is True and len(f.sent) == 1


def test_unknown_egress_still_alerts():
    result, f = fire("INVALID_IP", fakes=Fakes(ip=""))
    assert result is True and "unknown" in f.sent[0][0]


def test_helpers_that_raise_never_raise():
    def boom(*a, **k):
        raise RuntimeError("x")

    assert ipalert.maybe_alert("INVALID_IP", DAY, send=boom, egress=boom, read_log=boom) is False


def test_no_full_ip_is_logged(caplog):
    from loguru import logger

    lines = []
    sink = logger.add(lines.append, format="{message}")
    try:
        fire("INVALID_IP")
    finally:
        logger.remove(sink)
    assert lines and all("49.207.12.34" not in str(m) for m in lines)
    assert any("49.207.xxx.xxx" in str(m) for m in lines)


def test_readiness_broker_check_calls_the_alert(monkeypatch):
    from signal_engine.ops import readiness

    seen = []
    monkeypatch.setattr(readiness.ipalert, "maybe_alert", lambda text, day, **k: seen.append((text, day)))

    async def boom():
        raise RuntimeError("Broker auth failed: Invalid Input : INVALID_IP")

    import signal_engine.api_client as api

    monkeypatch.setattr(api, "fetch_available_capital", boom)
    checks = readiness._broker_and_mode("2026-11-06")
    assert checks[0].status == "FAIL" and seen and "INVALID_IP" in seen[0][0]


def test_readiness_broker_check_does_not_alert_when_healthy(monkeypatch):
    from signal_engine.ops import readiness

    seen = []
    monkeypatch.setattr(readiness.ipalert, "maybe_alert", lambda *a, **k: seen.append(a))
    import signal_engine.api_client as api

    async def cap():
        return 1000.0

    async def mode():
        return "analyze", True

    monkeypatch.setattr(api, "fetch_available_capital", cap)
    monkeypatch.setattr(api, "fetch_trading_mode", mode)
    assert readiness._broker_and_mode("2026-11-06")[0].status == "OK" and seen == []
