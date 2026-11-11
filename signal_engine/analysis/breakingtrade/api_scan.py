"""Read the Market Profile scanner from the vendor's own JSON endpoint instead of the page.

The scanner page is a thin renderer: it POSTs ``liveIntradayScannerController`` and turns each
JSON row into table cells (``IntradaySignal.js``). Scraping those cells breaks whenever the
vendor re-labels a column, and needs a whole browser, a page reload per poll and a sign-in that
the page may lose. The JSON carries the same facts under stable field names, with the vendor's
own snapshot time (``time``) and its age in seconds (``dataAgeSec``).

This module does two things and nothing else:

1. ``ApiSession`` calls the endpoint over plain HTTP with the signed-in ``JSESSIONID`` cookie and
   the per-page ``X-BT-Token`` that ``Home.jsp`` embeds. A reply of "Login required" raises
   ``ApiSignedOut`` and the caller falls back to the browser, which is the only place a sign-in
   happens. This module never signs in.
2. ``build_market_profile_frame`` rebuilds the cell text the page would have shown, so the rows go
   through the same ``normalize_table`` as a scraped table. The label rules below are a port of
   ``_translateSignal`` / ``_pillBadge`` / ``_tailCell`` / ``_singlePrintCell`` in the vendor
   script; when that script changes, ``test_breakingtrade_api_parity`` and the populated-column
   check in the extractor are what notice.
"""

from __future__ import annotations

import re

import httpx
import pandas as pd

HOME_URL = "https://breakingtrade.com/Home.jsp"
INTRADAY_URL = "https://breakingtrade.com/liveIntradayScannerController"
VOLUME_URL = "https://breakingtrade.com/liveVolumeScannerController"
SYMBOLS_URL = "https://breakingtrade.com/dynamicSymbolController"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\.\d{13}")
_SCRIPT_VERSION_RE = re.compile(r"JavaScript/[A-Za-z]+\.js\?v=(\d+)")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)
_BODY = {"symbol": "NIFTY FUT", "market": "NSE"}


class ApiError(RuntimeError):
    """The endpoint could not be read (network, shape, or an unexpected reply)."""


class ApiSignedOut(ApiError):
    """The vendor says the session is not signed in."""


class ApiSession:
    """One HTTP client holding the signed-in session cookie."""

    def __init__(self, jsessionid: str, timeout: float = 30.0, transport=None):
        self._client = httpx.Client(
            cookies={"JSESSIONID": jsessionid},
            headers={
                "user-agent": _USER_AGENT,
                "x-requested-with": "XMLHttpRequest",
                "referer": HOME_URL,
            },
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self.script_version: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiSession:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _token(self) -> str:
        page = self._client.get(HOME_URL)
        if page.status_code != 200:
            raise ApiError(f"Home page answered {page.status_code}")
        match = _TOKEN_RE.search(page.text)
        if not match:
            raise ApiError("Home page carries no API token any more")
        version = _SCRIPT_VERSION_RE.search(page.text)
        self.script_version = version.group(1) if version else None
        return match.group(0)

    def keepalive(self) -> None:
        """Touch the session so the vendor's idle timeout does not end it between polls."""
        self._token()

    def symbol_sectors(self) -> dict:
        """Symbol -> sector name, as the page's own sector column shows it. Empty on any
        failure: the sector is context, not a reason to lose a poll."""
        try:
            reply = self._client.post(
                SYMBOLS_URL, data={"market": "NSE"}, headers={"x-bt-token": self._token()}
            )
            symbols = reply.json().get("symbols", [])
            return {
                str(item["value"]): str(item["sectorName"])
                for item in symbols
                if item.get("value") and item.get("sectorName")
            }
        except (httpx.HTTPError, ValueError, AttributeError, KeyError, TypeError):
            return {}

    def market_profile_rows(self) -> list:
        return self._rows(INTRADAY_URL)

    def volume_rows(self) -> list:
        return self._rows(VOLUME_URL)

    def _rows(self, url: str) -> list:
        token = self._token()
        reply = self._client.post(url, json=_BODY, headers={"x-bt-token": token})
        if reply.status_code == 403 and "login" in reply.text.lower():
            raise ApiSignedOut("The vendor reports the session is signed out")
        if reply.status_code != 200:
            raise ApiError(f"Scanner endpoint answered {reply.status_code}")
        try:
            rows = reply.json()
        except ValueError as exc:
            raise ApiError("Scanner endpoint did not return JSON") from exc
        if not isinstance(rows, list):
            raise ApiError("Scanner endpoint returned an unexpected shape")
        return rows


# ── cell text, ported from the vendor script ────────────────────────────────────────────────

_LABELS = {
    "Poor High": "Unfinished high",
    "Poor Low": "Unfinished low",
    "Poor H & L": "Unfinished high & low",
    "gapUp": "Gap Up",
    "gapDown": "Gap Down",
    "aboveYesterdayVAH": "Above VAH",
    "belowYesterdayVAL": "Below VAL",
    "withinYesterdayValue": "In Value",
    "openDriveUpwards": "Open Drive ↑",
    "openDriveDownwards": "Open Drive ↓",
    "openTestDriveUpwards": "Test Drive ↑",
    "openTestDriveDownwards": "Test Drive ↓",
    "openRejectionUpwards": "Rejection ↑",
    "openRejectionDownwards": "Rejection ↓",
    "buyingTail": "Buy Tail",
    "sellingTail": "Sell Tail",
    "singlePrintUpSide": "Single Print ↑",
    "singlePrintDownSide": "Single Print ↓",
    "FailedToGoHigh": "Failed High",
    "FailedToGoDown": "Failed Low",
}
for _ref in ("PDH", "IBH"):
    _LABELS[f"failedAuction_up_{_ref}"] = f"Failed above {_ref}"
for _ref in ("PDL", "IBL"):
    _LABELS[f"failedAuction_down_{_ref}"] = f"Failed below {_ref}"
for _grade in ("Strong", "Moderate", "Weak"):
    _LABELS[f"devBuyingTail_{_grade}"] = f"Early Buy Tail · {_grade}"
    _LABELS[f"devSellingTail_{_grade}"] = f"Early Sell Tail · {_grade}"

_PERIOD_SIGNALS = {"singlePrintUpSide", "singlePrintDownSide", "buyingTail", "sellingTail"}
_MARKER_RE = re.compile(r"\{([^}]*)\}\s*$")
_TPO_EXT_RE = re.compile(r"^(\d+)\s*Single TPO\s+(above|below)\s+Days\s+(High|Low)", re.I)
_PHRASES = (
    (r"^Inside Value Area", "In VA"),
    (r"^Above Value Area High", "Above VA"),
    (r"^Below Value Area Low", "Below VA"),
    (r"^Near Value Area High", "Near VA Hi"),
    (r"^Near Value Area Low", "Near VA Lo"),
    (r"^Near Day High", "Near Hi"),
    (r"^Near Day Low", "Near Lo"),
    (r"^Above Previous Day High", "Above PDH"),
    (r"^Below Previous Day Low", "Below PDL"),
    (r"^Within Yesterday Range", "In PDR"),
)
_EMPTY = {"", "undefined", "null", "NA", "-"}
_DEV_DOTS = {"Strong": "●●●", "Moderate": "●●○", "Weak": "●○○"}


def translate_signal(raw) -> str:
    """Port of the vendor's ``_translateSignal``."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if text in _EMPTY:
        return ""
    marker = _MARKER_RE.search(text)
    core = re.sub(r"\s*\{[^}]*\}\s*$", "", text).strip()
    if core in _LABELS:
        label = _LABELS[core]
        if marker and core in _PERIOD_SIGNALS:
            letters = re.sub(r'[\[\]"]', "", marker.group(1)).strip()
            if letters:
                label += f" [{letters}]"
        return label
    ext = _TPO_EXT_RE.match(core)
    if ext:
        arrow = "↑" if ext.group(2).lower() == "above" else "↓"
        return f"{ext.group(1)} TPO {arrow} {ext.group(3)}"
    for pattern, label in _PHRASES:
        if re.match(pattern, core, re.I):
            return label
    return core


def _pill(value) -> str:
    """Text of ``_pillBadge``: the label, then the raw key when it differs (hidden in the page,
    but part of the cell text the scraper reads)."""
    if value is None or str(value) in _EMPTY:
        return "—"
    raw = str(value)
    label = translate_signal(raw) or raw
    return f"{label}  {raw}" if label != raw else label


def _tail_cell(item: dict) -> str:
    pills = [_pill(tail) for tail in item.get("tails") or []]
    for dev in item.get("devTails") or []:
        buying = dev.get("side") == "buying"
        grade = dev.get("grade", "")
        raw = ("devBuyingTail_" if buying else "devSellingTail_") + grade
        label = f"Early {'Buy' if buying else 'Sell'} {_DEV_DOTS.get(grade, grade)} {dev.get('time') or ''}"
        pills.append(f"{label.strip()}  {raw}")
    return "  ".join(pills) if pills else _pill(item.get("tail"))


def _single_print_cell(item: dict) -> str:
    failed = item.get("failedAuctions") or []
    if not failed:
        return _pill(item.get("minusDevelopment"))
    pills = []
    for auction in failed:
        raw = f"failedAuction_{auction.get('side')}_{auction.get('ref')}"
        label = f"{_LABELS.get(raw, raw)} {auction.get('failTime') or ''}"
        pills.append(f"{label}  {raw}")
    single = item.get("minusDevelopment")
    if single and str(single) not in _EMPTY:
        pills.insert(0, _pill(single))
    return "  ".join(pills)


def _tpo_cell(value) -> str:
    if value is None or str(value) in ("", "undefined", "null"):
        return "—"
    raw = str(value)
    label = translate_signal(raw) or raw
    return f"{label}  {raw}" if label != raw else label


def build_market_profile_frame(rows: list, sectors: dict | None = None) -> pd.DataFrame:
    """Rows of the JSON reply as the raw table the page would have drawn."""
    records = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict) or "stockName" not in item:
            continue
        other = item.get("otherInfo") or {}
        ib = item.get("ibPercentageValue")
        records.append(
            {
                "#": str(index),
                "Name": item["stockName"],
                "Sector": (sectors or {}).get(item["stockName"]),
                "Opening": _pill(item.get("opening")),
                "IB %": "-" if ib is None else str(ib),
                "OpenDrive": _pill(item.get("openDrive")),
                "Tail": _tail_cell(item),
                "SinglePrint": _single_print_cell(item),
                "Poor H/L": _pill(item.get("poorHighLow")),
                "Latest Time": item.get("time"),
                "Day Type": _pill(item.get("dayType")),
                "Price vs today’s profile": _tpo_cell(other.get("currentTpoPos")),
                "TPO Pos (Prev)": _tpo_cell(other.get("currentTpoPosFromPreviousDay")),
                "Price": other.get("currentData"),
                "Change%": item.get("percentChange"),
            }
        )
    return pd.DataFrame.from_records(records)


#: Session letters in the order the page lists them (the 9:15-9:20 opening stub first).
VOLUME_SESSIONS = ("O", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M")


def build_volume_frame(rows: list, sectors: dict | None = None) -> pd.DataFrame:
    """Rows of the volume JSON as the raw table the page would have drawn.

    The vendor's page no longer shows delivery %, so "Del%" is left empty here on purpose: the
    column exists so the table keeps its shape, and the poller raises a critical alert when it
    sees the delivery figure missing. A session that has not traded yet is "-" (not zero).
    """
    records = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict) or "stockName" not in item:
            continue
        other = item.get("otherInfo") or {}
        surge = (other.get("IntradaySurge") or {}).get("factor")
        factors = other.get("VolumeFactor") or {}
        today = other.get("Day_Volume")
        average = other.get("Average_Volume")
        record = {
            "#": str(index),
            "Name": item["stockName"],
            "Sector": (sectors or {}).get(item["stockName"]),
            "Price": other.get("currentData"),
            "Chg%": item.get("percentChange"),
            "Surge x": None if surge is None else f"{surge}x",
            "Today (M)": None if today is None else abs(float(today)) / 1.0e6,
            "7D Avg (M)": None if average is None else abs(float(average)) / 1.0e6,
            "Del%": None,
            "Latest Time": item.get("time"),
        }
        for letter in VOLUME_SESSIONS:
            value = factors.get(letter)
            record[letter] = "-" if value is None else value
        records.append(record)
    return pd.DataFrame.from_records(records)
