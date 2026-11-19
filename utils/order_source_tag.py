"""
Compact order-source tag carried in a broker's free-text order remark (broker-agnostic).

Purpose: a live automated (strategy) order placed through OpenAlgo carries, AT the broker call,
a short tag that names its source and strategy, so the discretionary ledger can identify the
fill's origin from broker data alone (no post-order write, no timer).

Format v1, 12 characters, only [A-Z0-9] (the broker's remark length and charset limits are
undocumented, so the tag is far below the 20-character design ceiling and uses no separator,
space or symbol that a broker might strip, escape or reject):

    OA 1 L 9F3A2C1B
    |  | | +-------- strategy code: first 8 hex chars (upper case) of sha256(strategy name)
    |  | +---------- mode: L = live (paper/analyze orders never reach a broker, so only L is sent)
    |  +------------ format version
    +--------------- OpenAlgo marker

Why a hash code and not a truncated name: a name can exceed any limit, collide on a shared
prefix ("Momentum Long" / "Momentum Long v2"), and contain spaces, '&' or non-ASCII characters
a broker may reject or rewrite; the hash is fixed length, charset-safe and deterministic. The
reverse mapping (code -> name) is a lookup over known strategy names (the registered live order
tags); an unknown code still proves an OpenAlgo strategy order, only its name is unknown.

Settings (read at call time, default OFF): DISCRETIONARY_ORDER_TAG_ENABLED.
DISCRETIONARY_MANUAL_ORDERSOURCES (default "WEB,MOB"): the broker order-source values that are a
human-initiated channel (see manual_ordersources). Nothing here performs I/O or raises to the
caller of build_order_tag / parse_remarks / manual_ordersources / system_marker.
"""

import hashlib
import os
import re

from utils.logging import get_logger

logger = get_logger(__name__)

TAG_MARKER = "OA"
TAG_VERSION = "1"
MODE_CODES = {"live": "L"}
TAG_MAX_LEN = 20  # design ceiling; the v1 tag is 12
CODE_LEN = 8

# Key under which place_order_service passes the tag to a broker module that declares
# ACCEPTS_ORDER_SOURCE_TAG = True. Other brokers never receive it.
ORDER_TAG_KEY = "order_source_tag"

# Strategy labels that are not a strategy identity: OpenAlgo UI actions and the close-all
# square-off. Their orders are never tagged (a UI action is discretionary).
NON_STRATEGY_LABELS = frozenset({"UI Exit Position", "Squareoff"})

_TRUE = ("1", "true", "yes", "on")
_V1 = re.compile(r"^OA1([A-Z])([0-9A-F]{8})$")
# OpenAlgo marker at the start, any case ("oa1l..." is a damaged tag, not a user note)
_OURS = re.compile(r"^OA(\d)", re.IGNORECASE)
# a v1-shaped tag anywhere inside longer text (a broker note quoting it, a prefix/suffix)
_EMBEDDED = re.compile(r"OA1[A-Z][0-9A-F]{8}", re.IGNORECASE)
_REMARK_SCAN_MAX = 512  # longer remarks are scanned only up to here (bounded regex work)

KIND_EMPTY = "empty"  # no remark at all
KIND_TAG = "tag"  # a valid v1 OpenAlgo tag
KIND_OTHER_VERSION = "other_version"  # OpenAlgo marker with another version
KIND_MALFORMED = "malformed"  # OpenAlgo v1 marker but not a valid v1 tag
KIND_FOREIGN = "foreign"  # some other text (a broker note, a user remark)


def flag(name: str) -> bool:
    """Boolean env setting, read at call time; anything but 1/true/yes/on is False."""
    return (os.getenv(name, "") or "").strip().lower() in _TRUE


def tag_enabled() -> bool:
    return flag("DISCRETIONARY_ORDER_TAG_ENABLED")


def strategy_code(strategy: str) -> str:
    """8 upper-case hex chars of sha256 over the stripped strategy name (UTF-8)."""
    digest = hashlib.sha256((strategy or "").strip().encode("utf-8")).hexdigest()
    return digest[:CODE_LEN].upper()


def build_order_tag(strategy, mode: str = "live") -> str | None:
    """The v1 tag for ``strategy`` in ``mode``; None when it must not be tagged.

    None for a missing/blank strategy, a NON_STRATEGY_LABELS label or a mode other than live.
    Never raises.
    """
    try:
        name = strategy.strip() if isinstance(strategy, str) else ""
        if not name or name in NON_STRATEGY_LABELS:
            return None
        mode_code = MODE_CODES.get((mode or "").strip().lower())
        if mode_code is None:
            return None
        tag = f"{TAG_MARKER}{TAG_VERSION}{mode_code}{strategy_code(name)}"
        return tag if len(tag) <= TAG_MAX_LEN else None
    except Exception as e:  # never let tagging affect an order
        logger.warning("Order source tag not built: %s", type(e).__name__)
        return None


def parse_remarks(value) -> dict:
    """Classify a broker remark. Returns {"kind", "mode", "code"} (mode/code None unless a tag).

    Only a full v1 match is a tag; an OpenAlgo marker with another version or a broken body is
    reported as such (never as foreign), any other text is foreign. Never raises.
    """
    out = {"kind": KIND_EMPTY, "mode": None, "code": None}
    try:
        if not isinstance(value, str) or not value.strip():
            return out
        text = value.strip()[:_REMARK_SCAN_MAX]
        m = _V1.match(text)
        if m:
            out.update(kind=KIND_TAG, mode=m.group(1), code=m.group(2))
            return out
        ours = _OURS.match(text)
        if ours:
            out["kind"] = KIND_MALFORMED if ours.group(1) == TAG_VERSION else KIND_OTHER_VERSION
            return out
        # a tag inside other text is never trusted as a tag, nor read as a plain note
        out["kind"] = KIND_MALFORMED if _EMBEDDED.search(text) else KIND_FOREIGN
    except Exception as e:  # never let a strange remark break a sync
        logger.warning("Remarks not parsed: %s", type(e).__name__)
        out = {"kind": KIND_MALFORMED, "mode": None, "code": None}
    return out


# ------------------------------------------------------------- human channels

# Broker order-source values that mean "entered by a person". Documented (Noren / Flattrade
# PlaceOrder): "MOB / WEB / TT"; observed on manual Flattrade orders: WEB. TT is documented but
# its meaning is not, so it is NOT in the default; add it via the setting once confirmed.
DEFAULT_MANUAL_ORDERSOURCES = ("WEB", "MOB")
_SOURCE_TOKEN = re.compile(r"^[A-Z0-9_-]{1,20}$")


def manual_ordersources() -> frozenset:
    """Upper-case allow-list from DISCRETIONARY_MANUAL_ORDERSOURCES (comma separated, read at
    call time). Unset or blank -> the default {WEB, MOB}. Tokens are stripped and upper-cased;
    a token with other characters is ignored; "API" is never a human channel and is dropped.
    Never raises (any problem -> the default)."""
    try:
        raw = (os.getenv("DISCRETIONARY_MANUAL_ORDERSOURCES", "") or "").strip()
        if not raw:
            return frozenset(DEFAULT_MANUAL_ORDERSOURCES)
        tokens = {t.strip().upper() for t in raw.split(",")}
        return frozenset(t for t in tokens if _SOURCE_TOKEN.match(t) and t != "API")
    except Exception:
        return frozenset(DEFAULT_MANUAL_ORDERSOURCES)


def is_human_ordersource(value) -> bool:
    """True when ``value`` (case-insensitive) is an allow-listed human channel."""
    if not isinstance(value, str) or not value.strip():
        return False
    return value.strip().upper() in manual_ordersources()


# ------------------------------------------------------------- system markers

# Remark patterns of a broker / RMS / platform generated order (case-insensitive). The single
# list of record; kept conservative: each class names an ACTION by the system (square-off,
# liquidation, a triggered GTT/OCO, a bracket/cover leg), not just a word the broker writes on
# any order. A bare "RMS" is NOT a marker: the RMS also annotates human orders (for example a
# note that it modified the price of a market order), so RMS counts only next to an exit word.
_EXIT_WORD = r"(?:sq(?:uare)?[\s._-]*off|sqoff|exit|close|closed|cut|liquidat\w*)"
SYSTEM_REMARK_PATTERNS = (
    ("square_off", re.compile(r"\bsq(?:uare)?[\s._-]*off\b|\bsqoff\b", re.IGNORECASE)),
    ("auto", re.compile(r"\bauto[\s._-]*" + _EXIT_WORD, re.IGNORECASE)),
    (
        "rms_action",
        re.compile(
            r"\bRMS\b[^\n]{0,30}?\b" + _EXIT_WORD + r"|\b" + _EXIT_WORD + r"[^\n]{0,30}?\bRMS\b",
            re.IGNORECASE,
        ),
    ),
    ("mtm", re.compile(r"\bMTM\b", re.IGNORECASE)),
    ("liquidation", re.compile(r"\bliquidat\w*|\bforced?[\s._-]*(?:close|exit)", re.IGNORECASE)),
    (
        "expiry",
        re.compile(
            r"\b(?:expiry|physical)[\s._-]*(?:settlement|delivery|close|exit)", re.IGNORECASE
        ),
    ),
    ("gtt", re.compile(r"\bGTT\b|\bOCO\b", re.IGNORECASE)),
    ("bracket_cover", re.compile(r"\bbracket\b|\bcover[\s._-]*order\b", re.IGNORECASE)),
)


def _field(row, key) -> str:
    value = row.get(key) if isinstance(row, dict) else None
    return value.strip() if isinstance(value, str) else ""


def system_marker(row) -> str | None:
    """Class name of a broker/RMS/system marker on an OrderBook (or TradeBook) row, else None.

    Remark text against SYSTEM_REMARK_PATTERNS, then documented Noren fields: ``snonum``
    (child leg of a cover/bracket order), ``algo_name`` (a broker-side algo), and ``src_uid``
    ("Source User Id") naming a user other than the row's own uid/actid (an order entered by
    someone else, e.g. a dealer or the RMS). A remark that is our own tag is never a marker.
    Only the class name leaves this function (never the raw text). Never raises.
    """
    try:
        if not isinstance(row, dict):
            return None
        remarks = _field(row, "remarks")[:_REMARK_SCAN_MAX]
        if remarks and parse_remarks(remarks)["kind"] != KIND_TAG:
            for name, pattern in SYSTEM_REMARK_PATTERNS:
                if pattern.search(remarks):
                    return name
        if _field(row, "snonum"):
            return "bracket_cover_child"
        if _field(row, "algo_name"):
            return "algo"
        src = _field(row, "src_uid").upper()
        own = {_field(row, "uid").upper(), _field(row, "actid").upper()} - {""}
        if src and own and src not in own:
            return "other_user"
    except Exception as e:
        logger.warning("System marker not evaluated: %s", type(e).__name__)
        return "unreadable"
    return None
