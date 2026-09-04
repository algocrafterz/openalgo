"""Headless-browser fetch of the breakingtrade.com scanner tables.

WHY A BROWSER AND NOT httpx
breakingtrade.com serves the scanners from a hash-routed SPA (Home.jsp#/intradaySignal,
Home.jsp#/volumeScanner) behind a login, with the table built client-side - an unauthenticated
GET of Home.jsp returns a ~900-character shell containing none of the scanner vocabulary. There
is no public API or webhook (see ../../pinescripts/intraday/breaking-trade/README.md), so a
headless browser rendering the page as the subscriber's own session is the way in.

TABLE LOCATION IS BY CONTENT, NOT BY SELECTOR
Nothing here depends on a CSS class or element id from the vendor's markup, because those are
undocumented and change without notice. The scanner table is found by looking for the table
whose header row carries the known signal columns - the same content-based identification the
.xlsx reader uses to find its header row. A restyle does not break this; only renaming the
columns themselves would, and that would break the .xlsx path too, where the tests would catch
it.

SESSION
A persistent browser profile keeps the login across runs, so a normal poll performs no login at
all. When the session has lapsed and credentials are present in signal_engine/.env
(BREAKINGTRADE_EMAIL / BREAKINGTRADE_PASSWORD), a generic form login is attempted; otherwise
LoginRequired is raised rather than silently returning an empty table.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta

import pandas as pd
from dotenv import dotenv_values

from signal_engine.analysis.breakingtrade.extractor import (
    MARKET_PROFILE_COLUMNS,
    VOLUME_COLUMNS,
    Snapshot,
    normalize_table,
    parse_captured_at,
)

_DIR = os.path.dirname(os.path.abspath(__file__))
_SIGNAL_ENGINE_DIR = os.path.dirname(os.path.dirname(_DIR))
_ENV_PATH = os.path.join(_SIGNAL_ENGINE_DIR, ".env")
PROFILE_DIR = os.path.join(_SIGNAL_ENGINE_DIR, "data", "breakingtrade_profile")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

BASE_URL = "https://breakingtrade.com/Home.jsp"
SCANNER_ROUTES = {
    "market_profile": "#/intradaySignal",
    "volume": "#/volumeScanner",
}

# A scanner table is identified by how many known signal columns its header row carries.
_MIN_SIGNAL_COLUMNS = 4
_ALL_SIGNAL_COLUMNS = MARKET_PROFILE_COLUMNS | VOLUME_COLUMNS


class FetchError(RuntimeError):
    """Raised when the scanner table could not be read from the rendered page."""


class LoginRequired(FetchError):
    """Raised when the session has lapsed and no usable credentials are configured."""


# The scanner is a DataTables grid that paginates to 20 of ~220 rows. Rather than clicking
# through pages, ask DataTables itself for every row - `tables({api: true})` is its documented
# way to reach every instance on the page, so this survives a change of table id or CSS.
_EXPAND_PAGINATION_JS = """
() => {
  if (!(window.jQuery && jQuery.fn && jQuery.fn.dataTable)) return false;
  jQuery.fn.dataTable.tables({api: true}).page.len(-1).draw();
  return true;
}
"""

# Extracts every table as {head: [...], body: [[...], ...]}. One JS round trip rather than many
# Playwright calls - 220 rows x 16 columns would otherwise be hopelessly chatty.
#
# textContent, NOT innerText. innerText returns the RENDERED text, and these headers carry a
# CSS text-transform, so innerText yields "OPENDRIVE" where the real column name is
# "OpenDrive" - which silently defeats matching against the known column names. textContent
# reads the source text, and has the side benefit of working on a hidden panel too. It also
# picks up each cell's hidden tooltip text, producing the same run-on strings
# ("Above VAH aboveYesterdayVAH") the .xlsx export has, which the extractor already expects.
#
# Cell text joins the cell's DIRECT CHILD NODES with a double space rather than flattening the
# whole cell. A ticker with a corporate-action badge ("CANBK" + <span>Fund Raise</span>) then
# comes out as "CANBK  Fund Raise" - the exact double-space convention the .xlsx export uses,
# which is what lets the extractor separate the badge from the symbol. Flattening it to a
# single space instead produces the symbol "CANBK FUND RAISE", which matches nothing.
_EXTRACT_TABLES_JS = """
() => {
  const text = el => {
    const parts = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const t = (walker.currentNode.textContent || '').replace(/\\s+/g, ' ').trim();
      if (t) parts.push(t);
    }
    return parts.join('  ');
  };
  return [...document.querySelectorAll('table')].map(t => {
    const headCells = [...t.querySelectorAll('thead th, thead td')];
    const bodyRows = [...t.querySelectorAll('tbody tr')];
    const head = headCells.length
      ? headCells.map(text)
      : (t.rows.length ? [...t.rows[0].cells].map(text) : []);
    const body = bodyRows.length
      ? bodyRows.map(r => [...r.cells].map(text))
      : [...t.rows].slice(1).map(r => [...r.cells].map(text));
    return {head, body};
  });
}
"""


_DATE_LABEL_FORMATS = (
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%b %d, %Y",
    "%d %b",  # year omitted by the vendor - filled in below
    "%d %B",
)


def parse_date_label(label: str) -> datetime | None:
    """Read the scanner's own date label into a datetime stamped at the session close.

    A historical snapshot is a completed day, so it is stamped 15:30 rather than midnight -
    that is when the data it shows was true.
    """
    if not label:
        return None
    text = re.sub(r"\s+", " ", label).replace("Live", "").strip(" ,")
    if not text:
        return None

    for fmt in _DATE_LABEL_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:  # vendor omitted the year - assume the most recent such date
            today = datetime.today()
            parsed = parsed.replace(year=today.year)
            if parsed.date() > today.date():
                parsed = parsed.replace(year=today.year - 1)
        return parsed.replace(hour=15, minute=30)
    return None


def session_stamp(now: datetime) -> datetime:
    """The moment the fetched data actually belongs to.

    Inside market hours that is simply now. Outside them the scanner is still showing the last
    COMPLETED session, so stamping it "now" would both misdate it and, worse, make a 02:00
    fetch look like a newer poll than the real 15:20 one - which would corrupt the
    transition diff. Out of hours the snapshot is therefore stamped at the close it belongs to.
    """
    now = now.replace(second=0, microsecond=0)
    close = now.replace(hour=15, minute=30)
    if MARKET_OPEN <= now.time() <= MARKET_CLOSE:
        return now
    if now.time() > MARKET_CLOSE:
        return close
    # Before the open: the data is the previous calendar day's close. Weekends and holidays
    # are not resolved here - the stamp is only ever a label for an out-of-hours fetch.
    return close - timedelta(days=1)


def _credentials() -> tuple:
    env = dotenv_values(_ENV_PATH) if os.path.exists(_ENV_PATH) else {}
    return (
        env.get("BREAKINGTRADE_EMAIL") or os.getenv("BREAKINGTRADE_EMAIL"),
        env.get("BREAKINGTRADE_PASSWORD") or os.getenv("BREAKINGTRADE_PASSWORD"),
    )


# Known columns keyed by lowercase, so header casing cannot defeat matching, and so the frame
# handed on always uses the canonical spelling the extractor's normalizers expect.
_CANONICAL_BY_LOWER = {name.lower(): name for name in _ALL_SIGNAL_COLUMNS}


def _canonical_head(head: list) -> list:
    return [_CANONICAL_BY_LOWER.get(str(h).strip().lower(), str(h).strip()) for h in head]


def _pick_scanner_table(tables: list) -> pd.DataFrame:
    """Choose the table whose header carries the most known signal columns."""
    best = None
    best_hits = 0
    for table in tables:
        hits = len(set(_canonical_head(table.get("head", []))) & _ALL_SIGNAL_COLUMNS)
        if hits > best_hits and table.get("body"):
            best, best_hits = table, hits

    if best is None or best_hits < _MIN_SIGNAL_COLUMNS:
        raise FetchError(
            f"No table on the page carried at least {_MIN_SIGNAL_COLUMNS} known scanner "
            f"columns (best was {best_hits}). The page may not have finished loading, the "
            "session may have lapsed, or the vendor may have renamed columns."
        )

    head = _canonical_head(best["head"])
    body = [row for row in best["body"] if len(row) == len(head)]
    if not body:
        # DataTables renders "No matching records found" as ONE cell spanning every column, so
        # an empty grid and a genuinely malformed one look identical to a width check. Telling
        # them apart matters: the first means the page lost its data (stale render, lapsed
        # session), the second means the vendor changed the table.
        widths = sorted({len(row) for row in best["body"]})
        if widths in ([1], []):
            raise FetchError(
                f"The scanner grid rendered no data rows (header has {len(head)} columns). "
                "The page is stale or the session lapsed."
            )
        raise FetchError(
            f"Found the scanner table ({best_hits} known columns) but no row matched its "
            f"{len(head)} columns - row widths seen: {widths}."
        )
    return pd.DataFrame(body, columns=head)


LOGIN_URL = "https://breakingtrade.com/login?action=login"

# Anonymous visitors get a usable but reduced view of the scanners - the tables render, so a
# "did the table load?" check says nothing about whether the subscription is in use. The
# reliable tell is the sign-in control still being on the page.
_SIGNED_OUT_LABELS = ("Sign In →", "Sign In", "Sign in", "Login")


def _is_signed_out(page) -> bool:
    return bool(
        page.evaluate(
            """(labels) => {
              const visible = e => !!(e.offsetParent || e.getClientRects().length);
              return [...document.querySelectorAll('button, a')]
                .filter(visible)
                .some(e => labels.includes((e.textContent || '').replace(/\\s+/g, ' ').trim()));
            }""",
            list(_SIGNED_OUT_LABELS),
        )
    )


def _attempt_login(page, email: str, password: str) -> None:
    """Sign in on the vendor's login page.

    The email field is addressed by type, never as "the first text input": the login page also
    carries an AI demo box, which is the first text input on it, so a generic selector types
    the address into the wrong field and silently fails to log in.

    One attempt only, deliberately - retrying a rejected password risks locking the account.
    """
    page.goto(LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    page.locator("#modalEmail, input[type=email]").first.fill(email, timeout=15000)
    page.locator("#modalPassword, input[type=password]").first.fill(password, timeout=15000)

    submit = page.get_by_role("button", name="Log In", exact=True)
    if submit.count():
        submit.first.click(timeout=15000)
    else:
        page.keyboard.press("Enter")
    page.wait_for_timeout(6000)


def _dump_debug(page, scanner: str, debug_dir: str) -> str:
    os.makedirs(debug_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = os.path.join(debug_dir, f"{scanner}_{stamp}.html")
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(page.content())
    page.screenshot(path=os.path.join(debug_dir, f"{scanner}_{stamp}.png"), full_page=True)
    return html_path


class ScannerSession:
    """One browser, held open across many polls.

    Worth the extra machinery for two reasons. A cold `launch_persistent_context` costs on the
    order of 15 seconds, and a polling day makes ~54 scanner reads - about a quarter of an hour
    spent starting browsers. More importantly the vendor's login does NOT survive a browser
    restart (it is a session cookie, not a persisted one), so a fresh launch per fetch means a
    fresh LOGIN per fetch: ~54 sign-ins a day against the account, which is both wasteful and
    the kind of pattern that gets an account flagged.

    Used as a context manager:

        with ScannerSession() as session:
            market_profile = session.fetch("market_profile")
            volume = session.fetch("volume")
    """

    def __init__(self, headless: bool = True, settle_ms: int = 8000, debug_dir: str = None):
        self.headless = headless
        self.settle_ms = settle_ms
        self.debug_dir = debug_dir
        self._playwright = None
        self._context = None
        self._page = None

    def __enter__(self):
        # Imported here, not at module import time, so the rest of the package (and its tests)
        # stay usable on a machine where playwright's browser binary was never installed.
        from playwright.sync_api import sync_playwright

        os.makedirs(PROFILE_DIR, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            PROFILE_DIR, headless=self.headless
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self

    def __exit__(self, *_exc):
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._context = self._page = self._playwright = None

    def _ensure_signed_in(self, url: str) -> None:
        if not _is_signed_out(self._page):
            return

        email, password = _credentials()
        if not email or not password:
            raise LoginRequired(
                "Not signed in to breakingtrade.com and no credentials are set. The scanners "
                "still render for anonymous visitors, so this would otherwise silently scrape "
                "the reduced free view. Add BREAKINGTRADE_EMAIL and BREAKINGTRADE_PASSWORD to "
                f"signal_engine/.env, or run once with headless=False to sign in by hand into "
                f"{PROFILE_DIR}."
            )

        _attempt_login(self._page, email, password)
        self._page.goto(url, timeout=60000, wait_until="domcontentloaded")
        self._page.wait_for_timeout(self.settle_ms)
        if _is_signed_out(self._page):
            raise LoginRequired(
                "Sign-in did not take - the scanner page still shows a login control. Check "
                "BREAKINGTRADE_EMAIL / BREAKINGTRADE_PASSWORD, or whether the account needs "
                "an OTP or captcha (run with headless=False to watch it happen)."
            )

    def _date_display(self) -> str:
        """Whatever the scanner's own date label currently reads - "Live", or a past date."""
        return self._page.evaluate(
            """() => {
              const el = document.querySelector('.nav-controls .date-display');
              return el ? (el.textContent || '').replace(/\\s+/g, ' ').trim() : '';
            }"""
        )

    def _step_back(self, days: int) -> str:
        """Click the scanner's own "previous trading day" arrow `days` times.

        Both scanners carry a `.nav-controls` block titled "Browse previous trading days" - a
        prev button, a date label, a next button. Addressed by class rather than by id because
        the two scanners use different ids for the same control.

        Each click reloads the grid over AJAX, so this waits for the DATE LABEL to actually
        change before clicking again. Clicking blind would race the reload and silently land
        on the wrong day - the worst possible failure here, since the data would look
        perfectly valid while being stamped with someone else's date.
        """
        for _ in range(days):
            before = self._date_display()
            button = self._page.locator(".nav-controls button.nav-btn.prev").first
            if not button.count():
                raise FetchError(
                    "No 'previous trading day' control on this page - historical snapshots may "
                    "need a subscription, or the vendor has changed the control."
                )
            button.click(timeout=15000)

            for _ in range(30):  # up to ~15s for the AJAX redraw
                self._page.wait_for_timeout(500)
                if self._date_display() != before:
                    break
            else:
                raise FetchError(
                    f"The date label stayed on {before!r} after clicking back - the historical "
                    "view did not load."
                )
        return self._date_display()

    def iter_history(self, scanner: str, days: int):
        """Yield one Snapshot per completed session, walking backwards from Live.

        Deliberately a generator over a SINGLE walk. Calling fetch(days_back=k) for k in
        1..N re-navigates and re-clicks from Live every time, which is O(N^2) clicks - 60
        sessions would be 1,830 clicks per scanner instead of 60, turning minutes into hours.
        Sample size is the binding constraint on answering whether any of this has an edge,
        so deep history has to be cheap.
        """
        url = BASE_URL + SCANNER_ROUTES[scanner]
        self._page.goto(url, timeout=60000, wait_until="domcontentloaded")
        self._page.wait_for_timeout(self.settle_ms)
        self._ensure_signed_in(url)

        for _ in range(days):
            label = self._step_back(1)
            captured_at = parse_date_label(label)
            if captured_at is None:
                raise FetchError(f"Could not read a date out of the label {label!r}")

            self._page.evaluate(_EXPAND_PAGINATION_JS)
            self._page.wait_for_timeout(2000)
            frame = _pick_scanner_table(self._page.evaluate(_EXTRACT_TABLES_JS))
            yield normalize_table(frame, captured_at=captured_at)

    def fetch(
        self, scanner: str = "market_profile", days_back: int = 0, attempts: int = 3
    ) -> Snapshot:
        """Fetch with retries. A scraped page fails transiently far more often than it fails
        permanently - a slow render, a redraw mid-read, a stale SPA - and a poll lost to one of
        those is a data point that cannot be recovered, because the scanner keeps no history of
        intraday state.

        Backoff is deliberately generous rather than tight. At ~27 polls a day there is no need
        to hurry, and hammering a subscription site after a failure is exactly the pattern that
        gets an account rate-limited or blocked. Three tries spaced 5s and 15s costs at most 20
        extra seconds against a 15-minute poll interval.
        """
        last_error = None
        for attempt in range(attempts):
            try:
                return self._fetch_once(scanner, days_back)
            except LoginRequired:
                raise  # credentials will not fix themselves by trying again
            except FetchError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    self._page.wait_for_timeout(5000 * (1 + 2 * attempt))
        raise last_error

    def _fetch_once(self, scanner: str = "market_profile", days_back: int = 0) -> Snapshot:
        """Render one scanner route and return its table as a normalized Snapshot.

        days_back > 0 walks the scanner's own day navigation backwards first, which is what
        makes a forward test possible without waiting weeks for live polls to accumulate.
        """
        if scanner not in SCANNER_ROUTES:
            raise ValueError(f"scanner must be one of {sorted(SCANNER_ROUTES)}, got {scanner!r}")

        url = BASE_URL + SCANNER_ROUTES[scanner]
        page = self._page
        # A goto() to a URL differing only in its hash does NOT reload a single-page app, so a
        # page held open all day keeps drifting further from a clean boot. Observed live: polls
        # succeeded at 11:01 and 11:16 then failed every 15 minutes after, the grid rendering a
        # single "no records" row. Reload explicitly so every poll starts from a fresh render.
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.reload(timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(self.settle_ms)

        self._ensure_signed_in(url)

        historical_label = self._step_back(days_back) if days_back else None

        # Ask the grid for every row before reading it, otherwise only the first page
        # (20 of ~220 names) is in the DOM.
        page.evaluate(_EXPAND_PAGINATION_JS)
        page.wait_for_timeout(2500)

        try:
            frame = _pick_scanner_table(page.evaluate(_EXTRACT_TABLES_JS))
        except FetchError:
            if self.debug_dir:
                _dump_debug(page, scanner, self.debug_dir)
            raise

        # A historical view must be stamped with ITS OWN date, never the wall clock - the whole
        # point of pulling past days is to line them up against what price did next.
        if historical_label:
            captured_at = parse_date_label(historical_label)
            if captured_at is None:
                raise FetchError(
                    f"Could not read a date out of the scanner's label {historical_label!r}; "
                    "refusing to stamp a historical snapshot with today's date."
                )
        else:
            captured_at = parse_captured_at(frame) or session_stamp(datetime.now())
        return normalize_table(frame, captured_at=captured_at)


def fetch_snapshot(
    scanner: str = "market_profile",
    headless: bool = True,
    settle_ms: int = 8000,
    debug_dir: str = None,
) -> Snapshot:
    """One-shot convenience wrapper: open a browser, fetch a single scanner, close it.

    Prefer ScannerSession when fetching more than once - this pays the launch and the login
    every call.
    """
    if scanner not in SCANNER_ROUTES:
        raise ValueError(f"scanner must be one of {sorted(SCANNER_ROUTES)}, got {scanner!r}")

    with ScannerSession(headless=headless, settle_ms=settle_ms, debug_dir=debug_dir) as session:
        return session.fetch(scanner)
