"""G0.1 structural guard: the rules live in strategy_core, adapters only wire them.

A lasting, automated part of the G0.1 evidence (the attestation records the code review and the
run of these checks). It is a structural heuristic, not a proof: it fails when the dependency
shape that "one code path" needs is broken, for instance a core importing an adapter or an
adapter computing rule inputs from the indicator modules itself."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Strategy classes with a shared core and the adapter files that wire each mode to it.
STRATEGIES = {
    "VwapReversion": {
        "core": "vwap_reversion",
        "adapters": {"backtest": "backtest/strategies/vwap_reversion.py"},
    },
    "Breakout": {
        "core": "breakout",
        "adapters": {
            "backtest": "backtest/strategies/breakout.py",
            "paper_live": "pipeline/strategies/breakout.py",
        },
    },
    "EmaVwapEarly": {
        "core": "ema_vwap_early",
        "adapters": {
            "backtest": "backtest/strategies/ema_vwap_early.py",
            "paper_live": "pipeline/strategies/ema_vwap_early.py",
        },
    },
}
EXIT_ADAPTERS = {"pipeline/exit_driver.py": "exits", "pipeline/trail_driver.py": "ema_vwap_early"}
#: What a core may import from this package: itself, the shared features and the clock.
CORE_MAY_IMPORT = ("signal_engine.strategy_core", "signal_engine.features", "signal_engine.timeutils")
#: Indicator and level modules whose output feeds a rule; an adapter reading them is computing
#: rule inputs outside the core. `features.instruments` (tick size) is plumbing, not a rule.
RULE_INPUT_MODULES = (
    "signal_engine.features.price", "signal_engine.features.htf", "signal_engine.features.levels",
    "signal_engine.features.value_area", "signal_engine.features.volume",
)  # fmt: skip


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
    return out


class TestTheCore:
    @pytest.mark.parametrize("path", sorted((ROOT / "strategy_core").glob("*.py")), ids=lambda p: p.name)
    def test_a_core_module_depends_only_on_features_and_itself(self, path):
        bad = [m for m in imports(path) if m.startswith("signal_engine") and not m.startswith(CORE_MAY_IMPORT)]
        assert bad == [], f"{path.name} imports adapter-side modules: {bad}"

    @pytest.mark.parametrize("path", sorted((ROOT / "strategy_core").glob("*.py")), ids=lambda p: p.name)
    def test_a_core_module_does_not_reach_the_executor_or_the_tracker(self, path):
        bad = [m for m in imports(path) if m.split(".")[0] in {"executor", "tracker", "risk", "main"}]
        assert bad == []


class TestTheAdapters:
    @pytest.mark.parametrize("name", STRATEGIES)
    def test_every_adapter_imports_its_strategy_core(self, name):
        spec = STRATEGIES[name]
        for mode, rel in spec["adapters"].items():
            assert (ROOT / rel).exists(), rel
            assert f"signal_engine.strategy_core.{spec['core']}" in imports(ROOT / rel), (name, mode)

    @pytest.mark.parametrize("name", STRATEGIES)
    def test_adapters_do_not_compute_rule_inputs_themselves(self, name):
        for rel in STRATEGIES[name]["adapters"].values():
            bad = [m for m in imports(ROOT / rel) if m in RULE_INPUT_MODULES]
            assert bad == [], f"{rel} reads rule-input modules directly: {bad}"

    def test_the_backtest_breakout_adapter_wraps_the_live_pipeline_strategy(self):
        assert "signal_engine.pipeline.strategies.breakout" in imports(ROOT / "backtest/strategies/breakout.py")

    @pytest.mark.parametrize("rel,core", EXIT_ADAPTERS.items())
    def test_the_exit_drivers_call_the_shared_exit_logic(self, rel, core):
        assert f"signal_engine.strategy_core.{core}" in imports(ROOT / rel)

    def test_the_strategies_with_no_paper_or_live_adapter_are_exactly_the_ones_listed(self):
        live = {p.stem for p in (ROOT / "pipeline" / "strategies").glob("*.py") if p.stem != "__init__"}
        with_live = {s["core"] for s in STRATEGIES.values() if "paper_live" in s["adapters"]}
        assert live == with_live == {"breakout", "ema_vwap_early"}
