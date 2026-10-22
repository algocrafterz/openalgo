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
                   _ledger_rows([0], status="unresolved"),
                   _ledger_rows([99], volume_ok=False)], ignore_index=True)
    s.loc[2, "defect_type"] = "volume"
    q = ib.quality_table(s, 98)
    # row 1 is sparse (99 > N = 98) with complete volume: kept and flagged (P2);
    # row 4 is sparse with incomplete volume: still excluded as partial
    assert list(q.price_valid) == [True, True, True, False, False]
    assert list(q.sparse_complete) == [False, True, False, False, False]
    assert list(q.volume_valid) == [True, True, False, False, False]
    assert list(q.price_status) == [ic.CONVERTED, ic.CONVERTED, ic.CONVERTED, ic.UNRESOLVED,
                                    "excluded:partial_session"]
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


def test_canonical_bars_are_written_one_file_per_symbol_and_year(tmp_path):
    from signal_engine.backtest.data_readiness import intraday_build as ib

    bars = pd.DataFrame({
        "symbol": ["AAA"] * 3,
        "session": pd.to_datetime(["2023-12-29", "2024-01-02", "2024-01-02"]),
        "timestamp": [1703841900, 1704167100, 1704167160],
        "open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3, "close": [1.0] * 3,
        "volume": [1.0, 2.0, 3.0]})
    ib.write_by_year(bars, tmp_path / "one" / "AAA")
    assert sorted(p.name for p in (tmp_path / "one" / "AAA").iterdir()) == [
        "2023.parquet", "2024.parquet"]
    assert len(pd.read_parquet(tmp_path / "one" / "AAA" / "2024.parquet")) == 2


# ---------------------------------------------------------------- post-3-Aug source choice (P3a)

CAS1, CAS2 = date(2026, 8, 4), date(2026, 8, 5)


def _ts(day, minute, second=0):
    return int(datetime(day.year, day.month, day.day, minute // 60, minute % 60, second,
                        tzinfo=IST).timestamp())


def _cas(day, vol=100, auction=True, extras=(), symbol="AAA"):
    """A regular session 09:15-15:14 at the 100 / 110 / 90 pattern, then the closing window."""
    rows = []
    for m in range(555, 915):
        rows.append((m, 100.0, 110.0 if m == 600 else 100.0, 90.0 if m == 660 else 100.0,
                     100.0, vol))
    if auction:
        rows.append((929, 100.0, 100.0, 100.0, 100.0, 5))
    rows += list(extras)
    return pd.DataFrame([{"symbol": symbol, "timestamp": _ts(day, m), "open": o, "high": h,
                          "low": low, "close": c, "volume": v} for m, o, h, low, c, v in rows])


def _unify(legacy, fetched, classes, ref, requests=None):
    return ib._unify("AAA", legacy, classes, fetched, requests or _requests(), ref, T)


def _origin(bars, day):
    return bars[bars.session == pd.Timestamp(day)].origin.iloc[0]


def test_both_sources_pass_the_one_closer_to_nse_wins_and_a_tie_goes_to_legacy():
    ref = _ref(CAS1, CAS2)                         # raw_volume 37,500; 360 bars
    classes = _classes({CAS1: ip.LEGACY, CAS2: ip.LEGACY})
    legacy = pd.concat([_cas(CAS1, vol=104), _cas(CAS2, vol=100)])       # 0.9984 and 0.96
    fetched = ic.with_clock(pd.concat([_cas(CAS1, vol=100), _cas(CAS2, vol=100)]))
    bars, sess, _, _ = _unify(legacy, fetched, classes, ref)
    assert _origin(bars, CAS1) == "legacy"          # 0.9984 is closer to 1 than 0.96
    assert _origin(bars, CAS2) == "legacy"          # an exact tie goes to legacy
    choice = dict(zip(sess.session.dt.date, sess.source_choice, strict=True))
    assert choice[CAS1] == "both_pass:legacy_closer" and choice[CAS2] == "both_pass:tie_legacy"
    closer = _unify(pd.concat([_cas(CAS1, vol=96)]), ic.with_clock(_cas(CAS1, vol=104)),
                    _classes({CAS1: ip.LEGACY}), _ref(CAS1))
    assert _origin(closer[0], CAS1) == "recovered"


def test_the_source_that_passes_is_used_when_only_one_does():
    ref, classes = _ref(CAS1, CAS2), _classes({CAS1: ip.LEGACY, CAS2: ip.LEGACY})
    # CAS1: legacy has a negative volume inside the regular window; CAS2: fetched has two prints
    bad_legacy = _cas(CAS1, extras=[(700, 100.0, 100.0, 100.0, 100.0, -5)])
    two_prints = _cas(CAS2, extras=[(928, 100.0, 100.0, 100.0, 100.0, 5)])
    legacy = pd.concat([bad_legacy, _cas(CAS2)])
    fetched = ic.with_clock(pd.concat([_cas(CAS1), two_prints]))
    bars, sess, _, _ = _unify(legacy, fetched, classes, ref)
    assert _origin(bars, CAS1) == "recovered" and _origin(bars, CAS2) == "legacy"
    choice = dict(zip(sess.session.dt.date, sess.source_choice, strict=True))
    assert choice == {CAS1: "recovered_only_passes", CAS2: "legacy_only_passes"}


def test_a_replaced_symbol_never_uses_its_legacy_bars():
    classes = _classes({CAS1: ip.RECOVER_REPLACED})
    bars, sess, _, _ = _unify(_cas(CAS1), ic.with_clock(_cas(CAS1)), classes, _ref(CAS1))
    assert _origin(bars, CAS1) == "recovered"
    assert sess.source_choice.iloc[0] == "single_source:recovered"


def test_closing_noise_is_quarantined_with_its_original_values():
    noise = [(916, 100.0, 100.0, 100.0, 100.0, 0), (920, 100.0, 100.0, 100.0, 100.0, -9_000_000),
             (928, 100.0, 100.0, 100.0, 100.0, 51_000)]
    bars, sess, _, quarantine = _unify(_cas(CAS1, extras=noise), pd.DataFrame(), _classes(
        {CAS1: ip.LEGACY}), _ref(CAS1))
    assert bars.minute.max() == 914                 # legacy keeps nothing after the regular window
    assert sorted(quarantine.minute) == [916, 920, 928, 929]
    assert quarantine[quarantine.minute == 920].volume.item() == -9_000_000
    assert (quarantine.source == "legacy").all()
    row = sess.iloc[0]
    assert row.closing_noise_bars == 0              # already removed before the session summary
    assert row.structure_ok and row.status == ic.CONVERTED


def test_auction_fields_come_only_from_a_recovered_session():
    classes = _classes({CAS1: ip.RECOVER_REPLACED})
    _, sess, _, _ = _unify(pd.DataFrame(), ic.with_clock(_cas(CAS1)), classes, _ref(CAS1))
    row = sess.iloc[0]
    assert row.auction_price == 100.0 and row.auction_volume == 5
    _, legacy_sess, _, _ = _unify(_cas(CAS1), pd.DataFrame(), _classes({CAS1: ip.LEGACY}),
                                  _ref(CAS1))
    assert pd.isna(legacy_sess.auction_price.iloc[0])
    assert pd.isna(legacy_sess.auction_volume.iloc[0])


def test_a_session_fetched_on_the_day_itself_is_not_settled_and_is_not_used():
    ref, classes = _ref(CAS1, CAS2), _classes({CAS1: ip.RECOVER_REPLACED, CAS2: ip.LEGACY})
    fetched = ic.with_clock(pd.concat([_cas(CAS1), _cas(CAS2)]))
    fetched["requested_ist"] = [
        datetime(2026, 8, 4, 17, 10, tzinfo=IST).isoformat() if d == pd.Timestamp(CAS1)
        else datetime(2026, 8, 6, 9, 0, tzinfo=IST).isoformat() for d in fetched.session]
    bars, sess, _, _ = _unify(_cas(CAS2), fetched, classes, ref)
    status = dict(zip(sess.session.dt.date, sess.status, strict=True))
    assert status[CAS1] == "excluded:unsettled"      # same-evening Angel data is provisional
    assert (bars.session == pd.Timestamp(CAS2)).any() and _origin(bars, CAS2) == "legacy"
    assert not bool(sess.set_index("session").loc[pd.Timestamp(CAS1), "settled"])


def test_a_non_unit_factor_session_that_does_not_reconcile_is_flagged_not_excluded():
    s = _ledger_rows([0, 0, 0]).assign(non_unit_factor=[True, True, False],
                                       daily_reconciled=[True, False, False])
    q = ib.quality_table(s, 98)
    assert list(q.price_valid) == [True, True, True]          # the existing gates are unchanged
    assert list(q.ca_unreconciled) == [False, True, False]


def test_the_spec_two_columns_exist_and_have_types_without_corporate_action_inputs():
    q = ib.quality_table(_ledger_rows([0]), 98)
    for c in ("sparse_complete", "settled", "daily_reconciled", "ca_unreconciled",
              "fallback_conversion", "ca_demerger_approx"):
        assert q[c].dtype == bool, c
    for c in ("closing_noise_bars", "auction_price", "auction_volume"):
        assert c in q


def test_an_unsettled_session_carries_its_own_exclusion_reason():
    s = _ledger_rows([0]).assign(status="excluded:unsettled", origin=None)
    q = ib.quality_table(s, 98)
    assert q.price_status.item() == "excluded:unsettled"
    assert q.final_defect.item() == "unsettled_session"
    assert q.treatment.item() == "excluded:unsettled_session"
