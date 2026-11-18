"""Live-shape Flattrade TradeBook fixture (masked) through the real parser and dedup.

Captured from a real connected account on 2026-11-17 (5 rows, 4 orders, one order with a
partial fill). flid, norenordno, uid/actid and exchordid are replaced by consistent
synthetic ids; prices, quantities, times and symbols are as received.
The second live read, 60+ s later, was byte-identical, so the "second call" payload below
is the same rows with SYNTHETIC extra rows appended / inserted.

Run with: uv run pytest test/test_flattrade_tradebook_live_shape.py -q
"""

import copy
import random
import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from broker.flattrade.mapping import order_data as raw
from services.discretionary_ledger_service import assign_dedup, validate_fill


@pytest.fixture(autouse=True)
def _symbol_lookup():
    with patch.object(
        raw, "get_oa_symbol", side_effect=lambda sym, exch: {"TCS-EQ": "TCS"}.get(sym)
    ):
        yield


LIVE_ROWS = [
    {
        "stat": "Ok",
        "norenordno": "261117000001",
        "uid": "ACCT01",
        "actid": "ACCT01",
        "exch": "NSE",
        "prctyp": "LMT",
        "ret": "DAY",
        "s_prdt_ali": "MIS",
        "prd": "I",
        "flid": "900000001",
        "fltm": "17-11-2026 13:05:39",
        "trantype": "S",
        "tsym": "TCS-EQ",
        "qty": "45",
        "token": "11536",
        "fillshares": "45",
        "flqty": "45",
        "mult": "1",
        "pp": "2",
        "ls": "1",
        "ti": "0.10",
        "prc": "2419.20",
        "prcftr": "1.000000",
        "flprc": "2419.20",
        "norentm": "13:05:39 17-11-2026",
        "avgprc": "2419.20",
        "exch_tm": "17-11-2026 13:05:39",
        "exchordid": "1300000000000001",
    },
    {
        "stat": "Ok",
        "norenordno": "261117000002",
        "uid": "ACCT01",
        "actid": "ACCT01",
        "exch": "NSE",
        "prctyp": "LMT",
        "ret": "DAY",
        "s_prdt_ali": "MIS",
        "prd": "I",
        "flid": "900000002",
        "fltm": "17-11-2026 12:44:30",
        "trantype": "B",
        "tsym": "TCS-EQ",
        "qty": "10",
        "token": "11536",
        "fillshares": "10",
        "flqty": "10",
        "mult": "1",
        "pp": "2",
        "ls": "1",
        "ti": "0.10",
        "prc": "2425.00",
        "prcftr": "1.000000",
        "flprc": "2425.00",
        "norentm": "12:44:30 17-11-2026",
        "avgprc": "2425.00",
        "exch_tm": "17-11-2026 12:44:30",
        "exchordid": "1300000000000002",
    },
    {
        "stat": "Ok",
        "norenordno": "261117000003",
        "uid": "ACCT01",
        "actid": "ACCT01",
        "exch": "NSE",
        "prctyp": "LMT",
        "ret": "DAY",
        "s_prdt_ali": "MIS",
        "prd": "I",
        "flid": "900000003",
        "fltm": "17-11-2026 12:24:49",
        "trantype": "B",
        "tsym": "TCS-EQ",
        "qty": "10",
        "token": "11536",
        "fillshares": "10",
        "flqty": "10",
        "mult": "1",
        "pp": "2",
        "ls": "1",
        "ti": "0.10",
        "prc": "2431.50",
        "prcftr": "1.000000",
        "flprc": "2431.50",
        "norentm": "12:24:49 17-11-2026",
        "avgprc": "2431.50",
        "exch_tm": "17-11-2026 12:24:49",
        "exchordid": "1300000000000003",
    },
    {
        "stat": "Ok",
        "norenordno": "261117000004",
        "uid": "ACCT01",
        "actid": "ACCT01",
        "exch": "NSE",
        "prctyp": "LMT",
        "ret": "DAY",
        "s_prdt_ali": "MIS",
        "prd": "I",
        "flid": "900000004",
        "fltm": "17-11-2026 12:11:32",
        "trantype": "B",
        "tsym": "TCS-EQ",
        "qty": "25",
        "token": "11536",
        "fillshares": "25",
        "flqty": "23",
        "mult": "1",
        "pp": "2",
        "ls": "1",
        "ti": "0.10",
        "prc": "2433.50",
        "prcftr": "1.000000",
        "flprc": "2433.50",
        "norentm": "12:11:32 17-11-2026",
        "avgprc": "2433.50",
        "exch_tm": "17-11-2026 12:11:32",
        "exchordid": "1300000000000004",
    },
    {
        "stat": "Ok",
        "norenordno": "261117000004",
        "uid": "ACCT01",
        "actid": "ACCT01",
        "exch": "NSE",
        "prctyp": "LMT",
        "ret": "DAY",
        "s_prdt_ali": "MIS",
        "prd": "I",
        "flid": "900000005",
        "fltm": "17-11-2026 12:11:30",
        "trantype": "B",
        "tsym": "TCS-EQ",
        "qty": "25",
        "token": "11536",
        "fillshares": "2",
        "flqty": "2",
        "mult": "1",
        "pp": "2",
        "ls": "1",
        "ti": "0.10",
        "prc": "2433.50",
        "prcftr": "1.000000",
        "flprc": "2433.50",
        "norentm": "12:11:30 17-11-2026",
        "avgprc": "2433.50",
        "exch_tm": "17-11-2026 12:11:30",
        "exchordid": "1300000000000004",
    },
]


def _validated(fills):
    return [v for v in (validate_fill(f, "flattrade") for f in fills) if v is not None]


def _fills(rows):
    fills, skipped = raw.map_trade_fills(rows)
    return _validated(fills), skipped


def test_real_shape_parses_without_skips():
    rows = raw._fill_rows(copy.deepcopy(LIVE_ROWS))
    fills, skipped = _fills(rows)
    assert skipped == 0 and len(fills) == len(LIVE_ROWS)
    assert {f["fill_id"] for f in fills} == {r["flid"] for r in LIVE_ROWS}
    f = next(f for f in fills if f["fill_id"] == LIVE_ROWS[-1]["flid"])
    assert f["qty"] == float(LIVE_ROWS[-1]["flqty"]) and f["price"] == float(LIVE_ROWS[-1]["flprc"])
    assert f["fill_time"] == datetime(2026, 11, 17, 12, 11, 30)
    assert f["side"] == "BUY" and f["exchange"] == "NSE" and f["product"] == "MIS"


def test_partial_fills_of_one_order_are_separate_rows():
    fills, _ = _fills(LIVE_ROWS)
    by_order = {}
    for f in fills:
        by_order.setdefault(f["order_id"], []).append(f)
    partial = [v for v in by_order.values() if len(v) > 1]
    assert partial and all(len({f["fill_id"] for f in v}) == len(v) for v in partial)
    # fill quantities, not the order quantity, are used
    assert sorted(f["qty"] for f in partial[0]) == [2.0, 23.0]


def _synthetic_extra():
    """SYNTHETIC: two new fills, plus an identical clone of a live row without flid."""
    new = copy.deepcopy(LIVE_ROWS[0])
    new.update(flid="900000099", fltm="17-11-2026 14:00:00", norenordno="261117009999")
    new2 = copy.deepcopy(LIVE_ROWS[1])
    new2.update(flid="900000098", fltm="17-11-2026 14:01:00", norenordno="261117009998")
    return [new, new2]


def _store(conn, fills):
    conn.execute("create table if not exists t (k text primary key, flid text, q real)")
    for f in assign_dedup(fills):
        conn.execute(
            "insert or ignore into t values (?,?,?)", (f["dedup_key"], f["fill_id"], f["qty"])
        )


def _run(strip_flid, shuffle_seed):
    first = copy.deepcopy(LIVE_ROWS)
    second = _synthetic_extra() + copy.deepcopy(LIVE_ROWS)  # new rows inserted at the front
    if strip_flid:
        for r in first + second:
            r.pop("flid")
    conn = sqlite3.connect(":memory:")
    f1, s1 = _fills_noflid(first) if strip_flid else _fills(first)
    _store(conn, f1)
    rnd = random.Random(shuffle_seed)
    rnd.shuffle(second)
    f2, _ = _fills_noflid(second) if strip_flid else _fills(second)
    _store(conn, f2)
    n = conn.execute("select count(*) from t").fetchone()[0]
    return n, len(first) + 2


def _fills_noflid(rows):
    # map_trade_fills skips rows without flid; give it a placeholder then clear it
    # so the dedup key falls back to the no-flid tuple path.
    tmp = []
    for r in rows:
        r = dict(r)
        r["flid"] = "x"
        tmp.append(r)
    fills, skipped = raw.map_trade_fills(tmp)
    for f in fills:
        f["fill_id"] = ""
    return _validated(fills), skipped


def test_dedup_with_flid_stable_under_shuffle():
    for seed in range(20):
        n, expected = _run(False, seed)
        assert n == expected


def test_dedup_without_flid_stable_under_shuffle():
    # live rows have no identical fills, so tuple keys are unique per row.
    for seed in range(20):
        n, expected = _run(True, seed)
        assert n == expected


def test_no_flid_synthetic_identical_clone_keeps_both_and_is_idempotent():
    rows = copy.deepcopy(LIVE_ROWS)
    rows.append(copy.deepcopy(LIVE_ROWS[0]))  # SYNTHETIC identical clone
    conn = sqlite3.connect(":memory:")
    for _ in range(3):
        random.Random(_).shuffle(rows)
        f, _s = _fills_noflid(rows)
        _store(conn, f)
    assert conn.execute("select count(*) from t").fetchone()[0] == len(LIVE_ROWS) + 1
