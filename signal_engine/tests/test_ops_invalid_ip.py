"""INVALID_IP finding: the broker rejected the login from an IP that is not on the allow-list (BUG-024)."""

from signal_engine.ops import ipalert

DAY = "2026-11-06"
LOG = (
    "[2026-11-06 09:44:38,259] ERROR in auth_api: Flattrade token exchange failed: "
    "Invalid Input : INVALID_IP\n"
)


class Fakes:
    def __init__(self, ip="49.207.12.34"):
        self.ip = ip

    def egress(self):
        return self.ip


def fire(text, log="", fakes=None):
    fakes = fakes or Fakes()
    result = ipalert.log_invalid_ip(text, DAY, egress=fakes.egress, read_log=lambda day: log)
    return result, fakes


def test_mask_ip():
    assert ipalert.mask_ip("49.207.12.34") == "49.207.xxx.xxx"
    assert ipalert.mask_ip("") == "unknown"
    assert ipalert.mask_ip("not an ip") == "unknown"
    assert ipalert.mask_ip("2401:4900:1:2::3") == "2401:4900:xxxx"


def test_invalid_ip_in_the_failure_text_logs_the_allow_list_message(logged):
    result, _ = fire("HTTP 403: Invalid Input : INVALID_IP")
    assert result is True
    lines = logged.messages("ERROR")
    assert len(lines) == 1
    assert "update the Flattrade IP allow-list" in lines[0]
    assert "49.207.xxx.xxx" in lines[0] and "12.34" not in lines[0]


def test_failure_text_without_invalid_ip_and_clean_log_logs_nothing(logged):
    result, _ = fire("HTTP 403: Invalid openalgo apikey", log="all fine\n")
    assert result is False and logged.messages("WARNING") == []


def test_apikey_403_with_invalid_ip_in_todays_openalgo_log_still_logs(logged):
    """On 2026-11-06 the funds API said 'Invalid openalgo apikey'; INVALID_IP was only in the app log."""
    result, _ = fire("HTTP 403: Invalid openalgo apikey", log=LOG)
    assert result is True and len(logged.messages("ERROR")) == 1


def test_unknown_egress_still_logs(logged):
    result, _ = fire("INVALID_IP", fakes=Fakes(ip=""))
    assert result is True and "unknown" in logged.text("ERROR")


def test_helpers_that_raise_never_raise(logged):
    def boom(*a, **k):
        raise RuntimeError("x")

    assert ipalert.log_invalid_ip("INVALID_IP", DAY, egress=boom, read_log=boom) is False
    assert logged.has("INVALID_IP check failed: RuntimeError", "ERROR")


def test_no_full_ip_is_logged(logged):
    fire("INVALID_IP")
    lines = [msg for _no, _name, msg in logged]
    assert lines and all("49.207.12.34" not in msg for msg in lines)
    assert any("49.207.xxx.xxx" in msg for msg in lines)


def test_readiness_broker_check_logs_the_invalid_ip_finding(monkeypatch):
    from signal_engine.ops import readiness

    seen = []
    monkeypatch.setattr(readiness.ipalert, "log_invalid_ip", lambda text, day, **k: seen.append((text, day)))

    async def boom():
        raise RuntimeError("Broker auth failed: Invalid Input : INVALID_IP")

    import signal_engine.api_client as api

    monkeypatch.setattr(api, "fetch_available_capital", boom)
    checks = readiness._broker_and_mode("2026-11-06")
    assert checks[0].status == "FAIL" and seen and "INVALID_IP" in seen[0][0]


def test_readiness_broker_check_does_not_check_the_ip_when_healthy(monkeypatch):
    from signal_engine.ops import readiness

    seen = []
    monkeypatch.setattr(readiness.ipalert, "log_invalid_ip", lambda *a, **k: seen.append(a))
    import signal_engine.api_client as api

    async def cap():
        return 1000.0

    async def mode():
        return "analyze", True

    monkeypatch.setattr(api, "fetch_available_capital", cap)
    monkeypatch.setattr(api, "fetch_trading_mode", mode)
    assert readiness._broker_and_mode("2026-11-06")[0].status == "OK" and seen == []
