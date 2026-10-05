"""Intraday recovery planning and the recovery ledger (INTRADAY-5M-DATASET-PLAN.md section 5)."""

from datetime import date

import pandas as pd

from signal_engine.backtest.data_readiness import intraday_plan as ip
from signal_engine.backtest.data_readiness import intraday_recovery as ir

TS = 1704167100  # 2024-01-02 09:15 IST


# ---------------------------------------------------------------- planning

def test_windows_are_fixed_29_day_grid_blocks():
    a, b = ip.block(date(2017, 1, 1)), ip.block(date(2017, 1, 29))
    assert a == (date(2017, 1, 1), date(2017, 1, 29)) and b == a
    assert ip.block(date(2017, 1, 30)) == (date(2017, 1, 30), date(2017, 2, 27))
    sess = pd.to_datetime(["2017-01-02", "2017-01-03", "2017-02-01"])
    assert ip.windows(sess) == [a, (date(2017, 1, 30), date(2017, 2, 27))]
    assert all((e - s).days == 28 for s, e in ip.windows(pd.bdate_range("2017", "2026")))


def _classes():
    ref = pd.DataFrame({"symbol": ["AAA"] * 5 + ["EMAMILTD"],
                        "session": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03",
                                                   "2024-01-04", "2024-01-05", "2024-01-02"])})
    legacy = pd.DataFrame({
        "symbol": ["AAA"] * 4 + ["EMAMILTD"],
        "session": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04",
                                   "2024-01-02"]),
        "status": ["converted", "excluded:volume", "converted", "excluded:volume", "converted"],
        "structure_ok": [True, True, True, True, True],
        "contaminated": [False, True, False, False, False],
        "negative_volume": [False, False, True, False, False]})
    return ip.classify(ref, legacy)


def test_classification_follows_the_cleanup_policy():
    c = _classes()
    got = dict(zip(zip(c.symbol, c.session.dt.day, strict=False), c.cls, strict=False))
    assert got[("AAA", 1)] == ip.LEGACY
    assert got[("AAA", 2)] == ip.RECOVER_CONTAMINATED       # contaminated and failing
    assert got[("AAA", 3)] == ip.RECOVER_NEGVOL
    assert got[("AAA", 4)] == ip.LEGACY                     # clean but failing: excluded later
    assert got[("AAA", 5)] == ip.RECOVER_GAP
    assert got[("EMAMILTD", 2)] == ip.RECOVER_REPLACED


def test_muhurat_sessions_are_out_of_scope_and_never_requested():
    ref = pd.DataFrame({"symbol": "AAA", "session": pd.to_datetime(["2024-11-01", "2024-11-04"])})
    legacy = pd.DataFrame({"symbol": pd.Series([], dtype=object), "session": pd.to_datetime([]),
                           "status": pd.Series([], dtype=object), "structure_ok": [],
                           "contaminated": [], "negative_volume": []})
    c = ip.classify(ref, legacy)
    assert list(c.cls) == [ip.OUT_OF_SCOPE_MUHURAT, ip.RECOVER_GAP]
    assert list(ip.recovery_requests(c).start) == [ip.block(date(2024, 11, 4))[0]]


def test_requests_are_deterministic_and_deduplicated():
    a, b = ip.build_requests(_classes()), ip.build_requests(_classes())
    pd.testing.assert_frame_equal(a, b)
    assert a.request_id.is_unique
    assert a[a.symbol == "AAA"].purpose.str.contains("recovery").any()


def test_provenance_sample_has_a_window_per_symbol_year_and_regime():
    ref = pd.DataFrame({"symbol": "AAA", "session": pd.bdate_range("2020-01-01", "2020-12-31")})
    legacy = ref.assign(status="converted", structure_ok=True, contaminated=False,
                        negative_volume=False)
    req = ip.provenance_requests(ip.classify(ref, legacy))
    assert "regime:2020-03 COVID crash" in set(req.stratum)
    years = req[req.stratum.str.startswith("year:")]
    assert set(years.stratum) == {"year:2019", "year:2020"}   # the block holding 2020-01-01
    for r in years.itertuples():
        assert (r.end - r.start).days == 28 and f"year:{r.start.year}" == r.stratum


def test_provenance_draw_does_not_depend_on_classification():
    ref = pd.DataFrame({"symbol": "AAA", "session": pd.bdate_range("2020-01-01", "2020-12-31")})
    legacy = ref.assign(status="converted", structure_ok=True, contaminated=False,
                        negative_volume=False)
    a = ip.provenance_requests(ip.classify(ref, legacy))
    worse = legacy.assign(status=["excluded:volume"] * 50 + ["converted"] * (len(legacy) - 50),
                          contaminated=True)
    b = ip.provenance_requests(ip.classify(ref, worse))
    pd.testing.assert_frame_equal(a, b)


# ---------------------------------------------------------------- recovery ledger

def _build(tmp_path, n=3):
    build = tmp_path / "build"
    build.mkdir()
    pd.DataFrame({"request_id": [f"r{i}" for i in range(n)], "symbol": "AAA",
                  "start": date(2024, 1, 2), "end": date(2024, 1, 30), "purpose": "recovery",
                  "stratum": "recover:gap"}).to_parquet(build / "requests.parquet")
    return build


def _fetcher(script):
    calls = []

    def fetch(symbol, start, end):
        calls.append(symbol)
        outcome = script[len(calls) - 1]
        if outcome == "raise":
            raise ConnectionError("reset")
        if outcome == "error":
            return False, {"message": "rate limited"}, 403
        rows = [] if outcome == "empty" else [{"timestamp": TS, "open": 1, "high": 1, "low": 1,
                                               "close": 1, "volume": 5}]
        return True, {"status": "success", "data": rows}, 200

    return fetch, calls


def test_every_outcome_is_a_ledger_line_and_responses_are_hashed(tmp_path):
    build = _build(tmp_path)
    fetch, _ = _fetcher(["ok", "empty", "error"])
    counts = ir.run(build, fetch, landing=tmp_path / "landing", pause=0)
    assert counts == {"ok": 1, "empty": 1, "error": 1}
    ledger = ir.read_ledger(build)
    assert list(ledger.outcome) == ["ok", "empty", "error"]
    assert ledger.iloc[0].sessions == ["2024-01-02"] and ledger.iloc[0].n_bars == 1
    assert "rate limited" in ledger.iloc[2].message
    body = ir.load_response(ledger.iloc[0].path)
    assert body["request"]["request_id"] == "r0" and body["response"]["data"][0]["volume"] == 5


def test_rerun_retries_only_errors_and_stops_after_max_attempts(tmp_path):
    build = _build(tmp_path)
    fetch, _ = _fetcher(["ok", "raise", "error", "error", "error", "error"])
    for _ in range(4):
        ir.run(build, fetch, landing=tmp_path / "landing", pause=0)
    ledger = ir.read_ledger(build)
    assert ledger[ledger.request_id == "r0"].shape[0] == 1
    assert ledger[ledger.request_id == "r1"].attempt.max() == ir.MAX_ATTEMPTS
    assert "exception: ConnectionError" in ledger.iloc[1].message
    assert any("r1 has no final outcome" in e for e in ir.verify(build))


def test_landing_bytes_are_deterministic_and_tampering_is_detected(tmp_path):
    build = _build(tmp_path, n=1)
    fetch, _ = _fetcher(["ok", "ok"])
    ir.run(build, fetch, landing=tmp_path / "landing", pause=0)
    first = ir.read_ledger(build).iloc[0]
    line = ir.fetch_one(fetch, {"request_id": "r0", "symbol": "AAA", "start": "2024-01-02",
                                "end": "2024-01-30"}, 2, tmp_path / "landing")
    assert line["sha256"] == first.sha256
    assert ir.verify(build) == []
    (tmp_path / "landing" / "r0.json.gz").write_bytes(b"x")
    assert ir.verify(build) == ["landing file of r0 missing or changed"]
