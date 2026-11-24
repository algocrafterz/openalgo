"""The close-day review outcome: what the review achieved, read from its report.

review_outcome() is what the /logs workflow page and the close-day manifest show. Nothing here
sends to Telegram (outgoing notifications were removed in November 2026).
"""

from __future__ import annotations

import json
import os
import re

from signal_engine.ops.model import CRITICAL_STEPS, RED_EXIT_OK_STEPS, REPORTS

_BLOCK = re.compile(r"```close-day-verdict\s*\n(.*?)```", re.S)
_TRACKER = re.compile(r"^```bug-tracker\s*$", re.M)
#: Appended by close_day.sh and the catch-up when the run was cut off by a deadline.
_STOPPED = re.compile(r"Review stopped at the (15:58 deadline|time limit)")
#: Where eod_claude_review.sh appends its own sections after the model's text.
_APPENDED = re.compile(r"\n## (Bug tracker updates|Close-day steps)")
#: Real failure texts, seen in the reports (the 5-hour and session limits) plus the Claude CLI's
#: sign-in errors. Conservative on purpose: the "this sign-in ends between ..." warning that a
#: healthy run prints must not match, and only a short output with no verdict is ever tested.
_USAGE_LIMIT = re.compile(
    r"hit your (5-hour |session |weekly |usage )?limit|usage limit (reached|exceeded)"
    r"|credit balance is too low",
    re.I,
)
_SIGN_IN = re.compile(
    r"please run /login|not logged in|invalid api key|oauth token (has )?(expired|revoked)"
    r"|authentication[_ ](error|failed)|sign-in (has )?expired|login (has )?expired",
    re.I,
)
_FAILURE_TEXT_MAX = 3000
ATTEMPTS = "review-attempts"
OUTCOMES = ("verdict", "no_verdict", "limit_or_auth", "failed")


def _valid_verdict(text: str) -> dict | None:
    """The last verdict block when it is a readable GREEN, AMBER or RED with a line."""
    blocks = _BLOCK.findall(text)
    try:
        data = json.loads(blocks[-1])
        if data.get("verdict") in ("GREEN", "AMBER", "RED") and data.get("line"):
            return {"verdict": data["verdict"], "line": str(data["line"])[:300]}
    except (IndexError, ValueError, AttributeError):
        pass
    return None


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


#: Largest part of a review attempt file that is read: the head and the tail of it.
_ATTEMPT_READ_BYTES = 64 * 1024


def _read_capped(path: str) -> str | None:
    """Like _read, but a file over twice the cap yields only its first and last 64 KB."""
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size <= 2 * _ATTEMPT_READ_BYTES:
                raw = handle.read()
            else:
                head = handle.read(_ATTEMPT_READ_BYTES)
                handle.seek(size - _ATTEMPT_READ_BYTES)
                raw = head + b"\n" + handle.read(_ATTEMPT_READ_BYTES)
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def _attempt_order(name: str) -> tuple[str, int]:
    """Sort key of an attempt id <day>-<HHMMSS>[-<n>]: the time, then the same-second counter."""
    parts = name.split("-")[3:]  # after the three date parts
    return (
        parts[0] if parts else "",
        int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1,
    )


def latest_attempt(day: str, folder: str = REPORTS) -> dict | None:
    """The newest review attempt of a day: {id, out, err, exit}. `exit` is None when the attempt
    never wrote its exit file (it was killed). Reads the pointer file first, then the newest file."""
    attempts = os.path.join(folder, ATTEMPTS)
    pointer = _read(os.path.join(attempts, f"{day}-latest.txt"))
    attempt_id = pointer.strip() if pointer else ""
    if not attempt_id or not os.path.exists(os.path.join(attempts, f"{attempt_id}.out")):
        try:
            names = sorted(
                (
                    n[:-4]
                    for n in os.listdir(attempts)
                    if n.startswith(f"{day}-") and n.endswith(".out")
                ),
                key=_attempt_order,
            )
        except OSError:
            return None
        if not names:
            return None
        attempt_id = names[-1]
    code = _read(os.path.join(attempts, f"{attempt_id}.exit"))
    return {
        "id": attempt_id,
        "out": _read_capped(os.path.join(attempts, f"{attempt_id}.out")) or "",
        "err": _read_capped(os.path.join(attempts, f"{attempt_id}.err")) or "",
        "exit": int(code.strip()) if code and code.strip().lstrip("-").isdigit() else None,
    }


def latest_steps(phase: str, day: str, folder: str = REPORTS) -> dict[str, int]:
    """Newest recorded exit code of each step in a status file (a step run again appends a row)."""
    text = _read(os.path.join(folder, f"{phase}-{day}-status.tsv")) or ""
    latest: dict[str, int] = {}
    for line in text.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) == 4 and cols[3].lstrip("-").isdigit():
            latest[cols[0]] = int(cols[3])
    return latest


def failed_critical_steps(phase: str, day: str, folder: str = REPORTS) -> list[str]:
    """Critical steps (signal_engine/ops/model.py CRITICAL_STEPS) whose newest row failed.

    Exit 1 of a reporting step is its own RED finding, not a crash, and is judged by its report.
    """
    out = []
    for name, code in latest_steps(phase, day, folder).items():
        if name not in CRITICAL_STEPS.get(phase, ()) or code == 0:
            continue
        if code == 1 and name in RED_EXIT_OK_STEPS:
            continue
        out.append(name)
    return out


def review_outcome(day: str, folder: str = REPORTS) -> dict:
    """What the close-day review achieved, from its report, never from its exit code alone.

    outcome:
      verdict        a complete report with a readable verdict block. The verdict is the outcome.
                     A non-zero exit code is only noted (`note`). A GREEN verdict whose evidence is
                     incomplete (stop banner, no tracker block, or a failed critical step) is
                     lowered to AMBER.
      no_verdict     no usable verdict: no report, no block, or stopped at a deadline. AMBER.
      limit_or_auth  the review could not run: usage limit or expired sign-in (`cause`). AMBER.
      failed         a non-zero exit with no verdict and no limit or sign-in text. AMBER.
    """
    report = _read(os.path.join(folder, f"eod-{day}-claude-review.md"))
    attempt = latest_attempt(day, folder)
    code = attempt["exit"] if attempt else None
    code_from = "attempt" if code is not None else None
    if code is None:
        code = latest_steps("close-day", day, folder).get("review")
        code_from = "status" if code is not None else None
    result = {
        "outcome": "no_verdict",
        "cause": "",
        "verdict": "AMBER",
        "line": "",
        "exit": code,
        "exit_source": code_from,
        "complete": False,
        "note": "",
        "attempt": attempt["id"] if attempt else None,
    }
    found = _valid_verdict(report) if report else None
    if found:
        stopped = bool(_STOPPED.search(report))
        complete = not stopped and bool(_TRACKER.search(report))
        result.update(outcome="verdict", verdict=found["verdict"], line=found["line"])
        result["complete"] = complete
        if found["verdict"] == "GREEN" and not complete:
            result.update(verdict="AMBER", cause="incomplete")
            result["line"] = (
                f"{found['line']} The review report is incomplete, so this is not GREEN."
            )
        if code not in (0, None):
            result["note"] = (
                f"The review process ended with exit code {code} after it wrote a verdict. "
                "The verdict decides the outcome; the exit code is kept as diagnostic evidence."
            )
        broken = failed_critical_steps("close-day", day, folder)
        if found["verdict"] == "GREEN" and broken and result["verdict"] == "GREEN":
            result.update(verdict="AMBER", cause="critical_step")
            result["line"] = (
                f"{found['line']} A required step failed ({', '.join(broken)}), so this is not GREEN."
            )
        result["text"] = result["line"]
        return result
    # No usable verdict. Look for a limit or sign-in text, in the attempt's output when there is
    # one, else in the report body before the sections the scripts append.
    sample = attempt["out"] + "\n" + attempt["err"] if attempt else ""
    if not sample.strip() and report:
        sample = _APPENDED.split(report)[0]
    sample = sample.strip()
    cause = ""
    if sample and len(sample) <= _FAILURE_TEXT_MAX:
        if _USAGE_LIMIT.search(sample):
            cause = "usage_limit"
        elif _SIGN_IN.search(sample):
            cause = "auth"
    if cause == "usage_limit":
        result.update(outcome="limit_or_auth", cause=cause)
        result["text"] = (
            "The AI review could not run because the Claude usage limit was reached. "
            "Next: wait for the limit to reset. The 16:01 retry and the next morning's 08:36 recovery try again."
        )
    elif cause == "auth":
        result.update(outcome="limit_or_auth", cause=cause)
        result["text"] = (
            "The AI review could not run because the Claude sign-in has expired. "
            "Next: run claude auth login on the server, or save a token with claude setup-token."
        )
    elif (report and _STOPPED.search(report)) or code == 124:
        result["cause"] = "deadline"
        result["text"] = (
            "The AI review produced no verdict because it ran out of time. "
            "Next: wait for the catch-up run, or run /close-day by hand."
        )
    elif code not in (0, None):
        result.update(outcome="failed", cause="exit")
        result["text"] = (
            "The AI review failed. Next: read the newest files in signal_engine/reports/"
            "review-attempts and signal_engine/logs/eod_claude_cron.log."
        )
    else:
        result["cause"] = "no_report" if report is None else "no_block"
        result["text"] = (
            "The AI review produced no verdict, so the day is not confirmed. "
            "Next: wait for the catch-up run, or run /close-day by hand."
        )
    result["line"] = result["text"]
    if code not in (0, None):
        result["note"] = f"The review process exit code was {code} (diagnostic only)."
    return result


def close_day_verdict(day: str, folder: str = REPORTS) -> dict:
    """The review's verdict and line. Never GREEN without a complete verdict (AMBER and the reason)."""
    found = review_outcome(day, folder)
    return {"verdict": found["verdict"], "line": found["line"]}


def _main(argv: list[str] | None = None) -> int:
    """`python3 -m signal_engine.ops.verdict record-review --day D`: store the review outcome in the
    close-day manifest. Called by close_day.sh and review_catchup.sh with the system python."""
    import argparse
    import sys

    from signal_engine.ops.model import manifest_review

    parser = argparse.ArgumentParser(prog="verdict")
    parser.add_argument("action", choices=("record-review",))
    parser.add_argument("--day", required=True)
    args = parser.parse_args(argv)
    found = review_outcome(args.day)
    try:
        manifest_review(
            "close-day", args.day, found["outcome"], found["attempt"] or "", found["exit"] or 0
        )
    except (OSError, TimeoutError, ValueError) as exc:
        sys.stderr.write(f"record-review failed: {type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(f"review {args.day}: {found['outcome']} {found['cause']}\n".rstrip() + "\n")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
