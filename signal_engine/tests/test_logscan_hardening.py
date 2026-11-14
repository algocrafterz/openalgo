"""F4: log detector fixes: benign noise, dry-run slice, placeholders, secrets, two-letter tickers."""

from collections import Counter

import pytest

from signal_engine.bugs import store
from signal_engine.ops import logscan

pytestmark = pytest.mark.filterwarnings("ignore")
DAY = "2026-11-16"


def _counter(base):
    return lambda day: Counter(base)


# (1) benign noise ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sig",
    [
        "yfinance: $TCS.NS: possibly delisted; no price data found",
        "yfinance: NIFTYFPI.NS: quoteSummary failed for # symbols",
        "yfinance: HTTP Error #: quoteSummary request blocked",
    ],
)
def test_yfinance_noise_is_not_a_candidate_even_through_normalizing(tmp_path, sig):
    out = logscan.detect(DAY, counter=_counter({sig: 40}), path=str(tmp_path / "b.json"))
    assert out == []


def test_real_pipeline_failure_stays_a_true_positive(tmp_path):
    sigs = {f"pipeline: pipeline {s} bar #: evaluate failed": 3 for s in ("LT", "HAL", "BEL")}
    out = logscan.detect(DAY, counter=_counter(sigs), path=str(tmp_path / "b.json"))
    assert len(out) == 1 and "pipeline <SYM> bar" in out[0] and "x9" in out[0]


# (2) dry-run slice --------------------------------------------------------------------------


def test_dry_run_prints_the_title_without_losing_its_first_letter(tmp_path):
    out = logscan.detect(
        DAY, counter=_counter({"orders: boom happened": 6}), path=str(tmp_path / "b.json")
    )
    assert out[0].endswith(": orders: boom happened")


# (3) placeholders ---------------------------------------------------------------------------


def test_placeholders_are_not_masked_twice():
    once = logscan.normalize("a: failed for RELIANCE with id 1a2b3c4d5e6f7a8b")
    assert logscan.normalize(once) == once
    assert "<<" not in logscan.normalize("a: tok abcdefghijklmnopqrstuvwxyz1 sym RELIANCE")


# (4) secrets --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "auth: Authorization Bearer abc123SECRET failed",
        "auth: Authorization: Bearer abc123SECRET failed",
        "auth: Authorization Basic dXNlcjpwYXNz failed",
        "auth: bearer abc123SECRET failed",
    ],
)
def test_authorization_scheme_value_is_masked(text):
    for line in (logscan.mask_title(text), logscan.mask_evidence(text, 5, 1)):
        assert "abc123SECRET" not in line and "dXNlcjpwYXNz" not in line
        assert "<masked>" in line


# (5) two-letter tickers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "p: bad price for symbol LT today",
        "p: order for LT rejected",
        "p: ticker=LT not found",
        "p: instrument: LT missing",
        "p: position in LT closed",
        "p: failed for LT order",
    ],
)
def test_two_letter_ticker_is_masked_in_an_explicit_symbol_context(text):
    out = logscan.normalize(text)
    assert "<SYM>" in out and "LT" not in out.replace("<SYM>", "")


@pytest.mark.parametrize(
    "text",
    [
        "p: status OK for order accepted",
        "p: symbol ID missing",
        "p: for IP order retried",
        "p: DB error while saving",
        "p: failed LT bar",  # no explicit context, no universe: left alone
        "p: pipeline LT bar #: evaluate failed",
    ],
)
def test_two_letter_words_outside_a_symbol_context_or_in_the_stoplist_are_kept(text):
    assert logscan.normalize(text) == text


def test_ticker_variations_give_one_signature_and_it_is_stable_across_runs(tmp_path):
    path = str(tmp_path / "b.json")
    counter = _counter(
        {
            "p: order for LT rejected": 3,
            "p: order for RELIANCE rejected": 3,
            "p: order for M&M rejected": 3,
            "p: order for TCS rejected": 3,
        }
    )
    first = logscan.detect(DAY, dry_run=False, counter=counter, path=path)
    second = logscan.detect(DAY, dry_run=False, counter=counter, path=path)
    bugs = store.load(path)["bugs"]
    assert len(first) == 1 and first[0].startswith("created") and "x12" in first[0]
    assert len(second) == 1 and not second[0].startswith("created")
    assert len(bugs) == 1 and len(bugs[0]["sources"]) == 1
    assert logscan.source_key(logscan.normalize("p: order for LT rejected")) == logscan.source_key(
        logscan.normalize("p: order for INFY rejected")
    )
