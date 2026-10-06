"""The Rs 10 price floor tests the NSE raw close: factor access, run wiring, record validity."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from signal_engine.backtest import manifest as mf
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.data_readiness import intraday_access, snapshot
from signal_engine.tests import test_evidence_ledgers as tel
from signal_engine.tests.published_fixtures import frames_resolution, publish_fixture
from signal_engine.tests.test_data_session_integration import (
    SETTINGS,
    _Params,
    _Strat,
)

BONUS_DAYS = [dt.date(2024, 10, 24), dt.date(2024, 10, 25), dt.date(2024, 10, 28),
              dt.date(2024, 10, 29)]
BONUS_CLOSES = [2700.0, 2655.7, 1334.35, 1340.0]
ANN = [{"symbol": "AAA", "exDate": "28-Oct-2024", "subject": "Bonus 1:1", "faceVal": "10"}]
RUNS = Path(__file__).resolve().parents[1] / "research" / "ideas"


@pytest.fixture
def daily(tmp_path):
    def rows(s, i, d):
        return BONUS_CLOSES[i]

    return publish_fixture(tmp_path, ("AAA",), BONUS_DAYS, rows=rows, anns=ANN)


def _manifest(daily, root_hash=None, **drop):
    root, git_dir, vid, _ = daily
    real = snapshot.resolve_version(vid, root=root, git_dir=git_dir)
    contract = {"daily_version": vid, "daily_root_hash": root_hash or real["root_hash"]}
    for k in drop:
        contract.pop(k)
    return {"dataset_version": "nse-intraday-5m-test-001", "contract": contract}


def test_session_price_factors_reads_linked_daily_version(daily):
    root, git_dir, _, _ = daily
    out = intraday_access.session_price_factors(
        _manifest(daily), ["AAA", "ZZZ"], None, root=root, git_dir=git_dir
    )
    assert out["AAA"] == {
        BONUS_DAYS[0]: pytest.approx(0.5),
        BONUS_DAYS[1]: pytest.approx(0.5),
        BONUS_DAYS[2]: 1.0,
        BONUS_DAYS[3]: 1.0,
    }
    assert out["ZZZ"] == {}


def test_session_price_factors_honours_the_end_bound(daily):
    root, git_dir, _, _ = daily
    end_ts = int(pd.Timestamp(BONUS_DAYS[1], tz="Asia/Kolkata").timestamp()) + 86399
    out = intraday_access.session_price_factors(
        _manifest(daily), ["AAA"], end_ts, root=root, git_dir=git_dir
    )
    assert sorted(out["AAA"]) == BONUS_DAYS[:2]


def test_published_source_end_bound_is_the_until_date(daily):
    # record_run passes `until` as "YYYY-MM-DD 23:59:59"; the next session is not included.
    from types import SimpleNamespace

    from signal_engine.backtest.data_readiness.preflight import PublishedSource

    root, git_dir, _, _ = daily
    res = SimpleNamespace(manifest=_manifest(daily), root=root, git_dir=git_dir)
    out = PublishedSource(None, res).session_price_factors(["AAA"], "2024-10-25 23:59:59")
    assert sorted(out["AAA"]) == BONUS_DAYS[:2]


def test_session_price_factors_rejects_wrong_daily_root_hash(daily):
    root, git_dir, _, _ = daily
    with pytest.raises(snapshot.SnapshotError, match="recorded"):
        intraday_access.session_price_factors(
            _manifest(daily, root_hash="0" * 64), ["AAA"], None, root=root, git_dir=git_dir
        )


@pytest.mark.parametrize("missing", ["daily_version", "daily_root_hash"])
def test_session_price_factors_requires_the_pin(daily, missing):
    root, git_dir, _, _ = daily
    with pytest.raises(snapshot.SnapshotError, match="daily_version"):
        intraday_access.session_price_factors(
            _manifest(daily, **{missing: None}), ["AAA"], None, root=root, git_dir=git_dir
        )


def _bars(close, days):
    parts = []
    for d in days:
        idx = pd.date_range(f"{d} 09:15", periods=75, freq="5min", tz="Asia/Kolkata")
        parts.append(
            pd.DataFrame(
                {"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
                 "Volume": 1000.0},
                index=idx,
            )
        )
    return pd.concat(parts)


def test_execute_on_keeps_an_adjusted_penny_session_with_raw_above_floor(monkeypatch):
    from signal_engine.backtest import __main__ as cli

    days = [d.date() for d in pd.bdate_range("2025-01-06", "2025-01-17")]
    frames = {"ADA": _bars(6.0, days), "PEN": _bars(6.0, days)}
    factors = {"ADA": dict.fromkeys(days, 0.2), "PEN": dict.fromkeys(days, 1.0)}
    monkeypatch.setattr(cli, "_register", lambda: None)
    monkeypatch.setattr(cli, "REGISTRY", {"k": (_Strat, _Params, "intraday")})
    monkeypatch.setattr(rr, "random_entry_baseline_intraday", lambda *a, **k: None)
    monkeypatch.setattr(rr, "_cost_model_or_none", lambda kind, slip=None: tel.MODEL)
    res = frames_resolution(intraday=frames, factors=factors, eligible=["ADA", "PEN"])
    exe = rr.execute_backtest("k", dataset=res, settings=SETTINGS)
    ex = exe.ledgers["exclusions"]
    ne = ex[ex["reason_code"] == "NOT_ELIGIBLE"]
    assert set(ne["symbol"]) == {"PEN"}
    assert set(ne["detail"]) == {"PRICE_FLOOR"}
    m = exe.membership
    assert m["price_floor_basis"] == "nse_raw_last_bar_close"
    assert m["eligibility_policy_version"] == 2
    assert m["price_floor_source"]["field"] == "price_factor"
    assert m["price_floor_source"]["daily_version"] == "nse-daily-fake-001"


def test_committed_run_manifests_still_validate():
    folders = sorted(p.parent for p in RUNS.glob("*/runs/*/manifest.json"))
    assert folders, "no committed run manifests found"
    for folder in folders:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert mf.validate_manifest(manifest) == [], folder
        assert mf.verify_artifacts(folder) == [], folder
