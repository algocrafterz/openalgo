"""Spec gate: new ideas need a complete spec.yaml before they can be backtested.

Ideas numbered below `spec.SPEC_REQUIRED_FROM_ID` predate the gate and are exempt; a
spec.yaml they do carry is still validated.
"""

import copy
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br
from signal_engine.research import spec

GOOD = {
    "name": "VWAP EMA momentum",
    "market": "NSE cash equity",
    "timeframe": "5m",
    "trading_style": "intraday",
    "direction": "both",
    "entry": ["5-min close > VWAP", "EMA9 > EMA20", "Enter at next candle open"],
    "exit": ["Close below VWAP", "Maximum hold 12 bars"],
    "stop_loss": ["1 ATR(14) below entry"],
    "take_profit": "none",
    "position_sizing": "Risk 1% per trade",
    "max_holding_period": "12 bars",
    "filters": "none",
    "session_filters": "none",
    "indicators": ["VWAP", "EMA(9)"],
    "parameters": {"ema_fast": 9},
    "cost_assumptions": "16 bps round trip",
    "constraints": "none",
    "rationale": "Trend continuation.",
}

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


def _record(root: Path, number: str):
    return rr.record_idea(
        root, f"IDEA-{number}", execute=_execute, run_date="2026-09-30", supported={"orb"}
    )


class TestRecordGate:
    def test_new_idea_without_spec_is_refused_and_nothing_runs(self, tmp_path):
        folder = _idea(tmp_path, "027")
        called = []
        with pytest.raises(spec.SpecError, match="spec.yaml is required"):
            rr.record_idea(
                tmp_path,
                "IDEA-027",
                execute=lambda k: called.append(k),
                run_date="2026-09-30",
                supported={"orb"},
            )
        assert called == []
        assert not (folder / "runs").exists()

    def test_new_idea_with_incomplete_spec_is_refused(self, tmp_path):
        folder = _idea(tmp_path, "027")
        bad = copy.deepcopy(GOOD)
        bad["entry"] = ["Buy when momentum is strong"]
        (folder / "spec.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(spec.SpecError, match="entry"):
            _record(tmp_path, "027")

    def test_new_idea_with_complete_spec_records(self, tmp_path):
        folder = _idea(tmp_path, "027")
        (folder / "spec.yaml").write_text(yaml.safe_dump(GOOD), encoding="utf-8")
        assert _record(tmp_path, "027").exists()

    def test_existing_idea_without_spec_still_records(self, tmp_path):
        _idea(tmp_path, "012")
        assert _record(tmp_path, "012").exists()


class TestRegistryCheck:
    def test_invalid_spec_on_any_idea_fails_the_registry(self, tmp_path):
        folder = _idea(tmp_path, "012")
        bad = copy.deepcopy(GOOD)
        bad["stop_loss"] = ["below the low"]
        (folder / "spec.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(br.RegistryError, match="stop_loss"):
            br.load_ideas(tmp_path)

    def test_valid_spec_loads(self, tmp_path):
        folder = _idea(tmp_path, "012")
        (folder / "spec.yaml").write_text(yaml.safe_dump(GOOD), encoding="utf-8")
        assert len(br.load_ideas(tmp_path)) == 1

    def test_new_active_idea_without_spec_fails_the_registry(self, tmp_path):
        _idea(tmp_path, "027")
        with pytest.raises(br.RegistryError, match="spec.yaml is required"):
            br.load_ideas(tmp_path)

    def test_new_inbox_idea_without_spec_is_allowed(self, tmp_path):
        folder = _idea(tmp_path, "027")
        text = (folder / "idea.md").read_text(encoding="utf-8").replace("evaluating", "inbox")
        (folder / "idea.md").write_text(text, encoding="utf-8")
        assert len(br.load_ideas(tmp_path)) == 1

    def test_existing_idea_without_spec_loads(self, tmp_path):
        _idea(tmp_path, "012")
        assert len(br.load_ideas(tmp_path)) == 1
