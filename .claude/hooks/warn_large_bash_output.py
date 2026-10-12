#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool: remind Claude about commands that print a lot.

Advisory only. The hook never blocks, never rewrites the command and never truncates
output. It reads the hook JSON on stdin and, for a command that is likely to flood the
context, prints one additionalContext message that suggests a bounded alternative.
Fail-open: any error ends silently with exit 0, so a bug here can never get in the way of
a command. The settings entry that registers this script ends with "|| true" for the same
reason: python exits 2 for a missing script, and exit 2 would block the Bash call.

A command is not warned about when a later pipeline stage clearly limits the output
(head, tail, wc, grep or rg with -l -L -c -q -m, sed -n with a line range) or when stdout
goes to a file, when it runs in the background, or when it is targeted (git diff --stat,
git log -5, a single test file, and so on). A plain grep, rg, awk or jq stage filters
records but does not bound them, so it does not suppress the warning. Full output stays
available: run the command unchanged.

Standard library only, and run as /usr/bin/python3 -I rather than through uv. This is
intentional: the hook runs before every Bash call, so start-up cost matters (about 40 ms
with the system python, against several times that through uv or the pyenv shim).
"""

import glob
import json
import os
import re
import shlex
import sys

MAX_COMMAND_CHARS = 20000
CAT_LARGE_BYTES = 40000
CAT_MAX_FILES = 50
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?\n[ \t]*\2[ \t]*(?:\n|$)", re.DOTALL)
BACKTEST_TARGET = re.compile(r"backtest|sweep|walk_?forward|record_run", re.IGNORECASE)

# Later pipeline stages that clearly limit the amount of output. A plain grep, rg, awk or jq
# is not here on purpose: it filters records but can still print an enormous number of them.
BOUNDING_PROGRAMS = {"head", "tail", "wc"}
FILTER_PROGRAMS = {"grep", "egrep", "fgrep", "rg"}
SED_LINE_RANGE = re.compile(r"^\d+(,\d+)?p$")
DIFF_BOUNDING_FLAGS = (
    "--stat",
    "--shortstat",
    "--numstat",
    "--dirstat",
    "--name-only",
    "--name-status",
    "--summary",
    "--compact-summary",
    "--quiet",
    "--check",
)
GREP_LIMIT_SHORT = set("lLcqm")
GREP_LIMIT_LONG = (
    "--files-with-matches",
    "--files-without-match",
    "--count",
    "--quiet",
    "--max-count",
)
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
UV_RUN_WITH_VALUE = {
    "--with",
    "--python",
    "-p",
    "--directory",
    "--project",
    "--env-file",
    "--group",
    "--extra",
}

WARNING_PREFIX = "Output-size reminder (advisory only, the command still runs unchanged): "
WARNING_SUFFIX = " If the full output is genuinely needed, run the command as it is."


class Command:
    """One simple command: its words and whether its stdout goes to a file."""

    __slots__ = ("words", "stdout_redirected")

    def __init__(self, words, stdout_redirected):
        self.words = words
        self.stdout_redirected = stdout_redirected


def _is_redirect_op(token):
    return bool(token) and all(c in "<>&|" for c in token) and ("<" in token or ">" in token)


def _make_command(tokens):
    words, redirected = [], False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_redirect_op(token):
            fd = words.pop() if words and words[-1] in ("1", "2") else ""
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            index += 2
            if token.startswith("&") or (token in (">", ">>", ">|") and fd != "2"):
                redirected = True
            elif token == ">&" and fd != "2" and target and not (target.isdigit() or target == "-"):
                redirected = True
            continue
        words.append(token)
        index += 1
    return Command(words, redirected)


def parse_pipelines(command):
    """Split a shell command line into pipelines, each a list of Command."""
    text = HEREDOC.sub(r"<<HEREDOC\3\n", command.replace("\\\n", " "))
    text = text.replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = list(lexer)

    pipelines, pipeline, current = [], [], []
    substitution_depth = 0

    def end_command():
        nonlocal current
        if current:
            pipeline.append(_make_command(current))
        current = []

    def end_pipeline():
        nonlocal pipeline
        end_command()
        if pipeline:
            pipelines.append(pipeline)
        pipeline = []

    previous = ""
    for token in tokens:
        punct_only = all(c in "();<>|&" for c in token)
        if substitution_depth:
            # Output of $( ... ) is captured by the shell, not shown, so skip it.
            if punct_only:
                substitution_depth += token.count("(") - token.count(")")
            previous = token
            continue
        if punct_only and "(" in token and previous.endswith("$"):
            substitution_depth = max(token.count("(") - token.count(")"), 0)
            previous = token
            continue
        if punct_only and not _is_redirect_op(token):
            if token in ("|", "|&"):
                end_command()
            else:
                end_pipeline()
        else:
            current.append(token)
        previous = token
    end_pipeline()
    return pipelines


def _strip_wrappers(words):
    """Drop env assignments and wrappers (uv run, timeout, time ...) before the program."""
    i = 0
    while i < len(words):
        word = words[i]
        if ENV_ASSIGNMENT.match(word) or word in (
            "time",
            "nohup",
            "command",
            "exec",
            "sudo",
            "env",
        ):
            i += 1
        elif word in ("nice", "timeout"):
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in ("-n", "-s", "-k", "--signal", "--kill-after") else 1
            if word == "timeout" and i < len(words):
                i += 1
        elif word == "uv" and i + 1 < len(words) and words[i + 1] == "run":
            i += 2
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in UV_RUN_WITH_VALUE else 1
        else:
            break
    return words[i:]


def _program(words):
    stripped = _strip_wrappers(words)
    if not stripped:
        return "", []
    return os.path.basename(stripped[0]), stripped[1:]


def _is_bounding(command):
    program, args = _program(command.words)
    if command.stdout_redirected:
        return True
    if program == "sed":
        return "-n" in args and any(SED_LINE_RANGE.match(a) for a in args)
    if program in FILTER_PROGRAMS:
        return _has_search_limit(args)
    return program in BOUNDING_PROGRAMS


def _has_search_limit(args):
    """True for a grep or rg call that limits its output: -l, -L, -c, -q, -m N."""
    if any(set(flags) & GREP_LIMIT_SHORT for flags in _short_flags(args)):
        return True
    return any(a.startswith(GREP_LIMIT_LONG) for a in args)


def _short_flags(args):
    return [a[1:] for a in args if a.startswith("-") and not a.startswith("--") and len(a) > 1]


def _resolve(path, cwd):
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def _check_git(args, cwd):
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in GIT_GLOBAL_WITH_VALUE else args[1:]
    if not args:
        return None
    sub, rest = args[0], args[1:]
    if sub in ("diff", "show"):
        if any(a.startswith(DIFF_BOUNDING_FLAGS) for a in rest):
            return None
        if "--" in rest and rest.index("--") < len(rest) - 1:
            return None
        if any(os.path.exists(_resolve(a, cwd)) for a in rest if not a.startswith("-")):
            return None
        if sub == "show" and (
            "-s" in rest
            or "--no-patch" in rest
            or any(":" in a for a in rest if not a.startswith("-"))
        ):
            return None
        return f"git {sub} prints the full patch. Consider git {sub} --stat first, or git {sub} -- <file> for one file."
    if sub == "log":
        for i, arg in enumerate(rest):
            if re.fullmatch(r"-\d+|-n\d+|--max-count=\d+", arg) or arg == "--max-count":
                return None
            if arg == "-n" and i + 1 < len(rest):
                return None
        return "git log without a limit prints the whole history. Consider git log -N (for example -20) or --oneline -N."
    return None


def _check_find(args):
    if any(a in ("-maxdepth", "-quit", "-delete", "-exec", "-execdir") for a in args):
        return None
    for i, arg in enumerate(args):
        if (
            arg in ("-name", "-iname")
            and i + 1 < len(args)
            and not re.search(r"[*?\[]", args[i + 1])
        ):
            return None
    return "find with no -maxdepth walks the whole tree. Consider -maxdepth N, a narrower start directory, or -name with an exact file name."


def _search_paths(args):
    """Return the positional paths that follow the search pattern."""
    positional = [a for a in args if not a.startswith("-")]
    by_option = any(
        a in ("-e", "-f", "--regexp", "--file") or a.startswith("--regexp=") for a in args
    )
    return positional if by_option else positional[1:]


def _check_search(program, args, cwd):
    if program in ("grep", "egrep", "fgrep", "zgrep"):
        recursive = (
            "--recursive" in args
            or "--dereference-recursive" in args
            or any(set(flags) & set("rR") for flags in _short_flags(args))
        )
        if not recursive:
            return None
    if _has_search_limit(args):
        return None
    paths = _search_paths(args)
    if paths and all(os.path.isfile(_resolve(p, cwd)) for p in paths):
        return None
    return f"{program} over a directory tree can return hundreds of lines. Consider -l (file names), -c (counts), -m N, a narrower path, or the Grep tool with a limit."


def _check_listing(program, args):
    flags = _short_flags(args)
    if program == "ls" and ("--recursive" in args or any("R" in f for f in flags)):
        return "ls -R lists the whole tree. Consider ls on one directory, find -maxdepth 2, or head -N if a partial view is enough."
    if program == "tree" and not any(a in ("-d", "--noreport") or a.startswith("-L") for a in args):
        return "tree with no -L prints the whole tree. Consider tree -L 2 or tree -d."
    return None


def _check_cat(args, cwd):
    total, seen = 0, 0
    for arg in (a for a in args if not a.startswith("-")):
        for path in glob.glob(_resolve(arg, cwd))[:CAT_MAX_FILES]:
            if os.path.isfile(path):
                total += os.path.getsize(path)
                seen += 1
        if seen >= CAT_MAX_FILES:
            break
    if total <= CAT_LARGE_BYTES:
        return None
    return f"cat would print about {total // 1024} KB (roughly {total // 4000}k tokens). Consider Read with offset and limit, or grep -n for the lines you need."


def _check_runner(program, args):
    if program in ("run_full_tests.sh", "run_full_tests"):
        if "--status" in args:
            return None
        return "The full test suite prints a lot. Consider redirecting to a log (> /tmp/full.log 2>&1) and reading the tail, or grep -E 'FAILED|ERROR|passed|failed'."
    is_pytest = program in ("pytest", "py.test") or (
        program.startswith("python") and args[:2] == ["-m", "pytest"]
    )
    if is_pytest:
        if any(a.endswith(".py") or "::" in a for a in args):
            return None
        return "pytest over a directory or the whole suite can print thousands of lines. Consider running the affected files only, or redirect to a log and use tail -n 30 or grep FAILED."
    if program.startswith("python") and "-c" not in args:
        if any(a in ("--help", "-h", "--list") for a in args):
            return None
        if "-m" in args and args.index("-m") + 1 < len(args):
            target = args[args.index("-m") + 1]
        else:
            target = next((a for a in args if not a.startswith("-")), "")
        if BACKTEST_TARGET.search(target):
            return "Backtest and sweep runs can print thousands of lines. Consider redirecting to a log (> /tmp/run.log 2>&1) and reading the tail or the summary."
    return None


def _check_command(command, cwd):
    program, args = _program(command.words)
    if program == "git":
        return _check_git(args, cwd)
    if program == "find":
        return _check_find(args)
    if program in ("grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack"):
        return _check_search(program, args, cwd)
    if program in ("ls", "tree"):
        return _check_listing(program, args)
    if program == "cat":
        return _check_cat(args, cwd)
    return _check_runner(program, args)


def find_warnings(command, context):
    """Return the list of advisory messages for a Bash command (empty when none)."""
    try:
        if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
            return []
        if context.get("run_in_background"):
            return []
        cwd = context.get("cwd") if isinstance(context.get("cwd"), str) else os.getcwd()
        messages = []
        for pipeline in parse_pipelines(command):
            for index, simple in enumerate(pipeline):
                if simple.stdout_redirected or any(
                    _is_bounding(later) for later in pipeline[index + 1 :]
                ):
                    continue
                message = _check_command(simple, cwd)
                if message and message not in messages:
                    messages.append(message)
        return messages
    except Exception:
        return []


def main():
    try:
        data = json.load(sys.stdin)
        tool_input = data.get("tool_input") if isinstance(data, dict) else None
        if not isinstance(tool_input, dict):
            return 0
        context = {"cwd": data.get("cwd"), "run_in_background": tool_input.get("run_in_background")}
        messages = find_warnings(tool_input.get("command"), context)
        if messages:
            payload = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": WARNING_PREFIX + " ".join(messages) + WARNING_SUFFIX,
                }
            }
            sys.stdout.write(json.dumps(payload))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
