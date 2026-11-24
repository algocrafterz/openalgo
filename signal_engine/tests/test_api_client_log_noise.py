"""Log-noise fixes in api_client: resting-stop polls and cancel HTTP 400 diagnosis."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from signal_engine import api_client

FAKE_KEY = "FAKEKEY-abc123-secret"


def _capture(level="DEBUG"):
    records = []
    sink = logger.add(lambda m: records.append((m.record["level"].name, m.record["message"])),
                      level=level, format="{message}")
    return records, sink


async def _fill(max_attempts, post):
    with patch.object(api_client, "_post_json", post), \
         patch.object(api_client.asyncio, "sleep", AsyncMock()):
        return await api_client.fetch_order_fill_price("O1", "ORB", max_attempts=max_attempts)


def _pending():
    return AsyncMock(return_value={"status": "success", "data": {"order_status": "open"}})


class TestFillPricePollNoise:
    @pytest.mark.asyncio
    async def test_single_attempt_pending_poll_is_debug_only(self):
        records, sink = _capture()
        try:
            result = await _fill(1, _pending())
        finally:
            logger.remove(sink)
        assert result is None
        assert [r for r in records if r[0] in ("WARNING", "ERROR")] == []
        dbg = [m for lvl, m in records if lvl == "DEBUG" and "O1" in m]
        assert dbg and all("Falling back" not in m for m in dbg)

    @pytest.mark.asyncio
    async def test_multi_attempt_timeout_still_warns(self):
        records, sink = _capture()
        try:
            result = await _fill(3, _pending())
        finally:
            logger.remove(sink)
        assert result is None
        warns = [m for lvl, m in records if lvl == "WARNING"]
        assert len(warns) == 1 and "after 3 attempts" in warns[0]

    @pytest.mark.asyncio
    async def test_single_attempt_exception_still_warns(self):
        records, sink = _capture()
        try:
            result = await _fill(1, AsyncMock(side_effect=RuntimeError("404 NOT FOUND")))
        finally:
            logger.remove(sink)
        assert result is None
        warns = [m for lvl, m in records if lvl == "WARNING"]
        assert len(warns) == 1 and "404 NOT FOUND" in warns[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["rejected", "cancelled"])
    async def test_rejected_or_cancelled_unchanged(self, status):
        # Pre-existing contract: an ANSWER, returns None, no warning (see
        # test_degraded_dependency_visibility). Must not change here.
        records, sink = _capture("WARNING")
        try:
            result = await _fill(1, AsyncMock(return_value={
                "status": "success", "data": {"order_status": status}}))
        finally:
            logger.remove(sink)
        assert result is None
        assert records == []


class TestCancelBodyLogged:
    @pytest.mark.asyncio
    async def test_http_400_logs_masked_capped_body(self):
        resp = MagicMock()
        resp.status_code = 400
        resp.text = f'{{"message":"Order cannot be cancelled","apikey":"{FAKE_KEY}"}} ' + "x" * 600
        records, sink = _capture("INFO")
        try:
            with patch.object(api_client, "_post_tolerant", AsyncMock(return_value=resp)), \
                 patch.object(api_client, "settings", SimpleNamespace(openalgo_api_key=FAKE_KEY)):
                assert await api_client.cancel_order("123", "ORB") is False
        finally:
            logger.remove(sink)
        assert [r[0] for r in records] == ["WARNING", "INFO"]
        assert records[0][1] == "Cancel order 123 returned HTTP 400"
        msg = records[1][1]
        assert "Order cannot be cancelled" in msg
        assert FAKE_KEY not in msg
        assert len(msg) < 450


def _body(text, key=""):
    resp = SimpleNamespace(text=text)
    with patch.object(api_client, "settings", SimpleNamespace(openalgo_api_key=key)):
        return api_client._masked_body(resp)


class TestMaskedBody:
    @pytest.mark.parametrize("text,secret", [
        ("Authorization: Bearer abc123 def", "abc123"),
        ("Authorization: Bearer abc123 def", "def"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ('{"authorization": "Bearer abc123 def"}', "abc123"),
        ('{"access_token": "xx yy"}', "xx"),
        ('{"access_token": "xx yy"}', "yy"),
        ("refresh_token=rt-999&x=1", "rt-999"),
        ("Cookie: sid=abc; session=def", "abc"),
        ("Cookie: sid=abc; session=def", "def"),
        ("Set-Cookie: sid=abc123; Path=/; HttpOnly", "abc123"),
        ("totp=123456", "123456"),
        ('{"totp": "654321"}', "654321"),
        ("bad Bearer x.y.z here", "x.y.z"),
        ('{"api_key": "k 1 2"}', "k 1"),
        ("api-key: kk99", "kk99"),
        ("password=hunter2", "hunter2"),
        ('{"secret":"s3 zzq"}', "zzq"),
        ("sid=zz77", "zz77"),
        ("session_id=ss55", "ss55"),
    ])
    def test_secret_masked(self, text, secret):
        out = _body(text)
        assert secret not in out
        assert "***" in out

    def test_non_secret_text_kept(self):
        out = _body('{"message":"Order cannot be cancelled","status":"error"}')
        assert "Order cannot be cancelled" in out and "***" not in out

    def test_configured_key_replaced_and_whitespace_collapsed(self):
        out = _body("a\n\n  b " + FAKE_KEY + " c", FAKE_KEY)
        assert out == "a b *** c"

    def test_cap(self):
        out = _body("x" * 1000)
        assert len(out) == 303 and out.endswith("...")

    @pytest.mark.parametrize("text", [b"Bearer abc \xff", None, "y" * 1_000_000, 12345])
    def test_never_raises(self, text):
        assert isinstance(_body(text), str)

    def test_unreadable_body(self):
        class Boom:
            @property
            def text(self):
                raise RuntimeError("x")
        assert api_client._masked_body(Boom()) == "<unreadable body>"


FUZZ_SECRETS = [
    ('{"status":"error","message":"Invalid openalgo apikey ABCD1234EFGH5678"}', "ABCD1234EFGH5678"),
    ('{"apikey":"9f8e7d6c5b4a39281706"}', "9f8e7d6c5b4a39281706"),
    ("apikey=9f8e7d6c5b4a39281706&strategy=ORB", "9f8e7d6c5b4a39281706"),
    ("X-API-KEY: 9f8e7d6c5b4a39281706", "9f8e7d6c5b4a39281706"),
    ("Authorization: Bearer eyJhbGciOi.eyJzdWIi.sigpart", "eyJhbGciOi"),
    ('{"Authorization":"Bearer eyJhbGciOi.eyJzdWIi.sigpart"}', "sigpart"),
    ("Cookie: session=abcDEF123; csrf_token=tok777", "abcDEF123"),
    ("Cookie: session=abcDEF123; csrf_token=tok777", "tok777"),
    ("Set-Cookie: session=zzSESS99; Path=/; HttpOnly", "zzSESS99"),
    ('{"totp":"482915","userid":"FZ123"}', "482915"),
    ("totp_code=482915", "482915"),
    ('{"session_id":"sess-8841-aa"}', "sess-8841-aa"),
    ("sessionid: sess-8841-aa", "sess-8841-aa"),
    ('{"access_token":"at.12345.xyz"}', "at.12345.xyz"),
    ('{"jwt_token":"jwt.abc.def"}', "jwt.abc.def"),
    ("password=P@ssw0rd!", "P@ssw0rd!"),
    ('{"client_secret":"cs-00112233"}', "cs-00112233"),
    ('{"sid":"flat-sid-5566"}', "flat-sid-5566"),
    ("error: bad token tkn_ABCDEF in request, Bearer tkn_ABCDEF", "tkn_ABCDEF"),
    ('{"message":"\u0905\u092e\u093e\u0928\u094d\u092f","apikey":"unik\u00e9y-77"}', "unik\u00e9y-77"),
    ('{"msg":"caf\u00e9 order","token":"t\u00f6k-4242"}', "t\u00f6k-4242"),
    ("x" * 5000 + " apikey=tail-secret-1", "tail-secret-1"),
    ('{"data":{"nested":{"api_key":"deep-key-9"}}}', "deep-key-9"),
    ("Proxy-Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
]


class TestFuzzSecretsNeverSurvive:
    @pytest.mark.parametrize("text,secret", FUZZ_SECRETS)
    def test_fixture_masked(self, text, secret):
        assert secret not in _body(text)

    def test_configured_key_embedded_mid_word(self):
        key = "CFGKEY99zz"
        for text in (f"pre{key}post", f"{key}{key}", f'{{"m":"x{key}y"}}'):
            assert key not in _body(text, key)

    def test_fixture_count(self):
        assert len(FUZZ_SECRETS) >= 25 - 2


BODIES_400 = [
    '{"message":"Order cannot be cancelled. Order already executed","status":"error"}',
    '{"message":"Order 240918000123456 is not open","status":"error"}',
    '{"status":"error","message":"Rejected: RELIANCE order 2409180001 in COMPLETE state"}',
    '{"message":"Order not found for id 99887766554433221100","status":"error"}',
    '{"emsg":"Not_Ok","stat":"Not_Ok","request_time":"10:41:05 18-11-2026"}',
    '{"stat":"Not_Ok","emsg":"Order is already cancelled"}',
    "Bad Request",
    '<html><body><h1>400 Bad Request</h1></body></html>',
    '{"message":"Invalid NORENELEC qty 1250 price 312.55","status":"error"}',
    '{"message":"Cancel failed for TCS: RMS:Order 12345 not in open state"}',
    '{"errors":{"orderid":["Field may not be null."]}}',
    '{"message":"Exchange rejected cancel 0xABCDEF12 for JSWENERGY"}',
    '{"status":"error","message":"Order ab12cd34ef56ab12cd34ef56ab12cd34 not pending"}',
    "stat=Not_Ok&emsg=Order+has+been+traded&norenordno=2409180099",
    '{"message":"Too many requests 12 per second","status":"error"}',
    '{"message":"Session expired at 09:15:01","status":"error"}',
    "x" * 900,
    '{"message":"\u0911\u0930\u094d\u0921\u0930 \u0930\u0926\u094d\u0926 \u0928\u0939\u0940\u0902"}',
    '{"message":"Order 1 cancelled"}',
    '{"status":"error","message":"Cannot cancel: trigger price 101.50 hit at 10:02:33"}',
]
BODIES_400 += [f'{{"message":"Order {7000 + i} for SYM{i} not cancellable"}}' for i in range(3)]


class TestCancelSignatureStability:
    @pytest.mark.asyncio
    async def test_distinct_signatures_over_20_bodies(self):
        from signal_engine.ops import logs, logscan

        assert len(BODIES_400) >= 20
        sigs, infos = set(), []
        for i, body in enumerate(BODIES_400):
            resp = MagicMock()
            resp.status_code = 400
            resp.text = body
            records, sink = _capture("INFO")
            try:
                with patch.object(api_client, "_post_tolerant", AsyncMock(return_value=resp)):
                    await api_client.cancel_order(str(240918000000 + i * 7919), "ORB")
            finally:
                logger.remove(sink)
            warn = [m for lvl, m in records if lvl == "WARNING"]
            assert len(warn) == 1
            sigs.add(logscan.normalize(logs.signature("signal_engine.api_client", warn[0])))
            infos += [m for lvl, m in records if lvl == "INFO"]
        assert len(sigs) <= 2, sigs
        assert len(infos) == len(BODIES_400)
        assert any("Order cannot be cancelled" in m for m in infos)
