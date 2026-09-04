"""Parse a breakingtrade.com "save page as Excel" snapshot into a clean DataFrame.

The site renders each scanner as an HTML table; saving the page to .xlsx carries the
visible label AND the element's hidden CSS class / tooltip text into the same cell,
concatenated with no separator, e.g. "Open Drive ↑ openDriveUpwards" or
"3 TPO ↑ High 3Single TPO above Days High {C}". Market Profile vocabulary is a closed
enumeration (day types, open types, tail, single print, TPO position all come from a fixed
taxonomy), so normalization here is keyword substring matching against that taxonomy rather
than a generic string-cleaning heuristic that could easily be fooled by one of those
run-on tooltip strings.

Two export kinds seen so far:
  - "market_profile": Opening / IB % / OpenDrive / Tail / SinglePrint / Poor H/L / Day Type /
    TPO Pos / TPO Pos (Prev) - the Market Profile scanner.
  - "volume": Today (M) / 7D Avg (M) / Surge x / Del% plus 13 half-hour volume buckets - the
    volume scanner.

A snapshot of an unrecognized kind raises SnapshotFormatError rather than guessing - silently
misreading a column here would corrupt every score downstream.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
import yaml

MARKET_PROFILE_COLUMNS = {
    "Opening",
    "IB %",
    "OpenDrive",
    "Tail",
    "SinglePrint",
    "Poor H/L",
    "Day Type",
    "TPO Pos",
    "TPO Pos (Prev)",
}
VOLUME_COLUMNS = {"Today (M)", "7D Avg (M)", "Surge x", "Del%"}


class SnapshotFormatError(ValueError):
    """Raised when a .xlsx file does not match a known breakingtrade.com export shape."""


@dataclass
class Snapshot:
    kind: str  # "market_profile" | "volume"
    captured_at: datetime | None
    frame: pd.DataFrame


_SIGNAL_COLUMNS = MARKET_PROFILE_COLUMNS | VOLUME_COLUMNS


def _find_header_row(raw: pd.DataFrame, max_scan_rows: int = 8) -> int:
    """Locate the header row.

    Two shapes are in play and both must work: a browser "save page as Excel" export puts a
    page title on row 0 and headers (starting with '#') on row 1, while a workbook generated
    from a DOM scrape carries title + source + blank rows and headers with no '#' column at
    all. So the test is "Name plus at least two known signal columns", not a fixed row or a
    required '#'.
    """
    for i in range(min(max_scan_rows, len(raw))):
        row_values = {str(v).strip() for v in raw.iloc[i].tolist()}
        if "Name" in row_values and len(row_values & _SIGNAL_COLUMNS) >= 2:
            return i
    raise SnapshotFormatError(
        f"Could not find a header row ('Name' plus known signal columns) in the first "
        f"{max_scan_rows} rows - is this a breakingtrade.com scanner export?"
    )


def _detect_kind(columns: set) -> str:
    if MARKET_PROFILE_COLUMNS <= columns:
        return "market_profile"
    if VOLUME_COLUMNS <= columns:
        return "volume"
    raise SnapshotFormatError(
        "Unrecognized column set - not a market_profile or volume breakingtrade.com export. "
        f"Got columns: {sorted(columns)}"
    )


def _strip_tag(text: str) -> str:
    """Drop a trailing '{B}' / '{C}' / '{["B"]}' style tag."""
    return re.sub(r"\s*\{[^}]*\}\s*$", "", text).strip()


def _direction(raw: str) -> str | None:
    if "↑" in raw:
        return "up"
    if "↓" in raw:
        return "down"
    return None


def _match_keyword(raw: str, keyword_to_code: list[tuple[str, str]]) -> str | None:
    """First matching keyword wins - order the table most-specific first."""
    for keyword, code in keyword_to_code:
        if keyword in raw:
            return code
    return None


_OPENING_KEYWORDS = [
    ("Above VAH", "above_prior_vah"),
    ("Below VAL", "below_prior_val"),
    ("Gap Up", "gap_up"),
    ("Gap Down", "gap_down"),
    ("In Value", "in_prior_value"),
]
_OPENDRIVE_KEYWORDS = [
    ("Open Drive", "open_drive"),
    ("Test Drive", "test_drive"),
    ("Rejection", "rejection"),
]
_TAIL_KEYWORDS = [("Buy Tail", "buy_tail"), ("Sell Tail", "sell_tail")]
_SINGLEPRINT_KEYWORDS = [
    ("Failed High", "failed_high"),
    ("Failed Low", "failed_low"),
    ("Single Print", "single_print"),
]
_POOR_HL_KEYWORDS = [("Poor High", "poor_high"), ("Poor Low", "poor_low")]
_TPO_POS_KEYWORDS = [
    ("Above VA", "above_va"),
    ("Below VA", "below_va"),
    ("Near VA Hi", "near_va_high"),
    ("Near VA Lo", "near_va_low"),
    ("Near Hi", "near_day_high"),
    ("Near Lo", "near_day_low"),
    ("In VA", "in_va"),
]
_TPO_EXTENSION_RE = re.compile(r"^(\d+)\s*TPO\s*([↑↓])")
_TPO_POS_PREV_KEYWORDS = [
    ("Above PDH", "above_pdh"),
    ("Below PDL", "below_pdl"),
    ("In PDR", "in_pdr"),
    # The scanner guide's "Value Migration" scan filters this column on Above VA, so the
    # column can evidently carry a Value-Area reading too - neither sample snapshot showed
    # one, but parse it rather than silently returning None if it ever appears.
    ("Above VA", "above_va"),
    ("Below VA", "below_va"),
    ("In VA", "in_va"),
]


def _normalize_cell(raw, keywords: list) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text in ("—", "-", ""):
        return None
    return _match_keyword(_strip_tag(text), keywords)


def _normalize_tpo_pos(raw) -> tuple:
    """Returns (code, tpo_count). tpo_count is set only for the extension codes."""
    if not isinstance(raw, str):
        return None, None
    text = _strip_tag(raw.strip())
    if text in ("—", "-", ""):
        return None, None
    ext_match = _TPO_EXTENSION_RE.match(text)
    if ext_match:
        count = int(ext_match.group(1))
        direction = "up" if ext_match.group(2) == "↑" else "down"
        return (f"tpo_ext_{'high' if direction == 'up' else 'low'}", count)
    return _match_keyword(text, _TPO_POS_KEYWORDS), None


def _normalize_day_type(raw) -> tuple:
    """Returns (base_label, direction). e.g. 'Normal Var ↓' -> ('Normal Var', 'down')."""
    if not isinstance(raw, str):
        return None, None
    text = _strip_tag(raw.strip())
    if not text:
        return None, None
    direction = _direction(text)
    base = text.replace("↑", "").replace("↓", "").strip()
    return base, direction


def parse_captured_at(frame: pd.DataFrame) -> datetime | None:
    if "Latest Time" not in frame.columns:
        return None
    values = frame["Latest Time"].dropna()
    if values.empty:
        return None
    # e.g. "2026-09-03 10:28 IST" - strip the tz label, timestamps in this export are IST.
    raw = str(values.iloc[0]).replace("IST", "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _split_corporate_action(name: str) -> tuple:
    """'CANBK  Fund Raise' -> ('CANBK', 'Fund Raise'). Tag is double-space separated -
    a single space is a legitimate multi-word name like 'BANKNIFTY FUT'."""
    parts = re.split(r"\s{2,}", name.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return parts[0].strip(), None


def _pick_column(frame: pd.DataFrame, *candidates: str) -> str:
    """First present column name among candidates - exports disagree on spacing
    ('Change%' in the browser save, 'Change %' in a DOM-scraped workbook)."""
    for name in candidates:
        if name in frame.columns:
            return name
    raise SnapshotFormatError(f"None of the expected columns {candidates} are present")


def _as_percent(series: pd.Series) -> pd.Series:
    """Normalize a change column to PERCENT.

    The browser export writes -1.75 for -1.75%; a DOM-scraped workbook writes -0.0175 for the
    same move. Scale is inferred from the data: a whole column inside +/-1 is a fraction, since
    a real universe-wide snapshot always contains at least one stock that moved more than 1%.
    """
    numeric = pd.to_numeric(series, errors="coerce")
    largest = numeric.abs().max()
    if pd.notna(largest) and largest <= 1.0:
        return numeric * 100.0
    return numeric


def _as_fraction(series: pd.Series) -> pd.Series:
    """Normalize a percentage column to a FRACTION (0-1).

    Del% arrives as 0.544 from the .xlsx export but as the string "54.4%" from the live grid,
    which plain to_numeric turns into NaN - silently emptying the column and, downstream,
    silently emptying the BTST list. The fraction is the canonical form because that is what
    DELIVERY_GENUINE (0.70) and the row-shape rules compare against.
    """
    cleaned = (
        series.replace({"—": None, "-": None})
        .astype(str)
        .str.replace("%", "", regex=False)
        .str.strip()
    )
    numeric = pd.to_numeric(cleaned, errors="coerce")
    largest = numeric.abs().max()
    # A real universe always contains a stock above 1% delivery, so a column whose maximum
    # exceeds 1.0 is expressed in percent rather than as a fraction.
    if pd.notna(largest) and largest > 1.0:
        return numeric / 100.0
    return numeric


def _normalize_market_profile(frame: pd.DataFrame) -> pd.DataFrame:
    change_col = _pick_column(frame, "Change%", "Change %")
    out = frame[["Name", "Sector", "Price", change_col]].copy()
    out = out.rename(columns={change_col: "Change%"})
    split = out["Name"].astype(str).apply(_split_corporate_action)
    out["symbol"] = split.apply(lambda t: t[0]).str.upper()
    out["corporate_action"] = split.apply(lambda t: t[1])
    out["ib_pct"] = pd.to_numeric(frame["IB %"], errors="coerce")

    out["opening"] = frame["Opening"].apply(lambda v: _normalize_cell(v, _OPENING_KEYWORDS))

    out["open_type"] = frame["OpenDrive"].apply(lambda v: _normalize_cell(v, _OPENDRIVE_KEYWORDS))
    out["open_type_dir"] = frame["OpenDrive"].apply(
        lambda v: _direction(v) if isinstance(v, str) else None
    )

    out["tail"] = frame["Tail"].apply(lambda v: _normalize_cell(v, _TAIL_KEYWORDS))

    out["single_print"] = frame["SinglePrint"].apply(
        lambda v: _normalize_cell(v, _SINGLEPRINT_KEYWORDS)
    )
    out["single_print_dir"] = frame["SinglePrint"].apply(
        lambda v: _direction(v) if isinstance(v, str) else None
    )

    out["poor_hl"] = frame["Poor H/L"].apply(lambda v: _normalize_cell(v, _POOR_HL_KEYWORDS))

    day_type_parsed = frame["Day Type"].apply(_normalize_day_type)
    out["day_type"] = day_type_parsed.apply(lambda t: t[0])
    out["day_type_dir"] = day_type_parsed.apply(lambda t: t[1])

    tpo_parsed = frame["TPO Pos"].apply(_normalize_tpo_pos)
    out["tpo_pos"] = tpo_parsed.apply(lambda t: t[0])
    out["tpo_pos_count"] = tpo_parsed.apply(lambda t: t[1])

    out["tpo_pos_prev"] = frame["TPO Pos (Prev)"].apply(
        lambda v: _normalize_cell(v, _TPO_POS_PREV_KEYWORDS)
    )

    out = out.rename(columns={"Sector": "sector", "Change%": "change_pct", "Price": "price"}).drop(
        columns=["Name"]
    )
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out["change_pct"] = _as_percent(out["change_pct"])
    return out


# The volume export carries one column per half-hour session, headed like "9:45-10:15B" or
# "3:15-30M · close". The trailing letter is the TPO period, and that letter is what the
# vendor's row-shape patterns are described in terms of ("green in G/H/I", "building into
# K/L/M"), so the letter - not the time text, which varies in format - is the key we keep.
_VOLUME_BUCKET_RE = re.compile(r"([A-O])(?:\s*[·-].*)?$")


def _bucket_letter(column: str):
    match = _VOLUME_BUCKET_RE.search(str(column).strip())
    return match.group(1) if match else None


def _normalize_volume(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame[["Name", "Sector", "Price", "Chg%"]].copy()
    split = out["Name"].astype(str).apply(_split_corporate_action)
    out["symbol"] = split.apply(lambda t: t[0]).str.upper()
    out["corporate_action"] = split.apply(lambda t: t[1])
    out["today_volume_m"] = pd.to_numeric(frame["Today (M)"], errors="coerce")
    out["avg_7d_volume_m"] = pd.to_numeric(frame["7D Avg (M)"], errors="coerce")
    out["surge_x"] = pd.to_numeric(
        frame["Surge x"].astype(str).str.replace("x", "", regex=False), errors="coerce"
    )
    out["delivery_pct"] = _as_fraction(frame["Del%"])

    # Per-session volume factors, kept as vol_a ... vol_m (plus vol_o, the 9:15-9:20 opening
    # stub). A session yet to happen reads "-" and becomes NaN, which is what distinguishes
    # "not traded yet" from a genuine zero and stops a half-finished day looking like a dead
    # one to the row-shape detectors.
    for column in frame.columns:
        letter = _bucket_letter(column)
        if letter:
            out[f"vol_{letter.lower()}"] = pd.to_numeric(
                frame[column].replace({"—": None, "-": None}), errors="coerce"
            )

    out = out.drop(columns=["Name"]).rename(
        columns={"Sector": "sector", "Price": "price", "Chg%": "change_pct"}
    )
    out["change_pct"] = _as_percent(out["change_pct"])
    return out


_SIGNAL_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SECTORS_YAML_PATH = os.path.join(_SIGNAL_ENGINE_DIR, "sectors.yaml")


def load_sector_map() -> dict:
    """symbol -> sector, from signal_engine/sectors.yaml.

    breakingtrade.com's own Sector column is empty in both sample exports (probably a
    premium/JS-rendered field that a static page save doesn't capture), so this reuses the
    same sector map the RiskEngine's per-sector position cap already relies on, without
    pulling in signal_engine.config (which eagerly loads config.yaml + .env secrets at
    import time - too heavy a dependency for a read-only analysis CLI).
    """
    if not os.path.exists(_SECTORS_YAML_PATH):
        return {}
    with open(_SECTORS_YAML_PATH) as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        return {}
    return {symbol.upper(): sector for sector, symbols in data.items() for symbol in symbols}


def enrich_sector(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill frame['sector'] from sectors.yaml wherever the scanner left it blank."""
    sector_map = load_sector_map()
    if not sector_map:
        return frame
    frame = frame.copy()
    frame["sector"] = frame["sector"].fillna(frame["symbol"].map(sector_map))
    return frame


DEFAULT_EXCEL_DIR = os.path.join(
    _SIGNAL_ENGINE_DIR, "pinescripts", "intraday", "breaking-trade", "excel"
)


def discover_latest(excel_dir: str = DEFAULT_EXCEL_DIR) -> dict:
    """Scan excel_dir for .xlsx exports and return the freshest market_profile / volume
    snapshot found, keyed by kind. "Freshest" is by the timestamp embedded IN the file
    (captured_at, parsed from its own 'Latest Time' cell) rather than filesystem mtime -
    mtime is unreliable (a git checkout, a copy, a sync tool all reset it), while the
    embedded timestamp is the actual moment breakingtrade.com rendered the page.

    Files that don't parse as a known export are skipped, not raised - discovery scans
    whatever happens to be in the folder, so one stray unrelated .xlsx must not break it.
    """
    candidates: dict = {}
    for name in sorted(os.listdir(excel_dir)):
        if not name.lower().endswith(".xlsx") or name.startswith("~$"):
            continue
        path = os.path.join(excel_dir, name)
        try:
            snapshot = load_snapshot(path)
        except SnapshotFormatError:
            continue

        current_best = candidates.get(snapshot.kind)
        current_ts = current_best.captured_at if current_best else None
        new_ts = snapshot.captured_at
        # None captured_at sorts last - an unparseable timestamp is not "freshest".
        if current_best is None or (
            new_ts is not None and (current_ts is None or new_ts > current_ts)
        ):
            candidates[snapshot.kind] = snapshot

    return candidates


_TIMESTAMP_IN_TEXT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T]+(\d{1,2}):(\d{2})")
_TIMESTAMP_IN_FILENAME_RE = re.compile(r"(\d{8})[_-](\d{4})")


def _captured_at_fallback(raw: pd.DataFrame, path: str):
    """For exports with no 'Latest Time' column: read the timestamp off the sheet's own title
    rows ('Snapshot: 2026-09-03 12:31 IST'), then off the filename (..._20260903_1231IST)."""
    header_text = " ".join(str(v) for v in raw.head(4).to_numpy().ravel() if pd.notna(v))
    match = _TIMESTAMP_IN_TEXT_RE.search(header_text)
    if match:
        try:
            return datetime.strptime(
                f"{match.group(1)} {int(match.group(2)):02d}:{match.group(3)}", "%Y-%m-%d %H:%M"
            )
        except ValueError:
            pass

    match = _TIMESTAMP_IN_FILENAME_RE.search(os.path.basename(path))
    if match:
        try:
            return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M")
        except ValueError:
            pass
    return None


def normalize_table(frame: pd.DataFrame, captured_at=None) -> Snapshot:
    """Normalize an already-extracted scanner table (the raw vendor column names) into a
    Snapshot. Shared by the .xlsx readers and by the live fetcher, so a scraped table and a
    saved export go through exactly one normalization path and cannot drift apart.
    """
    kind = _detect_kind(set(frame.columns))
    normalized = (
        _normalize_market_profile(frame) if kind == "market_profile" else _normalize_volume(frame)
    )
    return Snapshot(kind=kind, captured_at=captured_at, frame=enrich_sector(normalized))


def load_snapshot(path: str) -> Snapshot:
    """Read one breakingtrade.com .xlsx export and return a normalized Snapshot.

    Every sheet is tried, not just the first: a workbook generated from a DOM scrape leads
    with a summary tab and carries the raw scanner table on a later one.
    """
    workbook = pd.ExcelFile(path)
    errors = []

    for sheet_name in workbook.sheet_names:
        raw = workbook.parse(sheet_name, header=None)
        try:
            header_row = _find_header_row(raw)
            frame = workbook.parse(sheet_name, header=header_row).dropna(how="all")
            kind = _detect_kind(set(frame.columns))
        except SnapshotFormatError as exc:
            errors.append(f"{sheet_name}: {exc}")
            continue

        captured_at = parse_captured_at(frame) or _captured_at_fallback(raw, path)
        if kind == "market_profile":
            normalized = _normalize_market_profile(frame)
        else:
            normalized = _normalize_volume(frame)

        return Snapshot(kind=kind, captured_at=captured_at, frame=enrich_sector(normalized))

    raise SnapshotFormatError(f"No sheet in {os.path.basename(path)} parsed: {'; '.join(errors)}")
