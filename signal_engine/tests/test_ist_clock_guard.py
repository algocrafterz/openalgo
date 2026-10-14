"""Guard: no timezone-naive clock reads in signal_engine.

Every timestamp the platform reasons about is IST (see signal_engine/timeutils.py). A bare
datetime.now() reads the HOST timezone, which is only correct while the host happens to be
IST. On a UTC host it silently shifts trading-day keys, market-hours checks and dedup windows.

This test parses every non-test module under signal_engine/ and fails on:
  - datetime.now() without a timezone (or with tz=None)
  - datetime.utcnow() and datetime.utcfromtimestamp() (always naive)
  - datetime.fromtimestamp() without a timezone (or with tz=None)
  - datetime.today(), date.today(), date.fromtimestamp() and time.localtime() (host-local)

Legitimate naive uses (display text, log lines, file names) are listed in ALLOWED_NAIVE below,
keyed by (file, function) with a reason. A new violation fails the test. A stale allowlist
entry (the function no longer has a violation) also fails, so the list cannot rot.
"""

from __future__ import annotations

import ast
from datetime import UTC
from pathlib import Path

SIGNAL_ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = SIGNAL_ENGINE.parent

#: (path relative to the repo root, qualified function name) -> reason the naive clock is fine.
ALLOWED_NAIVE: dict[tuple[str, str], str] = {
    # --- display text, log lines and file names: no trading decision reads these ---
    (
        "signal_engine/scripts/openalgoscheduler.py",
        "build_startup_summary",
    ): "Telegram startup message text only; the time is shown to the user, not compared.",
    (
        "signal_engine/scripts/openalgoscheduler.py",
        "build_shutdown_summary",
    ): "Telegram shutdown message text only; the time is shown to the user, not compared.",
    (
        "signal_engine/scripts/openalgoscheduler.py",
        "notify_failure",
    ): "Telegram failure message text only; the time is shown to the user, not compared.",
    (
        "signal_engine/analysis/breakingtrade/fetcher.py",
        "_dump_debug",
    ): "Builds a debug-dump file name; never compared or stored as a trading time.",
    (
        "signal_engine/scripts/watchlist_screen.py",
        "main",
    ): "Console banner line with the scan time; display only.",
    # --- stored audit stamps: written beside an explicit trade day, never used as a key ---
    (
        "signal_engine/analysis/breakingtrade/store.py",
        "save_btst_candidates",
    ): "recorded_at audit column; the trade_day key is passed in by the caller.",
    (
        "signal_engine/scripts/watchlist_screen.py",
        "run_weekly_screen",
    ): "computed_at metadata on the result object; not used as a date key.",
    (
        "signal_engine/analysis/__main__.py",
        "snapshot",
    ): "Builds a tradebook snapshot file name; never compared as a trading day.",
    # --- retention and housekeeping: a few hours of skew changes nothing ---
    (
        "signal_engine/analysis/breakingtrade/maintenance.py",
        "prune_database",
    ): "Retention cutoff measured in whole days; hours of skew only shift which old rows are pruned.",
    (
        "signal_engine/logger_setup.py",
        "_prune_error_logs",
    ): "Log-file retention cutoff in whole days; no trading decision reads it.",
    # --- date used purely as a placeholder or in operator-run tools ---
    (
        "signal_engine/analysis/breakingtrade/scans.py",
        "_elapsed_fraction",
    ): "Placeholder date for combine(); only the time of day is compared, so the date cancels out.",
    (
        "signal_engine/backtest/attestations.py",
        "append",
    ): "Audit record date when an operator signs off a review; not a trading day.",
    (
        "signal_engine/backtest/broker_login.py",
        "probe_depth",
    ): "Manual operator probe of broker data depth; never run unattended.",
}


def find_violations(source: str) -> list[tuple[str, int, str]]:
    """Return (qualified function, line, call) for each timezone-naive clock read."""
    tree = ast.parse(source)

    module_aliases: set[str] = set()  # names bound to the datetime MODULE
    class_aliases: set[str] = set()  # names bound to the datetime.datetime CLASS
    date_aliases: set[str] = set()  # names bound to the datetime.date CLASS
    time_modules: set[str] = set()  # names bound to the time MODULE
    localtime_names: set[str] = set()  # names bound to time.localtime via from-import
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "datetime":
                    module_aliases.add(alias.asname or "datetime")
                elif alias.name == "time":
                    time_modules.add(alias.asname or "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for alias in node.names:
                if alias.name == "datetime":
                    class_aliases.add(alias.asname or "datetime")
                elif alias.name == "date":
                    date_aliases.add(alias.asname or "date")
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name == "localtime":
                    localtime_names.add(alias.asname or "localtime")

    def is_class(expr: ast.expr, names: set[str], attr: str) -> bool:
        """`expr` is the class bound by `names` (from-import) or `<datetime module>.<attr>`."""
        if isinstance(expr, ast.Name):
            return expr.id in names
        return (
            isinstance(expr, ast.Attribute)
            and expr.attr == attr
            and isinstance(expr.value, ast.Name)
            and expr.value.id in module_aliases
        )

    def tz_given(call: ast.Call, positional_index: int) -> bool:
        """True when a real (non-None) timezone argument is passed."""
        arg = None
        if len(call.args) > positional_index:
            arg = call.args[positional_index]
        for kw in call.keywords:
            if kw.arg == "tz":
                arg = kw.value
        if arg is None:
            return False
        return not (isinstance(arg, ast.Constant) and arg.value is None)

    found: list[tuple[str, int, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def _enter(self, node: ast.AST) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            call = None
            if isinstance(func, ast.Attribute):
                if is_class(func.value, class_aliases, "datetime"):
                    if (
                        (func.attr == "now" and not tz_given(node, 0))
                        or func.attr in {"utcnow", "utcfromtimestamp", "today"}
                        or (func.attr == "fromtimestamp" and not tz_given(node, 1))
                    ):
                        call = func.attr
                elif is_class(func.value, date_aliases, "date"):
                    # date has no timezone parameter: both read the host clock.
                    if func.attr in {"today", "fromtimestamp"}:
                        call = f"date.{func.attr}"
                elif (
                    func.attr == "localtime"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in time_modules
                ):
                    call = "time.localtime"
            elif isinstance(func, ast.Name) and func.id in localtime_names:
                call = "time.localtime"
            if call:
                found.append((".".join(self.scope) or "<module>", node.lineno, call))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def _scan_tree() -> dict[tuple[str, str], list[int]]:
    hits: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SIGNAL_ENGINE.rglob("*.py")):
        rel_parts = path.relative_to(SIGNAL_ENGINE).parts
        if "tests" in rel_parts or "__pycache__" in rel_parts or ".venv" in rel_parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for function, line, _call in find_violations(path.read_text(encoding="utf-8")):
            hits.setdefault((rel, function), []).append(line)
    return hits


class TestDetector:
    """The AST check itself, on small snippets."""

    def test_flags_bare_now(self):
        src = "from datetime import datetime\ndef f():\n    return datetime.now()\n"
        assert find_violations(src) == [("f", 3, "now")]

    def test_accepts_now_with_timezone_positional_and_keyword(self):
        src = (
            "from datetime import datetime\nfrom zoneinfo import ZoneInfo\n"
            "def f():\n    a = datetime.now(ZoneInfo('Asia/Kolkata'))\n"
            "    return datetime.now(tz=ZoneInfo('Asia/Kolkata'))\n"
        )
        assert find_violations(src) == []

    def test_rejects_explicit_none_timezone(self):
        src = (
            "from datetime import datetime\n"
            "def f():\n    a = datetime.now(None)\n    b = datetime.now(tz=None)\n"
            "    return datetime.fromtimestamp(0, None)\n"
        )
        assert [v[2] for v in find_violations(src)] == ["now", "now", "fromtimestamp"]

    def test_flags_utc_variants(self):
        src = (
            "from datetime import datetime\n"
            "def f():\n    return datetime.utcnow(), datetime.utcfromtimestamp(0)\n"
        )
        assert [v[2] for v in find_violations(src)] == ["utcnow", "utcfromtimestamp"]

    def test_fromtimestamp_needs_a_timezone(self):
        src = (
            "from datetime import datetime\n"
            "def f(ts, tz):\n    a = datetime.fromtimestamp(ts)\n"
            "    b = datetime.fromtimestamp(ts, tz)\n    return datetime.fromtimestamp(ts, tz=tz)\n"
        )
        assert [v[1] for v in find_violations(src)] == [3]

    def test_follows_class_alias(self):
        src = "from datetime import datetime as DT\ndef f():\n    return DT.now()\n"
        assert len(find_violations(src)) == 1

    def test_follows_module_alias(self):
        src = "import datetime as dt\ndef f():\n    return dt.datetime.now()\n"
        assert len(find_violations(src)) == 1

    def test_ignores_unrelated_now_calls(self):
        src = "import time\nclass Clock:\n    def now(self): ...\ndef f(c):\n    return c.now(), time.time()\n"
        assert find_violations(src) == []

    def test_qualifies_methods_and_module_level(self):
        src = "from datetime import datetime\nX = datetime.now()\nclass A:\n    def m(self):\n        return datetime.now()\n"
        assert [v[0] for v in find_violations(src)] == ["<module>", "A.m"]


class TestDetectorHostLocalDates:
    """date.today(), datetime.today() and time.localtime() read the host timezone too."""

    def test_flags_datetime_today(self):
        src = "from datetime import datetime\ndef f():\n    return datetime.today()\n"
        assert [v[2] for v in find_violations(src)] == ["today"]

    def test_flags_date_today_and_date_fromtimestamp(self):
        src = (
            "from datetime import date\n"
            "def f(ts):\n    return date.today(), date.fromtimestamp(ts)\n"
        )
        assert [v[2] for v in find_violations(src)] == ["date.today", "date.fromtimestamp"]

    def test_follows_date_alias_and_module_path(self):
        src = (
            "import datetime as dt\nfrom datetime import date as D\n"
            "def f():\n    return dt.date.today(), D.today()\n"
        )
        assert len(find_violations(src)) == 2

    def test_flags_time_localtime_in_every_import_form(self):
        src = (
            "import time\nimport time as t\nfrom time import localtime as lt\n"
            "def f():\n    return time.localtime(), t.localtime(0), lt()\n"
        )
        assert [v[2] for v in find_violations(src)] == ["time.localtime"] * 3

    def test_ignores_host_independent_time_calls(self):
        src = (
            "import time\nfrom datetime import time as clock_time\n"
            "def f():\n    return time.time(), time.monotonic(), clock_time(9, 15), time.gmtime(0)\n"
        )
        assert find_violations(src) == []

    def test_ignores_a_date_object_that_is_not_the_datetime_class(self):
        src = "def f(d):\n    return d.today(), d.date.today()\n"
        assert find_violations(src) == []


class TestIstToday:
    def test_ist_today_is_the_ist_date_on_a_utc_host(self, monkeypatch):
        from datetime import datetime, timezone

        from signal_engine import timeutils

        # 19:00 UTC on 14 Oct is 00:30 IST on 15 Oct.
        instant = datetime(2026, 10, 14, 19, 0, tzinfo=UTC)

        class UtcHost(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)

        monkeypatch.setattr(timeutils, "datetime", UtcHost)
        assert timeutils.ist_today().isoformat() == "2026-10-15"


class TestSignalEngineTree:
    def test_no_unlisted_naive_clock_reads(self):
        unlisted = {k: v for k, v in _scan_tree().items() if k not in ALLOWED_NAIVE}
        assert not unlisted, (
            "Timezone-naive clock read(s) in signal_engine. Use datetime.now(IST) "
            "(signal_engine.timeutils.IST), or add a justified entry to ALLOWED_NAIVE: "
            f"{unlisted}"
        )

    def test_allowlist_has_no_stale_entries(self):
        stale = sorted(set(ALLOWED_NAIVE) - set(_scan_tree()))
        assert not stale, f"ALLOWED_NAIVE entries with no matching violation, remove them: {stale}"

    def test_every_allowlist_entry_states_a_reason(self):
        assert all(len(reason.split()) >= 4 for reason in ALLOWED_NAIVE.values())
