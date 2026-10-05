"""Corporate-action layer: NSE announcement text -> factors -> backward adjustment (plan rev. 4, D23)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import corporate_actions as ca


def _ann(symbol, ex, subject, face="10"):
    return {"symbol": symbol, "exDate": pd.Timestamp(ex).strftime("%d-%b-%Y"), "subject": subject,
            "faceVal": face, "series": "EQ"}


class TestParse:
    @pytest.mark.parametrize("subject,kind,params", [
        ("Bonus 1:1", ca.BONUS, (1, 1)),
        ("Bonus 2:1", ca.BONUS, (2, 1)),
        ("Bonus 1:4", ca.BONUS, (1, 4)),
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
         ca.SPLIT, (10.0, 1.0)),
        ("Face Value Split (Sub-Division) - From Rs10/- Per Share To Re 1/- Per Share",
         ca.SPLIT, (10.0, 1.0)),
        ("Face Value Split (Sub-Division) - From Rs 5/- Per Share To Rs 2/- Per Share",
         ca.SPLIT, (5.0, 2.0)),
        ("Rights 1:15 @ Premium Rs 1247", ca.RIGHTS, (1, 15, 1247.0)),
        ("Rights 17:74@ Premium Rs 55 Per Share", ca.RIGHTS, (17, 74, 55.0)),
        ("Rights 3:26 @ Premium Rs 560/-", ca.RIGHTS, (3, 26, 560.0)),
        ("Demerger", ca.DEMERGER, ()),
    ])
    def test_price_affecting_subjects(self, subject, kind, params):
        p = ca.parse_subject(subject)
        assert (p.kind, p.params) == (kind, params)

    @pytest.mark.parametrize("subject", [
        "Dividend - Rs 6 Per Share", "Interim Dividend - Rs 10 Per Share",
        "Annual General Meeting", "Buy Back", "Interest Payment",
    ])
    def test_ignored_subjects(self, subject):
        assert ca.parse_subject(subject).kind == ca.IGNORED

    @pytest.mark.parametrize("subject", [
        "Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held",
        "Scheme Of Arrangement - Bonus Ncrps 4:1",
    ])
    def test_non_equity_bonus_is_a_documented_limitation(self, subject):
        assert ca.parse_subject(subject).kind == ca.NON_EQUITY

    @pytest.mark.parametrize("subject", ["Bonus one for one", "Consolidation Of Shares",
                                         "Capital Reduction", "Split something"])
    def test_price_affecting_but_unreadable_is_unresolved(self, subject):
        assert ca.parse_subject(subject).kind == ca.UNRESOLVED


class TestFactors:
    def _events(self, anns, closes=None, overrides=()):
        closes = closes or {}
        return ca.build_events(anns, prev_close=lambda s, d: closes.get((s, d)),
                               overrides=overrides)

    def test_known_events(self):
        events, unresolved = self._events([
            _ann("RELIANCE", "2024-10-28", "Bonus 1:1"),
            _ann("HAL", "2023-09-28", "Face Value Split (Sub-Division) - From Rs 10/- Per Share "
                 "To Rs 5/- Per Share"),
            _ann("NMDC", "2024-12-27", "Bonus 2:1", face="1"),
        ])
        f = {(e.symbol, e.kind): (e.price_factor, e.volume_factor) for e in events}
        assert f[("RELIANCE", ca.BONUS)] == (0.5, 0.5)
        assert f[("HAL", ca.SPLIT)] == (0.5, 0.5)
        assert f[("NMDC", ca.BONUS)] == pytest.approx((1 / 3, 1 / 3))
        assert unresolved == []

    def test_split_and_bonus_on_one_ex_date_multiply(self):
        events, _ = self._events([
            _ann("BAJAJFINSV", "2022-09-13", "Bonus 1:1", face="5"),
            _ann("BAJAJFINSV", "2022-09-13", "Face Value Split (Sub-Division) - From Rs 5/- Per "
                 "Share To Re 1/- Per Share", face="5"),
        ])
        table = ca.factor_table(events)
        assert table.loc[("BAJAJFINSV", date(2022, 9, 13)), "price_factor"] == pytest.approx(0.1)

    def test_rights_factor_uses_the_official_close_before_the_ex_date(self):
        c = 1500.0
        events, _ = self._events([_ann("RELIANCE", "2020-05-13", "Rights 1:15 @ Premium Rs 1247")],
                                 closes={("RELIANCE", date(2020, 5, 13)): c})
        terp = (15 * c + 1 * (10 + 1247)) / 16
        (e,) = events
        assert e.price_factor == pytest.approx(terp / c) and e.volume_factor == 1.0
        assert "TERP" in e.method

    def test_rights_above_the_market_price_has_no_effect(self):
        events, _ = self._events([_ann("X", "2021-01-21", "Rights 1:1 @ Premium Rs 500")],
                                 closes={("X", date(2021, 1, 21)): 100.0})
        assert events[0].price_factor == 1.0

    def test_rights_without_a_close_is_unresolved(self):
        events, unresolved = self._events([_ann("X", "2021-01-21", "Rights 1:4 @ Premium Rs 5")])
        assert events == [] and unresolved[0].symbol == "X"

    def test_demerger_without_official_prices_is_unresolved(self):
        events, unresolved = self._events([_ann("ITC", "2025-01-06", "Demerger")])
        assert events == [] and [u.kind for u in unresolved] == [ca.DEMERGER]

    def test_override_resolves_an_event(self):
        ov = [ca.Override("ITC", date(2025, 1, 6), ca.DEMERGER, 0.94, 1.0, "https://x/circular")]
        events, unresolved = self._events([_ann("ITC", "2025-01-06", "Demerger")], overrides=ov)
        assert unresolved == [] and events[0].price_factor == 0.94
        assert events[0].method == "override" and events[0].source == "https://x/circular"

    def test_unresolved_subject_is_reported(self):
        _, unresolved = self._events([_ann("X", "2022-01-03", "Consolidation Of Shares")])
        assert unresolved[0].subject == "Consolidation Of Shares"

    def test_dividends_and_non_equity_bonuses_produce_no_event(self):
        events, unresolved = self._events([
            _ann("RELIANCE", "2025-08-14", "Dividend - Rs 5.5 Per Share"),
            _ann("BRITANNIA", "2021-05-25", "Scheme Of Arangement- Bonus - 1 Debenture For 1 "
                 "Equity Share Held"),
        ])
        assert events == [] and unresolved == []


class TestBackwardAdjustment:
    def test_factor_applies_strictly_before_the_ex_date(self):
        ev = [ca.Event("A", date(2024, 1, 10), ca.BONUS, 0.5, 0.5, "ratio", "nse")]
        sessions = pd.DataFrame({"symbol": "A", "session": [date(2024, 1, 9), date(2024, 1, 10),
                                                            date(2024, 1, 11)]})
        out = ca.apply_factors(sessions, ev)
        assert list(out["price_factor"]) == [0.5, 1.0, 1.0]
        assert list(out["volume_factor"]) == [0.5, 1.0, 1.0]

    def test_factors_accumulate_backwards(self):
        ev = [ca.Event("A", date(2024, 1, 10), ca.BONUS, 0.5, 0.5, "ratio", "nse"),
              ca.Event("A", date(2024, 6, 10), ca.SPLIT, 0.2, 0.2, "ratio", "nse")]
        sessions = pd.DataFrame({"symbol": "A", "session": [date(2024, 1, 9), date(2024, 3, 1),
                                                            date(2024, 7, 1)]})
        out = ca.apply_factors(sessions, ev)
        assert list(out["price_factor"]) == pytest.approx([0.1, 0.2, 1.0])

    def test_other_symbols_are_untouched(self):
        ev = [ca.Event("A", date(2024, 1, 10), ca.BONUS, 0.5, 0.5, "ratio", "nse")]
        sessions = pd.DataFrame({"symbol": ["B"], "session": [date(2024, 1, 9)]})
        assert list(ca.apply_factors(sessions, ev)["price_factor"]) == [1.0]

    def test_factor_outside_zero_one_is_rejected(self):
        with pytest.raises(ca.CorporateActionError):
            ca.factor_table([ca.Event("A", date(2024, 1, 10), ca.SPLIT, 1.5, 1.5, "ratio", "nse")])


def test_version_changes_with_events():
    a = [ca.Event("A", date(2024, 1, 10), ca.BONUS, 0.5, 0.5, "ratio", "nse")]
    b = [ca.Event("A", date(2024, 1, 10), ca.BONUS, 1 / 3, 1 / 3, "ratio", "nse")]
    assert ca.events_version(a) != ca.events_version(b)
    assert ca.events_version(a) == ca.events_version(list(a))


# ---- review findings (2026-10-05) ----------------------------------------------------------

class TestReviewFindings:
    def _events(self, anns, closes=None):
        closes = closes or {}
        return ca.build_events(anns, prev_close=lambda s, d: closes.get((s, d)))

    def test_duplicate_announcements_apply_once(self):
        events, _ = self._events([_ann("RELIANCE", "2024-10-28", "Bonus 1:1"),
                                  _ann("RELIANCE", "2024-10-28", "Bonus  1:1")])
        assert len(events) == 1
        table = ca.factor_table(events)
        assert table.loc[("RELIANCE", date(2024, 10, 28)), "price_factor"] == 0.5

    def test_conflicting_announcements_of_one_kind_raise(self):
        with pytest.raises(ca.CorporateActionError, match="conflicting"):
            self._events([_ann("X", "2024-10-28", "Bonus 1:1"),
                          _ann("X", "2024-10-28", "Bonus 1:2")])

    def test_thousands_separator_in_the_premium(self):
        assert ca.parse_subject("Rights 3:25 @ Premium Rs 1,799/-").params == (3, 25, 1799.0)

    @pytest.mark.parametrize("subject,params", [
        ("Face Value Split (Sub-Division) - From Rs.10/- To Rs.2/-", (10.0, 2.0)),
        ("Face Value Split (Sub-Division) - From Re 1/- Per Share To Re 0.50/- Per Share",
         (1.0, 0.5)),
    ])
    def test_split_variants(self, subject, params):
        p = ca.parse_subject(subject)
        assert (p.kind, p.params) == (ca.SPLIT, params)

    def test_bonus_and_split_in_one_subject_is_unresolved(self):
        assert ca.parse_subject("Bonus 1:1 And Face Value Split From Rs 10/- To Rs 5/-").kind == \
            ca.UNRESOLVED

    def test_apply_factors_with_a_duplicated_index(self):
        ev = [ca.Event("A", date(2024, 1, 10), ca.BONUS, 0.5, 0.5, "ratio", "nse")]
        sessions = pd.DataFrame({"symbol": ["A", "A"], "session": [date(2024, 1, 9),
                                                                    date(2024, 1, 11)]},
                                index=[0, 0])
        assert list(ca.apply_factors(sessions, ev)["price_factor"]) == [0.5, 1.0]


# ---- real NSE subject variants (2026-10-05 build) ------------------------------------------

class TestRealSubjects:
    @pytest.mark.parametrize("subject,params", [
        ("Fv Splt Frm Rs 10 To Re 1", (10.0, 1.0)),
        ("Fv Splt Frm Rs 10 To Rs 2", (10.0, 2.0)),
    ])
    def test_abbreviated_split(self, subject, params):
        p = ca.parse_subject(subject)
        assert (p.kind, p.params) == (ca.SPLIT, params)

    @pytest.mark.parametrize("subject,params", [
        ("Rights 1:10 @ Prem Rs 197/- Per Share", (1, 10, 197.0)),
        ("Rights 87:38 @ Premium Of Rs 2.50 Per Share", (87, 38, 2.5)),
    ])
    def test_rights_premium_variants(self, subject, params):
        p = ca.parse_subject(subject)
        assert (p.kind, p.params) == (ca.RIGHTS, params)

    def test_compound_subject_is_split_into_its_parts(self):
        parts = ca.parse_announcement("Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per "
                                      "Share To Rs 2/- Per Share")
        assert [(p.kind, p.params) for p in parts] == [(ca.BONUS, (1, 1)), (ca.SPLIT, (10.0, 2.0))]

    def test_compound_subject_with_a_dividend_and_a_revision_note(self):
        parts = ca.parse_announcement(" Annual General Meeting/Dividend - Rs 2.50 Per Share/"
                                      "Bonus 1:10 (Revised)")
        assert [(p.kind, p.params) for p in parts if p.kind != ca.IGNORED] == [(ca.BONUS, (1, 10))]

    def test_compound_bonus_and_split_give_both_events(self):
        events, unresolved = ca.build_events(
            [_ann("BAJFINANCE", "2016-09-08", "Bonus 1:1/Face Value Split (Sub-Division) - From "
                  "Rs 10/- Per Share To Rs 2/- Per Share")], prev_close=lambda s, d: None)
        assert unresolved == []
        assert ca.factor_table(events).loc[("BAJFINANCE", date(2016, 9, 8)),
                                           "price_factor"] == pytest.approx(0.1)

    def test_scheme_of_arrangement_stays_unresolved(self):
        assert ca.parse_subject("Scheme Of Arrangement").kind == ca.UNRESOLVED


def test_announcement_without_an_ex_date():
    anns = [{"symbol": "X", "exDate": "-", "subject": "Dividend - Rs 2 Per Share", "faceVal": "1"},
            {"symbol": "Y", "exDate": "-", "subject": "Bonus 1:1", "faceVal": "1"}]
    events, unresolved = ca.build_events(anns, prev_close=lambda s, d: None)
    assert events == [] and [(u.symbol, u.reason) for u in unresolved] == [("Y", "no ex-date")]


@pytest.mark.parametrize("subject", [
    " Annual General Meeting/Div-Rs 1.75 Per Share",
    " Annual Genaral Meeting/ Dividend - Re 0.50/- Per Share (Book Closure Start Date Revised)",
    " Annual General Meeting/ Divdend - Re 0.50/- Per Share",
    " Annual General Meeting/ Change In Registrar And Transfer Agent",
])
def test_real_non_price_compound_subjects_are_ignored(subject):
    assert {p.kind for p in ca.parse_announcement(subject)} == {ca.IGNORED}


# ---- feed fetch (date-range queries; the dateless per-symbol query is truncated) -----------

class TestFetchFeed:
    def _fake(self, rows_by_month):
        calls = []

        def fetch(params):
            calls.append(params)
            return 200, rows_by_month.get(params["from_date"], [])
        return fetch, calls

    def test_one_query_per_month_and_rows_combined(self):
        fetch, calls = self._fake({
            "01-01-2018": [{"symbol": "TCS", "exDate": "15-Jan-2018", "subject": "Bonus 1:1"}],
            "01-03-2018": [{"symbol": "INFY", "exDate": "02-Mar-2018", "subject": "Bonus 1:1"}]})
        rows = ca.fetch_feed(date(2018, 1, 1), date(2018, 3, 31), fetch=fetch)
        assert [c["from_date"] for c in calls] == ["01-01-2018", "01-02-2018", "01-03-2018"]
        assert [c["to_date"] for c in calls] == ["31-01-2018", "28-02-2018", "31-03-2018"]
        assert {r["symbol"] for r in rows} == {"TCS", "INFY"}

    def test_identical_rows_from_overlapping_answers_are_kept_once(self):
        row = {"symbol": "TCS", "exDate": "15-Jan-2018", "subject": "Bonus 1:1"}
        fetch, _ = self._fake({"01-01-2018": [row, dict(row)]})
        assert len(ca.fetch_feed(date(2018, 1, 1), date(2018, 1, 31), fetch=fetch)) == 1

    def test_an_http_error_stops_the_fetch(self):
        with pytest.raises(ca.CorporateActionError, match="HTTP 403"):
            ca.fetch_feed(date(2018, 1, 1), date(2018, 1, 31), fetch=lambda p: (403, None))

    def test_snapshot_is_written_once_with_its_sha256(self, tmp_path):
        rows = [{"symbol": "TCS", "exDate": "15-Jan-2018", "subject": "Bonus 1:1"}]
        path, sha = ca.save_snapshot(rows, tmp_path / "ca.json")
        assert len(sha) == 64 and path.exists()
        with pytest.raises(ca.CorporateActionError, match="exists"):
            ca.save_snapshot(rows, tmp_path / "ca.json")


# ---- owner decisions 2026-10-05 ------------------------------------------------------------

def _closes_opens(prev, ex_open):
    return (lambda s, d: prev.get((s, d))), (lambda s, d: ex_open.get((s, d)))


class TestOwnerDecisions:
    def test_demerger_factor_is_the_ex_date_open_over_the_previous_close(self):
        pc, eo = _closes_opens({("RELIANCE", date(2023, 7, 20)): 2841.85},
                               {("RELIANCE", date(2023, 7, 20)): 2580.0})
        events, unresolved = ca.build_events([_ann("RELIANCE", "2023-07-20", "Demerger")],
                                             prev_close=pc, ex_open=eo)
        (e,) = events
        assert unresolved == [] and e.kind == ca.DEMERGER
        assert e.price_factor == pytest.approx(2580.0 / 2841.85) and e.volume_factor == 1.0
        assert "ex-date open" in e.method and "approximation" in e.method

    def test_demerger_open_above_the_previous_close_is_unresolved(self):
        pc, eo = _closes_opens({("X", date(2025, 1, 6)): 100.0}, {("X", date(2025, 1, 6)): 101.0})
        events, unresolved = ca.build_events([_ann("X", "2025-01-06", "Demerger")],
                                             prev_close=pc, ex_open=eo)
        assert events == [] and "not below" in unresolved[0].reason

    def test_scheme_of_arrangement_classified_as_a_spin_off_by_override(self):
        ov = [ca.Override("GRASIM", date(2017, 7, 19), ca.DEMERGER, None, None, "nse evidence",
                          match_kind=ca.UNRESOLVED, method=ca.OPEN_OVER_PREV_CLOSE)]
        pc, eo = _closes_opens({("GRASIM", date(2017, 7, 19)): 1200.0},
                               {("GRASIM", date(2017, 7, 19)): 1000.0})
        events, unresolved = ca.build_events([_ann("GRASIM", "2017-07-19", "Scheme Of Arrangement")],
                                             prev_close=pc, ex_open=eo, overrides=ov)
        (e,) = events
        assert unresolved == [] and e.kind == ca.DEMERGER
        assert e.price_factor == pytest.approx(1000 / 1200) and e.source == "nse evidence"

    def test_tatasteel_2018_rights_tranches_match_the_nse_circular(self):
        ov = [ca.Override("TATASTEEL", date(2018, 1, 31), ca.RIGHTS, None, None,
                          "https://nsearchives.nseindia.com/content/circulars/FAOP36862.pdf",
                          match_kind=ca.UNRESOLVED, method=ca.RIGHTS_TRANCHES,
                          params=(25, ((4, 510.0), (2, 615.0))))]
        pc, eo = _closes_opens({("TATASTEEL", date(2018, 1, 31)): 775.85}, {})
        subject = ("Rights - 4:25 Fully Paid Up Shares @ Premium Rs 500/- Per Share / 2:25 Partly "
                   "Paid Up Shares @ Premium Rs 605/- Per Share")
        events, unresolved = ca.build_events([_ann("TATASTEEL", "2018-01-31", subject)],
                                             prev_close=pc, ex_open=eo, overrides=ov)
        (e,) = events
        assert unresolved == [] and e.price_factor == pytest.approx(0.942411, abs=1e-6)
        assert e.volume_factor == 1.0 and "FAOP36862" in e.source

    def test_overrides_load_from_yaml(self, tmp_path):
        y = tmp_path / "ov.yaml"
        y.write_text("""
overrides:
  - symbol: TATASTEEL
    ex_date: 2018-01-31
    match_kind: unresolved
    kind: rights
    method: rights_tranches
    held: 25
    tranches: [[4, 510], [2, 615]]
    source: https://nsearchives.nseindia.com/content/circulars/FAOP36862.pdf
  - symbol: GRASIM
    ex_date: 2017-07-19
    match_kind: unresolved
    kind: demerger
    method: open_over_prev_close
    source: NSE feed and bhavcopy evidence
  - symbol: X
    ex_date: 2020-01-01
    kind: demerger
    method: fixed
    price_factor: 0.9
    volume_factor: 1.0
    source: circular
""")
        ov = ca.load_overrides(y)
        assert [o.method for o in ov] == [ca.RIGHTS_TRANCHES, ca.OPEN_OVER_PREV_CLOSE, ca.FIXED]
        assert ov[0].params == (25, ((4, 510.0), (2, 615.0))) and ov[0].match_kind == ca.UNRESOLVED
        assert ov[2].match_kind == ca.DEMERGER

    @pytest.mark.parametrize("bad", [
        "method: rights_tranches\n    source: s",            # no held/tranches
        "method: fixed\n    source: s",                       # no factors
        "method: magic\n    source: s",
        "method: open_over_prev_close\n    source: ''",
    ])
    def test_bad_overrides_fail_fast(self, tmp_path, bad):
        y = tmp_path / "ov.yaml"
        y.write_text(f"overrides:\n  - symbol: X\n    ex_date: 2020-01-01\n    kind: rights\n    {bad}\n")
        with pytest.raises(ca.CorporateActionError):
            ca.load_overrides(y)


def test_repository_overrides_file_loads():
    ov = {(o.symbol, o.ex_date): o for o in ca.load_overrides()}
    t = ov[("TATASTEEL", date(2018, 1, 31))]
    assert t.method == ca.RIGHTS_TRANCHES and "FAOP36862" in t.source
    assert {o.method for k, o in ov.items() if k[0] != "TATASTEEL"} == {ca.OPEN_OVER_PREV_CLOSE}
