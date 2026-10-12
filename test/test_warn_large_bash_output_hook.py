"""Tests for .claude/hooks/warn_large_bash_output.py.

The hook is a PreToolUse reminder. It must never block, rewrite or truncate a command,
so every test also checks the stdout contract: nothing, or one additionalContext object.
"""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "warn_large_bash_output.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("warn_large_bash_output", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(payload, raw=None):
    stdin = raw if raw is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=stdin, capture_output=True, text=True, timeout=10
    )


WARN = [
    # git diff / show
    "git diff",
    "git diff main...HEAD",
    "git diff HEAD~3",
    "git show HEAD",
    "git show abc1234",
    # git log
    "git log",
    "git log --oneline",
    "git log --format='%ad' --date=short",
    "git log -S foo",
    # find
    "find . -name '*.py'",
    "find /home/anand/github/openalgo -type f",
    "cd signal_engine && find . -type f",
    # broad recursive search
    "grep -rn foo .",
    "grep -R foo",
    "rg foo",
    "rg -n 'def main' signal_engine",
    "ls -R",
    "ls -lR signal_engine",
    "tree",
    "tree signal_engine",
    # test and backtest commands
    "uv run pytest",
    "PYTHONPATH=. uv run pytest signal_engine/tests -q",
    "uv run pytest test/ -v",
    "signal_engine/scripts/run_full_tests.sh",
    "signal_engine/scripts/run_full_tests.sh --wait",
    "uv run python -m signal_engine.backtest --idea IDEA-033",
    "uv run python backtesting/capital_sweep.py",
    "uv run python -m signal_engine.backtest.record_run --idea X",
    # a filter that does not limit the record count is not a bound
    "git diff | grep '^+'",
    "uv run pytest test/ 2>&1 | grep -E 'FAILED|passed|failed'",
    "git diff | awk '{print $1}'",
    "git diff | jq .",
    "rg foo . | rg bar",
    "git diff | sed -n '/foo/p'",
    "git log | less",
    # chained: the unbounded part is still reported
    "cd /tmp && git log",
    "git status && git diff",
]

PASS = [
    # small by nature
    "git status",
    "git status --short",
    "git branch",
    "git ls-files signal_engine/risk.py",
    "ls",
    "ls -la signal_engine",
    "pwd",
    "echo hello",
    "uv run ruff check .",
    "uv run python -c 'print(1)'",
    "uv run python signal_engine/tools/small.py",
    # already targeted or bounded git
    "git diff --stat",
    "git diff --name-only",
    "git diff --name-status HEAD~1",
    "git diff --shortstat",
    "git diff --quiet",
    "git diff -- signal_engine/risk.py",
    "git diff HEAD -- signal_engine/risk.py signal_engine/config.py",
    "git show --stat HEAD",
    "git show --name-only HEAD",
    "git show HEAD:signal_engine/risk.py",
    "git log -5",
    "git log -n 5 --oneline",
    "git log --max-count=10",
    "git log --oneline -20",
    "git log --format=%ad -1",
    "git log -1",
    # targeted find
    "find . -maxdepth 2 -name '*.md'",
    "find . -name CLAUDE.md -print -quit",
    "find . -name CLAUDE.md",
    # targeted search
    "grep -rl foo .",
    "grep -rc foo signal_engine",
    "grep -rn -m 3 foo .",
    "grep -n foo signal_engine/risk.py",
    "grep foo signal_engine/risk.py signal_engine/config.py",
    "rg -l foo",
    "rg --count foo",
    "rg -m 5 foo",
    "rg --files-with-matches foo",
    "ls -R | head -50",
    "tree -L 2",
    "tree -d",
    # tests that are targeted
    "PYTHONPATH=. uv run pytest signal_engine/tests/test_risk.py -q -p no:cacheprovider",
    "uv run pytest signal_engine/tests/test_risk.py::test_sizing -q",
    "uv run pytest test/test_warn_large_bash_output_hook.py",
    "signal_engine/scripts/run_full_tests.sh --status",
    # bounded in the same pipeline
    "git diff | head -100",
    "git diff | tail -50",
    "git log | head -20",
    "find . -name '*.py' | head -20",
    "find . -type f | wc -l",
    "grep -rn foo . | head -20",
    "rg foo | head",
    "uv run pytest | tail -30",
    "uv run pytest 2>&1 | tail -30",
    "git diff | grep '^+' | wc -l",
    "git diff | sed -n '1,80p'",
    # filters that clearly limit the output
    "git diff | grep -c '^+'",
    "git diff | grep -l foo",
    "git diff | grep -m 5 foo",
    "git diff | rg -m 3 foo",
    "git diff | rg --count foo",
    "git diff | sed -n '1,80p'",
    "git diff | sed -n 5p",
    "git diff | grep -q foo",
    # bounded by a redirect to a file
    "git diff > /tmp/out.diff",
    "git log >> /tmp/log.txt",
    "uv run pytest > /tmp/pytest.log 2>&1",
    "uv run pytest &> /tmp/pytest.log",
    "uv run python -m signal_engine.backtest --idea X > /tmp/bt.log 2>&1",
    "find . -type f > /tmp/files.txt",
    "uv run pytest | tee /tmp/pytest.log | tail -20",
    # heredoc cat is not a file dump
    "cat <<'EOF'\nhello\nEOF",
    "cat > /tmp/x.txt <<'EOF'\nhello\nEOF",
    # a quoted or echoed mention is not a command
    "echo 'run git log later'",
    "git commit -m 'git diff cleanup'",
    # empty and odd input
    "",
    "   ",
]


@pytest.mark.parametrize("command", WARN)
def test_warns_on_unbounded_large_output(hook, command):
    assert hook.find_warnings(command, {}) != [], command


@pytest.mark.parametrize("command", PASS)
def test_no_warning_for_small_or_bounded(hook, command):
    assert hook.find_warnings(command, {}) == [], command


def test_background_commands_are_not_warned(hook):
    # Output goes to a file the caller reads later, so nothing floods the context.
    assert hook.find_warnings("uv run pytest", {"run_in_background": True}) == []
    assert hook.find_warnings("git log", {"run_in_background": True}) == []


def test_each_warning_names_a_bounded_alternative(hook):
    expected = {
        "git diff": "--stat",
        "git log": "-N",
        "find . -type f": "-maxdepth",
        "grep -rn foo .": "-l",
        "uv run pytest": "tail",
        "ls -R": "head",
    }
    for command, fragment in expected.items():
        text = " ".join(hook.find_warnings(command, {}))
        assert fragment in text, (command, text)


def test_cat_warns_only_for_large_files(hook, tmp_path):
    big = tmp_path / "big.log"
    big.write_text("x" * 200_000)
    small = tmp_path / "small.txt"
    small.write_text("hello\n")
    cwd = {"cwd": str(tmp_path)}

    assert hook.find_warnings(f"cat {big}", cwd) != []
    assert hook.find_warnings("cat big.log", cwd) != []
    assert hook.find_warnings("cat *.log", cwd) != []
    assert "Read" in " ".join(hook.find_warnings("cat big.log", cwd))

    assert hook.find_warnings(f"cat {small}", cwd) == []
    assert hook.find_warnings("cat small.txt", cwd) == []
    # unknown or missing files are never guessed to be large
    assert hook.find_warnings("cat does-not-exist.txt", cwd) == []
    # bounded in the pipeline or redirected
    assert hook.find_warnings("cat big.log | head -20", cwd) == []
    assert hook.find_warnings("cat big.log > /tmp/copy.log", cwd) == []


def test_cat_of_many_small_files_that_add_up_still_warns(hook, tmp_path):
    names = []
    for i in range(5):
        path = tmp_path / f"part{i}.txt"
        path.write_text("y" * 20_000)
        names.append(path.name)
    assert hook.find_warnings("cat " + " ".join(names), {"cwd": str(tmp_path)}) != []


def test_git_diff_of_an_existing_path_without_separator_is_targeted(hook, tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    (tmp_path / "pkg").mkdir()
    cwd = {"cwd": str(tmp_path)}
    assert hook.find_warnings("git diff mod.py", cwd) == []
    assert hook.find_warnings("git diff HEAD~1 mod.py", cwd) == []
    assert hook.find_warnings("git diff pkg", cwd) == []
    assert hook.find_warnings("git show HEAD mod.py", cwd) == []
    # a revision that is not a path is still a whole-patch diff
    assert hook.find_warnings("git diff HEAD~1", cwd) != []
    assert hook.find_warnings("git diff nosuchfile.py", cwd) != []


def test_rg_on_single_existing_file_is_not_a_broad_search(hook, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("def main():\n    pass\n")
    cwd = {"cwd": str(tmp_path)}
    assert hook.find_warnings("rg main mod.py", cwd) == []
    assert hook.find_warnings("grep -r main mod.py", cwd) == []
    assert hook.find_warnings("rg main", cwd) != []


def test_hook_prints_only_additional_context_and_never_blocks():
    result = run_hook({"tool_name": "Bash", "tool_input": {"command": "git log"}, "cwd": "/tmp"})
    assert result.returncode == 0
    assert result.stderr == ""
    out = json.loads(result.stdout)
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert "additionalContext" in specific
    assert "permissionDecision" not in specific
    assert "updatedInput" not in specific
    assert set(out) == {"hookSpecificOutput"}


def test_hook_is_silent_for_small_command():
    result = run_hook({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        "null",
        json.dumps({}),
        json.dumps({"tool_input": None}),
        json.dumps({"tool_input": {"command": None}}),
        json.dumps({"tool_input": {"command": 123}}),
        json.dumps({"tool_input": {"command": "echo 'unterminated"}}),
        json.dumps({"tool_input": {"command": "cat " + "\x00" * 4}}),
    ],
)
def test_hook_never_fails_on_malformed_input(raw):
    result = run_hook(None, raw=raw)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_fail_open_when_internal_code_raises(hook, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated hook bug")

    monkeypatch.setattr(hook, "parse_pipelines", boom)
    assert hook.find_warnings("git log", {}) == []

    monkeypatch.setattr(hook, "find_warnings", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_input":{"command":"git log"}}'))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_fail_open_when_stdout_cannot_be_written(hook, monkeypatch):
    class BrokenOut:
        def write(self, _):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_input":{"command":"git log"}}'))
    monkeypatch.setattr(sys, "stdout", BrokenOut())
    assert hook.main() == 0


def test_registered_command_form_cannot_block_when_script_is_missing():
    # python exits 2 for a missing script, and exit 2 is a blocking hook error.
    # The registration therefore ends with "|| true".
    missing = "/usr/bin/python3 -I /nonexistent/warn_large_bash_output.py || true"
    result = subprocess.run(["sh", "-c", missing], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0


def test_hook_source_has_no_block_or_rewrite_path():
    source = HOOK.read_text()
    assert "updatedInput" not in source
    assert "permissionDecision" not in source
