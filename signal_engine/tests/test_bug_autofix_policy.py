"""Auto-fix policy: which bug an unattended run may attempt, and which changes it may keep."""

from signal_engine.bugs import autofix


def _bug(**over):
    bug = {
        "id": "BUG-100",
        "status": "open",
        "kind": "BUG",
        "severity": "LOW",
        "area": "ops",
        "occurrences": ["2026-11-01"],
        "history": [],
    }
    return {**bug, **over}


def test_only_open_low_or_medium_bug_or_data_in_scope_is_eligible():
    assert autofix.eligible(_bug())
    assert autofix.eligible(_bug(kind="DATA", severity="MEDIUM", area="data"))
    assert not autofix.eligible(_bug(severity="HIGH"))
    assert not autofix.eligible(_bug(kind="EXECUTION"))
    assert not autofix.eligible(_bug(kind="TUNING-IDEA"))
    assert not autofix.eligible(_bug(area="openalgo-core"))
    assert not autofix.eligible(_bug(area="strategy"))
    for status in ("in_progress", "fixed", "verified", "wontfix"):
        assert not autofix.eligible(_bug(status=status))


def test_a_bug_is_attempted_at_most_twice():
    note = {"note": "autofix attempt 1: reason"}
    assert autofix.eligible(_bug(history=[note]))
    assert not autofix.eligible(_bug(history=[note, {"note": "autofix attempt 2: x"}]))


def test_pick_prefers_medium_then_most_days_then_oldest():
    bugs = [
        _bug(id="BUG-1", severity="LOW", occurrences=["a", "b", "c"]),
        _bug(id="BUG-2", severity="MEDIUM", occurrences=["a"]),
        _bug(id="BUG-3", severity="MEDIUM", occurrences=["a", "b"]),
        _bug(id="BUG-4", severity="HIGH", occurrences=["a", "b", "c", "d"]),
    ]
    picked = autofix.pick_next(bugs)
    assert picked is not None and picked["id"] == "BUG-3"
    assert autofix.pick_next([_bug(severity="HIGH")]) is None


def test_a_fix_in_ops_code_with_a_new_test_is_in_scope():
    changes = [
        ("signal_engine/ops/readiness.py", 4, 2),
        ("signal_engine/tests/test_ops_phases.py", 20, 0),
    ]
    assert autofix.violations(changes) == []


def test_trading_code_config_and_the_core_are_out_of_scope():
    test = ("signal_engine/tests/test_x.py", 5, 0)
    for path in (
        "signal_engine/executor.py",
        "signal_engine/config.yaml",
        "signal_engine/strategies/orb.py",
        "services/risk/trailing.py",
        "broker/flattrade/api/order_api.py",
        "upgrade/migrate_x.py",
        "signal_engine/analysis/breakingtrade/scan.py",
        "signal_engine/bugs/bugs.json",
        "signal_engine/bugs/autofix.py",
    ):
        assert autofix.violations([(path, 1, 1), test]), path


def test_a_change_without_a_test_or_that_deletes_test_lines_is_refused():
    assert "no test was added" in autofix.violations([("signal_engine/ops/jobs.py", 1, 1)])
    weakened = [
        ("signal_engine/ops/jobs.py", 1, 1),
        ("signal_engine/tests/test_ops_phases.py", 3, 2),
    ]
    assert any("deletes 2 existing test" in p for p in autofix.violations(weakened))


def test_size_limits():
    many = [(f"signal_engine/tests/test_{i}.py", 1, 0) for i in range(9)]
    assert any("files changed" in p for p in autofix.violations(many))
    big = [("signal_engine/ops/jobs.py", 400, 0), ("signal_engine/tests/test_x.py", 1, 0)]
    assert any("lines changed" in p for p in autofix.violations(big))
    assert autofix.violations([]) == ["the run changed nothing"]


def test_numstat_parsing_treats_a_binary_file_as_too_large():
    text = "3\t1\tsignal_engine/ops/jobs.py\n-\t-\tsignal_engine/tests/data.bin\n"
    parsed = autofix.parse_numstat(text)
    assert parsed[0] == ("signal_engine/ops/jobs.py", 3, 1)
    assert any("lines changed" in p for p in autofix.violations(parsed))
