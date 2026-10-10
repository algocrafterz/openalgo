"""The UI regression fixture is a faithful copy of IDEA-029 v4's frozen spec.

`StrategyDefinition.test.tsx` renders `fixtures/idea029-v4-frozen-spec.json`. If it drifted from
the recorded spec, those tests would pass against a shape no run has.
"""

import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SPEC = (
    REPO
    / "signal_engine/research/ideas/idea-029-vwap-2sd-range-mean-reversion/runs"
    / "2026-10-09-vwap-2sd-range-mean-reversion-v4/strategy/spec.yaml"
)
FIXTURE = REPO / "frontend/src/components/backtest-results/fixtures/idea029-v4-frozen-spec.json"


def test_the_ui_fixture_equals_the_recorded_v4_frozen_spec():
    recorded = json.loads(json.dumps(yaml.safe_load(SPEC.read_text("utf-8")), default=str))
    assert json.loads(FIXTURE.read_text("utf-8")) == recorded


def test_the_recorded_spec_uses_the_schema_the_ui_must_support():
    spec = yaml.safe_load(SPEC.read_text("utf-8"))
    for key in ("stop", "sizing", "holding_period", "re_entry", "timing", "style"):
        assert key in spec, key
    assert all(isinstance(i, dict) for i in spec["indicators"])
