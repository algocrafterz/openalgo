"""Spec gate: every idea needs a complete v2 spec.yaml before it can be backtested.

There is no older-idea exemption: a missing spec refuses the run for any idea number, and an
idea past the inbox without one fails the registry check.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br
from signal_engine.research.intake import spec
from signal_engine.tests.intake_fixtures import GOOD_SPEC, make_spec

IDEA_MD = """---
id: IDEA-{number}
title: Sample
status: evaluating
verdict: none
category: intraday-breakout
horizon: intraday
source: {{type: self}}
summary: One line.
backtest_module: backtest/strategies/orb.py
last_reviewed: 2026-09-29
---

Body.
"""


def _trade(day):
    return Trade(
        symbol="A.NS",
        day=day,
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=f"{day} 15:00",
        reason="tp",
        r_gross=1.1,
        r_net=1.0,
    )


def _execute(key):
    days = [date(2026, 1 + i // 20, 1 + i % 20) for i in range(40)]
    return rr.Execution(
        trades=[_trade(d) for d in days],
        params={},
        data={
            "source": "historify",
            "as_of": days[-1].isoformat(),
            "universe": "u",
            "bar_size": "5m",
        },
        period={"start": days[0].isoformat(), "end": days[-1].isoformat()},
        costs={"model": "flat-round-trip-bps", "round_trip_bps": 16.0},
        days=[d.isoformat() for d in days],
    )


def _idea(root: Path, number: str) -> Path:
    folder = root / "research" / "ideas" / f"idea-{number}-sample"
    folder.mkdir(parents=True)
    module = root / "backtest" / "strategies" / "orb.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("", encoding="utf-8")
    (folder / "idea.md").write_text(IDEA_MD.format(number=number), encoding="utf-8")
    return folder


def _write_spec(folder: Path, data: dict) -> None:
    (folder / "spec.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _record(root: Path, number: str):
    return rr.record_idea(
        root, f"IDEA-{number}", execute=_execute, run_date="2026-09-30", supported={"orb"}
    )


class TestRecordGate:
    @pytest.mark.parametrize("number", ["012", "027"])
    def test_idea_without_spec_is_refused_and_nothing_runs(self, tmp_path, number):
        folder = _idea(tmp_path, number)
        called = []
        with pytest.raises(spec.SpecError, match="spec.yaml is required"):
            rr.record_idea(
                tmp_path,
                f"IDEA-{number}",
                execute=lambda k: called.append(k),
                run_date="2026-09-30",
                supported={"orb"},
            )
        assert called == []
        assert not (folder / "runs").exists()

    def test_idea_with_incomplete_spec_is_refused(self, tmp_path):
        folder = _idea(tmp_path, "012")
        _write_spec(folder, make_spec(entry=["Buy when momentum is strong"]))
        with pytest.raises(spec.SpecError, match="entry"):
            _record(tmp_path, "012")

    def test_idea_with_malformed_spec_is_refused(self, tmp_path):
        folder = _idea(tmp_path, "012")
        _write_spec(folder, make_spec(style="swing", direction="both", product="CNC"))
        with pytest.raises(spec.SpecError, match="long_only"):
            _record(tmp_path, "012")

    def test_idea_with_complete_spec_records(self, tmp_path):
        folder = _idea(tmp_path, "012")
        _write_spec(folder, GOOD_SPEC)
        assert _record(tmp_path, "012").exists()


class TestRegistryCheck:
    def test_invalid_spec_on_any_idea_fails_the_registry(self, tmp_path):
        folder = _idea(tmp_path, "012")
        _write_spec(folder, make_spec(stop=["below the low"]))
        with pytest.raises(br.RegistryError, match="stop"):
            br.load_ideas(tmp_path)

    def test_valid_spec_loads(self, tmp_path):
        folder = _idea(tmp_path, "012")
        _write_spec(folder, GOOD_SPEC)
        assert len(br.load_ideas(tmp_path)) == 1

    @pytest.mark.parametrize("number", ["012", "027"])
    def test_active_idea_without_spec_fails_the_registry(self, tmp_path, number):
        _idea(tmp_path, number)
        with pytest.raises(br.RegistryError, match="spec.yaml is required"):
            br.load_ideas(tmp_path)

    def test_inbox_idea_without_spec_is_allowed(self, tmp_path):
        folder = _idea(tmp_path, "027")
        text = (folder / "idea.md").read_text(encoding="utf-8").replace("evaluating", "inbox")
        (folder / "idea.md").write_text(text, encoding="utf-8")
        assert len(br.load_ideas(tmp_path)) == 1
