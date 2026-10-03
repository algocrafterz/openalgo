"""Assumption ledger: every resolved value is an entry with a source and a confirmation."""

from __future__ import annotations

import pytest
import yaml

from signal_engine.backtest import record_run
from signal_engine.research.intake import fields, ledger
from signal_engine.tests.intake_fixtures import AMBIGUOUS_TIMING, GOOD_SPEC, make_spec


def _resolve(spec=None, **kw):
    return ledger.resolve(spec if spec is not None else make_spec(), **kw)


class TestUserEntries:
    def test_stated_fields_are_user_sourced_and_confirmed(self):
        led = _resolve()
        for name in (
            "style",
            "direction",
            "timeframe",
            "entry",
            "stop",
            "profile",
            "sizing.capital_per_trade",
        ):
            entry = led.get(name)
            assert entry.source == "user" and entry.user_confirmed is True
            assert entry.section == "intent" and entry.confidence == "high"

    def test_value_is_what_the_author_wrote(self):
        led = _resolve()
        assert led.get("style").value == "intraday"
        assert led.get("entry").value == GOOD_SPEC["entry"]
        assert led.get("sizing.capital_per_trade").value == 100000

    def test_absent_optional_fields_have_no_entry(self):
        led = _resolve()
        assert not led.has("kill_criterion")
        assert not led.has("order_types")

    def test_materiality_comes_from_the_catalog(self):
        led = _resolve()
        assert led.get("entry").materiality == fields.get("entry").materiality


class TestInferredTiming:
    def test_implied_timing_is_recorded_at_medium(self):
        led = _resolve()
        for name, value in (
            ("timing.signal_evaluation", "bar_close"),
            ("timing.order_submission", "signal_close"),
            ("timing.fill_model", "next_bar_open"),
        ):
            entry = led.get(name)
            assert entry.value == value
            assert entry.source == "inferred"
            assert entry.materiality == "medium" and entry.confidence == "high"
            assert entry.user_confirmed is False

    def test_reason_quotes_the_entry_text(self):
        assert "close" in _resolve().get("timing.signal_evaluation").reason

    def test_not_implied_timing_is_high_materiality(self):
        led = _resolve(make_spec(**AMBIGUOUS_TIMING))
        entry = led.get("timing.signal_evaluation")
        assert entry.value == "bar_close"
        assert entry.materiality == "high" and entry.confidence == "medium"
        assert led.get("timing.fill_model").materiality == "high"

    def test_stated_timing_is_user_sourced(self):
        s = make_spec(
            timing={
                "signal_evaluation": "bar_open",
                "order_submission": "next_bar_open",
                "fill_model": "next_bar_open",
            }
        )
        entry = _resolve(s).get("timing.signal_evaluation")
        assert entry.source == "user" and entry.value == "bar_open"

    def test_partially_stated_timing_infers_only_the_rest(self):
        led = _resolve(make_spec(timing={"fill_model": "signal_close"}))
        assert led.get("timing.fill_model").source == "user"
        assert led.get("timing.signal_evaluation").source == "inferred"

    def test_intrabar_wording_is_inferred_as_intrabar_and_high(self):
        s = make_spec(entry=["Price touches the 20 day high", "Enter on the touch"])
        entry = _resolve(s).get("timing.signal_evaluation")
        assert entry.value == "intrabar" and entry.materiality == "high"


class TestOtherInferences:
    def test_missing_scope_is_inferred_high_per_symbol(self):
        led = _resolve(make_spec(portfolio={"mode": "single_position"}))
        entry = led.get("portfolio.scope")
        assert entry.value == "per_symbol" and entry.source == "inferred"
        assert entry.materiality == "high" and entry.user_confirmed is False

    def test_one_position_wording_infers_global(self):
        s = make_spec(
            portfolio={"mode": "single_position"},
            filters=["Only one position at a time across the universe"],
        )
        assert _resolve(s).get("portfolio.scope").value == "global"

    def test_stated_scope_is_user(self):
        assert _resolve().get("portfolio.scope").source == "user"

    def test_multi_position_has_no_scope_entry(self):
        from signal_engine.tests.intake_fixtures import MULTI_POSITION

        assert not _resolve(make_spec(portfolio=MULTI_POSITION)).has("portfolio.scope")

    def test_re_entry_trigger_defaults_when_allowed(self):
        led = _resolve(make_spec(re_entry={"allowed": True, "max_per_symbol_day": 2}))
        entry = led.get("re_entry.trigger")
        assert entry.value == "after_stop" and entry.source == "platform_default"

    def test_no_trigger_entry_when_re_entry_off(self):
        assert not _resolve().has("re_entry.trigger")


class TestExecutionEntries:
    def test_platform_defaults_are_entries(self):
        led = _resolve()
        slip = led.get("execution.slippage_bps_per_side")
        assert slip.value == record_run.SLIPPAGE_BPS_PER_SIDE
        assert slip.source == "platform_default" and slip.section == "execution"
        assert slip.materiality == "medium" and slip.user_confirmed is False

    def test_profile_values_are_broker_config_entries(self):
        entry = _resolve().get("profile.mis_squareoff_min")
        assert entry.value == 900 and entry.source == "broker_config"
        assert entry.section == "profile"

    def test_profile_values_do_not_need_confirmation(self):
        assert fields.get("profile.mis_squareoff_min").materiality != "high"

    def test_product_is_a_derived_entry(self):
        entry = _resolve(make_spec(product=None)).get("execution.product")
        assert entry.source == "derived" and entry.value == "MIS"

    def test_override_is_user_sourced_with_its_reason(self):
        led = _resolve(overrides={"slippage_bps_per_side": {"value": 8, "reason": "thin"}})
        entry = led.get("execution.slippage_bps_per_side")
        assert entry.value == 8 and entry.source == "user"
        assert entry.reason == "thin" and entry.user_confirmed is True

    def test_bad_override_raises_not_hidden(self):
        from signal_engine.research.intake import execution

        with pytest.raises(execution.ExecutionError):
            _resolve(overrides={"slippage_bps_per_side": {"value": -1, "reason": "x"}})


class TestRobustOnDrafts:
    def test_missing_entry_does_not_raise_and_skips_timing(self):
        led = _resolve(make_spec(entry=None))
        assert led.get("style").value == "intraday"
        assert not led.has("timing.signal_evaluation")

    def test_unknown_profile_skips_execution_entries(self):
        led = _resolve(make_spec(profile="no-such"))
        assert not any(e.section in ("execution", "profile") for e in led.entries)

    def test_invalid_style_skips_execution_entries(self):
        led = _resolve(make_spec(style="weekly"))
        assert not any(e.section == "execution" for e in led.entries)


class TestIds:
    def test_ids_are_sequential_and_unique(self):
        led = _resolve()
        ids = [e.id for e in led.entries]
        assert ids == [f"A-{i:03d}" for i in range(1, len(ids) + 1)]
        assert led.next_id == len(ids) + 1

    def test_every_entry_has_a_valid_source_materiality_and_confidence(self):
        for e in _resolve().entries:
            assert e.source in ledger.SOURCES
            assert e.materiality in fields.MATERIALITIES
            assert e.confidence in ledger.CONFIDENCES
            assert e.reason

    def test_field_names_are_unique(self):
        names = [e.field for e in _resolve().entries]
        assert len(names) == len(set(names))


class TestMerge:
    def _confirmed(self, spec):
        first = _resolve(spec)
        return ledger.confirm(first, [e.id for e in first.entries if e.materiality == "high"])

    def test_unchanged_reresolve_keeps_ids_and_confirmations(self):
        spec = make_spec(portfolio={"mode": "single_position"})
        confirmed = self._confirmed(spec)
        again = _resolve(spec, existing=confirmed)
        assert again.entries == confirmed.entries
        assert again.get("portfolio.scope").user_confirmed is True

    def test_changed_value_keeps_id_but_resets_confirmation(self):
        spec = make_spec(portfolio={"mode": "single_position"})
        confirmed = self._confirmed(spec)
        changed = make_spec(
            portfolio={"mode": "single_position"},
            filters=["Only one position at a time across the universe"],
        )
        again = _resolve(changed, existing=confirmed)
        before, after = confirmed.get("portfolio.scope"), again.get("portfolio.scope")
        assert after.id == before.id
        assert after.value == "global" and after.user_confirmed is False

    def test_new_field_gets_a_fresh_id(self):
        first = _resolve()
        again = _resolve(make_spec(kill_criterion="stop at 3R loss"), existing=first)
        new = again.get("kill_criterion")
        assert new.id not in {e.id for e in first.entries}
        assert again.get("style").id == first.get("style").id

    def test_ids_are_never_reused_after_a_field_disappears(self):
        first = _resolve(make_spec(kill_criterion="stop at 3R loss"))
        gone = first.get("kill_criterion").id
        second = _resolve(make_spec(), existing=first)
        assert not second.has("kill_criterion")
        third = _resolve(make_spec(rationale="trend"), existing=second)
        assert third.get("rationale").id != gone

    def test_user_entries_stay_confirmed_when_their_value_changes(self):
        first = _resolve()
        again = _resolve(make_spec(direction="long_only"), existing=first)
        assert again.get("direction").user_confirmed is True

    def test_open_questions_and_blocked_reason_survive_merge(self):
        first = ledger.Ledger(
            entries=_resolve().entries,
            open_questions=("what is a breakout?",),
            blocked_reason="waiting on author",
            next_id=99,
        )
        again = _resolve(existing=first)
        assert again.open_questions == ("what is a breakout?",)
        assert again.blocked_reason == "waiting on author"
        assert again.next_id >= 99


class TestConfirm:
    def test_confirm_marks_entries_without_mutating_the_original(self):
        led = _resolve(make_spec(portfolio={"mode": "single_position"}))
        target = led.get("portfolio.scope").id
        out = ledger.confirm(led, [target])
        assert out.get("portfolio.scope").user_confirmed is True
        assert led.get("portfolio.scope").user_confirmed is False

    def test_unknown_id_raises(self):
        with pytest.raises(ledger.LedgerError, match="A-999"):
            ledger.confirm(_resolve(), ["A-999"])


class TestPersistence:
    def test_roundtrip(self, tmp_path):
        led = ledger.Ledger(
            entries=_resolve().entries,
            open_questions=("q1",),
            blocked_reason=None,
            next_id=77,
        )
        ledger.save(tmp_path, led)
        assert ledger.load(tmp_path) == led

    def test_load_missing_is_none(self, tmp_path):
        assert ledger.load(tmp_path) is None

    def test_file_is_plain_yaml_with_the_documented_keys(self, tmp_path):
        ledger.save(tmp_path, _resolve())
        raw = yaml.safe_load((tmp_path / "assumptions.yaml").read_text(encoding="utf-8"))
        assert set(raw) == {"next_id", "open_questions", "blocked_reason", "entries"}
        assert set(raw["entries"][0]) == {
            "id",
            "field",
            "section",
            "value",
            "source",
            "confidence",
            "materiality",
            "reason",
            "user_confirmed",
        }

    def test_unreadable_yaml_raises(self, tmp_path):
        (tmp_path / "assumptions.yaml").write_text("a: [unclosed", encoding="utf-8")
        with pytest.raises(ledger.LedgerError, match="unreadable"):
            ledger.load(tmp_path)

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda e: e.update(source="guess"),
            lambda e: e.update(materiality="huge"),
            lambda e: e.update(confidence="sure"),
            lambda e: e.update(user_confirmed="yes"),
            lambda e: e.pop("reason"),
        ],
    )
    def test_invalid_entries_are_rejected_on_load(self, tmp_path, mutation):
        ledger.save(tmp_path, _resolve())
        path = tmp_path / "assumptions.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutation(raw["entries"][0])
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(ledger.LedgerError):
            ledger.load(tmp_path)

    def test_duplicate_ids_are_rejected_on_load(self, tmp_path):
        ledger.save(tmp_path, _resolve())
        path = tmp_path / "assumptions.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["entries"][1]["id"] = raw["entries"][0]["id"]
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(ledger.LedgerError, match="duplicate"):
            ledger.load(tmp_path)


class TestInferenceRegexes:
    @pytest.mark.parametrize(
        "text",
        [
            "Enter above the previous close by 0.5 percent",
            "Skip the market close auction, 5 bars",
        ],
    )
    def test_close_as_a_noun_does_not_imply_bar_close(self, text):
        entry = _resolve(make_spec(entry=[text, "Enter at the next candle open"]))
        assert entry.get("timing.signal_evaluation").materiality == "high"

    @pytest.mark.parametrize(
        "text",
        ["5-min close > VWAP", "Candle closes above EMA20 by 1 percent", "bar close above 100"],
    )
    def test_real_close_wording_implies_bar_close(self, text):
        entry = _resolve(make_spec(entry=[text, "Enter at the next candle open"]))
        assert entry.get("timing.signal_evaluation").materiality == "medium"

    def test_a_price_tick_is_not_intrabar_evaluation(self):
        s = make_spec(entry=["Stop 1 tick above the high", "5-min close > VWAP"])
        assert _resolve(s).get("timing.signal_evaluation").value == "bar_close"

    @pytest.mark.parametrize("text", ["Only one trade per day", "only one position per symbol"])
    def test_one_per_something_is_not_a_global_scope(self, text):
        s = make_spec(portfolio={"mode": "single_position"}, filters=[text + ", 1 max"])
        assert _resolve(s).get("portfolio.scope").value == "per_symbol"


class TestMergeStrictness:
    def test_confirmation_does_not_survive_a_materiality_change(self):
        medium = make_spec()
        first = _resolve(medium)
        confirmed = ledger.confirm(first, [e.id for e in first.entries])
        assert confirmed.get("timing.signal_evaluation").materiality == "medium"
        vague = make_spec(**AMBIGUOUS_TIMING)
        again = _resolve(vague, existing=confirmed)
        entry = again.get("timing.signal_evaluation")
        assert entry.value == "bar_close" and entry.materiality == "high"
        assert entry.user_confirmed is False


class TestImports:
    def test_ledger_imports_first_in_a_fresh_interpreter(self):
        import subprocess
        import sys

        done = subprocess.run(
            [sys.executable, "-c", "import signal_engine.research.intake.ledger"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr


class TestSealedHighWater:
    def _seal(self, folder, run_id, ids, next_id):
        import json

        run = folder / "runs" / run_id
        run.mkdir(parents=True)
        entries = [{"id": i, "field": f"f{n}"} for n, i in enumerate(ids)]
        (run / "assumptions.json").write_text(
            json.dumps(
                {"sealed_ist": "2026-10-02T14:00:00+05:30", "next_id": next_id, "entries": entries}
            ),
            encoding="utf-8",
        )

    def test_no_runs_gives_one(self, tmp_path):
        assert ledger.sealed_next_id(tmp_path) == 1

    def test_is_above_every_id_and_next_id_in_every_snapshot(self, tmp_path):
        self._seal(tmp_path, "2026-10-02-x-v1", ["A-001", "A-050"], 51)
        self._seal(tmp_path, "2026-10-02-x-v2", ["A-002", "A-007"], 60)
        assert ledger.sealed_next_id(tmp_path) == 60

    def test_ids_above_next_id_still_count(self, tmp_path):
        self._seal(tmp_path, "r1", ["A-090"], 5)
        assert ledger.sealed_next_id(tmp_path) == 91

    def test_unreadable_snapshot_raises(self, tmp_path):
        run = tmp_path / "runs" / "r1"
        run.mkdir(parents=True)
        (run / "assumptions.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ledger.LedgerError, match="snapshot"):
            ledger.sealed_next_id(tmp_path)

    def test_stale_temp_run_directories_are_skipped(self, tmp_path):
        self._seal(tmp_path, "2026-10-02-x-v1", ["A-003"], 4)
        stale = tmp_path / "runs" / ".2026-10-02-x-v2.tmp-abc123"
        stale.mkdir()
        (stale / "assumptions.json").write_text("{half written", encoding="utf-8")
        assert ledger.sealed_next_id(tmp_path) == 4

    def test_resolve_floor_keeps_new_ids_above_sealed_ids(self, tmp_path):
        led = ledger.resolve(make_spec(), floor_next_id=60)
        ids = [int(e.id[2:]) for e in led.entries]
        assert min(ids) == 60 and led.next_id == max(ids) + 1

    def test_deleted_working_ledger_cannot_restart_below_a_sealed_id(self, tmp_path):
        from signal_engine.research.intake import freeze
        from signal_engine.tests.intake_fixtures import write_spec

        write_spec(tmp_path)
        first = freeze.freeze(tmp_path, register_version=False)
        top = max(int(e.id[2:]) for e in first.ledger)
        self._seal(tmp_path, "2026-10-02-x-v1", [e.id for e in first.ledger], top + 1)
        again = freeze.freeze(tmp_path, register_version=False)
        assert min(int(e.id[2:]) for e in again.ledger) > top

    def test_cli_resolve_respects_the_floor(self, tmp_path):
        from signal_engine.research.intake import cli
        from signal_engine.tests.intake_fixtures import write_spec

        folder = tmp_path / "research" / "ideas" / "idea-012-sample"
        write_spec(folder)
        self._seal(folder, "r1", ["A-040"], 41)
        assert cli.main(["resolve", "IDEA-012"], root=tmp_path) == 0
        saved = ledger.load(folder)
        assert min(int(e.id[2:]) for e in saved.entries) >= 41


class TestEligibilityEntries:
    def test_eligibility_defaults_are_ledger_entries(self):
        led = _resolve()
        for name, value in (
            ("execution.eligibility_min_prior_sessions", 60),
            ("execution.eligibility_min_volume", 1),
            ("execution.eligibility_min_price", 10.0),
        ):
            entry = led.get(name)
            assert entry.value == value
            assert entry.source == "platform_default" and entry.section == "execution"
            assert entry.materiality == "medium" and entry.user_confirmed is False
            assert entry.reason.strip()
