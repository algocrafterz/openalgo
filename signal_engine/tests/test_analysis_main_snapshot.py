"""Tests for signal_engine/analysis/__main__.py's broker tradebook snapshot.

_fetch_tradebook() is the only place that calls OpenAlgo's /api/v1/tradebook endpoint to
capture the day's real fills - every other number in the EOD/weekly reports is signal-side
without it. See ledger.py's module docstring for why this join matters.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from signal_engine.analysis import __main__ as analysis_main


def _fake_response(status_code=200, json_data=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    return response


class TestFetchTradebook:
    @pytest.mark.asyncio
    async def test_success_sends_apikey_and_parses_fills(self, monkeypatch):
        captured = {}

        async def fake_post_tolerant(path, payload):
            captured["path"] = path
            captured["payload"] = payload
            return _fake_response(200, {"status": "success", "data": [{"orderid": "1"}]})

        monkeypatch.setattr("signal_engine.api_client._post_tolerant", fake_post_tolerant)

        trades = await analysis_main._fetch_tradebook()

        assert trades == [{"orderid": "1"}]
        assert captured["path"] == "tradebook"
        assert "apikey" in captured["payload"]

    @pytest.mark.asyncio
    async def test_http_400_raises_with_status_and_body_not_repr(self, monkeypatch):
        async def fake_post_tolerant(path, payload):
            return _fake_response(400, {"status": "error", "message": {"apikey": ["Missing data"]}})

        monkeypatch.setattr("signal_engine.api_client._post_tolerant", fake_post_tolerant)

        with pytest.raises(RuntimeError, match=r"HTTP 400.*Missing data"):
            await analysis_main._fetch_tradebook()

    @pytest.mark.asyncio
    async def test_non_json_error_body_does_not_crash(self, monkeypatch):
        async def fake_post_tolerant(path, payload):
            response = MagicMock()
            response.status_code = 500
            response.json.side_effect = ValueError("not json")
            response.text = "internal server error"
            return response

        monkeypatch.setattr("signal_engine.api_client._post_tolerant", fake_post_tolerant)

        with pytest.raises(RuntimeError, match=r"HTTP 500"):
            await analysis_main._fetch_tradebook()
