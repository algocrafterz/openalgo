"""Guard: signal_engine sends no outgoing Telegram notification, and the inbound signal transport stays.

Outgoing Telegram notifications were removed in November 2026. What remains is NOT a notification:
analysis/breakingtrade/alerts.py posts the scanner's entry signals (and tp_watch's staged TP exits,
through alerts.send) into the strategy channels, and the engine's own Telethon listener reads them
back and trades them. This file pins both halves:

  (a) a static guard: no non-test module or shell script outside an explicit, reasoned allowlist
      contains an outgoing Telegram call or uses a deleted notifier module. A stale allowlist entry
      also fails, so the list cannot rot (same design as test_ist_clock_guard).
  (b) the deleted modules cannot be imported.
  (c) the inbound transport still posts the unchanged signal text to the channel resolved from
      telegram.channels, and send() refuses every kind that is not a trade signal.
  (d) the listener subscribes only to enabled channels.
  (e) the ops phases write their report files and make no HTTP call.
  (f) former alerts are structured log lines, with no HTTP call and an unchanged return value.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.util
import io
import json
import re
import tokenize
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import signal_engine.config as _se_config
from signal_engine.analysis.breakingtrade import alerts, store, tp_watch
from signal_engine.config import TelegramChannel
from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tracker import TrackedPosition

SIGNAL_ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = SIGNAL_ENGINE.parent

# The real send(), captured before any fixture replaces it.
_real_send = alerts.send

# ---------------------------------------------------------------------------------------------
# (a) static guard
# ---------------------------------------------------------------------------------------------

#: Patterns an outgoing Telegram call needs, all case-insensitive. Matched against CODE only
#: (comments and docstrings are removed first, so history written in a comment never trips the
#: guard) and, for Python, against every string the code can build from constants (see _folded).
_SEND_METHODS = (
    r"photo|document|media|video|audio|voice|animation|poll|sticker|location|contact|dice"
)
_CALL_PATTERNS: dict[str, re.Pattern] = {
    name: re.compile(rx, re.IGNORECASE)
    for name, rx in {
        # the Bot API host, and any other Telegram host a request could go to
        "telegram host": r"telegram\.org|\bt\.me/",
        # Bot API methods: sendMessage, sendPhoto, sendDocument, sendMedia, sendMediaGroup, ...
        "sendMessage": r"sendmessage",
        "send<media> (Bot API)": rf"send_?(?:{_SEND_METHODS})",
        # Telethon client methods
        "send_message": r"send_message\b",
        "send_file": r"send_file\b",
        "pin_message": r"pin_?(?:chat_?)?message",
        "forward_messages": r"(?:forward|copy)_?messages?",
        "edit_message": r"edit_?message",
        # Telethon raw requests: SendMessageRequest, ForwardMessagesRequest, ...
        "telethon request": (
            r"\b(?:Send(?:Message|Media|MultiMedia|InlineBotResult|Reaction|Vote)"
            r"|ForwardMessages|UpdatePinnedMessage|EditMessage)Request\b"
        ),
        # OpenAlgo's own Telegram notify endpoint, and its service modules
        "openalgo telegram api": r"api/v1/telegram|telegram/notify",
        "services.telegram_*": (
            r"services[./]telegram|from\s+services\s+import\s+[^\n]*telegram"
            r"|\btelegram_(?:alert|bot)_service\b"
        ),
    }.items()
}

#: The deleted modules. A reference in code (import, patch target, subprocess -m) is a regression.
_DELETED_MODULE_PATTERNS: dict[str, re.Pattern] = {
    "signal_engine.notifier": re.compile(r"signal_engine[./]notifier\b"),
    "from signal_engine import notifier": re.compile(
        r"from\s+signal_engine\s+import\s+[^\n]*\bnotifier\b"
    ),
    "strategy_cards": re.compile(r"\bstrategy_cards\b"),
    "ops.alerts": re.compile(r"\bops[./]alerts\b"),
    "ops.bugalerts": re.compile(r"\bops[./]bugalerts\b"),
    "ops.loginalert": re.compile(r"\bops[./]loginalert\b"),
    "from signal_engine.ops import <deleted>": re.compile(
        r"from\s+signal_engine\.ops\s+import\s+[^\n]*\b(alerts|bugalerts|loginalert)\b"
    ),
}

#: File types the guard reads. A script that posts to Telegram is as much a notification as a
#: Python module: Windows task scripts are .ps1 and .bat.
_SCANNED_SUFFIXES = (".py", ".sh", ".ps1", ".bat")

#: path (relative to the repo root) -> {pattern name: reason}. Every entry must still be needed.
#: Only the inbound signal transport and a reachability probe belong here. The Telethon modules
#: (listener, ingest, eod_review's read-only session check) need NO entry: they never match.
ALLOWED: dict[str, dict[str, str]] = {
    "signal_engine/analysis/breakingtrade/alerts.py": {
        "telegram host": (
            "INBOUND SIGNAL TRANSPORT: the Bot API URL send() posts trade signals to, so the "
            "engine's Telethon listener reads them back and trades them. send() refuses every "
            "kind outside _SIGNAL_KINDS, so no notification can go through it"
        ),
        "sendMessage": (
            "INBOUND SIGNAL TRANSPORT: the Bot API method in that same URL (_TELEGRAM_API)"
        ),
    },
    "signal_engine/scripts/openalgoctl.sh": {
        "telegram host": (
            "network_up() makes a plain GET connectivity check against api.telegram.org as "
            "one of three fallback hosts (curl -fs, output discarded, no token, no message "
            "sent); it notifies nobody"
        ),
    },
}


def _docstring_nodes(parsed: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for node in ast.walk(parsed):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                nodes.add(id(body[0].value))
    return nodes


def _docstring_rows(parsed: ast.AST) -> set[int]:
    rows: set[int] = set()
    for node in ast.walk(parsed):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                rows.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return rows


def _python_code_text(source: str) -> str:
    """The source with comments and docstrings removed. Raises SyntaxError on bad source."""
    parsed = ast.parse(source)
    lines = source.split("\n")
    for row in _docstring_rows(parsed):
        lines[row - 1] = ""
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            lines[row - 1] = lines[row - 1][:col]
    return "\n".join(lines)


_UNKNOWN = "\x00"  # stands for a value the guard cannot know (a variable, a call result)


def _folded(source: str) -> str:
    """Every string the code builds from constants, one per line.

    A URL or method name assembled from pieces never appears whole in the source:
    `"https://api." + "tele" + "gram.org"`, `".".join(["api", "telegram", "org"])`,
    `f"https://api.telegram.org/bot{token}/send" + "Photo"`, `"send{}".format("Photo")`.
    This folds string constants, f-strings, `+`, `%`, `str.join`, `str.format` and names assigned
    a constant string, so the patterns see the joined text. It is a guard against a careless
    change, not against deliberate obfuscation (an encoded host is out of reach by design).
    Docstrings are skipped. Raises SyntaxError on bad source.
    """
    parsed = ast.parse(source)
    skip = _docstring_nodes(parsed)
    known: dict[str, str] = {}

    def fold(node: ast.AST) -> str:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else _UNKNOWN
        if isinstance(node, ast.Name):
            return known.get(node.id, _UNKNOWN)
        if isinstance(node, ast.JoinedStr):
            return "".join(
                fold(v.value) if isinstance(v, ast.FormattedValue) else fold(v) for v in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return fold(node.left) + fold(node.right)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return fold(node.left)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "join" and len(node.args) == 1:
                if isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set)):
                    return fold(node.func.value).join(fold(e) for e in node.args[0].elts)
                return _UNKNOWN
            if node.func.attr == "format":
                return _format(fold(node.func.value), node)
        return _UNKNOWN

    def _format(template: str, call: ast.Call) -> str:
        positional = [fold(a) for a in call.args]
        named = {k.arg: fold(k.value) for k in call.keywords if k.arg}
        counter = iter(range(len(positional) + 1))

        def fill(match: re.Match) -> str:
            field = re.split(r"[!:.\[]", match.group(1), maxsplit=1)[0]
            if field == "":
                index = next(counter)
                return positional[index] if index < len(positional) else _UNKNOWN
            if field.isdigit():
                return positional[int(field)] if int(field) < len(positional) else _UNKNOWN
            return named.get(field, _UNKNOWN)

        return re.sub(r"\{([^{}]*)\}", fill, template)

    # Names assigned a constant string, in source order, so `HOST = "api."; HOST + "telegram.org"`
    # folds. Later assignments win; that is enough for a guard.
    for node in ast.walk(parsed):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            value = fold(node.value)
            if value and _UNKNOWN not in value:
                known[node.targets[0].id] = value
    out = []
    for node in ast.walk(parsed):
        if id(node) in skip:
            continue
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("join", "format")
        ):
            text = fold(node)
            if text.replace(_UNKNOWN, ""):
                out.append(text.replace(_UNKNOWN, "\n"))
    return "\n".join(out)


def _shell_code_text(source: str, rel: str = "x.sh") -> str:
    """A shell, PowerShell or batch file without comments, with split string literals rejoined."""
    if rel.endswith(".ps1"):
        source = re.sub(r"<#.*?#>", "", source, flags=re.DOTALL)
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or (
            rel.endswith(".bat") and re.match(r"(?i)(rem\b|::)", stripped)
        ):
            continue
        if not rel.endswith(".bat"):
            line = re.sub(r"\s#\s.*$", "", line)  # trailing comment
        lines.append(line)
    code = "\n".join(lines)
    # 'api.' + 'telegram.org' (PowerShell) and "api.""telegram.org" (shell) rejoined
    code = re.sub(r"""["']\s*\+\s*["']""", "", code)
    code = re.sub(r"""["']["']""", "", code)
    code = code.replace("^", "") if rel.endswith(".bat") else code  # batch escape character
    return code


def find_hits(rel: str, source: str) -> set[str]:
    """Names of the forbidden patterns that appear in the code of one file."""
    if rel.endswith(".py"):
        code = _python_code_text(source) + "\n" + _folded(source)
    else:
        code = _shell_code_text(source, rel)
    hits = {name for name, pat in _CALL_PATTERNS.items() if pat.search(code)}
    hits |= {name for name, pat in _DELETED_MODULE_PATTERNS.items() if pat.search(code)}
    return hits


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(SIGNAL_ENGINE.rglob("*")):
        if path.suffix not in _SCANNED_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(SIGNAL_ENGINE).parts
        if "__pycache__" in rel or rel[0] == "tests":
            continue
        files.append(path)
    return files


def _scan_tree() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in _scanned_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            hits = find_hits(rel, text)
        except (
            SyntaxError,
            tokenize.TokenError,
        ) as exc:  # a file we cannot read is not proven clean
            pytest.fail(f"{rel} could not be scanned: {exc}")
        if hits:
            found[rel] = hits
    return found


def test_guard_scans_the_files_it_claims_to():
    """A guard that scans nothing passes vacuously."""
    rels = {p.relative_to(REPO_ROOT).as_posix() for p in _scanned_files()}
    assert "signal_engine/main.py" in rels
    assert "signal_engine/analysis/breakingtrade/alerts.py" in rels
    assert "signal_engine/scripts/openalgoctl.sh" in rels
    assert "signal_engine/scripts/openalgoctl.ps1" in rels
    assert "signal_engine/scripts/OpenAlgo-Control.bat" in rels
    assert len(rels) > 100
    assert not any("/tests/" in r for r in rels)


#: (mechanism, file name, snippet, pattern that must be reported). One row per way to send.
_DETECTED = [
    # Bot API host and methods
    ("host", "x.py", 'URL = "https://api.telegram.org/bot1/x"\n', "telegram host"),
    ("host upper case", "x.py", 'URL = "HTTPS://API.TELEGRAM.ORG/bot1"\n', "telegram host"),
    ("sendMessage", "x.py", 'M = "sendMessage"\n', "sendMessage"),
    ("sendMessage lower", "x.py", 'M = "sendmessage"\n', "sendMessage"),
    ("sendPhoto", "x.py", 'M = "sendPhoto"\n', "send<media> (Bot API)"),
    ("sendDocument", "x.py", 'M = "SENDDOCUMENT"\n', "send<media> (Bot API)"),
    ("sendMedia", "x.py", 'M = "sendMediaGroup"\n', "send<media> (Bot API)"),
    ("send_photo", "x.py", "def f(c):\n    c.send_photo(1, b'')\n", "send<media> (Bot API)"),
    ("send_document", "x.py", "def f(c):\n    c.send_document(1, b'')\n", "send<media> (Bot API)"),
    ("send_media", "x.py", "def f(c):\n    c.send_media(1, b'')\n", "send<media> (Bot API)"),
    # Telethon client methods and raw requests
    ("send_message", "x.py", "async def f(c):\n    await c.send_message(1, 'x')\n", "send_message"),
    (
        "send_message by name",
        "x.py",
        "def f(c):\n    return getattr(c, 'send_message')\n",
        "send_message",
    ),
    ("send_file", "x.py", "async def f(c):\n    await c.send_file(1, 'x')\n", "send_file"),
    ("pin_chat_message", "x.py", "def f(b):\n    b.pin_chat_message(1, 2)\n", "pin_message"),
    ("pin_message", "x.py", "async def f(c):\n    await c.pin_message(1, 2)\n", "pin_message"),
    (
        "forward_messages",
        "x.py",
        "async def f(c):\n    await c.forward_messages(1, 2, 3)\n",
        "forward_messages",
    ),
    ("edit_message", "x.py", "def f(c):\n    c.edit_message(1, 2, 'x')\n", "edit_message"),
    (
        "SendMessageRequest",
        "x.py",
        "from telethon.tl.functions.messages import SendMessageRequest\n",
        "telethon request",
    ),
    (
        "SendMessageRequest call",
        "x.py",
        "async def f(c):\n    await c(SendMessageRequest(peer=1, message='x'))\n",
        "telethon request",
    ),
    ("ForwardMessagesRequest", "x.py", "x = ForwardMessagesRequest\n", "telethon request"),
    ("UpdatePinnedMessageRequest", "x.py", "x = UpdatePinnedMessageRequest\n", "telethon request"),
    # URL or method assembled from pieces
    (
        "concatenated URL",
        "x.py",
        'URL = "https://api." + "telegram" + ".org/bot" + "1" + "/sendPhoto"\n',
        "telegram host",
    ),
    (
        "concatenated sendPhoto",
        "x.py",
        'M = "send" + "Photo"\n',
        "send<media> (Bot API)",
    ),
    (
        "joined host",
        "x.py",
        'URL = "https://" + ".".join(["api", "telegram", "org"])\n',
        "telegram host",
    ),
    (
        "f-string host",
        "x.py",
        'def f(t):\n    return f"https://api.telegram.org/bot{t}/send"\n',
        "telegram host",
    ),
    (
        "f-string with a split host",
        "x.py",
        "def f(t):\n    return f\"https://api.{'tele' + 'gram'}.org/bot{t}\"\n",
        "telegram host",
    ),
    (
        "format host",
        "x.py",
        'def f(h):\n    return "https://{}.telegram.org/x".format(h)\n',
        "telegram host",
    ),
    (
        "format method",
        "x.py",
        'def f(t):\n    return "https://h/bot{t}/send{m}".format(t=t, m="Photo")\n',
        "send<media> (Bot API)",
    ),
    (
        "percent format",
        "x.py",
        'def f(t):\n    return "https://api.telegram.org/bot%s/x" % t\n',
        "telegram host",
    ),
    (
        "host through a variable",
        "x.py",
        'A = "api.tele"\nB = "gram.org"\nURL = "https://" + A + B\n',
        "telegram host",
    ),
    (
        "method through a variable",
        "x.py",
        'S = "send"\nURL = "https://h/bot1/" + S + "Message"\n',
        "sendMessage",
    ),
    # OpenAlgo's own Telegram notifier, and its modules
    (
        "notify endpoint",
        "x.py",
        'URL = "http://127.0.0.1:5000/api/v1/telegram/notify"\n',
        "openalgo telegram api",
    ),
    (
        "notify endpoint, concatenated",
        "x.py",
        'URL = base + "/api/v1/" + "telegram/" + "notify"\n',
        "openalgo telegram api",
    ),
    (
        "services.telegram_alert_service",
        "x.py",
        "from services.telegram_alert_service import send_order_alert\n",
        "services.telegram_*",
    ),
    (
        "services.telegram_bot_service",
        "x.py",
        "import services.telegram_bot_service\n",
        "services.telegram_*",
    ),
    (
        "from services import telegram",
        "x.py",
        "from services import telegram_alert_service\n",
        "services.telegram_*",
    ),
    (
        "dynamic import of a telegram service",
        "x.py",
        'import importlib\nm = importlib.import_module("services." + "telegram_alert_service")\n',
        "services.telegram_*",
    ),
    # PowerShell, batch and shell
    (
        "ps1 Invoke-RestMethod",
        "x.ps1",
        'Invoke-RestMethod -Uri "https://api.telegram.org/bot$t/sendMessage" -Method Post\n',
        "telegram host",
    ),
    (
        "ps1 Invoke-WebRequest sendMessage",
        "x.ps1",
        '$u = "https://h/bot$t/sendMessage"\nInvoke-WebRequest -Uri $u\n',
        "sendMessage",
    ),
    (
        "ps1 concatenated host",
        "x.ps1",
        "$u = 'https://api.' + 'telegram' + '.org/bot'\nInvoke-RestMethod $u\n",
        "telegram host",
    ),
    (
        "ps1 sendPhoto",
        "x.ps1",
        'Invoke-RestMethod -Uri "$base/sendPhoto" -Form $form\n',
        "send<media> (Bot API)",
    ),
    (
        "ps1 openalgo notify",
        "x.ps1",
        'Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/telegram/notify"\n',
        "openalgo telegram api",
    ),
    (
        "bat curl host",
        "x.bat",
        "curl -s https://api.telegram.org/bot%T%/sendMessage\n",
        "telegram host",
    ),
    ("bat curl sendMessage", "x.bat", "curl -s %URL%/sendMessage -d x\n", "sendMessage"),
    ("bat escaped host", "x.bat", "curl https://api.tele^gram.org/bot1\n", "telegram host"),
    ("sh curl host", "x.sh", "curl https://api.telegram.org/bot1\n", "telegram host"),
    (
        "sh curl sendDocument",
        "x.sh",
        "curl -F document=@f $U/sendDocument\n",
        "send<media> (Bot API)",
    ),
    (
        "sh notify endpoint",
        "x.sh",
        "curl -X POST $O/api/v1/telegram/notify\n",
        "openalgo telegram api",
    ),
    # the deleted notifier modules
    (
        "signal_engine.notifier",
        "x.py",
        "from signal_engine.notifier import notify\n",
        "signal_engine.notifier",
    ),
    (
        "from signal_engine import notifier",
        "x.py",
        "from signal_engine import main, notifier\n",
        "from signal_engine import notifier",
    ),
    ("strategy_cards", "x.py", "import signal_engine.strategy_cards\n", "strategy_cards"),
    ("ops.alerts", "x.py", "from signal_engine.ops.alerts import x\n", "ops.alerts"),
    ("ops.bugalerts", "x.py", "import signal_engine.ops.bugalerts\n", "ops.bugalerts"),
    ("ops.loginalert", "x.py", "import signal_engine.ops.loginalert\n", "ops.loginalert"),
    (
        "from signal_engine.ops import <deleted>",
        "x.py",
        "from signal_engine.ops import jobs, alerts\n",
        "from signal_engine.ops import <deleted>",
    ),
]


@pytest.mark.parametrize(
    ("name", "suffix", "snippet", "expected"),
    [(n, Path(f).suffix, snip, exp) for n, f, snip, exp in _DETECTED],
    ids=[row[0] for row in _DETECTED],
)
def test_guard_detects_each_way_of_sending(tmp_path, name, suffix, snippet, expected):
    """Each mechanism, written to a real file and read back the way the tree scan reads files."""
    path = tmp_path / f"snippet{suffix}"
    path.write_text(snippet, encoding="utf-8", newline="")
    assert expected in find_hits(path.name, path.read_text(encoding="utf-8")), name


def test_every_pattern_has_a_detection_row():
    """A pattern with no row could break without a test failing."""
    covered = {row[3] for row in _DETECTED}
    assert set(_CALL_PATTERNS) | set(_DELETED_MODULE_PATTERNS) <= covered


_QUIET = [
    (
        "x.py",
        '"""Module doc mentions api.telegram.org and sendMessage and strategy_cards."""\n'
        "# send_message( in a comment, signal_engine.notifier too\n"
        "def f():\n"
        '    """sendMessage in a function docstring."""\n'
        "    return 1\n",
    ),
    ("x.sh", "# curl api.telegram.org\necho hi # api.telegram.org\n"),
    (
        "x.ps1",
        "# Invoke-RestMethod https://api.telegram.org/bot1/sendMessage\n"
        "<#\n Invoke-WebRequest https://api.telegram.org\n#>\n"
        "Write-Output 'ok' # sendMessage in a trailing comment\n",
    ),
    ("x.bat", "REM curl https://api.telegram.org/bot1/sendMessage\n:: sendMessage\n@echo ok\n"),
    # Ordinary code that only looks similar: Telethon reading, an inbound handler, os.sendfile.
    (
        "x.py",
        "import os\n"
        "from telethon import TelegramClient, events\n"
        "async def f(c):\n"
        "    await c.get_messages(1)\n"
        "    os.sendfile(1, 2, 0, 1)\n"
        "    return 'telegram' + ' channel'\n",
    ),
]


@pytest.mark.parametrize(("name", "snippet"), _QUIET)
def test_guard_ignores_comments_docstrings_and_inbound_reading(tmp_path, name, snippet):
    path = tmp_path / name
    path.write_text(snippet, encoding="utf-8", newline="")
    assert find_hits(path.name, path.read_text(encoding="utf-8")) == set()


def test_the_allowlisted_paths_pass_and_nothing_else_in_them_is_hidden():
    """alerts.py and the reachability probe report exactly the patterns their reasons cover."""
    for rel, allowed in ALLOWED.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert find_hits(rel, text) == set(allowed), rel


def test_the_inbound_signal_modules_need_no_allowlist_entry():
    """The listener, ingest, tp_watch and eod_review's session check read or call alerts.send;
    none of them contains an outgoing Telegram call of its own."""
    for rel in (
        "signal_engine/listener.py",
        "signal_engine/ingest.py",
        "signal_engine/analysis/breakingtrade/tp_watch.py",
        "signal_engine/analysis/eod_review.py",
    ):
        path = REPO_ROOT / rel
        if path.exists():
            assert find_hits(rel, path.read_text(encoding="utf-8")) == set(), rel


def test_no_outgoing_telegram_outside_the_allowlist():
    found = _scan_tree()
    violations = []
    for rel, hits in sorted(found.items()):
        unexpected = hits - set(ALLOWED.get(rel, {}))
        if unexpected:
            violations.append(f"{rel}: {sorted(unexpected)}")
    assert not violations, (
        "Outgoing Telegram code or a deleted notifier module is referenced:\n  "
        + "\n  ".join(violations)
        + "\nOutgoing notifications were removed; log the event instead. Only an inbound signal "
        "transport belongs in ALLOWED, with a reason."
    )


def test_every_allowlist_entry_is_still_needed():
    found = _scan_tree()
    stale = []
    for rel, patterns in ALLOWED.items():
        if not (REPO_ROOT / rel).exists():
            stale.append(f"{rel}: file no longer exists")
            continue
        for name, reason in patterns.items():
            assert reason.strip(), f"{rel} {name}: an allowlist entry needs a reason"
            if name not in found.get(rel, set()):
                stale.append(f"{rel}: {name!r} is no longer present, remove the entry")
    assert not stale, "Stale allowlist entries:\n  " + "\n  ".join(stale)


def test_the_bot_api_is_called_from_exactly_one_place():
    """alerts.py makes the one HTTP post; tp_watch only goes through alerts.send."""
    alerts_code = _python_code_text(
        (SIGNAL_ENGINE / "analysis/breakingtrade/alerts.py").read_text()
    )
    assert len(re.findall(r"httpx\s*\.\s*post\s*\(", alerts_code)) == 1
    assert "_TELEGRAM_API" in alerts_code
    tp_code = _python_code_text((SIGNAL_ENGINE / "analysis/breakingtrade/tp_watch.py").read_text())
    assert "httpx" not in tp_code and "requests" not in tp_code
    assert "alerts.send" in tp_code


# ---------------------------------------------------------------------------------------------
# (b) deleted modules cannot be imported
# ---------------------------------------------------------------------------------------------

_DELETED = (
    "signal_engine.notifier",
    "signal_engine.strategy_cards",
    "signal_engine.ops.alerts",
    "signal_engine.ops.bugalerts",
    "signal_engine.ops.loginalert",
)


@pytest.mark.parametrize("name", _DELETED)
def test_deleted_module_cannot_be_imported(name):
    assert importlib.util.find_spec(name) is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)
    stem = name.split("signal_engine.", 1)[1].replace(".", "/")
    assert not (SIGNAL_ENGINE / f"{stem}.py").exists()


# ---------------------------------------------------------------------------------------------
# (c) inbound signal transport
# ---------------------------------------------------------------------------------------------

TOKEN = "123456:TESTTOKENXXXXXXXX"
_CHANNELS = (
    TelegramChannel(name="intraday-breakingtrade-analyze", id=-1001),
    TelegramChannel(name="intraday-breakingtrade-live", id=-1002),
    TelegramChannel(name="intraday-breakingtrade-watchlist-analyze", id=-1003),
    TelegramChannel(name="intraday-breakingtrade-watchlist-live", id=-1004),
    TelegramChannel(name="intraday-breakingtrade-ri-analyze", id=-1005),
    TelegramChannel(name="intraday-breakingtrade-ri-live", id=-1006),
)


def _plan():
    return SimpleNamespace(
        symbol="TCS",
        direction="up",
        entry=2283.1,
        stop=2270.0,
        targets=[2310.0, 2325.0, 2340.0],
        triggered_at=None,
    )


class _Resp:
    def __init__(self, status_code=200, message_id=77):
        self.status_code = status_code
        self.text = "boom"
        self._message_id = message_id

    def json(self):
        if self.status_code != 200:
            return {"description": "Forbidden: bot is not a member"}
        return {"result": {"message_id": self._message_id}}


@pytest.fixture
def transport(tmp_path, monkeypatch):
    """The real send() with every boundary stubbed: token, channels, OpenAlgo mode, HTTP, database."""
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    monkeypatch.setattr(alerts, "send", _real_send)
    monkeypatch.setattr(alerts, "_env", lambda: {"BREAKINGTRADE_BOT_TOKEN": TOKEN})
    monkeypatch.setattr(_se_config, "settings", SimpleNamespace(telegram_channels=_CHANNELS))
    state = SimpleNamespace(posts=[], phase="analyze", response=_Resp())
    monkeypatch.setattr(alerts, "_current_phase", lambda: state.phase)

    def fake_post(url, json=None, timeout=None, **kwargs):
        state.posts.append({"url": url, "json": dict(json or {}), "timeout": timeout})
        if isinstance(state.response, Exception):
            raise state.response
        return state.response

    monkeypatch.setattr(alerts.httpx, "post", fake_post)
    # Nothing else may reach the network either.
    monkeypatch.setattr(httpx.Client, "send", _forbidden_http(state))
    return state


def _forbidden_http(state):
    def boom(*args, **kwargs):
        state.posts.append({"url": "UNEXPECTED httpx.Client.send", "json": {}, "timeout": None})
        raise AssertionError("unexpected HTTP call")

    return boom


def _rows():
    with alerts._connect() as conn:
        return conn.execute(
            "SELECT kind, symbol, message, delivered, message_id, strategy, scan FROM alerts "
            "ORDER BY rowid"
        ).fetchall()


@pytest.mark.parametrize(
    ("phase", "kind", "strategy", "channel", "tag"),
    [
        ("analyze", "trade_signal", "BREAKINGTRADE", "-1001", "[PAPER] "),
        ("live", "trade_signal", "BREAKINGTRADE", "-1002", "[LIVE] "),
        ("analyze", "trade_signal_watchlist", "BREAKINGTRADE-WATCHLIST", "-1003", "[PAPER] "),
        ("live", "trade_signal_watchlist", "BREAKINGTRADE-WATCHLIST", "-1004", "[LIVE] "),
        ("analyze", "trade_signal_ri", "BREAKINGTRADE-RI", "-1005", "[PAPER] "),
        ("live", "trade_signal_ri", "BREAKINGTRADE-RI", "-1006", "[LIVE] "),
    ],
)
def test_alert_trade_signal_posts_the_signal_to_its_strategy_channel(
    transport, phase, kind, strategy, channel, tag
):
    transport.phase = phase
    plan = _plan()
    assert alerts.alert_trade_signal(plan, strategy=strategy, kind=kind) is True
    (post,) = transport.posts
    assert post["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert post["json"]["chat_id"] == channel
    body = alerts.build_trade_signal_message(plan, strategy)
    assert post["json"]["text"] == f"{tag}{body}"
    assert body.startswith(f"{strategy} LONG\nSymbol: TCS\nEntry: 2283.1\nSL: 2270.0\n")
    assert post["timeout"] == 15
    ((row_kind, symbol, message, delivered, message_id, row_strategy, _scan),) = _rows()
    assert (row_kind, symbol, message, delivered, message_id, row_strategy) == (
        kind,
        "TCS",
        body,
        1,
        77,
        strategy,
    )


@pytest.mark.parametrize(
    ("kind", "strategy", "channel"),
    [
        ("trade_signal", "BREAKINGTRADE", "-1001"),
        ("trade_signal_watchlist", "BREAKINGTRADE-WATCHLIST", "-1003"),
        ("trade_signal_ri", "BREAKINGTRADE-RI", "-1005"),
    ],
)
def test_send_tp_hit_posts_the_exit_to_the_entrys_channel(transport, kind, strategy, channel):
    tp_watch._send_tp_hit("TCS", "up", 2296.1, "TP1", 50.0, strategy, kind)
    (post,) = transport.posts
    assert post["json"]["chat_id"] == channel
    assert post["json"]["text"] == (
        f"[PAPER] {strategy} EXIT\nSymbol: TCS\nEntry: 0.0\nSL: 0.0\nTP: 2296.1\n"
        "TPLevel: TP1\nExitQtyPct: 50.0"
    )
    ((row_kind, _sym, _msg, delivered, message_id, row_strategy, scan),) = _rows()
    assert (row_kind, delivered, message_id, row_strategy, scan) == (
        "tp_hit",
        1,
        77,
        strategy,
        "TP1",
    )


def test_send_tp_hit_is_recorded_undelivered_when_the_post_fails(transport):
    transport.response = _Resp(status_code=403)
    tp_watch._send_tp_hit("TCS", "up", 2296.1, "TP1", 50.0, "BREAKINGTRADE", "trade_signal")
    assert len(transport.posts) == 1
    ((row_kind, _s, _m, delivered, message_id, _st, _sc),) = _rows()
    assert (row_kind, delivered, message_id) == ("tp_hit", 0, None)


@pytest.mark.parametrize("failure", [_Resp(status_code=404), ConnectionError("no route")])
def test_send_never_raises_and_reports_not_delivered(transport, failure):
    transport.response = failure
    assert alerts.send("x", "trade_signal") == (False, None)
    alerts.alert_trade_signal(_plan())  # recorded undelivered, never raises
    assert [r[3] for r in _rows()] == [0]


def test_send_is_a_no_op_without_a_token(transport, monkeypatch):
    monkeypatch.setattr(alerts, "_env", lambda: {"BREAKINGTRADE_BOT_TOKEN": None})
    alerts._warned_missing_config.clear()
    assert alerts.send("x", "trade_signal") == (False, None)
    assert transport.posts == []


def test_credentials_clean_the_token_and_resolve_the_chat(transport, monkeypatch):
    monkeypatch.setattr(alerts, "_env", lambda: {"BREAKINGTRADE_BOT_TOKEN": f'  "{TOKEN}"  '})
    assert alerts._credentials("trade_signal") == (TOKEN, "-1001")
    assert alerts._credentials("trade_signal_ri") == (TOKEN, "-1005")


def test_bot_token_is_read_from_the_environment(monkeypatch):
    monkeypatch.setattr(alerts, "_ENV_PATH", "/nonexistent/.env")
    monkeypatch.setenv("BREAKINGTRADE_BOT_TOKEN", TOKEN)
    assert alerts._env()["BREAKINGTRADE_BOT_TOKEN"] == TOKEN


@pytest.mark.parametrize(
    ("kind", "phase", "expected"),
    [
        ("trade_signal", "analyze", "-1001"),
        ("trade_signal", "live", "-1002"),
        ("trade_signal_watchlist", "live", "-1004"),
        ("trade_signal_ri", "live", "-1006"),
    ],
)
def test_chat_id_for_resolves_from_telegram_channels_by_phase(transport, kind, phase, expected):
    transport.phase = phase
    assert alerts.chat_id_for(kind) == expected


def test_chat_id_for_is_none_when_the_channel_is_not_configured(transport, monkeypatch):
    monkeypatch.setattr(_se_config, "settings", SimpleNamespace(telegram_channels=_CHANNELS[:1]))
    transport.phase = "live"
    assert alerts.chat_id_for("trade_signal") is None


def test_the_signal_kinds_are_exactly_the_three_trade_signals():
    assert alerts._SIGNAL_KINDS == {"trade_signal", "trade_signal_watchlist", "trade_signal_ri"}


_REFUSED_KINDS = sorted(
    (set(alerts._CHANNEL_GROUP_BY_KIND) - alerts._SIGNAL_KINDS)
    | {"btst", "btst_empty", "health", "tp_hit", "eod_summary", "intraday_transition", "unknown"}
)


@pytest.mark.parametrize("kind", [*_REFUSED_KINDS, None, ""])
def test_send_refuses_every_kind_that_is_not_a_trade_signal(transport, kind):
    assert alerts.send("a notification", kind) == (False, None)
    assert alerts.send("a notification", kind, monospace=True) == (False, None)
    assert transport.posts == []


def test_notification_helpers_record_and_post_nothing(transport):
    from datetime import datetime

    import pandas as pd

    watch = pd.DataFrame(
        [
            {
                "symbol": "SWIGGY",
                "price": 276.1,
                "change_pct": 2.53,
                "delivery_pct": 0.807,
                "day_type": "Normal Var",
            }
        ]
    )
    when = datetime(2026, 11, 12, 11, 16)
    alerts.alert_transitions({"Volume Scan": ["SBIN"]}, when)
    alerts.alert_btst(watch, when)
    alerts.alert_btst(None, when)
    alerts.alert_health("slow poll")
    alerts.alert_started("analyze", True, "09:20-15:15", 3)
    alerts.alert_stopped(10, 2)
    alerts.record("intraday_transition", "x")  # deliver=True still goes through send()
    assert transport.posts == []
    kinds = [r[0] for r in _rows()]
    assert {"intraday_transition", "btst", "btst_empty", "health"} <= set(kinds)
    assert all(r[3] == 0 for r in _rows())


# ---------------------------------------------------------------------------------------------
# (d) the listener subscribes to enabled channels only
# ---------------------------------------------------------------------------------------------


def _channel_set():
    return (
        TelegramChannel(name="intraday-orb", id=-11),
        TelegramChannel(name="intraday-breakout", id=-22, enabled=False),
        TelegramChannel(name="smidestn", id=-33),
    )


@pytest.mark.asyncio
async def test_start_listener_subscribes_to_the_enabled_channels_only(monkeypatch):
    from telethon import events

    from signal_engine import listener

    seen: dict = {}

    class FakeClient:
        def on(self, builder):
            seen["builder"] = builder
            return lambda handler: handler

        async def disconnect(self):
            seen["disconnected"] = True

    class FakeBuilder:
        def __init__(self, chats=None, **kwargs):
            seen["chats"] = list(chats)

    monkeypatch.setattr(events, "NewMessage", FakeBuilder)
    monkeypatch.setattr(listener, "settings", SimpleNamespace(telegram_channels=_channel_set()))
    monkeypatch.setattr(listener, "_build_client", lambda: FakeClient())
    monkeypatch.setattr(listener, "_retry_until_exhausted", AsyncMock(return_value=(0, 0)))
    monkeypatch.setattr(listener.ingest, "register", lambda client: None)
    monkeypatch.setattr(listener.ingest, "close", lambda: None)
    await listener.start_listener(AsyncMock())
    assert seen["chats"] == [-11, -33]
    assert seen["disconnected"] is True


@pytest.mark.asyncio
async def test_start_listener_builds_no_client_when_every_channel_is_disabled(monkeypatch, logged):
    from signal_engine import listener

    channels = tuple(TelegramChannel(name=f"c{i}", id=-i, enabled=False) for i in (1, 2))
    build = patch.object(listener, "_build_client", side_effect=AssertionError("client built"))
    monkeypatch.setattr(listener, "settings", SimpleNamespace(telegram_channels=channels))
    with build:
        await listener.start_listener(AsyncMock())
    assert logged.has("are disabled", level="ERROR")


def test_split_by_enabled_is_the_subscription_rule():
    from signal_engine.listener import _split_by_enabled

    watching, skipped = _split_by_enabled(_channel_set())
    assert [c.id for c in watching] == [-11, -33]
    assert [c.id for c in skipped] == [-22]


# ---------------------------------------------------------------------------------------------
# no-HTTP boundary used by (e) and (f)
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def no_http(monkeypatch):
    """Every outbound HTTP boundary records the call and raises. Assert `calls == []` afterwards:
    code that catches Exception would otherwise swallow the failure."""
    import socket
    import urllib.request

    calls: list[str] = []

    def make(name):
        def boom(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"outbound HTTP attempted: {name}")

        return boom

    for fn in ("get", "post", "put", "patch", "delete", "head", "request", "stream"):
        monkeypatch.setattr(httpx, fn, make(f"httpx.{fn}"))
    monkeypatch.setattr(httpx.Client, "send", make("httpx.Client.send"))
    monkeypatch.setattr(httpx.AsyncClient, "send", make("httpx.AsyncClient.send"))
    monkeypatch.setattr(urllib.request, "urlopen", make("urllib.request.urlopen"))
    monkeypatch.setattr(socket, "create_connection", make("socket.create_connection"))
    with contextlib.suppress(ImportError):
        import requests

        monkeypatch.setattr(requests.sessions.Session, "request", make("requests"))
    return calls


# ---------------------------------------------------------------------------------------------
# (e) ops phases write their files and make no HTTP call
# ---------------------------------------------------------------------------------------------

from signal_engine.tests.test_ops_manifest import (  # noqa: E402,F401  (tree is a fixture)
    DAY,
    _manifest,
    _run,
    pytestmark_shell,
    tree,
)


def _stub_check(name, status="OK"):
    from signal_engine.ops.model import Check

    return Check(name, status, "stubbed")


def _stub_readiness(monkeypatch, tmp_path):
    from functools import partial

    from signal_engine.ops import model, readiness

    one = {
        "_services": "services",
        "_health": "health",
        "_engine_startup": "engine startup",
        "_poller_started": "poller",
        "_clock": "clock",
        "_late_wake": "late wake",
        "_overnight_errors": "overnight errors",
        "_yesterday_close": "yesterday close",
        "_calendar_coverage": "calendar",
        "_first_cycle": "first cycle",
        "_first_poll": "first poll",
    }
    many = {
        "_broker_and_mode": ["broker", "trading mode"],
        "_ports": ["port 5000"],
        "_disk_and_databases": ["disk"],
        "_morning_tasks": ["tasks"],
        "_storm": ["storm"],
    }
    for attr, name in one.items():
        monkeypatch.setattr(readiness, attr, lambda *a, _n=name, **k: _stub_check(_n))
    for attr, names in many.items():
        monkeypatch.setattr(
            readiness, attr, lambda *a, _ns=names, **k: [_stub_check(n) for n in _ns]
        )
    monkeypatch.setattr(readiness, "_price_history", lambda *a, **k: _stub_check("price history"))
    monkeypatch.setattr(readiness, "write_phase", partial(model.write_phase, folder=str(tmp_path)))
    return readiness


@pytest.mark.parametrize("phase", ["open-day", "liveness"])
def test_ops_phase_writes_its_report_and_makes_no_http_call(phase, tmp_path, monkeypatch, no_http):
    from signal_engine.ops.model import read_phase

    readiness = _stub_readiness(monkeypatch, tmp_path)
    path = readiness.open_day(DAY) if phase == "open-day" else readiness.liveness(DAY, "09:40")
    assert Path(path) == tmp_path / f"{phase}-{DAY}.md"
    assert Path(path).read_text().startswith("# ")
    data = json.loads((tmp_path / f"{phase}-{DAY}.json").read_text())
    assert data["verdict"] == "GREEN" and data["phase"] == phase and data["checks"]
    assert read_phase(phase, DAY, folder=str(tmp_path))["verdict"] == "GREEN"
    assert no_http == []


def test_ops_phase_red_check_still_gives_a_red_report_and_no_http_call(
    tmp_path, monkeypatch, no_http
):
    readiness = _stub_readiness(monkeypatch, tmp_path)
    monkeypatch.setattr(readiness, "_health", lambda *a, **k: _stub_check("health", "FAIL"))
    readiness.open_day(DAY)
    assert json.loads((tmp_path / f"open-day-{DAY}.json").read_text())["verdict"] == "RED"
    assert no_http == []


def test_close_day_verdict_reads_files_and_makes_no_http_call(tmp_path, no_http):
    from signal_engine.ops import verdict

    folder = tmp_path / "review-attempts"
    folder.mkdir()
    out = folder / f"{DAY}-run1.out"
    out.write_text(
        "text\n```close-day-verdict\n"
        '{"verdict": "AMBER", "line": "Net 100; watch one thing."}\n```\n'
    )
    (folder / f"{DAY}-run1.exit").write_text("0\n")
    (folder / f"{DAY}-latest.txt").write_text(out.stem + "\n")
    result = verdict.close_day_verdict(DAY, str(tmp_path))
    assert set(result) == {"verdict", "line"}
    assert result["verdict"] in {"GREEN", "AMBER", "RED"}
    assert no_http == []


@pytest.fixture
def phase_tree(tree, tmp_path):  # noqa: F811 - the imported fixture
    """test_ops_manifest's script tree, plus curl, wget and nc stubs that record any call."""
    repo, env = tree
    stubs = Path(env["PATH"].split(":")[0])
    log = tmp_path / "net.log"
    for name in ("curl", "wget", "nc"):
        (stubs / name).write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log}"\nexit 7\n')
        (stubs / name).chmod(0o755)
    return repo, env, log


@pytestmark_shell
@pytest.mark.parametrize(
    ("script", "args"),
    [
        ("signal_engine/ops/run_phase.sh", ("open-day",)),
        ("signal_engine/ops/run_phase.sh", ("liveness",)),
        ("signal_engine/analysis/close_day.sh", ()),
    ],
)
def test_phase_scripts_write_their_manifest_and_make_no_network_call(phase_tree, script, args):
    repo, env, net_log = phase_tree
    res = _run(repo, env, script, *args, DAY)
    assert res.returncode == 0, res.stderr + res.stdout
    phase = args[0] if args else "close-day"
    manifest = _manifest(repo, phase)
    assert manifest["state"] == "completed"
    assert manifest["steps"]
    assert not net_log.exists(), net_log.read_text()  # no curl, wget or nc call


# ---------------------------------------------------------------------------------------------
# (f) former alerts are log lines, with no HTTP call
# ---------------------------------------------------------------------------------------------


def _position(**overrides):
    defaults = {
        "symbol": "RELIANCE",
        "strategy": "ORB",
        "exchange": "NSE",
        "product": "MIS",
        "entry_price": 2500.0,
        "quantity": 50,
        "sl": 2485.0,
        "tp": 2540.0,
        "direction": Direction.LONG,
        "entry_order_id": "E1",
        "sl_order_id": "SL1",
        "fill_price": 2500.0,
    }
    defaults.update(overrides)
    return TrackedPosition(**defaults)


def _signal():
    return SimpleNamespace(
        symbol="RELIANCE", strategy="ORB", sl=2485.0, entry=2500.0, product="MIS"
    )


@pytest.mark.asyncio
async def test_sl_failure_is_a_structured_error_line_and_returns_no_stop_id(logged, no_http):
    from signal_engine import main

    failed = TradeResult(status=OrderStatus.REJECTED, message="RMS: margin shortfall")
    with patch.object(main, "send_bracket_legs", AsyncMock(return_value=(failed, None))):
        sl_order_id = await main._place_entry_bracket(_signal(), 50, "E1")
    assert sl_order_id == ""
    assert logged.messages("ERROR") == ["SL FAILED | RELIANCE [ORB]: RMS: margin shortfall"]
    assert no_http == []


@pytest.mark.asyncio
async def test_sl_placed_is_an_info_line_and_returns_the_stop_id(logged, no_http):
    from signal_engine import main

    ok = TradeResult(status=OrderStatus.SUCCESS, order_id="SL9", message="ok")
    with patch.object(main, "send_bracket_legs", AsyncMock(return_value=(ok, None))):
        assert await main._place_entry_bracket(_signal(), 50, "E1") == "SL9"
    assert logged.messages("WARNING") == []
    assert no_http == []


@pytest.mark.asyncio
async def test_exit_failure_with_the_position_still_open_logs_error_and_frees_the_retry(
    logged, no_http
):
    from signal_engine import main

    pos = _position(exit_pending=True)
    failed = TradeResult(status=OrderStatus.REJECTED, message="exchange closed")
    with patch.object(main.tracker, "fetch_own_position", AsyncMock(return_value=50)):
        assert await main._handle_exit_order_failure(pos, failed) is None
    errors = logged.messages("ERROR")
    assert "EXIT order failed for RELIANCE: exchange closed" in errors
    assert (
        "EXIT FAILED RELIANCE [ORB]: the broker position is still open (qty 50): exchange closed"
        in errors
    )
    assert pos.exit_pending is False  # the next TP or EXIT signal may retry
    assert pos.sl_order_id == "SL1"  # the stop was not cancelled, so it is kept
    assert no_http == []


@pytest.mark.asyncio
async def test_exit_failure_after_the_stop_was_cancelled_logs_critical_and_reprotects(
    logged, no_http
):
    from signal_engine import main

    pos = _position(exit_pending=True)
    failed = TradeResult(status=OrderStatus.REJECTED, message="exchange closed")
    with (
        patch.object(main.tracker, "fetch_own_position", AsyncMock(return_value=50)),
        patch.object(main.order_reconcile, "get_uncertain", return_value=None),
    ):
        await main._handle_exit_order_failure(pos, failed, stop_cancelled=True)
    assert logged.has("after its stop was cancelled: re-protecting", level="CRITICAL")
    assert pos.sl_order_id == ""  # the dead id is cleared so the poll re-places the stop
    assert pos.exit_pending is False
    assert no_http == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [(OrderStatus.REJECTED, "RMS: insufficient margin"), (OrderStatus.ERROR, "broker 500")],
)
async def test_order_rejection_is_a_warning_line_naming_status_symbol_and_strategy(
    logged, no_http, status, message
):
    from signal_engine import main

    result = TradeResult(status=status, message=message)
    assert await main._notify_entry_outcome(_signal(), result, 1.5) is None
    assert logged.messages("WARNING") == [f"Order {status.value} for RELIANCE [ORB]: {message}"]
    assert no_http == []


@pytest.mark.asyncio
async def test_order_accepted_is_an_info_line_only(logged, no_http):
    from signal_engine import main

    result = TradeResult(status=OrderStatus.SUCCESS, order_id="O1", message="ok")
    await main._notify_entry_outcome(_signal(), result, 1.5)
    assert logged.messages("WARNING") == []
    assert [m for _no, name, m in logged if name == "INFO"] == ["Order placed for RELIANCE: id=O1"]
    assert no_http == []


def test_the_no_http_fixture_really_trips(no_http):
    """Without this the (e) and (f) no-HTTP assertions could pass vacuously."""
    with pytest.raises(AssertionError):
        httpx.post("https://example.invalid/")
    assert no_http == ["httpx.post"]
