"""executor._filled_since: when a stop found already triggered may be adopted after a timeout.

It parsed only the sandbox stamp ("2026-10-26 10:15:02"). Live Flattrade stamps are day-first
("10:15:02 26-10-2026"), so on a live book every already-triggered stop failed the check, was never
recognised, and a fresh stop could be placed on a position that stop had already closed. It now
uses the shared order_reconcile.parse_broker_stamp; an unreadable stamp still never matches.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from signal_engine.executor import _filled_since
from signal_engine.timeutils import IST

SINCE = datetime(2026, 10, 26, 10, 15, 0, tzinfo=IST)


def _row(stamp, key="timestamp"):
    return {key: stamp}


class TestFormats:
    def test_sandbox_stamp_after_the_send(self):
        assert _filled_since(_row("2026-10-26 10:15:02"), SINCE) is True

    def test_flattrade_time_first_stamp_after_the_send(self):
        assert _filled_since(_row("10:15:02 26-10-2026"), SINCE) is True

    def test_flattrade_date_first_stamp_after_the_send(self):
        assert _filled_since(_row("26-10-2026 10:15:02"), SINCE) is True

    def test_order_timestamp_key_is_read_too(self):
        assert _filled_since(_row("10:15:02 26-10-2026", key="order_timestamp"), SINCE) is True


class TestTimezone:
    def test_an_iso_stamp_in_utc_is_converted_to_ist(self):
        # 04:45:02 UTC is 10:15:02 IST: after the send.
        assert _filled_since(_row("2026-10-26T04:45:02+00:00"), SINCE) is True

    def test_an_iso_utc_stamp_before_the_send_in_ist_is_not_ours(self):
        assert _filled_since(_row("2026-10-26T04:40:00Z"), SINCE) is False

    def test_since_in_another_zone_compares_by_instant(self):
        since_utc = SINCE.astimezone(UTC)
        assert _filled_since(_row("10:15:02 26-10-2026"), since_utc) is True


class TestNotOurs:
    def test_an_earlier_fill_of_the_day_is_not_adopted(self):
        assert _filled_since(_row("09:40:00 26-10-2026"), SINCE) is False

    @pytest.mark.parametrize("stamp", ["garbage", "26/10/2026 10:15", "", None])
    def test_a_malformed_or_missing_stamp_never_matches(self, stamp):
        assert _filled_since(_row(stamp), SINCE) is False


class TestDelayedFill:
    def test_a_stop_that_fired_minutes_after_the_send_is_adopted(self):
        later = (SINCE + timedelta(minutes=7)).strftime("%H:%M:%S %d-%m-%Y")
        assert _filled_since(_row(later), SINCE) is True

    def test_a_fill_stamped_in_the_send_second_is_adopted(self):
        assert _filled_since(_row("10:15:00 26-10-2026"), SINCE) is True
