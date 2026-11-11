"""The Market Profile poll reads the vendor's JSON; these pin the cell text it rebuilds, the
signed-out and drift handling, and the fall back to the browser path."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from signal_engine.analysis.breakingtrade import api_scan, fetcher
from signal_engine.analysis.breakingtrade.extractor import normalize_table

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "breakingtrade_scanner_api_2026_11_11.json"
)
_TOKEN = "A" * 43 + ".1794375787459"


def _rows() -> list:
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize(
    "raw, label",
    [
        ("gapUp", "Gap Up"),
        ("openDriveUpwards", "Open Drive ↑"),
        ("buyingTail {A}", "Buy Tail [A]"),
        ("Inside Value Area {M}", "In VA"),
        ("7Single TPO above Days High {D}", "7 TPO ↑ High"),
        ("Above Previous Day High", "Above PDH"),
        ("Normal Var ↑", "Normal Var ↑"),
        (None, ""),
        ("undefined", ""),
    ],
)
def test_translate_signal_matches_vendor_labels(raw, label):
    assert api_scan.translate_signal(raw) == label


def test_fixture_rows_normalize_through_the_shared_path():
    frame = api_scan.build_market_profile_frame(_rows(), {"360ONE": "Banking & Finance"})
    snap = normalize_table(frame, captured_at=None)
    out = snap.frame
    assert snap.kind == "market_profile"
    assert len(out) == len(_rows())
    for column in ("opening", "day_type", "tpo_pos", "tpo_pos_prev", "ib_pct", "price"):
        assert out[column].notna().all(), column
    assert {"gap_up", "above_prior_vah"} & set(out["opening"])
    assert {"buy_tail", "sell_tail"} & set(out["tail"].dropna())
    assert out.loc[out["symbol"] == "360ONE", "sector"].iloc[0] == "Banking & Finance"


def test_vendor_time_becomes_the_snapshot_time():
    from signal_engine.analysis.breakingtrade.extractor import parse_captured_at

    frame = api_scan.build_market_profile_frame(_rows())
    assert parse_captured_at(frame).strftime("%Y-%m-%d") == "2026-11-11"


def _transport(post_status: int, post_body: str, home: str | None = None):
    home_html = home if home is not None else f"<script>btApiToken='{_TOKEN}'</script>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=home_html)
        return httpx.Response(post_status, text=post_body)

    return httpx.MockTransport(handler)


def test_signed_out_reply_raises_signed_out():
    body = '{"error":"Login required for live scanner data."}'
    with api_scan.ApiSession("x", transport=_transport(403, body)) as api:
        with pytest.raises(api_scan.ApiSignedOut):
            api.market_profile_rows()


def test_missing_token_is_an_api_error_not_signed_out():
    with api_scan.ApiSession("x", transport=_transport(200, "[]", home="<html></html>")) as api:
        with pytest.raises(api_scan.ApiError) as err:
            api.market_profile_rows()
    assert not isinstance(err.value, api_scan.ApiSignedOut)


def test_non_json_reply_is_an_api_error():
    with api_scan.ApiSession("x", transport=_transport(200, "<html>maintenance</html>")) as api:
        with pytest.raises(api_scan.ApiError):
            api.market_profile_rows()


def _session(monkeypatch, rows=None, error=None, jsessionid="abc"):
    monkeypatch.setattr(fetcher, "saved_jsessionid", lambda: jsessionid)
    monkeypatch.setattr(fetcher, "_dump_raw_frame", lambda *_a, **_k: None)

    class FakeApi:
        script_version = "123"

        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def market_profile_rows(self):
            if error:
                raise error
            return rows

        def symbol_sectors(self):
            return {}

    monkeypatch.setattr(fetcher.api_scan, "ApiSession", FakeApi)
    return fetcher.ScannerSession()


def test_api_path_returns_snapshot(monkeypatch):
    session = _session(monkeypatch, rows=_rows())
    snap = session._fetch_via_api()
    assert snap is not None and len(snap.frame) == len(_rows())
    assert session.api_note is None and session.script_version == "123"


def test_no_saved_session_uses_browser(monkeypatch):
    session = _session(monkeypatch, rows=_rows(), jsessionid=None)
    assert session._fetch_via_api() is None
    assert "no saved session" in session.api_note


def test_signed_out_uses_browser(monkeypatch):
    session = _session(monkeypatch, error=api_scan.ApiSignedOut("x"))
    assert session._fetch_via_api() is None
    assert session.api_note.startswith("session signed out")


def test_changed_json_shape_is_reported_not_stored(monkeypatch):
    rows = [{k: v for k, v in r.items() if k not in ("opening", "dayType")} for r in _rows()]
    session = _session(monkeypatch, rows=rows)
    assert session._fetch_via_api() is None
    assert "no longer matches" in session.api_note


def test_empty_reply_uses_browser(monkeypatch):
    session = _session(monkeypatch, rows=[])
    assert session._fetch_via_api() is None
    assert "no rows" in session.api_note


def test_fetch_prefers_api_and_skips_the_browser(monkeypatch):
    session = _session(monkeypatch, rows=_rows())
    monkeypatch.setattr(
        session, "_fetch_once", lambda *_a, **_k: pytest.fail("browser must not be used")
    )
    snap = session.fetch("market_profile")
    assert session.last_source == "api" and len(snap.frame) == len(_rows())
