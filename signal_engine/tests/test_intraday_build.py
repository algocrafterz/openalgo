"""Intraday unify step: session-level replacement, provenance, missing-minute policy (plan 5, 7.3)."""

from datetime import date, datetime

import numpy as np
import pandas as pd

from signal_engine.backtest.data_readiness import intraday_build as ib
from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import intraday_plan as ip
from signal_engine.timeutils import IST

T = ic.load_templates()
D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


def _session(day, price=100.0, vol=100, symbol="AAA", minutes=range(555, 930)):
    rows = []
    for m in minutes:
        ts = int(datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=IST).timestamp())
        h = 110.0 if m == 600 else 100.0
        low = 90.0 if m == 660 else 100.0
        rows.append({"symbol": symbol, "timestamp": ts, "open": 100.0 * price / 100,
                     "high": h * price / 100, "low": low * price / 100,
                     "close": 100.0 * price / 100, "volume": vol})
    return pd.DataFrame(rows)


def _ref(*days):
    return pd.DataFrame([{"symbol": "AAA", "session": pd.Timestamp(d), "raw_open": 100.0,
                          "raw_high": 110.0, "raw_low": 90.0, "raw_volume": 37500.0,
                          "price_factor": 1.0, "volume_factor": 1.0, "ref_close": 100.0,
                          "raw_close": 100.0}
                         for d in days])


def _classes(mapping):
    return pd.DataFrame([{"symbol": "AAA", "session": pd.Timestamp(d), "cls": c,
                          "defect_type": None, "contaminated": False}
                         for d, c in mapping.items()])


def _requests(*rows):
    return pd.DataFrame([{"request_id": r, "symbol": "AAA", "start": a, "end": b, "purpose": p,
                          "stratum": s} for r, a, b, p, s in rows],
                        columns=["request_id", "symbol", "start", "end", "purpose", "stratum"])


def test_recovered_session_replaces_every_legacy_bar_of_that_session():
    legacy = pd.concat([_session(D1), _session(D2, minutes=range(555, 700))])
    fetched = ic.with_clock(_session(D2, vol=100))
    classes = _classes({D1: ip.LEGACY, D2: ip.RECOVER_GAP})
    bars, sess, _ = ib.unify_symbol("AAA", legacy, classes, fetched, _requests(), _ref(D1, D2), T)
    per = bars.groupby("session").origin.agg(set)
    assert per[pd.Timestamp(D1)] == {"legacy"} and per[pd.Timestamp(D2)] == {"recovered"}
    assert (bars.session == pd.Timestamp(D2)).sum() == 375
    assert set(sess.status) == {ic.CONVERTED}


def test_recovery_failure_is_missing_and_muhurat_is_out_of_scope():
    classes = _classes({D1: ip.RECOVER_GAP, D2: ip.OUT_OF_SCOPE_MUHURAT})
    empty = pd.DataFrame(columns=ib._BAR_COLS)
    _, sess, _ = ib.unify_symbol("AAA", empty, classes, empty, _requests(), _ref(D1, D2), T)
    assert dict(zip(sess.session.dt.date, sess.status, strict=False)) == {
        D1: "missing", D2: ip.OUT_OF_SCOPE_MUHURAT}


def test_provenance_mismatch_replaces_the_legacy_session_and_exact_keeps_it():
    legacy = pd.concat([_session(D1), _session(D2), _session(D3)])
    fresh = ic.with_clock(pd.concat([_session(D1), _session(D2, price=50), _session(D3, vol=101)]))
    classes = _classes({D1: ip.LEGACY, D2: ip.LEGACY, D3: ip.LEGACY})
    req = _requests(("p1", D1, D3, "provenance", "year:2024"))
    bars, _, prov = ib.unify_symbol("AAA", legacy, classes, fresh, req, _ref(D1, D2, D3), T)
    assert dict(zip(prov.session.dt.date, prov.result, strict=False)) == {D1: "exact", D2: "scaled",
                                                             D3: "mismatch"}
    origin = bars.groupby("session").origin.first()
    assert origin[pd.Timestamp(D1)] == "legacy"
    assert origin[pd.Timestamp(D2)] == "recovered:provenance"
    assert origin[pd.Timestamp(D3)] == "recovered:provenance"


def test_compare_session_outcomes():
    a = _session(D1)
    assert ib.compare_session(a, a.copy()) == "exact"
    assert ib.compare_session(a, a.iloc[:0]) == "fresh_missing"
    assert ib.compare_session(a, a.iloc[1:]) == "mismatch"
    scaled = a.assign(**{c: a[c] * 0.5 for c in ("open", "high", "low", "close")})
    assert ib.compare_session(a, scaled) == "scaled"


def _ledger_rows(missing, volume_ok=True, status=ic.CONVERTED):
    n = len(missing)
    return pd.DataFrame({"session": pd.Timestamp(D1), "status": status, "structure_ok": True,
                         "edge_ok": True, "volume_ok": volume_ok, "final_bar_convention": "both",
                         "missing_minutes": missing, "cls": ip.LEGACY,
                         "defect_type": None, "contaminated": False,
                         "origin": "legacy"}, index=range(n))


def test_missing_minute_threshold_is_p99_of_volume_valid_sessions_rounded_up_to_5():
    s = pd.concat([_ledger_rows(np.arange(100)), _ledger_rows([999], volume_ok=False)],
                  ignore_index=True)
    pol = ib.missing_minute_threshold(s)
    assert pol["population"] == 100 and pol["percentile_value"] == np.percentile(range(100), 99)
    assert pol["N"] == 100


def test_quality_table_flags_each_field_family_and_treatment():
    s = pd.concat([_ledger_rows([0, 99]), _ledger_rows([0], volume_ok=False),
                   _ledger_rows([0], status="unresolved")], ignore_index=True)
    s.loc[2, "defect_type"] = "volume"
    q = ib.quality_table(s, 98)
    assert list(q.price_valid) == [True, False, True, False]
    assert list(q.volume_valid) == [True, False, False, False]
    assert list(q.price_status) == [ic.CONVERTED, "excluded:partial_session", ic.CONVERTED,
                                    ic.UNRESOLVED]
    assert q.treatment[2] == "unresolved:irrecoverable_at_source"
    assert q.treatment[0] == "accepted"


def test_aligned_upl_session_is_recorded_as_repaired():
    day = date(2019, 6, 3)
    rows = []
    for m in range(555, 930):
        ts = int(datetime(day.year, day.month, day.day, (m - 1) // 60, (m - 1) % 60, 58,
                          tzinfo=IST).timestamp())
        rows.append({"symbol": "UPL", "timestamp": ts, "open": 100.0,
                     "high": 110.0 if m == 600 else 100.0, "low": 90.0 if m == 660 else 100.0,
                     "close": 100.0, "volume": 100})
    fetched = ic.with_clock(pd.DataFrame(rows))
    classes = _classes({day: ip.RECOVER_GAP}).assign(symbol="UPL")
    ref = _ref(day).assign(symbol="UPL")
    _, sess, _ = ib.unify_symbol("UPL", pd.DataFrame(columns=ib._BAR_COLS), classes, fetched,
                                 _requests(), ref, T)
    q = ib.quality_table(sess, 190)
    row = q.iloc[0]
    assert row.price_valid and row.timestamp_aligned
    assert row.treatment == ic.ALIGNED_TREATMENT and "58-58" in row.timestamp_basis
