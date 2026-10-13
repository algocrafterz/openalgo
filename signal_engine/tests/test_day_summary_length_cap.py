"""Telegram rejects messages over 4096 characters. On 2026-10-12 the consolidated day summary
(72 positions) was rejected with "Message was too long" and never delivered."""

from signal_engine.notifier import TELEGRAM_SAFE_LEN, _join_capped


class TestJoinCapped:
    def test_short_summary_is_unchanged(self):
        head = ["DAY SUMMARY", "-" * 36]
        rows = [f"row {i}" for i in range(5)]
        assert _join_capped(head, rows) == "\n".join(head + rows)

    def test_72_trade_summary_fits_and_says_how_many_were_dropped(self):
        head = ["DAY SUMMARY | 12-Oct-2026", "-" * 36]
        rows = [f"{i:02d} PAYTM       SHORT 93  1650.10 -> 1658.00  -Rs 735  (-0.5R)  TIME" for i in range(72)]
        text = _join_capped(head, rows)
        assert len(text) <= TELEGRAM_SAFE_LEN
        assert text.startswith("DAY SUMMARY | 12-Oct-2026")
        assert text.splitlines()[-1].startswith("... and ")
        kept = sum(1 for ln in text.splitlines() if ln[:2].isdigit())
        assert text.splitlines()[-1] == f"... and {72 - kept} more trade(s) not shown"
